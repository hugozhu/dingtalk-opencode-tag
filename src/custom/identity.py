"""identity.py — 主管身份判定（custom 层的单一真相源）

谁是「主管」这件事被多个能力用到：
- supervisor_review：判定入站消息是不是主管的裁决；把待审卡片发给主管
- ack：只对主管的消息贴状态表情（#106）

放这里而不是各自实现，是为了避免能力之间互相 import —— ack 默认**开**、
supervisor_review 默认**关**，让常开的能力依赖一个可选能力，等于把可选变成硬依赖。
也不放 core/agent_common：主管身份是钉钉组织概念（corp userId / 花名），属 @custom。

**身份判定只能靠显示名**：dws_event_bridge 的 connect-log 行只带 sender 显示名，
不带 userId（见 dws_event_bridge._to_connect_line）。故入站比对用
AGENT_SUPERVISOR_NAME + AGENT_SUPERVISOR_ALIASES（同一人可能显示为花名或姓名，
如 "hugozhu"/"朱鸿"，都列上）。**主动发消息**给主管才用得上 userId
（AGENT_SUPERVISOR_USER_ID，走 `dws chat message send --user`）。

env 在**调用时**读取、不在 import 期定型：测试可直接改 os.environ，不必 patch 模块属性。
"""

import os


def supervisor_names():
    """主管显示名集合（判定入站发送人用）。未配置返回空集。"""
    names = set()
    for key in ("AGENT_SUPERVISOR_NAME", "AGENT_SUPERVISOR_ALIASES"):
        for n in os.environ.get(key, "").split(","):
            n = n.strip()
            if n:
                names.add(n)
    return names


def supervisor_id():
    """主管 userId（主动发消息给主管用）。未配置返回 ""。"""
    return os.environ.get("AGENT_SUPERVISOR_USER_ID", "").strip()


def has_supervisor():
    """是否配了主管（名字或 userId 任一即算）。

    调用方据此决定「没配主管」时退化成什么行为 —— 例如 ack 在没配主管时
    仍对所有人贴表情（保持原行为），而不是一个都不贴。
    """
    return bool(supervisor_names() or supervisor_id())


def is_supervisor(user):
    """该发送人显示名是主管吗？未配主管时恒为 False（调用方自行兜底）。"""
    return bool(user) and user in supervisor_names()
