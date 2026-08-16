"""group_gate — 群消息闸门：只在被 @ 时开口，并合并双流重复投递

订阅了整个群（DWS_EVENT_GROUP）之后，群里**每一条**消息都会进到能力链，而
text_reply 是 catch-all —— 数字员工会插嘴群里所有的人际闲聊。真人同事不会这样。
本能力把"看得见"和"要开口"拆开：群消息照常入站（trace 记账、ack 标已读），
但**没 @ 我的不往下传**，不进大脑、不回复、也不刷主管审核卡片。

两件事都由它做，因为两件事共用同一份"这条消息我处理过没有"的记忆：

1. **只回被 @ 的**（群聊；单聊不管）。
2. **双流去重**（订阅了整群 + DWS_EVENT_AT 时必需）。同一条 @ 消息会被投两次：
   一次走群流（user_im_message_receive_group），一次走 @ 流
   （user_im_message_receive_at）。**只有 @ 流那份带 atMention 标记** —— bridge
   拿事件类型当唯一信号，群流的 payload 里没有 atUsers（见 dws_event_bridge
   ._to_connect_line）。core 的 dedup 是**按能力**记的，配合"首个返回 True 就短路"
   会漏：第一份被 supervisor_review 消费掉，第二份到 text_reply 时它还没见过这个
   msgId → 直接回一条，绕过审核。所以去重必须在链条**最前面**做一次，只放行一份。

判定用一张 msgId → 是否已放行 的有界表，而不是简单的"见过就丢"：两份的到达顺序
不定，先到的可能是**没有标记**的群流那份。若见过就丢，被 @ 的消息反而永远没人答。
故：先到的没标记 → 记为"已压下"但仍等后面那份；带标记的那份到了 → 放行并记牢；
之后再来的同 msgId 一律吞掉。

挂点顺序：priority=2 —— 晚于 trace(0)/ack(1)，让群消息照常记账和标已读（真人也会
读群消息）；早于 cancel(5)/stats(10)/permission(15)/question(20)/supervisor_review(30)/
text_reply(100)，在任何"要开口"或"认命令"的能力之前截住（群里没 @ 我时打 /cancel、
/stats 也不该被当成对数字员工说话）。handles_kinds 留空 = 管所有类型：群里没 @ 我的
图片/文件/合并转发同样不该触发处理。

开关：CAP_GROUP_GATE_ENABLED（默认开）。设 0 = 回到"群里每条都回"，同时**失去双流
去重**（订阅了整群 + @ 时会重复回复），故只有在没订阅整群时才适合关。
"""

import os
import threading
from collections import OrderedDict

from core.capabilities import Capability, register
from core.agent_common import log

# 会话类型："2"=群聊（core.inbound）。单聊只有一条流，不归本能力管。
_CONV_TYPE_GROUP = "2"
# msgId 记忆上限（有界 FIFO，防长跑内存增长）。与 core 的 CAP_DEDUP_MAX 同量级。
_SEEN_MAX = int(os.environ.get("GROUP_GATE_SEEN_MAX", "2048"))

# msgId -> True(已放行) / False(已压下，等带 @ 标记的那份)
_seen = OrderedDict()
_seen_lock = threading.Lock()


def _remember(msg_id, passed):
    """记住这条 msgId 的处置，返回之前的处置（None=没见过）。超上限丢最旧的。"""
    with _seen_lock:
        prev = _seen.get(msg_id)
        _seen[msg_id] = passed
        _seen.move_to_end(msg_id)
        while len(_seen) > _SEEN_MAX:
            _seen.popitem(last=False)
        return prev


def on_inbound(msg):
    """群消息闸门。返回 True=吞掉（不再往下传），False=放行给后续能力。"""
    if str(msg.conv_type) != _CONV_TYPE_GROUP:
        return False                      # 单聊照常
    at_me = bool(msg.extra.get("at_mention"))
    if not msg.msg_id:
        # 没有 msgId 就无从去重（也就不会有双份），只按"有没有 @ 我"判
        return not at_me

    prev = _remember(msg.msg_id, at_me)
    if prev:
        return True                       # 这条已经放行过一份了 → 后到的是重复投递
    if at_me:
        return False                      # 被 @ → 放行（哪怕先前压下过没标记的那份）
    log(f"group_gate: 群消息未 @ 我，不处理 user={msg.user} text={(msg.text or '')[:30]!r}")
    return True


# 测试用：清空 msgId 记忆
def _reset():
    with _seen_lock:
        _seen.clear()


CAPABILITY = Capability(
    name="group_gate",
    on_inbound=on_inbound,
    priority=2,              # 晚于 trace(0)/ack(1)，早于 cancel(5)/…/text_reply(100)
    default_enabled=True,
    loop_guard=True,         # 自己发的群消息不处理（与其他能力一致）
    dedup=False,             # **必须关**：去重是本能力自己的职责，要看到每一份投递
)
register(CAPABILITY)
