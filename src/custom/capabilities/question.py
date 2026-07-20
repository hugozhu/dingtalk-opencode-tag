"""question — Question 交互能力（钉钉端回答 agent 提问）(#27)

agent 调 `question` 工具时，opencode serve 发 `question.asked` SSE 事件。本能力：
  1. on_sse_event：收 question.asked → 渲染纯文本编号问题 → 发到该 session 对应的**来源群**
     （事件只有 sessionID，用 brain.session_conv 反查 conv_id）。同时把 pending question
     记进内存（含 answers 状态、超时定时器）。
  2. on_inbound（优先级高于 text_reply）：存在 pending question 时，用户在群里的回复被本能力
     拦截 → _match_option 三级匹配（序号 / 严格 label / 包含）→ 单选答完 / 多选回「提交」→
     `POST /question/{id}/reply`；回「取消」→ reject。未匹配的文本放行给 text_reply。
  3. 超时：Q_TIMEOUT（默认 60s）未答 → `POST /question/{id}/reject` 解卡（agent 的阻塞 POST
     随之返回）。

同步 brain 模型下这条链路成立：agent 的 message POST 阻塞在 worker 线程等答案，本能力在
另一路径（log-tail 入站）POST reply/reject，阻塞 POST 随即解开返回回复（实测验证，见 #27）。
event-consume 不自动转发答案文本，故**不需要** spurious 轮次 cleanup。

开关：CAP_QUESTION_ENABLED（默认开）。on_inbound 优先级 20（先于 image 40 / forward 50 /
text_reply 100，必须最先看到用户回复以判断是否在答问题）。
"""

import base64
import json
import os
import threading
import time
import urllib.error
import urllib.request

from core.agent_common import find_serve_credentials, log
from core.capabilities import Capability, register
from core.inbound import KIND_TEXT
from core.brain import session_conv
from core.replier import send_reply

# 超时未答自动 reject 的秒数（serve 端 question 无 TTL，这是安全网防会话卡死）
_Q_TIMEOUT = int(os.environ.get("CAP_QUESTION_TIMEOUT", os.environ.get("Q_TIMEOUT", "60")))
# 多选提交 / 取消 关键词
_SUBMIT_KEYWORDS = {"提交", "submit", "确定", "确认", "ok", "done", "完成"}
_CANCEL_KEYWORDS = {"取消", "cancel", "算了", "不选了"}

# pending questions：req_id -> {sid, conv_id, conv_type, questions, answers{qidx: str|list}, timer}
_pending = {}
_pending_lock = threading.Lock()


# ---------------------------------------------------------------------------
# serve HTTP：POST /question/{id}/reply | reject
# ---------------------------------------------------------------------------

def _post_question(req_id, action, answers=None):
    """POST /question/{req_id}/{reply|reject}。返回 (ok, msg)。

    reply body = {"answers": [["label"], ...]}；reject body = {}。
    reject 返回 404 视为已过期（serve 重启/finalizer 清了）——当作成功。
    """
    pid, port, pwd = find_serve_credentials()
    if not port:
        return False, "serve 凭据缺失"
    body = {"answers": answers} if action == "reply" else {}
    data = json.dumps(body).encode()
    headers = {"Content-Type": "application/json"}
    if pwd:
        headers["Authorization"] = "Basic " + base64.b64encode(
            f"opencode:{pwd}".encode()).decode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/question/{req_id}/{action}",
        data=data, headers=headers, method="POST")
    try:
        r = urllib.request.urlopen(req, timeout=10)
        return r.status == 200, "ok"
    except urllib.error.HTTPError as e:
        if action == "reject" and e.code == 404:
            return True, "已过期(404)"
        return False, f"HTTP {e.code}"
    except Exception as e:
        return False, str(e)


# ---------------------------------------------------------------------------
# 渲染 + 匹配（纯函数，可单测）
# ---------------------------------------------------------------------------

