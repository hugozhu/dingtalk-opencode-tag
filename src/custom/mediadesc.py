"""mediadesc — 图片描述的单飞识别 + 落盘缓存（custom 层）

**同一张图只识别一次**，无论有几条路径同时想要它的描述。

为什么必须单飞：`at_mention` 是**每一份投递**的属性，不是消息的属性。线上同时开了群订阅
和 `DWS_EVENT_AT`，一条被 @ 的图会进来两份、到达顺序不定，**群流那份没有 @ 标记**
（group_gate 的 docstring 早就写死了这个坑）。所以任何"只处理没被 @ 的图"的过滤都不成立
—— 群流那份先到就会被当成"没人 @ 我的图"识别一次，@ 流那份再让 image 能力识别一次，
**一次 @ 图 = 两次下载 + 两次视觉调用**。

改成谁先到谁真跑、后来者 join 同一个 Future，这个竞态就消失了；顺带也解决了"预识别还
没跑完，追问又重新识别一遍"的浪费。

结果落 msgstore 的 desc 记录，所以**跨重启仍然复用**。失败也记（`ok:false` + 短 TTL），
否则一个坏 mediaId 会被时间窗内每一条追问反复重试，每次都是 30s 下载 + 视觉超时。
"""

import os
import re
import threading
import time

from core.agent_common import log, submit_handler
from custom import msgstore

# 同时最多几个识别在跑。task 池默认才 8 个 worker，且与 file/forward/audio 共用 ——
# 群里有人贴 20 张截图时，不设上限会把整个池占满，真正被 @ 的重活反而排不上。
_MAX_INFLIGHT = int(os.environ.get("AGENT_MEDIA_MAX_INFLIGHT", "2") or 2)
# 识别失败后多久允许重试（秒）。坏 mediaId 不该被每条追问反复重试。
_FAIL_TTL = int(os.environ.get("AGENT_MEDIA_FAIL_TTL", "60") or 60)

_MEDIA_ID_RE = re.compile(r"mediaId=([^)\s]+)")

_inflight = {}                      # (conv_id, msg_id) -> Future
_inflight_lock = threading.Lock()
_sem = threading.Semaphore(_MAX_INFLIGHT)


def media_id_of(text):
    """从消息正文里抽 mediaId；不是媒体消息返回 ""。"""
    m = _MEDIA_ID_RE.search(text or "")
    return m.group(1) if m else ""


def _cached(conv_id, msg_id, path=None):
    """已有的描述记录 → (desc, 是否有结论, err)。失败记录在 TTL 内也算"有结论"。"""
    rec = msgstore.description_of(conv_id, msg_id, path=path)
    if not rec:
        return None, False, ""
    if rec.get("ok"):
        return rec.get("text") or "", True, ""
    if time.time() - (rec.get("ts") or 0) < _FAIL_TTL:
        return "", True, rec.get("err") or "recognize"   # 刚失败过，短期内别再试
    return None, False, ""          # 失败很久了，可以重试


def _run(conv_id, msg_id, media_id, by, store):
    """真正干活：下载 → 识别 → 落盘。返回 (desc, err)。

    err 区分 download / recognize：**这两种失败对用户是不同的可操作建议**
    （"再发一次" vs "把关键内容打字发我"），合并成一条会让人无从下手。
    """
    from custom.capabilities.image import _download_image, _recognize
    desc, err = "", ""
    if not _sem.acquire(blocking=False):
        log(f"mediadesc: 并发已满（{_MAX_INFLIGHT}），跳过 {msg_id[:16]}")
        return "", "busy"           # 不落盘：这不是失败，是没轮上，之后还能再试
    try:
        img, tmp = _download_image(media_id, msg_id, conv_id)
        if not img:
            err = "download"
        else:
            desc = _recognize(img, tmp) or ""
            err = "" if desc else "recognize"
    except Exception as e:
        log(f"mediadesc: 识别异常 {e}")
        err = "recognize"
    finally:
        _sem.release()
    msgstore.record_description(conv_id, msg_id, desc, by=by, ok=bool(desc), err=err,
                                path=store)
    log(f"mediadesc: {'识别完成' if desc else '失败(' + err + ')'} {msg_id[:16]} "
        f"by={by} len={len(desc)}")
    return desc, err


def describe(conv_id, msg_id, text, wait=None, by="ondemand", path=None):
    """拿这张图的描述 → (desc, status)。

    status ∈ ok（有描述）| download（下载失败）| recognize（识别失败）|
             busy（并发已满，没跑）| pending（还在跑，没等到）| skip（不是媒体消息）

    wait=None 表示只发起、不等（预识别用）；wait=秒数 表示最多等这么久（追问用）。
    **绝不无限等**：调用方多半跑在 reply 池（默认只有 4 个 worker），等下去会让所有
    会话的回复一起排队饿死。
    """
    media_id = media_id_of(text)
    if not media_id or not msg_id:
        return "", "skip"

    # **把存储路径在这里定死**再传给后台线程：_run 跑在 task 池里，而 msgstore 的根目录
    # 是**调用时**读 env 的 —— 等它落盘时 env 可能已经变了（测试 tearDown 弹掉变量就会
    # 让描述写进真实的 knowledge/）。
    store = path or msgstore._root()
    desc, decided, err = _cached(conv_id, msg_id, store)
    if decided:
        return desc, ("ok" if desc else (err or "empty"))

    key = (conv_id, msg_id)
    with _inflight_lock:
        fut = _inflight.get(key)
        if fut is None:
            fut = submit_handler(_run, conv_id, msg_id, media_id, by, store)
            _inflight[key] = fut
            fut.add_done_callback(
                lambda _f, k=key: _inflight.pop(k, None))
    if wait is None:
        return "", "pending"
    try:
        desc, err = fut.result(timeout=wait)
    except Exception:
        return "", "pending"        # 超时/异常都当"还没好"，调用方降级即可
    return (desc or ""), ("ok" if desc else (err or "empty"))


# 测试用：清空在途表（描述缓存在 msgstore 里，由它自己的 path 注入隔离）
def _reset():
    with _inflight_lock:
        _inflight.clear()
