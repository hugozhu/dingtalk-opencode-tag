#!/usr/bin/env python3
"""端到端验证 #109：主管审核的「不发消息」出口要收尾 ack，不能一直播「仍在处理中」。

复现的线上故障：主管在群里 @ 数字员工提问 → 草稿转交主管 → 主管单聊回「#1 忽略」→
双方都不再有任何回复，但**两条消息的 ack worker 都还在等 reply-sent 信号**，于是每
ACK_PROGRESS_INTERVAL 秒各往会话里播一条「⏳ 仍在处理中」，直到 ACK_DONE_TIMEOUT
（默认 65 分钟）。群里尤其刺眼：主管已经决定不回答了，群里还在报进度。

链路全真实（仅 stub 平台发送 / 表情 CLI / LLM 草稿）：
  dispatch_inbound(群消息, @我)
    → group_gate 放行（被 @）→ ack 起 worker 贴「处理中」
    → supervisor_review 拦截：后台出草稿 → 转交主管 → 登记待审（**不回群**）
  dispatch_inbound(主管单聊「#1 忽略」)
    → ack 起 worker 贴「处理中」→ supervisor_review 裁决
    → _close_ack(群, ok=None)   静默收尾提问者那条：只移除进度，不贴「完成」
    → _close_ack(主管, ok=True) 裁决消息自己收尾（回执裸发不经 send_reply）

把 ACK_PROGRESS_INTERVAL 压到 1s：修好后裁决完再等 3.5s 一条心跳都不该有；
旧代码会稳定喷 3 条以上。
"""
import atexit
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "src"))

# 环境必须在 import 能力之前设好（ack/supervisor_review 的开关在 import 期定型）
os.environ["CAP_SUPERVISOR_REVIEW_ENABLED"] = "1"
os.environ["AGENT_SUPERVISOR_USER_ID"] = "sup-e2e"
os.environ["AGENT_SUPERVISOR_NAME"] = "boss"
os.environ["ACK_STAGES"] = "0:稍等:已收到，正在处理…"   # 立即贴处理中，无中间升级
os.environ["ACK_PROGRESS_INTERVAL"] = "1"              # 心跳压到 1s（线上 300s）
os.environ["ACK_PROGRESS_MESSAGE"] = "1"               # 心跳要发独立消息（本测就是抓它）
os.environ["SUPERVISOR_REVIEW_TIMEOUT"] = "600"    # 不让超时兜底插进来
# 审核流水指到 tmpdir —— 否则会写进真实 knowledge/，且短号从上次的高水位续起，
# 本测里写死的「#1 …」就对不上了（e2e 之间互相串味）
_TMP = tempfile.mkdtemp(prefix="e2e-sup-")
atexit.register(shutil.rmtree, _TMP, True)   # sys.exit 在前，收尾只能挂 atexit
os.environ["SUPERVISOR_REVIEW_JOURNAL"] = os.path.join(_TMP, "reviews.jsonl")
os.environ["AGENT_MSGSTORE_DIR"] = os.path.join(_TMP, "messages")

import custom.capabilities                      # noqa: E402  注册全部能力
import custom.capabilities.ack as ACK           # noqa: E402
import custom.capabilities.supervisor_review as SR  # noqa: E402
import custom.replier as CUSTOM_REPLIER         # noqa: E402
import core.replier as CR                       # noqa: E402
from core.capabilities import dispatch_inbound  # noqa: E402
from core.inbound import InboundMessage, KIND_TEXT  # noqa: E402

GRP, GRP_MSG = "e2e-grp-conv", "m-grp-1"
SUP, SUP_MSG = "e2e-sup-conv", "m-sup-1"

replies = []    # send_reply 真发出去的（conv_id, text）
notices = []    # send_notice 发的进度心跳（conv_id, text）—— 核心观测对象
cards = []      # 转交主管的待审卡片
emotions = []   # 表情时间线 (msg_id, op, emoji, text)


def fake_send_impl(conv_id, conv_type, text, *, at_user_id=None):
    replies.append((conv_id, text))
    print(f"  [reply→{conv_id}] {text[:40]!r}")
    return True


def fake_notice(conv_id, conv_type, text, *, at_user_id=None):
    """send_notice 的平台出口 —— ack 的进度心跳走这里。"""
    notices.append((conv_id, text))
    print(f"  [⏳心跳→{conv_id}] {text[:40]!r}")
    return True


def fake_card(text):
    cards.append(text)
    print(f"  [卡片→主管] {text.splitlines()[0][:50]!r}")
    return True


