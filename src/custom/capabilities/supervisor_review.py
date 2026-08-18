"""supervisor_review — 主管审核回路（custom 能力）

数字员工要开口说的话，先 **AI 出草稿 → 转交主管审核 → 主管裁决后才发出**。
主管改写过的答案沉淀进知识库，下次同类问题 AI 能自己答对（见 brain._effective_system_prompt）。

流程：
    张三私聊/群里问 → AI 生成草稿（不发张三）→ 转交主管单聊（带短号 #N）
             → 主管裁决：给卡片**贴 👍/❌**（零打字）或回「#N 同意 / 忽略 / 改：<答案>」
             → 最终答案发回原会话（张三全程只看到一条）

三条裁决入口，最终都汇到 `_execute_verdict`（#107）：
  1. **贴表情**（`_poll_reactions_once`）—— 最高频的「同意/忽略」降到一次点击
  2. **引用回复 = 正式作答**（`classify_line` → `extra['quoted_seq']`）—— 引用**任意一条**
     与那次审核相关的消息都能定位（8 种消息正文开头都带「待审 #N」，bridge 抽出来放进
     行尾）。内容交大模型判"能不能原样发给提问者"，能就直接发。**忽略/超时的审核事后
     照样能补裁**（含重启之前的，见 _revive_seq）；已经答过的默认拒绝改口，除非
     明说「更正：」
  3. **文字**（`_parse_verdict`）—— 改写时仍然要打字，这是本来就避不掉的

设计要点：
- **priority=30**：晚于 question(20)/permission(15) 不抢它们的答复，早于 image(40)/
  forward(50)/text_reply(100) —— 必须在 text_reply 把草稿发给提问者之前截住。
- **群聊同样拦**（#107）：群里的回答是公开发言，比单聊更需要过一遍主管。范围与
  text_reply 对齐 —— 凡是数字员工本来会开口回的（订阅决定：整群订阅=每条，
  DWS_EVENT_AT=只被 @ 的），都先审。要退回"只审单聊"设 SUPERVISOR_REVIEW_O2O_ONLY=1。
- **群里主管的提问也走审核**（#107）：审核闸门管的是"数字员工说什么"，不是"谁在问"。
  主管在群里问 → 草稿私聊发给主管确认 → 同意后才公开发到群里。
- **裁决只认主管单聊**：群里主管发的一律当新提问。否则主管在群里说一句「同意」会莫名
  其妙放行另一个人的待审，且审核细节会暴露在群里。
- **主管单聊无待审时照常放行**：return False 交 text_reply 正常对话，否则主管没法和
  数字员工聊天，且会与自己的裁决互相触发。
- **身份判定靠显示名**：bridge 只传 sender 显示名不传 userId（见 dws_event_bridge
  ._to_connect_line），故用 AGENT_SUPERVISOR_NAME + AGENT_SUPERVISOR_ALIASES 比对
  msg.user。发给主管则用 userId 走 `dws chat message send --user`。
- **短号 #N 对应**：主管一个会话里可能同时挂多人的问题。优先级是
  **引用 > #N > 最近一条**（单人场景零心智负担）。#N 由审核流水保证跨重启唯一。
  **铁律：只要主管引用了消息，就绝不回落"最近一条"**（见 _resolve_target）——回落是
  唯一能把答案发给错的人的路径，而引用恰恰是主管指向性最明确的动作。
- **认不出就问，绝不猜**（#107 D）：老逻辑「不在白名单=改写」会把主管随口回的「可以发」
  当答案发给提问者（群聊里直接公开）。现在短文本认不出 → 保留待审 + 回一句澄清；
  表情认不出 → 同理（还会把表情名记进日志，方便补进 SUPERVISOR_APPROVE_EMOJIS）。
- **给主管发卡片不用 send_reply**：卡片不是"对提问者的回复"，不该驱动 ack 状态机收尾。
  发给提问者的最终答案才用 send_reply（让 ack 回执正常落地）。
- **不发消息的出口要显式收尾 ack**（#109）：ack 的"处理中→完成"只由 send_reply 广播的
  reply-sent 驱动，而本能力有几条路径压根不产生 send_reply（主管的裁决消息、忽略、
  转交失败且无草稿、超时且无草稿）。不收尾 → 那条消息的 ack worker 一直等信号，每
  ACK_PROGRESS_INTERVAL 秒往会话播一条「仍在处理中」直到 ACK_DONE_TIMEOUT（默认 65
  分钟）。群聊里尤其刺眼：主管已经决定忽略，群里还在报进度。见 _close_ack。
- **超时 = 不回复，不是放行**：默认 600s 无人裁决 → 按「忽略」处理（提问者收不到任何
  东西）。没人管**不等于**默认同意 —— 自动放行意味着一条从没被人看过的草稿被发出去，
  群里更是直接公开发言，等于在审核闸门上开了个洞。沉默的代价可见可补救，发错话的不是。
  超时的审核在流水里记为 expired，主管**事后引用与它相关的任意一条消息**随时能补裁
  （见 _revive_seq），所以问题不会被丢掉，只是没人替他答应。

开关：CAP_SUPERVISOR_REVIEW_ENABLED（模板默认关，config/constants.local.sh 里开）。
"""

import json
import os
import re
import threading
import time
from collections import OrderedDict

from core.agent_common import PROFILE, env_flag, log, submit_reply, _run_cli
from core.brain import generate_reply_ex
from core.capabilities import Capability, dispatch_reply_sent, register
from core.inbound import InboundMessage, KIND_TEXT, parse_line
from core.replier import send_reply
from custom import msgstore, quoted
from custom.identity import is_supervisor, supervisor_id, supervisor_names

# 超时未裁决 → 按**不回复**处理并归档（秒），主管事后引用卡片仍可补裁。
# 0=永不超时（待审一直挂着，提问者也一直等不到，慎用）。
_TIMEOUT = int(os.environ.get("SUPERVISOR_REVIEW_TIMEOUT", "600"))
# 仅拦单聊（=1 时群聊放行，回到 #107 之前的行为）。默认 0：群里的公开发言更该先审。
_O2O_ONLY = env_flag("SUPERVISOR_REVIEW_O2O_ONLY", default=False)
# 会话类型：单聊。群聊为 "2"（见 core.inbound）
_CONV_TYPE_O2O = "1"
# 知识库路径（相对 PROJECT_DIR）
_KNOWLEDGE_FILE = os.environ.get("AGENT_KNOWLEDGE_FILE", "knowledge/supervisor_qa.jsonl")

# 表情 → 裁决映射。钉钉在 list-emotion-replies 里返回的**表情名**（不是 unicode 码点），
# 各客户端/版本可能不同 —— 认不出的表情不猜，回主管问一句并把名字记进日志，见 _emoji_action。
_APPROVE_EMOJIS = {e.strip() for e in os.environ.get(
    "SUPERVISOR_APPROVE_EMOJIS", "赞,好的,OK,👍,✅").split(",") if e.strip()}
_IGNORE_EMOJIS = {e.strip() for e in os.environ.get(
    "SUPERVISOR_IGNORE_EMOJIS", "不行,取消,❌,🚫").split(",") if e.strip()}


def _env_int(key, default):
    """读一个整数配置。空串/垃圾值一律回落默认值。

    这些常量在 import 期求值，而本模块又在 custom.capabilities 的 import 链上 —— 直接
    int("") 抛 ValueError 会让**所有**能力注册失败，数字员工对谁都不吭声了。
    """
    try:
        return int(str(os.environ.get(key, "")).strip() or default)
    except (TypeError, ValueError):
        log(f"supervisor_review: {key} 值非法，用默认 {default}")
        return default


