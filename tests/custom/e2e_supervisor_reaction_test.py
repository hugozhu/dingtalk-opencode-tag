#!/usr/bin/env python3
"""端到端验证 #107：主管零打字裁决（贴表情）+ 认不出就问，绝不替主管发话。

链路全真实（仅 stub 平台发送 / dws CLI / LLM 草稿）：
  dispatch_inbound(群消息, @我)
    → group_gate 放行 → ack 贴「处理中」→ supervisor_review 拦截
    → 后台出草稿 → 转交主管（卡片）→ 反查卡片 msgId → 登记待审
  主管给卡片贴 👍
    → bridge 产出的表情行 → classify_line → _on_reaction → _execute_verdict(approve)
    → 草稿发回**群里**，主管收到 ✅ 回执 —— 全程没打一个字

同时验证 #107 D 的防误发：主管回一句「这个不太对」不能被当成答案公开发到群里。

反证开关：E2E_SIMULATE_BUG=1 把表情映射与 unclear 判定退回老行为（认不出=改写），
此时必须 FAIL —— 否则这些断言证明不了任何事。
"""
import atexit
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "src"))

# 环境必须在 import 能力之前设好（开关在 import 期定型）
os.environ["CAP_SUPERVISOR_REVIEW_ENABLED"] = "1"
os.environ["AGENT_SUPERVISOR_USER_ID"] = "sup-e2e"
os.environ["AGENT_SUPERVISOR_NAME"] = "boss"
os.environ["AGENT_SELF_NAMES"] = "一粟"
os.environ["ACK_STAGES"] = "0:稍等:已收到，正在处理…"
os.environ["ACK_PROGRESS_INTERVAL"] = "0"          # 关掉心跳，本测不关心 #109
os.environ["SUPERVISOR_REVIEW_TIMEOUT"] = "600"    # 不让超时兜底插进来
# 审核流水指到 tmpdir —— 否则会写进真实 knowledge/，且短号从上次的高水位续起，
# 本测里写死的「#1 …」就对不上了（e2e 之间互相串味）
_TMP = tempfile.mkdtemp(prefix="e2e-sup-")
atexit.register(shutil.rmtree, _TMP, True)   # sys.exit 在前，收尾只能挂 atexit
os.environ["SUPERVISOR_REVIEW_JOURNAL"] = os.path.join(_TMP, "reviews.jsonl")
os.environ["AGENT_MSGSTORE_DIR"] = os.path.join(_TMP, "messages")
os.environ["SUPERVISOR_REACTION_DEBOUNCE"] = "0"   # 不等反悔宽限期

import custom.capabilities                          # noqa: E402  注册全部能力
import custom.capabilities.ack as ACK               # noqa: E402
import custom.capabilities.supervisor_review as SR  # noqa: E402
import core.replier as CR                           # noqa: E402
from core.capabilities import dispatch_inbound      # noqa: E402
from core.inbound import InboundMessage, KIND_TEXT  # noqa: E402

GRP, GRP_MSG = "e2e-grp", "m-grp-1"
SUP = "e2e-sup"
CARD_MSG = "msgCARD=="

replies = []    # 真发给提问者的 (conv_id, text)
cards = []      # 发给主管的卡片/回执


def fake_send_impl(conv_id, conv_type, text, *, at_user_id=None):
    replies.append((conv_id, text))
    print(f"  [reply→{conv_id}] {text[:50]!r}")
    return True


def fake_card(text):
    cards.append(text)
    print(f"  [→主管] {text.splitlines()[0][:60]!r}")
    return True


def fake_run_cli(args, timeout=60):
    """反查被贴表情的消息正文 —— 贴表情裁决靠它定位是第几号审核。"""
    if "list-by-ids" in args:
        import json as _json
        return 0, _json.dumps({"result": {"messages": [
            {"openMessageId": CARD_MSG, "content": "📋 **待审 #1**　来自：**boss**"}]}})
    return 0, "{}"


