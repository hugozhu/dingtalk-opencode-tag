"""ack — 消息回执能力：已读 + 状态「文字表情」时间线（custom 插件）

数字员工收到（默认：单聊）消息时给出即时可见的处理状态，弥补 brain 生成回复的
几秒空窗——用户不再干等、不知道"到底收到没"。用一条贴在**用户消息上**的
「文字表情」回应（DingTalk text-emotion：**表情 + 文字同时呈现**），随处理进度
**原地更新**（不发独立消息、不刷屏、无卡片"生成中"加载态）：

  🈺 收到「稍等｜已收到，正在处理…」→ 5s「稍等｜正在处理中…」
     → 5 分钟仍在处理「咖啡｜仍在处理（约 5 分钟）…」
  回复发出 → 「OK｜已完成」；处理失败 → 「疑问｜处理未完成」

任一时刻消息上只有一个文字表情（升级=移除旧 + 贴新，remove/add text-emotion）。

时间线由 ACK_STAGES 配置（`delay秒:表情名:文字`，多阶段用 `|` 分隔，按 delay 升序），
完成/失败用 ACK_DONE / ACK_ERROR（`表情名:文字`）。

DingTalk 约定（实测）：
- 文字表情需先 `create-text-emotion --emotion-name <表情> --text <文字>` 拿到
  emotionId + backgroundId，再 `add-text-emotion`；本模块按 (表情名,文字) 进程内缓存
  emotionId，避免重复创建。
- add/remove-text-emotion 用 --conversation-id + --msg-id，单聊/群聊通用（无需 openDingTalkId）。

设计要点：
- **非消费型**：on_inbound 只做回执副作用后返回 False，让 text_reply 等照常回复
  （priority=1 最先跑；dispatch_inbound 遇 True 才短路，False 继续分发）。
- **生命周期靠 reply-sent 信号**：core 的 `on_reply_sent(conv_id, conv_type, ok)` hook
  （replier.send_reply 后广播）驱动"进度→完成/失败"切换。每条消息一个后台 worker：
  mark-read + 走时间线（按 elapsed 逐级升级），收到信号或整体超时即收尾。
- **best-effort**：mark-read / create/add/remove-text-emotion 任一失败只记日志，
  绝不影响正常回复链路。
- **防回环 + 去重**：跳过 AGENT_SELF_NAMES 自己发的；msgId 去重（对齐 text_reply/image）。

开关：CAP_ACK_ENABLED（**默认开**）。默认文案已实测可被 DingTalk `create-text-emotion`
保存；改文案后建议先用 `dws chat message create-text-emotion --emotion-name <名> --text <文>`
手测能否保存（部分含特殊 emoji/标点的文案会报"暂不支持保存该文字表情"）。需数字员工 profile
有回执权限。停用设 CAP_ACK_ENABLED=0。
"""

import json
import os
import threading
import time
from collections import OrderedDict

from core.agent_common import _run_cli, env_flag, log
from core.capabilities import Capability, register

# --- 配置（constants.local.sh 覆盖）---
_O2O_ONLY = env_flag("ACK_O2O_ONLY", default=True)       # 默认只单聊（群里逐条贴噪音大）
_AT_MENTION = env_flag("ACK_AT_MENTION", default=True)   # 群里被 @ 数字员工时也回执（#46）
_MARK_READ = env_flag("ACK_MARK_READ", default=True)      # 是否同时标记已读


def _parse_stages(spec):
    """把 'delay:表情:文字|delay:表情:文字|…' 解析成按 delay 升序的 [(delay, 表情, 文字)]。

    - 阶段之间用 `|` 分隔；每个阶段 `delay:表情名:文字`，只在前两个 `:` 处切分
      （文字里可含 `:`/`，`）。首个阶段应为 delay=0（收到即贴）。
    - 非法/空阶段跳过；整体为空则回退到单一「稍等｜正在处理…」（0s）。
    """
    stages = []
    for item in (spec or "").split("|"):
        item = item.strip()
        if not item:
            continue
        parts = item.split(":", 2)
        if len(parts) < 3:
            continue
        d, emoji, text = parts
        emoji, text = emoji.strip(), text.strip()
        try:
            delay = float(d.strip())
        except ValueError:
            continue
        if emoji and text and delay >= 0:
            stages.append((delay, emoji, text))
    stages.sort(key=lambda s: s[0])
    if not stages:
        stages = [(0.0, "稍等", "正在处理…")]
    return stages


def _parse_status(spec, default_emoji, default_text):
    """把 '表情名:文字' 解析成 (表情, 文字)；缺省用默认。"""
    if spec and ":" in spec:
        emoji, _, text = spec.partition(":")
        emoji, text = emoji.strip(), text.strip()
        if emoji and text:
            return (emoji, text)
    return (default_emoji, default_text)


