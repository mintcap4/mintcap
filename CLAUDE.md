# CLAUDE.md — mintcap 저장소 가이드

## 이 저장소가 하는 일

Arduino UNO Q (`mintcap`, Debian 13 / aarch64) 에서 도는 **실내 공기질 모니터링 ·
진단 · 추론 시스템**의 코드 원본. GitHub 가 단일 원본이고, Q 는 이 저장소를 1분마다
pull 해서 자동 반영한다 (**pull 전용 — Q 에서 직접 파일을 고치지 않는다**).

```
센서 노드 --MQTT/TLS--> HiveMQ Cloud --구독--> hub.py --> sensor_data.db
                                                            │  readings / occupancy (원본)
                                            analysis/runner ┤  매시간·매일 배치
                                                            ↓  analysis / actuator_state (결과)
                                                          web/  FastAPI + 정적 HTML (:8501)
```

## 구성 요소

| 경로 | 역할 | 실행 주체 |
|---|---|---|
| `hub.py` | MQTT 구독 → `sensor_data.db` 기록. 토픽 `multinode_aq/+/env`, `.../+/occ` | `mintcap-hub` |
| `analysis/` | 진단·추론 파이프라인 (아래 참조) | `mintcap-analysis-{hourly,daily}.timer` |
| `models/` | 학습된 모델 JSON. 장치는 이것만 읽고 판정한다 | — |
| `web/` | FastAPI 백엔드 + 정적 프런트(4탭) | `mintcap-web` (:8501) |
| `static/` | 디자인 토큰 CSS. 웹이 `/static` 으로 재사용 | — |
| `nodes.json` | 노드 ID → 표시 이름 (선택) | hub · web |
| `deploy/` | systemd user 유닛 + 배포 스크립트 | Q |

## 파이프라인 (`analysis/`)

`docs/` 의 파이프라인 문서 0~9단계를 그대로 구현한다. **원본은 건드리지 않는다** —
`readings`/`occupancy` 는 읽기만 하고 결과는 `analysis` 테이블에만 쓴다.

| 단계 | 내용 | 위치 |
|---|---|---|
| 0 | 적재 | `pipeline.load_readings` |
| 1 | `bucket = floor(ts, 5min)`, 키 = (node, bucket) | `to_buckets` |
| 2 | 범위 규칙 → NaN · 노드×일 95% 게이트. **보간하지 않는다** | `apply_range_rules` / `qc_gate` |
| 3 | Spearman — 공선 축 제거 (PM2.5 하나, 온도는 SCD 하나) | `spearman` |
| 4 | 고정 스케일 `x_co2 = co2/400`, `x_voc = voc/100` | `add_model_input` |
| 5 | GMM 레짐 판정 | `regimes.RegimeModel` |
| 6 | 45분 다수결 평활, gap 에서 창을 끊음 | `smooth_regime` |
| 7 | 전이행렬 · 체류 | `transition_matrix` / `dwell_segments` |
| 8 | 규칙층 (진단·판단·안정 3층) | `decide_actions` |
| 9 | 30분 예측 | `forecast.ForecastModel` |
| 10 | 결과를 `analysis` 테이블에 저장 | `runner.py` |

### 모델 (`models/`)

**학습된 모델은 JSON 계수만 싣고 장치에서는 numpy 로만 추론한다.** 그래서 Q 에
scikit-learn 을 설치하지 않는다(aarch64 빌드가 무겁다). 학습할 때만
`uv sync --group train`.

- `gmm_v1.json` — 레짐. **k=6 으로 밀도를 잡고 중심의 분면으로 이름 붙여 4개로 병합**한다.
  문서는 k=4 지만 이 데이터에서 k=4 는 CO₂ 축 분산이 커 `clean` 을 둘로 쪼개고
  `matter` 를 놓친다. 분면 중심을 초기값으로 강제해도 seed 7회 중 5회가 이름 충돌로
  실패했다. 출력 레짐은 여전히 4개라 대응 조치 4개와 1:1 로 맞는다.
  앵커(CO₂ 700 · VOC 120)는 **이름표에만 쓰이고 학습에는 관여하지 않는다.**
- `forecast_v1.json` — 30분 예측(능형회귀). **성능이 약하다**: 지속 기준선 대비
  CO₂ +5.1% · VOC +3.2%. R² 0.82 는 CO₂ 자기상관 때문이므로 그 숫자만 보면 안 된다.

