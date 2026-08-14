"""supervisor_review — 主管审核回路（custom 能力）

非主管的单聊消息不直接回复提问者，而是 **AI 先出草稿 → 转交主管审核 → 主管裁决后才发出**。
主管改写过的答案沉淀进知识库，下次同类问题 AI 能自己答对（见 brain._effective_system_prompt）。

流程：
    张三私聊 → AI 生成草稿（不发张三）→ 转交主管单聊（带短号 #N）
             → 主管回「#N 同意」= 放行草稿 / 「#N <答案>」= 改写 / 「#N 忽略」= 不回
             → 最终答案发给张三（张三全程只看到一条）

设计要点：
- **priority=30**：晚于 question(20)/permission(15) 不抢它们的答复，早于 image(40)/
  forward(50)/text_reply(100) —— 必须在 text_reply 把草稿发给提问者之前截住。
- **主管自己的单聊照常放行**：主管消息进来时若无待审 → return False 交 text_reply 正常对话，
  否则主管没法和数字员工聊天，且会与自己的裁决互相触发。
- **身份判定靠显示名**：bridge 只传 sender 显示名不传 userId（见 dws_event_bridge
  ._to_connect_line），故用 AGENT_SUPERVISOR_NAME + AGENT_SUPERVISOR_ALIASES 比对
  msg.user。发给主管则用 userId 走 `dws chat message send --user`。
- **短号 #N 对应**：主管一个会话里可能同时挂多人的问题。回复以 #N 开头精确对应；
  不带编号时对应**最近一条**待审（单人场景零心智负担）。
- **给主管发卡片不用 send_reply**：卡片不是"对提问者的回复"，不该驱动 ack 状态机收尾。
  发给提问者的最终答案才用 send_reply（让 ack 回执正常落地）。
- **超时兜底**：默认 600s 未裁决 → 把 AI 草稿发给提问者，避免提问者被无限期挂着。

开关：CAP_SUPERVISOR_REVIEW_ENABLED（模板默认关，config/constants.local.sh 里开）。
"""

import json
import os
import threading
import time

from core.agent_common import PROFILE, env_flag, log, submit_reply, _run_cli
from core.brain import generate_reply_ex
from core.capabilities import Capability, register
from core.inbound import KIND_TEXT
from core.replier import send_reply
from custom.identity import is_supervisor, supervisor_id, supervisor_names

# 超时未裁决 → 放行 AI 草稿（秒）。0=永不超时（提问者可能被无限期挂着，慎用）。
_TIMEOUT = int(os.environ.get("SUPERVISOR_REVIEW_TIMEOUT", "600"))
# 仅拦单聊（群聊消息不拦，避免群里刷屏）
_O2O_ONLY = env_flag("SUPERVISOR_REVIEW_O2O_ONLY", default=True)
# 知识库路径（相对 PROJECT_DIR）
_KNOWLEDGE_FILE = os.environ.get("AGENT_KNOWLEDGE_FILE", "knowledge/supervisor_qa.jsonl")

# 裁决关键词
_APPROVE_KEYWORDS = {"同意", "ok", "可以", "放行", "approve", "y", "yes", "行"}
_IGNORE_KEYWORDS = {"忽略", "不回", "跳过", "ignore", "skip", "算了"}

# 待审表：seq -> {asker, asker_conv_id, asker_conv_type, question, draft, ts, timer}
_pending = {}
_pending_lock = threading.Lock()
_seq_counter = 0


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
    rc, out = _run_cli(["chat", "message", "send", "--user", sid, "--text", text])
    if rc != 0:
        log(f"supervisor_review: 转交主管失败 rc={rc} out={out[:200]}")
        return False
    return True


# ---------------------------------------------------------------------------
# 渲染 / 解析（纯函数，可单测）
# ---------------------------------------------------------------------------

def _render_card(seq, asker, question, draft):
    """渲染给主管的待审卡片。"""
    return "\n".join([
        f"📋 **待审 #{seq}**　来自：**{asker}**",
        "",
        "**【问题】**",
        question,
        "",
        "**【AI 草稿】**",
        draft or "（AI 未能生成草稿）",
        "",
        "---",
        f"回「#{seq} 同意」放行草稿",
        f"回「#{seq} <你的答案>」改写后发出（会被学习）",
        f"回「#{seq} 忽略」不回复",
    ])


def _parse_verdict(text):
    """解析主管回复 → (seq|None, action, payload)。

    action ∈ approve | ignore | rewrite。seq=None 表示未指名（对应最近一条待审）。
    形如 "#3 同意" / "#3 你应该这样答…" / "同意" / "直接写答案"。
    """
    t = (text or "").strip()
    seq = None
    if t.startswith("#"):
        rest = t[1:].lstrip()
        num = ""
        for ch in rest:
            if ch.isdigit():
                num += ch
            else:
                break
        if num:
            seq = int(num)
            t = rest[len(num):].strip()
    low = t.lower()
    if low in _APPROVE_KEYWORDS:
        return seq, "approve", ""
    if low in _IGNORE_KEYWORDS:
        return seq, "ignore", ""
    return seq, "rewrite", t


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


