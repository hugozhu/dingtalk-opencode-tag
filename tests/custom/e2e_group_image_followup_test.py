#!/usr/bin/env python3
"""端到端验证 #112 步骤 2：群里先发图（**不 @**）、再 @ 追问，数字员工看得到那张图。

修之前的真实症状（monitor.log 原文）：

    [group_gate] 群消息未 @ 我，不处理 text='[图片消息](mediaId=$iwEc…'
    → 紧接着的「这个怎么理解」进了大脑，而大脑对那张图一无所知

链路全真实（仅 stub 平台发送 / 视觉模型 / LLM 草稿）：
  dispatch_inbound(群里未 @ 的图片)  → msgstore 落盘（priority=-10）→ group_gate 吞掉
  dispatch_inbound(群里 @ 的文本追问) → supervisor_review → context.build 回看
                                     → mediadesc 单飞识别 → 描述进 prompt

四条断言：图被吞但入了库 / 描述进了 prompt 且是 raw / 措辞是推测语气 /
**同一张图只下载一次**（追问那次复用了缓存，不是重下）。

反证开关：E2E_SIMULATE_BUG=1 让 recent_media 恒返回 []（= 修复前的行为），必须 FAIL。
"""
import atexit
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "src"))

_TMP = tempfile.mkdtemp(prefix="e2e-imgfollow-")
atexit.register(shutil.rmtree, _TMP, True)

os.environ["CAP_SUPERVISOR_REVIEW_ENABLED"] = "1"
os.environ["CAP_GROUP_GATE_ENABLED"] = "1"
os.environ["AGENT_SUPERVISOR_USER_ID"] = "sup-e2e"
os.environ["AGENT_SUPERVISOR_NAME"] = "boss"
os.environ["AGENT_SELF_NAMES"] = "一粟"
os.environ["ACK_PROGRESS_INTERVAL"] = "0"
os.environ["SUPERVISOR_REVIEW_TIMEOUT"] = "0"
os.environ["SUPERVISOR_REVIEW_JOURNAL"] = os.path.join(_TMP, "reviews.jsonl")
os.environ["AGENT_MSGSTORE_DIR"] = os.path.join(_TMP, "messages")
os.environ["AGENT_KNOWLEDGE_FILE"] = os.path.join(_TMP, "qa.jsonl")

import custom.capabilities                          # noqa: E402  注册全部能力
import custom.capabilities.ack as ACK               # noqa: E402
import custom.capabilities.image as IMG             # noqa: E402
import custom.capabilities.supervisor_review as SR  # noqa: E402
import core.replier as CR                           # noqa: E402
from custom import msgstore                         # noqa: E402
from core.capabilities import dispatch_inbound      # noqa: E402
from core.inbound import InboundMessage, KIND_IMAGE, KIND_TEXT  # noqa: E402

GRP = "cid群+带/特殊=字符"
cards = []
prompts = []
downloads = []

CR.register_replier(lambda c, t, x, **k: True)
SR._send_to_supervisor = lambda t: cards.append(t) or True
ACK._mark_read = lambda c, m: True
ACK._emotion_id = lambda e, t: ("eid", "bid")
ACK._add_text_emotion = lambda *a: True
ACK._update_text_emotion = lambda *a: True
ACK._run_cli = lambda a, timeout=15: (0, "{}")


def _fake_draft(user, text, ctx=None, raw=False):
    prompts.append((text, raw))
    return "AI 草稿", "ok"


SR.generate_reply_ex = _fake_draft
# 视觉链路 stub 到最底层：下载和识别各记一次，**下载次数**就是"有没有重复识别"的证据
IMG._download_image = lambda mid, msg_id, conv: (downloads.append(msg_id),
                                                 ("/tmp/fake.png", None))[1]
IMG._recognize = lambda path, tmp_dir=None: "图中是一张 8 月考勤表，张三有 2 次迟到"

