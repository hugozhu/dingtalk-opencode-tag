"""convq — 让大脑自己查落盘的会话记录（custom 层）

## 为什么有这个东西

以前的做法是**抢在大脑前面把上下文预烤好**塞进 prompt。2026-08-18 14:27 的真实事故：

    14:27:45  群里发一张图（未 @）→ group_gate 吞掉，msgstore 落盘
    14:28:12  同一人 @ 提问「这个图你统计下」→ context 回看，发起识别
    14:28:32  20s 预算用尽 → prompt 写「这张图还在识别中，暂时拿不到内容」
    14:28:36  识别完成（475 字 OCR）—— 晚了 4 秒

答案最后是对的，但**不是因为设计对**：opencode agent 自己跑去读了
`knowledge/messages/…/2026-08-18.jsonl` 把 desc 记录翻了出来（它的 reasoning 里写着
「Confirmed: desc matches the image message ID…」）。换个不肯翻文件的模型就只会回
「图还在识别中」。

所以：**别再拿 4 个 worker 的 reply 池去跟视觉模型赛跑**。把落盘数据做成 AI 读得懂的
查询接口，让大脑自己决定要什么、自己去取。它已经在这么干了，这里只是把那个本能改道
到一个稳定、按会话限定、有输出上限的正门。

## AI Ready 体现在哪

存储侧一个字节没改（jsonl 仍是追加日志）。"AI Ready" 全在**查询时的渲染**：

- `desc`（图片识别）和 `fb`（主管裁决）join 到它们所属的消息上 —— 裸读 jsonl 拿到的是
  三种散落的记录，要自己对 id
- epoch 秒渲染成**绝对时间 + 相对时间**：大脑没有可靠的"现在"，相对时间才解得开
  「刚才」；绝对时间才能和日志对上号
- `dir=="out"` 渲染成「我」：否则模型把自己过去的回复当成第三方断言，反复推翻
- `mediaId=$iwEc…` 换成 `[图片]`，但保留 `msg=` —— mediaId 对文本模型是纯 token 浪费，
  msg_id 才是 `convq image` 的把手
- 截断**永远带可见尾注**：静默截断会让模型以为"没有更多了"然后停止追问

## 边界

**防误不防敌**：`--conv` 必填、没有"全部会话"模式、没有列举会话的子命令，输出有硬上限。
这挡的是"顺手把别的群捞进答案里"这类事故，挡不住一个铁了心的 agent —— 它仍然有 bash。
真正的收紧要靠 `AGENT_OPENCODE_PERMISSION`（机制早已写好并有 e2e，只是没启用）。
"""

import argparse
import json
import os
import sys
import time

from custom import mediadesc, msgstore

# 本文件在 src/custom/ 下 → 上两级是仓库根
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CLI_PATH = os.path.join(_REPO_ROOT, "bin", "custom", "convq.py")

EXIT_OK, EXIT_ARGS, EXIT_FAILED, EXIT_NOT_FOUND, EXIT_TIMEOUT = 0, 2, 3, 4, 5

# main() 入口固定下来的"真正的输出流"。**core 的 log() 是 print 到 stdout 的**，
# 而 core 不能改 —— 不把两条流分开，大脑 `convq image ... | ...` 拿到的正文里就会
# 掺进「[agent] mediadesc: 识别完成…」这样的日志行。
_OUT = None


def _write(s):
    (_OUT or sys.stdout).write(s)

# 上限：默认值够用，硬上限防止"给我全部"把大脑的上下文撑爆
_LIMIT_MAX, _DAYS_MAX, _BYTES_MAX = 200, 30, 64000
_TEXT_MAX = 4000                    # 与 msgstore._TEXT_MAX 对齐
_IMAGE_WAIT_MAX = 240               # brain 的 idle watchdog 是 300s，留足余量


def cmd_hint(conv_id, sub="recent", *args):
    """给提示词用的可直接运行的命令串。

    conv_id / msg_id 含 `+ / =`，**必须单引号**，否则大脑复制过去就是一条坏命令。
    """
    parts = [f"python3 {CLI_PATH}", sub]
    parts += [f"'{a}'" for a in args]
    parts.append(f"--conv '{conv_id}'")
    return " ".join(parts)


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------