# 草稿超过多少行就在卡片里折叠（全文另发一条）。0=不折叠
_CARD_DRAFT_MAX_LINES = _env_int("SUPERVISOR_CARD_DRAFT_MAX_LINES", 12)
# 短于此长度且认不出的裁决 → 问一句而不是当答案发出去（#107 D）。
# 注意这只兜"没引用卡片"的路径；引用回复走大模型判意图（见 _judge_directly_sendable）。
_UNCLEAR_MAX_LEN = _env_int("SUPERVISOR_UNCLEAR_MAX_LEN", 8)
# 引用回复的内容是否交大模型判"能不能原样发给提问者"。0=关（退回长度启发式）
_JUDGE_QUOTED = env_flag("SUPERVISOR_JUDGE_QUOTED", default=True)

# 裁决关键词。approve 收得比 ignore 宽：主管想放行时说法五花八门（「可以发」「就这样」），
# 认不出就会掉进 rewrite 把这几个字当答案公开发出去 —— 这是 #107 要修的事故。
_APPROVE_KEYWORDS = {"同意", "ok", "可以", "放行", "approve", "y", "yes", "行",
                     "可以发", "就这样", "发吧", "没问题", "可以的", "同意发", "通过"}
_IGNORE_KEYWORDS = {"忽略", "不回", "跳过", "ignore", "skip", "算了", "不用回", "别回"}
# 无歧义改写前缀：主管显式声明"下面是答案"，跳过一切猜测
_REWRITE_PREFIXES = ("改：", "改:", "答：", "答:", "更正：", "更正:")

# 待审表：seq -> {asker, asker_conv_id, asker_conv_type, asker_msg_id, question,
#                draft, ts, timer}
_pending = {}
_pending_lock = threading.Lock()
_seq_counter = 0

# 审核流水（#108）：短号 #N 必须**跨重启唯一**。以前 _seq_counter 是纯内存的，重启归零，
# 主管会话里于是同时存在多张「待审 #3」—— 已经造成过一次静默事故（反查卡片时匹配到上一
# 轮的同号卡片，贴表情从此无效，commit 531d039）。
# 放 knowledge/ 下而不是根目录点文件：那些会被 bin/core/lib.sh clean_runtime_state()
# 在 stop/reboot 时删掉，而这份要跨重启活着。knowledge/ 已整目录 gitignore（含真实对话）。
_JOURNAL_FILE = os.environ.get("SUPERVISOR_REVIEW_JOURNAL",
                               "knowledge/supervisor_reviews.jsonl")
# 只读末尾这么多字节重建状态：本模块在 custom.capabilities 的 import 链上，跑几个月的
# 流水会有几十 MB，每次重启全量 json.loads 不可接受。seq 单调递增，尾部就够拿高水位。
_JOURNAL_TAIL_BYTES = _env_int("SUPERVISOR_REVIEW_JOURNAL_TAIL", 262144)
_journal_lock = threading.Lock()
_replay_lock = threading.Lock()
_replayed = False
# 历史 review：seq -> {state, asker, asker_conv_id, asker_conv_type, question, draft}
# state ∈ open | answered | ignored | expired。有界，replay 时从流水尾部重建。
_history = OrderedDict()
_HISTORY_MAX = 256
# 已处理过的表情事件 id。**必须持久化**：dws 是 at-least-once，重连/重启会重投旧事件，
# 而 core 的声明式 dedup 是内存态、重启即丢。重投一条几小时前的 👍 + "忽略/超时的可以
# 事后补裁"的语义 = 把当时没人放行的草稿幽灵般发出去。
_handled_events = OrderedDict()
_HANDLED_EVENTS_MAX = 512

# 表情回应事件（#108）：bridge 产出的专用行，kind 定义在 custom（core 不解读 kind，
# 不必为此改 core/inbound.py）
KIND_REACTION = "reaction"
_REACTION_RE = re.compile(
    r"^\[connect\] 表情回应 @(?P<op>.+?): (?P<name>.+?) \(convType=(?P<ct>\d+) "
    r"convId=(?P<conv>[^\s)]*) reactedMsgId=(?P<mid>[^\s)]*) "
    r"reactionOp=(?P<rop>[^\s)]*) eventId=(?P<eid>[^\s)]*)\)")
# 贴错了立刻撤的宽限期（秒）。轮询时代 5s 内撤掉等于没发生，改成事件驱动后每次贴都是
# **不可撤的即时裁决**，而"手滑把没审过的草稿公开发到群里"不该是一键操作。0=不宽限。
_REACTION_DEBOUNCE = float(os.environ.get("SUPERVISOR_REACTION_DEBOUNCE", "3") or 0)
_debounce_timers = {}

# 引用回复的标记（bridge 在行尾追加，见 classify_line）
_QUOTED_RE = re.compile(r"\bquotedMsgId=([^\s)]+)")
_QUOTED_SEQ_RE = re.compile(r"\bquotedSeq=(\d+|\?)")
# 显式覆盖口令：答案已经发出去之后要改口，必须明说（见 _resolve_target）
_AMEND_PREFIXES = ("更正：", "更正:")


# ---------------------------------------------------------------------------
# 主管身份 / 发送
# ---------------------------------------------------------------------------

def _supervisor_id():
    """主管 userId（发卡片用）。取不到返回 ""。"""
    return supervisor_id()


def _supervisor_names():
    """主管显示名集合（判定入站发送人用）。

    bridge 只传显示名，故必须靠名字比对。同一人可能显示为 "hugozhu"/"朱鸿"，
    用 AGENT_SUPERVISOR_ALIASES 补别名。
    """
    return supervisor_names()


def _is_supervisor(user):
    """该发送人是主管吗？"""
    return is_supervisor(user)


def _send_to_supervisor(text):
    """给主管单聊发一条消息（按 userId）。返回 True=已发出。

    走 dws chat message send --user，**不经 send_reply** —— 这不是对提问者的回复，
    不该广播 reply-sent 驱动 ack 收尾。
    """
    sid = _supervisor_id()
    if not sid:
        log("supervisor_review: AGENT_SUPERVISOR_USER_ID 未配置，无法转交主管")
        return False
    rc, out = _run_cli(["chat", "message", "send", "--user", sid,
                        "--text", text, "--ai-tag=false"])
    if rc != 0:
        log(f"supervisor_review: 转交主管失败 rc={rc} out={out[:200]}")
        return False
    return True


def _close_ack(conv_id, conv_type, ok=True):
    """显式收尾这条消息的 ack 回执（#109）。

    只在**不会有 send_reply** 的出口调用（见模块 docstring）。ok=None → 只移除
    「处理中」不贴终态：忽略场景没给答案，贴「完成」是骗提问者。
    best-effort —— 回执收不了尾绝不能拖累审核主流程。
    """
    if not conv_id:
        return
    try:
        dispatch_reply_sent(conv_id, conv_type, ok)
    except Exception as e:
        log(f"supervisor_review: 收尾 ack 失败 {e}")


# ---------------------------------------------------------------------------
# 渲染 / 解析（纯函数，可单测）
# ---------------------------------------------------------------------------

def _scene(conv_type):
    """答案将发到哪 —— 主管必须一眼看出这条会不会公开（群聊 vs 单聊）。"""
    return "单聊" if str(conv_type) == _CONV_TYPE_O2O else "群聊 · 答案会公开发到群里"


