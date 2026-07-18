"""forward — 合并转发（chatRecord）消息能力（custom 插件）

钉钉「合并转发」聊天记录消息，在 dws event consume 模型下**以普通文本消息到达**
（content 是一段摘要，形如 `群聊的聊天记录\nhugozhu:[消息]\nopencode:[消息]`），事件里
没有 msgtype。检测靠 content 里的「聊天记录」摘要特征，再用 `list-by-ids` 反查该 msgId
拿到 `forwardMessages`（普通消息反查不会有这个字段，故也能二次确认）。

流程：
  1. on_inbound(kind=text)：content 命中「聊天记录」摘要 → 认领（return True），派发
     handle_forward；未命中 → 放行（return False）给后面的 text_reply。
  2. handle_forward：list-by-ids 反查 forwardMessages → 补齐 sender（内层 msgId 再反查，
     DingTalk 的 forwardMessage.sender 常为 "null"）→ 拆图/文件/文本 → 组装结构化 prompt →
     走 brain 生成回复 → send_reply 回**来源群**。
  3. 反查不到 forwardMessages（疑似转发的假阳性）→ 回退普通文本回复，不丢消息。

与生产版 forward_handler.py 的差异：event-consume **不自动把原始 JSON 转给 opencode**，
故**不需要** spurious 轮次 cleanup（省掉 _find_session_with_predicate 轮询）。

开关：CAP_FORWARD_ENABLED（默认开）。优先级 50（先于 catch-all 文本回复 100）。
"""

import json
import os
import re
import threading
from collections import OrderedDict

from core.agent_common import _run_cli, log, submit_handler
from core.capabilities import Capability, register
from core.inbound import KIND_TEXT
from custom.brain import generate_reply
from custom.handler import fetch_attachments, _fetch_senders
from custom.replier import send_reply

# 合并转发聊天记录的 content 摘要特征。DingTalk 合并转发（chatRecord）的 content 是一段
# 摘要，含「聊天记录」（如「群聊的聊天记录」「X与Y的聊天记录」）。可用环境变量覆盖。
_FORWARD_SUMMARY_RE = re.compile(
    os.environ.get("CAP_FORWARD_SUMMARY_PATTERN", r"聊天记录"))

# 防回环：数字员工自己发的转发不处理（与 text_reply 同一份自我名单）
_SELF_NAMES = {
    n.strip() for n in os.environ.get("AGENT_SELF_NAMES", "数字员工,Claude Code").split(",")
    if n.strip()
}

# msgId 去重（断线重连可能重投同一事件）—— 有界 FIFO，避免长驻内存泄漏
_seen = OrderedDict()
_seen_lock = threading.Lock()
_SEEN_MAX = 2048


def _seen_before(msg_id):
    """msgId 去重：见过返回 True。空 msgId 不去重。"""
    if not msg_id:
        return False
    with _seen_lock:
        if msg_id in _seen:
            return True
        _seen[msg_id] = None
        if len(_seen) > _SEEN_MAX:
            _seen.popitem(last=False)
    return False


def _looks_like_forward(text):
    """content 是否像合并转发聊天记录摘要（便宜的预筛，真伪由 list-by-ids 确认）。"""
    return bool(_FORWARD_SUMMARY_RE.search(text or ""))


def _fetch_forward_body(msg_id):
    """list-by-ids 反查 msgId，返回 (body, forwardMessages)；失败返回 (None, [])。"""
    rc, out = _run_cli(["chat", "message", "list-by-ids", "--msg-ids", msg_id], timeout=30)
    if rc != 0:
        log(f"forward: list-by-ids 反查失败 rc={rc} msgId={msg_id[:24]}")
        return None, []
    try:
        d = json.loads(out)
        msgs = d.get("result", {}).get("messages", [])
    except Exception as e:
        log(f"forward: 解析 list-by-ids 响应失败: {e}")
        return None, []
    if not msgs:
        return None, []
    body = msgs[0]
    return body, (body.get("forwardMessages") or [])


# 合并转发聊天记录 prompt 末句指令。点明"这是钉钉合并转发的聊天记录、含多方对话、
# 图片/文件已识别转写"，比泛化的"请回应用户"更利于 agent 理解上下文。可用环境变量覆盖。
_FORWARD_PROMPT_FOOTER = os.environ.get(
    "CAP_FORWARD_PROMPT_FOOTER",
    "以上是一段钉钉「合并转发」的聊天记录，包含多位参与者的对话，"
    "其中图片、文件的内容已由系统识别/转写并内联在对应条目里。"
    "请理解这段记录的完整语境（谁说了什么、讨论的主题、引用的图片/文件/链接），"
    "然后对转发者的意图做出有帮助的回应或总结。",
)

