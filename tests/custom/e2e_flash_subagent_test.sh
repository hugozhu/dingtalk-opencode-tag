#!/bin/bash
# e2e_flash_subagent_test.sh — flash-worker 子代理委派 e2e（#123，本地冒烟）
#
# 验证「skill 委派路线」整条闭环：brain 主模型（默认模型，留在复用 session）收到
# 要求委派的指令 → 用 Task 工具调 opencode.json 定义的 flash-worker 子代理 → 子代理
# 跑在独立 child session（model=AGENT_OPENCODE_MODEL_FLASH）→ brain 回合结束后把
# 子代理用量记进该 conv 的 flash_* 统计，主计数器只含主轮自身。
#
# 用**临时 serve**（独立端口，跑完即杀），不依赖已托管 serve / dws；serve 进程带上
# AGENT_OPENCODE_MODEL_FLASH（生产里来自 monitor 环境），opencode.json 的
# {env:AGENT_OPENCODE_MODEL_FLASH} 才能解析出 flash-worker 的模型。
#
# 验证点：
#   V1. brain.generate_reply 走 HTTP 返回非空（主模型拿到子代理结果并转述）
#   V2. 主 session 下确有 child session，其中 flash 模型的 assistant 消息有 token 用量
#   V3. flash_* 统计已记账（flash_rounds ≥ 1 且 flash_input_tokens > 0），
#       主计数器 rounds == 1（委派不推进主会话轮次、不污染主会话窗口）
#
# SKIP 条件：无 opencode / 未配置 AGENT_OPENCODE_MODEL_FLASH / FLASH 与主模型同值。

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$SCRIPT_DIR"

# 模型兜底（#71）：未显式设模型时先 source 本地配置取真实可用模型
if [[ -z "${AGENT_OPENCODE_MODEL:-}" && -f "$SCRIPT_DIR/config/constants.local.sh" ]]; then
    # shellcheck disable=SC1091
    source "$SCRIPT_DIR/config/constants.local.sh"
fi

OPENCODE_BIN="${AGENT_OPENCODE_BIN:-opencode}"
MODEL="${AGENT_OPENCODE_MODEL:-}"
FLASH="${AGENT_OPENCODE_MODEL_FLASH:-}"
PORT="${E2E_FLASH_SERVE_PORT:-47792}"
PW="e2e$(openssl rand -hex 6)"
TMP_LOG="$SCRIPT_DIR/opencode.e2e.flash.log"
PORT_FILE="$SCRIPT_DIR/.serve.port.e2e"
PWD_FILE="$SCRIPT_DIR/.serve.pwd.e2e"
PID_FILE="$SCRIPT_DIR/.serve.pid.e2e"
CONV="e2e-flash-sub"

if ! command -v "$OPENCODE_BIN" >/dev/null 2>&1; then
    echo "SKIP: 未找到 opencode（$OPENCODE_BIN），跳过 flash-worker 委派 e2e"
    exit 0
fi
if [[ -z "$FLASH" ]]; then
    echo "SKIP: 未配置 AGENT_OPENCODE_MODEL_FLASH（特性关闭），跳过"
    exit 0
fi
if [[ -n "$MODEL" && "$MODEL" == "$FLASH" ]]; then
    echo "SKIP: FLASH 与主模型同值（没换缓存桶，无委派意义），跳过"
    exit 0
fi

cleanup() {
    [[ -n "${SVPID:-}" ]] && kill "$SVPID" 2>/dev/null
    pkill -f "opencode serve --port $PORT" 2>/dev/null
    rm -f "$PORT_FILE" "$PWD_FILE" "$PID_FILE" "$TMP_LOG"
}
trap cleanup EXIT

echo "=== 阶段 1: 起临时 serve（端口 ${PORT}，主模型 ${MODEL:-默认}，flash ${FLASH}）==="
echo "$PORT" > "$PORT_FILE"
echo "$PW"   > "$PWD_FILE"
AGENT_OPENCODE_MODEL_FLASH="$FLASH" OPENCODE_SERVER_PASSWORD="$PW" nohup "$OPENCODE_BIN" serve \
    --port "$PORT" --hostname 127.0.0.1 >/tmp/e2e_flash_serve.log 2>&1 &
SVPID=$!
echo "$SVPID" > "$PID_FILE"
disown "$SVPID" 2>/dev/null || true

# 就绪探测（#71）：轮询 /session 到 HTTP 200（最多 30s）
_auth="$(printf '%s' "opencode:$PW" | base64)"
READY=0
for ((i = 0; i < 30; i++)); do
    sleep 1
    if curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Basic $_auth" \
            "http://127.0.0.1:$PORT/session" 2>/dev/null | grep -q '^200$'; then
        READY=1; break
    fi
done
if [[ "$READY" -ne 1 ]]; then
    echo "❌ 临时 serve 30s 内未就绪（/tmp/e2e_flash_serve.log 尾部如下）"
    tail -20 /tmp/e2e_flash_serve.log 2>/dev/null | sed 's/^/    /'
    exit 1
fi
echo "  serve 就绪（$((i + 1))s）"