def _card_marker(seq):
    """卡片首行的唯一前缀。`**` 收尾保证 #1 不会前缀命中 #11。"""
    return f"📋 **待审 #{seq}**"


def _fold_draft(draft):
    """长草稿折叠 → (卡片里显示的部分, 需另发的全文 or None)。

    草稿动辄几十行，把审核信息（问题/操作提示）挤出一屏，手机上要滚半天才找得到怎么裁决。
    折叠后全文紧跟着单独发一条 —— 主管想细看仍然看得到，只是不再占着卡片。
    """
    text = draft or ""
    lines = text.splitlines()
    if _CARD_DRAFT_MAX_LINES <= 0 or len(lines) <= _CARD_DRAFT_MAX_LINES:
        return text, None
    head = "\n".join(lines[:_CARD_DRAFT_MAX_LINES])
    return f"{head}\n…（共 {len(lines)} 行，全文见下条）", text


def _card_actions(seq):
    """卡片底部的操作提示 —— 压成一行，别把草稿挤下去。"""
    line = f"「#{seq} 同意」放行 / 「#{seq} 忽略」不回 / 「#{seq} 改：<答案>」改写"
    return line + "　贴 👍 / ❌ 也行"


def _render_card(seq, asker, question, draft, conv_type=_CONV_TYPE_O2O):
    """渲染给主管的待审卡片。长草稿在此折叠（全文由调用方另发一条）。"""
    shown, _ = _fold_draft(draft)
    return "\n".join([
        f"{_card_marker(seq)}　来自：**{asker}**（{_scene(conv_type)}）",
        "",
        "**【问题】**",
        question,
        "",
        "**【AI 草稿】**",
        shown or "（AI 未能生成草稿）",
        "",
        "---",
        _card_actions(seq),
    ])


def _strip_seq(text):
    """把开头的「#N」剥掉 → (seq|None, 余下正文)。"""
    t = (text or "").strip()
    if not t.startswith("#"):
        return None, t
    rest = t[1:].lstrip()
    num = ""
    for ch in rest:
        if ch.isdigit():
            num += ch
        else:
            break
    if not num:
        return None, t
    return int(num), rest[len(num):].strip()


def _has_amend_prefix(text):
    """主管有没有明说"要推翻已经发出去的答复"（更正：）。"""
    _, rest = _strip_seq(text)
    return any(rest.startswith(p) for p in _AMEND_PREFIXES)


def _has_rewrite_prefix(text):
    """主管有没有显式声明"下面是答案"（改：/答：）—— 有就不必再劳烦大模型判意图。"""
    _, rest = _strip_seq(text)
    return any(rest.startswith(p) for p in _REWRITE_PREFIXES)


def _parse_verdict(text):
    """解析主管回复 → (seq|None, action, payload)。

    action ∈ approve | ignore | rewrite | unclear。seq=None 表示未指名（对应最近一条待审）。
    形如 "#3 同意" / "#3 改：你应该这样答…" / "同意" / 一整段答案。

    **认不出的短文本返回 unclear 而不是 rewrite**（#107 D）：老逻辑「不在白名单=改写」
    会把主管随口回的「可以发」「这个不太对」当成答案 send_reply 给提问者，群聊场景直接
    公开发出去。宁可多问一句，也不能替主管发一句他没打算发的话。长文本仍按改写处理
    —— 真答案通常不短，且这条路径是主管明确在写东西。
    """
    seq, t = _strip_seq(text)
    for pre in _REWRITE_PREFIXES:
        if t.startswith(pre):
            return seq, "rewrite", t[len(pre):].strip()
    low = t.lower()
    if low in _APPROVE_KEYWORDS:
        return seq, "approve", ""
    if low in _IGNORE_KEYWORDS:
        return seq, "ignore", ""
    if len(t) < _UNCLEAR_MAX_LEN:
        return seq, "unclear", t
    return seq, "rewrite", t


def _emoji_action(emoji):
    """表情名 → 裁决动作；不认识返回 None（**不猜**，交给调用方问主管一句）。"""
    e = (emoji or "").strip()
    if e in _APPROVE_EMOJIS:
        return "approve"
    if e in _IGNORE_EMOJIS:
        return "ignore"
    return None


# ---------------------------------------------------------------------------
# 待审表操作
# ---------------------------------------------------------------------------

def _pop(seq):
    """取出并注销一条待审（连带取消超时定时器）。"""
    with _pending_lock:
        p = _pending.pop(seq, None)
    if p and p.get("timer"):
        try:
            p["timer"].cancel()
        except Exception:
            pass
    return p


def _repend(seq, p):
    """把已取出的待审放回去（裁决没能真正完成时）。

    `_pop` 顺手取消了超时定时器，所以放回去要重新起一个 —— 否则这条待审既没被裁决、
    也没有兜底，提问者会被无限期挂着（连带 ack 每 5 分钟播「仍在处理中」，见 #109）。
    **按原始 ts 续上剩余时间，不是重新计时**：兜底的契约是"提问者最迟 _TIMEOUT 秒后
    有回音"，每次放回都续满会把这个上限无限推后。
    """
    p["timer"] = None
    if _TIMEOUT > 0:
        left = _TIMEOUT - (time.time() - p.get("ts", time.time()))
        timer = threading.Timer(max(1.0, left), _timeout, args=(seq,))
        timer.daemon = True
        p["timer"] = timer
    with _pending_lock:
        _pending[seq] = p
    if p["timer"]:
        p["timer"].start()


def _asker_label(p):
    """回执里怎么指代提问者 —— 群聊要标出来，主管才知道答案是公开发出去的。"""
    if str(p.get("asker_conv_type")) == _CONV_TYPE_O2O:
        return p["asker"]
    return f"{p['asker']}（群里）"


def _latest_seq():
    """最近登记的一条待审 seq（主管不带编号回复时用）。无待审返回 None。"""
    with _pending_lock:
        if not _pending:
            return None
        return max(_pending, key=lambda s: _pending[s]["ts"])


# ---------------------------------------------------------------------------
# 审核流水（journal）：让短号 #N 跨重启唯一，并留下事后可查的裁决记录
# ---------------------------------------------------------------------------

def _journal_path(path=None):
    """流水文件绝对路径。path 参数是给测试注入用的（同 core.brain 的 ext-session 写法）。"""
    p = path or _JOURNAL_FILE
    if os.path.isabs(p):
        return p
    return os.path.join(os.environ.get("PROJECT_DIR", os.getcwd()), p)


def _journal_append(rec, path=None):
    """往流水追加一条记录。返回 True=已落盘。

    **不能照抄 _record_knowledge 的无锁 append**：那里一条记录几十字节，这里 open 记录
    带完整草稿（几十行），Python 的 write() 会拆成多次 write(2)，两个 reply-pool 线程
    并发追加就会把长记录撕开、互相插进对方中间。故：模块锁 + 单个 fd 内写完。
    """
    line = (json.dumps(rec, ensure_ascii=False) + "\n").encode("utf-8")
    p = _journal_path(path)
    try:
        d = os.path.dirname(p)
        if d:
            os.makedirs(d, exist_ok=True)
        with _journal_lock:
            fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                written = 0
                while written < len(line):
                    written += os.write(fd, line[written:])
            finally:
                os.close(fd)
        return True
    except OSError as e:
        log(f"supervisor_review: 写审核流水失败 {e}")
        return False