def _render_question(questions):
    """把 question.asked 的 questions 渲染成纯文本编号列表（无链接）。"""
    lines = ["❓ 需要你的输入", ""]
    submit_needed = False
    for i, q in enumerate(questions):
        qtext = q.get("question", "")
        header = q.get("header", "")
        multiple = bool(q.get("multiple", False))
        title = f"Q{i+1}: {qtext}"
        if header:
            title += f"（{header}）"
        if multiple:
            title += " [多选]"
            submit_needed = True
        lines.append(title)
        for j, opt in enumerate(q.get("options", []) or []):
            label = opt.get("label", "")
            desc = opt.get("description", "")
            lines.append(f"  {j+1}. {label}" + (f" — {desc}" if desc else ""))
        lines.append("")
    lines.append("💬 回复序号(如 1)最稳，也可回复选项文字")
    if submit_needed:
        lines.append("📦 多选逐个回复序号，回「提交」提交")
    lines.append("🚫 回「取消」取消提问")
    return "\n".join(lines)


def _match_option(text, questions, answered_idxs):
    """匹配用户回复 text。返回 (qidx, label) 或 None。

    单选已答的跳过；多选始终允许（累积切换）。
    优先级：1) 序号(1-based) 2) 严格等于 label 3) 包含关系（最长 label 优先）。
    """
    t = (text or "").strip()
    if not t:
        return None
    # 1) 序号
    if t.isdigit():
        n = int(t)
        for i, q in enumerate(questions):
            if i in answered_idxs and not q.get("multiple"):
                continue
            opts = q.get("options", []) or []
            if 1 <= n <= len(opts):
                return (i, opts[n - 1].get("label", ""))
    # 2) 严格 label
    for i, q in enumerate(questions):
        if i in answered_idxs and not q.get("multiple"):
            continue
        for o in (q.get("options", []) or []):
            lbl = o.get("label", "") or ""
            if lbl and lbl == t:
                return (i, lbl)
    # 3) 包含关系（最长 label 优先）
    cand = []
    for i, q in enumerate(questions):
        if i in answered_idxs and not q.get("multiple"):
            continue
        for o in (q.get("options", []) or []):
            lbl = o.get("label", "") or ""
            if lbl and (lbl in t or t in lbl):
                cand.append((len(lbl), i, lbl))
    if cand:
        cand.sort(key=lambda x: -x[0])
        return (cand[0][1], cand[0][2])
    return None


# ---------------------------------------------------------------------------
# pending 状态操作
# ---------------------------------------------------------------------------

def _answered_idxs(p):
    """已答题目下标集合（单选=有值，多选=有非空 list）。"""
    out = set()
    for i, q in enumerate(p["questions"]):
        v = p["answers"].get(i)
        if q.get("multiple"):
            if isinstance(v, list) and v:
                out.add(i)
        elif v:
            out.add(i)
    return out


def _all_answered(p):
    """所有题都答了？（单选每题有值；多选每题至少选一个）"""
    return len(_answered_idxs(p)) == len(p["questions"])


def _answers_arr(p):
    """构建 POST reply 的 answers（每题一个 label 列表）。"""
    arr = []
    for i in range(len(p["questions"])):
        v = p["answers"].get(i)
        arr.append(list(v) if isinstance(v, list) else ([v] if v else []))
    return arr


def _pop(req_id):
    with _pending_lock:
        p = _pending.pop(req_id, None)
    if p and p.get("timer"):
        try:
            p["timer"].cancel()
        except Exception:
            pass
    return p


def _timeout(req_id):
    """超时未答：reject 解卡 + 通知来源群。"""
    p = _pop(req_id)
    if not p:
        return
    ok, msg = _post_question(req_id, "reject")
    log(f"question 超时自动取消 req={req_id[:16]} ok={ok} {msg}")
    send_reply(p["conv_id"], p["conv_type"], "⏰ 提问超时已自动取消。")


def _submit(req_id, p):
    """提交答案。已从 _pending pop 出。"""
    arr = _answers_arr(p)
    ok, msg = _post_question(req_id, "reply", arr)
    summary = "、".join(
        f"Q{i+1}={'/'.join(arr[i]) if arr[i] else '空'}" for i in range(len(arr)))
    if ok:
        log(f"question 已提交 req={req_id[:16]} answers={arr}")
        send_reply(p["conv_id"], p["conv_type"], f"✅ 已回答（{summary}），正在处理…")
    else:
        log(f"question 提交失败 req={req_id[:16]} {msg}")
        send_reply(p["conv_id"], p["conv_type"], f"⚠️ 提交失败：{msg}")


# ---------------------------------------------------------------------------
# hooks
# ---------------------------------------------------------------------------

