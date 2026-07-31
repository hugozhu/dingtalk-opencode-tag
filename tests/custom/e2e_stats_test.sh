#!/bin/bash
# e2e_stats_test.sh — /stats 会话统计「真实链路」端到端测试
#
# 验证 stats 能力（src/custom/capabilities/stats.py）的完整闭环：
#   预热消息 → brain(opencode serve HTTP reuse) 累积 _conv_sessions 统计
#   → 发纯 /stats → stats 能力精确匹配 → 回复真实统计摘要（Session ID/轮数/Tokens）
#
# 关键约束（排查结论，务必先读）：
#   统计数据存在 brain.py 的进程内存表 _conv_sessions 里，只有走 _http_reuse 路径
#   （AGENT_SESSION_REUSE=1 + 有 conv_id）成功回复后才由 _update_stats 累加。
#   故本测试**必须先发一轮正常消息预热**，紧接着发 /stats 才有内容；否则 stats
#   能力会回 "当前没有活跃的会话统计信息"（这是预期行为，不是 bug）。
#   进程重启（/reboot）会清零内存表 —— 预热步骤同时抵消了重启的影响。
#
# 验证点：
#   V1. 预热发送成功：真人身份 dws send 返回 success
#   V2. 预热入站被记录：agent-connect.log 有 "[connect] 收到 @<真人>: [<CODE>] …"
#   V3. 预热回复正确：list --group <convId> 拉到数字员工回复含正确答案（42）
#       —— 证明 brain 走通 HTTP reuse，该 conv 已累积统计（rounds>=1）
#   V4. /stats 发送成功 + 入站被记录：connect log 出现 ": /stats (convType" 行
#   V5. /stats 回复是真实统计：list --group（时间窗 >= 发 /stats 时刻）拉到数字员工
#       回复含 "Session ID"，且**不含** "没有活跃"（区分真实摘要 vs 空统计兜底文案）
#
# 设计：沿用 e2e_text_reply_test.sh 的范式 —— 参数化身份不写死、SKIP 友好、
#   list --group <convId> 独立校验（o2o/群都可靠；list-by-sender 不索引 o2o 回复）。
#
# 用法：
#   bash tests/custom/e2e_stats_test.sh                    # 私聊闭环（默认）
#   E2E_SENDER_PROFILE="<corpId>:<真人userId>" bash ...     # 显式指定发送方
#   E2E_TARGET=group bash ...                              # 改走群聊（发到 DWS_EVENT_GROUP）
#   E2E_WAIT=90 bash ...                                   # 单阶段等回复超时秒数（默认 60）
#
# 环境坑：V2 超时未见入站多半是订阅投递停滞（AGENTS.md 坑#3），先 bin/core/reboot.sh
#   重建订阅、warmup ~20s 再跑。V3 过而 V5 挂 → 检查 AGENT_SESSION_REUSE 是否为 1。

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$SCRIPT_DIR"

pass() { echo "  ✅ $1"; }
fail() { echo "  ❌ $1"; FAIL=1; }
skip() { echo "  ⏭️  SKIP：$1"; exit 0; }

FAIL=0
CONNECT_LOG="${CONNECT_LOG:-$SCRIPT_DIR/agent-connect.log}"
MONITOR_LOG="${MONITOR_LOG:-$SCRIPT_DIR/monitor.log}"
WAIT="${E2E_WAIT:-60}"
TARGET="${E2E_TARGET:-o2o}"          # o2o | group

# 从 list --group 的 JSON（stdin）里挑数字员工回复：含 MARKER 的那条，否则第一条。
# env: BOT_USER（userId）、SELF_NAMES（AGENT_SELF_NAMES）、MARKER
extract_bot_reply() {
    BOT_USER="$BOT_USER" SELF_NAMES="${AGENT_SELF_NAMES:-}" MARKER="$1" python3 -c '
import json, os, sys
try:
    d = json.load(sys.stdin)
except Exception:
    print(""); sys.exit(0)
res = d.get("result", d)
msgs = res.get("messages", []) if isinstance(res, dict) else []
bot = os.environ.get("BOT_USER", "")
names = {n.strip() for n in os.environ.get("SELF_NAMES", "").split(",") if n.strip()}
marker = os.environ.get("MARKER", "")
bot_msgs = [(m.get("content") or "").strip() for m in msgs
            if (str(m.get("sender") or "") == bot or str(m.get("sender") or "") in names)
            and (m.get("content") or "").strip()]
hit = next((c for c in bot_msgs if marker and marker in c), "")
print(hit or (bot_msgs[0] if bot_msgs else ""))
' 2>/dev/null
}

echo "=== 前置：环境与身份 ==="
command -v dws >/dev/null 2>&1 || skip "未找到 dws CLI"
command -v python3 >/dev/null 2>&1 || skip "未找到 python3"

# shellcheck disable=SC1091
[[ -f "$SCRIPT_DIR/config/constants.local.sh" ]] && source "$SCRIPT_DIR/config/constants.local.sh"

