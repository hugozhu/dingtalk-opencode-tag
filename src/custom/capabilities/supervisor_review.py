"""supervisor_review — 主管审核回路（custom 能力）

数字员工要开口说的话，先 **AI 出草稿 → 转交主管审核 → 主管裁决后才发出**。
主管改写过的答案沉淀进知识库，下次同类问题 AI 能自己答对（见 brain._effective_system_prompt）。

流程：
    张三私聊/群里问 → AI 生成草稿（不发张三）→ 转交主管单聊（带短号 #N）
             → 主管裁决：给卡片**贴 👍/❌**（零打字）或回「#N 同意 / 忽略 / 改：<答案>」
             → 最终答案发回原会话（张三全程只看到一条）

三条裁决入口，最终都汇到 `_execute_verdict`（#107）：
  1. **贴表情**（`_poll_reactions_once`）—— 最高频的「同意/忽略」降到一次点击
  2. **引用卡片回复 = 正式作答**（`classify_line` → `extra['quoted_msg_id']`）—— 不用敲
     #N，内容交大模型判"能不能原样发给提问者"，能就直接发（见 _judge_directly_sendable）；
     超时归档的卡片照样能补裁
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
  **引用的卡片 > #N > 最近一条**（单人场景零心智负担）。
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
  超时的待审进 _archive，主管**事后引用那张卡片**随时能补裁（见 _revive），所以问题
  不会被丢掉，只是没人替他答应。

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
from core.inbound import KIND_TEXT, parse_line
from core.replier import send_reply
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

# 贴表情裁决（#107 A）：轮询待审卡片上的表情，秒=间隔，0=关闭（退回纯文字裁决）
_REACTION_POLL = float(os.environ.get("SUPERVISOR_REACTION_POLL", "5") or 0)
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
# 数字员工自己的显示名（反查自己发的卡片用）。默认值与 ack/forward 对齐 —— 留空会让
# _locate_card_msg_id 里的发送人校验静默失效（那正是防止匹配到主管引用回显的那道闸）。
_SELF_NAMES = {n.strip() for n in os.environ.get(
    "AGENT_SELF_NAMES", "数字员工,Claude Code").split(",") if n.strip()}

# 裁决关键词。approve 收得比 ignore 宽：主管想放行时说法五花八门（「可以发」「就这样」），
# 认不出就会掉进 rewrite 把这几个字当答案公开发出去 —— 这是 #107 要修的事故。
_APPROVE_KEYWORDS = {"同意", "ok", "可以", "放行", "approve", "y", "yes", "行",
                     "可以发", "就这样", "发吧", "没问题", "可以的", "同意发", "通过"}
_IGNORE_KEYWORDS = {"忽略", "不回", "跳过", "ignore", "skip", "算了", "不用回", "别回"}
# 无歧义改写前缀：主管显式声明"下面是答案"，跳过一切猜测
_REWRITE_PREFIXES = ("改：", "改:", "答：", "答:")

# 待审表：seq -> {asker, asker_conv_id, asker_conv_type, question, draft, ts, timer,
#                card_msg_id}
_pending = {}
_pending_lock = threading.Lock()
_seq_counter = 0

# 表情轮询线程（单例，懒启动；没有待审时自行退出，下次转交再拉起）
_poller_thread = None
_poller_lock = threading.Lock()
_poller_stop = threading.Event()

# 已处理过的表情：(card_msg_id, emoji) -> True，有界 FIFO。
# **表情是"状态"不是"事件"**：主管贴上去就一直挂在那，每轮轮询都会重新读到。没有这张表
# 的话，任何"没能真正裁完"的分支（如同意但没草稿 → 待审放回去）都会被下一轮重新触发，
# 变成每 5 秒给主管发一条同样的提示、且超时定时器被反复重置，提问者永远等不到兜底。
_handled_reactions = OrderedDict()
_HANDLED_MAX = 512

# 发过的卡片：card_msg_id -> seq，有界 FIFO。待审注销后仍保留一小段时间，用来分辨
# "主管引用了一张已处理的旧卡片"（该告诉他已处理）与"引用了别的消息"（该回落到最近一条）。
_card_history = OrderedDict()
_CARD_HISTORY_MAX = 256

# 超时未裁决而被归档的待审：card_msg_id -> (seq, record)，有界 FIFO。
# 超时按"不回复"处理，但**问题本身没消失** —— 主管事后引用那张卡片仍能补裁，
# 届时由 _revive 把它重新挂回待审表。
_archive = OrderedDict()
_ARCHIVE_MAX = 64

# 引用回复的原消息 id（bridge 在行尾追加，见 classify_line）
_QUOTED_RE = re.compile(r"\bquotedMsgId=([^\s)]+)")


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
    """卡片首行的唯一前缀 —— 发完之后靠它把自己那条捞回来（见 _locate_card_msg_id）。

    `**` 收尾保证 #1 不会前缀命中 #11。
    """
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
    if _REACTION_POLL > 0:
        line += "　也可直接给本条贴 👍 / ❌"
    return line


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
    _ensure_poller()   # 轮询线程可能已因空闲退出，放回待审就得把它拉起来


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


def _seq_by_card_msg_id(msg_id):
    """按卡片 msgId 反查待审 seq（引用回复裁决用，#107 B）。无匹配返回 None。"""
    if not msg_id:
        return None
    with _pending_lock:
        for seq, p in _pending.items():
            if p.get("card_msg_id") == msg_id:
                return seq
    return None


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

