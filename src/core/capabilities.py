"""capabilities.py — 能力注册表（core 稳定层）

让钉钉数字员工的业务能力（文本回复 / 合并转发 / 图片识别 / Question 交互 / 群聊
聚合 …）成为**可组装、可选配的插件**：

- 每个能力是 custom 层的一个模块，声明一个 `Capability` 并 `register()` 进来。
- core 只认注册表，不认具体能力 —— 加/删能力不动 core，能干净 merge upstream。
- 每个能力一个 `CAP_<NAME>_ENABLED` 环境变量开关；关掉的能力压根不注册。
- core 把入站消息/SSE 事件/cleanup 事件交给注册表**按序分发**，短路于第一个
  声明"已消费"（返回 True）的能力。

分发点（core 调用）：
- `dispatch_inbound(msg)`  —— log-tail 解析出的 InboundMessage（按 kind 路由）
- `dispatch_sse(event, port, pwd)` —— serve SSE 事件
- `dispatch_cleanup(event, state, lock)` —— spurious 轮次清理状态机
- `dispatch_reply_sent(conv_id, conv_type, ok)` —— 回复发出后通知（custom.replier 调用）

能力 hook 约定（都可选；不实现即不参与该分发）：
- `on_inbound(msg) -> bool`         True=已消费，停止继续分发
- `on_sse_event(event, port, pwd) -> bool`
- `on_cleanup(event, state, lock) -> bool`
- `on_reply_sent(conv_id, conv_type, ok) -> None`   通知型，不短路（所有能力都收到）
"""

import os
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Callable, Optional, Set

from core.agent_common import env_flag, log


@dataclass
class Capability:
    """一个可选配的业务能力。

    Attributes:
        name: 能力名（小写短横/下划线），也用于开关 CAP_<NAME_UPPER>_ENABLED
        on_inbound: 处理入站消息，返回 True 表示已消费（registry 停止继续分发）
        handles_kinds: 只把这些 kind 的 InboundMessage 派给本能力；空集=全部 kind
        classify_line: 可选，识别本能力的**特殊日志格式**（如合并转发 chatRecord）。
            签名 (line: str) -> InboundMessage | None。core 只认标准 "收到 @user" 格式
            （inbound.parse_line）；特殊格式由能力自带正则/状态机在此产出 InboundMessage。
            可有状态（跨行），故只应由 core 的 log-tail 单线程调用。
        on_sse_event: 处理 serve SSE 事件，返回 True 表示已消费
        on_cleanup: spurious 轮次清理 hook，返回 True 表示已消费
        on_reply_sent: 回复已发出通知 hook（replier.send_reply 后调用）。签名
            (conv_id, conv_type, ok) -> None；ok 表示发送是否成功。**通知型**，返回值
            忽略、不短路（所有能力都会收到）。ack 回执能力据此把"处理中"表情换成完成/失败。
        priority: 分发顺序，**小的先**（catch-all 的文本回复用较大值兜底）
        default_enabled: 未设开关环境变量时的默认启用状态
        loop_guard: True 时 dispatch_inbound 在调 on_inbound 前跳过“数字员工自己发的”消息
            （msg.user ∈ AGENT_SELF_NAMES），能力无需各自写防回环样板。
        dedup: True 时 dispatch_inbound 按 msg.msg_id **每能力独立**去重（有界 FIFO），
            见过的直接跳过，能力无需各自写 _seen 样板。注意：需要“见过但未处理→升级”这类
            自定义去重语义的能力（如 ack 的 read/begun 三态）不要用本项，自己在 on_inbound 内处理。
    """
    name: str
    on_inbound: Optional[Callable] = None
    handles_kinds: Set[str] = field(default_factory=set)
    classify_line: Optional[Callable] = None
    on_sse_event: Optional[Callable] = None
    on_cleanup: Optional[Callable] = None
    on_reply_sent: Optional[Callable] = None
    priority: int = 100
    default_enabled: bool = True
    loop_guard: bool = False
    dedup: bool = False

    def enabled(self):
        """读 CAP_<NAME>_ENABLED 开关；未设置用 default_enabled。"""
        return env_flag(f"CAP_{self.name.upper()}_ENABLED", default=self.default_enabled)


# 注册表（进程内单例）。custom 能力模块在 import 时 register()。
_registry = []
_lock = threading.Lock()


def register(cap):
    """注册一个能力。重复 name 覆盖旧的（便于测试重载）。"""
    with _lock:
        global _registry
        _registry = [c for c in _registry if c.name != cap.name]
        _registry.append(cap)
        _registry.sort(key=lambda c: c.priority)
    log(f"capability 注册: {cap.name} (priority={cap.priority}, "
        f"default_enabled={cap.default_enabled})")


def clear():
    """清空注册表（测试用）。"""
    with _lock:
        _registry.clear()
    _dedup_clear()


