"""routes.py — 路由兼容垫片（DEPRECATED，逻辑已迁到 capabilities/）

历史上 core↔custom 的路由契约在这里手写。现在改为**能力插件化**：业务逻辑迁到
`src/custom/capabilities/<name>.py`，每个能力向 `core.capabilities` 注册；core 认
注册表而非本文件。见 FORKING.md「新增能力插件」。

本文件保留为**薄兼容层**：仍暴露旧的 4 个 route_* 函数（转调注册表），供任何还
import 它们的旧代码/测试使用。**新增能力不要改这里**——写一个 capability 模块。
"""

import os
import sys

SRC_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from core import inbound as _inbound
from core.capabilities import (
    classify_line as _classify_line,
    dispatch_inbound as _dispatch_inbound,
    dispatch_sse as _dispatch_sse,
    dispatch_cleanup as _dispatch_cleanup,
)
# import 触发能力注册（与 event_watcher 里一致；单独 import 本模块的旧代码也能拿到能力）
import custom.capabilities  # noqa: F401


def route_reply(user, text, conv_type, raw_line):
    """[兼容] 普通文本回复：归一成 InboundMessage → 注册表分发。"""
    msg = _inbound.parse_line(raw_line)
    if msg is None:
        # 旧调用方直接传字段而非完整日志行时的兜底
        msg = _inbound.InboundMessage(
            user=user, text=(text or "").strip(), conv_type=conv_type,
            kind=_inbound.classify(text), raw_line=raw_line or "",
        )
        conv_id, msg_id = _inbound._extract_ids(raw_line or "")
        msg.conv_id, msg.msg_id = conv_id, msg_id
    _dispatch_inbound(msg)


def route_business_line(line):
    """[兼容] 业务消息行：能力认领特殊格式（合并转发等）→ 分发。返回是否命中。"""
    msg = _classify_line(line)
    if msg is None:
        return False
    return _dispatch_inbound(msg)


def route_sse_event(event, port, password):
    """[兼容] SSE 事件 → 注册表分发。返回 True 表示被能力消费。"""
    return _dispatch_sse(event, port, password)


def route_cleanup_state(event, cleanup_state, cleanup_lock):
    """[兼容] cleanup 事件 → 注册表分发。返回 True 表示被能力消费。"""
    return _dispatch_cleanup(event, cleanup_state, cleanup_lock)
