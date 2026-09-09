"""9단계 학습 -- models/forecast_v1.json 을 만든다.

    python -m analysis.train_forecast --csv data/sensor_all.csv

시간 순으로 앞 80% 학습 / 뒤 20% 검증. 무작위 분할을 쓰면 미래 정보가 학습에
새어 들어가 성능이 실제보다 좋게 나온다.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from . import pipeline as P
from .forecast import FEATURES, TARGETS, ForecastModel, evaluate, fit_ridge
from .schema import connect

DEFAULT_OUT = "models/forecast_v1.json"


def main() -> None:
    ap = argparse.ArgumentParser(description="30분 예측 모델 학습")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--db")
    src.add_argument("--csv")
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--alpha", type=float, default=1.0)
    ap.add_argument("--version", default="v1")
    a = ap.parse_args()

    if a.db:
        with connect(a.db, read_only=True) as con:
            raw = P.load_readings(con)
        source = f"db:{Path(a.db).name}"
    else:
        raw = pd.read_csv(a.csv, encoding="utf-8-sig").rename(columns={"recv_time": "ts"})
        raw["ts"] = pd.to_datetime(raw.ts, errors="coerce")
        raw = raw.dropna(subset=["ts"])
        source = f"csv:{Path(a.csv).name}"

    d = P.to_buckets(raw)
    d = P.apply_range_rules(d)
    d = P.drop_failed(d, P.qc_gate(d))
    d = P.build_forecast_frame(d)

    d = d.dropna(subset=FEATURES + TARGETS).sort_values("bucket")
    print(f"학습 프레임 {len(d):,}행 · {d.bucket.min()} → {d.bucket.max()}")

    cut = int(len(d) * 0.8)
    tr, te = d.iloc[:cut], d.iloc[cut:]
    print(f"  시간 분할  학습 {len(tr):,} (~{tr.bucket.max()}) · 검증 {len(te):,}")

    X, Y = tr[FEATURES].to_numpy(float), tr[TARGETS].to_numpy(float)
    coef, intercept = fit_ridge(X, Y, a.alpha)

    m = ForecastModel(version=a.version, features=FEATURES, targets=TARGETS,
                      coef=coef, intercept=intercept, meta={})
    pred = m.predict(te)

    print("\n검증 (뒤 20%):")
    meta_scores = {}
    for i, t in enumerate(TARGETS):
        sensor = "co2" if "co2" in t else "voc"
        model_s = evaluate(te[t].to_numpy(float), pred[t].to_numpy(float))
        # 기준선: 30분 뒤에도 지금과 같다고 두는 것
        base_s = evaluate(te[t].to_numpy(float), te[sensor].to_numpy(float))
        gain = 100 * (1 - model_s["mae"] / base_s["mae"])
        print(f"  {t:10s} MAE {model_s['mae']:7.2f}  R² {model_s['r2']:+.3f}   "
              f"|  지속 기준선 MAE {base_s['mae']:7.2f}  →  개선 {gain:+.1f}%")
        meta_scores[t] = {"model": model_s, "persistence": base_s,
                          "mae_gain_pct": round(gain, 1)}

    m.meta = {
        "trained_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": source, "alpha": a.alpha,
        "horizon_min": P.HORIZON_BUCKETS * 5,
        "n_train": int(len(tr)), "n_test": int(len(te)),
        "split": "time-ordered 80/20",
        "window": [str(d.bucket.min()), str(d.bucket.max())],
        "scores": meta_scores,
    }

    print("\n계수:")
    for i, t in enumerate(TARGETS):
        terms = "  ".join(f"{f}={c:+.4f}" for f, c in zip(FEATURES, coef[i]))
        print(f"  {t}: {terms}  intercept={intercept[i]:+.2f}")

    m.to_json(a.out)
    print(f"\n저장: {a.out}")


if __name__ == "__main__":
    main()
