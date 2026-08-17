"""ack — 消息回执能力：已读 + 状态「文字表情」时间线（custom 插件）

数字员工收到（默认：单聊）消息时给出即时可见的处理状态，弥补 brain 生成回复的
几秒空窗——用户不再干等、不知道"到底收到没"。用一条贴在**用户消息上**的
「文字表情」回应（DingTalk text-emotion：**表情 + 文字同时呈现**），随处理进度
**原地更新**（不发独立消息、不刷屏、无卡片"生成中"加载态）：

  🈺 收到「稍等｜已收到，正在处理…」→ 1s「稍等｜正在处理中…」
     → 长任务每 5 分钟：原地更新「咖啡｜处理中N分钟」+ 另发一条独立进度消息（#75）
  回复发出 → 「OK｜已完成」；处理失败 → 「疑问｜处理未完成」

任一时刻消息上只有一个文字表情。状态升级优先走 `update-text-emotion` **原地更新**
（单次调用把旧表情/文字直接改成新的，无中间空档、无闪烁、少一半 CLI 往返；#85）；
仅「首次贴」用 add、「清除」用 remove。update 失败时兜底回退到 remove 旧 + add 新。

时间线由 ACK_STAGES 配置（`delay秒:表情名:文字`，多阶段用 `|` 分隔，按 delay 升序），
完成/失败用 ACK_DONE / ACK_ERROR（`表情名:文字`）。长任务的周期进度心跳由 ACK_PROGRESS_*
配置（默认每 300s：更新表情 + 发独立进度消息），进度消息走 replier.send_notice 不触发收尾。

DingTalk 约定（实测）：
- 文字表情需先 `create-text-emotion --emotion-name <表情> --text <文字>` 拿到
  emotionId + backgroundId，再 `add-text-emotion`；本模块按 (表情名,文字) 进程内缓存
  emotionId，避免重复创建。
- add/remove/update-text-emotion 用 --conversation-id + --msg-id，单聊/群聊通用（无需 openDingTalkId）。
  update 额外需 --old-emotion-id（原表情 emotionId）+ 新的 emotionId/name/text/backgroundId。

设计要点：
- **非消费型**：on_inbound 只做回执副作用后返回 False，让 text_reply 等照常回复
  （priority=1 最先跑；dispatch_inbound 遇 True 才短路，False 继续分发）。
- **只给主管贴状态表情**（#106，ACK_SUPERVISOR_ONLY 默认开）：真人同事不会在每条消息上
  贴状态标签；且主管审核回路开启后非主管的消息实际由主管处理，给提问者贴「正在处理中」
  是语义错误。已读不受影响——所有人照常 mark-read。未配主管时退化为原行为（都贴）。
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
import re
import threading
import time
from collections import OrderedDict

from core.agent_common import _run_cli, env_flag, log
from core.capabilities import Capability, register
from custom.identity import has_supervisor, is_supervisor

# --- 配置（constants.local.sh 覆盖）---
_O2O_ONLY = env_flag("ACK_O2O_ONLY", default=True)       # 默认只单聊（群里逐条贴噪音大）
_AT_MENTION = env_flag("ACK_AT_MENTION", default=True)   # 群里被 @ 数字员工时也回执（#46）
_MARK_READ = env_flag("ACK_MARK_READ", default=True)      # 是否同时标记已读
# 只对**主管**的消息贴状态表情（#106，拟人化）。真人同事不会在每条消息上贴状态标签；
# 且主管审核回路开启后，非主管的消息实际由主管处理，给提问者贴「正在处理中」是语义错误。
# 已读(mark-read)不受本项影响——所有人照常标已读（真人也会已读）。
# **未配主管时退化为原行为（都贴）**：否则没配主管的部署会一个表情都不贴（无声回退）。
_SUPERVISOR_ONLY = env_flag("ACK_SUPERVISOR_ONLY", default=True)

# ack 动作轨迹（收到/升级/收尾/仅已读）AGENT_DEBUG 时记到 monitor.log —— 和 ack 失败日志
# 同一文件、同一 "ack:" 前缀，一处看全某条消息的回执经过（按 msgId 与 agent-connect.log 对齐）。
# 生产默认关（避免每条消息都刷 monitor.log）；失败日志不受此开关影响，恒记。
_ACK_DEBUG = env_flag("AGENT_DEBUG", default=False)


def _dlog(msg):
    """AGENT_DEBUG 时把一行 ack 动作轨迹记到 monitor.log（复用 core.log）。"""
    if _ACK_DEBUG:
        log(f"ack: {msg}")


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


# 进度「文字表情」时间线：收到即贴，1s 处理中。5 分钟以上的长任务由**周期进度心跳**接管
# （见下方 _PROGRESS_*），不再靠单个 300s 阶段。
# 表情名本身已是钉钉贴纸（稍等/咖啡/OK/疑问），文字只作补充。
_STAGES = _parse_stages(
    os.environ.get("ACK_STAGES")
    or "0:稍等:收到|1:稍等:处理中"
)
_DONE = _parse_status(os.environ.get("ACK_DONE"), "OK", "完成")
_ERROR = _parse_status(os.environ.get("ACK_ERROR"), "疑问", "未完成")

# 周期进度心跳（长任务）：每隔 ACK_PROGRESS_INTERVAL 秒，若仍在处理 → ①原地更新消息上的
# 文字表情（带已耗时分钟），②另发一条独立进度消息到来源会话（send_notice，不触发收尾）。
# 配合 #75 活动感知超时：任务可能跑很久，用户需要周期性"还在干活"的反馈。
#   ACK_PROGRESS_INTERVAL   心跳间隔秒（默认 300=5min；0=关闭周期心跳，回退旧行为）
#   ACK_PROGRESS_MESSAGE    是否额外发独立进度消息（默认开；关=只更新表情不发消息）
#   ACK_PROGRESS_EMOJI      心跳阶段的表情名（默认「咖啡」）
#   ACK_PROGRESS_EMOJI_TEXT 表情附带文字模板（{mins}=已耗时分钟；文字须能被 create-text-emotion 保存）
#   ACK_PROGRESS_MSG        独立进度消息文案模板（{mins}=已耗时分钟）
_PROGRESS_INTERVAL = float(os.environ.get("ACK_PROGRESS_INTERVAL", "300"))
_PROGRESS_MESSAGE = env_flag("ACK_PROGRESS_MESSAGE", default=True)
_PROGRESS_EMOJI = os.environ.get("ACK_PROGRESS_EMOJI", "咖啡")
_PROGRESS_EMOJI_TEXT = os.environ.get("ACK_PROGRESS_EMOJI_TEXT", "处理中{mins}分钟")
_PROGRESS_MSG = os.environ.get("ACK_PROGRESS_MSG", "⏳ 仍在处理中，已耗时约 {mins} 分钟，请稍候…")

# 等"回复已发出"信号的上限秒数（brain 慢 / 空回复不发时兜底收尾）。默认覆盖到 opencode
# 长任务上限（AGENT_OPENCODE_MAX_TIMEOUT，#75）之后仍留冗余，避免心跳中途被误收尾。
_OPENCODE_MAX = float(os.environ.get("AGENT_OPENCODE_MAX_TIMEOUT", "3600") or 0)
_DONE_TIMEOUT = float(
    os.environ.get("ACK_DONE_TIMEOUT")
    or str(max(180.0, _STAGES[-1][0] + 300.0, _OPENCODE_MAX + 300.0))
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
    __slots__ = ("conv_id", "conv_type", "msg_id", "event", "ok", "cur", "cur_eid", "cur_bid")

    def __init__(self, conv_id, conv_type, msg_id):
        self.conv_id = conv_id
        self.conv_type = conv_type
        self.msg_id = msg_id
        self.event = threading.Event()
        self.ok = None            # None=未收到/被取代/超时；True=成功；False=失败
        self.cur = None           # 当前贴着的 (表情, 文字)（worker 独占，无需锁）
        self.cur_eid = None       # 当前贴着的 emotionId（#95：update 需精确定位旧表情）
        self.cur_bid = None       # 当前贴着的 backgroundId


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
      - 非主管的消息（ACK_SUPERVISOR_ONLY 开且已配主管）→ 不贴状态表情（#106）
      - 单聊(conv_type=1) → 回执（#45 原行为）
      - 群(conv_type=2) 且本条被 @ 数字员工（extra['at_mention']）且 ACK_AT_MENTION 开 → 回执（#46）
      - ACK_O2O_ONLY=0 → 群里所有消息也完整回执（原逃生口，噪音大，默认关）
      - 其它群消息（未被 @）→ 不做完整回执（但可能只标记已读，见 _should_mark_read）
    """
    if not msg.conv_id or not msg.msg_id:
        return False
    # 主管闸门（#106）：只给主管贴状态表情。has_supervisor() 前置——没配主管时
    # is_supervisor() 对谁都是 False，少了这层判断会导致**谁都不贴**（无声回退）。
    if _SUPERVISOR_ONLY and has_supervisor() and not is_supervisor(msg.user):
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

    #95 fix：移除进度文字的缓存 key 标准化。每个不同的「处理中N分钟」创建独立 emotionId，
    使 update-text-emotion 能正确识别 old != new 并原地更新。静态文字（收到/完成/失败）
    仍缓存复用。

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