if os.environ.get("E2E_SIMULATE_BUG") == "1":
    msgstore.recent_media = lambda *a, **k: []      # = 修复前：追问时看不到刚才的图
    print("⚠️  E2E_SIMULATE_BUG=1：回看被禁用（期望 FAIL）\n")


def _wait(cond, timeout=8.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


print("=== 1) 群里有人发一张图，**没有 @ 数字员工** ===")
consumed_img = dispatch_inbound(InboundMessage(
    user="张三", text="[图片消息](mediaId=$iwEcAqNqcGc)", conv_type="2", conv_id=GRP,
    msg_id="msgIMG==", kind=KIND_IMAGE))
stored = msgstore.find(GRP, "msgIMG==")
print(f"    被能力消费: {consumed_img}（group_gate 吞掉）")
print(f"    但已落盘  : {bool(stored)}")

print("\n=== 2) 一秒后，同一个人 @ 数字员工追问 ===")
time.sleep(0.2)
dispatch_inbound(InboundMessage(
    user="张三", text="@一粟 这个怎么理解？", conv_type="2", conv_id=GRP,
    msg_id="msgASK==", kind=KIND_TEXT, extra={"at_mention": True}))
_wait(lambda: prompts)

prompt, raw = prompts[0] if prompts else ("", False)
print(f"\n送进大脑的 prompt（raw={raw}）:\n---\n{prompt}\n---")
print(f"下载次数: {len(downloads)} {downloads}")

v1 = bool(stored) and stored["dir"] == "in"
v2 = bool(prompt) and "考勤表" in prompt and raw is True
v3 = ("可能" in prompt and "忽略" in prompt
      and "这个怎么理解" in prompt)
v4 = len(downloads) == 1

print("\n=== 3) 打开 premedia（默认关）：图一到就先识别好放着 ===")
os.environ["CAP_PREMEDIA_ENABLED"] = "1"
dispatch_inbound(InboundMessage(
    user="李四", text="[图片消息](mediaId=$iwEcBBBB)", conv_type="2", conv_id=GRP,
    msg_id="msgIMG2==", kind=KIND_IMAGE))
_wait(lambda: msgstore.description_of(GRP, "msgIMG2=="))
pre = msgstore.description_of(GRP, "msgIMG2==")
print(f"    追问之前就已有描述: {bool(pre)} by={pre and pre.get('by')}")

dl_before = len(downloads)
dispatch_inbound(InboundMessage(
    user="李四", text="@一粟 这张呢？", conv_type="2", conv_id=GRP,
    msg_id="msgASK2==", kind=KIND_TEXT, extra={"at_mention": True}))
_wait(lambda: len(prompts) > 1)
prompt2 = prompts[1][0] if len(prompts) > 1 else ""
print(f"    追问又下载了几次: {len(downloads) - dl_before}")

v5 = bool(pre) and pre.get("by") == "premedia" and pre.get("ok")
v6 = len(downloads) == dl_before and "考勤表" in prompt2

print("\n=== 结果 ===")
print(f"  V1 未 @ 的图被吞但已入库      : {'✅' if v1 else '❌'}")
print(f"  V2 追问的 prompt 带上了图片内容: {'✅' if v2 else '❌'}（raw=True，不加发言人前缀）")
print(f"  V3 措辞是推测语气且可忽略      : {'✅' if v3 else '❌'}（时间邻近≠用户在说它）")
print(f"  V4 同一张图只下载一次          : {'✅' if v4 else '❌'}（单飞缓存生效）")
print(f"  V5 premedia 在追问前就识别好了 : {'✅' if v5 else '❌'}（by=premedia）")
print(f"  V6 追问零下载、直接命中缓存    : {'✅' if v6 else '❌'}（预识别没白做）")

allok = v1 and v2 and v3 and v4 and v5 and v6
print("PASS" if allok else "FAIL")
sys.exit(0 if allok else 1)