# 进度「文字表情」时间线：收到即贴，5s 处理中，5 分钟(300s)仍在处理。
# 默认文案简洁清晰：收到 → 处理中 → 完成/未完成
# 表情名本身已是钉钉贴纸（稍等/咖啡/OK/疑问），文字只作补充。
_STAGES = _parse_stages(
    os.environ.get("ACK_STAGES")
    or "0:稍等:收到|5:稍等:处理中|300:咖啡:处理中"
)
_DONE = _parse_status(os.environ.get("ACK_DONE"), "OK", "完成")
_ERROR = _parse_status(os.environ.get("ACK_ERROR"), "疑问", "未完成")

# 等"回复已发出"信号的上限秒数（brain 慢 / 空回复不发时兜底收尾）。默认覆盖到最后一个
# 进度阶段之后仍留足冗余（最后阶段 delay + 300s，至少 180s）。
_DONE_TIMEOUT = float(
    os.environ.get("ACK_DONE_TIMEOUT")
    or str(max(180.0, _STAGES[-1][0] + 300.0))
)

_CONV_TYPE_O2O = "1"

# 防回环：数字员工自己发的消息不回执
_SELF_NAMES = {
    n.strip() for n in os.environ.get("AGENT_SELF_NAMES", "数字员工,Claude Code").split(",")
    if n.strip()
}

# msgId 去重（断线重连可能重投同一条）—— 有界 FIFO
_seen = OrderedDict()
_seen_lock = threading.Lock()
_SEEN_MAX = 2048

# 未完成回执登记表：conv_id -> _Pending。单聊里 conv_id 唯一对应当前处理的消息。
_pending = {}
_pending_lock = threading.Lock()

# 文字表情模板缓存：(表情名, 文字) -> (emotionId, backgroundId)。首次 create，之后复用。
_emotion_cache = {}
_emotion_lock = threading.Lock()


class _Pending:
    """一条消息的回执生命周期状态。"""
    __slots__ = ("conv_id", "conv_type", "msg_id", "event", "ok", "cur")

    def __init__(self, conv_id, conv_type, msg_id):
        self.conv_id = conv_id
        self.conv_type = conv_type
        self.msg_id = msg_id
        self.event = threading.Event()
        self.ok = None            # None=未收到/被取代/超时；True=成功；False=失败
        self.cur = None           # 当前贴着的 (表情, 文字)（worker 独占，无需锁）


def _note_and_plan(msg_id, want_begin, want_read):
    """登记 msgId 并返回本次应执行的动作 (do_begin, do_read)，处理双投行序竞态（#46）。

    _seen 的值是该 msgId 已达到的最高状态：
      None(未见) < "read"(已标记已读) < "begun"(已启动完整回执 worker)
    同一 msgId 可能被投递两次（群订阅未打标 + @我订阅打标），合流进单一 log-tail、顺序不定：
      - 群非AT(want_begin=False,want_read=True) 先到 → 标记已读，记 "read"；
        随后 @我(want_begin=True) 到 → 升级启动 worker（worker 会再 mark-read，幂等无害）。
      - @我 先到 → 启动 worker，记 "begun"；随后群非AT 到 → 已是 begun，什么都不做。
    返回 (do_begin, do_read)：
      do_begin = want_begin 且 之前未 begun
      do_read  = want_read 且 之前既未 read 也未 begun（避免重复 mark-read；begin 内部自带 mark-read）
    """
    order = {None: 0, "read": 1, "begun": 2}
    if not msg_id:
        # 无 msgId 不去重：按意愿直接执行（begin 优先，其自带 mark-read）
        return (want_begin, want_read and not want_begin)
    with _seen_lock:
        prev = _seen.get(msg_id)
        do_begin = want_begin and order[prev] < order["begun"]
        do_read = want_read and not want_begin and prev is None
        new_state = "begun" if (want_begin or prev == "begun") else \
                    ("read" if (want_read or prev == "read") else prev)
        if new_state is not None:
            _seen[msg_id] = new_state
            _seen.move_to_end(msg_id)
            if len(_seen) > _SEEN_MAX:
                _seen.popitem(last=False)
    return (do_begin, do_read)


