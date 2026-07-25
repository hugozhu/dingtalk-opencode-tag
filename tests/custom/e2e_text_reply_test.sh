#!/bin/bash
# e2e_text_reply_test.sh — 基础文本回复「真实链路」端到端测试（三 agent 通用）
#
# 这条是 AGENTS.md「测试约定 → 基础文本 e2e」范式的可执行落地：
# 以**真人身份**私聊/群聊发一条带唯一校验码的算式，验证数字员工经完整链路
#   dws consume → bridge → event_watcher → brain(opencode serve) → replier(dws send)
# 收到并回复了**正确答案**。opencode / Claude Code / Codex 都可直接：
#   bash tests/custom/e2e_text_reply_test.sh
#
# 与其它 e2e 的分工：
#   - e2e_text_http_test.sh：只把 brain 单拎出来直调 serve HTTP（不碰钉钉，CI 冒烟）
#   - e2e_at_test.sh       ：验 @我 订阅链路，末段 LIVE 只读建联
#   - 本脚本               ：**打通到钉钉的真实收发闭环**，需已托管的服务在跑
#
# 验证点：
#   V1. 发送成功：以真人身份 dws chat message send 返回 success
#   V2. 入站被 connect 记录：agent-connect.log 有 "[connect] 收到 @<真人>: [<CODE>] …"
#   V3. 出站被 agent 记录：monitor.log 在发送后出现 "reply user OK"（或 reply group OK）
#   V4. 钉钉实际回复正确：从入站行取本会话 convId，独立拉数字员工发来的消息，
#       断言含正确答案 —— 用 list --group <convId>（对 o2o/群都可靠；list-by-sender
#       实测不索引 o2o 回复，故不用它）
#
# 设计：
#   - 参数化身份，不写死任何 userId/convId —— fork 出去也能跑
#     * 数字员工身份取 AGENT_PROFILE（config/constants.local.sh）
#     * 发送方（真人）取 E2E_SENDER_PROFILE；未设则自动探测：同 corp、active、
#       且 userId != 数字员工 的那个 profile（dws profile list）
#   - 唯一校验码 + 确定答案（37+5=42）：防串话 / 防命中历史缓存
#   - SKIP 友好：无 dws / 未登录 / 无发送方 profile / 服务未跑 → SKIP（exit 0），不算失败
#
# 用法：
#   bash tests/custom/e2e_text_reply_test.sh                 # 私聊闭环（默认）
#   E2E_SENDER_PROFILE="<corpId>:<真人userId>" bash ...       # 显式指定发送方
#   E2E_TARGET=group bash ...                                # 改走群聊（发到 DWS_EVENT_GROUP）
#   E2E_WAIT=90 bash ...                                     # 等回复的超时秒数（默认 60）
#
# 已知环境坑：某些环境下 dws event 订阅偶发「投递停滞」——子进程还在但连接静默失活，
# 消息迟迟不进 connect log。若 V2 超时未见入站，先 bash bin/core/reboot.sh 重建订阅再跑。

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

echo "=== 前置：环境与身份 ==="
command -v dws >/dev/null 2>&1 || skip "未找到 dws CLI"
command -v python3 >/dev/null 2>&1 || skip "未找到 python3"

# shellcheck disable=SC1091
[[ -f "$SCRIPT_DIR/config/constants.local.sh" ]] && source "$SCRIPT_DIR/config/constants.local.sh"

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
# 自动探测为空的常见原因（#71）：macOS keychain 锁定时 dws profile list 直接返回空。
# 给出可操作的提示再 SKIP，而不是让人误以为没登录。
if [[ -z "$SENDER_PROFILE" ]]; then
    echo "  提示：若已登录过真人账号但探测为空，可能是 macOS keychain 锁定，先解锁再试："
    echo "        security unlock-keychain ~/Library/Keychains/login.keychain-db"
    echo "        或显式指定发送方：E2E_SENDER_PROFILE=\"<corpId>:<真人userId>\" bash $0"
    skip "无可用发送方 profile（设 E2E_SENDER_PROFILE 或 dws login 一个真人账号）"
fi
SENDER_USER="${SENDER_PROFILE##*:}"

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

# 服务是否在跑（软判定：不在跑就 SKIP，避免误判为失败）
if ! bash "$SCRIPT_DIR/bin/core/healthcheck.sh" >/dev/null 2>&1; then
    skip "healthcheck 未通过（服务未在跑？先 bash bin/core/start.sh）"
fi
pass "环境就绪，服务健康"

# 唯一校验码（不用 date +%s 命令替换到脚本层也行，这里就地取秒级时间戳）
CODE="E2E-$(date +%H%M%S)"
QUESTION="[$CODE] 请只回复一个数字，不要任何其他文字：37 加 5 等于多少？"
ANSWER="42"
START_HUMAN="$(date '+%Y-%m-%d %H:%M:%S')"

echo ""
echo "=== V1: 以真人身份发送（${CODE}）==="
SEND_OUT="$(dws chat message send --profile "$SENDER_PROFILE" "${SEND_TO[@]}" \
    --text "$QUESTION" -y 2>&1)"
if echo "$SEND_OUT" | grep -q '"success": true'; then
    pass "发送成功"
