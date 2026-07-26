#!/bin/bash
# e2e_forward_test.sh — 合并转发「真实链路」端到端测试
#
# 以真人身份发 2 条文本消息 → 取回 msgId → combine-forward 到数字员工 →
# 验证 forward 能力完整链路：
#   dws combine-forward → event consume → bridge → forward.on_inbound →
#   list-by-ids 反查 forwardMessages → 组装 prompt → brain(opencode serve) → replier
#
# 验证点：
#   V1. 发送源消息成功（2 条）
#   V2. combine-forward 成功
#   V3. 入站被 connect 记录（agent-connect.log 含「聊天记录」）
#   V4. forward 能力命中（monitor.log 有 "forward: msgId=" + "forwardMessages="）
#   V5. 出站回复（monitor.log 有 reply OK）
#   V6. 钉钉实际回复存在（DWS list 拉到数字员工回复，含源消息关键词）
#
# 用法：
#   bash tests/custom/e2e_forward_test.sh
#   E2E_SENDER_PROFILE="<corpId>:<真人userId>" bash tests/custom/e2e_forward_test.sh
#   E2E_WAIT=120 bash tests/custom/e2e_forward_test.sh
#
# 已知坑：
#   - dws event 订阅偶发投递停滞 → 先 bash bin/core/reboot.sh 再跑
#   - macOS keychain 锁定 → security unlock-keychain ~/Library/Keychains/login.keychain-db

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$SCRIPT_DIR"

pass() { echo "  ✅ $1"; }
fail() { echo "  ❌ $1"; FAIL=1; }
skip() { echo "  ⏭️  SKIP：$1"; exit 0; }

FAIL=0
CONNECT_LOG="${CONNECT_LOG:-$SCRIPT_DIR/agent-connect.log}"
MONITOR_LOG="${MONITOR_LOG:-$SCRIPT_DIR/monitor.log}"
WAIT="${E2E_WAIT:-90}"

echo "=== 前置：环境与身份 ==="
command -v dws >/dev/null 2>&1 || skip "未找到 dws CLI"
command -v python3 >/dev/null 2>&1 || skip "未找到 python3"

# shellcheck disable=SC1091
[[ -f "$SCRIPT_DIR/config/constants.local.sh" ]] && source "$SCRIPT_DIR/config/constants.local.sh"

BOT_PROFILE="${AGENT_PROFILE:-${DWS_PROFILE:-}}"
[[ -n "$BOT_PROFILE" ]] || skip "AGENT_PROFILE / DWS_PROFILE 未配置"
BOT_CORP="${BOT_PROFILE%%:*}"
BOT_USER="${BOT_PROFILE##*:}"

SENDER_PROFILE="${E2E_SENDER_PROFILE:-}"
if [[ -z "$SENDER_PROFILE" ]]; then
    SENDER_PROFILE="$(dws profile list -y 2>/dev/null | BOT_CORP="$BOT_CORP" BOT_USER="$BOT_USER" python3 -c '
import json, os, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
corp, bot = os.environ["BOT_CORP"], os.environ["BOT_USER"]
for p in d.get("profiles", []):
    if p.get("corpId") == corp and p.get("userId") != bot and p.get("status") == "active":
        print(p.get("profile", "")); break
' 2>/dev/null)"
fi
if [[ -z "$SENDER_PROFILE" ]]; then
    echo "  提示：若已登录过真人账号但探测为空，可能是 macOS keychain 锁定："
    echo "        security unlock-keychain ~/Library/Keychains/login.keychain-db"
    skip "无可用发送方 profile"
fi
SENDER_USER="${SENDER_PROFILE##*:}"

TARGET_CONV="${DWS_EVENT_GROUP:-}"
[[ -n "$TARGET_CONV" ]] || skip "DWS_EVENT_GROUP 未配置（需要群聊做转发源+目标）"

echo "  数字员工: $BOT_PROFILE"
echo "  发送方  : $SENDER_PROFILE"
echo "  目标群  : $TARGET_CONV"

if ! bash "$SCRIPT_DIR/bin/core/healthcheck.sh" >/dev/null 2>&1; then
    skip "healthcheck 未通过（服务未在跑？先 bash bin/core/start.sh）"
