"""inbound.py — 入站消息的统一抽象（core 稳定层）

core 的 log-tail 把 connect 日志里的一行原始文本**解析一次**，归一成一个
`InboundMessage`，再交给能力注册表（core.capabilities）分发。好处：

- **解析集中**：kind 判定（文本/图片/合并转发/reboot）只在这里做一次，能力插件
  不用各自重复解析日志行、判前缀。
- **契约稳定**：能力拿到的是结构化对象而非原始行；core 日志格式变了只需改这里。
- **可组装**：registry 按 `kind` 路由到声明关心该 kind 的能力（见 capabilities.py）。

日志行格式（dws-connect.sh → bridge 产出）：
    [connect] 收到 @<user>: <text> (convType=<N> convId=<X> msgId=<Y>)
合并转发等业务消息是另一种格式（含 msgtype="chatRecord" 之类），文本为空、
靠 raw_line 交给 forward 能力自行反查，故 kind=forward 时 text 可能为空。
"""

import re
from dataclasses import dataclass, field

# 复用与 event_watcher 一致的解析正则（此处独立定义，避免 core 内循环 import）
_REPLY_RE = re.compile(r'\[connect\] 收到 @(.+?):\s*(.+?)\s+\(convType=(\d+)')
_CONVID_RE = re.compile(r"convId=([^\s)]+)")
_MSGID_RE = re.compile(r"msgId=([^\s)]+)")

# kind 常量（避免裸字符串到处飞）
KIND_TEXT = "text"        # 普通文本消息
KIND_IMAGE = "image"      # 图片消息（dws 把图片转发成文本 "[图片]"）
KIND_FILE = "file"        # 文件/文档消息（"[文件] <名> fileId: <id> ..."）
KIND_REBOOT = "reboot"    # /reboot 远程指令
KIND_FORWARD = "forward"  # 合并转发（chatRecord）等业务消息行
KIND_UNKNOWN = "unknown"  # 未匹配任何已知形态

# 图片占位文本（dws dev connect 把图片消息转发成这个；event-consume 下则是
# "[图片消息](mediaId=...)"，两种都识别为图片）
_IMAGE_PLACEHOLDER = "[图片]"
_IMAGE_MARKER = "[图片消息]"
# 文件消息标记（event-consume 下形如 "[文件] <名> fileId: <id> 注意：如需下载..."）
_FILE_MARKER = "[文件]"


@dataclass
class InboundMessage:
    """一条归一化的入站消息。

    Attributes:
        user: 发送者展示名（"收到 @user" 里的 user）；业务行可能为空
        text: 消息正文（已 strip）；图片是占位符、合并转发可能为空
        conv_type: 会话类型字符串（"1"=单聊 "2"=群聊），未知为 ""
        conv_id: 来源 openConversationId，未解析到为 ""
        msg_id: 消息 ID，未解析到为 ""
        kind: text|image|reboot|forward|unknown
        raw_line: 原始日志行（forward 等能力需要它自行反查）
        extra: 能力特定的附加数据（如 forward 的原始会话列表），core 不解读
    """
    user: str = ""
    text: str = ""
    conv_type: str = ""
    conv_id: str = ""
    msg_id: str = ""
    kind: str = KIND_UNKNOWN
    raw_line: str = ""
    extra: dict = field(default_factory=dict)


def _extract_ids(raw_line):
    """从原始行提取 (conv_id, msg_id)，缺失为 ""。"""
    conv_id = ""
    msg_id = ""
    m = _CONVID_RE.search(raw_line or "")
    if m:
        conv_id = m.group(1)
    m = _MSGID_RE.search(raw_line or "")
    if m:
        msg_id = m.group(1)
    return conv_id, msg_id


def parse_line(line):
    """把一行 connect 日志解析成 InboundMessage；无法识别为"收到"消息返回 None。

    只解析"收到 @user: text"这类入站消息行。业务消息行（合并转发等）本身也可能
    走这个格式（text=占位）或另有格式——kind=forward 的检测下放给能力（它们持有
    自己的业务正则），本函数只在能识别出 user/text 时给出 text/image/reboot 判定。

    Returns:
        InboundMessage 或 None（该行不是入站消息，如 agent 回复日志、状态行）。
    """
    if not line:
        return None
    m = _REPLY_RE.search(line)
    if not m:
        return None
    user = m.group(1)
    text = (m.group(2) or "").strip()
    conv_type = m.group(3)
    conv_id, msg_id = _extract_ids(line)
    kind = classify(text)
    return InboundMessage(
        user=user, text=text, conv_type=conv_type,
        conv_id=conv_id, msg_id=msg_id, kind=kind, raw_line=line,
    )


def classify(text):
    """根据文本判定 kind（reboot / image / file / text）。

    forward/unknown 不在这里判——合并转发靠能力的业务正则匹配整行（text 层面看
    不出来），由 registry 在 dispatch 前用能力自己的检测；本函数只覆盖"收到 @user:
    text"能直接看出的几类。
    """
    low = (text or "").strip().lower()
    if low == "/reboot":
        return KIND_REBOOT
    t = text.strip()
    # 图片：dev-connect 是精确 "[图片]"；event-consume 是 "[图片消息](mediaId=...)"（可带说明文字）
    if t == _IMAGE_PLACEHOLDER or _IMAGE_MARKER in t:
        return KIND_IMAGE
    # 文件：event-consume 下形如 "[文件] <名> fileId: <id> 注意：如需下载..."
    if _FILE_MARKER in t:
        return KIND_FILE
    return KIND_TEXT