def on_sse_event(event, port, password):
    """收 question.asked → 渲染发来源群 + 记 pending。返回 True=已消费。"""
    if event.get("type") != "question.asked":
        return False
    props = event.get("properties", {}) or {}
    req_id = props.get("id")
    sid = props.get("sessionID", "") or ""
    questions = props.get("questions", []) or []
    if not req_id or not questions:
        return False
    conv = session_conv(sid) or {}
    conv_id = conv.get("conv_id", "")
    conv_type = conv.get("conv_type", "2")
    if not conv_id:
        # 找不到来源群（非 brain 发起的 session？）→ 不认领，走 core 默认
        log(f"question: sid={sid[:12]} 无来源群映射，跳过")
        return False

    timer = threading.Timer(_Q_TIMEOUT, _timeout, args=(req_id,))
    with _pending_lock:
        _pending[req_id] = {
            "sid": sid, "conv_id": conv_id, "conv_type": conv_type,
            "questions": questions, "answers": {}, "timer": timer,
        }
    timer.daemon = True
    timer.start()
    send_reply(conv_id, conv_type, _render_question(questions))
    log(f"question: 发起 req={req_id[:16]} sid={sid[:12]} 共 {len(questions)} 题 → 群 {conv_id[:12]}")
    return True


def _find_pending_for_conv(conv_id):
    """找该群当前的 pending question（req_id, p）；无则 (None, None)。"""
    with _pending_lock:
        for req_id, p in _pending.items():
            if p["conv_id"] == conv_id:
                return req_id, p
    return None, None


def on_inbound(msg):
    """存在 pending question 时截获群里回复作答；否则放行给后续能力。"""
    req_id, p = _find_pending_for_conv(msg.conv_id)
    if not req_id:
        return False  # 该群没有待答问题，放行
    text = (msg.text or "").strip()
    low = text.lower()

    # 取消
    if low in _CANCEL_KEYWORDS:
        _pop(req_id)
        ok, m = _post_question(req_id, "reject")
        log(f"question 用户取消 req={req_id[:16]} ok={ok}")
        send_reply(msg.conv_id, msg.conv_type, "🚫 已取消提问。")
        return True

    # 提交（多选）
    if low in _SUBMIT_KEYWORDS:
        if _answered_idxs(p):
            popped = _pop(req_id)
            if popped:
                _submit(req_id, popped)
        else:
            send_reply(msg.conv_id, msg.conv_type, "还没选任何选项呢，先回复序号选择。")
        return True

    # 匹配选项
    m = _match_option(text, p["questions"], _answered_idxs(p))
    if not m:
        # 没匹配上：不干预，放行给 text_reply（用户可能在说别的）
        log(f"question: 回复未匹配任何选项，放行 text={text[:30]!r}")
        return False
    qidx, label = m
    q = p["questions"][qidx]
    with _pending_lock:
        if q.get("multiple"):
            cur = p["answers"].get(qidx)
            cur = list(cur) if isinstance(cur, list) else []
            if label in cur:
                cur.remove(label)          # 再选取消
            else:
                cur.append(label)          # 累积
            p["answers"][qidx] = cur
        else:
            p["answers"][qidx] = label
        multiple = bool(q.get("multiple"))
        done = _all_answered(p)

    if multiple:
        # 多选不自动提交，回显当前选择
        send_reply(msg.conv_id, msg.conv_type,
                   f"已记录 Q{qidx+1}：{label}（多选，回「提交」提交，或继续选）")
    elif done:
        # 单选全答完 → 自动提交
        popped = _pop(req_id)
        if popped:
            _submit(req_id, popped)
    else:
        send_reply(msg.conv_id, msg.conv_type, f"已记录 Q{qidx+1}：{label}")
    return True


# 测试用：清空 pending
def _reset():
    with _pending_lock:
        for p in _pending.values():
            if p.get("timer"):
                try:
                    p["timer"].cancel()
                except Exception:
                    pass
        _pending.clear()


CAPABILITY = Capability(
    name="question",
    on_inbound=on_inbound,
    handles_kinds={KIND_TEXT},   # 用户答案是文本
    on_sse_event=on_sse_event,
    priority=20,                 # 最先看到用户回复（先于 image40/forward50/text100）
    default_enabled=True,
)
register(CAPABILITY)
