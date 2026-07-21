#!/usr/bin/env python3
"""端到端验证 question 能力的【超时自动取消】分支（真 serve + 真 SSE + 真 question 能力）。

链路：agent 调 question 工具提问 → serve 发 question.asked SSE → dispatch_sse →
question.on_sse_event 渲染（send_reply 捕获）+ 记 pending + 启 Timer(_Q_TIMEOUT) →
【故意不回答】→ 定时器触发 _timeout → POST /question/{id}/reject → 阻塞的 message POST
解卡返回 → send_reply "⏰ 提问超时已自动取消"。

只替换 send_reply（避免真发钉钉），其余全是生产代码路径。超时设 8s 便于快速验证。
"""
import base64
import json
import os
import sys
import threading
import time
import urllib.request

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "src"))

# 关键：import 前设短超时，让 question 能力的 _Q_TIMEOUT 取到 8s
os.environ["CAP_QUESTION_TIMEOUT"] = "8"

import custom.capabilities            # noqa: 注册全部能力（含 question）
from core.capabilities import dispatch_sse
from core.agent_common import find_serve_credentials
from core.brain import register_session
import core.builtin_caps.question as Q

CONV = "e2e-qtimeout-conv"
MODEL = os.environ.get("AGENT_OPENCODE_MODEL", "local/qwen3-7-max")
captured = []
asked_evt = threading.Event()

def _fake_send_reply(conv_id, conv_type, text):
    captured.append((conv_id, text))
    print(f"  [send_reply→群] {text.splitlines()[0] if text else ''}")
    if "需要你的输入" in text:
        asked_evt.set()
    return True

Q.send_reply = _fake_send_reply
print(f"question 超时 _Q_TIMEOUT={Q._Q_TIMEOUT}s")

pid, port, pwd = find_serve_credentials()
if not port:
    print("SKIP: 未发现 opencode serve（需先起 serve 才能跑本 e2e）")
    sys.exit(0)
auth = "Basic " + base64.b64encode(f"opencode:{pwd}".encode()).decode()

def req(method, path, body=None, timeout=180):
    data = json.dumps(body).encode() if body is not None else None
    h = {"Authorization": auth}
    if data:
        h["Content-Type"] = "application/json"
    r = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method=method, headers=h)
    return json.loads(urllib.request.urlopen(r, timeout=timeout).read().decode() or "null")

sid = req("POST", "/session", {"title": "q-timeout-e2e"})["id"]
register_session(sid, {"conv_id": CONV, "conv_type": "2", "msg_id": "m1", "user": "tester"})
print(f"session={sid}")

# SSE 线程：喂真实 registry 分发（question.on_sse_event 会启 8s 定时器）
def sse_loop():
    r = urllib.request.Request(f"http://127.0.0.1:{port}/event", headers={"Authorization": auth})
    for raw in urllib.request.urlopen(r, timeout=180):
        line = raw.decode("utf-8").strip()
        if not line.startswith("data:"):
            continue
        try:
            evt = json.loads(line[5:].strip())
        except ValueError:
            continue
        if "question" in evt.get("type", ""):
            print(f"  [SSE] {evt['type']}")
        dispatch_sse(evt, port, pwd)
        if evt.get("type") in ("question.rejected", "question.replied"):
            return

threading.Thread(target=sse_loop, daemon=True).start()
time.sleep(0.5)

# POST message 触发 question 工具（会阻塞到超时 reject 解卡）
prov, mid = MODEL.split("/", 1) if "/" in MODEL else ("", MODEL)
prompt = ("你必须调用 question 工具向我提一个二选一的问题：晚饭吃什么？选项：面、饭。"
          "不要直接回答，一定要用 question 工具提问。")
print(f"POST message（触发 question，将挂起等超时；不回答）… 预计 ~{Q._Q_TIMEOUT}s 后解卡")
t0 = time.time()
d = req("POST", f"/session/{sid}/message",
        {"model": {"providerID": prov, "modelID": mid},
         "parts": [{"type": "text", "text": prompt}]})
elapsed = time.time() - t0
time.sleep(0.5)   # 等超时 send_reply 落地
req("DELETE", f"/session/{sid}")

# 断言
print(f"\n耗时={elapsed:.1f}s  finish={d.get('info',{}).get('finish')}")
ok_asked = any("需要你的输入" in t for _, t in captured)
ok_timeout = any("超时" in t for _, t in captured)
answered = any(("已回答" in t or "已记录" in t) for _, t in captured)
ok_unblocked = d.get("info", {}).get("finish") is not None
print("\n=== 结果 ===")
print(f"  V1 发提问到群       : {'✅' if ok_asked else '❌'}")
print(f"  V2 超时自动取消回执 : {'✅' if ok_timeout else '❌'}")
print(f"  V3 message POST 解卡: {'✅' if ok_unblocked else '❌'}（耗时≈超时{Q._Q_TIMEOUT}s）")
print(f"  V4 未注入任何答案   : {'✅' if not answered else '❌'}")
allok = ok_asked and ok_timeout and ok_unblocked and not answered
print("PASS" if allok else "FAIL")
sys.exit(0 if allok else 1)