def _rel(ts, now=None):
    """相对时间。大脑没有可靠的"现在"，「刚才」只能靠这个解开。"""
    d = int((now or time.time()) - (ts or 0))
    if d < 0:
        return "刚刚"
    if d < 60:
        return f"{d}秒前"
    if d < 3600:
        return f"{d // 60}分钟前"
    if d < 86400:
        return f"{d // 3600}小时前"
    return f"{d // 86400}天前"


def _cap(text, n):
    """截断 → (文本, 是否截过)。"""
    t = (text or "").replace("\n", " ").strip()
    return (t, False) if len(t) <= n else (t[:n], True)


def _who(rec):
    """`dir=="out"` → 我。不这么渲染，模型会把自己过去的回复当第三方断言反复推翻。"""
    return "我" if rec.get("dir") == "out" else (rec.get("from") or "某人")


def _body(rec, text_max):
    """消息正文。媒体消息剥掉 mediaId，只留类型标记。"""
    text = rec.get("text") or ""
    mid = mediadesc.media_id_of(text)
    if mid:
        text = text.replace(f"(mediaId={mid})", "").strip() or "[图片]"
    body, trunc = _cap(text, text_max)
    return body, (trunc or bool(rec.get("trunc")))


def _render_row(row, conv_id, text_max, now):
    """一条消息 + 它的 desc/fb → 若干行。"""
    m = row["msg"]
    ts = m.get("ts") or 0
    body, trunc = _body(m, text_max)
    head = (f"[{time.strftime('%m-%d %H:%M:%S', time.localtime(ts))} · {_rel(ts, now)}] "
            f"{_who(m)}: {body} msg={m.get('id')}")
    if trunc:
        head += f"（正文已截断，全文：{cmd_hint(conv_id, 'msg', m.get('id'))}）"
    lines = [head]
    d = row.get("desc")
    if d:
        if d.get("ok"):
            full = d.get("text") or ""
            shown, cut = _cap(full, text_max)
            tail = (f"（共 {len(full)} 字，全文：{cmd_hint(conv_id, 'msg', m.get('id'))}）"
                    if cut else "")
            lines.append(f"    └ 图片内容(OCR): {shown}{tail}")
        else:
            lines.append(f"    └ 图片识别失败({d.get('err') or '未知'})")
    f = row.get("fb")
    if f:
        ans, _ = _cap(f.get("answer") or "", text_max)
        lines.append(f"    └ 主管裁决: {f.get('action') or '?'}"
                     f"{' by ' + f['by'] if f.get('by') else ''}"
                     f"{' → 「' + ans + '」' if ans else ''}")
    return lines


def _emit(lines, max_bytes):
    """输出并在超限时**留下可见尾注** —— 静默截断会让模型以为没有更多了。"""
    out, used, dropped = [], 0, 0
    for ln in lines:
        b = len(ln.encode("utf-8")) + 1
        if used + b > max_bytes:
            dropped += 1
            continue
        out.append(ln)
        used += b
    if dropped:
        out.append(f"…（输出已截到 {max_bytes} 字节，还有 {dropped} 行没显示；"
                   f"用 --limit/--days 缩小范围，或用 msg 子命令定点取）")
    _write("\n".join(out) + "\n")


def _clamp(v, lo, hi):
    return max(lo, min(int(v), hi))


# ---------------------------------------------------------------------------
# 子命令
# ---------------------------------------------------------------------------

