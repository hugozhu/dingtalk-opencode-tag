#!/usr/bin/env python3
"""端到端验证 #108 步骤 0：短号 #N 跨重启唯一。

真实事故：`_seq_counter` 是纯内存的，重启归零 → 主管会话里同时存在多张「待审 #3」。
反查卡片时匹配到**上一轮**那张同号卡片，贴表情从此静默失效（commit 531d039）。

链路全真实（仅 stub 平台发送 / LLM 草稿）：
  dispatch_inbound(群消息, @我) → group_gate → ack → supervisor_review
    → 后台出草稿 → 转交主管（卡片）→ 短号先落审核流水再发卡
  _reset()（= 模拟进程重启，内存全丢、流水还在）
  再来一条 → 短号必须是 #2，不是 #1

反证开关：E2E_SIMULATE_BUG=1 跳过 replay（回到"重启归零"的老行为），此时必须 FAIL。
"""
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "src"))

_TMP = tempfile.mkdtemp(prefix="e2e-journal-")

# 环境必须在 import 能力之前设好（开关在 import 期定型）
os.environ["CAP_SUPERVISOR_REVIEW_ENABLED"] = "1"
os.environ["AGENT_SUPERVISOR_USER_ID"] = "sup-e2e"
os.environ["AGENT_SUPERVISOR_NAME"] = "boss"
os.environ["AGENT_SELF_NAMES"] = "一粟"
os.environ["ACK_PROGRESS_INTERVAL"] = "0"
os.environ["SUPERVISOR_REACTION_POLL"] = "0"        # 本测不关心贴表情
os.environ["SUPERVISOR_REVIEW_TIMEOUT"] = "0"       # 不起超时定时器
os.environ["SUPERVISOR_REVIEW_JOURNAL"] = os.path.join(_TMP, "reviews.jsonl")

import custom.capabilities                          # noqa: E402  注册全部能力
import custom.capabilities.ack as ACK               # noqa: E402
import custom.capabilities.supervisor_review as SR  # noqa: E402
import core.replier as CR                           # noqa: E402
from core.capabilities import dispatch_inbound      # noqa: E402
from core.inbound import InboundMessage, KIND_TEXT  # noqa: E402

cards = []


def fake_card(text):
    cards.append(text)
    print(f"  [→主管] {text.splitlines()[0][:52]!r}")
    return True


CR.register_replier(lambda c, t, x, **k: True)
SR._send_to_supervisor = fake_card
SR._locate_card_msg_id = lambda seq: f"CARD{seq}"
SR.generate_reply_ex = lambda user, text, ctx=None, raw=False: ("AI 草稿", "ok")
ACK._mark_read = lambda c, m: True
ACK._emotion_id = lambda e, t: ("eid", "bid")
ACK._add_text_emotion = lambda *a: True
ACK._update_text_emotion = lambda *a: True
ACK._run_cli = lambda a, timeout=15: (0, "{}")

# 反证：跳过 replay = 回到"重启后短号归零"的老行为
if os.environ.get("E2E_SIMULATE_BUG") == "1":
    SR._ensure_replayed = lambda path=None: None
    print("⚠️  E2E_SIMULATE_BUG=1：跳过流水重建，模拟重启归零（期望 FAIL）\n")


def _wait(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def ask(msg_id):
    before = len(cards)
    dispatch_inbound(InboundMessage(
        user="boss", text="@一粟 报销怎么走", conv_type="2", conv_id="e2e-grp",
        msg_id=msg_id, kind=KIND_TEXT, extra={"at_mention": True}))
    _wait(lambda: len(cards) > before)


try:
    print("=== 1) 第一次提问 ===")
    ask("m1")
    first = cards[-1] if cards else ""

    print("\n=== 2) 模拟进程重启：内存全丢，审核流水还在 ===")
    SR._reset()
    print(f"  重启后 _seq_counter={SR._seq_counter}（内存确实清空了）")

    print("\n=== 3) 重启后再提一次 ===")
    ask("m2")
    second = cards[-1] if cards else ""

    v_first = "待审 #1" in first
    v_second = "待审 #2" in second
    v_journal = os.path.exists(os.environ["SUPERVISOR_REVIEW_JOURNAL"])

    print(f"\n重启前卡片: {first.splitlines()[0][:44] if first else '（无）'}")
    print(f"重启后卡片: {second.splitlines()[0][:44] if second else '（无）'}")

    print("\n=== 结果 ===")
    print(f"  V1 首次是 #1              : {'✅' if v_first else '❌'}")
    print(f"  V2 重启后续号为 #2        : {'✅' if v_second else '❌'}（#108 核心：不再撞号）")
    print(f"  V3 流水已落盘             : {'✅' if v_journal else '❌'}")

    allok = v_first and v_second and v_journal
    print("PASS" if allok else "FAIL")
    sys.exit(0 if allok else 1)
finally:
    shutil.rmtree(_TMP, ignore_errors=True)
