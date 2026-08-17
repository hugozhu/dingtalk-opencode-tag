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

**例外：数字员工自己问出口的问题**。它在群里发了「🔐 需要授权」或一张 Question 选项
卡之后，对方回一句「同意」是不会 @ 它的 —— 一律吞掉等于自己把答案挡在门外，审批只能
超时自动拒绝。故闸门在关上之前，先把消息递给"正等着人回话"的能力（见
_offer_to_awaiting）；没人认领才吞。

开关：CAP_GROUP_GATE_ENABLED（默认开）。设 0 = 回到"群里每条都回"，同时**失去双流
去重**（订阅了整群 + @ 时会重复回复），故只有在没订阅整群时才适合关。
"""

import os
import sys
import threading
from collections import OrderedDict

from core.capabilities import Capability, enabled_capabilities, register
from core.agent_common import log

# 会话类型："2"=群聊（core.inbound）。单聊只有一条流，不归本能力管。
_CONV_TYPE_GROUP = "2"
# msgId 记忆上限（有界 FIFO，防长跑内存增长）。与 core 的 CAP_DEDUP_MAX 同量级。
_SEEN_MAX = int(os.environ.get("GROUP_GATE_SEEN_MAX", "2048"))

# 本能力自己的名字（探测"谁在等回答"时要跳过自己）
CAPABILITY_NAME = "group_gate"

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


def _awaiting_modules(conv_id):
    """本群里正等着人回话的能力模块（授权审批 / Question 选项）。

    **数字员工自己在群里问了一句，就不能再要求对方 @ 它才算数** —— 没人会 @ 一下再回
    「同意」。这类能力的共同形状是 `_find_pending_for_conv(conv_id) -> (req_id, p)`
    （permission / question 都是），故用鸭子类型认：将来新增同类能力只要提供同名函数
    就自动被认到，不用回来改这里，也不用改 core 加 Capability 字段。

    返回**模块**而不是 Capability：调用时要走模块上的 on_inbound，这样单测 patch
    模块属性就能生效（Capability 里存的是注册那一刻的函数引用，patch 不到）。
    """
    out = []
    for cap in enabled_capabilities():
        if cap.name == CAPABILITY_NAME:
            continue                        # 别把自己算进去
        mod = sys.modules.get(getattr(getattr(cap, "on_inbound", None), "__module__", ""))
        if mod is None or not hasattr(mod, "_find_pending_for_conv"):
            continue
        try:
            req_id, _ = mod._find_pending_for_conv(conv_id)
        except Exception as e:              # 探测失败不能拖垮闸门
            log(f"group_gate: 探测 {cap.name} 待答状态出错 {e}")
            continue
        if req_id:
            out.append((cap.name, mod))
    return out


def _offer_to_awaiting(msg):
    """把消息先递给正在等回答的能力；有人认领返回 True。

    为什么是"递给"而不是"放行"：放行意味着这条消息会一路走到 text_reply —— 而
    permission 对不认识的文本返回 False（用户可能在聊别的），于是群里的闲聊会在
    审批挂起的那几十秒里被数字员工接话，正是本能力要防的事。递给它们则精确得多：
    认领了就结束，没认领仍然吞掉。
    """
    for name, mod in _awaiting_modules(msg.conv_id):
        try:
            if mod.on_inbound(msg):
                log(f"group_gate: 群里未 @ 我，但 {name} 正等回答 → 交给它处理")
                return True
        except Exception as e:
            log(f"group_gate: {name} 处理待答消息出错 {e}")
    return False


def on_inbound(msg):
    """群消息闸门。返回 True=吞掉（不再往下传），False=放行给后续能力。"""
    if str(msg.conv_type) != _CONV_TYPE_GROUP:
        return False                      # 单聊照常
    at_me = bool(msg.extra.get("at_mention"))
    if not msg.msg_id:
        # 没有 msgId 就无从去重（也就不会有双份），只按"有没有 @ 我"判
        if at_me:
            return False
        _offer_to_awaiting(msg)   # 可能是在回答我的提问；认不认领都不再往下传
        return True

    prev = _remember(msg.msg_id, at_me)
    if prev:
        return True                       # 这条已经放行过一份了 → 后到的是重复投递
    if at_me:
        return False                      # 被 @ → 放行（哪怕先前压下过没标记的那份）
    if _offer_to_awaiting(msg):
        return True                       # 是在回答数字员工的提问 → 已处理
    log(f"group_gate: 群消息未 @ 我，不处理 user={msg.user} text={(msg.text or '')[:30]!r}")
    return True


# 测试用：清空 msgId 记忆
def _reset():
    with _seen_lock:
        _seen.clear()


CAPABILITY = Capability(
    name=CAPABILITY_NAME,
    on_inbound=on_inbound,
    priority=2,              # 晚于 trace(0)/ack(1)，早于 cancel(5)/…/text_reply(100)
    default_enabled=True,
    loop_guard=True,         # 自己发的群消息不处理（与其他能力一致）
    dedup=False,             # **必须关**：去重是本能力自己的职责，要看到每一份投递
)
register(CAPABILITY)
