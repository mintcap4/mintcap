#!/usr/bin/env bash
# mintcap 최초 설치 (Arduino Q에서 1회 실행, arduino 사용자 권한)
#
# 하는 일:
#   1) ~/.config/systemd/user/ 에 user 서비스 유닛 설치
#   2) uv sync 로 .venv 구성
#   3) hub / dashboard / deploy.timer 활성화 및 기동
#
# 사전 조건:
#   - ~/mintcap 이 이 저장소의 클론이어야 함
#   - ~/.config/mintcap/mintcap.env 에 MQTT_* 값이 있어야 함 (아래에서 확인)
#   - 부팅 시 자동 기동을 원하면 root 로 1회:  sudo loginctl enable-linger $USER
set -euo pipefail

REPO="${MINTCAP_REPO:-$HOME/mintcap}"
UV="$HOME/.local/bin/uv"
ENV_FILE="$HOME/.config/mintcap/mintcap.env"
USER_UNIT_DIR="$HOME/.config/systemd/user"

cd "$REPO"

echo "== 1. 비밀값 파일 확인 =="
if [ ! -f "$ENV_FILE" ]; then
    echo "!! $ENV_FILE 이 없습니다."
    echo "   deploy/mintcap.env.example 을 복사해 실제 값을 채운 뒤 다시 실행하세요:"
    echo "     mkdir -p ~/.config/mintcap"
    echo "     cp deploy/mintcap.env.example ~/.config/mintcap/mintcap.env"
    echo "     chmod 600 ~/.config/mintcap/mintcap.env && \$EDITOR ~/.config/mintcap/mintcap.env"
    exit 1
fi
chmod 600 "$ENV_FILE"
echo "   OK: $ENV_FILE"

echo "== 2. Python 의존성 (uv sync) =="
"$UV" sync --frozen
echo "   OK: $REPO/.venv"

echo "== 3. user systemd 유닛 설치 =="
mkdir -p "$USER_UNIT_DIR"
cp deploy/systemd/mintcap-hub.service \
   deploy/systemd/mintcap-dashboard.service \
   deploy/systemd/mintcap-deploy.service \
   deploy/systemd/mintcap-deploy.timer \
   "$USER_UNIT_DIR/"
systemctl --user daemon-reload
echo "   OK: $USER_UNIT_DIR"

echo "== 4. 서비스 활성화 및 기동 =="
systemctl --user enable --now mintcap-hub.service
systemctl --user enable --now mintcap-dashboard.service
systemctl --user enable --now mintcap-deploy.timer
echo "   OK"

echo
echo "== 상태 =="
systemctl --user --no-pager --lines=0 status mintcap-hub mintcap-dashboard mintcap-deploy.timer || true
echo
echo "다음(root 1회, 아직 안 했다면):"
echo "  sudo loginctl enable-linger $USER"
echo "  sudo systemctl disable --now multinode_aq_hub multinode_aq_dashboard"
echo
echo "대시보드:  http://<이 장치 IP>:8501"
