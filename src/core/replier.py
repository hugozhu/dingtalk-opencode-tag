"""replier.py — 回复发送协议（core 稳定层）

能力生成回复后，通过 `send_reply(conv_id, conv_type, text)` 发回来源会话。**发送的
平台实现**（钉钉 dws / 别的 IM SDK）由 custom 层注册进来（`register_replier`）；core 只
定义接口 + 默认实现（log-only，不真发），让能力依赖 core 而非某平台。

- 能力：`from core.replier import send_reply`
- custom：`register_replier(impl)` 注入真实发送实现（如 custom/replier.py 的 dws 版）
- 默认（未注册）：只记日志，不真发 —— 安全联调 / 无平台依赖也能跑。

发送后 core 广播 `dispatch_reply_sent(conv_id, conv_type, ok)` 给能力（ack 回执据此切表情）。
"""

from core.agent_common import log
from core.capabilities import dispatch_reply_sent

# 当前生效的发送实现。None = 用默认 log-only。
_impl = None


def register_replier(fn):
    """注册回复发送实现。签名 (conv_id, conv_type, text, *, at_user_id=None) -> bool。
    返回 True=已发送/已记录。重复注册覆盖。"""
    global _impl
    _impl = fn
    log(f"replier 实现已注册: {getattr(fn, '__module__', '?')}.{getattr(fn, '__name__', fn)}")


def _default_send(conv_id, conv_type, text, *, at_user_id=None):
    """默认发送：只记日志不真发（无平台依赖，安全联调）。"""
    log(f"[reply:log-default] → conv={conv_id[:16] if conv_id else ''} text={(text or '')[:120]!r}")
    return True


def send_reply(conv_id, conv_type, text, *, at_user_id=None):
    """把回复发回来源会话，返回 True=已发送/已记录。

    委托给已注册的平台实现；未注册时用默认 log-only。发送后广播 dispatch_reply_sent
    通知能力（best-effort，异常隔离，不影响本次结果）。
    """
    text = (text or "").strip()
    if not text:
        return False
    fn = _impl or _default_send
    try:
        ok = bool(fn(conv_id, conv_type, text, at_user_id=at_user_id))
    except Exception as e:
        log(f"replier send err: {e}")
        ok = False
    dispatch_reply_sent(conv_id, conv_type, ok)
    return ok
