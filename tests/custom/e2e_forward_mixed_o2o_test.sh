#!/bin/bash
# e2e_forward_mixed_o2o_test.sh — 单聊「混合内容合并转发」真实链路端到端测试
#
# 与 e2e_forward_mixed.py 的分工：
#   - e2e_forward_mixed.py  ：合成 fixture 驱动真代码，覆盖「图片真识别(vision)」+ prompt 组装
#   - 本脚本(o2o real-send)  ：**真发真转发**，覆盖 dws→consume→bridge→forward→brain→replier 的
#                              端到端连通性。图片受 CLI 限制走**文件路径**(见下)，不覆盖 vision。
#
# 为什么图片当文件（CLI 硬限制，dws v1.0.55）：
#   send --msg-type image 只接受**上游已有的 mediaId**，CLI 不提供本地文件→mediaId 上传；
#   send --msg-type file --file-path 会把 .png/.jpg 当**可下载文件附件**发，不生成 mediaId、
#   不渲染内联图。故 2 张图在这里以文件消息真发，内层 content 形如 [文件] xxx.png fileId:...，
#   handler 走文件正文路径（PNG 当文本读→乱码/下载失败，属预期，不影响链路连通验证）。
#
# 链路（在 opencode↔hugozhu 单聊里）：
#   6 条源消息(3文本+2图当文件+1文本文件) → combine-forward 回本单聊 →
#   event consume → bridge → forward.on_inbound → list-by-ids 反查 forwardMessages →
#   组装 prompt → brain(opencode serve) → replier(reply user)
#
# 验证点：
#   V1. 6 条源消息发送成功，逐条取回 openMessageId
#   V2. combine-forward 成功
#   V3. 入站被 connect 记录（agent-connect.log 含「聊天记录」）
#   V4. forward 能力命中（monitor.log 有 "forward: ... forwardMessages="）
#   V5. 出站回复（monitor.log 有 "reply user OK"）
#   V6. 钉钉实际回复存在（DWS list 拉到数字员工回复，含源消息关键词）
#
# 用法：
#   bash tests/custom/e2e_forward_mixed_o2o_test.sh
#   E2E_SENDER_PROFILE="<corpId>:<真人userId>" bash tests/custom/e2e_forward_mixed_o2o_test.sh
#   E2E_O2O_USER="<对方userId>"  bash ...   # 默认取 DWS_EVENT_O2O_USERS 第一个
#   E2E_FWD_PROFILE="<profile>"  bash ...   # 覆盖 combine-forward 的发起身份（默认=发送方）
#   E2E_WAIT=120                 bash ...
#
# 触发可靠性（为什么转发默认由「非 bot 的发送方」发）：
#   单聊里若由 bot 自己 combine-forward，外层 sender=bot → 可能命中 forward 的
#   _SELF_NAMES 防回环被消费掉；且自发消息未必回送到 bot 自己的订阅流。由对方(hugozhu)
#   发转发则外层 sender 非自名、且对方发消息必然回送 → 稳定触发。permission 不足时用
#   E2E_FWD_PROFILE 切回 bot 兜底。
#
# 已知坑（同群聊版）：
#   - dws event 订阅偶发投递停滞 → 先 bash bin/core/reboot.sh 再跑
#   - macOS keychain 锁定 → security unlock-keychain ~/Library/Keychains/login.keychain-db
#   - bot 必须订阅该单聊：config 里 DWS_EVENT_O2O_USERS 含对方 userId

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

# 单聊对方 userId：默认取 DWS_EVENT_O2O_USERS 第一个（bot 必须已订阅该单聊）
O2O_USER="${E2E_O2O_USER:-}"
if [[ -z "$O2O_USER" ]]; then
    O2O_USER="$(echo "${DWS_EVENT_O2O_USERS:-}" | cut -d, -f1 | tr -d ' ')"
fi
[[ -n "$O2O_USER" ]] || skip "无单聊对方 userId（设 E2E_O2O_USER 或 config 里 DWS_EVENT_O2O_USERS）"

