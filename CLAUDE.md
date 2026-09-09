# CLAUDE.md — mintcap 저장소 가이드

## 이 저장소가 하는 일

Arduino UNO Q (`mintcap`, Debian 13 / aarch64) 에서 도는 **실내 공기질 모니터링 시스템**의
코드 원본. GitHub 가 단일 원본이고, Q 는 이 저장소를 1분마다 pull 해서 자동 반영한다
(**pull 전용 — Q 에서 직접 파일을 고치지 않는다**).

```
센서 노드(ESP32 등)  --MQTT/TLS-->  HiveMQ Cloud  --구독-->  hub.py  -->  sensor_data.db  -->  dashboard.py (Streamlit :8501)
```

## 구성 요소

| 파일 | 역할 | 실행 주체 |
|---|---|---|
| `hub.py` | HiveMQ Cloud(MQTT 8883/TLS) 구독 → `sensor_data.db` (SQLite) 기록. 토픽 `multinode_aq/+/env`, `multinode_aq/+/occ` | user 서비스 `mintcap-hub` |
| `dashboard.py` | Streamlit 대시보드(`0.0.0.0:8501`). `sensor_data.db` **읽기 전용** | user 서비스 `mintcap-dashboard` |
| `static/*.css`, `static/*.html` | 대시보드 프레젠테이션 계층. **Python 에 스타일 문자열을 넣지 않는다** (Plotly 색상 `SYS` dict 만 예외) | — |
| `nodes.json` | 노드 ID → 표시 이름 매핑 (선택) | 양쪽에서 읽음 |
| `.streamlit/config.toml` | Streamlit 기본 테마 | dashboard |
| `deploy/` | systemd user 유닛 + 배포 스크립트. 아래 참조 | Q |

## 비밀값 — 절대 커밋 금지

`hub.py` 는 MQTT 접속 정보를 **환경변수**에서 읽는다:
`MQTT_BROKER`, `MQTT_PORT`, `MQTT_USERNAME`, `MQTT_PASSWORD`.

실제 값은 Q 의 `~/.config/mintcap/mintcap.env` (chmod 600) 에만 있고 git 에 없다.
템플릿은 `deploy/mintcap.env.example`. 코드에 접속정보를 하드코딩하지 말 것.

## 배포 방식 (수정 → 반영)

1. 이 저장소에서 코드를 고치고 `main` 에 push (또는 PR merge).
2. Q 의 `mintcap-deploy.timer` 가 1분 안에 `deploy/deploy.sh` 실행:
   `git fetch` → `origin/main` 과 다르면 `git reset --hard` → 바뀐 파일에 따라
   `uv sync` (의존성 변경 시) 및 해당 서비스만 재시작.
3. `git clean` 은 하지 않음 → `.venv`, `sensor_data.db`, 백업 CSV 는 건드리지 않음.

즉 **`main` 에 들어간 것은 1분 뒤 Q 에서 그대로 돈다.** 문법 오류나 잘못된 커밋도
그대로 배포되므로, push 전에 로컬에서 확인할 것.

## 로컬 개발

```bash
uv sync
uv run streamlit run dashboard.py          # 대시보드 (로컬 DB 없으면 빈 화면)
uv run python hub.py                        # MQTT_* 환경변수 필요
```

- Python **3.13**, 의존성은 `uv` (`pyproject.toml` + `uv.lock`). 새 패키지는 `uv add <pkg>`.
- `hub.py` 를 로컬 실행하려면 `MQTT_*` 를 셸에 export 하거나
  `set -a; . ~/.config/mintcap/mintcap.env; set +a` (그 파일이 있는 환경에서).

## 코드 규칙

- `dashboard.py`: 스타일은 `static/` 로. 색상 토큰을 바꾸면 `static/tokens.css` 와
  `dashboard.py` 의 `SYS` / `BG` / `INK` 등을 **양쪽 다** 맞춰야 한다 (Plotly 는 CSS 를 못 읽음).
- `hub.py` 의 SQLite 스키마(`readings`, `occupancy`)를 바꾸면 기존 `sensor_data.db` 와의
  호환(마이그레이션)을 고려. `CREATE TABLE IF NOT EXISTS` 는 기존 테이블을 바꾸지 않는다.
- 서비스 유닛(`deploy/systemd/*`)을 바꾸면 `deploy.sh` 가 자동으로 재설치·재시작한다.

## Q 에서 상태 확인 (SSH: `ssh arduino-q`)

```bash
systemctl --user status mintcap-hub mintcap-dashboard mintcap-deploy.timer
journalctl --user -u mintcap-hub -n 50 --no-pager
journalctl --user -u mintcap-deploy -n 50 --no-pager
systemctl --user list-timers | grep mintcap
```
