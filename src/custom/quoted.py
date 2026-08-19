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


def resolve(conv_id, msg_id, media_wait=None):
    """取回被引用的消息 → {"text","sender","media","desc","msg_id","conv_id"}；取不到 None。

    被引用的是图片时**一律发起识别**（走 mediadesc 单飞，可能命中已有描述而零开销）——
    **引用是显式证据**，用户明确指了这张图，比"时间上挨着"的猜测可靠得多。

    注意 `media_wait` 只决定**等不等**，不决定发不发起：写成 `if media and media_wait`
    会让 wait=0 的调用方（改造后的 context.py 就是）连识别都不触发，于是描述永远不会
    出现，convq 那条路也只能从冷启动干起。等不到就返回空 desc，由调用方给出取内容的
    命令 —— 降级的是"这一轮拿不拿得到"，不是"要不要开始"。
    """
    if not msg_id:
        return None
    got = _from_store(conv_id, msg_id) or _from_cli(msg_id)
    if not got:
        return None
    text, sender = got
    media = bool(_MEDIA_RE.match(text or ""))
    desc = ""
    if media:
        from custom import mediadesc
        desc, _st = mediadesc.describe(conv_id, msg_id, text,
                                       wait=media_wait or None, by="quoted")
    return {"text": text, "sender": sender, "media": media, "desc": desc,
            "msg_id": msg_id, "conv_id": conv_id}


def build_prompt(user, text, quoted):
    """把被引用内容和用户原话拼成结构化 prompt（配 generate_reply 的 raw=True）。

    分块写而不是拼成一句话：下游得能分清哪句是用户说的、哪段是被引用的，否则模型会把
    引用内容当成用户的诉求。
    """
    who = quoted.get("sender") or "某人"
    if quoted.get("desc"):
        body = "（这是一张图片，以下是它的内容识别结果）\n" + quoted["desc"]
    elif quoted.get("media"):
        # 以前这里写的是「请说明你需要对方直接把它发给你」—— 那在有 convq 之后是**错误
        # 建议**：内容拿得到，只是还没识别完。给命令，并明确禁止在拿到之前瞎猜。
        from custom import convq
        body = ("（这是一张图片或一个文件，内容还在识别中。**需要它的内容时先运行下面这条"
                "命令**，它会等到识别完成再返回：\n  "
                + convq.cmd_hint(quoted.get("conv_id") or "",
                                 "image", quoted.get("msg_id") or "")
                + "\n 在拿到结果之前，不要猜测里面是什么。）")
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
