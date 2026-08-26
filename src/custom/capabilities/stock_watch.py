"""stock_watch — 行情快照异动监控能力

树莓派/PiBot 会周期性推送美股行情快照（形如 `# 名称 ## 现价 涨跌(涨跌%) **盘后...**`）。
按 docs/EVENT_HANDLING_POLICY.md「数据推送」策略：常规推送默认静默，仅**异常波动**告警。

本能力：
- 识别行情快照格式（含三大指数标记 + ≥3 个标的）。
- 解析每个标的的现价，维护价格历史（状态文件，默认 8 天）。
- 计算两类波动并对照阈值：
    - 单次推送较上一次的变化 ≥ STOCK_WATCH_SINGLE_PCT（默认 5%）
    - 24h 累计变化 ≥ STOCK_WATCH_DAY_PCT（默认 10%）
- 命中任一 → 向来源会话发统一格式告警；否则**消费且静默**（不再交给 LLM 总结，
  避免每次推送都刷屏）。

设计要点：
- 只处理「推送 → 推送」的对比，首次推送仅播种状态、不告警（无对比基准）。
- 24h 基准取历史里最接近 24h 前、且至少 STOCK_WATCH_MIN_REF_AGE_H（默认 20h）前的点；
  历史不足时跳过 24h 判定，避免用太近的基准误报。
- 状态文件原子写（tmp + rename），线程锁保护。

开关：CAP_STOCK_WATCH_ENABLED（默认开）
环境变量：
  STOCK_WATCH_STATE_FILE      状态文件路径（默认 $PROJECT_DIR/.stock-watch-state.json）
  STOCK_WATCH_SINGLE_PCT      单次推送告警阈值 %（默认 5）
  STOCK_WATCH_DAY_PCT         24h 累计告警阈值 %（默认 10）
  STOCK_WATCH_DAY_WINDOW_H    24h 窗口小时数（默认 24）
  STOCK_WATCH_MIN_REF_AGE_H   24h 基准点最小年龄小时（默认 20）
  STOCK_WATCH_RETENTION_D     价格历史保留天数（默认 8）

优先级：30（晚于 cancel5/stats10/permission15/question20，早于 image/audio/file40、
forward50、aggregation90、text_reply100）。行情快照是普通文本，前面的命令型能力都
不命中，会透传到本能力。
"""

import json
import os
import re
import threading
import time

from core.agent_common import log
from core.capabilities import Capability, register
from core.inbound import KIND_TEXT
from core.replier import send_reply


# ---------------------------------------------------------------------------
# 阈值 / 路径（env 可调）
# ---------------------------------------------------------------------------
_SINGLE_PCT = float(os.environ.get("STOCK_WATCH_SINGLE_PCT", "5"))
_DAY_PCT = float(os.environ.get("STOCK_WATCH_DAY_PCT", "10"))
_DAY_WINDOW = float(os.environ.get("STOCK_WATCH_DAY_WINDOW_H", "24")) * 3600
_MIN_REF_AGE = float(os.environ.get("STOCK_WATCH_MIN_REF_AGE_H", "20")) * 3600
_RETENTION = float(os.environ.get("STOCK_WATCH_RETENTION_D", "8")) * 86400


def _state_file():
    override = os.environ.get("STOCK_WATCH_STATE_FILE")
    if override:
        return override
    base = os.environ.get("PROJECT_DIR") or os.getcwd()
    if not os.path.isdir(base):
        # PROJECT_DIR 占位/缺失时回退仓库根（本文件位于 src/custom/capabilities/，向上 3 级）
        base = os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))))
    return os.path.join(base, ".stock-watch-state.json")


# ---------------------------------------------------------------------------
# 快照解析
# ---------------------------------------------------------------------------
# 单条标的：`# 名称 ## 现价 涨跌(涨跌%)`；名称到 ` ## ` 为止，现价/涨跌允许千分位逗号。
_ENTRY_RE = re.compile(
    r"#\s*(?P<name>.+?)\s*##\s*(?P<price>[\d,]*\.?\d+)\s+"
    r"(?P<chg>[+-]?[\d,]*\.?\d+)\((?P<pct>[+-]?\d+\.?\d*)%\)"
)
# 行情快照必然带三大指数之一——用它做格式判定，避免误伤普通含 `#` 的文本。
_INDEX_MARKERS = ("道琼斯", "标普", "纳斯达克", ".DJI", ".INX", ".IXIC")

# 展示别名（状态 key 用原始名归一化，别名仅用于告警可读性）
_ALIASES = [
    ("alibaba", "阿里 BABA"),
    ("pdd", "拼多多 PDD"),
    ("amazon", "亚马逊 AMZN"),
    ("alphabet", "谷歌 GOOGL"),
    ("microsoft", "微软 MSFT"),
    ("apple", "苹果 AAPL"),
    ("tesla", "特斯拉 TSLA"),
    ("道琼斯", "道指"),
    ("标普", "标普500"),
    ("纳斯达克", "纳指"),
]


