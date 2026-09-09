"""진단 · 추론 파이프라인 -- 파이프라인 문서 0~9단계.

readings/occupancy 를 읽기만 하고, 정제·판정은 전부 여기서 한다.
각 함수는 문서의 한 단계에 대응하고 순수 함수로 두었다(입력 DataFrame -> 출력
DataFrame). 배치 실행과 결과 저장은 runner.py 가 맡는다.

    0 원 데이터      load_readings / load_occupancy
    1 시간 정렬      to_buckets
    2 데이터 검사    apply_range_rules / qc_gate / drop_failed
    3 변수 관계      spearman
    4 스케일         add_model_input
    5 레짐 발견      regimes.py (GMM -- 별도 모듈, 학습된 모델이 필요)
    6 평활           smooth_regime
    7 전이 · 체류    transition_matrix / dwell_segments
    8 규칙층         decide_actions
    9 예측           build_forecast_frame
"""
from __future__ import annotations

import sqlite3

import numpy as np
import pandas as pd

# ---- 상수 (문서에 명시된 값) -------------------------------------------------
BUCKET = "5min"

# 2단계 범위 규칙 -- 물리적으로 불가능한 값을 NaN 으로. 행은 지우지 않는다.
RANGE_RULES: dict[str, tuple[float, float]] = {
    "co2": (350, 5000),
    "voc": (1, 500),
    "sen_temp": (-10, 50),
    "scd_temp": (-10, 50),
    "sen_hum": (0, 100),
    "scd_hum": (0, 100),
}
QC_MIN_VALID_PCT = 95.0          # 노드×일 유효율이 이 미만이면 그날 그 노드는 제외

# 4단계 고정 스케일 -- 외기 기준. 데이터에 맞추지 않고 물리에 맞춘다.
CO2_OUTDOOR = 400.0              # x_co2 = co2 / 400  (외기 ≈ 1.0)
VOC_BASELINE = 100.0             # x_voc = voc / 100  (VOC index 기준선 = 1.0)

# 6단계 평활 -- 45분(9버킷) 다수결, gap 에서 창을 끊음
SMOOTH_WINDOW = 9
GAP_MINUTES = 7                  # 앞 행과 7분 초과면 연속이 아님

# 8단계 규칙층 -- ON/OFF 선이 다른 것이 히스테리시스
RULE_ON = {"co2": 1000.0, "voc": 200.0}
RULE_OFF = {"co2": 700.0, "voc": 120.0}
MIN_HOLD_MIN = 10                # 최소 동작 시간

# 9단계 예측 -- 30분 = 6버킷 뒤
HORIZON_BUCKETS = 6

REGIMES = ("clean", "matter", "human", "mixed")


# ============================================================================
# 0단계 -- 원 데이터
# ============================================================================
def load_readings(con: sqlite3.Connection, *, since: str | None = None) -> pd.DataFrame:
    """readings 를 그대로 읽는다. 정제는 하지 않는다(원본 보존)."""
    sql = "SELECT * FROM readings"
    params: tuple = ()
    if since:
        sql += " WHERE ts >= ?"
        params = (since,)
    d = pd.read_sql(sql, con, params=params)
    if not d.empty:
        d["ts"] = pd.to_datetime(d.ts, errors="coerce")
        d = d.dropna(subset=["ts"])
    return d


def load_occupancy(con: sqlite3.Connection, *, since: str | None = None) -> pd.DataFrame:
    sql = "SELECT * FROM occupancy"
    params: tuple = ()
    if since:
        sql += " WHERE ts >= ?"
        params = (since,)
    d = pd.read_sql(sql, con, params=params)
    if not d.empty:
        d["ts"] = pd.to_datetime(d.ts, errors="coerce")
        d = d.dropna(subset=["ts"])
    return d


# ============================================================================
# 1단계 -- 시간 정렬. 이후 모든 조인·그룹의 키는 (node, bucket).
# ============================================================================
def to_buckets(d: pd.DataFrame) -> pd.DataFrame:
    """bucket = floor(ts, 5min). 같은 (node, bucket) 이 여럿이면 마지막 행만 남긴다.

    수신 시각은 항상 버킷 시각보다 늦으므로 내림이 맞다. 반올림하면 다음 버킷으로
    넘어가는 행이 생긴다.
    """
    if d.empty:
        return d.assign(bucket=pd.Series(dtype="datetime64[ns]"))
    d = d.copy()
    d["bucket"] = d.ts.dt.floor(BUCKET)
    return (d.sort_values("ts")
             .drop_duplicates(subset=["node", "bucket"], keep="last")
             .sort_values(["node", "bucket"])
             .reset_index(drop=True))


