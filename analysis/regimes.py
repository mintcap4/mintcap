"""5단계 -- 레짐 발견 (GMM, 비지도).

왜 비지도인가. 내가 임계를 정해 라벨을 만들고 그걸 ML 로 맞히면 공식 복원일 뿐이다.
군집은 타깃이 없으므로 그 순환이 없고, 나온 군집이 이론(CO2=인체 · VOC=물질)과
맞는지가 검증이 된다.

입력은 (x_co2, x_voc) 두 열뿐. 노드ID·시간대를 넣지 않는 이유는 넣으면 '교실을
구분하는' 군집이 나오기 때문이다.

학습 결과는 models/gmm_v1.json 에 저장하고, 장치에서는 그 JSON 만 읽어 판정한다
(sklearn 없이도 예측되도록 순수 numpy 로 구현 -- predict 참조).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from .pipeline import CO2_OUTDOOR, VOC_BASELINE

# 앵커 -- 군집 번호는 실행마다 바뀌므로, 중심 좌표가 이 기준선의 어느 쪽인가로
# 이름을 정한다. 이름이 정본이고 cluster 번호는 부산물.
ANCHOR_CO2 = 700.0
ANCHOR_VOC = 120.0

QUADRANT_NAME = {
    ("low", "low"): "clean",     # 저CO2 · 저VOC -- 무조치
    ("low", "high"): "matter",   # 저CO2 · 고VOC -- 공기청정기
    ("high", "low"): "human",    # 고CO2 · 저VOC -- 환풍기
    ("high", "high"): "mixed",   # 고CO2 · 고VOC -- 둘 다
}


def quadrant_of(mu_co2: float, mu_voc: float) -> tuple[str, str]:
    """중심 좌표(스케일된 값) -> (분면, 레짐 이름)."""
    q = ("high" if mu_co2 * CO2_OUTDOOR > ANCHOR_CO2 else "low",
         "high" if mu_voc * VOC_BASELINE > ANCHOR_VOC else "low")
    return "·".join(q), QUADRANT_NAME[q]


@dataclass
class RegimeModel:
    """gmm_v1.json 의 내용. 장치에서는 이것만으로 판정한다."""
    version: str
    means: np.ndarray            # (k, 2)
    covariances: np.ndarray      # (k, 2, 2)
    weights: np.ndarray          # (k,)
    regime_of: list[str]         # cluster index -> 레짐 이름
    meta: dict

    # ---- 저장 / 적재 ----
    def to_json(self, path: str | Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({
            "version": self.version,
            "means": self.means.tolist(),
            "covariances": self.covariances.tolist(),
            "weights": self.weights.tolist(),
            "regime_of": self.regime_of,
            "meta": self.meta,
        }, indent=2, ensure_ascii=False), encoding="utf-8")

    @classmethod
    def from_json(cls, path: str | Path) -> "RegimeModel":
        o = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(version=o["version"],
                   means=np.asarray(o["means"], dtype=float),
                   covariances=np.asarray(o["covariances"], dtype=float),
                   weights=np.asarray(o["weights"], dtype=float),
                   regime_of=list(o["regime_of"]),
                   meta=o.get("meta", {}))

    # ---- 판정 (sklearn 불필요) ----
    def _log_prob(self, X: np.ndarray) -> np.ndarray:
        """각 성분의 로그 결합확률밀도. (n, k)"""
        n, k = len(X), len(self.means)
        out = np.empty((n, k))
        for i in range(k):
            d = X - self.means[i]
            cov = self.covariances[i]
            inv = np.linalg.inv(cov)
            _, logdet = np.linalg.slogdet(cov)
            maha = np.einsum("ij,jk,ik->i", d, inv, d)
            out[:, i] = (np.log(self.weights[i]) - 0.5 * (maha + logdet + 2 * np.log(2 * np.pi)))
        return out

    def predict(self, X: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """-> (cluster 번호, p_max). p_max 는 그 판정의 확신도."""
        lp = self._log_prob(np.asarray(X, dtype=float))
        lp -= lp.max(axis=1, keepdims=True)          # 언더플로 방지
        p = np.exp(lp)
        p /= p.sum(axis=1, keepdims=True)
        return p.argmax(axis=1), p.max(axis=1)

    def label(self, d: pd.DataFrame) -> pd.DataFrame:
        """x_co2/x_voc 를 가진 DataFrame -> cluster · p_max · regime_raw 추가.

        NaN 이 있는 행은 판정하지 않는다(보간하지 않는 것과 같은 이유).
        """
        out = d.copy()
        ok = out.x_co2.notna() & out.x_voc.notna()
        out["cluster"] = pd.NA
        out["p_max"] = np.nan
        out["regime_raw"] = pd.NA
        if ok.any():
            cl, pm = self.predict(out.loc[ok, ["x_co2", "x_voc"]].to_numpy())
            out.loc[ok, "cluster"] = cl
            out.loc[ok, "p_max"] = pm.round(4)
            out.loc[ok, "regime_raw"] = [self.regime_of[c] for c in cl]
        return out

    # ---- 사람이 읽는 요약 ----
    def table(self) -> pd.DataFrame:
        rows = []
        for i, (mu, w) in enumerate(zip(self.means, self.weights)):
            quad, _ = quadrant_of(mu[0], mu[1])
            rows.append({"cluster": i, "mu_co2": round(float(mu[0]), 3),
                         "mu_voc": round(float(mu[1]), 3),
                         "ppm": round(float(mu[0]) * CO2_OUTDOOR),
                         "voc": round(float(mu[1]) * VOC_BASELINE),
                         "weight": round(float(w), 4), "quadrant": quad,
                         "regime": self.regime_of[i]})
        return pd.DataFrame(rows).sort_values("weight", ascending=False)


def anchor_names(means: np.ndarray) -> tuple[list[str], list[str]]:
    """중심 -> (분면, 레짐 이름) 목록. 이름이 겹치는지는 호출자가 확인한다."""
    quads, names = [], []
    for mu in means:
        q, n = quadrant_of(mu[0], mu[1])
        quads.append(q)
        names.append(n)
    return quads, names
