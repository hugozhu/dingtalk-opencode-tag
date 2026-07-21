"""text_reply — 普通文本消息回复能力（custom 插件）

把入站文本消息交给大脑（brain）生成回复、发回来源会话（replier）。这是最基础的
"收→大脑→发"闭环能力。

挂点：
- on_inbound(kind=text)：提交大脑生成 + 发送（防回环 + msgId 去重由 core 声明式处理）
- on_sse_event：抑制 brain 走 serve HTTP 时临时 session 冒出的 SSE 业务通知
  （brain 与 SSE 循环同进程共享 serve，否则会刷"收到新请求/会话完成"）

失败反馈（#59）：LLM 后端不可用/超时/出错时 generate_reply_ex 返回 failed，本能力发一条
兜底提示（AGENT_FALLBACK_REPLY，默认「暂时无法处理，请稍后再试」）而非静默吞掉——这样
send_reply 被调用 → dispatch_reply_sent(ok=False) 广播 → ack 回执落到「处理未完成」终态，
不会永远停在「处理中」。设 AGENT_FALLBACK_REPLY="" 可关闭兜底（回退旧的静默行为）。
empty（模型正常但没话说）仍静默，不打扰用户。

开关：CAP_TEXT_REPLY_ENABLED（默认开）。
"""

import os

from core.agent_common import log, submit_handler
from core.capabilities import Capability, register
from core.inbound import KIND_TEXT
from core.brain import generate_reply_ex, is_textreply_session, STATUS_FAILED
from core.replier import send_reply

# LLM 不可用时给用户的兜底提示（空串=不发，保持旧的静默行为）
_FALLBACK_REPLY = os.environ.get("AGENT_FALLBACK_REPLY", "⚠️ 暂时无法处理你的消息，请稍后再试。")


def _handle_text_reply(user, text, conv_type, conv_id, msg_id):
    """后台线程执行：大脑生成回复 → 发回来源会话。

    - ok：发回复。
    - failed：发兜底提示（若配置了 AGENT_FALLBACK_REPLY），触发 ack 落失败终态。
    - empty：模型正常但无话说 → 静默（不打扰用户）。
    """
    reply, status = generate_reply_ex(user, text, ctx={
        "conv_id": conv_id, "conv_type": conv_type, "msg_id": msg_id, "user": user,
    })
    if reply:
        send_reply(conv_id, conv_type, reply)
        return
    if status == STATUS_FAILED and _FALLBACK_REPLY:
        log(f"reply: 大脑失败，发兜底提示 user={user} text={text[:40]!r}")
        # outcome_ok=False：兜底提示虽投递成功，但业务是失败结局 → ack 落「处理未完成」
        send_reply(conv_id, conv_type, _FALLBACK_REPLY, outcome_ok=False)
        return
    log(f"reply: 大脑无回复(status={status}) user={user} text={text[:40]!r}")


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
