"""task_stats — 任务完成后推送本次执行统计（#76）

每次 session 任务产出回复后，单独发一条「本次任务统计」到来源会话，让用户直观了解单次
交互的成本与规模：耗时 / 工具调用轮次 / 输入输出（含推理）token / 缓存命中率。

与 #63 会话统计摘要互补：#63 是**累计**统计、在会话结束/重置（reset/ttl/lru/command）时发；
本能力是**单次交互 delta**、在**每次回复发出后**发。

实现要点：
- 挂 on_reply_sent（回复已发出后触发，顺序在正文回复之后）：从 brain 暂存表取本次任务的
  delta 统计（brain._stash_task_stats 在成功产出后暂存），格式化后用 replier.send_notice 发出。
- **用 send_notice 不广播 reply-sent**：否则会再次驱动 ack 收尾 + 递归触发本能力。
- 仅在成败为 ok 时发（失败不发统计，避免和兜底提示叠加）；失败/空回复的 delta 也不会入暂存。
- CLI 回退路径无 token 数据 → 不暂存 → 不发。

开关：
- CAP_TASK_STATS_ENABLED（**默认关**，避免每条消息都刷一条统计；显式开启）。
- AGENT_TASK_STATS_O2O_ONLY（默认 1，仅单聊发；群聊默认不发避免噪音）。
"""

import os

from core.agent_common import env_flag, log
from core.capabilities import Capability, register

_O2O_ONLY = env_flag("AGENT_TASK_STATS_O2O_ONLY", default=True)


def on_reply_sent(conv_id, conv_type, ok):
    """回复已发出后：若本次成功且有暂存统计 → 发一条本次任务统计（best-effort）。"""
    if not ok or not conv_id:
        return
    if _O2O_ONLY and str(conv_type) != "1":
        return
    # 延迟导入避免 capabilities 载入期循环
    try:
        from custom.brain import pop_task_stats, format_task_stats
        from custom.replier import send_notice
    except ImportError:
        return
    rec = pop_task_stats(conv_id)
    if not rec:
        return
    msg = format_task_stats(rec)
    if not msg:
        return
    try:
        send_notice(conv_id, conv_type, msg)
        log(f"task_stats: 已推送本次任务统计 conv={conv_id[:12]}")
    except Exception as e:
        log(f"task_stats: 推送失败 {e}")


CAPABILITY = Capability(
    name="task_stats",
    on_reply_sent=on_reply_sent,
    default_enabled=False,     # 默认关：避免每条消息都刷统计。CAP_TASK_STATS_ENABLED=1 开启
)
register(CAPABILITY)
