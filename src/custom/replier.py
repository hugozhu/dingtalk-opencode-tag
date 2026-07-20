"""replier.py — 把回复发回钉钉（custom 层）

可插拔发送模式，由环境变量 AGENT_REPLY_MODE 选择：
  log  (默认)  只写日志，不真正发钉钉。安全联调用：先验证收发闭环与回复内容，
               确认无误后再开真发，避免误发/刷屏。
  bot          用机器人身份 send-by-bot 发到来源群（需 AGENT_ROBOT_CODE）。
  user         用当前登录用户身份 send 发到来源群。

接口：send_reply(conv_id, conv_type, text, *, at_user_id=None) -> bool
"""

import os
import subprocess

from core.agent_common import ROBOT_CODE, PROFILE, log
from core.capabilities import dispatch_reply_sent

_REPLY_MODE = os.environ.get("AGENT_REPLY_MODE", "log")
# 回复标题（send-by-bot 需要 title）
_REPLY_TITLE = os.environ.get("AGENT_REPLY_TITLE", "数字员工")


def send_reply(conv_id, conv_type, text, *, at_user_id=None):
    """把回复发回来源会话。返回 True 表示已发送/已记录。

    Args:
        conv_id:  来源 openConversationId
        conv_type: 会话类型（1=单聊 2=群聊；send --group 对两者通用，均按 conv_id 发）
        text:     回复正文
        at_user_id: 可选，群里 @ 回某人的 userId
    """
    text = (text or "").strip()
    if not text:
        return False
    if not conv_id:
        log(f"reply skip: 无 conv_id (mode={_REPLY_MODE})")
        return False

    # fail-fast：真发模式下 PROFILE 仍是占位值 → dws 会报"未登录"，提前给出可操作提示
    if _REPLY_MODE in ("bot", "user") and (not PROFILE or PROFILE == "your-profile"):
        log("reply skip: AGENT_PROFILE 未配置（仍为占位 'your-profile'）。"
            "请在 config/constants.local.sh 设 AGENT_PROFILE=<真实 profile>，"
            "否则 dws 报未登录。见 constants.sh 顶部坑#2。")
        return False

    if _REPLY_MODE == "bot":
        ok = _reply_bot(conv_id, text, at_user_id)
    elif _REPLY_MODE == "user":
        ok = _reply_user(conv_id, text)
    else:
        # 默认 log 模式：只记录不发送（仍视为"已回复"，让回执状态机收尾）
        log(f"[reply:log] → conv={conv_id[:16]} text={text[:120]!r}")
        ok = True

    # 通知回执能力（ack）"回复已发出"，把处理中表情换成完成/失败。best-effort，
    # 广播里的异常已被 dispatch 隔离，绝不影响本次回复结果。
    dispatch_reply_sent(conv_id, conv_type, ok)
    return ok


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
