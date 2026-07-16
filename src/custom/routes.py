"""routes.py — 业务路由注册表（FDE 在这里注册自己的业务 handler）

本文件是 **core 与 custom 之间的契约边界**：
  - core/event_watcher.py 的 log_tail_thread 解析每行日志后调用本模块的函数
  - FDE 在本文件注册自己的业务路由，**永不触碰 core/event_watcher.py**

这样实现 upstream → fork 的核心修复可干净 merge（core 路径一致），
fork 里发现的 core bug 也可 cherry-pick 回 upstream。

默认实现示范：把合并转发消息路由到 custom.handler.handle_message。
FDE 按业务扩展（图片/语音/文件/自定义指令等）。
"""
import os
import sys

SRC_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

import threading

from core.agent_common import log, submit_handler
from custom.handler import handle_message, match_business_line


def route_reply(user, text, conv_type, raw_line):
    """处理普通文本回复消息的路由。

    被 core/event_watcher.py 的 log_tail_thread 在解析到
    "[connect] 收到 @user: text (convType=N ...)" 时调用
    （已排除 /reboot 指令，/reboot 由 core 直接处理）。

    FDE 在这里实现自己的分发逻辑，例如:
      - text == "[图片]"   → handle_image(...)
      - text == "/cmd ..." → handle_custom_command(...)
      - 其他               → handle_reply(user, text)

    Args:
        user: 发送者标识
        text: 消息文本（已 strip）
        conv_type: 会话类型（从日志 convType=N 提取）
        raw_line: 原始日志行（用于需要完整上下文的复杂匹配）
    """
    # 默认：什么都不做。FDE 按业务实现
    pass


def route_business_line(line):
    """处理业务消息行的路由。返回 True 表示已处理（命中业务 handler）。

    被 core/event_watcher.py 的 log_tail_thread 对每行日志调用。
    用于检测业务特定消息格式（如合并转发、业务特殊消息）并派发 handler。

    FDE 在这里注册业务消息检测 + handler 派发。
    默认实现：调用 handler.match_business_line 检测合并转发消息。
    """
    m = match_business_line(line)
    if m:
        mid, convs = m
        submit_handler(handle_message, mid, convs)
        return True
    return False


def route_sse_event(event, port, password):
    """处理 SSE 事件的转发逻辑（可选 hook）。

    被 core/event_watcher.py 的 connect_sse 在收到每个 SSE 事件时调用。
    返回 True 表示已处理，core 不再做默认转发；返回 False 走 core 默认逻辑。

    FDE 一般不需要改这里（默认转发逻辑在 core/format_and_forward 里）。
    仅当需要自定义事件过滤/转换时实现本函数返回 True。
    """
    return False


def route_cleanup_state(event, cleanup_state, cleanup_lock):
    """spurious 多余轮次的 cleanup 状态机 hook（可选）。

    被 core/format_and_forward 在处理每个 SSE 事件时调用（core 已做 TTL 过期兜底）。
    core 有意把状态机下放到 custom：awaiting_spurious → cleaning 的具体判定与依赖
    服务的日志/消息格式强相关，属于业务特定逻辑，放在可编辑的 custom 层。

    参数：
        event: SSE 事件 dict（含 type / properties.sessionID / ...）
        cleanup_state: core 持有的共享状态 dict（sid[:12] -> {state, expires, ...}）
        cleanup_lock: 保护 cleanup_state 的 threading.Lock

    返回 True 表示本事件已被 cleanup 消费（core 不再默认转发）；False 走默认。

    默认实现：不做任何 cleanup（返回 False）。需要清理 spurious 轮次的 FDE，
    参考 agent_common._abort_and_clean_session 在这里实现状态机。
    """
    return False