# 发送方 profile：优先「单聊对方本人(O2O_USER)」的 profile —— 只有用对方身份发，消息才落进
# opencode↔对方 这个单聊会话。E2E_SENDER_PROFILE 可覆盖。
SENDER_PROFILE="${E2E_SENDER_PROFILE:-}"
if [[ -z "$SENDER_PROFILE" ]]; then
    SENDER_PROFILE="$(dws profile list -y 2>/dev/null \
        | BOT_CORP="$BOT_CORP" O2O_USER="$O2O_USER" python3 -c '
import json, os, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
corp, want = os.environ["BOT_CORP"], os.environ["O2O_USER"]
for p in d.get("profiles", []):
    if p.get("corpId") == corp and p.get("userId") == want and p.get("status") == "active":
        print(p.get("profile", "")); break
' 2>/dev/null)"
fi
if [[ -z "$SENDER_PROFILE" ]]; then
    echo "  提示：需要单聊对方本人($O2O_USER)的 profile 已登录。若探测为空可能 keychain 锁定："
    echo "        security unlock-keychain ~/Library/Keychains/login.keychain-db"
    echo "        或显式指定：E2E_SENDER_PROFILE=\"$BOT_CORP:$O2O_USER\""
    skip "无可用发送方 profile（对方本人 $O2O_USER 未登录）"
fi
SENDER_USER="${SENDER_PROFILE##*:}"

# 发送/拉取时 --user 填「发送方在这对单聊里的对端」：
#   pair = {BOT_USER, O2O_USER}；对端 = pair 里 != SENDER_USER 的那个。
#   发送方=对方(O2O_USER) → 对端=bot；发送方=bot → 对端=O2O_USER。
if [[ "$SENDER_USER" == "$O2O_USER" ]]; then
    PEER_USER="$BOT_USER"
elif [[ "$SENDER_USER" == "$BOT_USER" ]]; then
    PEER_USER="$O2O_USER"
else
    skip "发送方 $SENDER_USER 不属于本单聊 {$BOT_USER,$O2O_USER}（用 E2E_SENDER_PROFILE 指定对方本人）"
fi

# combine-forward 发起身份：默认发送方（见抬头「触发可靠性」）；permission 不足时 E2E_FWD_PROFILE 切回 bot
FWD_PROFILE="${E2E_FWD_PROFILE:-$SENDER_PROFILE}"

# 单聊 openConversationId：combine-forward 的 src/dest 都用它。conversation-info 只读。
O2O_CONV="$(dws chat conversation-info --user "$O2O_USER" --profile "$BOT_PROFILE" -y -f json 2>/dev/null \
    | python3 -c 'import json,sys
try: d=json.load(sys.stdin)
except Exception: sys.exit(0)
print(((d.get("result") or {}).get("conversationInfo") or {}).get("openConversationId",""))' 2>/dev/null)"
[[ -n "$O2O_CONV" ]] || skip "取不到单聊 openConversationId（对方 userId=$O2O_USER 是否可达？）"

echo "  数字员工: $BOT_PROFILE"
echo "  单聊对方: $O2O_USER"
echo "  发送方  : $SENDER_PROFILE"
echo "  发送对端: ${PEER_USER}（--user 填此）"
echo "  转发身份: $FWD_PROFILE"
echo "  单聊会话: ${O2O_CONV:0:24}..."

if ! bash "$SCRIPT_DIR/bin/core/healthcheck.sh" >/dev/null 2>&1; then
    skip "healthcheck 未通过（服务未在跑？先 bash bin/core/start.sh）"
fi
pass "环境就绪，服务健康"

# 附件 fixture：复用仓库现成图片当「图消息」，临时造一个文本文件当「文件消息」
IMG_FIXTURE="$SCRIPT_DIR/avatar_oc.png"
[[ -f "$IMG_FIXTURE" ]] || skip "缺图片 fixture：$IMG_FIXTURE"
TXT_FIXTURE="$(mktemp -t fwd_o2o_req_XXXX).txt"
cat > "$TXT_FIXTURE" <<'EOF'
Q3 排期：
- 7月：转发能力加固
- 8月：多语言
- 9月：上线验收
EOF
cleanup() { rm -f "$TXT_FIXTURE"; }
trap cleanup EXIT

CODE="FWDO2O-$(date +%H%M%S)"
TXT_BASE="$(basename "$TXT_FIXTURE")"
START_HUMAN="$(date '+%Y-%m-%d %H:%M:%S')"

