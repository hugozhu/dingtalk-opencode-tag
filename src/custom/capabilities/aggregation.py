"""aggregation — 群消息聚合 + 摘要回复能力（custom 插件）(#29)

群里短时间多条消息逐条回复会打扰。开启本能力后，群消息不逐条回复，而是**按会话
缓冲**，到时间窗（默认 300s）后合并成一次 prompt 让 agent 出摘要/统一回复。

**默认关闭**（CAP_AGGREGATION_ENABLED 默认 0）——它与 text_reply 的逐条回复互斥，
开了就接管群文本消息。单聊、数字员工自己发的消息不聚合。

触发语义（本实现取最简单、可预测的**纯时间窗**）：
  - 每条群文本进来 → 追加到该会话的缓冲。
  - 缓冲第一条时启动一个 flush 定时器（AGG_WINDOW 秒后触发）。
  - 定时器到点 → 把缓冲的全部消息组成一次 prompt → brain → 摘要发回群 → 清空缓冲。
  - 数量上限（AGG_MAX_MSGS）：缓冲满了立即 flush，不等窗口。
后续可扩展"@机器人立即触发"等，但默认不做，保持行为可预测。

优先级 90：先于 text_reply(100) 拦截群文本（消费掉 → text_reply 不再逐条回复），
但晚于 question(20)/image(40)/forward(50)——那些是更具体的消息类型，各自先处理。
"""

import os
import threading
import time

from core.agent_common import log, submit_handler
from core.capabilities import Capability, register
from core.inbound import KIND_TEXT
from core.brain import generate_reply
from core.replier import send_reply

# 时间窗（秒）：缓冲第一条后多久 flush
_AGG_WINDOW = float(os.environ.get("CAP_AGGREGATION_WINDOW", "300"))
# 数量上限：缓冲达到即立即 flush（不等窗口）
_AGG_MAX_MSGS = int(os.environ.get("CAP_AGGREGATION_MAX_MSGS", "50"))
# 群聊 convType（bridge 约定：2=群聊）。单聊不聚合。
_GROUP_CONV_TYPE = "2"

# 防回环：数字员工自己发的不缓冲
_SELF_NAMES = {
    n.strip() for n in os.environ.get("AGENT_SELF_NAMES", "数字员工,Claude Code").split(",")
    if n.strip()
}

# 聚合 prompt 末句
_AGG_PROMPT_FOOTER = os.environ.get(
    "CAP_AGGREGATION_PROMPT_FOOTER",
    "以上是群里最近一段时间的多条消息。请理解整体语境，做一个简洁有条理的总结/回应"
    "（该答疑的答疑、该归纳的归纳），不要逐条复述。",
)

# 每会话缓冲：conv_id -> {"conv_type", "msgs": [(user, text, ts)], "timer", "seen": set}
_buffers = {}
_lock = threading.Lock()


def _flush(conv_id):
    """把某会话缓冲的消息组成 prompt → brain → 摘要发回群 → 清空。"""
    with _lock:
        buf = _buffers.pop(conv_id, None)
    if not buf or not buf["msgs"]:
        return
    if buf.get("timer"):
        try:
            buf["timer"].cancel()
        except Exception:
            pass
    msgs = buf["msgs"]
    conv_type = buf["conv_type"]
    log(f"aggregation: flush conv={conv_id[:12]} 共 {len(msgs)} 条")

    lines = [f"群里最近有 {len(msgs)} 条消息：\n"]
    for i, (user, text, ts) in enumerate(msgs):
        tstr = time.strftime("%H:%M:%S", time.localtime(ts))
        lines.append(f"[{i+1}] [{tstr}] {user}: {text}")
    lines.append("")
    lines.append(_AGG_PROMPT_FOOTER)
    prompt = "\n".join(lines)

    reply = generate_reply("群消息", prompt, ctx={"conv_id": conv_id, "conv_type": conv_type},
                           raw=True)
    if reply:
        send_reply(conv_id, conv_type, reply)
    else:
        log(f"aggregation: 大脑无回复 conv={conv_id[:12]}")


def _schedule_flush(conv_id):
    """给某会话起一个 window 后的 flush 定时器（daemon）。"""
    t = threading.Timer(_AGG_WINDOW, lambda: submit_handler(_flush, conv_id))
    t.daemon = True
    t.start()
    return t


def on_inbound(msg):
    """群文本入站：缓冲；满则立即 flush，否则等窗口。返回 True=已消费（不逐条回复）。

    单聊 / 数字员工自己发的 → 放行（return False），交给 text_reply 逐条处理。
    """
    if msg.conv_type != _GROUP_CONV_TYPE:
        return False  # 单聊不聚合
    if msg.user in _SELF_NAMES:
        return True   # 自己发的，消费掉不缓冲（防回环）
    if not msg.conv_id:
        return False

    flush_now = False
    with _lock:
        buf = _buffers.get(msg.conv_id)
        if buf is None:
            buf = {"conv_type": msg.conv_type, "msgs": [], "timer": None, "seen": set()}
            _buffers[msg.conv_id] = buf
        if msg.msg_id and msg.msg_id in buf["seen"]:
            return True   # 去重
        if msg.msg_id:
            buf["seen"].add(msg.msg_id)
        buf["msgs"].append((msg.user, msg.text, time.time()))
        if buf["timer"] is None:
            buf["timer"] = _schedule_flush(msg.conv_id)
        if len(buf["msgs"]) >= _AGG_MAX_MSGS:
            flush_now = True

    if flush_now:
        submit_handler(_flush, msg.conv_id)
    return True


def _reset():
    """测试用：清空缓冲 + 取消定时器。"""
    with _lock:
        for buf in _buffers.values():
            if buf.get("timer"):
                try:
                    buf["timer"].cancel()
                except Exception:
                    pass
        _buffers.clear()


CAPABILITY = Capability(
    name="aggregation",
    on_inbound=on_inbound,
    handles_kinds={KIND_TEXT},
    priority=90,              # 先于 text_reply(100) 接管群文本；晚于 question/image/forward
    default_enabled=False,    # 默认关：与逐条回复互斥，开了才接管群消息
)
register(CAPABILITY)