def _update_text_emotion(conv_id, msg_id, old_eid, old_bid, new_emoji, new_text, new_eid, new_bid):
    """原地更新文字表情：把 old emotionId 直接改成 new (表情,文字,emotionId)，单次 CLI 调用（#85）。

    #95 fix：接收实际挂载的 old_eid（由 _Pending.cur_eid 传入），不再从缓存反查。
    避免缓存漂移导致 old==new 而无法原地更新。

    任一参数缺失或 CLI 失败返回 False，由调用方回退 remove+add 兜底。
    """
    if not old_eid or not new_eid:
        return False
    args = ["chat", "message", "update-text-emotion",
            "--conversation-id", conv_id, "--msg-id", msg_id,
            "--old-emotion-id", old_eid,
            "--emotion-id", new_eid, "--emotion-name", new_emoji, "--text", new_text]
    if new_bid:
        args += ["--background-id", new_bid]
    rc, out = _run_cli(args, timeout=15)
    if rc != 0:
        log(f"ack: update-text-emotion 失败 rc={rc} {new_emoji}/{new_text[:12]} out={out[:80]}")
    return rc == 0


def _set_status(rec, status):
    """把文字表情切到 status=(表情,文字)。status=None 只移除当前的。

    升级（当前有表情且新状态非空）优先走 update-text-emotion **原地更新**（单次调用，
    无闪烁、少一半开销，#85）；失败兜底回退 remove 旧 + add 新。首次贴用 add、清除用 remove。

    #95 fix：记录并使用实际挂载的 emotionId/backgroundId（rec.cur_eid/cur_bid），
    避免缓存与实际状态漂移导致 update 用同一 eid 无法原地更新。

    单个消息的表情操作都在其 worker 线程内串行发生（rec.cur 只由 worker 读写），无需加锁。
    """
    if rec.cur == status:
        return

    # 准备新状态的 emotionId（若需要）
    new_eid = new_bid = None
    if status:
        new_eid, new_bid = _emotion_id(status[0], status[1])
        if not new_eid:
            return  # 创建失败，放弃本次状态切换

    if rec.cur and status:
        # 升级：原地更新；失败回退 remove+add
        if not _update_text_emotion(rec.conv_id, rec.msg_id, rec.cur_eid, rec.cur_bid,
                                     status[0], status[1], new_eid, new_bid):
            # update 失败，兜底 remove+add（用实际挂载的 eid 移除）
            if rec.cur_eid:
                args = ["chat", "message", "remove-text-emotion",
                        "--conversation-id", rec.conv_id, "--msg-id", rec.msg_id,
                        "--emotion-id", rec.cur_eid, "--emotion-name", rec.cur[0], "--text", rec.cur[1]]
                if rec.cur_bid:
                    args += ["--background-id", rec.cur_bid]
                rc, out = _run_cli(args, timeout=15)
                if rc != 0:
                    log(f"ack: remove-text-emotion(兜底) 失败 rc={rc} {rec.cur[0]}/{rec.cur[1][:12]} out={out[:80]}")
            _add_text_emotion(rec.conv_id, rec.msg_id, status[0], status[1])
    elif rec.cur:
        # 清除：仅移除（用实际挂载的 eid）
        if rec.cur_eid:
            args = ["chat", "message", "remove-text-emotion",
                    "--conversation-id", rec.conv_id, "--msg-id", rec.msg_id,
                    "--emotion-id", rec.cur_eid, "--emotion-name", rec.cur[0], "--text", rec.cur[1]]
            if rec.cur_bid:
                args += ["--background-id", rec.cur_bid]
            rc, out = _run_cli(args, timeout=15)
            if rc != 0:
                log(f"ack: remove-text-emotion 失败 rc={rc} {rec.cur[0]}/{rec.cur[1][:12]} out={out[:80]}")
    elif status:
        # 首次贴
        _add_text_emotion(rec.conv_id, rec.msg_id, status[0], status[1])

    # 更新状态：记录实际挂载的 (表情,文字) 及其 emotionId/backgroundId
    rec.cur = status
    rec.cur_eid = new_eid if status else None
    rec.cur_bid = new_bid if status else None


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
    read = _mark_read(rec.conv_id, rec.msg_id) if _MARK_READ else None
    first = _first_status(_STAGES)
    _dlog("收到 msgId=%s conv=%s mark-read=%s 贴=%s" % (
        rec.msg_id or "-", (rec.conv_id or "-")[:16],
        "off" if read is None else ("ok" if read else "fail"),
        ("%s｜%s" % first) if first else "-"))
    if first:
        _set_status(rec, first)


