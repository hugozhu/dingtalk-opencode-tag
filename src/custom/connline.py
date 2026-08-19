"""connline — connect 行**尾部字段**的解析（custom 层）

bridge 把消息渲染成单行 connect 日志：

    [connect] 收到 @可菡: 明天请个假 (convType=1 convId=cid… msgId=msg… senderId=…)

core 的 `inbound.parse_line` 只认 `atMention=1` 一个尾部标记。要多认几个键（senderId /
quotedMsgId / quotedSenderId）本来得改 core —— 这里提供共享的尾部解析，让 custom 侧的
能力各取所需，零 core 改动。

**为什么必须先切出"尾部"再抽字段，不能对整行 search**：正文和尾部在同一行里，用户打一句
`senderId=我是别人` 或 `quotedSeq=9` 就能伪造出一个标记。取**最后一个** `(convType=`
之后的部分才是 bridge 真正拼的尾巴（正文里也可能出现这串字面量，所以取最后一个）。

这套切法原本写在 `supervisor_review._line_tail` 里，现在 msgstore 也要用，抽出来共享
—— 复制第二份的话，哪天防伪造的逻辑改了必然只改一处。
"""

import re

_MARK = "(convType="


def tail(line):
    """connect 行的尾部字段段；不是 connect 行返回 ""。"""
    i = (line or "").rfind(_MARK)
    return line[i:] if i >= 0 else ""


def field(line, key):
    """从尾部取一个字段值（`key=值`，止于空白或 `)`）；没有返回 ""。"""
    m = re.search(rf"\b{re.escape(key)}=([^\s)]+)", tail(line))
    return m.group(1) if m else ""
