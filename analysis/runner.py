"""10단계 -- 계산은 배치가, 표시는 읽기만.

웹 요청마다 수십만 행의 GMM·crosstab 을 돌리면 화면이 버벅인다. 계산은 매시간·
매일 한 번 하고 결과(JSON)만 analysis 테이블에 넣는다. 웹은 그것을 그리기만 한다.
이 분리 덕분에 프런트를 바꿔도 분석 코드는 한 줄도 안 바뀐다.

    python -m analysis.runner --db sensor_data.db --kinds hourly
    python -m analysis.runner --db sensor_data.db --kinds daily
    python -m analysis.runner --db sensor_data.db --kinds all

kind        주기     탭
qc          hourly   관리
regime_now  hourly   진단추론
action      hourly   제어경보
forecast    hourly   제어경보
summary     hourly   모니터링
band        daily    진단추론
transition  daily    진단추론
occ_co2     daily    모니터링
model_event weekly   관리
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from . import pipeline as P
from .forecast import FEATURES, ForecastModel
from .regimes import RegimeModel
from .schema import connect, ensure

HOURLY = ("qc", "regime_now", "action", "forecast", "summary")
DAILY = ("band", "transition", "occ_co2")
WEEKLY = ("model_event",)

REGIME_WINDOW_DAYS = 28      # 전이·체류·밴드를 집계하는 창
LIVE_WINDOW_DAYS = 3         # 현재 상태 판정에 쓰는 창 (평활에 앞뒤 45분이 필요)


def _j(o):
    """numpy/pandas 스칼라를 JSON 이 받는 형으로."""
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return None if np.isnan(o) else round(float(o), 4)
    if isinstance(o, (np.bool_,)):
        return bool(o)
    if isinstance(o, (pd.Timestamp, datetime)):
        return o.isoformat()
    if o is pd.NaT or (isinstance(o, float) and np.isnan(o)):
        return None
    raise TypeError(f"{type(o)} 직렬화 불가")


class Writer:
    """analysis 테이블에 결과를 넣는다. 같은 (kind, scope) 의 과거 행은 남겨 이력이 된다."""

    def __init__(self, con: sqlite3.Connection, run_at: str, model_ver: str | None):
        self.con, self.run_at, self.model_ver = con, run_at, model_ver
        self.n = 0

    def put(self, kind: str, scope: str, payload: dict, *,
            win: tuple[str, str] | None = None, model_ver: str | None = ...) -> None:
        self.con.execute(
            "INSERT INTO analysis(run_at,kind,scope,win_start,win_end,model_ver,payload)"
            " VALUES(?,?,?,?,?,?,?)",
            (self.run_at, kind, scope, win[0] if win else None, win[1] if win else None,
             self.model_ver if model_ver is ... else model_ver,
             json.dumps(payload, ensure_ascii=False, default=_j)))
        self.n += 1


# ============================================================================
def _prepare(con, days: int, gmm: RegimeModel):
    """0~6단계를 돌려 평활된 레짐까지 붙인 프레임과 QC 게이트를 돌려준다."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    raw = P.load_readings(con, since=since)
    if raw.empty:
        return raw, pd.DataFrame()
    d = P.to_buckets(raw)
    d = P.apply_range_rules(d)
    gate = P.qc_gate(d)
    d = P.drop_failed(d, gate)
    d = P.add_model_input(d)
    d = gmm.label(d)
    return P.smooth_regime(d), gate