def _finalize(rec, ok):
    """收尾：移除当前进度文字表情，按结果贴完成/失败文字表情。ok=None → 只移除进度。

    ok=None 有两种来路，日志要分得清（#109）：event 已置位 = 能力**显式静默收尾**
    （如主管审核选择忽略）；未置位 = 等信号等到超时兜底。两者都只移除进度表情，但
    前者是正常终态、后者是异常，混成一个「超时」会让排查看不出区别。
    """
    final = None
    if ok is True:
        final = _DONE
    elif ok is False:
        final = _ERROR
    why = {True: "成功", False: "失败"}.get(
        ok, "静默收尾" if rec.event.is_set() else "超时")
    _dlog("收尾 msgId=%s 结果=%s → %s" % (
        rec.msg_id or "-", why, ("%s｜%s" % final) if final else "移除进度"))
    _set_status(rec, final)


def _send_progress_message(conv_id, conv_type, mins):
    """发一条独立进度消息（不触发 ack 收尾）。best-effort，失败只记日志。"""
    if not _PROGRESS_MESSAGE:
        return
    try:
        from custom.replier import send_notice   # 延迟导入避免 capabilities 载入期循环
    except ImportError:
        return
    try:
        send_notice(conv_id, conv_type, _PROGRESS_MSG.format(mins=mins))
    except Exception as e:
        log(f"ack: 进度消息发送失败 {e}")


