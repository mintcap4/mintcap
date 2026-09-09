"""5단계 학습 -- GMM 을 적합해 models/gmm_v1.json 을 만든다.

    python -m analysis.train_gmm --db sensor_data.db
    python -m analysis.train_gmm --csv data/sensor_all.csv --out models/gmm_v1.json

[k 를 고른 근거]
문서는 k=4(대응 조치 수)를 쓰지만, 이 데이터에서 k=4 는 밀도를 따라 clean 을 둘로
쪼개고 matter 를 놓친다. CO2 축의 분산·왜도가 VOC 보다 커서 그쪽으로만 갈라지기
때문. 강제로 분면 중심을 초기값으로 주면 seed 7 회 중 5 회가 이름 충돌로 실패했다.

그래서 k=6 으로 밀도를 잡고, 각 성분을 중심의 분면으로 이름 붙인 뒤 같은 분면끼리
병합한다. 출력 레짐은 여전히 4 개(= 대응 조치 4 개)이고 의사결정 구조는 문서 그대로다.

    방식              BIC       앵커일치   seed 안정성
    k=4 분면시드    380,823     71.1%      5/7 실패
    k=6 분면병합    370,453     77.6%      6/7 성공   <- 채택

임계를 모델에 넣지 않는 원칙은 유지된다. 앵커는 이름표를 붙이는 데만 쓰고 학습에는
관여하지 않는다.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from . import pipeline as P
from .regimes import RegimeModel, anchor_names
from .schema import connect

DEFAULT_K = 6
DEFAULT_OUT = "models/gmm_v1.json"
MAX_SEED_TRIES = 12          # 4분면을 다 덮을 때까지 시드를 바꿔 재시도


def prepare(d: pd.DataFrame) -> pd.DataFrame:
    """0~4단계를 순서대로 적용해 학습 표본을 만든다."""
    d = P.to_buckets(d)
    d = P.apply_range_rules(d)
    gate = P.qc_gate(d)
    d = P.drop_failed(d, gate)
    d = P.add_model_input(d)
    return d.dropna(subset=["x_co2", "x_voc"]), gate


def fit(X: np.ndarray, k: int = DEFAULT_K) -> tuple:
    """4분면을 모두 덮는 적합을 찾을 때까지 시드를 바꿔 시도한다.

    덮지 못하면 그 모델은 레짐 하나를 영영 판정하지 못하므로 쓸 수 없다.
    """
    from sklearn.mixture import GaussianMixture

    best = None
    for seed in range(MAX_SEED_TRIES):
        g = GaussianMixture(k, covariance_type="full", random_state=seed, n_init=3).fit(X)
        _, names = anchor_names(g.means_)
        covered = set(names)
        if best is None:
            best = (g, seed, names, covered)
        if len(covered) == 4:
            return g, seed, names
        print(f"  seed {seed}: 분면 {len(covered)}/4 {sorted(covered)} — 재시도", file=sys.stderr)

    g, seed, names, covered = best
    raise SystemExit(
        f"오류: {MAX_SEED_TRIES}회 시도했지만 4분면을 모두 덮지 못했습니다 "
        f"(최선 {len(covered)}/4: {sorted(covered)}).\n"
        f"       학습 데이터에 특정 레짐의 표본이 거의 없다는 뜻입니다. "
        f"기간을 늘리거나 노드를 더 포함하세요."
    )


def main() -> None:
    ap = argparse.ArgumentParser(description="GMM 레짐 모델 학습")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--db", help="sensor_data.db 경로 (readings 테이블)")
    src.add_argument("--csv", help="readings CSV (recv_time,node,co2,voc,... 열)")
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("-k", type=int, default=DEFAULT_K)
    ap.add_argument("--version", default="v1")
    a = ap.parse_args()

    # ---- 0단계 적재 ----
    if a.db:
        with connect(a.db, read_only=True) as con:
            raw = P.load_readings(con)
        source = f"db:{Path(a.db).name}"
    else:
        raw = pd.read_csv(a.csv, encoding="utf-8-sig")
        raw = raw.rename(columns={"recv_time": "ts"})
        raw["ts"] = pd.to_datetime(raw.ts, errors="coerce")
        raw = raw.dropna(subset=["ts"])
        source = f"csv:{Path(a.csv).name}"
    print(f"0. 원 데이터 {raw.shape} · 노드 {raw.node.nunique()}개 · "
          f"{raw.ts.min()} → {raw.ts.max()}")

    # ---- 1~4단계 ----
    d, gate = prepare(raw)
    print(f"1-2. QC 게이트 통과 {int(gate.passed.sum())}/{len(gate)} 노드×일 "
          f"({100*gate.passed.mean():.1f}%)")
    print(f"3-4. 학습 표본 {len(d):,}행")
    if len(d) < 1000:
        print(f"경고: 표본 {len(d)}행은 k={a.k} GMM 에 부족합니다. 결과를 신뢰하지 마세요.",
              file=sys.stderr)

    # ---- 5단계 ----
    X = d[["x_co2", "x_voc"]].to_numpy()
    g, seed, names = fit(X, a.k)
    print(f"5. GMM k={a.k} 적합 (seed {seed}) · BIC {g.bic(X):,.0f}")

    model = RegimeModel(
        version=a.version,
        means=g.means_, covariances=g.covariances_, weights=g.weights_,
        regime_of=names,
        meta={
            "trained_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "source": source,
            "k": a.k, "seed": seed,
            "n_samples": int(len(X)),
            "bic": round(float(g.bic(X)), 1),
            "window": [str(raw.ts.min()), str(raw.ts.max())],
            "nodes": sorted(raw.node.unique().tolist()),
            "scale": {"co2_outdoor": P.CO2_OUTDOOR, "voc_baseline": P.VOC_BASELINE},
            "anchor": {"co2": 700.0, "voc": 120.0},
        },
    )

    # ---- 검증 ----
    labeled = model.label(d)
    share = labeled.regime_raw.value_counts(normalize=True)
    pure = np.where(d.co2 > 700, "high", "low") + "·" + np.where(d.voc > 120, "high", "low")
    pure = pd.Series(pure).map({"low·low": "clean", "low·high": "matter",
                                "high·low": "human", "high·high": "mixed"})
    agree = float((labeled.regime_raw.reset_index(drop=True) == pure).mean())
    model.meta["agreement_with_anchor_rule"] = round(agree, 4)
    model.meta["regime_share"] = {k: round(float(v), 4) for k, v in share.items()}

    print("\n" + model.table().to_string(index=False))
    print(f"\n레짐 분포   " + " · ".join(f"{k} {100*v:.1f}%" for k, v in share.items()))
    print(f"앵커 규칙과 일치율 {100*agree:.1f}%  "
          f"(나머지는 GMM 이 결합분포를 보고 다르게 판정한 경계 근처)")
    print(f"p_max 중앙값 {labeled.p_max.median():.2f} · "
          f"확신도 0.6 미만 {100*(labeled.p_max < 0.6).mean():.1f}%")

    model.to_json(a.out)
    print(f"\n저장: {a.out}")


if __name__ == "__main__":
    main()
