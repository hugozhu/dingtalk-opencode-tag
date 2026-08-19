#!/usr/bin/env python3
"""端到端验证 #111：消息收到即落盘，事后按 msgId 查得回。

在此之前数字员工是"把钉钉当存储"：主管引用旧消息裁决，靠正文里的「待审 #N」标记 +
`list-by-ids` 回读。没标记的消息（提问者原话、发到群里的答复、别的会话）完全定位不了。

链路全真实（仅 stub 平台发送 / LLM 草稿）：
  dispatch_inbound(提问者的群消息) → msgstore 落盘（priority=-10，先于一切）
  dispatch_inbound(数字员工自己发的卡片回显) → 同样落盘，dir=out
    ↑ 这条是关键：出站消息的 msgId 只能从订阅回显里拿（send 只返回 openTaskId），
      所以 msgstore 必须 loop_guard=False
  主管裁决 → 反馈挂到**提问者那条原始消息**上
  最后按 msgId 查回：提问者原话（无任何「待审 #N」标记）也能查到

反证开关：E2E_SIMULATE_BUG=1 让 msgstore 不落盘，此时必须 FAIL。
"""
import atexit
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "src"))

_TMP = tempfile.mkdtemp(prefix="e2e-msgstore-")
atexit.register(shutil.rmtree, _TMP, True)

os.environ["CAP_SUPERVISOR_REVIEW_ENABLED"] = "1"
os.environ["AGENT_SUPERVISOR_USER_ID"] = "sup-e2e"
os.environ["AGENT_SUPERVISOR_NAME"] = "boss"
os.environ["AGENT_SELF_NAMES"] = "一粟"
os.environ["ACK_PROGRESS_INTERVAL"] = "0"
os.environ["SUPERVISOR_REVIEW_TIMEOUT"] = "0"
os.environ["SUPERVISOR_REVIEW_JOURNAL"] = os.path.join(_TMP, "reviews.jsonl")
# **必须隔离**：主管裁决会把 Q&A 沉淀进知识库，而知识库只注入最后 20 条 ——
# 不隔离的话跑几遍 e2e 就能把生产知识库整个顶掉（已经发生过，31 条夹具）
os.environ["AGENT_KNOWLEDGE_FILE"] = os.path.join(_TMP, "qa.jsonl")
os.environ["AGENT_MSGSTORE_DIR"] = os.path.join(_TMP, "messages")

import custom.capabilities                          # noqa: E402  注册全部能力
import custom.capabilities.ack as ACK               # noqa: E402
import custom.capabilities.supervisor_review as SR  # noqa: E402
import core.replier as CR                           # noqa: E402
from custom import msgstore                         # noqa: E402
from core.capabilities import dispatch_inbound      # noqa: E402
from core.inbound import InboundMessage, KIND_TEXT  # noqa: E402

GRP = "cid群+带/特殊=字符"          # 故意用含 + / = 的会话 id，验证安全编码
SUP = "cid主管会话"
cards = []

CR.register_replier(lambda c, t, x, **k: True)
SR._send_to_supervisor = lambda t: cards.append(t) or True
SR.generate_reply_ex = lambda user, text, ctx=None, raw=False: ("AI 草稿", "ok")
ACK._mark_read = lambda c, m: True
ACK._emotion_id = lambda e, t: ("eid", "bid")
ACK._add_text_emotion = lambda *a: True
ACK._update_text_emotion = lambda *a: True
ACK._run_cli = lambda a, timeout=15: (0, "{}")

if os.environ.get("E2E_SIMULATE_BUG") == "1":
    msgstore.record = lambda *a, **k: False
    print("⚠️  E2E_SIMULATE_BUG=1：msgstore 不落盘（期望 FAIL）\n")


def _wait(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


print("=== 1) 提问者在群里 @ 数字员工 ===")
dispatch_inbound(InboundMessage(
    user="张三", text="报销怎么走？", conv_type="2", conv_id=GRP,
    msg_id="msgASK==", kind=KIND_TEXT, extra={"at_mention": True}))
_wait(lambda: cards)

print("\n=== 2) 数字员工发出的卡片经订阅回显（出站 msgId 的唯一来源）===")
dispatch_inbound(InboundMessage(
    user="一粟", text="📋 **待审 #1**　来自：**张三**", conv_type="1", conv_id=SUP,
    msg_id="msgCARD==", kind=KIND_TEXT))

print("\n=== 3) 主管裁决 ===")
SR._send_to_supervisor = lambda t: cards.append(t) or True
dispatch_inbound(InboundMessage(
    user="boss", text="#1 改：找财务小王签字", conv_type="1", conv_id=SUP,
    msg_id="msgVERDICT==", kind=KIND_TEXT))
_wait(lambda: not SR._pending)

asked = msgstore.find(GRP, "msgASK==")
carded = msgstore.find(SUP, "msgCARD==")
fb = msgstore.feedback_of(GRP, "msgASK==")
conv_dirs = sorted(os.listdir(os.environ["AGENT_MSGSTORE_DIR"])) \
    if os.path.isdir(os.environ["AGENT_MSGSTORE_DIR"]) else []

print(f"\n落盘目录: {conv_dirs}")
print(f"提问者原话: {asked and asked.get('text')!r}")
print(f"卡片(出站): {carded and (carded.get('dir'), carded.get('text')[:18])}")
print(f"裁决反馈  : {fb and (fb.get('action'), fb.get('answer'))}")

v1 = bool(asked) and asked["text"] == "报销怎么走？" and asked["dir"] == "in"
v2 = bool(carded) and carded["dir"] == "out"          # 出站靠回显才拿得到
v3 = bool(fb) and fb["action"] == "answered" and fb["answer"] == "找财务小王签字"
v4 = all("/" not in d for d in conv_dirs) and len(conv_dirs) == 2

print("\n=== 结果 ===")
print(f"  V1 提问者原话可按 msgId 查回   : {'✅' if v1 else '❌'}（它没有任何「待审 #N」标记）")
print(f"  V2 出站消息也入库且标 dir=out  : {'✅' if v2 else '❌'}（靠订阅回显拿到 msgId）")
print(f"  V3 裁决反馈挂在提问者原话上    : {'✅' if v3 else '❌'}")
print(f"  V4 会话目录名安全编码          : {'✅' if v4 else '❌'}（conv_id 含 + / =）")

allok = v1 and v2 and v3 and v4
print("PASS" if allok else "FAIL")
sys.exit(0 if allok else 1)