def _progress_tick(rec, mins):
    """一次进度心跳：原地更新消息上的文字表情（带已耗时分钟）+ 另发独立进度消息。"""
    _dlog("进度心跳 msgId=%s 已耗时=%d分钟" % (rec.msg_id or "-", mins))
    emoji_text = _PROGRESS_EMOJI_TEXT.format(mins=mins)
    _set_status(rec, (_PROGRESS_EMOJI, emoji_text))
    _send_progress_message(rec.conv_id, rec.conv_type, mins)


def _ack_worker(rec):
    """单条消息的回执 worker：走文字表情时间线（按 elapsed 逐级升级），再进入周期进度心跳，
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
            _dlog("升级 msgId=%s elapsed=%.0fs → %s｜%s" % (
                rec.msg_id or "-", time.monotonic() - start, emoji, text))
            _set_status(rec, (emoji, text))   # 到点升级

        # 进度阶段走完仍没信号 → 进入周期心跳，每 _PROGRESS_INTERVAL 秒更新表情 + 发进度消息，
        # 直到 reply-sent 信号或整体超时兜底（#75 长任务：只要还在处理就周期反馈）。
        while not rec.event.is_set():
            elapsed = time.monotonic() - start
            if elapsed >= _DONE_TIMEOUT:
                break
            if _PROGRESS_INTERVAL <= 0:
                rec.event.wait(timeout=_DONE_TIMEOUT - elapsed)   # 关闭心跳：等到超时兜底（旧行为）
                break
            # 下一个心跳时刻（interval 整数倍）；若它已越过 done-timeout，则只等到超时、不再心跳
            next_tick = (int(elapsed // _PROGRESS_INTERVAL) + 1) * _PROGRESS_INTERVAL
            if next_tick > _DONE_TIMEOUT:
                rec.event.wait(timeout=_DONE_TIMEOUT - elapsed)
                break
            wait = next_tick - elapsed
            if wait > 0 and rec.event.wait(timeout=wait):
                break   # 信号到达 → 收尾
            if rec.event.is_set():
                break
            mins = int(round((time.monotonic() - start) / 60.0))
            _progress_tick(rec, mins)

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
        _dlog("仅已读 msgId=%s conv=%s" % (msg.msg_id or "-", (msg.conv_id or "-")[:16]))
        _mark_read(msg.conv_id, msg.msg_id)   # 仅标记已读（best-effort，失败只记日志）
    return False   # 关键：不消费，text_reply 等仍会处理并回复


def on_reply_sent(conv_id, conv_type, ok):
    """收到"回复已发出"信号：唤醒对应 worker 切换完成/失败文字表情。

    ok=None → **静默收尾**：只移除「处理中」，不贴完成/失败终态。给"这条已经处理完了，
    但数字员工有意不出声"的场景用（如主管审核选择忽略，#109）——贴「完成」会让提问者
    看到 ✅ 却永远等不到回复，贴「未完成」又像是系统故障，两个终态都在说谎。
    注意 send_reply 的 outcome_ok=None 是另一个意思（"用投递结果"），那条路径永远不会
    把 None 传到这里；只有能力直接调 dispatch_reply_sent 时才用得上。
    """
    with _pending_lock:
        rec = _pending.get(conv_id)
    if rec is not None and not rec.event.is_set():
        rec.ok = None if ok is None else bool(ok)
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
