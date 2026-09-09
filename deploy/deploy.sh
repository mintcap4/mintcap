#!/usr/bin/env bash
# mintcap 자동 배포 스크립트 (Arduino Q에서 mintcap-deploy.timer 가 1분마다 실행)
#
# 동작: origin/main 을 확인해서 로컬과 다르면 git reset --hard 로 맞추고,
#       바뀐 파일에 따라 필요한 user 서비스만 재시작한다.
#       git clean 은 하지 않는다 → .venv / sensor_data.db / 백업 CSV 보존.
#
# 이 스크립트는 arduino 사용자 권한으로만 실행된다 (sudo 불필요).
set -euo pipefail

REPO="${MINTCAP_REPO:-$HOME/mintcap}"
UV="$HOME/.local/bin/uv"
BRANCH="main"

cd "$REPO"

# 네트워크가 죽어 있으면 조용히 종료 (다음 타이머 때 재시도)
if ! git fetch --quiet origin "$BRANCH" 2>/dev/null; then
    exit 0
fi

LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse "origin/$BRANCH")

if [ "$LOCAL" = "$REMOTE" ]; then
    exit 0
fi

CHANGED=$(git diff --name-only "$LOCAL" "$REMOTE")
echo "[mintcap-deploy] $LOCAL -> $REMOTE"
echo "$CHANGED" | sed 's/^/  changed: /'

git reset --hard "origin/$BRANCH"

restart_hub=0
restart_dash=0
reload_units=0

while IFS= read -r f; do
    case "$f" in
        pyproject.toml|uv.lock)          "$UV" sync --frozen ;;
        deploy/systemd/*)                reload_units=1 ;;
    esac
    case "$f" in
        hub.py|nodes.json)               restart_hub=1 ;;
    esac
    case "$f" in
        dashboard.py|nodes.json)         restart_dash=1 ;;
        static/*|.streamlit/*)           restart_dash=1 ;;
    esac
done <<< "$CHANGED"

if [ "$reload_units" = 1 ]; then
    mkdir -p "$HOME/.config/systemd/user"
    cp deploy/systemd/mintcap-hub.service \
       deploy/systemd/mintcap-dashboard.service \
       deploy/systemd/mintcap-deploy.service \
       deploy/systemd/mintcap-deploy.timer \
       "$HOME/.config/systemd/user/"
    systemctl --user daemon-reload
    restart_hub=1
    restart_dash=1
fi

[ "$restart_hub" = 1 ]  && systemctl --user restart mintcap-hub      && echo "  restarted: mintcap-hub"
[ "$restart_dash" = 1 ] && systemctl --user restart mintcap-dashboard && echo "  restarted: mintcap-dashboard"

logger -t mintcap-deploy "deployed ${LOCAL:0:7} -> ${REMOTE:0:7} (hub=$restart_hub dash=$restart_dash)" || true
echo "[mintcap-deploy] done"