def _journal_tail(path=None, max_bytes=None):
    """读流水**末尾**若干字节并解析 → (records, 坏行数)。文件不存在返回 ([], 0)。"""
    p = _journal_path(path)
    cap = max_bytes or _JOURNAL_TAIL_BYTES
    try:
        size = os.path.getsize(p)
        with open(p, "rb") as f:
            if size > cap:
                f.seek(size - cap)
                f.readline()      # 丢掉被切半的第一行
            raw = f.read()
    except OSError:
        return [], 0              # 没有流水 = 还没审过东西，不是错误
    recs, bad = [], 0
    for ln in raw.decode("utf-8", "replace").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            r = json.loads(ln)
        except ValueError:
            bad += 1
            continue
        if isinstance(r, dict):
            recs.append(r)
        else:
            bad += 1
    return recs, bad


def _remember_history(seq, **fields):
    """更新历史索引（有界 FIFO）。"""
    rec = _history.get(seq) or {}
    rec.update({k: v for k, v in fields.items() if v is not None})
    _history[seq] = rec
    _history.move_to_end(seq)
    while len(_history) > _HISTORY_MAX:
        _history.popitem(last=False)
    return rec


def _replay(path=None):
    """从流水重建短号高水位 + 历史索引。返回高水位。

    坏行**计数并上报**，不像知识库那样静默跳过 —— 这个文件决定 #N 从几开始，
    悄悄少算一行就是重号事故。
    """
    global _seq_counter
    recs, bad = _journal_tail(path)
    high = 0
    for r in recs:
        n = r.get("n")
        if not isinstance(n, int):
            continue
        high = max(high, n)
        t = r.get("t")
        if t == "open":
            _remember_history(n, state="open", asker=r.get("asker"),
                              asker_conv_id=r.get("conv_id"),
                              asker_conv_type=r.get("conv_type"),
                              asker_msg_id=r.get("msg_id"),
                              question=r.get("q"), draft=r.get("draft"))
        elif t == "done":
            _remember_history(n, state=r.get("action") or "answered",
                              answer=r.get("answer"))
        elif t == "expire":
            _remember_history(n, state="expired")
    for r in recs:
        if r.get("t") == "rx" and r.get("id"):
            _remember(_handled_events, r["id"], _HANDLED_EVENTS_MAX)
    with _pending_lock:
        _seq_counter = max(_seq_counter, high)
    if bad:
        log(f"supervisor_review: 审核流水有 {bad} 行无法解析（已跳过），"
            f"短号高水位取到 #{high}")
    if high:
        log(f"supervisor_review: 已从流水恢复，短号从 #{high + 1} 续起"
            f"（历史 {len(_history)} 条）")
    return high


def _ensure_replayed(path=None):
    """首次用到短号时重建一次状态。

    **不在 import 期做**：本模块在 custom.capabilities 的 import 链上，这里抛异常会让
    所有能力注册失败、数字员工对谁都不吭声（同 _env_int 的教训）。
    """
    global _replayed
    with _replay_lock:
        if _replayed:
            return
        _replayed = True
        try:
            _replay(path)
        except Exception as e:      # 恢复不了就当没有历史，绝不能拖垮能力注册
            log(f"supervisor_review: 重建审核流水失败，退化为从 #1 起 {e}")


def _record_decision(seq, state, answer="", p=None):
    """把裁决结果落进流水 + 历史索引。best-effort（裁决本身已经生效了）。

    state ∈ answered | ignored | expired。留档是为了主管事后引用旧消息时，能分辨
    "这条已经答过了"和"这条当时没人理"—— 两者的补裁语义不同。
    """
    _remember_history(seq, state=state, answer=answer or None)
    _journal_append({"t": "expire" if state == "expired" else "done", "n": seq,
                     "action": state, "answer": (answer or "")[:2000],
                     "ts": time.strftime("%Y-%m-%d %H:%M:%S")})
    # 同时挂到**提问者那条原始消息**上（#111）：人事后查的是"我问的那句后来怎么样了"，
    # 而不是"第 N 号待审"。best-effort，落盘失败不影响裁决本身。
    rec = p or _history.get(seq) or {}
    try:
        msgstore.record_feedback(rec.get("asker_conv_id", ""), rec.get("asker_msg_id", ""),
                                 seq=seq, action=state, answer=answer,
                                 by=", ".join(sorted(_supervisor_names())) or "主管")
    except Exception as e:
        log(f"supervisor_review: 记录裁决反馈失败 {e}")


def _next_seq():
    """分配一个跨重启唯一的短号 → (seq, 是否已落盘)。

    **先落盘再返回**：号分配了却没记下来，重启后会重号，正是这次要修的事故。
    落盘失败不放弃转交（丢掉提问者的问题更糟），但要显式告警，让人知道退化了。
    """
    global _seq_counter
    _ensure_replayed()
    with _pending_lock:
        _seq_counter += 1
        seq = _seq_counter
    return seq, _journal_append({"t": "seq", "n": seq})


# ---------------------------------------------------------------------------
# 知识库
# ---------------------------------------------------------------------------

def _knowledge_path():
    """知识库绝对路径。"""
    base = os.environ.get("PROJECT_DIR", os.getcwd())
    p = _KNOWLEDGE_FILE
    return p if os.path.isabs(p) else os.path.join(base, p)


def _record_knowledge(question, answer, asker=""):
    """把主管改写过的 Q→A 追加进知识库（JSONL 一行一条）。best-effort。

    只在**改写**时记录：主管点「同意」说明 AI 本来就答对了，没有新知识。
    """
    q = (question or "").strip()
    a = (answer or "").strip()
    if not q or not a:
        return False
    path = _knowledge_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        rec = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "asker": asker,
            "question": q,
            "answer": a,
        }
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        log(f"supervisor_review: 已学习 Q={q[:40]!r} → A={a[:40]!r}")
        return True
    except OSError as e:
        log(f"supervisor_review: 写知识库失败 {e}")
        return False


# ---------------------------------------------------------------------------
# 贴表情裁决（#107 A）：轮询卡片上的表情回应
#
# 为什么轮询而不订阅 user_im_message_reaction_o2o：reaction 事件的 message_id 是**被贴
# 表情那条原消息的 id**，会和 ack._pending[conv_id] / group_gate 的 msgId 表撞车 —— 用户
# 等回复时随手点个赞就会把「处理中」进度表情提前摘掉。且该事件没有 _all 变体，而现网用的
# 正是 DWS_EVENT_O2O_ALL=1。轮询把改动全锁在本文件里，代价只是最多一个轮询周期的延迟。
# ---------------------------------------------------------------------------

# 与 bridge 的 _REVIEW_SEQ_RE 同一套规则：锚在正文开头，防外部可控文本注入假编号
_REVIEW_SEQ_RE = re.compile(r"^\s*[^\w\s]*\s*\**待审\s*#(\d+)")