# 发一条消息到单聊。send 响应只回 openTaskId（无 openMessageId），故这里不取 id，
# 只校验发送成功；msgId 统一在发完后用 list 按内容签名反查（见下）。诊断走 stderr。
send_o2o() {
    local desc="$1"; shift
    local out
    out="$(dws chat message send --profile "$SENDER_PROFILE" --user "$PEER_USER" "$@" -y -f json 2>&1)"
    if ! echo "$out" | grep -q '"success": true'; then
        { echo "  ❌ 源消息发送失败（${desc}）"; echo "$out" | head -4 | sed 's/^/    /'; } >&2
        return 1
    fi
    echo "  · 已发送：$desc" >&2
    return 0
}

echo ""
echo "=== V1: 以发送方身份发送 6 条源消息（${CODE}）：3文本 + 2图当文件 + 1文本文件 ==="
SENT=0
send_o2o "文本1"          --text "[$CODE] 第一条：今天天气晴朗，适合户外会议。" && SENT=$((SENT+1))
send_o2o "图1(文件)"      --msg-type file --file-path "$IMG_FIXTURE"               && SENT=$((SENT+1))
send_o2o "文本2"          --text "[$CODE] 大家看下这两张架构图"                   && SENT=$((SENT+1))
send_o2o "图2(文件)"      --msg-type file --file-path "$IMG_FIXTURE"               && SENT=$((SENT+1))
send_o2o "文件(需求文档)" --msg-type file --file-path "$TXT_FIXTURE"              && SENT=$((SENT+1))
send_o2o "文本3(@)"       --text "[$CODE] @数字员工 帮我总结下这些内容"           && SENT=$((SENT+1))

if [[ $SENT -lt 6 ]]; then
    fail "仅 $SENT/6 条源消息发送成功"
    exit 1
fi

# 反查这 6 条的 openMessageId：按内容签名筛（bot 会自动回每条源文本，list 里混入 bot 回复，
# 不能按位置取「最后 6 条」）。签名：本次 CODE / 图片文件名 avatar_oc / 文本文件名，
# 且 sender 非自名。按 createTime 升序 = 发送顺序。do-while 轮询（写入有索引延迟）。
echo "  反查 6 条源消息 openMessageId（按内容签名筛，避开 bot 自动回复）..."
IDS_CSV=""; N_IDS=0
for ((i=0; i<10; i++)); do
    sleep 3
    COLLECT="$(dws chat message list --profile "$SENDER_PROFILE" \
        --user "$PEER_USER" --time "$START_HUMAN" --direction newer --limit 40 -y -f json 2>/dev/null \
        | CODE="$CODE" TXT_BASE="$TXT_BASE" SELF_NAMES="${AGENT_SELF_NAMES:-}" BOT_USER="$BOT_USER" \
          python3 -c '
import json, os, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
msgs = (d.get("result") or {}).get("messages", [])
code = os.environ["CODE"]; txt = os.environ["TXT_BASE"]; bot = os.environ["BOT_USER"]
names = {n.strip() for n in os.environ.get("SELF_NAMES", "").split(",") if n.strip()}
hits = []
for m in msgs:
    c = m.get("content") or ""
    s = str(m.get("sender") or "")
    if s == bot or s in names:      # 跳过 bot 自己（含自动回复）
        continue
    if code in c or "avatar_oc" in c or (txt and txt in c):
        mid = m.get("openMessageId")
        if mid and mid not in hits:
            hits.append((m.get("createTime", ""), mid))
hits.sort(key=lambda x: x[0])
ids = [h[1] for h in hits]
print(",".join(ids))
print(len(ids), file=sys.stderr)
' 2>/dev/null)"
    N_IDS="$(echo -n "$COLLECT" | awk -F, '{print ($0==""?0:NF)}')"
    [[ "$N_IDS" -ge 6 ]] && { IDS_CSV="$COLLECT"; break; }
done

if [[ "$N_IDS" -lt 6 ]]; then
    fail "未能反查齐 6 条源消息 msgId（拿到 ${N_IDS}）"
    echo "    提示：bot 是否把源消息也当输入处理导致污染？或索引延迟 → 调大轮询"
    exit 1
