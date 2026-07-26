"""cancel — 用户主动取消正在执行的长程任务（#75）

长程任务改为活动感知超时后（见 custom/brain.py），一次任务可能跑很久。本能力让用户
中途主动叫停：发「取消/停止/stop//cancel」→ abort 该会话正在跑的 message POST。

链路成立性（与 permission/question 同构）：brain 的 message POST 阻塞在 worker 线程，
该 conv 被 `_conv_lock` 串行——**取消消息若走普通回复路径会卡在同一把锁上**，故本能力
在 on_inbound 直接查 brain 的在跑登记表（`cancel_inflight`）POST abort，**不走 brain、
不抢锁**，从而解阻塞正卡住的 worker。

优先级 5：在 permission(15)/question(20)/text_reply(100) 之前，避免「取消」被当普通文本
发给 LLM 或被当作审批答复。命中关键词但当前无在跑任务 → 返回 False 放行后续能力
（用户可能只是在正常聊天里说「停止」）。

开关：CAP_CANCEL_ENABLED（默认开）。
"""

import os

from core.agent_common import log
from core.capabilities import Capability, register
from core.inbound import KIND_TEXT
from core.replier import send_reply

# 取消关键词（整句 lower 后严格匹配；未命中放行给后续能力）
_CANCEL_KEYWORDS = {
    k.strip().lower()
    for k in os.environ.get("AGENT_CANCEL_KEYWORDS", "/cancel,取消,停止,stop").split(",")
    if k.strip()
}


def on_inbound(msg):
    """取消命令：命中关键词且该 conv 有在跑任务 → abort 并回执。返回 True=已消费。"""
    text = (msg.text or "").strip().lower()
    if text not in _CANCEL_KEYWORDS:
        return False  # 不是取消命令，放行

    conv_id = msg.conv_id
    conv_type = msg.conv_type

    # 延迟导入避免循环依赖（cancel ← brain ← capabilities）
    try:
        from custom.brain import cancel_inflight
    except ImportError:
        log("cancel: 无法导入 custom.brain.cancel_inflight")
        return False

    if cancel_inflight(conv_id):
        log(f"cancel: 已 abort conv={conv_id[:12]} user={msg.user}")
        send_reply(conv_id, conv_type, "🛑 已取消当前任务。")
        return True

    # 无在跑任务：可能用户只是在正常对话里说了「停止」，放行给后续能力
    log(f"cancel: conv={conv_id[:12]} 无在跑任务，放行")
    return False


CAPABILITY = Capability(
    name="cancel",
    on_inbound=on_inbound,
    handles_kinds={KIND_TEXT},
    priority=5,            # 最前：先于 permission/question/text_reply
    default_enabled=True,
    dedup=True,            # msgId 去重
)
register(CAPABILITY)