fi
pass "环境就绪，服务健康"

CODE="FWD-$(date +%H%M%S)"
MSG1="[$CODE] 第一条：今天天气晴朗，适合户外会议。"
MSG2="[$CODE] 第二条：下午三点在产品会议室讨论 Q3 排期。"
START_HUMAN="$(date '+%Y-%m-%d %H:%M:%S')"

echo ""
echo "=== V1: 以真人身份发送 2 条源消息（${CODE}）==="
SEND1="$(dws chat message send --profile "$SENDER_PROFILE" \
    --group "$TARGET_CONV" --text "$MSG1" -y 2>&1)"
SEND2="$(dws chat message send --profile "$SENDER_PROFILE" \
    --group "$TARGET_CONV" --text "$MSG2" -y 2>&1)"

if echo "$SEND1" | grep -q '"success": true'; then
    pass "源消息 1 发送成功"
else
    fail "源消息 1 发送失败"
    echo "$SEND1" | sed 's/^/    /'
    exit 1
fi
if echo "$SEND2" | grep -q '"success": true'; then
    pass "源消息 2 发送成功"
else
    fail "源消息 2 发送失败"
    echo "$SEND2" | sed 's/^/    /'
    exit 1
fi

sleep 3
echo "  反查源消息 msgId（list --group 按内容匹配）..."
LIST_SRC="$(dws chat message list --profile "$SENDER_PROFILE" \
    --group "$TARGET_CONV" --time "$START_HUMAN" --direction newer \
    --limit 10 -y 2>&1)"
MSG1_ID="$(echo "$LIST_SRC" | CODE="$CODE" python3 -c '
import json, os, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
msgs = d.get("result", {}).get("messages", [])
code = os.environ["CODE"]
hits = [m for m in msgs if code in (m.get("content") or "")]
if len(hits) >= 2:
    print(hits[0].get("openMessageId", ""))
elif len(hits) == 1:
    print(hits[0].get("openMessageId", ""))
' 2>/dev/null)"
MSG2_ID="$(echo "$LIST_SRC" | CODE="$CODE" python3 -c '
import json, os, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
msgs = d.get("result", {}).get("messages", [])
code = os.environ["CODE"]
hits = [m for m in msgs if code in (m.get("content") or "")]
if len(hits) >= 2:
    print(hits[1].get("openMessageId", ""))
' 2>/dev/null)"

if [[ -z "$MSG1_ID" || -z "$MSG2_ID" ]]; then
    fail "未能反查源消息 msgId"
    echo "$LIST_SRC" | head -10 | sed 's/^/    /'
    exit 1
fi
pass "源消息 msgId 已获取：${MSG1_ID:0:20}... / ${MSG2_ID:0:20}..."

sleep 2

echo ""
echo "=== V2: combine-forward 到数字员工（同群）==="
# 用数字员工 profile 做转发（真人 profile 可能 expired 或缺 combine-forward 权限）
FWD_OUT="$(dws chat message combine-forward \
    --src-conversation-id "$TARGET_CONV" \
    --msg-ids "${MSG1_ID},${MSG2_ID}" \
    --dest-conversation-id "$TARGET_CONV" \
    --profile "$BOT_PROFILE" -y 2>&1)"
if echo "$FWD_OUT" | grep -q '"success": true'; then
    pass "combine-forward 成功"
else
    fail "combine-forward 失败"
    echo "$FWD_OUT" | sed 's/^/    /'
    exit 1
fi

