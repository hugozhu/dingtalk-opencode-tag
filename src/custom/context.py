"""context — 给这一轮推理拼上下文（custom 层，#112 步骤 2）

**引用优先、时间窗其次。**

真实缺口：群里有人发一张图（**不 @** 数字员工），紧接着 @ 它问「这个怎么理解」——
那张图被 group_gate 吞掉了，大脑只看到「这个怎么理解」，完全不知道在说什么。

    [group_gate] 群消息未 @ 我，不处理 text='[图片消息](mediaId=$iwEc…'

图其实一直在：msgstore 早就把它落盘了（#111），mediaId 几小时后仍可下载（实测 144KB
JPG 下载成功）。所以追问时回看一眼同会话最近的图，补进 prompt 即可。

两条来源的**证据强度完全不同**，措辞必须跟着变：

- **引用**是显式证据 —— 用户明确指了那条消息，可以说「用户是针对它说这句话的」
- **时间邻近**只是猜测 —— 4 分钟前的图 + 一句无关提问，说成「针对」会让模型强行编。
  所以这条路一律用推测语气，并明确授权模型忽略：「如果无关就完全忽略」

三条一起压误挂率：窗口只有 120s（不是 300s）、推测语气、跳过已经被单独回复过的图
（`by=="image"` 的那些描述已经在会话历史里，再塞一遍既重复又更容易误导）。

已知边界（明知故留）：

- **只在 supervisor_review 开着时生效**：唯一的调用点在 `_draft_and_forward` 里。关掉审核
  回路后提问走 core 的 text_reply，而 core 不能改。要覆盖得再加一个 custom 的
  `media_ctx`(priority=95) 能力并复制一遍 text_reply 的兜底逻辑，性价比不高。
  #112 的引用上下文本来就是同样的缺口。
- **陈旧误挂消不干净**：2 分钟前的图 + 一句无关提问，模型仍可能强行联系。上面三条只是
  把概率压低。
- **`from` 是显示名不是 userId**（bridge 只传 sender），同名同事会串。
- `[文件] xxx.png` 这种以文件形式发的图是 KIND_FILE，不在回看范围内。
"""

import os
import time

from core.agent_common import log
from custom import convq, mediadesc, msgstore, quoted

# 回看窗口（秒）。刻意短：越长越容易把无关的旧图挂到新问题上。
_WINDOW = int(os.environ.get("AGENT_MEDIA_LOOKBACK_SEC", "120") or 120)
# 最多带几张（有人连发几张再问"这几张什么意思"）
_MAX_IMAGES = int(os.environ.get("AGENT_MEDIA_LOOKBACK_MAX", "3") or 3)
# 等识别的总预算（秒）。**默认 0 = 不等**：识别照常发起，但这一轮不为它堵着 reply 池
# （默认只有 4 个 worker，等下去所有会话的回复一起排队饿死）。没赶上就在 prompt 里给出
# convq 命令，让大脑自己去取 —— 它有自己的 300s 预算，比这里宽裕得多。
# 设回 20 就是 2026-08-18 之前的行为，这是**回滚闸门**。
# 注意别复用 AGENT_MEDIA_WAIT_SEC：capabilities/image.py 用同一个名字但默认 120，
# 共用会让"关掉这里的等待"顺手把图片识别的等待也砍到 0。
_WAIT = int(os.environ.get("AGENT_CONTEXT_WAIT_SEC", "0") or 0)


def build(user, text, conv_id, quoted_msg_id=None, exclude_msg_id=None):
    """拼这一轮的 prompt → `(prompt, raw)`；没有额外上下文就返回 `(text, False)`。

    **整块吞异常**：这里抛出去会让卡片发不出、提问者收不到任何东西、ack 停在「处理中」
    周期播报 —— 症状和 #109 一模一样。少一段上下文远好过整条消息处理不了。
    """
    try:
        return _build(user, text, conv_id, quoted_msg_id, exclude_msg_id)
    except Exception as e:                          # noqa: BLE001 — 兜底就是要吞
        log(f"context: 组装上下文失败，按原文处理 {e}")
        return text, False


def _build(user, text, conv_id, quoted_msg_id, exclude_msg_id):
    if quoted_msg_id:
        q = quoted.resolve(conv_id, quoted_msg_id, media_wait=_WAIT)
        if q:
            log(f"context: 已补入被引用消息 from={q.get('sender')!r} "
                f"media={q.get('media')} desc={len(q.get('desc') or '')}")
            return quoted.build_prompt(user, text, q), True
        # 引用取不到（老消息、CLI 挂了）时继续走时间窗：被引用的那条多半也在窗口里

    shots = _lookback(conv_id, exclude_msg_id)
    if not shots:
        return text, False
    log(f"context: 回看补入 {len(shots)} 张图 conv={conv_id[:12]} "
        f"pending={sum(1 for s in shots if s['pending'])}")
    return _lookback_prompt(user, text, shots, conv_id), True


def _lookback(conv_id, exclude_msg_id):
    """窗口内最近的图 → `[{"desc", "pending"}]`，**时间正序**（先发的排前面）。"""
    cands = msgstore.recent_media(conv_id, within_sec=_WINDOW, limit=_MAX_IMAGES)
    cands = [c for c in cands if c.get("id") and c.get("id") != exclude_msg_id]
    cands.reverse()                                 # recent_media 是新→旧
    out = []
    deadline = time.monotonic() + _WAIT             # 预算是**所有图片共享**的
    for c in cands:
        rec = msgstore.description_of(conv_id, c["id"])
        if rec and rec.get("ok") and rec.get("by") == "image":
            continue        # image 能力已经就它单独回复过，描述在会话历史里了
        left = deadline - time.monotonic()
        # wait=None 仍然**发起**识别：premedia 和 daemon 侧单飞照旧受益，等大脑真的
        # 跑 convq image 时多半直接命中缓存，或者 join 上同一把锁
        desc, st = mediadesc.describe(conv_id, c["id"], c.get("text") or "",
                                      wait=left if left > 0 else None,
                                      by="ondemand")
        if desc:
            out.append({"msg_id": c["id"], "desc": desc, "pending": False})
        elif st in ("pending", "busy"):
            out.append({"msg_id": c["id"], "desc": "", "pending": True})
        # download / recognize 失败就当没这张图：跟模型说"有张图但读不出来"帮不上忙，
        # 只会让它绕着这个不存在的信息编话
    return out


def _lookback_prompt(user, text, shots, conv_id=""):
    """回看图片 + 用户原话 → 结构化 prompt（配 generate_reply 的 raw=True）。"""
    n = len(shots)
    blocks = []
    for i, s in enumerate(shots, 1):
        head = f"【图片 {i}/{n}】" if n > 1 else "【图片】"
        blocks.append(f"{head} msg={s['msg_id']}")
        if not s["pending"]:
            blocks.append(s["desc"])
        else:
            # **死胡同 + 一扇标好的门**。只说"还在识别中"是在邀请模型编内容。
            blocks.append(
                "（内容正在识别中。**需要它的内容时先运行下面这条命令**，它会等到识别"
                "完成再返回：\n  "
                + convq.cmd_hint(conv_id, "image", s["msg_id"])
                + "\n 在拿到结果之前，不要猜测这张图里是什么。）")
        blocks.append("")
    return "\n".join([
        "【刚才同一会话里发过的图片】",
        *blocks,
        f"【{user} 说】",
        text,
        "",
        "上面这些图片是刚才有人在同一个会话里发的，**可能**和用户的问题有关，也可能无关 "
        "—— 由你判断。如果无关就完全忽略它们，不要强行建立联系，也不要提起它们。",
    ])
