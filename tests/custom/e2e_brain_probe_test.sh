#!/bin/bash
# e2e_brain_probe_test.sh — 大脑真实自检探针端到端测试
#
# 覆盖两条路径（失败路径此前从未被跑过，而它恰恰是探针存在的理由）：
#   V1 通过路径：临时 serve 起来 → 探针问 "1+1" → rc=0 且回复非空
#   V2 失败路径：serve 杀掉 → 探针在超时内 rc=1（不是挂死、不是误报通过）
#   V3 无凭据   ：拿不到 port → rc=2（「无法探测」，不该和「大脑坏了」混为一谈）
#
# 用独立端口 + 独立状态文件，不碰托管的 4096 实例。
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$SCRIPT_DIR" || exit 1

if [[ -z "${AGENT_OPENCODE_MODEL:-}" && -f "$SCRIPT_DIR/config/constants.local.sh" ]]; then
    # shellcheck disable=SC1091
    source "$SCRIPT_DIR/config/constants.local.sh"
fi

PORT="${E2E_PROBE_PORT:-47791}"
PW="e2e$(openssl rand -hex 6)"
TMP_STATE="$(mktemp -d)"
PROBE_TIMEOUT=20
SVPID=""

cleanup() {
    [[ -n "$SVPID" ]] && kill "$SVPID" 2>/dev/null
    pkill -f "opencode serve --port $PORT" 2>/dev/null
    rm -rf "$TMP_STATE"
}
trap cleanup EXIT

if ! command -v opencode >/dev/null 2>&1; then
    echo "SKIP: 未找到 opencode 可执行文件"
    exit 0
fi

FAIL=0
_assert() {  # _assert <描述> <期望> <实际>
    if [[ "$2" == "$3" ]]; then
        echo "  ✅ $1（$3）"
    else
        echo "  ❌ $1：期望 $2，实际 $3"
        FAIL=1
    fi
}

# 探针默认从 .serve.port 发现凭据；这里显式用 AGENT_PROBE_PORT 指向临时实例，
# 与 healthcheck 传参给探针的方式一致。
_run_probe() {
    AGENT_PROBE_PORT="$1" AGENT_PROBE_PWD="$2" \
    PROJECT_DIR="$SCRIPT_DIR" HEALTHCHECK_BRAIN_PROBE_TIMEOUT="$PROBE_TIMEOUT" \
    AGENT_EXT_SESSION_FILE="$TMP_STATE/.ext-sessions" \
        python3 "$SCRIPT_DIR/bin/custom/brain_probe.py" 2>&1
}

echo "=== 阶段 1: 起临时 serve（端口 ${PORT}）==="
OPENCODE_SERVER_PASSWORD="$PW" nohup opencode serve \
    --port "$PORT" --hostname 127.0.0.1 > "$TMP_STATE/serve.log" 2>&1 &
SVPID=$!
disown 2>/dev/null

_auth="$(printf '%s' "opencode:$PW" | base64)"
READY=0
for _ in $(seq 1 30); do
    if curl -s -o /dev/null -w '%{http_code}' --max-time 3 \
            -H "Authorization: Basic $_auth" \
            "http://127.0.0.1:$PORT/session" 2>/dev/null | grep -q '^200$'; then
        READY=1; break
    fi
    sleep 1
done
if [[ "$READY" != "1" ]]; then
    echo "SKIP: 临时 serve 30s 未就绪"
    tail -20 "$TMP_STATE/serve.log"
    exit 0
fi
echo "  serve 就绪"

echo ""
echo "=== 阶段 2: V1 通过路径 ==="
OUT=$(_run_probe "$PORT" "$PW"); RC=$?
echo "  probe 输出: $(echo "$OUT" | tail -1)"
_assert "V1 探针对健康 serve 返回 0" "0" "$RC"
_assert "V1 输出含 OK" "0" "$(echo "$OUT" | grep -q 'probe: OK' && echo 0 || echo 1)"
_assert "V1 探针未留下 .ext-sessions 残留" "1" \
    "$([[ -f "$TMP_STATE/.ext-sessions" ]] && echo 0 || echo 1)"

echo ""
echo "=== 阶段 3: V3 无凭据 → rc=2（区别于「大脑坏了」）==="
# 让凭据发现落空：PROJECT_DIR（运行时状态文件所在目录）指到空目录，找不到 .serve.port。
# src/ 由脚本自身位置推导，不受影响 —— 所以这里得到的是「无凭据」而不是 ImportError。
OUT=$(AGENT_PROBE_PORT="" AGENT_PROBE_PWD="" \
      PROJECT_DIR="$TMP_STATE/empty" \
      python3 "$SCRIPT_DIR/bin/custom/brain_probe.py" 2>&1); RC=$?
echo "  probe 输出: $(echo "$OUT" | tail -1)"
_assert "V3 无凭据返回 2（无法探测，非失败）" "2" "$RC"

echo ""
echo "=== 阶段 4: V2 失败路径（杀掉 serve）==="
kill "$SVPID" 2>/dev/null
pkill -f "opencode serve --port $PORT" 2>/dev/null
sleep 2
SVPID=""
START=$(date +%s)
OUT=$(_run_probe "$PORT" "$PW"); RC=$?
ELAPSED=$(( $(date +%s) - START ))
echo "  probe 输出: $(echo "$OUT" | tail -1)"
_assert "V2 serve 死掉时返回 1（失败）" "1" "$RC"
_assert "V2 在超时上限内返回（未挂死）" "0" \
    "$([[ "$ELAPSED" -le $((PROBE_TIMEOUT + 10)) ]] && echo 0 || echo 1)"

echo ""
if [[ "$FAIL" == "0" ]]; then
    echo "=== 完成：大脑探针 e2e 全部通过 ==="
    exit 0
fi
echo "=== 失败：见上面 ❌ ==="
exit 1
