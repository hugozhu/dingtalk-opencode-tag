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

import json
import os
import re
import threading
import time
from urllib.parse import quote

from core.agent_common import log, submit_handler
from custom import msgstore

# 同时最多几个识别在跑。task 池默认才 8 个 worker，且与 file/forward/audio 共用 ——
# 群里有人贴 20 张截图时，不设上限会把整个池占满，真正被 @ 的重活反而排不上。
_MAX_INFLIGHT = int(os.environ.get("AGENT_MEDIA_MAX_INFLIGHT", "2") or 2)
# 识别失败后多久允许重试（秒）。坏 mediaId 不该被每条追问反复重试。
_FAIL_TTL = int(os.environ.get("AGENT_MEDIA_FAIL_TTL", "60") or 60)
# 锁多久算过期（秒）。必须大于「下载 30s + 视觉 90s」，否则正常的慢识别会被别人抢走。
_LOCK_TTL = int(os.environ.get("AGENT_MEDIA_LOCK_TTL", "180") or 180)
# 别的进程正在识别同一张图时，最多等它多久（秒）
_PEER_WAIT = int(os.environ.get("AGENT_MEDIA_PEER_WAIT", "150") or 150)
_PEER_POLL = 0.5

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


def _lock_path(conv_id, msg_id, store):
    """`<store>/.locks/<conv_key>/<pct(msg_id)>.lock`

    放 msgstore 根下而不是仓库根：`clean_runtime_state()` 在冻结的 bin/core/lib.sh 里、
    加不了新 basename；而挂在 store 根下能白捡测试隔离 —— 测试注入 path/env 时锁跟着走，
    不会污染生产（这个仓库已经往真实 knowledge/ 里漏过三次测试数据）。
    `prune()` 只删形如 `YYYY-MM-DD.jsonl` 的文件，对 .locks/ 无感。
    """
    return os.path.join(store, ".locks", msgstore.conv_key(conv_id),
                        quote(str(msg_id), safe="") + ".lock")


def _stale(p):
    """这把锁是不是没人认领了？

    **TTL 先判，pid 后判** —— 两个条件是"或"不是"先后"：
      · 超过 TTL 一律可抢：持有者可能进程还在、但那次识别已经卡死（视觉服务不回、
        线程挂住）。只认 pid 会把锁焊死到进程退出为止。
      · 没超 TTL 但进程已经没了：重启/被 kill 的常见情形，不必干等满 3 分钟。
    """
    try:
        if time.time() - os.path.getmtime(p) > _LOCK_TTL:
            return True
    except OSError:
        return True                     # 文件刚好没了 = 可以抢
    try:
        with open(p, encoding="utf-8") as f:
            pid = int((json.loads(f.read() or "{}") or {}).get("pid") or 0)
    except (OSError, ValueError, TypeError):
        return False                    # 读不出持有者：保守当它活着，等 TTL
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return False                    # 还活着，别抢
    except ProcessLookupError:
        return True                     # 进程没了，锁是遗骸
    except OSError:
        return False                    # 别的用户的进程 —— 当它活着，等 TTL


def _acquire(conv_id, msg_id, store):
    """抢这张图的识别权。True=归我干，False=别人在干。

    用 `O_CREAT|O_EXCL` 而不是 flock —— AGENTS.md 明令禁止 flock（macOS 上有坑），
    文件存在性判断正是仓库既定的替代做法。
    """
    p = _lock_path(conv_id, msg_id, store)
    for attempt in (1, 2):
        try:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            try:
                os.write(fd, json.dumps({"pid": os.getpid(),
                                         "ts": int(time.time())}).encode())
            finally:
                os.close(fd)
            return True
        except FileExistsError:
            if attempt == 2 or not _stale(p):
                return False
            log(f"mediadesc: 抢占过期锁 {msg_id[:16]}")
            try:
                os.unlink(p)            # 只重试一次：抢不到就当别人在干
            except OSError:
                return False
        except OSError as e:
            log(f"mediadesc: 加锁失败（降级为不加锁）{e}")
            return True                 # 锁只是优化，坏了也不能让识别停摆
    return False