# /stats 统计依赖会话复用（内存态 _conv_sessions 仅 _http_reuse 路径累加）。
# AGENT_SESSION_REUSE=0/空 = 无状态语义，统计永远空 → 本测试无意义，SKIP。
REUSE="${AGENT_SESSION_REUSE:-1}"
if [[ "$REUSE" == "0" || -z "$REUSE" ]]; then
    skip "AGENT_SESSION_REUSE=$REUSE（无状态），/stats 统计不会累积；设 AGENT_SESSION_REUSE=1 再跑"
fi

BOT_PROFILE="${AGENT_PROFILE:-${DWS_PROFILE:-}}"
[[ -n "$BOT_PROFILE" ]] || skip "AGENT_PROFILE / DWS_PROFILE 未配置（config/constants.local.sh）"
BOT_CORP="${BOT_PROFILE%%:*}"
BOT_USER="${BOT_PROFILE##*:}"

# 发送方（真人）：显式 > 自动探测（同 corp、active、userId != 数字员工）
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
    echo "  提示：若已登录过真人账号但探测为空，可能是 macOS keychain 锁定，先解锁再试："
    echo "        security unlock-keychain ~/Library/Keychains/login.keychain-db"
    echo "        或显式指定发送方：E2E_SENDER_PROFILE=\"<corpId>:<真人userId>\" bash $0"
    skip "无可用发送方 profile（设 E2E_SENDER_PROFILE 或 dws login 一个真人账号）"
fi

# 目标：私聊发给数字员工 userId；群聊发到 DWS_EVENT_GROUP
if [[ "$TARGET" == "group" ]]; then
    [[ -n "${DWS_EVENT_GROUP:-}" ]] || skip "E2E_TARGET=group 但 DWS_EVENT_GROUP 未配置"
    TARGET_DESC="group=$DWS_EVENT_GROUP"
    SEND_TO=(--group "$DWS_EVENT_GROUP")
else
    TARGET_DESC="o2o user=${BOT_USER}（数字员工私聊）"
    SEND_TO=(--user "$BOT_USER")
fi

echo "  数字员工: $BOT_PROFILE"
echo "  发送方  : $SENDER_PROFILE"
echo "  目标    : $TARGET_DESC"
echo "  会话复用: AGENT_SESSION_REUSE=$REUSE"

if ! bash "$SCRIPT_DIR/bin/core/healthcheck.sh" >/dev/null 2>&1; then
    skip "healthcheck 未通过（服务未在跑？先 bash bin/core/start.sh）"
fi
pass "环境就绪，服务健康"

# ---------------------------------------------------------------------------
# 阶段一：预热 —— 发一条算式，等数字员工正确回复，让该 conv 累积会话统计
# ---------------------------------------------------------------------------
CODE="STATS-$(date +%H%M%S)"
QUESTION="[$CODE] 请只回复一个数字，不要任何其他文字：37 加 5 等于多少？"
ANSWER="42"
WARMUP_START="$(date '+%Y-%m-%d %H:%M:%S')"

echo ""
echo "=== V1: 预热——以真人身份发送算式（${CODE}）==="
SEND_OUT="$(dws chat message send --profile "$SENDER_PROFILE" "${SEND_TO[@]}" \
    --text "$QUESTION" -y 2>&1)"
if echo "$SEND_OUT" | grep -q '"success": true'; then
    pass "预热发送成功"
else
    fail "预热发送失败"
    echo "$SEND_OUT" | sed 's/^/    /'
    echo "（预热失败，统计无从累积，后续无意义）"; exit 1
fi

echo ""
echo "=== V2+V3: 等预热回复（最多 ${WAIT}s，轮询入站 + 拉取校验）==="
GOT_IN=0; IN_LINE=""; WARMUP_REPLY=""
CONV_ID=""
for ((i=0; i<WAIT; i+=3)); do
    sleep 3
    if [[ $GOT_IN -eq 0 ]]; then
        IN_LINE="$(grep -F "$CODE" "$CONNECT_LOG" 2>/dev/null | tail -1)"
        [[ -n "$IN_LINE" ]] && GOT_IN=1
    fi
    # 入站行里取 convId（o2o/群都用 list --group <convId> 校验）
    if [[ -z "$CONV_ID" && -n "$IN_LINE" ]]; then
        CONV_ID="$(echo "$IN_LINE" | sed -nE 's/.*convId=([^ ]+).*/\1/p')"
    fi
    if [[ -n "$CONV_ID" ]]; then
        LIST_OUT="$(dws chat message list --profile "$SENDER_PROFILE" \
            --group "$CONV_ID" --time "$WARMUP_START" --direction newer \
            --limit 10 -y 2>&1)"
        WARMUP_REPLY="$(echo "$LIST_OUT" | extract_bot_reply "$ANSWER")"
        [[ -n "$WARMUP_REPLY" && "$WARMUP_REPLY" == *"$ANSWER"* ]] && break
    fi