CR.register_replier(fake_send_impl)
SR._send_to_supervisor = fake_card
SR._run_cli = fake_run_cli
SR.generate_reply_ex = lambda user, text, ctx=None: ("AI 草稿：我能干这些", "ok")
ACK._mark_read = lambda conv_id, msg_id: True
ACK._emotion_id = lambda emoji, text: ("eid", "bid")
ACK._add_text_emotion = lambda *a: True
ACK._update_text_emotion = lambda *a: True
ACK._run_cli = lambda args, timeout=15: (0, "{}")

# 反证：关掉贴表情裁决（让事件行认不出）+ 认不出的短文本一律当答案
if os.environ.get("E2E_SIMULATE_BUG") == "1":
    SR._on_reaction = lambda msg: None
    _orig_parse = SR._parse_verdict
    SR._parse_verdict = lambda t: (lambda r: (r[0], "rewrite", r[2])
                                   if r[1] == "unclear" else r)(_orig_parse(t))
    print("⚠️  E2E_SIMULATE_BUG=1：关掉贴表情裁决 + 认不出的短文本当答案（期望 FAIL）\n")


def _wait(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def _ask(text="@一粟 你有哪些能力", msg_id=GRP_MSG):
    """提一个问题，等到**待审登记完成**再返回。

    别只等卡片出现 —— 卡片是先发出去、随后才登记待审的，等早了后面读 _pending 会读到
    半成品状态（测试自身的竞态，不是产品行为）。
    """
    before = len(SR._pending)
    dispatch_inbound(InboundMessage(
        user="boss", text=text, conv_type="2", conv_id=GRP, msg_id=msg_id,
        kind=KIND_TEXT, extra={"at_mention": True}))
    return _wait(lambda: cards and len(SR._pending) > before)


print("=== 1) 主管在群里 @ 数字员工提问 ===")
got_card = _ask()
card_text = cards[0] if cards else ""
v_no_leak = got_card and not replies          # 草稿不能落到群里

print("\n=== 2) 卡片应该是瘦的（操作提示压成一行，草稿折叠）===")
v_card_slim = "「#1 同意」放行" in card_text and card_text.count("回「") == 0

print("\n=== 3) 主管给卡片贴 👍（零打字，走真实事件行）===")
_rx = ("[connect] 表情回应 @boss: 赞 (convType=1 convId=e2e-sup "
       f"reactedMsgId={CARD_MSG} reactionOp=add eventId=ev-1)")
dispatch_inbound(SR.classify_line(_rx))
v_approved = _wait(lambda: replies)

print("\n=== 4) 换一条：主管回「这个不太对」——不能被当成答案发到群里 ===")
replies.clear()
cards.clear()
_ask(text="@一粟 报销怎么走", msg_id="m-grp-2")
pending_before = len(SR._pending)
dispatch_inbound(InboundMessage(
    user="boss", text="这个不太对", conv_type="1", conv_id=SUP,
    msg_id="m-sup-2", kind=KIND_TEXT))
# 等澄清消息落地（不裸 sleep —— 见 AGENTS.md 测试约定）。等到了也仍然要断言
# replies 为空：这里要证明的是"什么都没发给提问者"。
_wait(lambda: any("没听懂" in c for c in cards))
v_no_misfire = not replies                      # 群里零消息
v_pending_kept = len(SR._pending) == pending_before
v_asked = any("没听懂" in c for c in cards)

print(f"\n发到群里的: {replies or '（无）'}")
print(f"待审数: {len(SR._pending)}（裁决前 {pending_before}）")

print("\n=== 结果 ===")
print(f"  V1 草稿只转主管、不落群里     : {'✅' if v_no_leak else '❌'}")
print(f"  V2 卡片操作提示压成一行       : {'✅' if v_card_slim else '❌'}")
print(f"  V3 贴 👍 即放行，全程零打字     : {'✅' if v_approved else '❌'}")
print(f"  V4 认不出的话不当答案发出去   : {'✅' if v_no_misfire else '❌'}（#107 核心）")
print(f"  V5 认不出时待审保留           : {'✅' if v_pending_kept else '❌'}")
print(f"  V6 认不出时回一句澄清         : {'✅' if v_asked else '❌'}")

allok = all([v_no_leak, v_card_slim, v_approved, v_no_misfire, v_pending_kept, v_asked])
print("PASS" if allok else "FAIL")
sys.exit(0 if allok else 1)