def _release(conv_id, msg_id, store):
    try:
        os.unlink(_lock_path(conv_id, msg_id, store))
    except OSError:
        pass


def _await_peer(conv_id, msg_id, store, timeout):
    """别人在识别 —— 轮询等它落盘，别重复干活。返回 (desc, status)。"""
    end = time.monotonic() + max(0.0, timeout)
    while True:
        desc, decided, err = _cached(conv_id, msg_id, store)
        if decided:
            return desc, ("" if desc else (err or "recognize"))
        if time.monotonic() >= end:
            return "", "pending"        # 等超了：降级，不是失败
        time.sleep(_PEER_POLL)


def _run(conv_id, msg_id, media_id, by, store):
    """真正干活：抢锁 → 复检 → 下载 → 识别 → 落盘。返回 (desc, err)。

    err 区分 download / recognize：**这两种失败对用户是不同的可操作建议**
    （"再发一次" vs "把关键内容打字发我"），合并成一条会让人无从下手。

    **跨进程单飞**：在途表是进程内的，而 bin/custom/convq.py 是独立进程 —— daemon 正在
    识别时 agent 调一次 convq image 就是第二次下载 + 第二次视觉调用，`Semaphore(2)` 的
    全局并发上限也会变成 2×N。所以这里再加一层文件锁。

    三处顺序是正确性本身，别动：
      1. **拿到锁之后复检缓存** —— 少了这步两个进程只是排队，活照样干两遍；
      2. **先落盘再解锁** —— 反过来的话对方会在空窗期看到"没锁也没结果"，又跑一遍；
      3. 失败也落盘（ok:false），于是 _FAIL_TTL 从"每进程一次"自动升级成"全局一次"。
    """
    from custom.capabilities.image import _download_image, _recognize
    if not _acquire(conv_id, msg_id, store):
        log(f"mediadesc: {msg_id[:16]} 已有进程在识别，等它")
        return _await_peer(conv_id, msg_id, store, _PEER_WAIT)
    try:
        desc, decided, err = _cached(conv_id, msg_id, store)
        if decided:
            return desc, ("" if desc else (err or "recognize"))
        desc, err = "", ""
        if not _sem.acquire(blocking=False):
            log(f"mediadesc: 并发已满（{_MAX_INFLIGHT}），跳过 {msg_id[:16]}")
            return "", "busy"       # 不落盘：这不是失败，是没轮上，之后还能再试
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
        msgstore.record_description(conv_id, msg_id, desc, by=by, ok=bool(desc),
                                    err=err, path=store)
        log(f"mediadesc: {'识别完成' if desc else '失败(' + err + ')'} {msg_id[:16]} "
            f"by={by} len={len(desc)}")
        return desc, err
    finally:
        _release(conv_id, msg_id, store)


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


def describe_sync(conv_id, msg_id, text, by="agent", path=None):
    """同 describe，但**在当前线程跑完**，不经过 task 池。给一次性 CLI 进程用。

    为什么不能复用 describe：`submit_handler` 背后的 ThreadPoolExecutor 注册了 atexit
    钩子，解释器退出时会 **join 所有 worker**。daemon 里无所谓，但在 convq 这种一次性
    进程里，`--wait 20` 超时返回后，进程会在退出时**再挂到视觉调用真正结束**（可能又是
    70 秒）。用户看到的是"命令打完结果还不退"。

    跨进程单飞仍然生效 —— 锁在 _run 里，跟哪个线程跑无关。
    """
    media_id = media_id_of(text)
    if not media_id or not msg_id:
        return "", "skip"
    store = path or msgstore._root()
    desc, decided, err = _cached(conv_id, msg_id, store)
    if decided:
        return desc, ("ok" if desc else (err or "empty"))
    desc, err = _run(conv_id, msg_id, media_id, by, store)
    return (desc or ""), ("ok" if desc else (err or "empty"))


# 测试用：清空在途表（描述缓存在 msgstore 里，由它自己的 path 注入隔离）
def _reset():
    with _inflight_lock:
        _inflight.clear()