# ============================================================================
# 2단계 -- 데이터 검사. 값 수준(범위)과 노드×일 수준(게이트) 두 층.
# ============================================================================
def apply_range_rules(d: pd.DataFrame) -> pd.DataFrame:
    """범위 밖 값을 NaN 으로. 보간은 하지 않는다 -- 존재하지 않는 중간 상태가
    5단계에서 군집으로 잡히기 때문."""
    d = d.copy()
    for col, (lo, hi) in RANGE_RULES.items():
        if col in d.columns:
            d.loc[~d[col].between(lo, hi), col] = np.nan
    return d


def qc_gate(d: pd.DataFrame) -> pd.DataFrame:
    """노드×일 유효율 표. 95% 미만이면 그날 그 노드는 이후 단계에서 제외.

    '눈으로 보니 이상해서 뺐다' 는 재현이 안 된다. 단일 수치 하나면 다음 주에
    같은 코드가 같은 판단을 내린다.
    """
    if d.empty:
        return pd.DataFrame(columns=["date", "node", "rows", "valid_co2_pct",
                                     "valid_voc_pct", "passed", "reason"])
    d = d.copy()
    d["date"] = d.bucket.dt.date
    g = (d.groupby(["date", "node"])
           .agg(rows=("bucket", "size"),
                valid_co2_pct=("co2", lambda s: 100.0 * s.notna().mean()),
                valid_voc_pct=("voc", lambda s: 100.0 * s.notna().mean()))
           .reset_index())
    g["passed"] = ((g.valid_co2_pct >= QC_MIN_VALID_PCT)
                   & (g.valid_voc_pct >= QC_MIN_VALID_PCT))

    def reason(r) -> str:
        if r.passed:
            out = int(round(r.rows * (1 - min(r.valid_co2_pct, r.valid_voc_pct) / 100)))
            return f"{out} rows out of range" if out else ""
        if r.rows == 0:
            return "no rows"
        if r.rows < 288 * 0.5:                    # 하루 288버킷의 절반 미만
            return f"only {r.rows}/288 buckets"
        return f"valid {min(r.valid_co2_pct, r.valid_voc_pct):.1f}% < {QC_MIN_VALID_PCT}%"

    g["reason"] = g.apply(reason, axis=1)
    return g.round({"valid_co2_pct": 1, "valid_voc_pct": 1})


def drop_failed(d: pd.DataFrame, gate: pd.DataFrame) -> pd.DataFrame:
    """게이트를 통과한 노드×일만 남긴다."""
    if d.empty or gate.empty:
        return d
    d = d.copy()
    d["date"] = d.bucket.dt.date
    keep = gate.loc[gate.passed, ["date", "node"]]
    return d.merge(keep, on=["date", "node"], how="inner").drop(columns="date")


# ============================================================================
# 3단계 -- 변수 관계. 무엇을 남기고 무엇을 버리나.
# ============================================================================
CORR_COLS = ["pm2p5", "pm10p0", "scd_temp", "sen_temp", "scd_hum", "co2", "voc"]


def spearman(d: pd.DataFrame, cols: list[str] | None = None) -> pd.DataFrame:
    """Spearman 순위상관. 공선인 축을 떨어내는 근거."""
    cols = [c for c in (cols or CORR_COLS) if c in d.columns]
    if d.empty or not cols:
        return pd.DataFrame()
    return d[cols].corr(method="spearman")


# ============================================================================
# 4단계 -- 스케일. 축이 물리량이라 원점이 고정된다 -> 지난달과 비교 가능.
# ============================================================================
def add_model_input(d: pd.DataFrame) -> pd.DataFrame:
    """x_co2 = co2/400, x_voc = voc/100. 이 두 열만 모델 입력."""
    return d.assign(x_co2=d.co2 / CO2_OUTDOOR, x_voc=d.voc / VOC_BASELINE)


# ============================================================================
# 6단계 -- 평활. 잠깐 튄 판정 걸러내기 (HMM 없이 상태 지속성 흉내).
# ============================================================================
def _mark_gaps(g: pd.DataFrame) -> pd.Series:
    """앞 행과 GAP_MINUTES 초과로 떨어져 있으면 True -- 여기서 창을 끊는다."""
    dt = g.bucket.diff().dt.total_seconds().div(60)
    return (dt > GAP_MINUTES) | dt.isna()