def _locate_card_msg_id(seq):
    """反查刚发出的待审卡片自己的 msgId（贴表情裁决靠它轮询）。取不到返回 ""。

    `dws chat message send` 返回的是 openTaskId 不是 msgId（见 query-send-status --help），
    所以只能发完再从主管单聊里把这条捞回来，按卡片首行前缀匹配**自己发的**那条。
    校验发送人很重要：主管引用卡片回复时，他那条消息的正文里也会带卡片原文。
    """
    sid = _supervisor_id()
    if not sid:
        return ""
    # **不能用本地时间当锚点**：守护进程由 reboot.sh 以 `env -i` 拉起，TZ 不被继承 → 进程
    # 跑在 UTC，而钉钉的时间戳是 CST，差 8 小时。而 `--time` 是"从这个点往后取最旧的 N 条"，
    # 锚点偏早 8 小时就会取到一堆历史消息、刚发的卡片反而不在结果里；更糟的是重启后
    # `_seq_counter` 归零，历史里那张同号的旧卡片会被匹配上，于是轮询一直盯着**上一轮的
    # 卡片**，主管贴在新卡片上的表情永远读不到，还全程不报错。
    # 改成 `--direction older` + 一个必然在未来的锚点：结果是"从最新往回数 N 条"，
    # 与进程时区差多少都无关，刚发出的卡片必在最前面。
    anchor = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() + 86400))
    rc, out = _run_cli(["chat", "message", "list", "--user", sid,
                        "--time", anchor, "--direction", "older", "--limit", "30"])
    if rc != 0:
        log(f"supervisor_review: #{seq} 反查卡片 msgId 失败 rc={rc} out={out[:120]}")
        return ""
    try:
        msgs = (json.loads(out).get("result") or {}).get("messages") or []
    except (ValueError, AttributeError, TypeError) as e:
        log(f"supervisor_review: #{seq} 卡片列表解析失败 {e}")
        return ""
    marker = _card_marker(seq)
    for m in msgs:
        if m.get("sender") not in _SELF_NAMES:
            continue
        if (m.get("content") or "").lstrip().startswith(marker):
            return m.get("openMessageId", "") or ""
    # 找不到就只能走文字裁决 —— 这条日志是唯一线索，别静默（常见原因：AGENT_SELF_NAMES
    # 与钉钉上的真实显示名对不上，或者短时间发了太多消息把卡片挤出窗口）
    log(f"supervisor_review: #{seq} 未在主管会话里找回卡片（自称 {sorted(_SELF_NAMES)}），"
        f"贴表情/引用裁决对这条不可用")
    return ""


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


