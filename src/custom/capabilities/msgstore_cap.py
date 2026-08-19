"""msgstore_cap — 收到即落盘（观察者能力，只记不消费，#111）

每条入站消息写进 custom/msgstore 的本地存储，供事后按 msgId 查回（主管引用旧消息或
贴表情做裁决时用）。始终 return False 放行，不影响任何业务能力。

三个声明式开关都是**设计要害**，别顺手改：

- **priority=-10**：先于 trace(0) 和一切业务能力——收到即存，哪怕后面某个能力把消息
  消费掉或抛异常，记录也已经落盘了。
- **loop_guard=False**：数字员工发到**群里**的消息会经群订阅回显进来（群订阅是双向的），
  这是拿到"出站消息 msgId"的唯一途径——`dws chat message send` 只返回 openTaskId，
  `replier._run` 连 stdout 都丢弃，`on_reply_sent` 也只有 (conv_id, conv_type, ok)
  三个参数。开着 loop_guard 会把这些出站消息一起丢掉。

  ⚠️ **单聊出站不回显**（实测）：现网用 `user_im_message_receive_o2o_all`
  （rule_type=all，语义是"当前用户**收到**的所有单聊消息"），自己发出去的单聊不在其中。
  所以**发给主管的待审卡片不在本存储里**——它们仍靠正文里的「待审 #N」标记定位
  （见 supervisor_review._seq_of_message）。要补齐得加订
  `user_im_message_receive_o2o --user <主管>`（rule_type=singleChat，双向），
  已评估过，当前决定不做。
- **dedup=True**：群里被 @ 的消息会被投递两次（群流一份、@ 流一份，msgId 相同），
  只存一份就够。注意 core 的 dedup 是内存态，重启后重投会多存一条——查询取最新一条，
  重复记录无害。

开关：CAP_MSGSTORE_ENABLED（默认开）。
"""

from core.agent_common import log
from core.capabilities import Capability, register
from custom import connline, msgstore
from custom.identity import self_names

# bridge 追加在行尾、core 的 parse_line 不认的字段 → extra 的键名（#113）
_EXTRA_FIELDS = (("senderId", "from_id"),
                 ("quotedMsgId", "quoted_msg_id"),
                 ("quotedSenderId", "quoted_from_id"))


def _enrich(msg):
    """把 connect 行尾部的 id 字段补进 `msg.extra`。

    放这里而不是新加一个 classify_line：`classify_line` 是**首个匹配即胜出**，插一个
    通吃的 enricher 会把 supervisor_review 的引用解析整个挤掉。而本能力 priority=-10
    最先跑，在这儿富化，**后面所有能力都能看到**，顺带白送。

    已有的键不覆盖：supervisor_review.classify_line 可能已经解析过 quoted_msg_id。
    """
    line = getattr(msg, "raw_line", "") or ""
    if not line:
        return
    for key, name in _EXTRA_FIELDS:
        if msg.extra.get(name):
            continue
        v = connline.field(line, key)
        if v:
            msg.extra[name] = v


def on_inbound(msg):
    """富化 extra + 落盘，然后放行（return False，非消费型）。"""
    try:
        _enrich(msg)
    except Exception as e:      # 富化失败不影响落盘，落盘失败不影响消息处理
        log(f"msgstore: 解析行尾字段失败 {e}")
    try:
        direction = "out" if msg.user and msg.user in self_names() else "in"
        msgstore.record(msg, direction)
    except Exception as e:
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