# 先确认 flash-worker 在临时 serve 上注册成功（model 解析自 env）
AGENTS_JSON="$(curl -s -H "Authorization: Basic $_auth" "http://127.0.0.1:$PORT/agent")"
if ! python3 -c "
import json, sys
agents = json.loads(sys.argv[1])
fw = [a for a in agents if a.get('name') == 'flash-worker']
assert fw and fw[0].get('model'), 'flash-worker 未注册或无模型'
print('  flash-worker:', fw[0]['model']['providerID'] + '/' + fw[0]['model']['modelID'])
" "$AGENTS_JSON"; then
    echo "❌ flash-worker 子代理未注册（检查 opencode.json / serve env）"
    exit 1
fi

echo ""
echo "=== 阶段 2: 驱动 brain（主模型委派 flash-worker）==="
PROMPT='请用 Task 工具把这个子任务委派给 flash-worker 子代理执行：让它只回复字符串 DELEGATE-OK，不做别的。拿到子代理结果后，你的回复只包含子代理返回的原文，不要任何额外文字。'
PROJECT_DIR="$SCRIPT_DIR" \
AGENT_BRAIN=opencode \
AGENT_SYSTEM_PROMPT="你是任务协调者，严格按用户指令执行。" \
AGENT_DEBUG=1 \
AGENT_OPENCODE_LOG="$TMP_LOG" \
AGENT_OPENCODE_MODEL="$MODEL" \
AGENT_OPENCODE_MODEL_FLASH="$FLASH" \
AGENT_OPENCODE_IDLE_TIMEOUT=240 \
AGENT_OPENCODE_MAX_TIMEOUT=300 \
python3 - "$PORT_FILE" "$PWD_FILE" "$CONV" "$PORT" "$PW" "$PROMPT" <<'PY'
import base64, json, os, sys, time, urllib.request

sys.path.insert(0, os.path.join(os.environ["PROJECT_DIR"], "src"))
# 让 find_serve_credentials 读我们的 .e2e 状态文件（同 e2e_text_http_test.sh）
import core.agent_common as ac
_orig = ac._read_state_file
_map = {".serve.port": os.path.basename(sys.argv[1]),
        ".serve.pwd": os.path.basename(sys.argv[2]),
        ".serve.pid": ".serve.pid.e2e"}
ac._read_state_file = lambda b: _orig(_map.get(b, b))
ac.invalidate_serve_credentials()
import custom.brain as brain

CONV, PORT, PW, PROMPT = sys.argv[3], sys.argv[4], sys.argv[5], sys.argv[6]
ctx = {"conv_id": CONV, "conv_type": "2", "msg_id": "m", "user": "e2e"}
t0 = time.time()
reply, status = brain.generate_reply_ex("e2e", PROMPT, ctx=ctx, raw=True)
dt = time.time() - t0
print(f"  reply={reply!r} status={status} elapsed={dt:.1f}s")
# V1
if not reply:
    print("  ❌ V1 失败：HTTP 路径返回空"); sys.exit(1)
print("  ✅ V1：主模型经委派返回非空")

# V2: 主 session 下有 child，且 child 里 flash 模型 assistant 消息有用量
sid = brain._lookup_sid(CONV)
assert sid, "复用 session 未登记"

def _get(path):
    req = urllib.request.Request(
        f"http://127.0.0.1:{PORT}{path}",
        headers={"Authorization": "Basic " + base64.b64encode(
            f"opencode:{PW}".encode()).decode()})
    return json.load(urllib.request.urlopen(req, timeout=10))

flash_id = os.environ.get("AGENT_OPENCODE_MODEL_FLASH", "").split("/")[-1]
children = _get(f"/session/{sid}/children")
flash_msgs = 0
flash_input = 0
for ch in children:
    for m in _get(f"/session/{ch['id']}/message"):
        info = m.get("info", {})
        if info.get("role") == "assistant" and info.get("modelID") == flash_id:
            flash_msgs += 1
            flash_input += (info.get("tokens", {}) or {}).get("input") or 0
print(f"  children={len(children)} flash_msgs={flash_msgs} flash_input={flash_input}")
if flash_msgs == 0 or flash_input == 0:
    print("  ❌ V2 失败：child session 无 flash 模型用量（委派未发生或统计口径不符）")
    sys.exit(1)
print("  ✅ V2：child session 内 flash 模型 assistant 消息有 token 用量")

# V3: flash_* 已记账，主计数器不被委派推进
stats = brain._get_session_stats(CONV)
print(f"  stats: rounds={stats['rounds']} input={stats['input_tokens']} "
      f"flash_rounds={stats['flash_rounds']} flash_input={stats['flash_input_tokens']}")
if stats["flash_rounds"] < 1 or stats["flash_input_tokens"] <= 0:
    print("  ❌ V3 失败：flash_* 统计未记账"); sys.exit(1)
if stats["rounds"] != 1:
    print(f"  ❌ V3 失败：主会话轮次应为 1，实际 {stats['rounds']}"); sys.exit(1)
print("  ✅ V3：flash_* 已记账；主会话 rounds=1，委派不进主会话窗口")
PY
RC=$?
[[ $RC -ne 0 ]] && { echo "brain 调用失败 rc=$RC（$TMP_LOG 尾部如下）"; tail -5 "$TMP_LOG" 2>/dev/null | sed 's/^/    /'; exit 1; }

echo ""
echo "=== 完成：flash-worker 委派 e2e 通过 ==="