def _poll_reactions_once():
    """拉一轮卡片表情 → 命中主管贴的就裁决。返回本轮执行的裁决条数。

    一次 CLI 拉全部待审（--msg-ids 支持逗号分隔），没有待审就完全不发请求。
    **每个 (卡片, 表情) 只作用一次**：表情贴上去会一直挂在消息上，每轮都读得到，
    不记账的话任何没裁干净的分支都会被无限重放（见 _handled_reactions）。
    """
    with _pending_lock:
        cards = {p["card_msg_id"]: seq for seq, p in _pending.items() if p.get("card_msg_id")}
    if not cards:
        return 0
    rc, out = _run_cli(["chat", "message", "list-emotion-replies",
                        "--msg-ids", ",".join(cards)])
    if rc != 0:
        log(f"supervisor_review: 拉表情失败 rc={rc} out={out[:120]}")
        return 0
    try:
        msgs = (json.loads(out).get("result") or {}).get("messages") or []
    except (ValueError, AttributeError, TypeError) as e:
        log(f"supervisor_review: 表情返回解析失败 {e}")
        return 0
    done = 0
    for m in msgs:
        card_id = m.get("openMessageId", "")
        seq = cards.get(card_id)
        if seq is None:
            continue
        emojis = _supervisor_emojis(m.get("emotionReplyList"))
        # 先找认得出的那个：主管可能误贴过别的表情，不该被它挡住
        actionable = [(e, _emoji_action(e)) for e in emojis]
        hit = next(((e, a) for e, a in actionable if a), None)
        if hit is None:
            for e, _ in actionable:
                if _remember(_handled_reactions, (card_id, e), _HANDLED_MAX):
                    _report_unknown_emoji(seq, e)
            continue
        emoji, action = hit
        if not _remember(_handled_reactions, (card_id, emoji), _HANDLED_MAX):
            continue   # 这个表情上一轮已经处理过了
        if _execute_verdict(seq, action, source=f"贴「{emoji}」"):
            done += 1
    return done


def _reaction_poller():
    """表情轮询循环。先拉一次再等（do-while），保证至少跑一轮。

    没有待审就退出线程 —— 守护进程 7x24 跑着，留个空转线程每 5s 抢一次锁毫无意义。
    下次转交待审时 _ensure_poller 会重新拉起。
    """
    while not _poller_stop.is_set():
        try:
            _poll_reactions_once()
        except Exception as e:   # 轮询挂了不能把线程带走，否则贴表情从此静默失效
            log(f"supervisor_review: 表情轮询异常 {e}")
        with _pending_lock:
            idle = not _pending
        if idle:
            break
        _poller_stop.wait(_REACTION_POLL)
    with _poller_lock:
        global _poller_thread
        if _poller_thread is threading.current_thread():
            _poller_thread = None


def _ensure_poller():
    """懒启动表情轮询线程（单例）。空闲退出后由下一条待审重新拉起。"""
    global _poller_thread
    if _REACTION_POLL <= 0:
        return
    with _poller_lock:
        if _poller_thread is not None and _poller_thread.is_alive():
            return
        _poller_stop.clear()
        _poller_thread = threading.Thread(target=_reaction_poller, daemon=True,
                                          name="supervisor-reaction-poll")
        _poller_thread.start()
        log(f"supervisor_review: 表情轮询已启动（每 {_REACTION_POLL}s）")


# ---------------------------------------------------------------------------
# 拦截：生成草稿 → 转交主管
# ---------------------------------------------------------------------------

def _timeout(seq):
    """超时未裁决 → **按不回复处理**，并归档等主管事后补裁。

    为什么默认是"不回复"而不是"放行草稿"：没人管**不等于**默认同意。自动放行意味着
    一条**从没被人看过**的 AI 草稿被发出去，群聊场景更是直接公开发言——审核闸门存在
    的全部意义就是不让这种事发生，超时兜底不该在闸门上开个洞。沉默的代价（提问者没
    收到答案）是可见、可补救的；发错话的代价不是。

    补救路径：归档进 _archive，主管什么时候想起来，引用那张卡片回一句就能补答（见
    _revive）。所以这不是"问题被丢掉"，只是"没人替你答应"。
    """
    p = _pop(seq)
    if not p:
        return
    log(f"supervisor_review: #{seq} 主管 {_TIMEOUT}s 未裁决 → 按不回复处理（已归档，可事后补裁）")
    # 与"忽略"完全一致：提问者那条静默收尾，不贴「完成」也不贴「未完成」
    _close_ack(p["asker_conv_id"], p["asker_conv_type"], ok=None)
    card_id = p.get("card_msg_id")
    if card_id:
        _archive[card_id] = (seq, p)
        _archive.move_to_end(card_id)
        while len(_archive) > _ARCHIVE_MAX:
            _archive.popitem(last=False)
    tail = ("　想补答就**引用那张卡片**回一句（同意 / 改：<答案>）。" if card_id
            else "")
    _send_to_supervisor(
        f"⏰ 待审 #{seq}（来自 {_asker_label(p)}）{_TIMEOUT}s 未裁决，"
        f"已按**不回复**处理。{tail}")


