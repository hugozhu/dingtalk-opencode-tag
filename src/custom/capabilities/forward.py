"""forward — 合并转发（chatRecord）消息能力（custom 插件）

钉钉「合并转发」消息不是标准 "收到 @user: text" 格式，而是含 msgtype="chatRecord"
的业务行（可能跨行）。本能力用 handler 的跨行状态机识别，产出 InboundMessage
(kind=forward)，再反查完整消息体 → 拆分 → 注入 agent → 回复转发。

挂点：
- classify_line：识别 chatRecord 业务行（含 msgId 去重、跨行状态），命中产出
  InboundMessage(kind=forward, msg_id=..., raw_line=...)
- on_inbound(kind=forward)：提交 handler.handle_message 反查 + 处理

开关：CAP_FORWARD_ENABLED（默认开）。

注：本 PR 只把现有 handler 骨架接成插件；完整反查/拆图文的实现见 issue #26。
"""

from core.agent_common import submit_handler
from core.capabilities import Capability, register
from core.inbound import InboundMessage, KIND_FORWARD
from custom.handler import handle_message, match_business_line


def classify_line(line):
    """识别合并转发业务行。命中返回 InboundMessage(kind=forward)，否则 None。

    match_business_line 内含跨行状态机 + msgId 去重，只由 log-tail 单线程调用。
    """
    m = match_business_line(line)
    if not m:
        return None
    mid, convs = m
    return InboundMessage(
        kind=KIND_FORWARD,
        msg_id=mid,
        raw_line=line,
        extra={"convs": convs},
    )


def on_inbound(msg):
    """合并转发入站：提交 handler 反查完整消息体并处理。返回 True=已消费。"""
    submit_handler(handle_message, msg.msg_id, msg.extra.get("convs"))
    return True


CAPABILITY = Capability(
    name="forward",
    classify_line=classify_line,
    on_inbound=on_inbound,
    handles_kinds={KIND_FORWARD},
    priority=50,           # 业务消息，先于 catch-all 文本
    default_enabled=True,
)
register(CAPABILITY)