def _should_ack(msg):
    """纯判定：这条消息是否要**完整回执**（已读 + 状态文字表情）。

    需要 conv_id + msg_id（回执 API 必填）。完整回执范围：
      - 单聊(conv_type=1) → 回执（#45 原行为）
      - 群(conv_type=2) 且本条被 @ 数字员工（extra['at_mention']）且 ACK_AT_MENTION 开 → 回执（#46）
      - ACK_O2O_ONLY=0 → 群里所有消息也完整回执（原逃生口，噪音大，默认关）
      - 其它群消息（未被 @）→ 不做完整回执（但可能只标记已读，见 _should_mark_read）
    """
    if not msg.conv_id or not msg.msg_id:
        return False
    if msg.conv_type == _CONV_TYPE_O2O:
        return True
    if _AT_MENTION and msg.extra.get("at_mention"):
        return True
    if not _O2O_ONLY:
        return True
    return False


def _should_mark_read(msg):
    """纯判定：这条消息是否要**标记已读**（比完整回执更宽——订阅群里的普通消息也标已读，
    只是不贴状态表情，避免群里逐条贴表情的噪音）。ACK_MARK_READ 总开关；需 conv_id+msg_id。
    订阅到的消息（单聊/群 @/群普通）都在范围内。"""
    if not _MARK_READ:
        return False
    if not msg.conv_id or not msg.msg_id:
        return False
    return True


# --- DingTalk 回执调用（best-effort，失败只记日志不抛）---
def _mark_read(conv_id, msg_id):
    rc, out = _run_cli(["chat", "mark-read",
                        "--conversation-id", conv_id, "--message-id", msg_id], timeout=15)
    if rc != 0:
        log(f"ack: mark-read 失败 rc={rc} msgId={msg_id[:16]} out={out[:80]}")
    return rc == 0


def _emotion_id(emoji, text):
    """按 (表情名, 文字) 拿到 (emotionId, backgroundId)，进程内缓存；首次 create。

    失败返回 (None, None)。
    """
    key = (emoji, text)
    with _emotion_lock:
        if key in _emotion_cache:
            return _emotion_cache[key]
    rc, out = _run_cli(["chat", "message", "create-text-emotion",
                        "--emotion-name", emoji, "--text", text], timeout=15)
    eid = bid = None
    if rc == 0:
        try:
            res = (json.loads(out).get("result", {}) or {})
            eid = res.get("emotionId")
            bid = res.get("backgroundId")
        except (ValueError, TypeError):
            pass
    if not eid:
        log(f"ack: create-text-emotion 失败 rc={rc} {emoji}/{text[:12]} out={out[:80]}")
        return (None, None)
    eid = str(eid)
    with _emotion_lock:
        _emotion_cache[key] = (eid, bid)
    return (eid, bid)


def _emotion_args(conv_id, msg_id, emoji, text, eid, bid):
    args = ["--conversation-id", conv_id, "--msg-id", msg_id,
            "--emotion-id", eid, "--emotion-name", emoji, "--text", text]
    if bid:
        args += ["--background-id", bid]
    return args


def _add_text_emotion(conv_id, msg_id, emoji, text):
    eid, bid = _emotion_id(emoji, text)
    if not eid:
        return False
    rc, out = _run_cli(["chat", "message", "add-text-emotion"]
                       + _emotion_args(conv_id, msg_id, emoji, text, eid, bid), timeout=15)
    if rc != 0:
        log(f"ack: add-text-emotion 失败 rc={rc} {emoji}/{text[:12]} out={out[:80]}")
    return rc == 0


def _remove_text_emotion(conv_id, msg_id, emoji, text):
    eid, bid = _emotion_id(emoji, text)
    if not eid:
        return False
    rc, out = _run_cli(["chat", "message", "remove-text-emotion"]
                       + _emotion_args(conv_id, msg_id, emoji, text, eid, bid), timeout=15)
    if rc != 0:
        log(f"ack: remove-text-emotion 失败 rc={rc} {emoji}/{text[:12]} out={out[:80]}")
    return rc == 0


def _set_status(rec, status):
    """把文字表情切到 status=(表情,文字)：移除当前的（若有）再贴新的。status=None 只移除。

    单个消息的表情操作都在其 worker 线程内串行发生（rec.cur 只由 worker 读写），无需加锁。
    """
    if rec.cur == status:
        return
    if rec.cur:
        _remove_text_emotion(rec.conv_id, rec.msg_id, rec.cur[0], rec.cur[1])
    if status:
        _add_text_emotion(rec.conv_id, rec.msg_id, status[0], status[1])
    rec.cur = status


def _first_status(stages):
    """时间线里 delay<=0 的最后一个 (表情,文字)（elapsed=0 此刻应显示的）；无则 None。"""
    val = None
    for delay, emoji, text in stages:
        if delay <= 0:
            val = (emoji, text)
        else:
            break
    return val


