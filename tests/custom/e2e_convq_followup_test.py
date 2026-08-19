#!/usr/bin/env python3
"""端到端验证：群里先发图（**不 @**）、再 @ 追问 —— 大脑不再被识别堵住，而是自己去取。

复现 2026-08-18 14:27 的真实事故并验证新设计：

    14:27:45  图到达（未 @）→ group_gate 吞掉，msgstore 落盘
    14:28:12  @ 提问「这个图你统计下」→ 旧实现在这里**堵 20 秒**等识别
    14:28:32  预算用尽 → prompt 写「还在识别中」，是个死胡同
    14:28:36  识别完成 —— 晚了 4 秒

当时答案还是对的，但**不是因为设计对**：agent 自己去 cat 了 knowledge/ 下的 jsonl。
新设计把这个本能改道到 convq，并且**不再等**。

链路全真实（仅 stub 平台发送 / 下载 / 视觉 / LLM 草稿）。七条断言：
  V1 未 @ 的图被吞但已入库
  V2 追问**不阻塞**（旧实现在这里要 20 秒）
  V3 prompt 给的是**可运行的命令**，不是死胡同
  V4 大脑跑那条命令能拿到 OCR 全文
  V5 全程**只下载一次**（daemon 侧发起 + CLI 侧 join 同一把锁）
  V6 convq recent 把 OCR 挂在图片消息下，且自己发的渲染成「我」
  V7 查不到别的会话

两个反证：
  E2E_SIMULATE_BUG=1  回到旧行为（等 20s + 不给命令）→ V2/V3 必须挂
  E2E_SIMULATE_BUG=2  关掉跨进程锁 → V5 必须挂（下载 2 次）
"""
import atexit
import io
import os
import shutil
import sys
import tempfile
import threading
import time
from contextlib import redirect_stdout

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "src"))

_TMP = tempfile.mkdtemp(prefix="e2e-convq-")
atexit.register(shutil.rmtree, _TMP, True)

BUG = os.environ.get("E2E_SIMULATE_BUG", "")

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
if BUG == "1":
    os.environ["AGENT_CONTEXT_WAIT_SEC"] = "20"     # 旧行为：堵着等
    print("⚠️  E2E_SIMULATE_BUG=1：回到「等 20 秒 + 不给命令」（期望 FAIL）\n")

import custom.capabilities                          # noqa: E402  注册全部能力
import custom.capabilities.ack as ACK               # noqa: E402
import custom.capabilities.image as IMG             # noqa: E402
import custom.capabilities.supervisor_review as SR  # noqa: E402
import core.replier as CR                           # noqa: E402
from custom import context, convq, mediadesc, msgstore   # noqa: E402
from core.capabilities import dispatch_inbound      # noqa: E402
from core.inbound import InboundMessage, KIND_IMAGE, KIND_TEXT  # noqa: E402

GRP = "cid群+带/特殊=字符"
OTHER = "cid别的群=="
OCR = "8 月考勤表：张三 迟到 2 次、早退 1 次；李四 全勤"
cards, prompts, downloads = [], [], []
vision_gate = threading.Event()

CR.register_replier(lambda c, t, x, **k: True)
SR._send_to_supervisor = lambda t: cards.append(t) or True
ACK._mark_read = lambda c, m: True
ACK._emotion_id = lambda e, t: ("eid", "bid")
ACK._add_text_emotion = lambda *a: True
ACK._update_text_emotion = lambda *a: True
ACK._run_cli = lambda a, timeout=15: (0, "{}")
SR.generate_reply_ex = lambda user, text, ctx=None, raw=False: (
    prompts.append((text, raw)) or ("AI 草稿", "ok"))


def _download(media_id, msg_id, conv_id):
    """慢下载：卡在闸门上，制造「识别没赶上这一轮」的确定性条件。"""
    downloads.append(msg_id)
    vision_gate.wait(20)
    return "/tmp/fake.png", None


IMG._download_image = _download
IMG._recognize = lambda path, tmp_dir=None: OCR

if BUG == "1":
    convq.cmd_hint = lambda *a, **k: ""          # 死胡同，没有出口