def run_hourly(con, w: Writer, gmm: RegimeModel, fc: ForecastModel | None) -> None:
    d, gate = _prepare(con, LIVE_WINDOW_DAYS, gmm)
    if d.empty:
        w.put("summary", "all", {"nodes": 0, "note": "최근 데이터 없음"}, model_ver=None)
        return

    # --- qc : 노드×일 유효율 (관리 탭) ---
    for node, g in gate.groupby("node"):
        w.put("qc", node, {"days": [
            {"date": str(r.date), "rows": int(r.rows),
             "valid_co2_pct": float(r.valid_co2_pct), "valid_voc_pct": float(r.valid_voc_pct),
             "passed": bool(r.passed), "reason": r.reason} for r in g.itertuples()]},
            model_ver=None)

    latest = d.sort_values("bucket").groupby("node").tail(1)

    # --- regime_now : 현재 레짐 + 체류 (진단추론 탭) ---
    dwell = P.dwell_segments(d)
    for r in latest.itertuples():
        seg = dwell[(dwell.node == r.node)].tail(1)
        w.put("regime_now", r.node, {
            "bucket": r.bucket, "regime": r.regime, "regime_raw": r.regime_raw,
            "p_max": r.p_max, "cluster": r.cluster,
            "co2": r.co2, "voc": r.voc, "pm2p5": r.pm2p5,
            "temp": r.scd_temp, "hum": r.scd_hum,
            "x_co2": r.x_co2, "x_voc": r.x_voc,
            "dwell_min": int(seg.minutes.iloc[0]) if len(seg) else None,
            "dwell_censored": bool(seg.censored.iloc[0]) if len(seg) else None,
        })

    # --- action : 행동지침 (제어경보 탭) ---
    state = pd.read_sql("SELECT * FROM actuator_state", con)
    now = pd.Timestamp.now("UTC").tz_localize(None)
    acts = P.decide_actions(latest, state, now)
    for node, g in acts.groupby("node"):
        w.put("action", node, {"devices": [
            {"device": r.device, "state": int(r.state), "rule": r.rule,
             "regime": r.regime, "value": r.value, "hold_until": r.hold_until}
            for r in g.itertuples()]})

    # 히스테리시스 기억을 갱신 -- 상태가 바뀐 것만 since 를 새로 찍는다
    prev = {(r.node, r.device): int(r.state) for r in state.itertuples()} if not state.empty else {}
    for r in acts.itertuples():
        if prev.get((r.node, r.device)) != int(r.state):
            con.execute("INSERT INTO actuator_state(node,device,state,since) VALUES(?,?,?,?)"
                        " ON CONFLICT(node,device) DO UPDATE SET state=excluded.state,"
                        " since=excluded.since",
                        (r.node, r.device, int(r.state), now.isoformat()))

    # --- forecast : 30분 뒤 예측 + 경보 (제어경보 탭) ---
    if fc is not None:
        ff = P.build_forecast_frame(d)
        tail = ff.sort_values("bucket").groupby("node").tail(1)
        pred = fc.predict(tail)
        for (r, (_, p)) in zip(tail.itertuples(), pred.iterrows()):
            co2p, vocp = p.get("y_co2_30"), p.get("y_voc_30")
            w.put("forecast", r.node, {
                "horizon_min": fc.meta.get("horizon_min", 30),
                "co2_now": r.co2, "voc_now": r.voc,
                "co2_pred": co2p, "voc_pred": vocp,
                "d_co2_10m": r.d_co2_10m, "d_voc_10m": r.d_voc_10m,
                # 예측이 규칙층 ON 임계를 넘으면 선제 경보
                "alert_co2": bool(pd.notna(co2p) and co2p > P.RULE_ON["co2"]),
                "alert_voc": bool(pd.notna(vocp) and vocp > P.RULE_ON["voc"]),
            }, model_ver=fc.version)

    # --- summary : 요약 카드 (모니터링 탭) ---
    share = d.regime.value_counts(normalize=True)
    w.put("summary", "all", {
        "nodes": int(d.node.nunique()),
        "last_bucket": d.bucket.max(),
        "regime_share": {k: float(v) for k, v in share.items()},
        "co2_median": float(latest.co2.median()) if latest.co2.notna().any() else None,
        "voc_median": float(latest.voc.median()) if latest.voc.notna().any() else None,
        "nodes_failing_qc": sorted(gate.loc[~gate.passed, "node"].unique().tolist()),
        "actions_on": int(acts.state.sum()) if not acts.empty else 0,
    })


