"""msgstore_cap — 收到即落盘（观察者能力，只记不消费，#111）

每条入站消息写进 custom/msgstore 的本地存储，供事后按 msgId 查回（主管引用旧消息或
贴表情做裁决时用）。始终 return False 放行，不影响任何业务能力。

三个声明式开关都是**设计要害**，别顺手改：

- **priority=-10**：先于 trace(0) 和一切业务能力——收到即存，哪怕后面某个能力把消息
  消费掉或抛异常，记录也已经落盘了。
- **loop_guard=False**：**必须**。数字员工自己发出的消息会经订阅回显进来（实测：单聊
  62 条、群聊 4 条），这是拿到"出站消息 msgId"的**唯一**途径——`dws chat message send`
  只返回 openTaskId，`replier._run` 连 stdout 都丢弃，`on_reply_sent` 也只有
  (conv_id, conv_type, ok) 三个参数。开着 loop_guard 就等于把出站消息全丢了，而主管
  引用的往往正是数字员工自己发的卡片/回执。
  （注：AGENTS.md 旧版坑#2 说"订阅不回显自己发的消息"，那是旧 dws dev connect 模式的
  结论，当前 event-consume 链路下不成立。）
- **dedup=True**：群里被 @ 的消息会被投递两次（群流一份、@ 流一份，msgId 相同），
  只存一份就够。注意 core 的 dedup 是内存态，重启后重投会多存一条——查询取最新一条，
  重复记录无害。

开关：CAP_MSGSTORE_ENABLED（默认开）。
"""

from core.agent_common import log
from core.capabilities import Capability, register
from custom import msgstore
from custom.identity import self_names


def on_inbound(msg):
    """把消息落盘后放行（return False，非消费型）。"""
    try:
        direction = "out" if msg.user and msg.user in self_names() else "in"
        msgstore.record(msg, direction)
    except Exception as e:      # 落盘失败绝不能影响消息处理
        log(f"msgstore: 记录失败 {e}")
    return False


CAPABILITY = Capability(
    name="msgstore",
    on_inbound=on_inbound,
    handles_kinds=set(),   # 所有类型都记（图片/文件/表情事件同样要可查）
    priority=-10,          # 最先跑：收到即存，先于 trace(0)/ack(1)/业务能力
    default_enabled=True,
    dedup=True,            # 双流投递只存一份（msgId 相同）
    loop_guard=False,      # **必须关**：自发消息的回显是出站 msgId 的唯一来源
)
register(CAPABILITY)
