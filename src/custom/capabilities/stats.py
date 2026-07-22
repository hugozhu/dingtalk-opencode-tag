"""stats — 会话统计查询能力

支持用户主动查询当前会话的统计信息：
- /stats：显示当前会话的统计摘要（session ID、耗时、模型、轮数、tokens 等）

需要开启 AGENT_SESSION_REUSE 才有多轮会话统计；无状态模式下无可用统计。

优先级：10（高优先级，在 text_reply 之前处理，避免被当作普通文本发给 LLM）

开关：CAP_STATS_ENABLED（默认开）
"""

import os

from core.agent_common import log
from core.capabilities import Capability, register
from core.inbound import KIND_TEXT
from core.replier import send_reply


def on_inbound(msg):
    """/stats 命令：返回当前会话的统计信息。返回 True=已消费。"""
    text = (msg.text or "").strip().lower()
    if text != "/stats":
        return False  # 不是 /stats 命令，不消费

    conv_id = msg.conv_id
    conv_type = msg.conv_type

    # 延迟导入避免循环依赖
    try:
        from custom.brain import _get_session_stats, _format_session_summary
    except ImportError:
        log("stats: 无法导入 custom.brain 统计函数")
        send_reply(conv_id, conv_type, "⚠️ 统计功能不可用。")
        return True

    stats = _get_session_stats(conv_id)
    if not stats:
        send_reply(conv_id, conv_type, "📊 当前没有活跃的会话统计信息。\n\n提示：需要开启 AGENT_SESSION_REUSE 才能追踪多轮会话统计。")
        return True

    summary = _format_session_summary(stats)
    if summary:
        send_reply(conv_id, conv_type, f"📊 当前会话统计\n\n{summary}")
    else:
        send_reply(conv_id, conv_type, "⚠️ 无法生成统计摘要。")

    return True


CAPABILITY = Capability(
    name="stats",
    on_inbound=on_inbound,
    handles_kinds={KIND_TEXT},
    priority=10,           # 高优先级，在 text_reply (100) 之前处理
    default_enabled=True,
    dedup=True,            # msgId 去重
)
register(CAPABILITY)