def _revive(card_msg_id):
    """主管事后引用了一张归档卡片 → 重新挂回待审表。返回 seq；不是归档卡片返回 None。

    重新计时（刷新 ts）：主管既然回来处理了，就该给一个完整的裁决窗口，而不是挂上去
    一秒后又超时。裁不完（比如同意但没草稿）就照常留在待审里，再超时会再归档一次。
    """
    entry = _archive.pop(card_msg_id, None)
    if entry is None:
        return None
    seq, p = entry
    p["ts"] = time.time()
    _repend(seq, p)
    log(f"supervisor_review: #{seq} 主管事后引用卡片，已重新挂回待审")
    return seq


def _draft_and_forward(user, text, conv_type, conv_id, msg_id):
    """后台线程：生成草稿 → 转交主管 → 登记待审。"""
    global _seq_counter
    draft, status = generate_reply_ex(user, text, ctx={
        "conv_id": conv_id, "conv_type": conv_type, "msg_id": msg_id, "user": user,
    })
    # 草稿生成失败也要转交 —— 主管仍可手写答案，不能因模型挂了就把问题丢了
    if not draft:
        log(f"supervisor_review: AI 草稿生成失败(status={status})，仍转交主管 user={user}")

    with _pending_lock:
        _seq_counter += 1
        seq = _seq_counter
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
            "question": text,
            "draft": draft or "",
            "ts": time.time(),
            "timer": timer,
            "card_msg_id": "",     # 稍后回填（见下）
        }
    if timer:
        timer.start()
    _ensure_poller()
    log(f"supervisor_review: #{seq} 已转交主管审核 asker={user} q={text[:40]!r}")

    # 卡片自己的 msgId：贴表情裁决(A)与引用回复裁决(B)都靠它定位。取不到只是这两条路走
    # 不通，「#N 同意」照常。**在锁外做**：这是一次网络往返，攥着 _pending_lock 会把轮询
    # 线程一起堵住。**排在补发全文之前**：反查是按时间窗口 + 前缀找自己那条，先发别的
    # 消息等于把要找的卡片往后挤。回填时待审可能已经被裁掉了（主管手快），所以要判在不在。
    card_msg_id = _locate_card_msg_id(seq)
    if card_msg_id:
        _remember(_card_history, card_msg_id, _CARD_HISTORY_MAX)
        _card_history[card_msg_id] = seq
        with _pending_lock:
            if seq in _pending:
                _pending[seq]["card_msg_id"] = card_msg_id

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
    _send_to_supervisor(f"📝 待审 #{seq} 已用你的答案回复 {_asker_label(p)}，并已存入知识库。")
    return True