done

if [[ $GOT_IN -eq 1 ]]; then
    pass "V2 预热入站已记录（agent-connect.log 含 ${CODE}）"
    echo "$IN_LINE" | sed 's/^/    /'
else
    fail "V2 未见预热入站（agent-connect.log 无 ${CODE}）"
fi
if [[ -n "$WARMUP_REPLY" && "$WARMUP_REPLY" == *"$ANSWER"* ]]; then
    pass "V3 预热回复正确（含 \"$ANSWER\"）→ 该 conv 已累积会话统计"
    echo "    预热回复：$WARMUP_REPLY"
else
    fail "V3 预热回复未拉到/不正确（convId=${CONV_ID:-?}）—— brain/serve 链路或会话复用异常"
    echo "    （V3 不过则统计未累积，/stats 必为空，提前终止）"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "❌ /stats 端到端测试失败于预热阶段（V1-V3）"
    echo "   排错：tail -f monitor.log agent-connect.log opencode.log"
    exit 1
fi

# ---------------------------------------------------------------------------
# 阶段二：发纯 /stats，断言回复是真实统计摘要
# ---------------------------------------------------------------------------
STATS_START="$(date '+%Y-%m-%d %H:%M:%S')"

echo ""
echo "=== V4: 发送纯 /stats 指令 ==="
STATS_OUT="$(dws chat message send --profile "$SENDER_PROFILE" "${SEND_TO[@]}" \
    --text "/stats" -y 2>&1)"
if echo "$STATS_OUT" | grep -q '"success": true'; then
    pass "/stats 发送成功"
else
    fail "/stats 发送失败"
    echo "$STATS_OUT" | sed 's/^/    /'; exit 1
fi

# 入站精确匹配 ": /stats (convType"（connect 日志格式 "收到 @<名>: /stats (convType=…"）
# connect 日志无时间戳前缀，取发送后新增的匹配行计数对比，避免命中历史 /stats。
GOT_STATS_IN=0
for ((i=0; i<30; i+=3)); do
    sleep 3
    if grep -E ": /stats \(convType" "$CONNECT_LOG" 2>/dev/null | tail -1 | grep -q "/stats"; then
        GOT_STATS_IN=1; break
    fi
done
if [[ $GOT_STATS_IN -eq 1 ]]; then
    pass "V4 /stats 入站已记录（connect log 有 \": /stats (convType\"）"
else
    fail "V4 未见 /stats 入站记录"
fi

echo ""
echo "=== V5: 拉数字员工对 /stats 的回复，断言真实统计摘要 ==="
# 时间窗 >= 发 /stats 时刻 → 排除预热回复（42），只取本次 /stats 的响应。
# list API 最终一致，轮询几轮。stats 能力即时回复（不走 LLM），通常很快。
STATS_REPLY=""
for ((i=0; i<10; i++)); do
    LIST_OUT="$(dws chat message list --profile "$SENDER_PROFILE" \
        --group "$CONV_ID" --time "$STATS_START" --direction newer \
        --limit 10 -y 2>&1)"
    STATS_REPLY="$(echo "$LIST_OUT" | extract_bot_reply "Session ID")"
    [[ -n "$STATS_REPLY" && "$STATS_REPLY" == *"Session ID"* ]] && break
    sleep 3
done

if [[ -z "$STATS_REPLY" ]]; then
    fail "V5 未拉到数字员工对 /stats 的回复（convId=${CONV_ID:-?}）"
    echo "$LIST_OUT" | head -6 | sed 's/^/    /'
elif echo "$STATS_REPLY" | grep -q "没有活跃"; then
    fail "V5 回复是空统计兜底文案（\"没有活跃的会话统计信息\"）—— 预热未累积/会话复用未生效"
    echo "    实际回复：$STATS_REPLY"
elif echo "$STATS_REPLY" | grep -q "Session ID"; then
    pass "V5 回复是真实统计摘要（含 Session ID）"
    echo "    ┌──── /stats 回复 ────"
    echo "$STATS_REPLY" | sed 's/^/    │ /'
    echo "    └────────────────────"
else
    fail "V5 回复既非统计摘要也非空统计文案，内容异常"
    echo "    实际回复：$STATS_REPLY"
fi

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
if [[ $FAIL -eq 0 ]]; then
    echo "✅ /stats 会话统计真实链路端到端测试通过（V1-V5）"
    echo "   预热 \"$QUESTION\" → \"$WARMUP_REPLY\"；/stats → 真实统计摘要"
    exit 0
else
    echo "❌ /stats 端到端测试存在失败项（见上 V1-V5）"
    echo "   排错：tail -f monitor.log agent-connect.log opencode.log"
    echo "   常见：V5 空统计 → AGENT_SESSION_REUSE 是否为 1 / 预热是否真走了 HTTP reuse"
    exit 1
fi