def _latest_seq():
    """最近登记的一条待审 seq（主管不带编号回复时用）。无待审返回 None。"""
    with _pending_lock:
        if not _pending:
            return None
        return max(_pending, key=lambda s: _pending[s]["ts"])


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
# 拦截：生成草稿 → 转交主管
# ---------------------------------------------------------------------------

def _timeout(seq):
    """超时未裁决：把 AI 草稿发给提问者兜底，并告知主管。"""
    p = _pop(seq)
    if not p:
        return
    draft = p.get("draft") or ""
    log(f"supervisor_review: #{seq} 主管 {_TIMEOUT}s 未裁决，放行 AI 草稿")
    if draft:
        send_reply(p["asker_conv_id"], p["asker_conv_type"], draft)
    _send_to_supervisor(f"⏰ 待审 #{seq}（来自 {p['asker']}）超时未裁决，已自动放行 AI 草稿。")


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
    card = _render_card(seq, user, text, draft)
    if not _send_to_supervisor(card):
        # 转交失败：不能把提问者永久挂着 —— 直接把草稿发给他（有什么发什么）
        log(f"supervisor_review: #{seq} 转交主管失败，回退直接回复提问者 user={user}")
        if draft:
            send_reply(conv_id, conv_type, draft)
        return

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
        }
    if timer:
        timer.start()
    log(f"supervisor_review: #{seq} 已转交主管审核 asker={user} q={text[:40]!r}")


def _handle_verdict(msg):
    """主管的裁决消息。有待审→消费(True)；无待审→放行(False) 让主管正常对话。"""
    seq, action, payload = _parse_verdict(msg.text)
    if seq is None:
        seq = _latest_seq()
        if seq is None:
            return False   # 主管没有待审，这是普通对话 → 交 text_reply
    elif seq not in _pending:
        _send_to_supervisor(f"⚠️ 待审 #{seq} 不存在或已处理。")
        return True

    p = _pop(seq)
    if not p:
        return False

    asker_conv_id = p["asker_conv_id"]
    asker_conv_type = p["asker_conv_type"]

    if action == "ignore":
        log(f"supervisor_review: #{seq} 主管选择忽略，不回复 asker={p['asker']}")
        _send_to_supervisor(f"🚫 待审 #{seq} 已忽略，未回复 {p['asker']}。")
        return True

    if action == "approve":
        draft = p.get("draft") or ""
        if not draft:
            _send_to_supervisor(f"⚠️ 待审 #{seq} 没有可放行的草稿，请直接写答案。")
            return True
        send_reply(asker_conv_id, asker_conv_type, draft)
        log(f"supervisor_review: #{seq} 主管同意，草稿已发给 {p['asker']}")
        _send_to_supervisor(f"✅ 待审 #{seq} 已按草稿回复 {p['asker']}。")
        return True

    # rewrite：用主管的答案回复提问者，并学习
    answer = payload
    if not answer:
        _send_to_supervisor(f"⚠️ 待审 #{seq} 未识别到答案内容，请重发。")
        return True
    send_reply(asker_conv_id, asker_conv_type, answer)
    _record_knowledge(p["question"], answer, asker=p["asker"])
    log(f"supervisor_review: #{seq} 主管改写并已回复 {p['asker']}")
    _send_to_supervisor(f"📝 待审 #{seq} 已用你的答案回复 {p['asker']}，并已存入知识库。")
    return True


def on_inbound(msg):
    """单聊消息入站。返回 True=已消费（不再交 text_reply）。"""
    if _O2O_ONLY and str(msg.conv_type) != "1":
        return False   # 群聊不拦
    if not _supervisor_id() and not _supervisor_names():
        return False   # 没配主管 → 本能力等于关闭，放行

    if _is_supervisor(msg.user):
        return _handle_verdict(msg)

    # 非主管：拦下来，后台出草稿 + 转交主管（不回提问者）
    submit_reply(_draft_and_forward, msg.user, msg.text, msg.conv_type,
                 msg.conv_id, msg.msg_id)
    return True


# 测试用：清空待审 + 重置短号
def _reset():
    global _seq_counter
    with _pending_lock:
        for p in _pending.values():
            if p.get("timer"):
                try:
                    p["timer"].cancel()
                except Exception:
                    pass
        _pending.clear()
        _seq_counter = 0


CAPABILITY = Capability(
    name="supervisor_review",
    on_inbound=on_inbound,
    handles_kinds={KIND_TEXT},
    priority=30,             # 晚于 question20/permission15，早于 image40/forward50/text100
    default_enabled=False,   # 显式开（改变默认回复行为，不该悄悄生效）
    loop_guard=True,         # 数字员工自己发的不处理
    dedup=True,              # msgId 去重
)
register(CAPABILITY)
