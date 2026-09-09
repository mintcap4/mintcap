"""분석 계층이 쓰는 테이블 -- hub.py 가 만드는 readings/occupancy 와는 별개.

설계 원칙(파이프라인 문서 0단계): 원본은 건드리지 않는다.
hub.py 는 계속 readings/occupancy 에만 쓰고, 분석 결과는 여기 정의한
analysis / actuator_state 에만 쌓인다. 웹은 이 두 테이블만 읽는다.
"""
from __future__ import annotations

import sqlite3

# analysis -- 한 행 = 한 종류(kind)의 결과 한 벌. payload 는 JSON 문자열.
#   kind        주기      화면
#   qc          hourly    관리 · 유효범위
#   regime_now  hourly    진단추론 · 현재 레짐
#   band        daily     진단추론 · 스위칭 밴드
#   transition  daily     진단추론 · 전이/체류
#   action      hourly    제어경보 · 행동지침
#   forecast    hourly    제어경보 · 예측
#   occ_co2     daily     모니터링 · 재실 x CO2
#   model_event weekly    관리 · 모델 이력
#   summary     daily     모니터링 · 요약 카드
SCHEMA = """
CREATE TABLE IF NOT EXISTS analysis(
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  run_at    TEXT NOT NULL,           -- 이 결과를 계산한 시각 (UTC)
  kind      TEXT NOT NULL,           -- qc | regime_now | band | transition | ...
  scope     TEXT NOT NULL,           -- node_XXXX 또는 'all'
  win_start TEXT,                    -- 집계 창 시작 (해당 없으면 NULL)
  win_end   TEXT,
  model_ver TEXT,                    -- 'v1' 등. 모델을 안 쓰는 kind 는 NULL
  payload   TEXT NOT NULL            -- JSON
);
CREATE INDEX IF NOT EXISTS ix_analysis_lookup ON analysis(kind, scope, run_at DESC);

-- 히스테리시스 기억. 재시작해도 유지되도록 DB 에 둔다(파이프라인 8단계).
CREATE TABLE IF NOT EXISTS actuator_state(
  node   TEXT NOT NULL,
  device TEXT NOT NULL,              -- fan | purifier
  state  INTEGER NOT NULL,           -- 0 | 1
  since  TEXT NOT NULL,              -- 이 상태가 된 시각 (UTC) -- 최소 동작 시간 판정용
  PRIMARY KEY(node, device)
);
"""


def connect(db_path: str, *, read_only: bool = False) -> sqlite3.Connection:
    """분석/웹 공용 커넥션. 읽기 전용 쪽은 실수로 쓰지 못하게 URI 모드로 연다."""
    if read_only:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, check_same_thread=False)
    else:
        con = sqlite3.connect(db_path, check_same_thread=False)
        con.execute("PRAGMA journal_mode=WAL")   # hub.py 쓰기와 동시 읽기 허용
    con.row_factory = sqlite3.Row
    return con


def ensure(con: sqlite3.Connection) -> None:
    """analysis/actuator_state 가 없으면 만든다. 기존 테이블은 건드리지 않는다."""
    con.executescript(SCHEMA)
    con.commit()
