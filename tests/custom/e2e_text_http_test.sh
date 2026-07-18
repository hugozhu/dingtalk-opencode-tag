#!/bin/bash
# e2e_text_http_test.sh — 基础文本回复 e2e（走 opencode serve HTTP 路径）
#
# 最底层冒烟：不碰合并转发/钉钉，只验 brain(opencode) 经 serve HTTP 生成回复，
# 并确认 AGENT_DEBUG=1 时 opencode.log 记了一条 transport=http。
#
# 与真实链路的关系：这条把 brain 从「链路」里单独拎出来直接调，用一个**临时 serve**
# （独立端口，跑完即杀），不依赖已托管的 serve / dws。CI / 本机都能跑。
#
# 验证点：
#   V1. brain.generate_reply("u","1+1") 走 HTTP 返回非空（模型正常时是 "2"）
#   V2. opencode.log 有一条 transport=http ok=True 记录
#   V3. HTTP 比 CLI 快（软断言，仅打印耗时对照，不作硬失败）

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$SCRIPT_DIR"

OPENCODE_BIN="${AGENT_OPENCODE_BIN:-opencode}"
MODEL="${AGENT_OPENCODE_MODEL:-opencode/deepseek-v4-flash-free}"
PORT="${E2E_SERVE_PORT:-47790}"
PW="e2e$(openssl rand -hex 6)"
TMP_LOG="$SCRIPT_DIR/opencode.e2e.log"
PORT_FILE="$SCRIPT_DIR/.serve.port.e2e"
PWD_FILE="$SCRIPT_DIR/.serve.pwd.e2e"

if ! command -v "$OPENCODE_BIN" >/dev/null 2>&1; then
    echo "SKIP: 未找到 opencode（$OPENCODE_BIN），跳过 HTTP e2e"
    exit 0
fi

cleanup() {
    [[ -n "${SVPID:-}" ]] && kill "$SVPID" 2>/dev/null
    pkill -f "opencode serve --port $PORT" 2>/dev/null
    rm -f "$PORT_FILE" "$PWD_FILE" "$TMP_LOG"
}
trap cleanup EXIT

echo "=== 阶段 1: 起临时 serve（端口 ${PORT}）==="
echo "$PORT" > "$PORT_FILE"
echo "$PW"   > "$PWD_FILE"
OPENCODE_SERVER_PASSWORD="$PW" nohup "$OPENCODE_BIN" serve \
    --port "$PORT" --hostname 127.0.0.1 >/tmp/e2e_serve.log 2>&1 &
SVPID=$!
disown "$SVPID" 2>/dev/null || true
sleep 5

echo ""
echo "=== 阶段 2: 驱动 brain（HTTP 路径）+ 采集耗时 ==="
PROJECT_DIR="$SCRIPT_DIR" \
AGENT_BRAIN=opencode \
AGENT_OPENCODE_MODEL="$MODEL" \
AGENT_SYSTEM_PROMPT="只输出算式结果的数字，不要任何解释或多余文字。" \
AGENT_DEBUG=1 \
AGENT_OPENCODE_LOG="$TMP_LOG" \
python3 - "$PORT_FILE" "$PWD_FILE" <<'PY'
import sys, os, time
sys.path.insert(0, os.path.join(os.environ["PROJECT_DIR"], "src"))
# 让 find_serve_credentials 读我们的 .e2e 状态文件
import core.agent_common as ac
_orig = ac._read_state_file
_map = {".serve.port": os.path.basename(sys.argv[1]),
        ".serve.pwd": os.path.basename(sys.argv[2]),
        ".serve.pid": ".serve.pid.e2e"}
ac._read_state_file = lambda b: _orig(_map.get(b, b))
ac.invalidate_serve_credentials()
import custom.brain as brain
t0 = time.time()
reply = brain.generate_reply("hugozhu", "1+1")
dt = time.time() - t0
print(f"  HTTP reply={reply!r} elapsed={dt:.2f}s")
# V1
if not reply:
    print("  ❌ V1 失败：HTTP 路径返回空"); sys.exit(1)
print("  ✅ V1：HTTP 路径返回非空")
PY
RC=$?
[[ $RC -ne 0 ]] && { echo "brain 调用失败 rc=$RC"; exit 1; }

echo ""
echo "=== 阶段 3: 校验 opencode.log ==="
# V2
if grep -q "transport=http.*ok=True" "$TMP_LOG" 2>/dev/null; then
    echo "  ✅ V2：opencode.log 有 transport=http ok=True"
    grep "transport=http" "$TMP_LOG" | tail -1 | sed 's/^/    /'
else
    echo "  ❌ V2 失败：opencode.log 无 transport=http 记录"
    cat "$TMP_LOG" 2>/dev/null | sed 's/^/    /'
    exit 1
fi

echo ""
echo "=== 完成：基础文本 HTTP e2e 通过 ==="
