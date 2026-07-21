#!/usr/bin/env python3
"""端到端验证 ask permission 审批链路（真 serve + 真 SSE + 真 permission 能力）。

链路：brain 建带 bash:ask 规则的 session → agent 调 bash 挂起 → serve 发 permission.asked
SSE → dispatch_sse → permission.on_sse_event 渲染（send_reply 被捕获）+ 记 pending →
脚本注入用户"同意" → dispatch_inbound → permission.on_inbound → POST reply once → bash 解卡执行。

只替换 send_reply（避免真发钉钉），其余全是生产代码路径。
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

# 关键：import 前设 per-session 权限规则，让 brain 建 session 时下传 bash:ask
os.environ["AGENT_OPENCODE_PERMISSION"] = '[{"permission":"bash","pattern":"*","action":"ask"}]'

import custom.capabilities            # noqa: 注册全部能力（含 permission）
from core.capabilities import dispatch_sse, dispatch_inbound
from core.inbound import InboundMessage, KIND_TEXT
from core.agent_common import find_serve_credentials
from core.brain import register_session
import core.builtin_caps.permission as P
import custom.brain as B

CONV = "e2e-perm-conv"
MODEL = os.environ.get("AGENT_OPENCODE_MODEL", "local/qwen3-7-max")
captured = []                          # send_reply 捕获的 (conv_id, text)
asked_evt = threading.Event()          # 审批请求已渲染

def _fake_send_reply(conv_id, conv_type, text):
    captured.append((conv_id, text))
    tag = text.splitlines()[0] if text else ""
    print(f"  [send_reply→群] {tag}")
    if "需要授权" in text:
        asked_evt.set()
    return True

P.send_reply = _fake_send_reply        # 换掉 permission 能力里的发送

pid, port, pwd = find_serve_credentials()
if not port:
    print("SKIP: 未发现 opencode serve（需先起 serve 才能跑本 e2e）")
    sys.exit(0)
auth = "Basic " + base64.b64encode(f"opencode:{pwd}".encode()).decode()
print(f"serve port={port}")

# 1) 建带 ask 规则的 session（走 brain 真实建 session 逻辑，含 _OPENCODE_PERMISSION）
sid = B._create_session(port, pwd)
register_session(sid, {"conv_id": CONV, "conv_type": "2", "msg_id": "m1", "user": "tester"})
print(f"session={sid}  ask规则已下传={bool(B._OPENCODE_PERMISSION)}")

# 2) SSE 读取线程：把事件喂给真实 registry 分发
def sse_loop():
    req = urllib.request.Request(f"http://127.0.0.1:{port}/event",
                                 headers={"Authorization": auth})
    r = urllib.request.urlopen(req, timeout=180)
    for raw in r:
        line = raw.decode("utf-8").strip()
        if not line.startswith("data:"):
            continue
        try:
            evt = json.loads(line[5:].strip())
        except ValueError:
            continue
        if "permission" in evt.get("type", ""):
            print(f"  [SSE] {evt['type']}")
        dispatch_sse(evt, port, pwd)
        if evt.get("type") in ("permission.replied", "permission.v2.replied"):
            return

threading.Thread(target=sse_loop, daemon=True).start()

# 3) 注入"同意"线程：等审批渲染出来后，走真实 on_inbound
def approve():
    if not asked_evt.wait(timeout=60):
        print("  !! 60s 未见审批请求"); return
    time.sleep(0.3)
    print("  [用户] 回复：同意")
    msg = InboundMessage(user="tester", text="同意", conv_type="2",
                         conv_id=CONV, msg_id="m2", kind=KIND_TEXT)
    consumed = dispatch_inbound(msg)
    print(f"  [dispatch_inbound] consumed={consumed}")

threading.Thread(target=approve, daemon=True).start()

# 4) POST message 触发 bash（阻塞到审批 reply）
prov, mid = B._split_model(MODEL)
t0 = time.time()
body = json.dumps({
    "model": {"providerID": prov, "modelID": mid},
    "parts": [{"type": "text",
               "text": "运行 bash 命令 `echo PERM_E2E_OK` 并把输出原样告诉我。"}],
}).encode()
req = urllib.request.Request(f"http://127.0.0.1:{port}/session/{sid}/message",
                             data=body, method="POST",
                             headers={"Authorization": auth, "Content-Type": "application/json"})
print("POST message（触发 bash，将挂起等审批）…")
d = json.loads(urllib.request.urlopen(req, timeout=180).read().decode())
elapsed = time.time() - t0

# 5) 断言
reply = "".join(p.get("text", "") for p in d.get("parts", []) if p.get("type") == "text")
tools = [(p.get("tool"), p.get("state", {}).get("status"))
         for p in d.get("parts", []) if p.get("type") == "tool"]
print(f"\n耗时={elapsed:.1f}s  finish={d.get('info',{}).get('finish')}")
print(f"tool 调用={tools}")
print(f"回复={reply.strip()[:200]!r}")

B._delete_session(port, pwd, sid)

ok_asked = any("需要授权" in t for _, t in captured)
ok_approved = any("已放行" in t for _, t in captured)
ok_bash = "PERM_E2E_OK" in reply or any("PERM_E2E_OK" in str(p) for p in d.get("parts", []))
print("\n=== 结果 ===")
print(f"  V1 发审批到群 : {'✅' if ok_asked else '❌'}")
print(f"  V2 同意回执    : {'✅' if ok_approved else '❌'}")
print(f"  V3 bash 解卡执行: {'✅' if ok_bash else '❌'}")
print("PASS" if (ok_asked and ok_approved and ok_bash) else "FAIL")