# ---------------------------------------------------------------------------
# 声明式预处理：防回环（loop_guard）+ 去重（dedup）—— 供能力零样板复用
# ---------------------------------------------------------------------------

# 防回环：数字员工自己的发送名（AGENT_SELF_NAMES，逗号分隔）。每次读取（便于测试改 env）。
def _self_names():
    return {
        n.strip()
        for n in os.environ.get("AGENT_SELF_NAMES", "数字员工,Claude Code").split(",")
        if n.strip()
    }


# 去重：每能力独立命名空间的有界 FIFO（避免能力间 msgId 互相干扰）。
_DEDUP_MAX = int(os.environ.get("CAP_DEDUP_MAX", "2048"))
_dedup = {}                 # cap_name -> OrderedDict(msg_id -> None)
_dedup_lock = threading.Lock()


def _dedup_seen(cap_name, msg_id):
    """本能力是否已见过该 msg_id（见过返回 True）。空 msg_id 不去重（放行）。"""
    if not msg_id:
        return False
    with _dedup_lock:
        seen = _dedup.get(cap_name)
        if seen is None:
            seen = _dedup[cap_name] = OrderedDict()
        if msg_id in seen:
            return True
        seen[msg_id] = None
        if len(seen) > _DEDUP_MAX:
            seen.popitem(last=False)
    return False


def _dedup_clear():
    """清空去重状态（测试用）。"""
    with _dedup_lock:
        _dedup.clear()


def enabled_capabilities():
    """返回当前启用的能力列表（按 priority 排序）。"""
    with _lock:
        caps = list(_registry)
    return [c for c in caps if c.enabled()]


def classify_line(line):
    """让启用能力尝试识别自己的特殊日志格式，返回首个非 None 的 InboundMessage。

    core 的 log-tail 对每行先调本函数（能力自带格式，如合并转发 chatRecord），
    没能力认领再回退 core 内置的标准解析（inbound.parse_line）。按 priority 顺序，
    第一个产出 InboundMessage 的能力胜出。能力的 classify_line 可有跨行状态，故本
    函数只应由 log-tail 单线程调用。
    """
    for cap in enabled_capabilities():
        if cap.classify_line is None:
            continue
        try:
            msg = cap.classify_line(line)
            if msg is not None:
                return msg
        except Exception as e:
            log(f"capability {cap.name} classify_line err: {e}")
    return None


def dispatch_inbound(msg):
    """把 InboundMessage 交给启用能力按序分发。返回 True 表示被某能力消费。

    按 kind 路由：能力 handles_kinds 非空时，只有 msg.kind 命中才调用它。
    声明式预处理（调 on_inbound 前）：
      - loop_guard=True：msg.user ∈ AGENT_SELF_NAMES → 跳过本能力（防自问自答）。
      - dedup=True：本能力已见过 msg.msg_id → 跳过（每能力独立有界 FIFO 去重）。
    短路：第一个 on_inbound 返回 True 的能力消费该消息，后续不再收到。
    """
    for cap in enabled_capabilities():
        if cap.on_inbound is None:
            continue
        if cap.handles_kinds and msg.kind not in cap.handles_kinds:
            continue
        if cap.loop_guard and msg.user and msg.user in _self_names():
            continue
        if cap.dedup and _dedup_seen(cap.name, msg.msg_id):
            continue
        try:
            if cap.on_inbound(msg):
                return True
        except Exception as e:
            log(f"capability {cap.name} on_inbound err: {e}")
    return False


def dispatch_sse(event, port, password):
    """SSE 事件按序分发。返回 True 表示被某能力消费（core 不再默认转发）。"""
    for cap in enabled_capabilities():
        if cap.on_sse_event is None:
            continue
        try:
            if cap.on_sse_event(event, port, password):
                return True
        except Exception as e:
            log(f"capability {cap.name} on_sse_event err: {e}")
    return False


def dispatch_cleanup(event, state, lock):
    """cleanup 事件按序分发。返回 True 表示被某能力消费。"""
    for cap in enabled_capabilities():
        if cap.on_cleanup is None:
            continue
        try:
            if cap.on_cleanup(event, state, lock):
                return True
        except Exception as e:
            log(f"capability {cap.name} on_cleanup err: {e}")
    return False


def dispatch_reply_sent(conv_id, conv_type, ok):
    """回复发出通知按序广播给所有启用能力（通知型，不短路）。

    custom.replier.send_reply 发送后调用。ack 回执能力据此把"处理中"表情换成
    完成/失败。异常隔离——单个能力抛错不影响其它能力，也绝不回传给 replier
    （回执是 best-effort，不能拖累正常回复链路）。
    """
    for cap in enabled_capabilities():
        if cap.on_reply_sent is None:
            continue
        try:
            cap.on_reply_sent(conv_id, conv_type, ok)
        except Exception as e:
            log(f"capability {cap.name} on_reply_sent err: {e}")