def smooth_regime(d: pd.DataFrame, *, window: int = SMOOTH_WINDOW) -> pd.DataFrame:
    """regime = rolling_mode(regime_raw, window, center=True). gap 에서 창을 끊음.

    45분 창의 다수결. 1회짜리 튐은 사라지고 실제 전환은 남는다.
    """
    if d.empty or "regime_raw" not in d.columns:
        return d.assign(regime=pd.Series(dtype=object), gap=pd.Series(dtype=bool))

    out = []
    for node, g in d.sort_values(["node", "bucket"]).groupby("node", sort=False):
        g = g.copy()
        g["gap"] = _mark_gaps(g)
        g["_seg"] = g.gap.cumsum()               # gap 마다 새 구간 -> 창이 구간을 넘지 않음
        codes = g.regime_raw.astype("category")
        cats = list(codes.cat.categories)
        cc = codes.cat.codes.to_numpy()

        sm = np.empty(len(g), dtype=int)
        for _, idx in g.groupby("_seg", sort=False).indices.items():
            seg = cc[idx]
            half = window // 2
            for j in range(len(seg)):
                lo, hi = max(0, j - half), min(len(seg), j + half + 1)
                w = seg[lo:hi]
                w = w[w >= 0]
                sm[idx[j]] = np.bincount(w).argmax() if len(w) else seg[j]
        g["regime"] = [cats[i] if 0 <= i < len(cats) else None for i in sm]
        out.append(g.drop(columns="_seg"))
    return pd.concat(out, ignore_index=True) if out else d


