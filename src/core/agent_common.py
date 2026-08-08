"""agent_common.py — 数字员工共享 Python 工具

提炼自: dingtalk-opencode-agent/agent_common.py (v4.1)
原作者: hugozhu

提供 5 类共享工具，被 event_watcher.py 和各能力共用：

1. 常量（机器人身份 / profile / 超时）
2. 日志 & 通知（log / send_notification / _md）
3. dws/CLI 包装（_run_cli）
4. opencode serve 访问（find_serve_credentials / serve_request）——
   凭据发现与 HTTP 出口集中一处；具体的**会话操作**（建/删/复用）属于后端策略，
   由 custom/brain.py 按 conv 维护（见 AGENT_SESSION_REUSE），不在这里。
5. 视觉/多模态识别（_proxy_vision）+ session title 归一（_clean_session_title）

纯工具——无模块级可变状态，无全局副作用。
"""

import base64
import json
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

# ---------------------------------------------------------------------------
# Config loading — config/config.local.json（真实值）覆盖占位默认
#
# 优先级（从高到低）: 环境变量 > config.local.json > config.example.json > 硬编码默认
# 这样 FDE 填了 config.local.json 就能真正生效（此前该文件从没被读取）。
# ---------------------------------------------------------------------------

_CONFIG_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "config")
# 项目根（运行时状态文件 .serve.port/.serve.pwd 等所在目录）
_PROJECT_ROOT = os.environ.get(
    "PROJECT_DIR",
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def _load_config_file():
    """读 config.local.json（优先）或 config.example.json，返回扁平 dict 或 {}。"""
    for name in ("config.local.json", "config.example.json"):
        path = os.path.join(_CONFIG_DIR, name)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


_CONFIG = _load_config_file()


def _cfg(env_key, *json_path, default=""):
    """取配置值：环境变量优先，然后 config.json 的嵌套路径，最后 default。"""
    if env_key and os.environ.get(env_key) is not None:
        return os.environ[env_key]
    node = _CONFIG
    for key in json_path:
        if not isinstance(node, dict):
            return default
        node = node.get(key)
        if node is None:
            return default
    return node if node is not None else default


_TRUE_TOKENS = ("1", "true", "yes", "on")
_FALSE_TOKENS = ("0", "false", "no", "off", "")


def env_flag(key, default=False):
    """统一布尔环境变量解析。1/true/yes/on → True；0/false/no/off/空 → False。

    未设置该变量时返回 default。能力开关（CAP_<NAME>_ENABLED）等用它，避免各处
    自己写 `os.environ.get(...) == "1"` 口径不一。
    """
    raw = os.environ.get(key)
    if raw is None:
        return default
    v = raw.strip().lower()
    if v in _TRUE_TOKENS:
        return True
    if v in _FALSE_TOKENS:
        return False
    return default



# ---------------------------------------------------------------------------
# Constants — 环境变量 > config.local.json > 默认
# ---------------------------------------------------------------------------

# 机器人/数字员工身份（用于 send_notification 用机器人身份发消息）
ROBOT_CODE = _cfg("AGENT_ROBOT_CODE", "identity", "robot_code", default="your-robot-code")
USER_ID = _cfg("AGENT_USER_ID", "identity", "user_id", default="your-user-id")
PROFILE = _cfg("AGENT_PROFILE", "identity", "profile", default="default-profile")

# 视觉/多模态模型（经代理服务调用）
PROXY_URL = _cfg("PROXY_URL", "vision", "proxy_url", default="http://localhost:4000/v1")
PROXY_KEY = _cfg("PROXY_KEY", "vision", "proxy_key", default="sk-1234")
VISION_MODEL = _cfg("VISION_MODEL", "vision", "model", default="gemini-3.1-flash-image")

# serve 凭据发现缓存（避免每次访问都全表 ps 扫描）
_CREDS_CACHE_TTL = float(os.environ.get("AGENT_CREDS_CACHE_TTL", "10"))
_creds_cache = {}
_creds_lock = threading.Lock()

# 业务 handler 派发线程池（有界并发，双池隔离）
# 此前每条消息 threading.Thread().start() 无上限；消息突发 + 每个 handler 阻塞
# 数十秒（轮询 + post_user_message）会导致线程数无界。用有界池限流。
#
# 单池的坑（#82）：所有能力（文本/图片/文件/转发/聚合）共用一个 8-worker 池，
# 而每个 handler 同步阻塞在 opencode POST（最长 AGENT_OPENCODE_MAX_TIMEOUT，默认
# 3600s）。8 个长任务即可打满整池 → 之后所有会话的消息全部排队饿死（跨会话
# head-of-line blocking）。拆成两条独立限流的车道，让重活不阻塞轻交互：
#   - reply 池：交互式文本回复（text_reply），走快车道
#   - task 池：媒体/文件/合并转发/聚合等较重处理
# 两池独立配置 worker 数；某一池打满不会拖垮另一池。
_TASK_MAX_WORKERS = int(os.environ.get("AGENT_HANDLER_WORKERS", "8"))
_REPLY_MAX_WORKERS = int(os.environ.get("AGENT_REPLY_WORKERS", "4"))
_task_pool = ThreadPoolExecutor(
    max_workers=_TASK_MAX_WORKERS, thread_name_prefix="task")
_reply_pool = ThreadPoolExecutor(
    max_workers=_REPLY_MAX_WORKERS, thread_name_prefix="reply")


def _submit(pool, fn, args, kwargs):
    """把 handler 提交到指定有界池；内部异常吞掉并记日志，避免污染池。"""
    def _wrapped():
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            log(f"handler {getattr(fn, '__name__', fn)} err: {e}")
    return pool.submit(_wrapped)


def submit_handler(fn, *args, **kwargs):
    """把较重的业务 handler 提交到 task 池执行（替代裸 threading.Thread）。

    返回 Future。图片/文件/合并转发/聚合等走这里。交互式文本回复请用
    submit_reply()，避免长任务饿死轻量对话。
    """
    return _submit(_task_pool, fn, args, kwargs)


def submit_reply(fn, *args, **kwargs):
    """把交互式文本回复提交到独立的 reply 池，与 task 池隔离限流（#82）。"""
    return _submit(_reply_pool, fn, args, kwargs)


# ---------------------------------------------------------------------------
# Logging & Notification
# ---------------------------------------------------------------------------

def log(msg):
    """统一日志：时间戳 + 组件前缀，打到 stdout（由 launchd 落盘）"""
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [agent] {msg}", flush=True)


def send_notification(title, text):
    """通过 dws CLI 以机器人身份发消息到指定用户。

    用户实现可替换为别的通知后端（Slack/邮件/企业微信等）。
    """
    try:
        r = subprocess.run(
            ["dws", "chat", "message", "send-by-bot",
             "--robot-code", ROBOT_CODE, "--users", USER_ID,
             "--title", title[:60],
             "--text", text,
             "--profile", PROFILE, "--format", "markdown"],
            capture_output=True, text=True, timeout=15,
        )
        if r.returncode != 0:
            log(f"send FAIL rc={r.returncode} title={title} stderr={r.stderr[:200]}")
            return False
        log(f"send OK title={title}")
        return True
    except Exception as e:
        log(f"send err: {e}")
        return False


def _md(title, status, body=""):
    """渲染简单的 markdown 消息（标题 + 粗体状态 + 可选正文）"""
    msg = f"### {title}\n\n**{status}**"
    if body:
        msg += f"\n\n{body}"
    return msg


# ---------------------------------------------------------------------------
# CLI wrapper
# ---------------------------------------------------------------------------

def _run_cli(args, timeout=60):
    """运行 CLI 子命令，带 profile。返回 (rc, stdout)。

    用于反查消息体（list-by-ids）、下载媒体、查群消息等。
    """
    cmd = ["dws"] + args
    if PROFILE:
        cmd += ["--profile", PROFILE]
    cmd += ["-y"]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return res.returncode, res.stdout.strip()
    except Exception as e:
        return -1, str(e)


# ---------------------------------------------------------------------------
# opencode serve access — 凭证/会话操作工具集
# ---------------------------------------------------------------------------

def find_serve_credentials():
    """从进程表定位 opencode serve 进程，返回 (pid, port, password) 或 (None, None, None)。

    通用思路: ps -ax 找 "<serve-cmd>" 进程 → 提取 --port + OPENCODE_SERVER_PASSWORD 环境变量

    带短 TTL 缓存（_CREDS_CACHE_TTL 秒）：多个调用方（brain / image / file / question
    / healthcheck 探测）各自调用本函数，一次对话可触发多次全表 ps。缓存命中时跳过
    subprocess，大幅降低进程表扫描频率。缓存的是成功结果；失败不缓存（下次立即重试）。
    """
    now = time.time()
    with _creds_lock:
        cached = _creds_cache.get("value")
        if cached and (now - _creds_cache.get("ts", 0)) < _CREDS_CACHE_TTL:
            return cached

    creds = _discover_serve_credentials()
    if creds[1]:  # 只缓存成功结果（port 非空）
        with _creds_lock:
            _creds_cache["value"] = creds
            _creds_cache["ts"] = now
    return creds


def invalidate_serve_credentials():
    """清除凭据缓存（端口/密码变更、连接失败重连前调用）。"""
    with _creds_lock:
        _creds_cache.pop("value", None)
        _creds_cache.pop("ts", None)


def _discover_serve_credentials():
    """定位 serve 凭据。返回 (pid, port, password)。

    单一真相源：优先读 .serve.port / .serve.pwd 文件（start_serve 写、healthcheck 读），
    避免与进程表扫描出现两套真相导致漂移。文件缺失/不完整时回退到 ps 扫描，
    并把扫描结果写回文件，让 healthcheck 与本模块看到一致的凭据。
    """
    # 1. 优先从状态文件读（与 healthcheck check_serve_http 同源）
    port = _read_state_file(".serve.port")
    pwd = _read_state_file(".serve.pwd")
    if port and pwd:
        try:
            pid = int(_read_state_file(".serve.pid") or 0) or None
        except ValueError:
            pid = None
        try:
            return pid, int(port), pwd
        except ValueError:
            pass  # port 文件损坏，回退扫描

    # 2. 回退：进程表扫描
    try:
        result = subprocess.run(
            ["ps", "-ax", "-o", "pid,args"],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.strip().splitlines():
            if "agent-serve" not in line or "grep" in line:
                continue
            pid = line.split()[0]
            m = re.search(r"--port\s+(\d+)", line)
            port = int(m.group(1)) if m else None
            if pid and port:
                pr = subprocess.run(["ps", "eww", "-p", str(pid)], capture_output=True, text=True, timeout=5)
                # 通用：从环境变量提取 password（用户替换为自己的 serve 实现的环境变量名）
                pm = re.search(r"AGENT_SERVER_PASSWORD=(\S+)", pr.stdout)
                pwd = pm.group(1) if pm else None
                if pwd:
                    # 写回状态文件，保持与 healthcheck 同源
                    _write_state_file(".serve.pid", str(pid))
                    _write_state_file(".serve.port", str(port))
                    _write_state_file(".serve.pwd", pwd)
                    return int(pid), port, pwd
    except Exception as e:
        log(f"find serve err: {e}")
    return None, None, None


def _read_state_file(basename):
    """读 PROJECT_ROOT 下的运行时状态文件，返回 strip 后内容或 None。"""
    path = os.path.join(_PROJECT_ROOT, basename)
    try:
        with open(path, "r", encoding="utf-8") as f:
            v = f.read().strip()
            return v or None
    except Exception:
        return None


def _write_state_file(basename, value):
    """写运行时状态文件（best-effort，失败仅记日志）。"""
    path = os.path.join(_PROJECT_ROOT, basename)
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(value)
    except Exception as e:
        log(f"write state {basename} err: {e}")


# ---------------------------------------------------------------------------
# serve_request — 向 opencode serve 发 HTTP 请求的**唯一出口**
#
# 此前 auth+Request+urlopen+loads 的模板在本模块被拷贝 8 份（下面各 helper），brain /
# image 各再拷一份。统一到这里：凭据发现、Basic auth、可选调试日志、JSON 解析集中一处。
# ---------------------------------------------------------------------------

# 调试日志开关：AGENT_DEBUG=1 时把每个 serve 请求/响应的完整 body 写到 opencode.log
# （便于排查发给模型的 prompt / 模型返回）。默认关，避免日志爆炸。路径同 brain 的 _oc_log。
# 统一调试总开关（原独立的 AGENT_SERVE_DEBUG 已并入）：serve body / brain 调用摘要 /
# ack 轨迹 / serve 自身日志级别都由 AGENT_DEBUG 控制。
_SERVE_DEBUG = os.environ.get("AGENT_DEBUG", "") in ("1", "true", "True", "yes", "on")
_SERVE_LOG = os.environ.get("AGENT_OPENCODE_LOG", os.path.join(_PROJECT_ROOT, "opencode.log"))
# 长 body（图片 base64 data_url 可达几百 KB）截断：留头 _SERVE_LOG_HEAD + 尾 _SERVE_LOG_TAIL。
_SERVE_LOG_HEAD = 1000
_SERVE_LOG_TAIL = 500


def _serve_log(line):
    """把一行调试日志写到 opencode.log（best-effort，写失败静默）。"""
    try:
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(_SERVE_LOG, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {line}\n")
    except Exception:
        pass


def _serve_log_trunc(s):
    """截断过长 body，保留头尾便于阅读（中间标注截断字符数）。"""
    if s is None:
        return ""
    if len(s) <= _SERVE_LOG_HEAD + _SERVE_LOG_TAIL:
        return s
    return f"{s[:_SERVE_LOG_HEAD]}...[截断{len(s)}字符]...{s[-_SERVE_LOG_TAIL:]}"


def serve_request(method, path, body=None, timeout=8, *, port=None, pwd=None):
    """向 opencode serve 发一个 HTTP 请求的唯一出口。返回解析后的 JSON（无响应体返回 None）。

    - port/pwd 缺省时自动 find_serve_credentials()；仍无端口 → 抛 RuntimeError。
    - pwd 非空 → Basic auth(opencode:<pwd>)；否则不带鉴权头（与 healthcheck 约定一致）。
    - AGENT_DEBUG=1 时把 method/path/请求 body、响应 status+body 写到 opencode.log；
      长 body（图片 data_url 等）自动截断头尾。
    - **不吞异常**：HTTPError/URLError 照常抛出，调用方保留各自的 404/超时/凭据缺失处理。
    """
    if port is None:
        _, port, pwd = find_serve_credentials()
    if not port:
        raise RuntimeError("serve 凭据缺失（port 为空）")
    data = json.dumps(body).encode() if body is not None else None
    headers = {}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if pwd:
        headers["Authorization"] = "Basic " + base64.b64encode(
            f"opencode:{pwd}".encode()).decode()
    if _SERVE_DEBUG:
        body_str = json.dumps(body, ensure_ascii=False) if body is not None else ""
        _serve_log(f">>> REQ {method} {path} body={_serve_log_trunc(body_str)}")
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=data, headers=headers, method=method)
    r = urllib.request.urlopen(req, timeout=timeout)
    raw = r.read().decode("utf-8")
    if _SERVE_DEBUG:
        _serve_log(f"<<< RESP {method} {path} status={r.status} body={_serve_log_trunc(raw)}")
    return json.loads(raw) if raw.strip() else None


def _clean_session_title(title, limit=60):
    """把 session title 压成单行短文本，供通知/日志展示。

    title 由 brain._session_title 生成（#89），形如「[群] 张三 · 帮我看下这个报错」，
    理论上已单行限长；但它源自用户消息，这里再收一次口——折叠换行与连续空白、超长截断，
    避免异常 title 把 markdown 通知撑坏。无 title 返回空串。
    """
    s = " ".join(str(title or "").split())
    if len(s) > limit:
        s = s[:limit] + "…"
    return s


# ---------------------------------------------------------------------------
# Vision / multimodal — via proxy
# ---------------------------------------------------------------------------

def _proxy_vision(image_bytes):
    """用多模态模型（经代理服务）识别图片，返回内容描述文本。

    prompt 改为逐字提取原文（保持顺序/换行/标点，不总结）。
    无文字图片则客观描述内容（场景/物体/UI 等），不做主观总结。
    """
    b64 = base64.b64encode(image_bytes).decode()
    body = json.dumps({
        "model": VISION_MODEL,
        "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
            {"type": "text", "text": "请逐字提取这张图片中的所有文字内容（保持原始顺序、换行、标点，不要省略或总结）。如果图片中没有文字，则客观描述图片内容（场景、物体、UI 元素、图表数据等），不要做主观总结或解读。"},
        ]}],
    }).encode()
    try:
        req = urllib.request.Request(
            f"{PROXY_URL}/chat/completions",
            data=body,
            headers={"Authorization": f"Bearer {PROXY_KEY}", "Content-Type": "application/json"},
            method="POST",
        )
        r = urllib.request.urlopen(req, timeout=60)
        d = json.loads(r.read().decode("utf-8"))
        return (d.get("choices", [{}])[0].get("message", {}).get("content", "") or "").strip()
    except Exception as e:
        log(f"vision err: {e}")
        return ""


