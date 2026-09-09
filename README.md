# mintcap

Arduino UNO Q 에서 도는 실내 공기질(멀티노드) 모니터링 시스템.
**GitHub 가 코드 원본이고, 장치는 이 저장소를 1분마다 pull 해서 자동 반영한다.**

```
센서 노드 --MQTT/TLS--> HiveMQ Cloud --구독--> hub.py --> sensor_data.db
                                                           │  readings / occupancy (원본)
                                           analysis/runner ┤  매시간·매일 배치
                                                           ↓  analysis (결과)
                                                         web/  FastAPI + 정적 HTML (:8501)
```

| 구성 | 설명 |
|---|---|
| `hub.py` | MQTT 구독 → SQLite 기록 (`mintcap-hub`) |
| `analysis/` | 진단·추론 파이프라인. 매시간·매일 배치로 결과를 `analysis` 테이블에 기록 |
| `models/` | 학습된 GMM·예측 모델(JSON). 장치는 numpy 로만 추론 |
| `web/` | FastAPI + 정적 HTML 대시보드 `:8501` (모니터링 · 진단추론 · 제어경보 · 관리) |
| `static/` | 디자인 토큰 CSS |
| `deploy/` | systemd user 유닛 + 자동배포 스크립트 |

### 진단 · 추론

5분 버킷 정렬 → 범위 규칙·QC 게이트(노드×일 95%) → 고정 스케일(CO₂/400, VOC/100)
→ GMM 레짐 판정(청정·물질·인체·복합) → 45분 다수결 평활 → 전이·체류 →
규칙층(히스테리시스 + 최소 동작 10분) → 30분 예측.
자세한 내용은 [CLAUDE.md](CLAUDE.md) 와 `docs/` 참조.

## 코드를 고치고 싶을 때

### 방법 A — 이 PC 의 Claude Code (기본)

1. 로컬 클론 폴더를 Claude Code 로 연다.
2. "dashboard 상단에 24시간 평균 CO2 카드 추가해줘" 처럼 지시한다.
3. Claude 가 고치고 커밋·push (또는 브랜치+PR).
4. `main` 에 들어가면 **1분 안에** Q 에 반영된다.

```bash
git clone https://github.com/mintcap4/mintcap.git
cd mintcap && uv sync
uv run uvicorn web.api:app --reload --port 8511    # 로컬 미리보기
```

### 방법 B — GitHub 에서 @claude 멘션 (나중에 활성화)

`.github/workflows/claude.yml` 이 이미 있음. 활성화하려면:
1. 저장소에 **Claude GitHub App** 설치 — https://github.com/apps/claude
2. `Settings → Secrets and variables → Actions` 에 `ANTHROPIC_API_KEY` 추가
   (Anthropic Console 에서 발급, 사용량 과금).
3. 이후 이슈/PR 에 `@claude dashboard 에 ... 추가해줘` → Claude 가 PR 생성.

## 장치 배포 구조

`deploy/deploy.sh` 가 `mintcap-deploy.timer` (1분 간격) 로 실행된다:

- `git fetch` → `origin/main` 과 로컬 HEAD 가 다르면 `git reset --hard origin/main`
- 바뀐 파일에 따라: `pyproject.toml`/`uv.lock` → `uv sync`,
  `hub.py` → `mintcap-hub` 재시작, `web/`·`analysis/`·`models/` → `mintcap-web` 재시작,
  `deploy/systemd/*` → 유닛 동기화(저장소에서 지운 유닛은 장치에서도 제거) 후 재시작
- `git clean` 은 안 함 → `.venv`, `sensor_data.db`, 백업 CSV 보존

`main` 에 들어간 것은 그대로 배포된다. **push 전에 로컬에서 확인할 것.**

## 최초 설치 (장치에서 1회)

```bash
# 1. 클론
git clone https://github.com/mintcap4/mintcap.git ~/mintcap && cd ~/mintcap

# 2. 비밀값
mkdir -p ~/.config/mintcap
cp deploy/mintcap.env.example ~/.config/mintcap/mintcap.env
chmod 600 ~/.config/mintcap/mintcap.env
nano ~/.config/mintcap/mintcap.env      # MQTT_* 채우기

# 3. 설치 (user 서비스 + uv sync + 기동)
bash deploy/install.sh

# 4. 부팅 자동기동 + 옛 서비스 정리 (root 1회)
sudo loginctl enable-linger $USER
sudo systemctl disable --now multinode_aq_hub multinode_aq_dashboard
```

## 상태 확인

```bash
systemctl --user status mintcap-hub mintcap-web
systemctl --user list-timers | grep mintcap
journalctl --user -u mintcap-analysis-hourly -n 30 --no-pager
journalctl --user -u mintcap-deploy -n 30 --no-pager
```