def _label(name):
    low = name.lower()
    for pat, lbl in _ALIASES:
        if pat in low:
            return lbl
    return name


def _key(name):
    """状态 key：归一化名称（压空白、去结尾句点），兼容 'Apple Inc'/'Apple Inc.'。"""
    return re.sub(r"\s+", " ", name).strip().rstrip(".").strip()


def is_stock_snapshot(text):
    text = text or ""
    if not any(m in text for m in _INDEX_MARKERS):
        return False
    return sum(1 for _ in _ENTRY_RE.finditer(text)) >= 3


def parse_snapshot(text):
    """解析快照 → [(name, price, reported_pct)]。price 为 float，reported_pct 为推送自带涨跌幅。"""
    out = []
    for m in _ENTRY_RE.finditer(text or ""):
        try:
            price = float(m.group("price").replace(",", ""))
        except ValueError:
            continue
        try:
            rep = float(m.group("pct"))
        except ValueError:
            rep = None
        out.append((m.group("name").strip(), price, rep))
    return out


# ---------------------------------------------------------------------------
# 波动计算
# ---------------------------------------------------------------------------
def _pct_change(old, new):
    if not old:
        return None
    return (new - old) / old * 100.0


def _day_ref(hist, now):
    """取最接近 24h 前、且年龄 ≥ _MIN_REF_AGE 的历史点；无则 None。"""
    cands = [e for e in hist if e.get("ts", 0) <= now - _MIN_REF_AGE]
    if not cands:
        return None
    target = now - _DAY_WINDOW
    return min(cands, key=lambda e: abs(e.get("ts", 0) - target))


def _is_anomaly(single, day):
    if single is not None and abs(single) >= _SINGLE_PCT:
        return True
    if day is not None and abs(day) >= _DAY_PCT:
        return True
    return False


# ---------------------------------------------------------------------------
# 状态读写（原子写 + 锁）
# ---------------------------------------------------------------------------
_state_lock = threading.Lock()


def _load_state(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(path, state):
    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(state, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception as e:
        log(f"stock_watch: 状态写入失败 {e}")


# ---------------------------------------------------------------------------
# 告警渲染
# ---------------------------------------------------------------------------
def format_alert(anoms):
    lines = [f"⚠️ [行情异动] {len(anoms)} 只标的波动超阈值"]
    for a in anoms:
        parts = [f"{a['label']} 现价 {a['price']:,.2f}"]
        if a["single"] is not None:
            parts.append(f"单次 {a['single']:+.2f}%（上次 {a['prev_price']:,.2f}）")
        if a["day"] is not None:
            parts.append(f"24h 累计 {a['day']:+.2f}%")
        lines.append("- " + "，".join(parts))
    lines.append(f"（阈值：单次≥{_SINGLE_PCT:g}% 或 24h≥{_DAY_PCT:g}%）")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 入站处理
# ---------------------------------------------------------------------------
def on_inbound(msg):
    """行情快照 → 更新状态、必要时告警。返回 True=已消费（含静默消费）。"""
    text = (msg.text or "").strip()
    if not is_stock_snapshot(text):
        return False
    entries = parse_snapshot(text)
    if not entries:
        return False

    now = time.time()
    path = _state_file()
    anomalies = []
    with _state_lock:
        state = _load_state(path)
        for name, price, rep in entries:
            key = _key(name)
            hist = state.get(key, [])
            prev = hist[-1] if hist else None
            single = _pct_change(prev["price"], price) if prev else None
            ref = _day_ref(hist, now)
            day = _pct_change(ref["price"], price) if ref else None
            if _is_anomaly(single, day):
                anomalies.append({
                    "label": _label(name),
                    "price": price,
                    "single": single,
                    "prev_price": prev["price"] if prev else None,
                    "day": day,
                    "reported": rep,
                })
            hist.append({"ts": now, "price": price})
            state[key] = [e for e in hist if e.get("ts", 0) >= now - _RETENTION]
        _save_state(path, state)

    log(f"stock_watch: 行情快照 {len(entries)} 只标的，异动 {len(anomalies)} 只")
    if anomalies:
        send_reply(msg.conv_id, msg.conv_type, format_alert(anomalies))
    return True


CAPABILITY = Capability(
    name="stock_watch",
    on_inbound=on_inbound,
    handles_kinds={KIND_TEXT},
    priority=30,
    default_enabled=True,
    dedup=True,       # msgId 去重，防同一推送重复处理
    loop_guard=True,  # 跳过数字员工自己发的消息，防回环
)
register(CAPABILITY)
