"""quoted — 把「被引用的那条消息」补进上下文（custom 层，#112）

群里引用一条消息再 @ 数字员工时，它以前只看得到你自己打的那几个字。真实案例：

    主管操作：引用一张图片 → 「@一粟(一粟) 看一下」
    大脑收到：「@一粟(一粟) 看一下」          ← 被引用的内容全丢了

「看一下」离开被引用的那条就没有任何意义。

数据其实一直拿得到：事件里的 `quoted_message` 带 content / sender / create_time，只是
bridge 出于**单行格式**的限制只透传了 `message_id`（正文可能很长、含换行，塞进 connect
行要截断+转义，不划算）。所以内容在能力侧按 id 取回：

  1. 先查本地消息存储（#111 的 msgstore）—— 群消息本来就入库，**零网络往返**
  2. 查不到再回落 `dws chat message list-by-ids` —— 老消息、存储启用之前的消息

组装 prompt 的形状照 image.py / forward.py 的既有范式（结构化 prompt + raw=True），
不把引用内容混进用户原话里 —— 混进去会让下游分不清哪句是用户说的。
"""

import json
import re

from core.agent_common import log, _run_cli
from custom import msgstore

# 媒体消息的正文形如 `[图片消息](mediaId=$iwEc…)`；第一阶段不做识别，但要让大脑知道
# "被引用的是一张图/一个文件"，而不是把这串 mediaId 当正文喂进去。
_MEDIA_RE = re.compile(r"^\s*\[(图片|图片消息|文件|语音消息|视频)\]")


def _from_store(conv_id, msg_id):
    """本地存储里的被引用消息 → (正文, 发送人)；没有返回 None。"""
    rec = msgstore.find(conv_id, msg_id)
    if not rec:
        return None
    return rec.get("text") or "", rec.get("from") or ""


def _from_cli(msg_id):
    """回落：按 msgId 从钉钉取回 → (正文, 发送人)；取不到返回 None。"""
    rc, out = _run_cli(["chat", "message", "list-by-ids", "--msg-ids", msg_id])
    if rc != 0:
        log(f"quoted: 取被引用消息失败 rc={rc} out={out[:120]}")
        return None
    try:
        msgs = (json.loads(out).get("result") or {}).get("messages") or []
    except (ValueError, AttributeError, TypeError) as e:
        log(f"quoted: 解析被引用消息失败 {e}")
        return None
    for m in msgs:
        if m.get("openMessageId") == msg_id or len(msgs) == 1:
            return (m.get("content") or ""), (m.get("sender") or "")
    return None


def resolve(conv_id, msg_id):
    """取回被引用的消息 → {"text", "sender", "media"}；取不到返回 None。

    media=True 表示被引用的是图片/文件这类媒体消息（第一阶段不做识别，见模块 docstring）。
    """
    if not msg_id:
        return None
    got = _from_store(conv_id, msg_id) or _from_cli(msg_id)
    if not got:
        return None
    text, sender = got
    return {"text": text, "sender": sender, "media": bool(_MEDIA_RE.match(text or ""))}


def build_prompt(user, text, quoted):
    """把被引用内容和用户原话拼成结构化 prompt（配 generate_reply 的 raw=True）。

    分块写而不是拼成一句话：下游得能分清哪句是用户说的、哪段是被引用的，否则模型会把
    引用内容当成用户的诉求。
    """
    who = quoted.get("sender") or "某人"
    if quoted.get("media"):
        body = ("（这是一张图片或一个文件，我目前还看不到它的内容 —— 如果用户的问题依赖"
                "它的具体内容，请说明你需要对方直接把它发给你，不要猜测内容。）")
    else:
        body = (quoted.get("text") or "").strip() or "（空消息）"
    return "\n".join([
        f"【被引用的消息】（来自 {who}）",
        body,
        "",
        f"【{user} 说】",
        text,
        "",
        "用户是**针对上面那条被引用的消息**说这句话的，请结合它来理解和回应。",
    ])