def run_daily(con, w: Writer, gmm: RegimeModel) -> None:
    d, _ = _prepare(con, REGIME_WINDOW_DAYS, gmm)
    if d.empty:
        return
    win = (str(d.bucket.min()), str(d.bucket.max()))

    # --- transition : 전이행렬 + 체류 (진단추론 탭) ---
    tm, meta = P.transition_matrix(d)
    dwell = P.dwell_segments(d)
    med = dwell.groupby("regime").minutes.median().to_dict() if not dwell.empty else {}
    w.put("transition", "all", {
        "regimes": list(P.REGIMES),
        "matrix": tm.reindex(index=list(P.REGIMES), columns=list(P.REGIMES)).values.tolist(),
        "dwell_median_min": {k: float(v) for k, v in med.items()},
        "dwell_n": {k: int(v) for k, v in dwell.groupby("regime").size().items()} if not dwell.empty else {},
        **meta,
    }, win=win)

    # --- band : 시간대별 레짐 밴드 (진단추론 탭 -- 스위칭 밴드) ---
    d = d.assign(slot=d.bucket.dt.hour * 60 + d.bucket.dt.minute)
    for node, g in d.groupby("node"):
        mode = (g.groupby("slot").regime
                 .agg(lambda s: s.mode().iat[0] if len(s.mode()) else None))
        w.put("band", node, {"slots": [{"min": int(k), "regime": v}
                                       for k, v in mode.items() if v]}, win=win)

    # --- occ_co2 : 재실 x CO2 (모니터링 탭) ---
    # 조인은 (node, bucket) 키로 한다. 비전 노드가 환경 노드와 다른 장치면
    # 교집합이 비어 결과가 0건이 되는데, 아무것도 안 쓰면 화면에서 원인을 알 수
    # 없으므로 '왜 비었는지'를 남긴다.
    occ = P.load_occupancy(con, since=win[0])
    written = 0
    if not occ.empty:
        o = P.to_buckets(occ)
        j = d.merge(o[["node", "bucket", "occ"]], on=["node", "bucket"], how="inner")
        for node, g in j.groupby("node"):
            g = g.dropna(subset=["occ", "co2"])
            if len(g) < 10:
                continue
            w.put("occ_co2", node, {
                "n": int(len(g)),
                "spearman": float(g[["occ", "co2"]].corr(method="spearman").iloc[0, 1]),
                "points": [{"occ": float(a), "co2": float(b), "regime": c}
                           for a, b, c in zip(g.occ, g.co2, g.regime)][-2000:],
            }, win=win)
            written += 1

    if written == 0:
        env, vis = sorted(d.node.unique()), sorted(occ.node.unique()) if not occ.empty else []
        note = ("occupancy 데이터 없음" if not vis else
                "환경 노드와 비전 노드가 겹치지 않아 (node, bucket) 조인이 성립하지 않음"
                if not set(env) & set(vis) else
                "겹치는 버킷이 10개 미만")
        w.put("occ_co2", "all", {"n": 0, "note": note,
                                 "env_nodes": env, "vision_nodes": vis},
              win=win, model_ver=None)


def run_weekly(con, w: Writer, gmm: RegimeModel, fc: ForecastModel | None) -> None:
    """model_event -- 어떤 모델이 언제 학습됐고 성능이 어떤가 (관리 탭)."""
    w.put("model_event", "gmm", {"kind": "gmm", **gmm.meta,
                                 "clusters": gmm.table().to_dict("records")})
    if fc is not None:
        w.put("model_event", "forecast", {"kind": "forecast", **fc.meta},
              model_ver=fc.version)


# ============================================================================
def main() -> None:
    ap = argparse.ArgumentParser(description="분석 배치 -- 결과를 analysis 테이블에 저장")
    ap.add_argument("--db", default="sensor_data.db")
    ap.add_argument("--kinds", default="hourly", choices=["hourly", "daily", "weekly", "all"])
    ap.add_argument("--gmm", default="models/gmm_v1.json")
    ap.add_argument("--forecast", default="models/forecast_v1.json")
    a = ap.parse_args()

    gmm = RegimeModel.from_json(a.gmm)
    fc = ForecastModel.from_json(a.forecast) if Path(a.forecast).is_file() else None
    if fc is None:
        print(f"경고: {a.forecast} 없음 -- 예측 없이 진행", flush=True)

    run_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    con = connect(a.db)
    ensure(con)
    w = Writer(con, run_at, gmm.version)

    try:
        if a.kinds in ("hourly", "all"):
            run_hourly(con, w, gmm, fc)
        if a.kinds in ("daily", "all"):
            run_daily(con, w, gmm)
        if a.kinds in ("weekly", "all"):
            run_weekly(con, w, gmm, fc)
        con.commit()
    finally:
        con.close()
    print(f"{run_at}  {a.kinds}: analysis 행 {w.n}개 기록")


if __name__ == "__main__":
    main()