if BUG == "2":
    mediadesc._acquire = lambda *a, **k: True    # 关掉跨进程锁
    print("⚠️  E2E_SIMULATE_BUG=2：跨进程锁已关闭（期望 FAIL）\n")


def _wait(cond, timeout=8.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def _cli(*argv):
    out = io.StringIO()
    with redirect_stdout(out):
        rc = convq.main(list(argv))
    return rc, out.getvalue()


print("=== 1) 群里有人发一张图，**没有 @ 数字员工** ===")
dispatch_inbound(InboundMessage(
    user="张三", text="[图片消息](mediaId=$iwEcAqNqcGc)", conv_type="2", conv_id=GRP,
    msg_id="msgIMG==", kind=KIND_IMAGE))
msgstore.record(InboundMessage(user="别人", text="别的群的秘密", conv_type="2",
                               conv_id=OTHER, msg_id="msgOTHER==", kind=KIND_TEXT), "in")
stored = msgstore.find(GRP, "msgIMG==")
print(f"    被吞掉但已落盘: {bool(stored)}")

print("\n=== 2) 一秒后 @ 提问 —— 视觉还卡着，这一轮绝不能等它 ===")
time.sleep(0.2)
t0 = time.monotonic()
dispatch_inbound(InboundMessage(
    user="张三", text="@一粟 这个图你统计下", conv_type="2", conv_id=GRP,
    msg_id="msgASK==", kind=KIND_TEXT, extra={"at_mention": True}))
_wait(lambda: prompts)
elapsed = time.monotonic() - t0
prompt, raw = prompts[0] if prompts else ("", False)
print(f"    从提问到草稿耗时: {elapsed:.2f}s")
print(f"    prompt:\n---\n{prompt}\n---")

print("=== 3) 大脑照 prompt 里的命令去取图片内容 ===")
# **必须在 daemon 那次识别还在途时进场**，否则 CLI 直接命中缓存，跨进程锁根本没被
# 考验到（反证 2 就会假通过）。所以闸门由后台定时器在 CLI 已经进入 _run 之后才放开。
threading.Timer(0.6, vision_gate.set).start()
rc, got = _cli("image", "msgIMG==", "--conv", GRP, "--wait", "10")
print(f"    convq image → rc={rc} {got.strip()[:40]}…")

print("=== 4) 主管放行后，答复发到群里并经订阅回显（群订阅是双向的）===")
dispatch_inbound(InboundMessage(
    user="一粟", text="张三 8 月迟到 2 次", conv_type="2", conv_id=GRP,
    msg_id="msgANS==", kind=KIND_TEXT))
_wait(lambda: msgstore.find(GRP, "msgANS=="))

rc2, recent = _cli("recent", "--conv", GRP)

v1 = bool(stored) and stored["dir"] == "in"
v2 = elapsed < 2.0
v3 = bool(prompt) and convq.CLI_PATH in prompt and "msgIMG==" in prompt and raw is True
v4 = rc == convq.EXIT_OK and OCR in got
v5 = len(downloads) == 1
v6 = OCR[:10] in recent and "我:" in recent
v7 = "别的群的秘密" not in recent

print("\n=== 结果 ===")
print(f"  V1 未 @ 的图被吞但已入库        : {'✅' if v1 else '❌'}")
print(f"  V2 追问不被识别阻塞({elapsed:.1f}s)  : {'✅' if v2 else '❌'}（旧实现在这要 20 秒）")
print(f"  V3 prompt 给的是可运行的命令    : {'✅' if v3 else '❌'}（死胡同 + 一扇标好的门）")
print(f"  V4 大脑跑那条命令拿到 OCR       : {'✅' if v4 else '❌'}")
print(f"  V5 全程只下载一次               : {'✅' if v5 else '❌'}（下载 {len(downloads)} 次，跨进程锁）")
print(f"  V6 recent 把 OCR 挂在图片下     : {'✅' if v6 else '❌'}（且自己发的渲染成「我」）")
print(f"  V7 查不到别的会话               : {'✅' if v7 else '❌'}")

allok = v1 and v2 and v3 and v4 and v5 and v6 and v7
print("PASS" if allok else "FAIL")
sys.exit(0 if allok else 1)