# prompt 单条内容截断上限（防止单条超长附件把整个 prompt 撑爆）
_FORWARD_ENTRY_MAX = int(os.environ.get("CAP_FORWARD_ENTRY_MAX", "4000"))


def _build_forward_prompt(forwarder, fms, senders, attachments):
    """组装合并转发的结构化 prompt（完整 input，供 brain raw=True 直接用）。

    结构：
        [语境头] 用户 X 转发了一段聊天记录（共 N 条），按时间顺序列出每条
        [1] [时间] 发送人: 内容
        ...
        [语境尾] 明确这是合并转发的聊天记录 + 任务指令（_FORWARD_PROMPT_FOOTER）

    每条 content 取 attachments[i].text（图片=识别描述、文件=正文、文本=原文），
    单条过长按 _FORWARD_ENTRY_MAX 截断。
    """
    if not fms:
        return None
    senders = list(senders)
    while len(senders) < len(fms):
        senders.append("未知发送人")

    lines = [
        f"用户 {forwarder} 转发了一段聊天记录（共 {len(fms)} 条消息）。"
        f"以下按时间顺序列出每一条（含发送人、时间、内容）：\n"
    ]
    for i, fm in enumerate(fms):
        att = attachments[i] if i < len(attachments) else {}
        t = att.get("time") or fm.get("createTime", "")
        s = senders[i] if i < len(senders) else "未知发送人"
        entry = (att.get("text") or fm.get("content", "") or "").strip()
        if len(entry) > _FORWARD_ENTRY_MAX:
            entry = entry[:_FORWARD_ENTRY_MAX] + "…（内容过长已截断）"
        lines.append(f"[{i + 1}] [{t}] {s}: {entry}\n")
    lines.append(_FORWARD_PROMPT_FOOTER)
    return "\n".join(lines)


def handle_forward(user, text, msg_id, conv_id, conv_type):
    """反查 forwardMessages → 解析 → 组装 prompt → brain 回复 → 发回来源群。

    反查不到 forwardMessages（疑似转发的假阳性）时回退普通文本回复，避免丢消息。
    """
    body, fms = _fetch_forward_body(msg_id)
    if not fms:
        # 假阳性：content 像转发摘要，但反查无 forwardMessages → 当普通消息回
        log(f"forward: msgId={msg_id[:24]} 无 forwardMessages，回退文本回复")
        reply = generate_reply(user, text)
        if reply:
            send_reply(conv_id, conv_type, reply)
        return

    sender = body.get("sender", user) or user
    log(f"forward: msgId={msg_id[:24]} forwardMessages={len(fms)} sender={sender!r}")

    # 内层消息自带 sender（DingTalk 有时给 "null"）。先用自带的，缺的（None/"null"）
    # 交给 _fetch_senders 对内层 msgId 批量反查补齐。
    fallback = [
        (fm.get("sender") if fm.get("sender") not in (None, "null", "") else None)
        for fm in fms
    ]
    senders = _fetch_senders(fms, fallback)
    attachments = fetch_attachments(fms, lookup_convs=None)
    prompt = _build_forward_prompt(sender, fms, senders, attachments)
    if not prompt:
        log(f"forward: prompt 为空 msgId={msg_id[:24]}")
        return

    # 走 brain（opencode serve HTTP）生成回复 → 发回来源群。
    # raw=True：prompt 已是完整结构化 input，brain 不要再拼 "{user}：" 前缀污染上下文。
    reply = generate_reply(sender, prompt, raw=True)
    if reply:
        send_reply(conv_id, conv_type, reply)
    else:
        log(f"forward: 大脑无回复 msgId={msg_id[:24]}")


def on_inbound(msg):
    """文本消息入站：命中合并转发摘要 → 认领并派发；否则放行给 text_reply。"""
    if not _looks_like_forward(msg.text):
        return False  # 不是转发，交给后面的 text_reply
    if msg.user in _SELF_NAMES:
        return True   # 数字员工自己发的转发，消费掉不处理（防回环）
    if _seen_before(msg.msg_id):
        return True   # 已处理过，消费掉不重复回复
    submit_handler(handle_forward, msg.user, msg.text, msg.msg_id, msg.conv_id, msg.conv_type)
    return True


CAPABILITY = Capability(
    name="forward",
    on_inbound=on_inbound,
    handles_kinds={KIND_TEXT},   # 转发在 event-consume 下以文本到达
    priority=50,                 # 先于 catch-all 文本回复（100）
    default_enabled=True,
)
register(CAPABILITY)