else
    fail "发送失败"
    echo "$SEND_OUT" | sed 's/^/    /'
    echo "（发送失败，后续校验无意义）"; exit 1
fi

echo ""
echo "=== V2+V3: 等待链路处理（最多 ${WAIT}s，轮询日志）==="
# do-while 风格：先睡一小段再判，保证服务有时间落日志
GOT_IN=0; GOT_OUT=0; IN_LINE=""
for ((i=0; i<WAIT; i+=3)); do
    sleep 3
    if [[ $GOT_IN -eq 0 ]]; then
        IN_LINE="$(grep -F "$CODE" "$CONNECT_LOG" 2>/dev/null | tail -1)"
        [[ -n "$IN_LINE" ]] && GOT_IN=1
    fi
    # 出站：monitor.log 里时间戳 >= 发送时刻 的 "reply user/group OK"
    # 日志时间格式 [YYYY-MM-DD HH:MM:SS]，字典序比较即时间序，取本次发送后的行
    if [[ $GOT_OUT -eq 0 ]] && \
       awk -v t="$START_HUMAN" '
           match($0, /\[[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}\]/) {
               ts = substr($0, RSTART+1, 19)
               if (ts >= t && $0 ~ /reply (user|group) OK/) { found=1 }
           }
           END { exit found ? 0 : 1 }' "$MONITOR_LOG" 2>/dev/null; then
        GOT_OUT=1
    fi
    [[ $GOT_IN -eq 1 && $GOT_OUT -eq 1 ]] && break
done

if [[ $GOT_IN -eq 1 ]]; then
    pass "V2 入站已记录（agent-connect.log 含 ${CODE}）"
    echo "$IN_LINE" | sed 's/^/    /'
else
    fail "V2 未见入站记录（agent-connect.log 无 ${CODE}）"
fi
if [[ $GOT_OUT -eq 1 ]]; then
    pass "V3 出站已记录（monitor.log 有 reply OK，时间在发送之后）"
else
    fail "V3 未见出站记录（monitor.log 无 发送后的 reply OK）"
fi

echo ""
echo "=== V4: 独立拉数字员工回复，断言正确答案 ==="
# 从 V2 入站行提取本会话 convId，用 list --group 拉（对 o2o/群都可靠；
# list-by-sender 实测不索引 o2o 回复，故不用它做 o2o 校验）
# list API 最终一致，可能先返回 0 条，故轮询几轮。
CONV_ID="$(echo "$IN_LINE" | sed -nE 's/.*convId=([^ ]+).*/\1/p')"
REPLY=""; LIST_OUT="(未能从入站行解析 convId)"
if [[ -n "$CONV_ID" ]]; then
    for ((i=0; i<6; i++)); do
        LIST_OUT="$(dws chat message list --profile "$SENDER_PROFILE" \
            --group "$CONV_ID" --time "$START_HUMAN" --direction newer \
            --limit 10 -y 2>&1)"
        # 匹配窗口内**任一**数字员工回复含正确答案（不取最后一条——避免被后续
        # 无关消息如 "⚠️ 暂时无法处理" 盖掉），命中即回填该条内容
        REPLY="$(echo "$LIST_OUT" | BOT_USER="$BOT_USER" ANSWER="$ANSWER" \
            SELF_NAMES="${AGENT_SELF_NAMES:-}" python3 -c '
import json, os, sys
try:
    d = json.load(sys.stdin)
except Exception:
    print(""); sys.exit(0)
res = d.get("result", d)
msgs = res.get("messages", []) if isinstance(res, dict) else []
bot = os.environ.get("BOT_USER", "")
ans = os.environ.get("ANSWER", "")
names = {n.strip() for n in os.environ.get("SELF_NAMES", "").split(",") if n.strip()}
# list --group 的 sender 是显示名（非 userId）；判定为数字员工回复：
# sender 命中 userId 或 AGENT_SELF_NAMES 任一名字
bot_msgs = [(m.get("content") or "").strip() for m in msgs
            if (str(m.get("sender") or "") == bot or str(m.get("sender") or "") in names)
            and (m.get("content") or "").strip()]
hit = next((c for c in bot_msgs if ans in c), "")
print(hit or (bot_msgs[0] if bot_msgs else ""))
' 2>/dev/null)"
        [[ -n "$REPLY" && "$REPLY" == *"$ANSWER"* ]] && break
        sleep 3
    done
fi

if [[ -z "$REPLY" ]]; then
    fail "V4 未拉到数字员工回复（convId=${CONV_ID:-?}）"
    echo "$LIST_OUT" | head -6 | sed 's/^/    /'
elif echo "$REPLY" | grep -q "$ANSWER"; then
    pass "V4 回复正确：数字员工回复含 \"$ANSWER\""
    echo "    回复内容：$REPLY"
else
    fail "V4 回复不含正确答案 \"$ANSWER\""
    echo "    实际回复：$REPLY"
fi

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
if [[ $FAIL -eq 0 ]]; then
    echo "✅ 文本回复真实链路端到端测试通过（V1-V4）"
    echo "   $CODE  \"$QUESTION\"  →  \"$REPLY\""
    exit 0
else
    echo "❌ 文本回复端到端测试存在失败项（见上 V1-V4）"
    echo "   排错：tail -f monitor.log agent-connect.log opencode.log"
    exit 1
fi
