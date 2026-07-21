#!/usr/bin/env python3
"""端到端验证 #59：LLM 不可用时用户收到兜底提示 + ack 落失败终态（不再静默吞消息）。

链路（全真实分发，仅 stub 平台发送 + DingTalk 表情 CLI）：
  dispatch_inbound(单聊消息)
    → ack.on_inbound：mark-read + 贴「处理中」文字表情（priority=1，返回 False 继续分发）
    → text_reply.on_inbound：submit_handler 后台线程 → generate_reply_ex
        （opencode 后端，serve 凭据强制缺失 + CLI 强制失败 → status=failed）
    → 发兜底提示 AGENT_FALLBACK_REPLY
    → send_reply → dispatch_reply_sent(conv_id, conv_type, ok=False)
    → ack.on_reply_sent：唤醒 worker → _finalize(ok=False) → 贴「处理未完成」终态表情

对照旧行为：failed 时 text_reply 直接 return，send_reply 不触发，ack 永远停在「处理中」。
"""
import os
import sys
import time
import threading
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "src"))

os.environ["AGENT_BRAIN"] = "opencode"
os.environ["AGENT_FALLBACK_REPLY"] = "⚠️ 暂时无法处理你的消息，请稍后再试。"
os.environ["ACK_STAGES"] = "0:稍等:已收到，正在处理…"   # 立即贴处理中
os.environ["AGENT_OPENCODE_TIMEOUT"] = "5"

import custom.capabilities            # noqa: 注册全部能力
import custom.brain as B
import custom.capabilities.ack as ACK
from core.capabilities import dispatch_inbound
from core.replier import send_reply as core_send_reply
import core.replier as CR
from core.inbound import InboundMessage, KIND_TEXT

CONV, MSG = "e2e-fallback-conv", "msg-fallback-1"
emotions = []          # ack 贴的文字表情时间线 (op, emoji, text)
sent = []              # 平台实际“发出”的回复

def fake_send_impl(conv_id, conv_type, text, *, at_user_id=None):
    sent.append(text)
    print(f"  [send→用户] {text!r}")
    return True        # 平台发送成功；ok 仍由 text_reply 是否调用它决定

def fake_add(conv_id, msg_id, emoji, text):
    emotions.append(("add", emoji, text))
    print(f"  [ack表情+] {emoji}｜{text}")
    return True

def fake_remove(conv_id, msg_id, emoji, text):
    emotions.append(("remove", emoji, text))
    return True

CR.register_replier(fake_send_impl)     # 真 send_reply 协议，仅换平台实现
ACK._add_text_emotion = fake_add
ACK._remove_text_emotion = fake_remove
ACK._mark_read = lambda conv_id, msg_id: True
ACK._emotion_id = lambda emoji, text: ("eid", "bid")

# 强制 LLM 后端彻底不可用：serve 凭据缺失 + CLI 子进程失败
cli_fail = MagicMock(returncode=1, stdout="", stderr="model gateway down")

print(f"兜底提示={B.__dict__.get('_SESSION_REUSE')!r} fallback 已配置")
print("模拟：serve 凭据缺失 + opencode CLI rc!=0（LLM 彻底不可用）")

msg = InboundMessage(user="tester", text="帮我查下天气", conv_type="1",
                     conv_id=CONV, msg_id=MSG, kind=KIND_TEXT)

with patch.object(B, "find_serve_credentials", return_value=(None, None, None)), \
     patch("subprocess.run", return_value=cli_fail):
    consumed = dispatch_inbound(msg)
    # 等后台 worker：生成失败 → 发兜底 → ack 收尾
    for _ in range(60):
        if sent and any(op == "add" and "未完成" in t or "疑问" == e
                        for op, e, t in emotions):
            break
        time.sleep(0.1)
    time.sleep(0.5)

print(f"\ndispatch_inbound consumed={consumed}")
print(f"表情时间线: {[(e,t) for _,e,t in emotions]}")
print(f"发给用户: {sent}")

# 断言
ok_processing = any(op == "add" and "处理" in t for op, e, t in emotions)
ok_fallback = any("暂时无法处理" in t for t in sent)
ok_terminal = any(op == "add" and (e == "疑问" or "未完成" in t) for op, e, t in emotions)
print("\n=== 结果 ===")
print(f"  V1 先贴「处理中」表情    : {'✅' if ok_processing else '❌'}")
print(f"  V2 LLM 失败发兜底提示    : {'✅' if ok_fallback else '❌'}（不再静默吞消息）")
print(f"  V3 ack 落「处理未完成」终态: {'✅' if ok_terminal else '❌'}（不再永远停在处理中）")
allok = ok_processing and ok_fallback and ok_terminal
print("PASS" if allok else "FAIL")
sys.exit(0 if allok else 1)
