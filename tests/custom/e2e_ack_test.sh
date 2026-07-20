#!/bin/bash
# e2e_ack_test.sh — 回执能力（已读 + 状态「文字表情」时间线）端到端测试
#
# 验证：收到消息 → mark-read + 在用户消息上贴「文字表情」（表情+文字）→ 随进度**原地更新**
# （收到→处理中→…）→ 回复发出（on_reply_sent）→ 换「完成」文字表情。
#
# 验证点：
#   V1. dws 命令可用（--dry-run 校验 create/add/remove-text-emotion + mark-read，无副作用）
#   V2. LIVE 真实文字表情生命周期（对一条真实单聊消息）：
#       走 ack 代码路径 on_inbound → 文字表情逐级升级（用短 stages 加速）→
#       on_reply_sent(ok=True) → 换「完成」，用 list-emotion-replies 确认表情变化，
#       最后 remove 清理。需提供真实消息坐标，否则 SKIP：
#         ACK_E2E_CONV_ID=<openConversationId> ACK_E2E_MSG_ID=<openMsgId>
#
# 用法：
#   bash tests/custom/e2e_ack_test.sh
#   ACK_E2E_CONV_ID=... ACK_E2E_MSG_ID=... bash tests/custom/e2e_ack_test.sh

set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$SCRIPT_DIR"
FAIL=0
pass() { echo "  ✅ $1"; }
fail() { echo "  ❌ $1"; FAIL=1; }

[[ -f "$SCRIPT_DIR/config/constants.local.sh" ]] && source "$SCRIPT_DIR/config/constants.local.sh"
PROF="${AGENT_PROFILE:-${DWS_PROFILE:-}}"

echo "=== V1: dws 文字表情命令 dry-run 校验（无副作用）==="
if ! command -v dws >/dev/null 2>&1; then
    echo "  ⏭️  SKIP：未找到 dws CLI"
else
    CID="${ACK_E2E_CONV_ID:-cidDUMMY==}"
    args=(--profile "$PROF"); [[ -z "$PROF" ]] && args=()
    ok=1
    dws chat mark-read --conversation-id "$CID" --message-id msgD== --dry-run "${args[@]}" -y >/dev/null 2>&1 \
        && echo "    dry-run mark-read ✓" || { echo "    dry-run mark-read ✗"; ok=0; }
    dws chat message create-text-emotion --emotion-name 稍等 --text 处理中 --dry-run "${args[@]}" -y >/dev/null 2>&1 \
        && echo "    dry-run create-text-emotion ✓" || { echo "    dry-run create-text-emotion ✗"; ok=0; }
    dws chat message add-text-emotion --conversation-id "$CID" --msg-id msgD== --emotion-id 1 --emotion-name 稍等 --text 处理中 --background-id im_bg_3 --dry-run "${args[@]}" -y >/dev/null 2>&1 \
        && echo "    dry-run add-text-emotion ✓" || { echo "    dry-run add-text-emotion ✗"; ok=0; }
    dws chat message remove-text-emotion --conversation-id "$CID" --msg-id msgD== --emotion-id 1 --emotion-name 稍等 --text 处理中 --background-id im_bg_3 --dry-run "${args[@]}" -y >/dev/null 2>&1 \
        && echo "    dry-run remove-text-emotion ✓" || { echo "    dry-run remove-text-emotion ✗"; ok=0; }
    [[ $ok -eq 1 ]] && pass "文字表情命令均可用" || fail "部分文字表情命令 dry-run 失败"
fi

echo ""
echo "=== V2 (LIVE): 真实文字表情生命周期 ==="
if [[ -z "${ACK_E2E_CONV_ID:-}" || -z "${ACK_E2E_MSG_ID:-}" ]]; then
    echo "  ⏭️  SKIP：未提供 ACK_E2E_CONV_ID / ACK_E2E_MSG_ID"