def _seq_of_message(msg_id, conv_id=""):
    """被贴表情的那条消息属于第几号审核；认不出返回 None。

    先查**本地消息存储**（#111，零网络往返）；查不到再回落 `list-by-ids` —— 存储启用
    之前的老消息不在库里，回落保证平滑过渡。两条路都是取回正文再抽「待审 #N」，与引用
    回复用的是同一套解析，于是 8 种消息都能贴表情（以前只有卡片行）。
    """
    if not msg_id:
        return None
    if conv_id:
        rec = msgstore.find(conv_id, msg_id)
        if rec:
            hit = _REVIEW_SEQ_RE.match(str(rec.get("text") or ""))
            if hit:
                return int(hit.group(1))
    rc, out = _run_cli(["chat", "message", "list-by-ids", "--msg-ids", msg_id])
    if rc != 0:
        log(f"supervisor_review: 取被贴表情的消息失败 rc={rc} out={out[:120]}")
        return None
    try:
        msgs = (json.loads(out).get("result") or {}).get("messages") or []
    except (ValueError, AttributeError, TypeError) as e:
        log(f"supervisor_review: 解析被贴表情的消息失败 {e}")
        return None
    for m in msgs:
        hit = _REVIEW_SEQ_RE.match(str(m.get("content") or ""))
        if hit:
            return int(hit.group(1))
    return None


def _on_reaction(msg):
    """主管给某条审核消息贴了表情 → 裁决。跑在 reply pool，不在入站线程里。"""
    _ensure_replayed()
    if msg.msg_id and not _remember(_handled_events, msg.msg_id, _HANDLED_EVENTS_MAX):
        log(f"supervisor_review: 表情事件 {msg.msg_id[:16]} 已处理过（重投），忽略")
        return
    if msg.msg_id:
        _journal_append({"t": "rx", "id": msg.msg_id})
    extra = msg.extra or {}
    emoji = extra.get("reaction") or ""
    action = _emoji_action(emoji)
    seq = _seq_of_message(extra.get("reacted_msg_id"), msg.conv_id)
    if seq is None:
        log(f"supervisor_review: 表情 {emoji!r} 贴在非审核消息上，忽略")
        return
    if action is None:
        _report_unknown_emoji(seq, emoji)
        return
    target, err = _target_from_seq(seq, "")
    if target is None:
        _send_to_supervisor(err or f"⚠️ 待审 #{seq} 处理不了。")
        return
    _execute_verdict(target, action, source=f"贴「{emoji}」")


def _schedule_reaction(msg):
    """给贴表情留一点反悔时间，然后执行。

    轮询时代贴错了 5 秒内撤掉等于没发生；改成事件驱动后每一次贴都是**不可撤的即时
    裁决**，而"手滑把一条没审过的草稿公开发到群里"不该是一键就能干成的事。
    宽限期内撤掉（reactionOp=remove）就取消。
    """
    key = (msg.extra.get("reacted_msg_id"), msg.extra.get("reaction"))
    old = _debounce_timers.pop(key, None)
    if old:
        old.cancel()
    if msg.extra.get("reaction_op") == "remove":
        log(f"supervisor_review: 主管撤回了表情 {key[1]!r}，取消该裁决")
        return
    if _REACTION_DEBOUNCE <= 0:
        submit_reply(_on_reaction, msg)
        return
    t = threading.Timer(_REACTION_DEBOUNCE, lambda: submit_reply(_on_reaction, msg))
    t.daemon = True
    _debounce_timers[key] = t
    t.start()


def _supervisor_emojis(entries):
    """这条消息上**主管**贴过的所有表情名（按返回顺序）。别人贴的一律不算裁决。

    replyUsers 给的是显示名，与 identity.is_supervisor 的判定口径正好一致（bridge 也只
    传显示名）。返回全部而不是第一个 —— 主管可能先误贴一个我们不认识的，再补贴 👍，
    只看第一个会让那个 👍 永远轮不上。
    """
    out = []
    for e in entries or []:
        users = e.get("replyUsers") or []
        if not isinstance(users, (list, tuple)):
            continue
        for u in users:
            if isinstance(u, str) and _is_supervisor(u):
                emoji = (e.get("emoji") or "").strip()
                if emoji:
                    out.append(emoji)
                break
    return out


def _remember(memo, key, cap):
    """有界 FIFO 记忆：已存在返回 False，新记下返回 True。"""
    if key in memo:
        return False
    memo[key] = True
    while len(memo) > cap:
        memo.popitem(last=False)
    return True


def _report_unknown_emoji(seq, emoji):
    """主管贴了个我们不认识的表情 —— 问一句，别猜。

    钉钉在 list-emotion-replies 里返回的是**表情名**，各客户端/版本的叫法无法离线穷举。
    这条提示就是发现真实名字的手段：主管看到后可以把它补进 SUPERVISOR_APPROVE_EMOJIS。
    """
    log(f"supervisor_review: #{seq} 收到未映射的表情 {emoji!r} "
        f"（可加进 SUPERVISOR_APPROVE_EMOJIS / SUPERVISOR_IGNORE_EMOJIS）")
    _send_to_supervisor(
        f"🤔 你在待审 #{seq} 上贴了「{emoji}」，我不认识这个表情，没敢动。\n"
        f"放行请贴 {'/'.join(sorted(_APPROVE_EMOJIS)[:3])}，忽略请贴 "
        f"{'/'.join(sorted(_IGNORE_EMOJIS)[:3])}，或直接回「#{seq} 同意」。")


# ---------------------------------------------------------------------------
# 拦截：生成草稿 → 转交主管
# ---------------------------------------------------------------------------

def _timeout(seq):
    """超时未裁决 → **按不回复处理**，并归档等主管事后补裁。

    为什么默认是"不回复"而不是"放行草稿"：没人管**不等于**默认同意。自动放行意味着
    一条**从没被人看过**的 AI 草稿被发出去，群聊场景更是直接公开发言——审核闸门存在
    的全部意义就是不让这种事发生，超时兜底不该在闸门上开个洞。沉默的代价（提问者没
    收到答案）是可见、可补救的；发错话的代价不是。

    补救路径：流水里记为 expired，主管什么时候想起来，引用那次审核的任意一条消息回
    一句就能补答（见 _revive_seq）。所以这不是"问题被丢掉"，只是"没人替你答应"。
    """
    p = _pop(seq)
    if not p:
        return
    log(f"supervisor_review: #{seq} 主管 {_TIMEOUT}s 未裁决 → 按不回复处理（已归档，可事后补裁）")
    _record_decision(seq, "expired", p=p)
    # 与"忽略"完全一致：提问者那条静默收尾，不贴「完成」也不贴「未完成」
    _close_ack(p["asker_conv_id"], p["asker_conv_type"], ok=None)
    _send_to_supervisor(
        f"⏰ 待审 #{seq}（来自 {_asker_label(p)}）{_TIMEOUT}s 未裁决，已按**不回复**处理。"
        f"　想补答就**引用这条或那张卡片**回一句（同意 / 改：<答案>）。")


def _revive_seq(seq):
    """把一条已经了结的审核重新挂回待审表（主管事后补裁）。返回 True=挂回了。

    数据来自持久化的 _history，所以**重启之前的审核照样能补裁** —— 这正是 #108 要的
    "任何时候"。重新计时（刷新 ts）：主管既然回来处理了，就该给一个完整的裁决窗口。
    """
    rec = _history.get(seq)
    if not rec or not rec.get("asker_conv_id"):
        return False
    _repend(seq, {
        "asker": rec.get("asker") or "提问者",
        "asker_conv_id": rec.get("asker_conv_id"),
        "asker_conv_type": rec.get("asker_conv_type") or _CONV_TYPE_O2O,
        "question": rec.get("question") or "",
        "draft": rec.get("draft") or "",
        "asker_msg_id": rec.get("asker_msg_id") or "",
        "ts": time.time(),
        "timer": None,
    })
    _remember_history(seq, state="open")
    log(f"supervisor_review: #{seq} 主管事后补裁，已从历史重新挂回待审")
    return True