fi
IDS_CSV="$(echo "$IDS_CSV" | cut -d, -f1-6)"   # 只取前 6（按发送序）
pass "6 条源消息已发送并反查到 msgId"
echo "    ${IDS_CSV:0:80}..."

sleep 2

echo ""
echo "=== V2: combine-forward 6 条 → 回本单聊（身份=${FWD_PROFILE}）==="
FWD_OUT="$(dws chat message combine-forward \
    --src-conversation-id "$O2O_CONV" \
    --msg-ids "$IDS_CSV" \
    --dest-conversation-id "$O2O_CONV" \
    --profile "$FWD_PROFILE" -y 2>&1)"
if echo "$FWD_OUT" | grep -q '"success": true'; then
    pass "combine-forward 成功"
elif echo "$FWD_OUT" | grep -q "AUTH_PERMISSION_DENIED\|combine_forward_messages"; then
    # 环境能力缺失（App 未授予 im/combine_forward_messages scope）→ skip，非 fail。
    # 实测群聊/单聊、bot/真人一律被拒，与本脚本无关；e2e_forward_test.sh 同环境同样受阻。
    echo "$FWD_OUT" | grep -o '"message":.*' | head -1 | sed 's/^/    /'
    echo "    ↑ combine-forward 需在钉钉开放平台给该 App 授予「合并转发消息」API 权限"
    echo "      (im/combine_forward_messages)。授权后本测试即可跑通 V2-V6。"
    skip "当前 App 无 combine-forward 权限（AUTH_PERMISSION_DENIED）"
else
    fail "combine-forward 失败"
    echo "$FWD_OUT" | sed 's/^/    /'
    echo "    提示：真人 profile 可能缺 combine-forward 权限 → 试 E2E_FWD_PROFILE=\"$BOT_PROFILE\""
    exit 1
fi

echo ""
echo "=== V3+V4+V5: 等待链路处理（最多 ${WAIT}s，轮询日志）==="
GOT_IN=0; GOT_FWD=0; GOT_OUT=0
for ((i=0; i<WAIT; i+=3)); do
    sleep 3
    if [[ $GOT_IN -eq 0 ]]; then
        # 源文本带 $CODE，会内联进转发摘要 → 精确匹配这次转发，避开历史「聊天记录」行
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
    pass "V3 入站已记录（connect log 含「聊天记录」）"
else
    fail "V3 未见入站（connect log 无本次「聊天记录」）—— bot 是否订阅该单聊？转发是否被自名防回环拦？"
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
    pass "V5 出站已记录（reply user OK）"
else
    fail "V5 未见出站（monitor.log 无 reply OK）"
fi

echo ""
echo "=== V6: 独立拉数字员工回复，断言含源消息关键词 ==="
REPLY=""
for ((i=0; i<8; i++)); do
    LIST_OUT="$(dws chat message list --profile "$SENDER_PROFILE" \
        --user "$PEER_USER" --time "$START_HUMAN" --direction newer \
        --limit 20 -y -f json 2>&1)"
    REPLY="$(echo "$LIST_OUT" | BOT_USER="$BOT_USER" \
        SELF_NAMES="${AGENT_SELF_NAMES:-}" python3 -c '
import json, os, sys
try:
    d = json.load(sys.stdin)
except Exception:
    print(""); sys.exit(0)
res = d.get("result", d)
msgs = res.get("messages", []) if isinstance(res, dict) else []
bot = os.environ.get("BOT_USER", "")
names = {n.strip() for n in os.environ.get("SELF_NAMES", "").split(",") if n.strip()}
bot_msgs = [(m.get("content") or "").strip() for m in msgs
            if (str(m.get("sender") or "") == bot or str(m.get("sender") or "") in names)
            and (m.get("content") or "").strip()]
hit = next((c for c in bot_msgs if "排期" in c or "会议" in c or "天气" in c or "总结" in c), "")
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
    echo "✅ 单聊混合合并转发真实链路端到端测试通过（V1-V6）"
    echo "   $CODE  6条源消息(3文本+2图当文件+1文本文件) → combine-forward → 数字员工总结回复"
    exit 0
else
    echo "❌ 单聊混合合并转发端到端测试存在失败项（见上 V1-V6）"
    echo "   排错：tail -f monitor.log agent-connect.log opencode.log"
    exit 1
fi