def _cmd_recent(a):
    limit, days = _clamp(a.limit, 1, _LIMIT_MAX), _clamp(a.days, 1, _DAYS_MAX)
    rows = msgstore.transcript(a.conv, limit=limit, days=days)
    if a.json:
        return _dump_json(rows, a)
    if not rows:
        _write(f"会话 {a.conv} 近 {days} 天没有消息记录。\n")
        return EXIT_OK
    now = time.time()
    header = [f"会话 {a.conv}｜近 {days} 天，下面是最新 {len(rows)} 条（时间正序）",
              f"时区 {time.strftime('%Z')}；「我」= 数字员工自己发的；"
              f"发送人是显示名不是 userId，可能重名", ""]
    body = []
    for r in rows:
        body += _render_row(r, a.conv, _clamp(a.text_max, 20, _TEXT_MAX), now)
    _emit(header + body, _clamp(a.max_bytes, 1000, _BYTES_MAX))
    return EXIT_OK


def _cmd_search(a):
    limit, days = _clamp(a.limit, 1, _LIMIT_MAX), _clamp(a.days, 1, _DAYS_MAX)
    rows = msgstore.search(a.conv, a.keyword, limit=limit, days=days)
    if a.json:
        return _dump_json(rows, a)
    if not rows:
        _write(f"会话 {a.conv} 近 {days} 天没有匹配「{a.keyword}」的消息"
               f"（正文和图片识别文本都搜过了）。\n")
        return EXIT_OK
    now = time.time()
    header = [f"会话 {a.conv}｜「{a.keyword}」命中 {len(rows)} 条（新→旧，"
              f"正文与图片识别文本都搜）", ""]
    body = []
    for r in rows:
        body += _render_row(r, a.conv, _clamp(a.text_max, 20, _TEXT_MAX), now)
    _emit(header + body, _clamp(a.max_bytes, 1000, _BYTES_MAX))
    return EXIT_OK


def _cmd_msg(a):
    row = msgstore.message(a.conv, a.msg_id)
    if not row:
        sys.stderr.write(f"会话 {a.conv} 里找不到消息 {a.msg_id}\n")
        return EXIT_NOT_FOUND
    if a.json:
        return _dump_json([row], a)
    # 定点取全文：这里**不截断**，它正是 recent 截断后的出口
    m = row["msg"]
    ts = m.get("ts") or 0
    out = [f"[{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ts))} · {_rel(ts)}] "
           f"{_who(m)}  msg={m.get('id')}  kind={m.get('kind') or '?'}",
           "", m.get("text") or "（空消息）"]
    if row.get("desc"):
        d = row["desc"]
        out += ["", "【图片识别结果】" if d.get("ok")
                else f"【图片识别失败】{d.get('err') or ''}", d.get("text") or ""]
    if row.get("fb"):
        f = row["fb"]
        out += ["", f"【主管裁决】{f.get('action') or '?'}"
                    f"{' by ' + f['by'] if f.get('by') else ''}", f.get("answer") or ""]
    _emit(out, _clamp(a.max_bytes, 1000, _BYTES_MAX))
    return EXIT_OK


def _cmd_image(a):
    """取这张图的内容，没识别过就**现在识别并等它**。"""
    row = msgstore.message(a.conv, a.msg_id)
    if not row:
        sys.stderr.write(f"会话 {a.conv} 里找不到消息 {a.msg_id}\n")
        return EXIT_NOT_FOUND
    text = row["msg"].get("text") or ""
    if not mediadesc.media_id_of(text):
        sys.stderr.write(f"{a.msg_id} 不是图片消息（正文：{text[:60]}）\n")
        return EXIT_NOT_FOUND
    wait = _clamp(a.wait, 1, _IMAGE_WAIT_MAX)
    # 用 describe_sync：一次性进程不能走 task 池，否则退出时会被 atexit join 卡住
    with _peer_wait(wait):
        desc, st = mediadesc.describe_sync(a.conv, a.msg_id, text)
    if desc:
        _write(desc + "\n")                 # stdout 只有内容，方便管道
        return EXIT_OK
    if st == "pending":
        sys.stderr.write(f"识别还没完成（等了 {wait}s，可能有别的进程正在识别）。"
                         f"稍后再运行一次同样的命令。\n")
        return EXIT_TIMEOUT
    hint = {"download": "图片没能从钉钉下载下来，可以请对方重发一次",
            "recognize": "识别服务没能读出内容，可以请对方把关键内容打字发出来",
            "busy": "识别并发已满，稍后重试"}.get(st, f"识别失败（{st}）")
    sys.stderr.write(hint + "\n")
    return EXIT_FAILED


