"""brain.py — 回复生成协议 + 临时会话登记表（core 稳定层）

能力通过 `generate_reply(user, text, ctx=None, raw=False)` 让"大脑"生成回复文本。
**生成的实现**（opencode serve HTTP / LLM proxy / echo）由 custom 注册（`register_brain`）；
core 只定义接口 + 默认实现（echo），让能力依赖 core 而非某后端。

另含**临时会话登记表**（纯机制，无平台耦合）：大脑在托管 serve 上建的临时 session 会在
SSE 流冒出 session.status/idle 事件；登记 sid（连同来源会话 conv 上下文）供：
  - text_reply 抑制这些事件的业务通知（is_textreply_session）；
  - question 把 question.asked/答案路由回来源群（session_conv，事件只有 sessionID）。

- 能力：`from core.brain import generate_reply, session_conv, is_textreply_session, register_session`
- custom：`register_brain(impl)` 注入真实生成实现（如 custom/brain.py 的 opencode 版）
- 默认（未注册）：echo 规则式回复（无网络依赖）。
"""

import threading
from collections import OrderedDict

from core.agent_common import log

# ---------------------------------------------------------------------------
# 生成实现：协议 + 注册 + 默认 echo
# ---------------------------------------------------------------------------
_brain_impl = None            # (user, text, ctx, raw) -> str（字符串契约，向后兼容）
_brain_impl_ex = None         # (user, text, ctx, raw) -> (str, status)（状态感知，可选）
_MAX_REPLY_CHARS = None  # 由实现自行截断；core 不强制

# 生成结果状态（#59）：让上层区分「模型正常但没话说」与「后端不可用/失败」。
STATUS_OK = "ok"          # 有回复
STATUS_EMPTY = "empty"    # 后端正常但返回空（模型选择不回复）
STATUS_FAILED = "failed"  # 后端不可用/超时/异常（应给用户兜底提示 + ack 落失败终态）


def register_brain(fn):
    """注册回复生成实现。签名 (user, text, ctx=None, raw=False) -> str（空串=不回复）。"""
    global _brain_impl
    _brain_impl = fn
    log(f"brain 实现已注册: {getattr(fn, '__module__', '?')}.{getattr(fn, '__name__', fn)}")


def register_brain_ex(fn):
    """注册**状态感知**的生成实现。签名 (user, text, ctx=None, raw=False) -> (str, status)。

    可选：注册了它，generate_reply_ex 用它拿到 ok/empty/failed 区分；否则回落到
    字符串实现（失败与空回复都塌缩成 empty，保持旧行为）。generate_reply（纯字符串）
    始终只取回复文本，向后兼容不受影响。
    """
    global _brain_impl_ex
    _brain_impl_ex = fn
    log(f"brain(ex) 实现已注册: {getattr(fn, '__module__', '?')}.{getattr(fn, '__name__', fn)}")


def _default_brain(user, text, ctx=None, raw=False):
    """默认 echo：零依赖规则式回复（无网络）。"""
    low = (text or "").strip().lower()
    if low in ("ping", "在吗", "在不在"):
        return "在的，有什么可以帮你？"
    if low in ("help", "帮助", "/help"):
        return "我是数字员工（echo 默认）。配置 AGENT_BRAIN=opencode/proxy 接入 LLM。"
    if low.startswith(("你好", "hi", "hello", "您好")):
        return f"你好 {user}！我是数字员工，很高兴为你服务。"
    return f"收到你的消息：{text}"


def generate_reply(user, text, ctx=None, raw=False):
    """生成回复文本（返回空串=不回复）。委托已注册实现；未注册用默认 echo。

    raw=True 时 text 已是完整 prompt，实现不应再拼 "{user}：" 前缀。

    纯字符串契约（向后兼容）：失败与空回复都返回 ""。需要区分失败的调用方用
    generate_reply_ex。
    """
    reply, _status = generate_reply_ex(user, text, ctx=ctx, raw=raw)
    return reply


def generate_reply_ex(user, text, ctx=None, raw=False):
    """生成回复 + 状态（#59）。返回 (reply, status)，status ∈ ok/empty/failed。

    - 注册了状态感知实现（register_brain_ex）→ 直接用它的 (reply, status)。
    - 只有字符串实现 → 拿到字符串：非空=ok，空=empty；实现内部抛异常=failed。
      （纯字符串实现无法自证"失败"，故失败仅能由异常体现——旧 echo/proxy 不受影响。）
    """
    text = (text or "").strip()
    if not text:
        return "", STATUS_EMPTY
    try:
        if _brain_impl_ex is not None:
            reply, status = _brain_impl_ex(user, text, ctx=ctx, raw=raw)
            reply = reply or ""
            if status not in (STATUS_OK, STATUS_EMPTY, STATUS_FAILED):
                status = STATUS_OK if reply else STATUS_EMPTY
            return reply, status
        fn = _brain_impl or _default_brain
        reply = fn(user, text, ctx=ctx, raw=raw) or ""
        return reply, (STATUS_OK if reply else STATUS_EMPTY)
    except Exception as e:
        log(f"brain generate err: {e}")
        return "", STATUS_FAILED


# ---------------------------------------------------------------------------
# 临时会话登记表（纯机制，供 text_reply 抑制 SSE 通知 + question 回程路由）
# ---------------------------------------------------------------------------
_SESSION_MAX = 256
_sessions = OrderedDict()   # sid -> ctx dict（含 conv_id/conv_type，无 ctx 时 {}）
_sessions_lock = threading.Lock()


def register_session(sid, ctx=None):
    """登记大脑临时 session；ctx 可含来源会话（conv_id/conv_type）供事件回程路由。有界 FIFO。"""
    if not sid:
        return
    with _sessions_lock:
        _sessions[sid] = dict(ctx or {})
        while len(_sessions) > _SESSION_MAX:
            _sessions.popitem(last=False)


def is_textreply_session(sid):
    """该 SSE sessionID 是否是大脑临时 session（命中则抑制其业务通知）。"""
    if not sid:
        return False
    with _sessions_lock:
        return sid in _sessions


def session_conv(sid):
    """取某 session 登记的来源会话 ctx（{conv_id, conv_type, ...}）；未登记返回 None。"""
    if not sid:
        return None
    with _sessions_lock:
        v = _sessions.get(sid)
        return dict(v) if v is not None else None
