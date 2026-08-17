"""msgstore — 消息落盘存储（custom 层，#111）

**收到即落盘**：任何会话进出的每条消息都写进本地文件，事后按 msgId 查回是一次本地查表。

为什么需要它：在此之前数字员工其实**把钉钉当存储**——主管引用一条旧消息做裁决时，靠
bridge 从被引用正文里抽「待审 #N」、靠 `list-by-ids` 回读正文、靠审核流水的 256KB 尾窗
回放。于是没有「待审 #N」标记的消息（提问者原话、发到群里的最终答复、任何别的会话）
完全定位不了；超出尾窗就查不到；文件被误删还会静默退回 #1 与历史撞号。

目录结构（issue #111 方案 B）：

    <AGENT_MSGSTORE_DIR>/<conv_key>/<YYYY-MM-DD>.jsonl

- `conv_key` = `urllib.parse.quote(conv_id, safe="")`。openConversationId 形如
  `cidQUwzlI5Y+edy9…4g=`，含 `+ / =`，**不能直接当目录名**。百分号编码可逆、是标准库
  最省事的做法，且目录名仍能一眼认出是哪个会话（比 hash 强）。
- 按天分片：既是查询的局部性单位，也是**保留策略的删除单位**——过期整文件删掉，
  不用改写任何文件。

**为什么不需要全局 msgId 索引**：两条查询入口天然自带会话——引用回复的入站消息带
conv_id（被引用的那条必在同一会话里），reaction 事件带 conversation_id。所以查一条
消息 = 在它所属会话的分片里从最新一天往回扫，扫描范围由保留天数封顶。

记录两类：
    {"t":"msg","dir":"in|out","id":..,"conv":..,"ct":..,"from":..,"kind":..,"text":..,"ts":..}
    {"t":"fb","id":<提问者原始消息 id>,"seq":N,"action":..,"answer":..,"by":..,"ts":..}

`ts` 存 **epoch 秒**而不是格式化字符串：守护进程由 reboot.sh 以 `env -i` 拉起、不继承
TZ，跑在 UTC，而钉钉给的时间戳是本地时区——存字符串必然埋雷（已经因此静默失效过一次，
见 commit 531d039）。

**已知缺口**：发给主管的待审卡片**不在库里**。现网单聊订阅用的是
`user_im_message_receive_o2o_all`（"当前用户**收到**的所有单聊消息"），自己发出的单聊
不回显；群订阅是双向的，所以发到群里的答复能入库。卡片仍靠正文里的「待审 #N」标记定位。
补齐的办法是加订 `user_im_message_receive_o2o --user <主管>`（双向），已评估，暂不做。

开关：CAP_MSGSTORE_ENABLED（默认开，见 capabilities/msgstore_cap.py）。
"""

import json
import os
import re
import threading
import time
from urllib.parse import quote, unquote

from core.agent_common import log

# 存储根目录（相对 PROJECT_DIR）。放 knowledge/ 下而不是根目录点文件——后者会被
# bin/core/lib.sh 的 clean_runtime_state() 在 stop/reboot 时删掉。knowledge/ 已整目录
# gitignore（这里存的是真实对话内容）。
_DEFAULT_DIR = "knowledge/messages"
# 保留天数：超过就整个分片文件删掉
_KEEP_DAYS = int(os.environ.get("AGENT_MSGSTORE_KEEP_DAYS", "30") or 30)
# 单条正文上限（超出截断并标记）。合并转发/长草稿可能很大，别让一条记录撑爆分片。
_TEXT_MAX = int(os.environ.get("AGENT_MSGSTORE_TEXT_MAX", "4000") or 4000)

_write_lock = threading.Lock()
# 分片文件名：只有形如 2026-08-17.jsonl 的才是我们自己写的（prune 靠它防呆）
_SHARD_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.jsonl$")


def _root(path=None):
    """存储根目录绝对路径。

    **env 在调用时读、不在 import 期定型**（同 custom/identity 的做法）：这个模块会被
    supervisor_review 间接调用，测试若只设了 os.environ 而没 patch 模块属性，import 期
    定型就会让测试**写进真实的 knowledge/**（已经踩过一次：单测把 266 条夹具数据写进了
    生产目录）。path 参数仍保留，供需要精确控制的用例。
    """
    p = path or os.environ.get("AGENT_MSGSTORE_DIR", _DEFAULT_DIR)
    if os.path.isabs(p):
        return p
    return os.path.join(os.environ.get("PROJECT_DIR", os.getcwd()), p)


def conv_key(conv_id):
    """会话 id → 安全目录名（可逆）。

    `quote(safe="")` 把 `+ / =` 编成 `%2B %2F %3D`，字母数字与 `_.-~` 原样保留，
    所以目录名仍然认得出是哪个会话。空 conv_id 归到 `_unknown`，避免写到根目录下。
    """
    if not conv_id:
        return "_unknown"
    return quote(str(conv_id), safe="")


def conv_id_of(key):
    """目录名 → 会话 id（conv_key 的逆运算，排查时用）。"""
    return "" if key == "_unknown" else unquote(key)


def _shard(conv_id, day=None, path=None):
    """某会话某天的分片文件路径。"""
    d = day or time.strftime("%Y-%m-%d")
    return os.path.join(_root(path), conv_key(conv_id), f"{d}.jsonl")