class _peer_wait:
    """临时把等锁时间压到本次 --wait 之内 —— 默认 150s 会超过调用方的耐心。"""

    def __init__(self, wait):
        self.wait = wait

    def __enter__(self):
        self.old = mediadesc._PEER_WAIT
        mediadesc._PEER_WAIT = self.wait

    def __exit__(self, *exc):
        mediadesc._PEER_WAIT = self.old
        return False


def _dump_json(rows, a):
    """结构化输出。**算数题给结构化数据比给散文强** —— 触发本次改造的正是「这个图你统计下」。"""
    out = []
    for r in rows:
        m = r["msg"]
        item = {"msg_id": m.get("id"), "ts": m.get("ts"),
                "time": time.strftime("%Y-%m-%d %H:%M:%S",
                                      time.localtime(m.get("ts") or 0)),
                "dir": m.get("dir"), "who": _who(m), "kind": m.get("kind"),
                "text": m.get("text") or "", "truncated": bool(m.get("trunc"))}
        if r.get("desc"):
            item["image_desc"] = {"text": r["desc"].get("text") or "",
                                  "ok": bool(r["desc"].get("ok")),
                                  "by": r["desc"].get("by") or ""}
        if r.get("fb"):
            item["supervisor"] = {"action": r["fb"].get("action") or "",
                                  "answer": r["fb"].get("answer") or "",
                                  "by": r["fb"].get("by") or ""}
        out.append(item)
    _write(json.dumps(out, ensure_ascii=False, indent=2) + "\n")
    return EXIT_OK


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def _parser():
    p = argparse.ArgumentParser(
        prog="convq", description="查本会话落盘的钉钉消息记录（只读）")
    subs = p.add_subparsers(dest="cmd")

    def _common(sp, days):
        # --conv 必填且没有"全部会话"模式：枚举别的群正是不想暴露的面
        sp.add_argument("--conv", required=True, help="会话 openConversationId")
        sp.add_argument("--days", type=int, default=days)
        sp.add_argument("--text-max", dest="text_max", type=int, default=300)
        sp.add_argument("--max-bytes", dest="max_bytes", type=int, default=24000)
        sp.add_argument("--json", action="store_true")

    r = subs.add_parser("recent", help="最近的消息（含图片识别结果、主管裁决）")
    r.add_argument("--limit", type=int, default=30)
    _common(r, days=2)

    s = subs.add_parser("search", help="按关键词搜（正文 + 图片识别文本）")
    s.add_argument("keyword")
    s.add_argument("--limit", type=int, default=20)
    _common(s, days=7)

    m = subs.add_parser("msg", help="一条消息的全文（recent 截断后的出口）")
    m.add_argument("msg_id")
    _common(m, days=30)

    i = subs.add_parser("image", help="取图片内容，没识别过就现在识别并等它")
    i.add_argument("msg_id")
    i.add_argument("--conv", required=True)
    i.add_argument("--wait", type=int, default=150)
    return p


def main(argv=None):
    global _OUT
    p = _parser()
    a = p.parse_args(argv if argv is not None else sys.argv[1:])
    if not a.cmd:
        p.print_help(sys.stderr)
        return EXIT_ARGS
    # 把库的 stdout 日志赶到 stderr，正文走 _write 到入口时的真 stdout
    _OUT, saved = sys.stdout, sys.stdout
    sys.stdout = sys.stderr
    try:
        return {"recent": _cmd_recent, "search": _cmd_search,
                "msg": _cmd_msg, "image": _cmd_image}[a.cmd](a)
    except BrokenPipeError:
        return EXIT_OK                  # 被 head 之类截断，不是错
    except Exception as e:              # noqa: BLE001 — CLI 不该把栈吐给大脑
        sys.stderr.write(f"convq 出错：{e}\n")
        return EXIT_FAILED
    finally:
        sys.stdout = saved
        _OUT = None
