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
UNIT_DIR="$HOME/.config/systemd/user"
BRANCH="main"

cd "$REPO"

# ---- 유닛 동기화 -------------------------------------------------------------
# 저장소에서 사라진 유닛은 복사만으로는 없어지지 않는다. 남겨두면 옛 서비스가
# 계속 돌면서 포트를 잡고 있으므로(예: streamlit -> uvicorn 전환), 저장소에
# 없는 mintcap-* 유닛은 멈추고 지운다.
sync_units() {
    mkdir -p "$UNIT_DIR"
    local repo_units=() f base
    for f in deploy/systemd/mintcap-*.service deploy/systemd/mintcap-*.timer; do
        [ -e "$f" ] || continue
        base=$(basename "$f")
        repo_units+=("$base")
        cp "$f" "$UNIT_DIR/$base"
    done

    for f in "$UNIT_DIR"/mintcap-*.service "$UNIT_DIR"/mintcap-*.timer; do
        [ -e "$f" ] || continue
        base=$(basename "$f")
        if [[ ! " ${repo_units[*]} " == *" $base "* ]]; then
            echo "  제거된 유닛: $base"
            systemctl --user disable --now "$base" 2>/dev/null || true
            rm -f "$f"
        fi
    done
    systemctl --user daemon-reload

    # 새로 생긴 유닛은 활성화해 둔다(이미 활성인 것은 그대로).
    for base in "${repo_units[@]}"; do
        case "$base" in
            *.timer) systemctl --user enable --now "$base" >/dev/null 2>&1 || true ;;
            mintcap-hub.service|mintcap-web.service)
                systemctl --user enable "$base" >/dev/null 2>&1 || true ;;
        esac
    done
}

# 설치된 유닛이 저장소와 다른가? -- 커밋 차이와 무관하게 매번 본다.
#
# 배포 스크립트는 자기 자신도 교체한다. 옛 스크립트가 새 커밋을 받다가 중간에
# 죽으면 저장소만 앞서 가고 유닛은 옛것으로 남는데, 그 다음 틱부터는 HEAD 가
# origin 과 같아 조기 종료해 영영 고쳐지지 않는다. 그래서 '커밋이 바뀌었나'가
# 아니라 '실제로 다른가'를 기준으로 삼는다.
units_drifted() {
    local f base
    for f in deploy/systemd/mintcap-*.service deploy/systemd/mintcap-*.timer; do
        [ -e "$f" ] || continue
        base=$(basename "$f")
        cmp -s "$f" "$UNIT_DIR/$base" || return 0
    done
    for f in "$UNIT_DIR"/mintcap-*.service "$UNIT_DIR"/mintcap-*.timer; do
        [ -e "$f" ] || continue
        [ -e "deploy/systemd/$(basename "$f")" ] || return 0
    done
    return 1
}

# ---- 여기서부터 실제 배포 ----------------------------------------------------
# 네트워크가 죽어 있으면 조용히 종료 (다음 타이머 때 재시도)
if ! git fetch --quiet origin "$BRANCH" 2>/dev/null; then
    exit 0
fi

LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse "origin/$BRANCH")

if [ "$LOCAL" = "$REMOTE" ]; then
    # 커밋은 최신인데 유닛만 뒤처진 경우를 여기서 따라잡는다.
    if units_drifted; then
        echo "[mintcap-deploy] 유닛이 저장소와 다름 -- 동기화"
        sync_units
        systemctl --user restart mintcap-hub mintcap-web || true
    fi
    exit 0
fi

CHANGED=$(git diff --name-only "$LOCAL" "$REMOTE")
echo "[mintcap-deploy] $LOCAL -> $REMOTE"
echo "$CHANGED" | sed 's/^/  changed: /'

git reset --hard "origin/$BRANCH"

restart_hub=0
restart_web=0
units_changed=0
deps_changed=0

while IFS= read -r f; do
    case "$f" in
        pyproject.toml|uv.lock)          deps_changed=1 ;;
        deploy/systemd/*)                units_changed=1 ;;
    esac
    case "$f" in
        hub.py|nodes.json)               restart_hub=1 ;;
    esac
    case "$f" in
        # 웹은 analysis 를 import 하고 모델을 읽으므로 그쪽이 바뀌어도 재시작한다
        web/*|nodes.json|analysis/*|models/*) restart_web=1 ;;
    esac
done <<< "$CHANGED"

if [ "$deps_changed" = 1 ]; then
    "$UV" sync --frozen
    restart_hub=1
    restart_web=1
fi

if [ "$units_changed" = 1 ]; then
    sync_units
    restart_hub=1
    restart_web=1
fi

if [ "$restart_hub" = 1 ]; then
    systemctl --user restart mintcap-hub && echo "  restarted: mintcap-hub"
fi
if [ "$restart_web" = 1 ]; then
    systemctl --user restart mintcap-web && echo "  restarted: mintcap-web"
fi

logger -t mintcap-deploy "deployed ${LOCAL:0:7} -> ${REMOTE:0:7} (hub=$restart_hub web=$restart_web)" || true
echo "[mintcap-deploy] done"