def _draft_and_forward(user, text, conv_type, conv_id, msg_id, quoted_msg_id=None):
    """后台线程：生成草稿 → 转交主管 → 登记待审。"""
    global _seq_counter
    # 被引用的消息要进上下文（#112）：先查本地存储，查不到回落 CLI。取不到就照常，
    # 只是少一段上下文，不该让整条消息处理不了。
    prompt, raw = text, False
    q = quoted.resolve(conv_id, quoted_msg_id) if quoted_msg_id else None
    if q:
        prompt, raw = quoted.build_prompt(user, text, q), True
        log(f"supervisor_review: 已补入被引用消息的上下文 from={q.get('sender')!r} "
            f"media={q.get('media')}")
    draft, status = generate_reply_ex(user, prompt, raw=raw, ctx={
        "conv_id": conv_id, "conv_type": conv_type, "msg_id": msg_id, "user": user,
    })
    # 草稿生成失败也要转交 —— 主管仍可手写答案，不能因模型挂了就把问题丢了
    if not draft:
        log(f"supervisor_review: AI 草稿生成失败(status={status})，仍转交主管 user={user}")

    seq, persisted = _next_seq()
    if not persisted:
        # 退化：短号仍然可用，但重启后可能与历史撞号（回到 #108 之前的行为）
        log(f"supervisor_review: #{seq} 未能写入审核流水，重启后该短号可能重复")
    card = _render_card(seq, user, text, draft, conv_type)
    if not _send_to_supervisor(card):
        # 转交失败：不能把提问者永久挂着 —— 直接把草稿发给他（有什么发什么）
        log(f"supervisor_review: #{seq} 转交主管失败，回退直接回复提问者 user={user}")
        if draft:
            send_reply(conv_id, conv_type, draft)   # send_reply 自带 reply-sent，ack 正常收尾
        else:
            _close_ack(conv_id, conv_type, ok=False)   # 草稿没有、主管也没转成 → 确实没办成
        return

    # **卡片一发出去就登记待审**：主管可能秒回/秒贴表情，登记晚一步那条裁决就会找不到
    # 待审、被当成普通对话落到 text_reply（提问者的 ack 也就永远收不了尾，#109 那类症状）。
    # 所以下面的补发全文、反查 msgId 这些网络往返都排在登记之后。
    timer = None
    if _TIMEOUT > 0:
        timer = threading.Timer(_TIMEOUT, _timeout, args=(seq,))
        timer.daemon = True
    with _pending_lock:
        _pending[seq] = {
            "asker": user,
            "asker_conv_id": conv_id,
            "asker_conv_type": conv_type,
            "asker_msg_id": msg_id,
            "question": text,
            "draft": draft or "",
            "ts": time.time(),
            "timer": timer,
        }
    if timer:
        timer.start()
    _journal_append({"t": "open", "n": seq, "asker": user, "conv_id": conv_id,
                     "conv_type": conv_type, "msg_id": msg_id, "q": text, "draft": draft or "",
                     "ts": time.strftime("%Y-%m-%d %H:%M:%S")})
    _remember_history(seq, state="open", asker=user, asker_conv_id=conv_id,
                      asker_conv_type=conv_type, asker_msg_id=msg_id,
                      question=text, draft=draft or "")
    log(f"supervisor_review: #{seq} 已转交主管审核 asker={user} q={text[:40]!r}")

    # 草稿被折叠了就把全文补一条（卡片只留前几行，见 _fold_draft）
    _, full = _fold_draft(draft)
    if full:
        _send_to_supervisor(f"📄 待审 #{seq} 的完整草稿：\n\n{full}")


def _execute_verdict(seq, action, payload="", source=""):
    """执行一条裁决（approve/ignore/rewrite）。**文字裁决与贴表情裁决共用这一份**。

    抽出来是为了两条入口的收尾语义完全一致 —— 尤其是 #109 的 ack 收尾，分两处写迟早
    漏一处。source 只影响回执措辞，让主管知道这条是打字裁的还是贴表情裁的。

    返回 True=已执行（待审已注销）；False=该 seq 已不在待审表（并发裁决时的正常竞态）。
    """
    if action not in ("approve", "ignore", "rewrite"):
        # 防御：unclear 之类的动作绝不能走到这儿 —— 下面的兜底分支会把 payload 当答案
        # 发给提问者，正是 #107 要消灭的事故。安全属性放在真正发消息的函数里，别只靠调用方。
        log(f"supervisor_review: #{seq} 未知裁决动作 {action!r}，不执行")
        return False
    p = _pop(seq)
    if not p:
        return False

    asker_conv_id = p["asker_conv_id"]
    asker_conv_type = p["asker_conv_type"]
    via = f"（{source}）" if source else ""

    if action == "ignore":
        log(f"supervisor_review: #{seq} 主管选择忽略{via}，不回复 asker={p['asker']}")
        _record_decision(seq, "ignored", p=p)
        # 提问者那条消息不会再收到任何回复 → 静默收尾，否则它每 5 分钟播「仍在处理中」
        _close_ack(asker_conv_id, asker_conv_type, ok=None)
        _send_to_supervisor(f"🚫 待审 #{seq} 已忽略{via}，未回复 {_asker_label(p)}。")
        return True

    if action == "approve":
        draft = p.get("draft") or ""
        if not draft:
            # 没草稿可放行 → 待审还得留着让主管手写，重新登记回去
            _repend(seq, p)
            _send_to_supervisor(f"⚠️ 待审 #{seq} 没有可放行的草稿，请直接写答案。")
            return True
        send_reply(asker_conv_id, asker_conv_type, draft)
        log(f"supervisor_review: #{seq} 主管同意{via}，草稿已发给 {p['asker']}")
        _record_decision(seq, "answered", draft, p=p)
        _send_to_supervisor(f"✅ 待审 #{seq} 已按草稿回复 {_asker_label(p)}{via}。")
        return True

    # rewrite：用主管的答案回复提问者，并学习
    answer = payload
    if not answer:
        _repend(seq, p)
        _send_to_supervisor(f"⚠️ 待审 #{seq} 未识别到答案内容，请重发。")
        return True
    send_reply(asker_conv_id, asker_conv_type, answer)
    _record_knowledge(p["question"], answer, asker=p["asker"])
    log(f"supervisor_review: #{seq} 主管改写并已回复 {p['asker']}")
    _record_decision(seq, "answered", answer, p=p)
    _send_to_supervisor(f"📝 待审 #{seq} 已用你的答案回复 {_asker_label(p)}，并已存入知识库。")
    return True


_JUDGE_PROMPT = """你在帮一个数字员工做一次判断，只判断、不作答。

主管**特意引用**了一条待审问题来回话——引用这个动作本身就表示"我在给提问者作答"。
所以**默认是 SEND**，只有明显是说给 AI 听的才判 HOLD。

- HOLD：这句话是冲着 AI 来的**指令或对草稿的评价**，提问者收到会莫名其妙。
  例如「这个不太对，你再想想」「太啰嗦了，简短点」「稍等我问问别人」「重写」。
- SEND：其余一切。**特别注意下面这些都算 SEND**：
  · 反问提问者的话（「你说的 X 是什么意思？」）—— 那是在跟提问者对话，不是在问 AI
  · 主管的观点、结论、补充说明，哪怕看起来像是在点评这件事
  · 口语、简短、不完整的句子 —— 主管本来就这么说话
  · 你拿不准的 —— 一律 SEND

【提问者的问题】
{question}

【AI 原本的草稿（仅供理解背景，主管的话**不是**在给这段草稿写评语）】
{draft}

【主管引用回复的内容】
{reply}

只回一个词：SEND 或 HOLD。"""


