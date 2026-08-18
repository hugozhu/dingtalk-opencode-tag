"""premedia — 群里的图**先识别好放着**（custom 能力，#112 步骤 3，默认关）

`context.py` 的回看已经能在追问时补上图片内容了，但那是**同步**的：追问到达时才开始
下载 + 视觉调用，主管/提问者要多等这一轮（20s 预算内等不到就只能降级成「识别中」）。
本能力把这一轮提前到图片刚发出来的那一刻 —— 追问到达时描述已经躺在 msgstore 里，
`describe()` 直接命中缓存，零等待。

**恒 `return False`**：它不消费消息、不回复任何人，只是让后面的 group_gate 照常把这条
未 @ 的图吞掉。唯一的副作用是往 msgstore 写一条 desc 记录。

## 为什么默认关

这是全仓第一个**为没人跟它说话的消息花钱**的能力，正好与 group_gate「没 @ 我就不动」
的哲学相反 —— 一个活跃群里每天几百张截图，全识别一遍是真金白银的视觉调用，而其中绝大
多数永远不会有人追问。按 supervisor_review 的先例，要显式开：

    export CAP_PREMEDIA_ENABLED=1        # config/constants.local.sh

## 为什么不判 at_mention

**`at_mention` 是每一份投递的属性，不是消息的属性。** 群订阅和 DWS_EVENT_AT 同开时一条
被 @ 的图会进来两份、到达顺序不定，群流那份没有标记。所以「只预识别没被 @ 的图」这个
过滤根本不成立。不判它，重复识别由 `mediadesc` 的单飞在下游解决（谁先到谁真跑，后来者
join 同一个 Future）—— 这是**唯一**正确的层次，不要试图在这里分流。
"""

import os
import threading
import time

from core.agent_common import log
from core.capabilities import Capability, register
from core.inbound import KIND_IMAGE
from custom import mediadesc

CAPABILITY_NAME = "premedia"

# 每会话每 5 分钟最多预识别几张。有人一口气贴 20 张截图不该打爆视觉模型 —— 这些图
# **没人在等**，让它们排在真正被 @ 的重活前面是本末倒置。
_RATE = int(os.environ.get("PREMEDIA_RATE_PER_5MIN", "6") or 6)
_WINDOW = 300.0

_buckets = {}                   # conv_id -> [时间戳]（该窗口内已发起的）
_bucket_lock = threading.Lock()


def _allow(conv_id):
    """令牌桶：该会话在窗口内还有额度吗？"""
    if _RATE <= 0:
        return False
    now = time.monotonic()
    with _bucket_lock:
        hits = [t for t in _buckets.get(conv_id, []) if now - t < _WINDOW]
        if len(hits) >= _RATE:
            _buckets[conv_id] = hits
            return False
        hits.append(now)
        _buckets[conv_id] = hits
        return True


def on_inbound(msg):
    """看到群里的图就发起识别，**永远返回 False**（不消费，交给后面的能力）。"""
    if msg.conv_type != "2":
        return False            # 单聊的图 image 能力总会识别，不用预热
    if not mediadesc.media_id_of(msg.text or ""):
        return False
    if not _allow(msg.conv_id):
        log(f"premedia: 会话预识别已限流（{_RATE}/5min），跳过 {(msg.msg_id or '')[:16]}")
        return False
    try:
        # wait=None：**只发起、不等**。这条消息没人在等回复，占住 worker 毫无意义。
        mediadesc.describe(msg.conv_id, msg.msg_id, msg.text, wait=None, by="premedia")
    except Exception as e:      # noqa: BLE001 — 预热失败绝不能影响这条消息的正常处理
        log(f"premedia: 发起识别失败 {e}")
    return False


# 测试用：清空令牌桶
def _reset():
    with _bucket_lock:
        _buckets.clear()


CAPABILITY = Capability(
    name=CAPABILITY_NAME,
    on_inbound=on_inbound,
    handles_kinds={KIND_IMAGE},
    priority=-5,             # msgstore(-10) 之后（要先落盘）、group_gate(2) 之前
    default_enabled=False,   # 显式开：全仓唯一"为没人跟它说话的消息花钱"的能力
    loop_guard=True,         # 自己发出去的图不预识别
    dedup=True,              # 同一 msgId 只发起一次（单飞是第二道保险，不是第一道）
)
register(CAPABILITY)
