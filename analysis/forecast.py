"""9단계 -- 30분 뒤 예측.

타깃은 30분 뒤의 **실측 절대값**(ppm, index)이다. 비율이나 요약지수를 타깃으로
삼으면 규칙층(8단계)의 임계와 단위가 달라져 "예측 CO2 > 1000 -> 선제 환기"로
이어지지 않는다.

모델은 능형회귀(ridge). 계수만 JSON 에 실어 장치에서는 numpy 로 추론한다
-- GMM 과 같은 이유로 장치에 scikit-learn 을 두지 않는다.

feature 는 전부 과거만 본다(기울기·1시간 평균). 타깃은 shift(-6) 이므로 마지막
6버킷은 학습에서 빠진다 -- 누수 없음.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

FEATURES = ["co2", "voc", "d_co2_10m", "d_voc_10m", "co2_mean_1h", "voc_mean_1h"]
TARGETS = ["y_co2_30", "y_voc_30"]


@dataclass
class ForecastModel:
    """models/forecast_v1.json. 선형이라 계수만 있으면 재현된다."""
    version: str
    features: list[str]
    targets: list[str]
    coef: np.ndarray          # (n_targets, n_features)
    intercept: np.ndarray     # (n_targets,)
    meta: dict

    def to_json(self, path: str | Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({
            "version": self.version,
            "features": self.features,
            "targets": self.targets,
            "coef": self.coef.tolist(),
            "intercept": self.intercept.tolist(),
            "meta": self.meta,
        }, indent=2, ensure_ascii=False), encoding="utf-8")

    @classmethod
    def from_json(cls, path: str | Path) -> "ForecastModel":
        o = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(version=o["version"], features=list(o["features"]),
                   targets=list(o["targets"]),
                   coef=np.asarray(o["coef"], dtype=float),
                   intercept=np.asarray(o["intercept"], dtype=float),
                   meta=o.get("meta", {}))

    def predict(self, d: pd.DataFrame) -> pd.DataFrame:
        """feature 가 하나라도 결측이면 그 행은 예측하지 않는다(보간 금지 원칙)."""
        out = pd.DataFrame(index=d.index, columns=self.targets, dtype=float)
        ok = d[self.features].notna().all(axis=1)
        if ok.any():
            X = d.loc[ok, self.features].to_numpy(dtype=float)
            out.loc[ok, self.targets] = X @ self.coef.T + self.intercept
        return out


def fit_ridge(X: np.ndarray, Y: np.ndarray, alpha: float = 1.0) -> tuple[np.ndarray, np.ndarray]:
    """표준화 후 능형회귀를 닫힌 형태로 푼다(scikit-learn 불필요).

    feature 스케일이 제각각(ppm 수백 vs 기울기 한자리)이라 정규화 없이 하나의
    alpha 를 쓰면 특정 계수만 눌린다. 표준화해서 풀고 원 스케일 계수로 되돌린다.
    """
    mu, sd = X.mean(axis=0), X.std(axis=0)
    sd = np.where(sd == 0, 1.0, sd)
    Xs = (X - mu) / sd
    ym = Y.mean(axis=0)

    n_f = Xs.shape[1]
    A = Xs.T @ Xs + alpha * np.eye(n_f)
    W = np.linalg.solve(A, Xs.T @ (Y - ym))       # (n_f, n_t) 표준화 공간 계수

    coef = (W / sd[:, None]).T                     # 원 스케일로 환원
    intercept = ym - (mu / sd) @ W
    return coef, intercept


def evaluate(y: np.ndarray, yhat: np.ndarray) -> dict:
    """MAE 와 함께 '그대로 유지' 기준선 대비 개선을 본다.

    공기질은 30분 뒤에도 대체로 비슷하므로, 지속(persistence) 기준선을 못 이기면
    모델을 쓸 이유가 없다.
    """
    ok = ~(np.isnan(y) | np.isnan(yhat))
    y, yhat = y[ok], yhat[ok]
    err = np.abs(y - yhat)
    ss = 1 - ((y - yhat) ** 2).sum() / ((y - y.mean()) ** 2).sum()
    return {"mae": round(float(err.mean()), 2),
            "rmse": round(float(np.sqrt(((y - yhat) ** 2).mean())), 2),
            "r2": round(float(ss), 4), "n": int(ok.sum())}