elif ! command -v dws >/dev/null 2>&1 || [[ -z "$PROF" ]]; then
    echo "  ⏭️  SKIP：dws / profile 不可用"
else
    CAP_ACK_ENABLED=1 ACK_DONE_TIMEOUT=20 \
    ACK_STAGES="0:收到:🈺 已收到，正在处理…|1:稍等:⏳ 正在处理中…|2:咖啡:⏳ 仍在处理…" \
    ACK_DONE="OK:✅ 已处理完成" \
    ACK_E2E_CONV_ID="$ACK_E2E_CONV_ID" ACK_E2E_MSG_ID="$ACK_E2E_MSG_ID" \
    python3 - <<'PY'
import os, sys, time
sys.path.insert(0, os.path.join(os.getcwd(), "src"))
from custom.capabilities import ack
from core.inbound import InboundMessage, KIND_TEXT

cid = os.environ["ACK_E2E_CONV_ID"]; mid = os.environ["ACK_E2E_MSG_ID"]
# 本轮涉及的所有 (表情,文字) 用于起点/收尾清理
ALL = [(e, t) for _, e, t in ack._STAGES] + [ack._DONE, ack._ERROR]

def reactors():
    """当前消息上有多少条表情回应（数字员工 opencode 的）。"""
    rc, out = ack._run_cli(["chat", "message", "list-emotion-replies", "--msg-ids", mid], timeout=15)
    import json
    try:
        d = json.loads(out)
        return sum(len(m.get("emotionReplyList", []))
                   for m in d.get("result", {}).get("messages", []))
    except Exception:
        return -1

# 干净起点：移除本轮所有可能残留
for e, t in ALL:
    ack._remove_text_emotion(cid, mid, e, t)
time.sleep(1)
base = reactors()
print(f"    起点表情回应数: {base}")

msg = InboundMessage(user="e2e-tester", text="hi", conv_type="1", conv_id=cid, msg_id=mid, kind=KIND_TEXT)
assert ack.on_inbound(msg) is False, "on_inbound 应非消费(False)"

# 观察升级：应先出现（表情回应数 > base），且随时间保持只有 1 条（升级=移除旧+贴新）
grew = False
max_extra = 0
for _ in range(20):
    n = reactors()
    if n > base:
        grew = True
        max_extra = max(max_extra, n - base)
    time.sleep(0.4)
    if max_extra >= 1 and grew:
        break
print(f"    出现文字表情回应: {grew}；峰值额外条数: {max_extra}")
assert grew, "未看到文字表情回应出现"

# 回复已发出 → 收尾切到「完成」。finalize=remove旧+create+add完成(~5s)，等它彻底落地。
ack.on_reply_sent(cid, "1", True)
time.sleep(7)
final_n = reactors()
print(f"    收尾后表情回应数: {final_n}（应为 base+1 = {base+1}，单条不堆积）")
assert final_n == base + 1, f"收尾后应只剩一条，实际 {final_n}"

# 证明"最后一条就是完成态"：list-emotion-replies 看不出文字，故直接移除「完成」文字表情，
# 若计数回落到 base，说明当前挂着的正是「完成」态（否则移除无效、计数不变）。
ok = ack._remove_text_emotion(cid, mid, ack._DONE[0], ack._DONE[1])
time.sleep(1)
after = reactors()
print(f"    移除「完成」态后: {after}（应回到 base={base}，证明收尾态确为完成）")
assert ok and after == base, f"收尾态非完成 / 清理不净: after={after}"
print("V2_OK")
PY
    if [[ $? -eq 0 ]]; then
        pass "真实文字表情时间线通过（收到→升级→完成，单条不堆积，已清理）"
    else
        fail "真实文字表情生命周期失败"
    fi
fi

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
if [[ $FAIL -eq 0 ]]; then
    echo "✅ 回执能力 e2e 通过（V1；V2 见上）"; exit 0
else
    echo "❌ 回执能力 e2e 存在失败项"; exit 1
fi