def _judge_directly_sendable(question, draft, reply):
    """主管引用回复的内容能不能原样发给提问者？True=能 / False=不能 / None=判不了。

    这是"用字数猜意图"（_UNCLEAR_MAX_LEN）的升级版：主管引用卡片就是在正式作答，
    但他也可能只是丢一句评语。长度分不开这两者（「这个回答我觉得不太对，你再想想」
    比「找财务小王签字」还长），交给大模型判才是对的抽象层次。

    判不了（模型不可用/答非所问）返回 None，调用方回落到原来的长度启发式 —— 大模型
    挂了不该让"引用回复"这条正式通道整个失灵。

    **默认偏向 SEND**：引用回复是主管特意选择的"正式作答"通道，指向性已经很明确；
    误拦的代价是每次都要重打一遍（实测很烦），误发的代价才是这道闸要防的，而那类
    "随口评论"在没引用的路径上还有长度启发式兜着。早期版本把「疑问」也列进 HOLD，
    结果主管反问提问者一句「X 是啥意思」就被拦下 —— 那明明是在跟提问者对话。
    """
    prompt = _JUDGE_PROMPT.format(
        question=(question or "（无）")[:800],
        draft=(draft or "（无）")[:800],
        reply=(reply or "")[:800])
    # conv_id 留空 = 无状态一次性会话，不污染任何人的多轮上下文，也不被别人的上下文影响
    out, status = generate_reply_ex("supervisor-judge", prompt, raw=True,
                                    ctx={"conv_id": "", "conv_type": _CONV_TYPE_O2O})
    if status != "ok" or not out:
        log(f"supervisor_review: 意图判断不可用(status={status})，回落长度启发式")
        return None
    up = out.strip().upper()
    if "SEND" in up and "HOLD" not in up:
        return True
    if "HOLD" in up and "SEND" not in up:
        return False
    log(f"supervisor_review: 意图判断结果无法解析 {out[:60]!r}，回落长度启发式")
    return None


def _judge_and_execute(seq, text):
    """后台：判主管这句引用回复能不能直接发，能就发（并学习），不能就问一句。

    放后台跑是因为这要等一次模型往返 —— 卡在 on_inbound 里会把整个入站事件循环堵住。
    """
    with _pending_lock:
        p = _pending.get(seq)
        question, draft = (p.get("question", ""), p.get("draft", "")) if p else ("", "")
    if not p:
        return
    verdict = _judge_directly_sendable(question, draft, text)
    if verdict is True:
        log(f"supervisor_review: #{seq} 引用回复判为可直接发出")
        _execute_verdict(seq, "rewrite", text, source="引用回复")
        return
    if verdict is False:
        log(f"supervisor_review: #{seq} 引用回复判为不宜直发 text={text[:40]!r}")
        _send_to_supervisor(
            f"🤔 「{text[:30]}」看着像是说给我听的，不像能直接发给{_asker_label(p)}的答复，"
            f"我没发。要原样发出请回「#{seq} 改：{text[:20]}…」，"
            f"要放行草稿贴 👍 或回「#{seq} 同意」。")
        return
    # 判不了 → 回落到原来的长度启发式（大模型挂了不该让正式通道失灵）
    _, action, payload = _parse_verdict(text)
    if action == "rewrite" and payload:
        _execute_verdict(seq, "rewrite", payload, source="引用回复")
    else:
        _send_to_supervisor(_unclear_hint(seq, text))


def _unclear_hint(seq, text):
    """认不出主管想干嘛时的澄清提示（待审保留，什么都不发给提问者）。"""
    return "\n".join([
        # seq 必须在最前：引用定位的正则锚在正文开头，若把主管原话放前面，他话里带的
        # 「待审 #3」会被当成本条的编号
        f"🤔 待审 #{seq} 仍在——没听懂「{text[:30]}」。",
        f"放行回「#{seq} 同意」或直接贴 👍；",
        f"不回复回「#{seq} 忽略」或贴 ❌；",
        f"要改写回「#{seq} 改：<你的答案>」。",
    ])


def _resolve_target(msg, typed_seq):
    """这条裁决说的是哪一次审核 → (seq, 错误提示)。seq=None 时看错误提示：
    有提示=已消费（回给主管），无提示=不是裁决（交回 text_reply）。

    优先级：**引用 > #N > 最近一条**。

    铁律：**只要主管引用了消息，就绝不回落到"最近一条"**。回落是这里唯一能把答案发给
    错的人的路径 —— 主管引用一条无关消息（启动报告、上周的回执）时，裁决会悄悄落到另
    一个提问者头上，而引用恰恰是主管指向性最明确的动作，猜错代价最大。
    """
    extra = msg.extra or {}
    quoted_id = extra.get("quoted_msg_id")
    if extra.get("quoted") or quoted_id:
        seq = extra.get("quoted_seq")             # 被引用正文里的「待审 #N」
        if seq is None:
            return None, ("🤔 你引用的这条我对不上号（不像是某次待审的消息）。"
                          "要裁决请引用那次审核的卡片或回执，或直接回「#N …」。")
        return _target_from_seq(seq, msg.text)

    if typed_seq is not None:
        if typed_seq in _pending:
            return typed_seq, None
        return _target_from_seq(typed_seq, msg.text)

    seq = _latest_seq()
    if seq is None:
        return None, None      # 主管没有待审，这是普通对话 → 交 text_reply
    return seq, None


def _target_from_seq(seq, text):
    """按编号定位一次审核（可能已经了结）→ (seq, 错误提示)。

    了结过的能不能补裁，取决于**提问者当时收到了什么**：
      - 忽略/超时 → 他什么都没收到，事后补答很自然，直接复活
      - 已经答过 → 再发一条就是数字员工自己推翻自己（群里更是两条互相矛盾的公开发言），
        默认拒绝；主管确实要改口就明说「更正：」
    """
    if seq in _pending:
        return seq, None
    rec = _history.get(seq)
    if not rec:
        hint = ""
        if seq > _seq_counter:
            hint = "（这个号还没发出去过）"
        elif _seq_counter:
            hint = "（可能太久远，已不在记录里）"
        return None, f"⚠️ 找不到待审 #{seq}{hint}。"
    state = rec.get("state")
    if state == "answered" and not _has_amend_prefix(text):
        return None, (f"⚠️ 待审 #{seq} 已经答复过提问者了，再发一条会自相矛盾，我没动。"
                      f"确实要改口请回「#{seq} 更正：<新答案>」。")
    if not _revive_seq(seq):
        return None, f"⚠️ 待审 #{seq} 的记录不全，没法补裁。"
    return seq, None