재학습:
```bash
uv sync --group train
uv run python -m analysis.train_gmm --csv <readings.csv>       # 또는 --db sensor_data.db
uv run python -m analysis.train_forecast --csv <readings.csv>
```
학습 스크립트는 4분면 커버리지를 검증하고, 못 덮으면 시드를 바꿔 재시도한 뒤
그래도 안 되면 실패한다(레짐 하나를 영영 판정 못 하는 모델을 내보내지 않는다).

## 비밀값 — 절대 커밋 금지

`hub.py` 는 MQTT 접속 정보를 환경변수에서 읽는다:
`MQTT_BROKER`, `MQTT_PORT`, `MQTT_USERNAME`, `MQTT_PASSWORD`.
실제 값은 Q 의 `~/.config/mintcap/mintcap.env` (chmod 600) 에만 있다.
템플릿은 `deploy/mintcap.env.example`. 코드에 하드코딩하지 말 것.

## 웹 (`web/`)

- **계산은 배치가, 표시는 읽기만.** `api.py` 는 `analysis` 테이블을 JSON 으로
  내보낼 뿐이다. 무거운 계산을 요청 경로에 넣지 말 것.
  예외는 `/api/timeseries` 하나 — 원본을 그리는 화면이라 배치를 거칠 이유가 없다.
- **외부 차트 라이브러리를 쓰지 않는다.** 장치가 인터넷 없이도 떠야 하고, 필요한
  도형이 선·점·사각형뿐이라 인라인 SVG 로 충분하다. CDN 을 추가하지 말 것.
- 색은 `dataviz` 검증을 돌려서 정했다. 레짐 4색은 dark 표면 `#1C1C1E` 에서
  인접 쌍 전 검사를 통과한다(최악 CVD ΔE 9.4). **색을 바꾸면 검증을 다시 돌릴 것.**
  레짐 산점도는 4색 산점도가 `--pairs all` 을 통과하지 못해 색 대신 위치로 읽는다.
- 시계열은 변수마다 차트를 따로 둔다. **이중 축을 만들지 말 것.**

## 배포 방식 (수정 → 반영)

1. 이 저장소에서 코드를 고치고 `main` 에 push (또는 PR merge).
2. Q 의 `mintcap-deploy.timer` 가 1분 안에 `deploy/deploy.sh` 실행:
   `git fetch` → 다르면 `git reset --hard` → 바뀐 파일에 따라 `uv sync`,
   유닛 동기화, 해당 서비스만 재시작.
3. `git clean` 은 안 함 → `.venv`, `sensor_data.db`, 백업 CSV 보존.
4. `deploy/systemd/` 에서 유닛을 **지우면** 배포 때 Q 에서도 멈추고 지워진다.

**`main` 에 들어간 것은 1분 뒤 Q 에서 그대로 돈다.** 문법 오류도 그대로 배포되므로
push 전에 로컬에서 확인할 것.

## 로컬 개발

```bash
uv sync
uv run uvicorn web.api:app --reload --port 8511     # MINTCAP_DB 로 DB 지정 가능
uv run python -m analysis.runner --db sensor_data.db --kinds all
```
Python **3.13**, 의존성은 `uv`. 새 패키지는 `uv add`, 그다음 `uv lock` 을 커밋해야
장치의 `uv sync --frozen` 이 통과한다.

## 코드 규칙

- 스키마(`readings`, `occupancy`)를 바꾸면 기존 DB 와의 호환을 고려.
  `CREATE TABLE IF NOT EXISTS` 는 기존 테이블을 바꾸지 않는다.
- 결과가 비면 **왜 비었는지를 기록한다.** 조용히 빠지면 화면에서 원인을 알 수 없다
  (`runner.run_daily` 의 `occ_co2` 참조).
- 임계·창 크기 같은 상수는 `analysis/pipeline.py` 상단에 모여 있다. 흩뿌리지 말 것.

## Q 에서 상태 확인 (SSH: `ssh arduino-q`)

```bash
systemctl --user status mintcap-hub mintcap-web
systemctl --user list-timers | grep mintcap
journalctl --user -u mintcap-analysis-hourly -n 30 --no-pager
journalctl --user -u mintcap-deploy -n 30 --no-pager
```
