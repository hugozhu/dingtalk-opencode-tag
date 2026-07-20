"""text_reply — 普通文本消息回复能力（custom 插件）

把入站文本消息交给大脑（brain）生成回复、发回来源会话（replier）。这是最基础的
"收→大脑→发"闭环能力。

挂点：
- on_inbound(kind=text)：提交大脑生成 + 发送（防回环 + msgId 去重由 core 声明式处理）
- on_sse_event：抑制 brain 走 serve HTTP 时临时 session 冒出的 SSE 业务通知
  （brain 与 SSE 循环同进程共享 serve，否则会刷"收到新请求/会话完成"）

开关：CAP_TEXT_REPLY_ENABLED（默认开）。
"""

from core.agent_common import log, submit_handler
from core.capabilities import Capability, register
from core.inbound import KIND_TEXT
from core.brain import generate_reply, is_textreply_session
from core.replier import send_reply


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
    """文本消息入站：提交大脑生成回复。返回 True=已消费。

    防回环（自己发的）+ msgId 去重由 core dispatch_inbound 依 loop_guard/dedup 声明处理，
    命中的消息压根不会进到这里。
    """
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
    loop_guard=True,       # core 统一防回环
    dedup=True,            # core 统一 msgId 去重
)
register(CAPABILITY)
