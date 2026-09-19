"""sse_suppress — 拦截 Task 子代理会话的 SSE 业务通知（#125）

core/event_watcher 对 session.status(busy) / session.idle 事件会默认转发业务通知
（「📥 收到新请求」「✅ 会话完成」，走 send_notification → send-by-bot）。brain 的
业务 session（文本/图片/文件/合并转发）都登记在 core.brain 注册表里，由
text_reply.on_sse_event 抑制；但 **Task 委派的 flash-worker 子会话**是 serve 自己
派生的（parentID 指向主会话），不在注册表——每次委派都会漏出 busy/idle 两条通知。
本部署 AGENT_ROBOT_CODE 是占位值，send-by-bot 通道 0 成功/30 失败（business
error: success=false），每条漏网事件刷一行 FAIL，误导排障（2026-09-19 wx post 408
事故里被误读成「兜底提示没发出去」，实际兜底提示经 user 模式发送成功）。

判定方式是**精确的**：GET /session/{sid} 查 parentID，非空 = 子代理会话 → 吞掉。
未登记但 parentID 为空的（合并转发业务 session 等）照常放行走 core 默认转发，
不破坏 text_reply 的既有契约。查询结果按 sid 缓存（有界），GET 失败一律放行
（偏保守，宁多一条通知不误吞业务事件）。

开关：CAP_SSE_SUPPRESS_ENABLED（默认开）。
"""

import threading
from collections import OrderedDict

from core.agent_common import log, serve_request
from core.brain import is_textreply_session
from core.capabilities import Capability, register

# sid -> True/False（是否子代理会话）。有界 FIFO，子会话量级很小。
_CHILD_CACHE_MAX = 512
_child_cache = OrderedDict()
_cache_lock = threading.Lock()


def _is_child_session(sid, port, password):
    """GET /session/{sid} 查 parentID：非空 = Task 派生的子代理会话。

    结果缓存；GET 失败返回 False（放行，偏保守）。
    """
    with _cache_lock:
        if sid in _child_cache:
            _child_cache.move_to_end(sid)
            return _child_cache[sid]
    is_child = False
    try:
        info = serve_request("GET", f"/session/{sid}", timeout=6,
                             port=port, pwd=password)
        if isinstance(info, dict):
            is_child = bool(info.get("parentID"))
    except Exception:
        return False   # 查不到不缓存，下次再试；本次放行
    with _cache_lock:
        _child_cache[sid] = is_child
        while len(_child_cache) > _CHILD_CACHE_MAX:
            _child_cache.popitem(last=False)
    return is_child


def on_sse_event(event, port, password):
    """消费子代理会话的 busy/idle 事件。返回 True=已消费（core 不再默认转发）。"""
    etype = event.get("type", "")
    props = event.get("properties", {}) or {}
    sid = props.get("sessionID", "") or ""
    if not sid:
        return False
    if etype == "session.idle":
        pass
    elif etype == "session.status":
        status = props.get("status", {}) or {}
        if not (isinstance(status, dict) and status.get("type") == "busy"):
            return False
    else:
        return False
    if is_textreply_session(sid):
        return False   # 已登记会话交给 text_reply 统一抑制（避免重复消费语义）
    if not _is_child_session(sid, port, password):
        return False   # 未登记的业务 session（如合并转发）照常走默认转发
    log(f"sse_suppress: 吞掉子代理会话通知 sid={sid[:12]} type={etype}")
    return True


CAPABILITY = Capability(
    name="sse_suppress",
    on_sse_event=on_sse_event,
    priority=95,           # text_reply(100) 之前；条件互斥，顺序仅为确定性
    default_enabled=True,
)
register(CAPABILITY)