def _handle_verdict(msg):
    """主管的裁决消息。有待审→消费(True)；无待审→放行(False) 让主管正常对话。"""
    typed_seq, action, payload = _parse_verdict(msg.text)
    seq, err = _resolve_target(msg, typed_seq)
    if seq is None:
        if err:
            _send_to_supervisor(err)
            return True
        return False       # 不是裁决，交回 text_reply 让主管正常对话
    quoted = bool((msg.extra or {}).get("quoted") or (msg.extra or {}).get("quoted_msg_id"))

    # 引用回复 = 正式作答的通道：内容交给大模型判"能不能原样发给提问者"，能就直接发。
    # 显式裁决（同意/忽略/改：）不必劳烦模型，先走完；剩下的才判。
    if (quoted and _JUDGE_QUOTED and action not in ("approve", "ignore")
            and not _has_rewrite_prefix(msg.text)):
        _, rest = _strip_seq(msg.text)
        if rest:
            log(f"supervisor_review: #{seq} 引用回复，交大模型判是否可直发")
            submit_reply(_judge_and_execute, seq, rest)   # 模型往返放后台，别堵事件循环
            return True

    if action == "unclear":
        # 关键：**不 _pop**。认不出就把待审留着，绝不拿这句话当答案发出去（#107 D）
        log(f"supervisor_review: #{seq} 裁决认不出 text={msg.text[:40]!r}，保留待审并追问")
        _send_to_supervisor(_unclear_hint(seq, (msg.text or "").strip()))
        return True

    if _execute_verdict(seq, action, payload):
        return True
    # 待审已经没了（多半是主管刚贴过表情、轮询线程抢先裁完了）。**照样算消费掉**：
    # 返回 False 会让这条「同意」落到 text_reply，被当成普通提问喂给大脑，主管会收到
    # 一条驴唇不对马嘴的回复。裁决重复提交是常态，不是对话。
    log(f"supervisor_review: #{seq} 裁决时待审已不在（多半已被贴表情裁掉），忽略这条")
    return True


def on_inbound(msg):
    """消息入站（单聊 + 群聊）。返回 True=已消费（不再交 text_reply）。

    **裁决只认主管单聊**：群里主管发的一律当新提问送审（#107）。群消息本就是冲着
    数字员工来的，若也拿去解析裁决，主管在群里说一句「同意」就会放行另一个人的待审。
    """
    if msg.kind == KIND_REACTION:
        # **必须在最前面拦掉**：本函数最后一个分支是无条件送审，杂散表情事件（比如
        # operator 没匹配上主管名）会掉进去，给一个空问题生成草稿、再发一张假卡片。
        if _is_supervisor(msg.user):
            _schedule_reaction(msg)
        else:
            log(f"supervisor_review: 忽略非主管 {msg.user!r} 的表情回应")
        return True

    is_o2o = str(msg.conv_type) == _CONV_TYPE_O2O
    if _O2O_ONLY and not is_o2o:
        return False   # 显式配了只审单聊 → 群聊放行
    if not _supervisor_id() and not _supervisor_names():
        return False   # 没配主管 → 本能力等于关闭，放行

    if is_o2o and _is_supervisor(msg.user):
        consumed = _handle_verdict(msg)
        if consumed:
            # 裁决消息自己也挂着一个 ack worker（#106 起 ack 只给主管贴表情，主管的每条
            # 裁决都必然进这条路）。给主管的回执走 _send_to_supervisor 裸发、不经
            # send_reply，不显式收尾就永远停在「处理中」并周期播报。
            _close_ack(msg.conv_id, msg.conv_type, ok=True)
        return consumed

    # 其余（含主管的群消息）：拦下来，后台出草稿 + 转交主管（不回提问者）
    # 带上被引用消息的 id：群里"引用一条消息再 @ 我"时，用户那句话（「看一下」）离开被
    # 引用的内容就没有意义（#112）。**裁决路径不经这里**，所以主管引用卡片仍按「待审 #N」
    # 走裁决，不会被当成"提问的上下文"。
    submit_reply(_draft_and_forward, msg.user, msg.text, msg.conv_type,
                 msg.conv_id, msg.msg_id, (msg.extra or {}).get("quoted_msg_id"))
    return True


def _line_tail(line):
    """connect 行的**尾部字段段**（最后一个 `(convType=` 之后的部分）。

    必须切出来再抽字段，不能对整行 search：正文也在同一行里，主管（或提问者，他的正文
    同样会原样进 connect 行）打一句 `quotedSeq=9` 就能伪造出一个引用标记。
    取**最后一个** `(convType=` 是因为正文里也可能出现这串字面量。
    """
    i = (line or "").rfind("(convType=")
    return line[i:] if i >= 0 else ""


def classify_line(line):
    """认领带引用标记的 connect 行，把被引用消息的信息塞进 extra（#107 B / #108）。

    core 的 parse_line 只认 atMention=1 一个尾部标记，要多认几个键本来得改 core。
    classify_line 正是 core 为这种情况留的口子（见 core/capabilities.py）——这里**复用
    parse_line 解析正文**，只补几个字段，零 core 改动。返回 None 则交回 core 标准解析。
    """
    m = _REACTION_RE.match(line or "")
    if m:
        # msg_id 填 **event_id**：core 的声明式 dedup 按 msg_id 记，正好等于按事件去重
        # （dws 是 at-least-once，重连会重投）。若填被贴表情那条消息的 id，同一条消息被
        # 反复贴/撤就会被 core 当成重复而吞掉。event_id 缺失时用内容合成一个。
        eid = m.group("eid") or f"{m.group('mid')}:{m.group('name')}:{m.group('rop')}"
        return InboundMessage(
            user=m.group("op"), text="", conv_type=m.group("ct"),
            conv_id=m.group("conv"), msg_id=eid, kind=KIND_REACTION,
            raw_line=line,
            extra={"reacted_msg_id": m.group("mid"), "reaction": m.group("name"),
                   "reaction_op": m.group("rop")})

    tail = _line_tail(line)
    if "quotedMsgId=" not in tail:
        return None
    msg = parse_line(line)
    if msg is None:
        return None
    m = _QUOTED_RE.search(tail)
    if m:
        msg.extra["quoted_msg_id"] = m.group(1)
    m = _QUOTED_SEQ_RE.search(tail)
    if m:
        # 引用的是审核消息但认不出号时 bridge 给的是 `?` —— 记成 None，与"没引用"区分开：
        # 前者该追问，后者才允许回落到 #N/最近一条。
        msg.extra["quoted"] = True
        msg.extra["quoted_seq"] = int(m.group(1)) if m.group(1).isdigit() else None
    return msg


# 测试用：清空待审 + 重置短号 + 停轮询
def _reset():
    global _seq_counter
    for t in list(_debounce_timers.values()):
        try:
            t.cancel()
        except Exception:
            pass
    _debounce_timers.clear()
    _handled_events.clear()
    with _pending_lock:
        for p in _pending.values():
            if p.get("timer"):
                try:
                    p["timer"].cancel()
                except Exception:
                    pass
        _pending.clear()
        _seq_counter = 0
    # 只清内存，**不碰流水文件**。replay 单独可调（_ensure_replayed）—— 单测的 setUp
    # 是先 _reset() 后 patch 路径的，若在这里读盘就会去读真实的 knowledge/ 文件。
    _history.clear()
    global _replayed
    _replayed = False


CAPABILITY = Capability(
    name="supervisor_review",
    on_inbound=on_inbound,
    classify_line=classify_line,   # 认领带引用信息的行（#107 B）
    handles_kinds={KIND_TEXT, KIND_REACTION},
    priority=30,             # 晚于 question20/permission15，早于 image40/forward50/text100
    default_enabled=False,   # 显式开（改变默认回复行为，不该悄悄生效）
    loop_guard=True,         # 数字员工自己发的不处理
    dedup=True,              # msgId 去重
)
register(CAPABILITY)
