"""웹 백엔드 -- analysis 테이블을 JSON 으로 내보내기만 한다.

읽기 전용이다. 계산은 전부 analysis/runner.py 가 배치로 끝내 두었고, 여기서는
가장 최근 run_at 의 행을 골라 payload 를 그대로 돌려준다. 그래서 프런트를 바꿔도
분석 코드는 손대지 않는다.

    uv run uvicorn web.api:app --host 0.0.0.0 --port 8501

시계열(/api/timeseries)만 readings 를 직접 읽는다 -- 원본을 그리는 화면이라
배치를 거칠 이유가 없고, 5분 버킷이라 양도 많지 않다.
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

DB_PATH = os.environ.get("MINTCAP_DB", "sensor_data.db")
NODES_PATH = os.environ.get("MINTCAP_NODES", "nodes.json")
ROOT = Path(__file__).resolve().parent

app = FastAPI(title="mintcap", docs_url="/api/docs", openapi_url="/api/openapi.json")


# ---------------------------------------------------------------- 공통
def db() -> sqlite3.Connection:
    """읽기 전용 커넥션. 웹이 실수로 쓰는 일이 없도록 URI 모드로 연다."""
    if not Path(DB_PATH).is_file():
        raise HTTPException(503, f"DB 없음: {DB_PATH}")
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con


def latest(kind: str, scope: str | None = None) -> list[dict]:
    """kind 의 가장 최근 run_at 행들. scope 를 주면 그 하나만.

    같은 kind 안에서도 scope 별로 run_at 이 다를 수 있으므로(노드가 늦게 들어온
    경우) scope 마다 자기 최신 행을 고른다.
    """
    sql = """
        SELECT a.* FROM analysis a
        JOIN (SELECT kind, scope, MAX(id) AS id FROM analysis
              WHERE kind = ? GROUP BY kind, scope) m
          ON a.id = m.id
    """
    params: list = [kind]
    if scope:
        sql += " AND a.scope = ?"
        params.append(scope)
    with db() as con:
        rows = con.execute(sql + " ORDER BY a.scope", params).fetchall()
    return [{"scope": r["scope"], "run_at": r["run_at"], "model_ver": r["model_ver"],
             "win_start": r["win_start"], "win_end": r["win_end"],
             **json.loads(r["payload"])} for r in rows]


def node_labels() -> dict[str, str]:
    """nodes.json 의 노드ID -> 표시 이름. 없으면 빈 매핑."""
    try:
        return json.loads(Path(NODES_PATH).read_text(encoding="utf-8"))
    except Exception:
        return {}


# ---------------------------------------------------------------- 메타
@app.get("/api/health")
def health() -> dict:
    """DB 가 살아 있는지, 마지막 수집·마지막 배치가 언제인지."""
    try:
        with db() as con:
            last_reading = con.execute("SELECT MAX(ts) FROM readings").fetchone()[0]
            n_readings = con.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
            try:
                last_run = con.execute("SELECT MAX(run_at) FROM analysis").fetchone()[0]
                n_analysis = con.execute("SELECT COUNT(*) FROM analysis").fetchone()[0]
            except sqlite3.OperationalError:
                last_run, n_analysis = None, 0        # 배치를 한 번도 안 돌린 상태
    except HTTPException as e:
        return JSONResponse({"ok": False, "error": e.detail}, status_code=503)

    stale = None
    if last_reading:
        age = datetime.now(timezone.utc).replace(tzinfo=None) - datetime.fromisoformat(last_reading)
        stale = age > timedelta(minutes=15)      # 5분 버킷 기준 3회 연속 결측
    return {"ok": True, "db": DB_PATH,
            "last_reading": last_reading, "readings": n_readings, "stale": stale,
            "last_run": last_run, "analysis_rows": n_analysis}


@app.get("/api/nodes")
def nodes() -> list[dict]:
    """노드 목록 + 마지막 수신 시각. 화면의 노드 선택기가 쓴다."""
    with db() as con:
        rows = con.execute(
            "SELECT node, COUNT(*) n, MAX(ts) last_ts FROM readings GROUP BY node ORDER BY node"
        ).fetchall()
    lab = node_labels()
    return [{"node": r["node"], "label": lab.get(r["node"], r["node"]),
             "rows": r["n"], "last_ts": r["last_ts"]} for r in rows]


# ---------------------------------------------------------------- 탭별
@app.get("/api/summary")
def summary() -> dict:
    """모니터링 탭 상단 요약 카드."""
    r = latest("summary", "all")
    return r[0] if r else {"nodes": 0, "note": "배치를 아직 돌리지 않았습니다"}


@app.get("/api/regime")
def regime(node: str | None = None) -> list[dict]:
    """진단추론 탭 -- 노드별 현재 레짐 · 확신도 · 체류."""
    lab = node_labels()
    return [{**r, "label": lab.get(r["scope"], r["scope"])} for r in latest("regime_now", node)]


@app.get("/api/action")
def action(node: str | None = None) -> list[dict]:
    """제어경보 탭 -- 행동지침. 실제 장비 제어는 하지 않고 판정만 기록한다."""
    lab = node_labels()
    return [{**r, "label": lab.get(r["scope"], r["scope"])} for r in latest("action", node)]


@app.get("/api/forecast")
def forecast(node: str | None = None) -> list[dict]:
    """제어경보 탭 -- 30분 뒤 예측과 선제 경보."""
    lab = node_labels()
    return [{**r, "label": lab.get(r["scope"], r["scope"])} for r in latest("forecast", node)]


@app.get("/api/transition")
def transition() -> dict:
    """진단추론 탭 -- 전이행렬 · 체류 분포."""
    r = latest("transition", "all")
    return r[0] if r else {"regimes": [], "matrix": [], "note": "일간 배치를 아직 돌리지 않았습니다"}


@app.get("/api/band")
def band(node: str | None = None) -> list[dict]:
    """진단추론 탭 -- 시간대별 레짐 밴드(스위칭 밴드)."""
    lab = node_labels()
    return [{**r, "label": lab.get(r["scope"], r["scope"])} for r in latest("band", node)]


@app.get("/api/qc")
def qc(node: str | None = None) -> list[dict]:
    """관리 탭 -- 노드×일 유효율 게이트."""
    lab = node_labels()
    return [{**r, "label": lab.get(r["scope"], r["scope"])} for r in latest("qc", node)]


@app.get("/api/models")
def models() -> list[dict]:
    """관리 탭 -- 어떤 모델이 언제 학습됐고 성능이 어떤가."""
    return latest("model_event")


@app.get("/api/occ_co2")
def occ_co2(node: str | None = None) -> list[dict]:
    """모니터링 탭 -- 재실 x CO2. 조인이 성립하지 않으면 그 이유가 담겨 온다."""
    return latest("occ_co2", node)


# ---------------------------------------------------------------- 원본 시계열
SERIES_COLS = ("co2", "voc", "pm2p5", "scd_temp", "scd_hum", "pm10p0", "nox")


@app.get("/api/timeseries")
def timeseries(
    node: str = Query(..., description="노드 ID"),
    hours: int = Query(24, ge=1, le=24 * 30),
    cols: str = Query("co2,voc,pm2p5,scd_temp", description="쉼표로 구분"),
) -> dict:
    """readings 원본을 5분 버킷 그대로 돌려준다(모니터링 탭 차트)."""
    want = [c for c in cols.split(",") if c in SERIES_COLS]
    if not want:
        raise HTTPException(400, f"cols 는 {SERIES_COLS} 중에서 고르세요")
    since = (datetime.now(timezone.utc).replace(tzinfo=None)
             - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    with db() as con:
        rows = con.execute(
            f"SELECT ts, {','.join(want)} FROM readings WHERE node = ? AND ts >= ?"
            " ORDER BY ts", (node, since)).fetchall()
    return {"node": node, "hours": hours, "cols": want,
            "rows": [dict(r) for r in rows]}


# ---------------------------------------------------------------- 정적 파일
# /static 은 저장소의 디자인 토큰(tokens.css 등)을 그대로 재사용한다.
_static = ROOT.parent / "static"
if _static.is_dir():
    app.mount("/static", StaticFiles(directory=_static), name="static")


def _file(name: str, media: str) -> FileResponse:
    p = ROOT / name
    if not p.is_file():
        raise HTTPException(404, name)
    # 배포하면 파일이 바뀌는데 파일명에 해시가 없다. 재검증(no-cache)만으로는
    # 브라우저가 옛 사본을 계속 쓰는 경우가 있어 아예 저장하지 않게 한다.
    # 셋 다 수십 KB 라 매번 받아도 부담이 없다.
    return FileResponse(p, media_type=media, headers={"Cache-Control": "no-store"})


@app.get("/")
def index() -> FileResponse:
    return _file("index.html", "text/html; charset=utf-8")


@app.get("/app.css")
def app_css() -> FileResponse:
    return _file("app.css", "text/css; charset=utf-8")


@app.get("/app.js")
def app_js() -> FileResponse:
    return _file("app.js", "text/javascript; charset=utf-8")
