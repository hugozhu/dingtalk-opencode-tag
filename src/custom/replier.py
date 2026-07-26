"""replier.py — 钉钉发送实现（custom 层，注册进 core.replier）

把回复用 dws 发回钉钉。发送**协议**（send_reply + dispatch_reply_sent 广播 + 空 conv 兜底）
在 core.replier；本模块只提供**钉钉平台实现**并 register_replier 注入。换平台只改这里。

可插拔发送模式，由环境变量 AGENT_REPLY_MODE 选择：
  log  (默认)  只写日志，不真正发钉钉。安全联调用：先验证收发闭环与回复内容。
  bot          用机器人身份 send-by-bot 发到来源群（需 AGENT_ROBOT_CODE）。
  user         用当前登录用户身份 send 发到来源群。

接口（供 core.replier 调用）：_dingtalk_send(conv_id, conv_type, text, *, at_user_id=None) -> bool
"""

import os
import subprocess

from core.agent_common import ROBOT_CODE, PROFILE, log
from core.replier import register_replier

_REPLY_MODE = os.environ.get("AGENT_REPLY_MODE", "log")
# 回复标题（send-by-bot 需要 title）
_REPLY_TITLE = os.environ.get("AGENT_REPLY_TITLE", "数字员工")
# 单条钉钉消息分片上限（字符）。长回复按段落/换行拆成多条顺序发出，避免撑爆钉钉
# 服务端单消息上限（~20000 字节）且不再一刀切丢内容。中文 UTF-8 下 3500 字符 ≈ 10.5KB，安全。
_CHUNK_CHARS = int(os.environ.get("AGENT_REPLY_CHUNK_CHARS", "3500"))
# 分片时给「（i/n）\n」前缀预留的字符余量
_PREFIX_MARGIN = 16


def _split_text(text, size):
    """把 text 切成每片 ≤ size 的列表，优先在 \\n 段落边界断开；单行超长则硬切。

    预留 _PREFIX_MARGIN 给「（i/n）」前缀，故实际累积上限为 size-_PREFIX_MARGIN。
    text 长度 ≤ 有效上限时原样返回单元素列表（不加前缀，见 _dingtalk_send）。
    """
    limit = max(1, size - _PREFIX_MARGIN)
    if len(text) <= limit:
        return [text]
    chunks, cur = [], ""
    for line in text.split("\n"):
        # 单行本身超长：先冲掉累积，再把该行硬切成多片
        while len(line) > limit:
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(line[:limit])
            line = line[limit:]
        add = line if not cur else cur + "\n" + line
        if len(add) > limit:
            chunks.append(cur)
            cur = line
        else:
            cur = add
    if cur:
        chunks.append(cur)
    return chunks


def _dingtalk_send(conv_id, conv_type, text, *, at_user_id=None):
    """钉钉发送实现。返回 True=已发送/已记录。core.replier 已做空 text 过滤 + 回执广播。

    长回复在此按 _CHUNK_CHARS 分片顺序发出；对上层透明——send_reply 只调本函数一次、
    只广播一次 reply-sent，内部发多条 dws 消息不影响 ack 回执语义。

    Args:
        conv_id:  来源 openConversationId
        conv_type: 会话类型（1=单聊 2=群聊；send --group 对两者通用，均按 conv_id 发）
        text:     回复正文
        at_user_id: 可选，群里 @ 回某人的 userId（多片时只在第 1 片带，避免重复 @）
    """
    if not conv_id:
        log(f"reply skip: 无 conv_id (mode={_REPLY_MODE})")
        return False

    # fail-fast：真发模式下 PROFILE 仍是占位值 → dws 会报"未登录"，提前给出可操作提示
    if _REPLY_MODE in ("bot", "user") and (not PROFILE or PROFILE == "your-profile"):
        log("reply skip: AGENT_PROFILE 未配置（仍为占位 'your-profile'）。"
            "请在 config/constants.local.sh 设 AGENT_PROFILE=<真实 profile>，"
            "否则 dws 报未登录。见 constants.sh 顶部坑#2。")
        return False

    # 默认 log 模式：只记录不发送（仍视为"已回复"，让回执状态机收尾）
    if _REPLY_MODE not in ("bot", "user"):
        log(f"[reply:log] → conv={conv_id[:16]} text={text[:120]!r}")
        return True

    chunks = _split_text(text, _CHUNK_CHARS)
    n = len(chunks)
    if n > 1:
        log(f"reply {_REPLY_MODE}: 长回复分 {n} 片发送 conv={conv_id[:16]} total_len={len(text)}")
    for i, ch in enumerate(chunks, 1):
        body = ch if n == 1 else f"（{i}/{n}）\n{ch}"
        if _REPLY_MODE == "bot":
            ok = _reply_bot(conv_id, body, at_user_id if i == 1 else None)
        else:
            ok = _reply_user(conv_id, body)
        if not ok:
            if n > 1:
                log(f"reply {_REPLY_MODE}: 第 {i}/{n} 片发送失败，终止后续分片")
            return False
    return True


def _reply_bot(conv_id, text, at_user_id):
    """机器人身份 send-by-bot 发到群。"""
    if not ROBOT_CODE or ROBOT_CODE == "your-robot-code":
        log("reply bot skip: AGENT_ROBOT_CODE 未配置")
        return False
    cmd = ["dws", "chat", "message", "send-by-bot",
           "--robot-code", ROBOT_CODE,
           "--group", conv_id,
           "--title", _REPLY_TITLE[:60],
           "--text", text,
           "--profile", PROFILE, "--format", "markdown", "-y"]
    if at_user_id:
        cmd += ["--at-user-ids", at_user_id]
    return _run(cmd, "bot")


def _reply_user(conv_id, text):
    """当前用户身份 send 发到来源会话（群或单聊，均按 openConversationId 发）。"""
    cmd = ["dws", "chat", "message", "send",
           "--group", conv_id,
           "--text", text,
           "--profile", PROFILE, "-y"]
    return _run(cmd, "user")


def _run(cmd, mode):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if r.returncode != 0:
            log(f"reply {mode} FAIL rc={r.returncode} stderr={r.stderr[:200]}")
            return False
        log(f"reply {mode} OK")
        return True
    except Exception as e:
        log(f"reply {mode} err: {e}")
        return False


# 注入钉钉实现，让能力经 core.replier.send_reply 统一发送。
register_replier(_dingtalk_send)


def send_notice(conv_id, conv_type, text, *, at_user_id=None):
    """发一条通知/进度消息，**不广播 reply-sent**（不触发 ack 收尾）。best-effort。

    与 send_reply 的区别：send_reply 发完会 dispatch_reply_sent，驱动 ack 切换完成/失败终态；
    进度/心跳消息若走 send_reply 会被 ack 误判为「回复已发出」而提前收尾。故单开此口——
    直接调平台发送实现，只发不广播。用途：长任务每 N 分钟的进度心跳（见 ack 能力）。
    """
    text = (text or "").strip()
    if not text or not conv_id:
        return False
    try:
        return bool(_dingtalk_send(conv_id, conv_type, text, at_user_id=at_user_id))
    except Exception as e:
        log(f"send_notice err: {e}")
        return False


# 向后兼容：仍暴露 send_reply（= core 版），旧代码/测试 `from custom.replier import send_reply` 不破。
from core.replier import send_reply  # noqa: E402,F401
