#!/bin/bash
# e2e_at_test.sh — @我(AT) 消息订阅 + 处理 端到端测试
#
# 验证新增的「@我」订阅链路端到端打通：
#   dws event consume user_im_message_receive_at   （订阅：被 @ 的消息，跨所有群）
#     → dws_event_bridge.py                        （转 connect-log 格式，convType=2）
#     → core.inbound.parse_line                    （归一成 InboundMessage，kind=text）
#     → core.capabilities.dispatch_inbound         （路由到能力，如 text_reply）
#
# 验证点：
#   V1. dws-connect.sh 订阅计划包含 at consumer（纯逻辑，无需网络）
#   V2. 真实 bridge 子进程把 AT NDJSON 事件转成正确 connect-log 行（convType=2 + 字段）
#   V3. 该行经 inbound 解析为 kind=text/conv_type=2，并被注册表分发到某能力
#   V4. 真实订阅可建联（LIVE）：dws event consume ...receive_at 打印 [event] ready
#       —— 只读、不发任何消息；无 dws / 未登录时 SKIP，不算失败
#
# 用法：
#   bash tests/custom/e2e_at_test.sh          # 跑 V1-V3 + 尝试 V4（可 SKIP）
#   AT_E2E_SKIP_LIVE=1 bash ...               # 只跑 V1-V3（CI / 离线）

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$SCRIPT_DIR"

DWS_CONNECT="$SCRIPT_DIR/bin/custom/dws-connect.sh"
BRIDGE="$SCRIPT_DIR/bin/custom/dws_event_bridge.py"
FAIL=0

pass() { echo "  ✅ $1"; }
fail() { echo "  ❌ $1"; FAIL=1; }

echo "=== V1: dws-connect.sh 订阅计划含 at consumer ==="
PLAN="$(env DWS_CONNECT_SKIP_LOCAL=1 DWS_CONNECT_DRY_RUN=1 CONNECT_LOG=/dev/null \
    DWS_PROFILE=p DWS_EVENT_AT=1 bash "$DWS_CONNECT" 2>/dev/null)"
echo "$PLAN" | sed 's/^/    /'
if echo "$PLAN" | grep -q 'consumer: user_im_message_receive_at'; then
    pass "订阅计划包含 at consumer"
else
    fail "订阅计划未包含 at consumer"
fi

echo ""
echo "=== V2: bridge 把 AT NDJSON 事件转成 connect-log 行 ==="
LINE="$(python3 - "$BRIDGE" <<'PY'
import json, subprocess, sys
bridge = sys.argv[1]
body = {"sender": "hugozhu", "content": "@Claude Code 帮我算下 3+4",
        "openConversationId": "cidE2EAT==", "openMessageId": "msgE2EAT==",
        "createTime": "1700000000000"}
evt = {"type": "event", "event_type": "user_im_message_receive_at",
       "event_id": "e2e-at-1", "data": json.dumps({"payload": {"body": body}}, ensure_ascii=False)}
out = subprocess.run([sys.executable, bridge], input=json.dumps(evt) + "\n",
                     capture_output=True, text=True)
sys.stdout.write(out.stdout)
PY
)"
echo "    $LINE"
if echo "$LINE" | grep -q '收到 @hugozhu: @Claude Code 帮我算下 3+4' \
   && echo "$LINE" | grep -q 'convType=2' \
   && echo "$LINE" | grep -q 'convId=cidE2EAT==' \
   && echo "$LINE" | grep -q 'msgId=msgE2EAT=='; then
    pass "bridge 转换正确（convType=2 + 发送人/正文/会话/消息ID 齐全）"
else
    fail "bridge 转换结果不符预期"
fi

echo ""
echo "=== V3: connect-log 行经 inbound 解析并被能力注册表分发 ==="
AT_LINE="$LINE" python3 - <<'PY'
import os, sys
sys.path.insert(0, os.path.join(os.getcwd(), "src"))
from core import inbound, capabilities

line = os.environ["AT_LINE"].strip()
m = inbound.parse_line(line)
assert m is not None, "inbound.parse_line 返回 None"
assert m.kind == inbound.KIND_TEXT, f"kind 应为 text，实际 {m.kind}"
assert m.conv_type == "2", f"conv_type 应为 2，实际 {m.conv_type}"
assert m.user == "hugozhu", f"user 解析错误：{m.user}"
print(f"    parsed: user={m.user} kind={m.kind} conv_type={m.conv_type} msg_id={m.msg_id}")

# 用一个 stub 能力证明 dispatch_inbound 会把 AT 消息路由过来（不依赖 brain/replier）
seen = {}
capabilities.clear()
capabilities.register(capabilities.Capability(
    name="e2e_at_probe",
    on_inbound=lambda msg: (seen.__setitem__("hit", msg) or True),
    handles_kinds={inbound.KIND_TEXT},
    priority=1,
))
consumed = capabilities.dispatch_inbound(m)
assert consumed is True, "dispatch_inbound 未被能力消费"
assert seen.get("hit") is m, "能力未收到该 AT 消息"
print("    dispatch_inbound → 能力已消费该 AT 文本消息")
print("V3_OK")
PY
if [[ $? -eq 0 ]]; then
    pass "inbound 解析为 text/convType=2 并成功分发到能力"
else
    fail "inbound 解析 / 分发失败"
fi

echo ""
echo "=== V4 (LIVE): 真实 AT 订阅建联（只读，不发消息）==="
if [[ -n "${AT_E2E_SKIP_LIVE:-}" ]]; then
    echo "  ⏭️  SKIP：AT_E2E_SKIP_LIVE 已设置"
elif ! command -v dws >/dev/null 2>&1; then
    echo "  ⏭️  SKIP：未找到 dws CLI"
else
    # shellcheck disable=SC1091
    [[ -f "$SCRIPT_DIR/config/constants.local.sh" ]] && source "$SCRIPT_DIR/config/constants.local.sh"
    if [[ -z "${DWS_PROFILE:-}" ]]; then
        echo "  ⏭️  SKIP：DWS_PROFILE 未配置（config/constants.local.sh）"
    else
        READY_LOG="$(mktemp -t at_e2e_live.XXXXXX)"
        # --duration 让 consumer 到点自停；抓 stderr 的就绪行。整体再套 timeout 兜底。
        timeout 15 dws event consume user_im_message_receive_at \
            --profile "$DWS_PROFILE" -f ndjson --duration 6s >/dev/null 2>"$READY_LOG"
        if grep -qE '\[event\] ready.*user_im_message_receive_at' "$READY_LOG"; then
            pass "AT 订阅建联成功（[event] ready event_key=user_im_message_receive_at）"
            grep -m1 'ready' "$READY_LOG" | sed 's/^/    /'
        else
            fail "AT 订阅未建联（未见 [event] ready）"
            tail -3 "$READY_LOG" | sed 's/^/    /'
        fi
        rm -f "$READY_LOG"
    fi
fi

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
if [[ $FAIL -eq 0 ]]; then
    echo "✅ AT 端到端测试通过（V1-V3 硬校验；V4 LIVE 见上）"
    exit 0
else
    echo "❌ AT 端到端测试存在失败项"
    exit 1
fi
