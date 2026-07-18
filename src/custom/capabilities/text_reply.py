"""text_reply — 普通文本消息回复能力（custom 插件）

把入站文本消息交给大脑（brain）生成回复、发回来源会话（replier）。这是最基础的
"收→大脑→发"闭环能力。

挂点：
- on_inbound(kind=text)：防回环过滤自己 → msgId 去重 → 提交大脑生成 + 发送
- on_sse_event：抑制 brain 走 serve HTTP 时临时 session 冒出的 SSE 业务通知
  （brain 与 SSE 循环同进程共享 serve，否则会刷"收到新请求/会话完成"）

开关：CAP_TEXT_REPLY_ENABLED（默认开）。
"""

import os
import threading
from collections import OrderedDict

from core.agent_common import log, submit_handler
from core.capabilities import Capability, register
from core.inbound import KIND_TEXT
from custom.brain import generate_reply, is_textreply_session
from custom.replier import send_reply

# 防回环：数字员工自己的发送名（sender 展示名）。订阅的是群消息，机器人/自己发的
# 回复也会被再次消费，必须过滤掉，否则无限自问自答。逗号分隔，可多个。
_SELF_NAMES = {
    n.strip() for n in os.environ.get("AGENT_SELF_NAMES", "数字员工,Claude Code").split(",")
    if n.strip()
}

# 文本消息去重（同一 msgId 只处理一次）—— 有界 FIFO，避免长驻内存泄漏
_reply_seen = OrderedDict()
_reply_seen_lock = threading.Lock()
_REPLY_SEEN_MAX = 2048


def _seen_before(msg_id):
    """msgId 去重：见过返回 True。空 msgId 不去重（放行）。"""
    if not msg_id:
        return False
    with _reply_seen_lock:
        if msg_id in _reply_seen:
            return True
        _reply_seen[msg_id] = None
        if len(_reply_seen) > _REPLY_SEEN_MAX:
            _reply_seen.popitem(last=False)
    return False


def _handle_text_reply(user, text, conv_type, conv_id, msg_id):
    """后台线程执行：大脑生成回复 → 发回来源会话。"""
    reply = generate_reply(user, text, ctx={
        "conv_id": conv_id, "conv_type": conv_type, "msg_id": msg_id, "user": user,
    })
    if not reply:
        log(f"reply: 大脑无回复 user={user} text={text[:40]!r}")
        return
    send_reply(conv_id, conv_type, reply)


def on_inbound(msg):
    """文本消息入站：防回环 → 去重 → 提交大脑生成回复。返回 True=已消费。"""
    # 1. 防回环：过滤数字员工自己发的消息（否则自问自答死循环）
    if msg.user in _SELF_NAMES:
        return True  # 是自己发的，消费掉不再往下传
    # 2. 去重（同一条消息只回一次）
    if _seen_before(msg.msg_id):
        return True
    # 3. 提交到有界线程池：生成回复 + 发送
    submit_handler(_handle_text_reply, msg.user, msg.text, msg.conv_type,
                   msg.conv_id, msg.msg_id)
    return True


def on_sse_event(event, port, password):
    """抑制 brain 文本回复临时 session 的 SSE 事件（不发业务通知）。

    brain 走 serve HTTP 生成回复，与 SSE 循环同进程共享 serve，其临时 session 会在
    SSE 流冒出 session.status/idle，若不拦截会触发 core 的"收到新请求/会话完成"刷屏。
    合并转发业务 session 不在登记表里，返回 False 走默认转发。
    """
    sid = (event.get("properties", {}) or {}).get("sessionID", "") or ""
    if is_textreply_session(sid):
        return True  # brain 文本回复 session，吞掉
    return False


CAPABILITY = Capability(
    name="text_reply",
    on_inbound=on_inbound,
    handles_kinds={KIND_TEXT},
    on_sse_event=on_sse_event,
    priority=100,          # catch-all 文本，放最后兜底
    default_enabled=True,
)
register(CAPABILITY)