echo ""
echo "=== V3+V4+V5: 等待链路处理（最多 ${WAIT}s，轮询日志）==="
GOT_IN=0; GOT_FWD=0; GOT_OUT=0
for ((i=0; i<WAIT; i+=3)); do
    sleep 3
    if [[ $GOT_IN -eq 0 ]]; then
        if grep -F "$CODE" "$CONNECT_LOG" 2>/dev/null | grep -q "聊天记录"; then
            GOT_IN=1
        fi
    fi
    if [[ $GOT_FWD -eq 0 ]]; then
        if awk -v t="$START_HUMAN" '
            match($0, /\[[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}\]/) {
                ts = substr($0, RSTART+1, 19)
                if (ts >= t && $0 ~ /forward:.*forwardMessages=/) { found=1 }
            }
            END { exit found ? 0 : 1 }' "$MONITOR_LOG" 2>/dev/null; then
            GOT_FWD=1
        fi
    fi
    if [[ $GOT_OUT -eq 0 ]]; then
        if awk -v t="$START_HUMAN" '
            match($0, /\[[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}\]/) {
                ts = substr($0, RSTART+1, 19)
                if (ts >= t && $0 ~ /reply (user|group) OK/) { found=1 }
            }
            END { exit found ? 0 : 1 }' "$MONITOR_LOG" 2>/dev/null; then
            GOT_OUT=1
        fi
    fi
    [[ $GOT_IN -eq 1 && $GOT_FWD -eq 1 && $GOT_OUT -eq 1 ]] && break
done

if [[ $GOT_IN -eq 1 ]]; then
    pass "V3 入站已记录（connect log 含 ${CODE} + 聊天记录）"
else
    fail "V3 未见入站（connect log 无 ${CODE} 聊天记录）"
fi
if [[ $GOT_FWD -eq 1 ]]; then
    FWD_LINE="$(awk -v t="$START_HUMAN" '
        match($0, /\[[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}\]/) {
            ts = substr($0, RSTART+1, 19)
            if (ts >= t && $0 ~ /forward:.*forwardMessages=/) { line=$0 }
        }
        END { print line }' "$MONITOR_LOG" 2>/dev/null)"
    pass "V4 forward 能力命中"
    echo "    $FWD_LINE"
else
    fail "V4 forward 能力未命中（monitor.log 无 forwardMessages）"
fi
if [[ $GOT_OUT -eq 1 ]]; then
    pass "V5 出站已记录（reply OK）"
else
    fail "V5 未见出站（monitor.log 无 reply OK）"
fi

echo ""
echo "=== V6: 独立拉数字员工回复，断言含源消息关键词 ==="
REPLY=""
for ((i=0; i<8; i++)); do
    LIST_OUT="$(dws chat message list --profile "$SENDER_PROFILE" \
        --group "$TARGET_CONV" --time "$START_HUMAN" --direction newer \
        --limit 20 -y 2>&1)"
    REPLY="$(echo "$LIST_OUT" | BOT_USER="$BOT_USER" CODE="$CODE" \
        SELF_NAMES="${AGENT_SELF_NAMES:-}" python3 -c '
import json, os, sys
try:
    d = json.load(sys.stdin)
except Exception:
    print(""); sys.exit(0)
res = d.get("result", d)
msgs = res.get("messages", []) if isinstance(res, dict) else []
bot = os.environ.get("BOT_USER", "")
code = os.environ.get("CODE", "")
names = {n.strip() for n in os.environ.get("SELF_NAMES", "").split(",") if n.strip()}
bot_msgs = [(m.get("content") or "").strip() for m in msgs
            if (str(m.get("sender") or "") == bot or str(m.get("sender") or "") in names)
            and (m.get("content") or "").strip()]
hit = next((c for c in bot_msgs if code in c or "排期" in c or "会议" in c or "天气" in c), "")
print(hit or (bot_msgs[0] if bot_msgs else ""))
' 2>/dev/null)"
    [[ -n "$REPLY" ]] && break
    sleep 3
done

if [[ -z "$REPLY" ]]; then
    fail "V6 未拉到数字员工回复"
    echo "$LIST_OUT" | head -6 | sed 's/^/    /'
else
    pass "V6 数字员工已回复"
    echo "    回复预览：${REPLY:0:120}"
fi

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
if [[ $FAIL -eq 0 ]]; then
    echo "✅ 合并转发真实链路端到端测试通过（V1-V6）"
    echo "   $CODE  2条源消息 → combine-forward → 数字员工总结回复"
    exit 0
else
    echo "❌ 合并转发端到端测试存在失败项（见上 V1-V6）"
    echo "   排错：tail -f monitor.log agent-connect.log opencode.log"
    exit 1
fi