def _do_processing(rec):
    """收到阶段：标记已读 + 贴时间线第一个（delay=0）文字表情。"""
    if _MARK_READ:
        _mark_read(rec.conv_id, rec.msg_id)
    first = _first_status(_STAGES)
    if first:
        _set_status(rec, first)


def _finalize(rec, ok):
    """收尾：移除当前进度文字表情，按结果贴完成/失败文字表情。ok=None → 只移除进度。"""
    final = None
    if ok is True:
        final = _DONE
    elif ok is False:
        final = _ERROR
    _set_status(rec, final)


def _ack_worker(rec):
    """单条消息的回执 worker：走文字表情时间线（按 elapsed 逐级升级），
    直到收到 reply-sent 信号或整体超时，再收尾切完成/失败。"""
    try:
        start = time.monotonic()
        _do_processing(rec)   # elapsed≈0：已读 + 首个文字表情

        # 剩余阶段（delay>0）：等到各自 delay 时切文字表情；期间若 event 触发则提前收尾
        for delay, emoji, text in _STAGES:
            if delay <= 0:
                continue
            wait = delay - (time.monotonic() - start)
            if wait > 0 and rec.event.wait(timeout=wait):
                break   # 回复已到/被取代：不再升级，跳到收尾
            if rec.event.is_set():
                break
            _set_status(rec, (emoji, text))   # 到点升级（如 5 分钟 → 咖啡｜仍在处理）

        # 所有进度阶段走完仍没信号 → 继续等到整体超时兜底
        if not rec.event.is_set():
            remaining = _DONE_TIMEOUT - (time.monotonic() - start)
            if remaining > 0:
                rec.event.wait(timeout=remaining)

        # ok：有信号取 rec.ok（成功/失败）；无信号（超时）→ None 仅移除进度文字表情
        _finalize(rec, rec.ok if rec.event.is_set() else None)
    except Exception as e:
        log(f"ack: worker err msgId={rec.msg_id[:16]} {e}")
    finally:
        # 仅当登记表里还是本 rec 时才清（避免误删已被新消息取代的登记）
        with _pending_lock:
            if _pending.get(rec.conv_id) is rec:
                _pending.pop(rec.conv_id, None)


def _begin(msg):
    """登记并启动一条消息的回执生命周期。"""
    rec = _Pending(msg.conv_id, msg.conv_type, msg.msg_id)
    with _pending_lock:
        old = _pending.get(msg.conv_id)
        _pending[msg.conv_id] = rec
    if old is not None:
        old.event.set()   # 取代旧的：ok 保持 None，让旧 worker 尽快收尾（仅移除进度）
    threading.Thread(target=_ack_worker, args=(rec,), daemon=True).start()


def on_inbound(msg):
    """回执入站：非消费型（返回 False 让后续能力照常回复）。

    自己发的 → 放行。否则按范围判定：
      - 完整回执（单聊 / 群被@）→ 启动 worker（已读 + 状态表情，随处理进度更新）；
      - 仅标记已读（订阅群里的普通消息）→ 只 mark-read，不贴表情、不起 worker。
    双投同一 msgId（群+@我）行序不定时，_note_and_plan 保证 begin 恰好一次、mark-read 不重复。
    绝不消费消息（text_reply 等仍会处理并回复）。
    """
    if msg.user in _SELF_NAMES:
        return False
    want_begin = _should_ack(msg)
    want_read = _should_mark_read(msg)
    do_begin, do_read = _note_and_plan(msg.msg_id, want_begin, want_read)
    if do_begin:
        _begin(msg)          # 完整回执（worker 内部会 mark-read + 贴表情）
    elif do_read:
        _mark_read(msg.conv_id, msg.msg_id)   # 仅标记已读（best-effort，失败只记日志）
    return False   # 关键：不消费，text_reply 等仍会处理并回复


def on_reply_sent(conv_id, conv_type, ok):
    """收到"回复已发出"信号：唤醒对应 worker 切换完成/失败文字表情。"""
    with _pending_lock:
        rec = _pending.get(conv_id)
    if rec is not None and not rec.event.is_set():
        rec.ok = bool(ok)
        rec.event.set()


CAPABILITY = Capability(
    name="ack",
    on_inbound=on_inbound,
    on_reply_sent=on_reply_sent,
    handles_kinds=set(),       # 所有 kind（文本/图片/文件…）都回执
    priority=1,                # 最先跑，抢在业务能力消费之前贴"处理中"
    default_enabled=True,      # 默认开：收到消息即 mark-read + 贴状态文字表情（默认文案实测
                               # 可存）。全 best-effort，失败只记日志不阻断回复。停用设 CAP_ACK_ENABLED=0
)
register(CAPABILITY)