def fake_add(conv_id, msg_id, emoji, text):
    emotions.append((msg_id, "add", emoji, text))
    print(f"  [表情+ {msg_id}] {emoji}｜{text}")
    return True


def fake_cli(args, timeout=15):
    """清除进度表情走的是 _set_status 里的内联 remove-text-emotion（不经 _remove_text_emotion）。"""
    if "remove-text-emotion" in args:
        msg_id = args[args.index("--msg-id") + 1]
        emotions.append((msg_id, "remove", "", ""))
        print(f"  [表情- {msg_id}] 移除进度，不贴终态")
    return (0, "{}")


def fake_update(conv_id, msg_id, old_eid, old_bid, new_emoji, new_text, new_eid, new_bid):
    emotions.append((msg_id, "add", new_emoji, new_text))
    print(f"  [表情↻ {msg_id}] → {new_emoji}｜{new_text}")
    return True


CR.register_replier(fake_send_impl)
CUSTOM_REPLIER._dingtalk_send = fake_notice      # send_notice 的底层出口
ACK._add_text_emotion = fake_add
ACK._update_text_emotion = fake_update
ACK._run_cli = fake_cli
ACK._mark_read = lambda conv_id, msg_id: True
ACK._emotion_id = lambda emoji, text: ("eid", "bid")
SR._send_to_supervisor = fake_card
SR.generate_reply_ex = lambda user, text, ctx=None, raw=False: ("AI 草稿：我能干这些", "ok")
SR._locate_card_msg_id = lambda seq: ""   # 反查卡片 msgId 会真调 dws，本测不关心贴表情

# 反证开关：E2E_SIMULATE_BUG=1 只把 _close_ack 变成 no-op（其余一字不改），精确还原
# #109 修复前的行为，此时本测必须 FAIL。证明这几条断言真能抓住回归，而不是恒绿。
if os.environ.get("E2E_SIMULATE_BUG") == "1":
    SR._close_ack = lambda *a, **k: None
    print("⚠️  E2E_SIMULATE_BUG=1：已禁用 _close_ack，模拟修复前行为（期望 FAIL）\n")


def _wait(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def _final_of(msg_id):
    """该消息表情时间线的最后一个动作。"""
    seq = [e for e in emotions if e[0] == msg_id]
    return seq[-1] if seq else None


print("=== 1) 主管在群里 @ 数字员工提问 ===")
dispatch_inbound(InboundMessage(
    user="boss", text="@一粟 你有哪些能力", conv_type="2",
    conv_id=GRP, msg_id=GRP_MSG, kind=KIND_TEXT, extra={"at_mention": True}))
got_card = _wait(lambda: cards)

print("\n=== 2) 主管单聊回「#1 忽略」===")
dispatch_inbound(InboundMessage(
    user="boss", text="#1 忽略", conv_type="1",
    conv_id=SUP, msg_id=SUP_MSG, kind=KIND_TEXT))
settled = _wait(lambda: not ACK._pending)   # 两个 worker 都收尾 = 登记表清空

print(f"\n=== 3) 再等 3.5s（3 个心跳周期），看还有没有「仍在处理中」===")
notices_at_verdict = len(notices)
time.sleep(3.5)

# ---- 断言 ----
grp_final = _final_of(GRP_MSG)
sup_final = _final_of(SUP_MSG)

v1 = got_card and not any(c == GRP for c, _ in replies)
v2 = settled
v3 = bool(grp_final) and grp_final[1] == "remove"
v4 = bool(sup_final) and sup_final[1] == "add" and "完成" in sup_final[3]
v5 = len(notices) == notices_at_verdict == 0

print(f"\n表情终态 群={grp_final} 主管={sup_final}")
print(f"进度心跳: {notices or '（无）'}")
print(f"发出的回复: {replies or '（无）'}")

print("\n=== 结果 ===")
print(f"  V1 群里不落草稿，先转交主管      : {'✅' if v1 else '❌'}")
print(f"  V2 裁决后两个 ack worker 都收尾  : {'✅' if v2 else '❌'}（不再挂死等信号）")
print(f"  V3 提问者那条静默收尾（只移除）  : {'✅' if v3 else '❌'}（没答就别贴「完成」）")
print(f"  V4 主管裁决消息落「完成」终态    : {'✅' if v4 else '❌'}")
print(f"  V5 全程零「⏳ 仍在处理中」心跳   : {'✅' if v5 else '❌'}（#109 核心回归）")

allok = v1 and v2 and v3 and v4 and v5
print("PASS" if allok else "FAIL")
sys.exit(0 if allok else 1)
