"""trace — 入站消息埋点能力（观察者，只记不消费）

每收到一条 consume 消息（log-tail 解析出的 InboundMessage），在 monitor.log 打一行
带 msgId 和 kind（类型）的记录，便于按 msgId 排查「收→处理→发」链路。始终 return False
放行，不消费任何消息，不影响后续业务能力。

字段：
  msgId=<消息ID>  kind=<text|image|file|forward|reboot|unknown>  user=<发送人>
  conv=<会话类型 1单聊/2群聊>:<openConversationId 前缀>

设计：
  - priority=0：跑在所有业务能力（ack=1 起）之前，保证「收到即记」先于任何处理/回复。
  - handles_kinds 为空集 = 关心所有 kind。
  - loop_guard=False：数字员工自己发的也记（排查回环时有用）。
  - dedup=False：断线重连重投同一 msgId 也各记一行，如实反映「每次收到」。

开关：CAP_TRACE_ENABLED（默认开）。
"""

from core.agent_common import log
from core.capabilities import Capability, register


def on_inbound(msg):
    """记录入站消息的 id/type 等字段到 monitor.log，始终放行（return False）。"""
    kind = msg.kind
    # 合并转发在 event-consume 下以 kind=text 到达（无 msgtype 字段可判），这里用 forward
    # 能力的廉价摘要预筛给个 suspect-forward 标记——权威判定仍由 forward 反查 forwardMessages 做。
    if kind == "text":
        try:
            from custom.capabilities.forward import _looks_like_forward
            if _looks_like_forward(msg.text):
                kind = "text(suspect-forward)"
        except Exception:
            pass
    conv = f"{msg.conv_type or '?'}:{(msg.conv_id or '')[:16]}"
    log(f"inbound: msgId={msg.msg_id or '-'} kind={kind} "
        f"user={msg.user or '-'} conv={conv}")
    return False  # 只观察，不消费——放行给后续业务能力


CAPABILITY = Capability(
    name="trace",
    on_inbound=on_inbound,
    handles_kinds=set(),   # 空集 = 所有 kind 都记
    priority=0,            # 最先跑，收到即记，先于 ack(1)/业务能力
    default_enabled=True,
    dedup=False,           # 每次收到都记（含重投），不去重
    loop_guard=False,      # 自己发的也记
)
register(CAPABILITY)