def _append(conv_id, rec, path=None):
    """往分片追加一条记录。返回 True=已落盘。

    **模块锁 + 单 fd 内写完**，不能照抄 knowledge 那种无锁 append：一条记录可能带几千字
    的正文，Python 的 write() 会拆成多次 write(2)，两个线程并发追加就会把长记录撕开、
    互相插进对方中间（reply pool / task pool / log-tail 都会写）。
    """
    line = (json.dumps(rec, ensure_ascii=False) + "\n").encode("utf-8")
    p = _shard(conv_id, path=path)
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with _write_lock:
            fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                written = 0
                while written < len(line):
                    written += os.write(fd, line[written:])
            finally:
                os.close(fd)
        return True
    except OSError as e:
        log(f"msgstore: 写入失败 {e}")
        return False


def _clip(text):
    """正文截断 → (文本, 是否截断过)。"""
    t = text or ""
    if len(t) <= _TEXT_MAX:
        return t, False
    return t[:_TEXT_MAX], True


def record(msg, direction="in", path=None):
    """记一条消息。direction: in=别人发的 / out=自己发的（经群订阅回显进来的）。"""
    if not getattr(msg, "msg_id", ""):
        return False        # 没有 id 就无从查回，存了也没用
    text, trunc = _clip(getattr(msg, "text", ""))
    rec = {"t": "msg", "dir": direction, "id": msg.msg_id,
           "conv": getattr(msg, "conv_id", ""), "ct": getattr(msg, "conv_type", ""),
           "from": getattr(msg, "user", ""), "kind": getattr(msg, "kind", ""),
           "text": text, "ts": int(time.time())}
    if trunc:
        rec["trunc"] = True
    return _append(rec["conv"], rec, path=path)


def record_feedback(conv_id, msg_id, seq=None, action="", answer="", by="", path=None):
    """记一条"这条消息最后被主管怎么处理了"。

    挂在**提问者那条原始消息**上——那才是人事后会去查的东西（"我问的那句后来怎么样了"）。
    """
    if not msg_id:
        return False
    ans, trunc = _clip(answer)
    rec = {"t": "fb", "id": msg_id, "conv": conv_id, "seq": seq, "action": action,
           "answer": ans, "by": by, "ts": int(time.time())}
    if trunc:
        rec["trunc"] = True
    return _append(conv_id, rec, path=path)


def _shard_days(conv_id, path=None):
    """该会话已有的分片日期，**新→旧**。"""
    d = os.path.join(_root(path), conv_key(conv_id))
    try:
        days = [m.group(1) for m in
                (_SHARD_RE.match(n) for n in os.listdir(d)) if m]
    except OSError:
        return []
    return sorted(days, reverse=True)


def find(conv_id, msg_id, path=None):
    """按 msgId 查回一条消息记录；查不到返回 None。

    从最新一天往回扫，**同一天内取最后一条**（同 id 可能被重投写过多次，最后一条最新）。
    扫描范围由保留天数封顶，不会随时间无限变慢。
    """
    if not msg_id:
        return None
    for day in _shard_days(conv_id, path=path):
        hit = None
        p = _shard(conv_id, day=day, path=path)
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                for ln in f:
                    ln = ln.strip()
                    if not ln:
                        continue
                    # 不做"子串预筛"式的优化：那要假设 id 在行里字面出现，一旦某个写入方
                    # 用了 ensure_ascii（把中文转成 \uXXXX）或 id 含被转义的字符，就会
                    # **静默漏查**。分片按"会话+天"切过，逐行解析的开销可以接受。
                    try:
                        r = json.loads(ln)
                    except ValueError:
                        continue        # 坏行跳过：查询是尽力而为，不该因一行坏了就失败
                    if isinstance(r, dict) and r.get("t") == "msg" and r.get("id") == msg_id:
                        hit = r
        except OSError:
            continue
        if hit:
            return hit
    return None


def feedback_of(conv_id, msg_id, path=None):
    """这条消息的裁决反馈（最后一条）；没有返回 None。"""
    if not msg_id:
        return None
    for day in _shard_days(conv_id, path=path):
        hit = None
        try:
            with open(_shard(conv_id, day=day, path=path), "r",
                      encoding="utf-8", errors="replace") as f:
                for ln in f:
                    ln = ln.strip()
                    if not ln:
                        continue
                    try:
                        r = json.loads(ln)
                    except ValueError:
                        continue
                    if isinstance(r, dict) and r.get("t") == "fb" and r.get("id") == msg_id:
                        hit = r
        except OSError:
            continue
        if hit:
            return hit
    return None


def prune(keep_days=None, path=None):
    """删掉过期分片，返回删了几个。

    **只删自己目录下形如 YYYY-MM-DD.jsonl 的文件**（照 file._cleanup_tmp 的防呆思路）：
    这个目录将来可能被人塞别的东西，无脑按 mtime 删会误伤。
    """
    keep = keep_days if keep_days is not None else _KEEP_DAYS
    if keep <= 0:
        return 0
    cutoff = time.strftime("%Y-%m-%d", time.localtime(time.time() - keep * 86400))
    root = _root(path)
    removed = 0
    try:
        convs = os.listdir(root)
    except OSError:
        return 0
    for c in convs:
        d = os.path.join(root, c)
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for n in names:
            m = _SHARD_RE.match(n)
            if not m or m.group(1) >= cutoff:
                continue
            try:
                os.remove(os.path.join(d, n))
                removed += 1
            except OSError as e:
                log(f"msgstore: 删除过期分片失败 {n} {e}")
    if removed:
        log(f"msgstore: 已清理 {removed} 个超过 {keep} 天的分片")
    return removed
