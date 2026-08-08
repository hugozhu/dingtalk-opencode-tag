#!/usr/bin/env python3
"""brain_probe.py — 大脑真实可用性探针（custom 层，opencode 特定）

被 healthcheck 的 `brain_probe` 钩子调用（`bin/custom/start_funcs.sh`），只在
opencode 失败计数超阈值时才跑：建临时 session → 问一句极简算术 → 断言拿到非空回复
→ 删 session。

**为什么需要它**：healthcheck 的「serve 进程存活」+「HTTP /session 应答 200」都不碰
模型。2026-08-08 大脑与模型网关失联 16 分钟，这两项全程 OK，任何请求都答不出来。

退出码：
  0 = 通过（大脑能答）
  1 = 失败（连不上 / 超时 / 回空）
  2 = 无法探测（serve 凭据缺失——那是 check_serve_http 的地盘，不重复判失败）

三条纪律，每条都是踩过的坑：

1. **不走 brain._post_message**：它会挂上生产的 watchdog（idle 300s / max 3600s），
   探针要的是自己的短超时。直接用底层 _serve_request。
2. **不写 $AGENT_OPENCODE_LOG**：那是 healthcheck 的计数来源。失败的探针若记一条
   ok=False，会抬高下一个窗口，把熔断永久闩死。绕开 _post_message 天然满足这条。
3. **登记到 .ext-sessions**：探针是独立进程，它的 sid 不在 event_watcher 的进程内
   登记表里；不登记的话每次探针都会让钉钉收到「📥 收到新请求」+「✅ 会话完成」。
"""

import os
import signal
import sys
import time

# src/ 从**脚本自身位置**推导，永远指向本仓库；PROJECT_DIR 只决定运行时状态文件
# （.serve.port 等）在哪。两者曾共用 PROJECT_DIR，导致把状态目录指走时连 src/ 一起
# 指丢，"找不到凭据"被误报成 ImportError。
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC_DIR = os.path.join(_REPO_ROOT, "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

PROBE_TIMEOUT = int(os.environ.get("HEALTHCHECK_BRAIN_PROBE_TIMEOUT", "60"))
PROBE_PROMPT = os.environ.get("HEALTHCHECK_BRAIN_PROBE_PROMPT", "1+1=?")
PROBE_SYSTEM = os.environ.get("HEALTHCHECK_BRAIN_PROBE_SYSTEM", "只输出结果，不要解释。")

EXIT_OK, EXIT_FAIL, EXIT_CANNOT_PROBE = 0, 1, 2


def _alarm(_sig, _frm):
    # 自杀兜底：即使 HTTP 层的超时没生效（socket 卡在某个奇怪状态），也保证进程能退。
    print("probe: 自身超时（signal.alarm）", file=sys.stderr)
    os._exit(EXIT_FAIL)


def main():
    signal.signal(signal.SIGALRM, _alarm)
    signal.alarm(PROBE_TIMEOUT + 5)

    from core.agent_common import find_serve_credentials
    from core.brain import register_ext_session, unregister_ext_session
    import custom.brain as brain

    # 优先用调用方（healthcheck 的 check_brain）已解析好的凭据。两边各自发现凭据的话，
    # 可能指向不同的 serve 实例 —— 出现「serve_http 说不通、探针说通」的自相矛盾裁决。
    port = os.environ.get("AGENT_PROBE_PORT") or ""
    pwd = os.environ.get("AGENT_PROBE_PWD") or ""
    if not port:
        _pid, port, pwd = find_serve_credentials()
    if not port:
        print("probe: serve 凭据缺失，无法探测")
        return EXIT_CANNOT_PROBE

    provider, model_id = brain._split_model(brain._OPENCODE_MODEL)
    sid = None
    t0 = time.time()
    try:
        sid = brain._create_session(port, pwd, "healthcheck-brain-probe")
        register_ext_session(sid)
        d = brain._serve_request(
            "POST", port, pwd, f"/session/{sid}/message",
            {
                "model": {"providerID": provider, "modelID": model_id},
                "system": PROBE_SYSTEM,
                "parts": [{"type": "text", "text": PROBE_PROMPT}],
            },
            timeout=PROBE_TIMEOUT,
        ) or {}
        reply = "".join(
            p.get("text", "") for p in d.get("parts", []) if p.get("type") == "text"
        ).strip()
        elapsed = time.time() - t0
        if not reply:
            # 空回复也算失败：这正是 session 中毒的症状（后端把回合 completed 成空），
            # 表现为「活着但没用」——恰恰是最需要被抓到的那种坏。
            print(f"probe: 回复为空 model={brain._OPENCODE_MODEL} elapsed={elapsed:.1f}s")
            return EXIT_FAIL
        print(f"probe: OK model={brain._OPENCODE_MODEL} elapsed={elapsed:.1f}s "
              f"reply={reply[:40]!r}")
        return EXIT_OK
    except Exception as e:
        print(f"probe: {type(e).__name__}: {str(e)[:120]} elapsed={time.time() - t0:.1f}s")
        return EXIT_FAIL
    finally:
        signal.alarm(0)
        if sid:
            unregister_ext_session(sid)
            brain._delete_session(port, pwd, sid)


if __name__ == "__main__":
    sys.exit(main())