# ============================================================================
# 7단계 -- 전이 · 체류. 움직임의 규칙.
# ============================================================================
def transition_matrix(d: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """연속 쌍 (regime.shift(1), regime) 을 교차표로 세고 행 정규화.

    gap 행은 쌍에서 제외한다 -- 결측 건너뛴 가짜 전이를 만들지 않기 위해.
    """
    pairs, n_gap = [], 0
    for _, g in d.sort_values(["node", "bucket"]).groupby("node", sort=False):
        prev, cur, gap = g.regime.shift(1), g.regime, g.gap
        ok = prev.notna() & cur.notna() & ~gap
        n_gap += int((prev.notna() & cur.notna() & gap).sum())
        pairs.append(pd.DataFrame({"from": prev[ok], "to": cur[ok]}))

    p = pd.concat(pairs, ignore_index=True) if pairs else pd.DataFrame(columns=["from", "to"])
    meta = {"valid_pairs": int(len(p)), "gap_pairs": n_gap}
    if p.empty:
        return pd.DataFrame(index=list(REGIMES), columns=list(REGIMES)).fillna(0.0), meta

    m = pd.crosstab(p["from"], p["to"]).reindex(index=list(REGIMES),
                                                columns=list(REGIMES), fill_value=0)
    return m.div(m.sum(axis=1).replace(0, np.nan), axis=0).fillna(0.0), meta


def dwell_segments(d: pd.DataFrame) -> pd.DataFrame:
    """같은 레짐이 이어진 구간의 길이. 결측으로 잘린 구간은 censored=True(하한)."""
    rows = []
    for node, g in d.sort_values(["node", "bucket"]).groupby("node", sort=False):
        g = g.reset_index(drop=True)
        # 레짐이 바뀌거나 gap 이 있으면 새 구간
        new = (g.regime != g.regime.shift(1)) | g.gap
        for _, seg in g.groupby(new.cumsum(), sort=False):
            if seg.regime.isna().all():
                continue
            start, end = seg.bucket.iloc[0], seg.bucket.iloc[-1]
            rows.append({
                "node": node,
                "regime": seg.regime.iloc[0],
                "start": start,
                "end": end,
                # 마지막 버킷도 5분간 지속된 것으로 본다
                "minutes": int((end - start).total_seconds() // 60) + 5,
                # 구간이 gap 으로 끊겼거나 데이터 끝에 닿았으면 실제로는 더 길 수 있음
                "censored": bool(seg.gap.iloc[-1:].any() or seg.index[-1] == g.index[-1]),
            })
    return pd.DataFrame(rows)


# ============================================================================
# 8단계 -- 규칙층. ML 이 기기를 고르고, 규칙이 타이밍을 정한다.
# ============================================================================
# 진단층: 레짐이 후보 기기를 고른다 (상대적 상태만 안다)
DEVICE_FOR = {
    "fan": {"human", "mixed"},        # CO2 가 문제 -> 환기
    "purifier": {"matter", "mixed"},  # VOC 가 문제 -> 공기청정
}
SENSOR_FOR = {"fan": "co2", "purifier": "voc"}


def decide_actions(latest: pd.DataFrame, state: pd.DataFrame, now: pd.Timestamp) -> pd.DataFrame:
    """노드별 마지막 행 + 이전 actuator_state -> 결정.

    3층 구조:
      진단층(ML)   레짐이 후보 기기를 고른다
      판단층(규칙) 절대값 임계로 '켤 만큼 나쁜가'를 본다. ON/OFF 선이 달라 히스테리시스
      안정층       최소 동작 10분. 임계 사이 구간은 이전 상태 유지 -> 깜빡임 방지
    """
    prev = {(r.node, r.device): r for r in state.itertuples()} if not state.empty else {}
    rows = []

    for r in latest.itertuples():
        for device, regimes in DEVICE_FOR.items():
            sensor = SENSOR_FOR[device]
            val = getattr(r, sensor, np.nan)
            p = prev.get((r.node, device))
            was = int(p.state) if p is not None else 0
            since = pd.to_datetime(p.since) if p is not None else None

            # 게이트 탈락 노드는 판단하지 않고 이전 상태를 유지한다
            if getattr(r, "excluded", False) or pd.isna(val) or pd.isna(r.regime):
                rows.append({"node": r.node, "device": device, "state": was,
                             "rule": "HOLD: qc gate failed", "hold_until": None,
                             "regime": getattr(r, "regime", None), "value": None})
                continue

            candidate = r.regime in regimes
            on_thr, off_thr = RULE_ON[sensor], RULE_OFF[sensor]

            # 안정층 -- 최소 동작 시간 안이면 바꾸지 않는다
            held = since is not None and (now - since) < pd.Timedelta(minutes=MIN_HOLD_MIN)
            if held:
                hold_until = since + pd.Timedelta(minutes=MIN_HOLD_MIN)
                rows.append({"node": r.node, "device": device, "state": was,
                             "rule": f"hold: min {MIN_HOLD_MIN}min", "regime": r.regime,
                             "hold_until": hold_until.isoformat(), "value": float(val)})
                continue

            # 판단층 -- 히스테리시스
            if candidate and val > on_thr:
                new, rule = 1, f"{r.regime} ∧ {sensor}>{on_thr:.0f}"
            elif val < off_thr:
                new, rule = 0, f"{sensor}<{off_thr:.0f}"
            elif candidate:
                # 임계 사이(off~on): 이전 상태 유지. 켜져 있으면 계속, 꺼져 있으면 대기.
                new = was
                rule = (f"hold: {sensor}>{off_thr:.0f}" if was
                        else f"{r.regime} but {sensor}<{on_thr:.0f} → wait")
            else:
                new, rule = was, f"{r.regime}: not a candidate"

            rows.append({"node": r.node, "device": device, "state": new, "rule": rule,
                         "regime": r.regime, "value": float(val),
                         "hold_until": (now + pd.Timedelta(minutes=MIN_HOLD_MIN)).isoformat()
                                       if new != was else None})
    return pd.DataFrame(rows)


# ============================================================================
# 9단계 -- 예측. 30분 뒤의 실측값을 타깃으로.
# ============================================================================
def build_forecast_frame(d: pd.DataFrame, *, horizon: int = HORIZON_BUCKETS) -> pd.DataFrame:
    """파생 feature 는 과거만 보고, 타깃은 미래 실측(shift(-horizon)).

    마지막 horizon 행은 타깃이 없어 학습에서 빠진다(누수 없음).
    비율이 아니라 절대값을 타깃으로 두어야 규칙층의 임계와 단위가 맞는다.
    """
    out = []
    for node, g in d.sort_values(["node", "bucket"]).groupby("node", sort=False):
        g = g.copy()
        g["d_co2_10m"] = g.co2.diff(2)                                   # 기울기 (2버킷=10분)
        g["d_voc_10m"] = g.voc.diff(2)
        g["co2_mean_1h"] = g.co2.rolling(12, min_periods=3).mean()       # 누적 (12버킷=1시간)
        g["voc_mean_1h"] = g.voc.rolling(12, min_periods=3).mean()
        g["y_co2_30"] = g.co2.shift(-horizon)                            # 타깃: 30분 뒤 실측
        g["y_voc_30"] = g.voc.shift(-horizon)
        out.append(g)
    return pd.concat(out, ignore_index=True) if out else d