_JUDGE_PROMPT = """你在帮一个数字员工做一次判断，只判断、不作答。

它的主管刚刚**引用**了一条待审问题，并回了一句话。请判断：主管这句话是不是可以
**原样转发给提问者**的正式答复？

- SEND：这句话本身就是对问题的答复，提问者读了能直接理解、能用。
- HOLD：这是说给数字员工/AI 听的评语、指令、疑问或闲聊（例如「这个不太对」「再想想」
  「太啰嗦了」「稍等我问问」），原样发给提问者会让对方莫名其妙。

【提问者的问题】
{question}

【AI 原本的草稿】
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
        f"🤔 没听懂「{text[:30]}」，待审 #{seq} 仍在。",
        f"放行回「#{seq} 同意」" + ("或直接贴 👍；" if _REACTION_POLL > 0 else "；"),
        f"不回复回「#{seq} 忽略」" + ("或贴 ❌；" if _REACTION_POLL > 0 else "；"),
        f"要改写回「#{seq} 改：<你的答案>」。",
    ])


def _handle_verdict(msg):
    """主管的裁决消息。有待审→消费(True)；无待审→放行(False) 让主管正常对话。"""
    seq, action, payload = _parse_verdict(msg.text)
    # 引用了哪张卡片就裁哪条 —— 比 #N 和"最近一条"都准，且主管不用敲编号（#107 B）
    quoted_id = (msg.extra or {}).get("quoted_msg_id")
    quoted = _seq_by_card_msg_id(quoted_id)
    if quoted is None and quoted_id:
        # 超时归档的卡片可以事后补裁 —— 超时只是"当时没人回复"，问题本身还在
        quoted = _revive(quoted_id)
    if quoted is not None:
        seq = quoted
    elif quoted_id and quoted_id in _card_history:
        # 引用的是一张**已裁决过**的旧卡片 —— 绝不能悄悄改判到"最近一条"，那会把裁决
        # 落到另一个提问者头上（主管在这里的指向性是最明确的，猜错代价最大）。
        old = _card_history[quoted_id]
        _send_to_supervisor(f"⚠️ 你引用的待审 #{old} 已经裁决过了，不能重复处理。"
                            f"要裁别的请引用它的卡片，或直接回「#N …」。")
        return True
    if seq is None:
        seq = _latest_seq()
        if seq is None:
            return False   # 主管没有待审，这是普通对话 → 交 text_reply
    elif seq not in _pending:
        _send_to_supervisor(f"⚠️ 待审 #{seq} 不存在或已处理。")
        return True

    # 引用回复 = 正式作答的通道：内容交给大模型判"能不能原样发给提问者"，能就直接发。
    # 显式裁决（同意/忽略/改：）不必劳烦模型，先走完；剩下的才判。
    if (quoted is not None and _JUDGE_QUOTED and action not in ("approve", "ignore")
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
    submit_reply(_draft_and_forward, msg.user, msg.text, msg.conv_type,
                 msg.conv_id, msg.msg_id)
    return True


def classify_line(line):
    """认领带 quotedMsgId= 的 connect 行，把被引用的消息 id 塞进 extra（#107 B）。

    core 的 parse_line 只认 atMention=1 一个尾部标记，要多认一个键本来得改 core。
    classify_line 正是 core 为这种情况留的口子（见 core/capabilities.py）——这里**复用
    parse_line 解析正文**，只补一个字段，零 core 改动。返回 None 则交回 core 标准解析。
    """
    if "quotedMsgId=" not in (line or ""):
        return None
    msg = parse_line(line)
    if msg is None:
        return None
    m = _QUOTED_RE.search(line)
    if m:
        msg.extra["quoted_msg_id"] = m.group(1)
    return msg


# 测试用：清空待审 + 重置短号 + 停轮询
def _reset():
    global _seq_counter, _poller_thread
    with _poller_lock:
        old = _poller_thread
        _poller_thread = None
    _poller_stop.set()
    # 等旧线程真正退出再放行：不 join 的话它还睡在 wait() 里，下一次 _ensure_poller
    # 的 _poller_stop.clear() 会把它一起唤醒，于是有两个轮询线程抢同一张待审表。
    if old is not None and old.is_alive() and old is not threading.current_thread():
        old.join(timeout=2.0)
    with _pending_lock:
        for p in _pending.values():
            if p.get("timer"):
                try:
                    p["timer"].cancel()
                except Exception:
                    pass
        _pending.clear()
        _seq_counter = 0
    _handled_reactions.clear()
    _card_history.clear()
    _archive.clear()


CAPABILITY = Capability(
    name="supervisor_review",
    on_inbound=on_inbound,
    classify_line=classify_line,   # 认领带引用信息的行（#107 B）
    handles_kinds={KIND_TEXT},
    priority=30,             # 晚于 question20/permission15，早于 image40/forward50/text100
    default_enabled=False,   # 显式开（改变默认回复行为，不该悄悄生效）
    loop_guard=True,         # 数字员工自己发的不处理
    dedup=True,              # msgId 去重
)
register(CAPABILITY)
