#!/usr/bin/env python3
"""convq.py — 查本会话落盘消息的命令行入口（薄壳，逻辑在 src/custom/convq.py）

大脑（opencode agent）通过 bash 调它。分成两层是为了 `main(argv)` 能在单测里直接跑、
捕获 stdout，不必起子进程。

两件必须在这里做、不能留给逻辑层的事：

1. **PROJECT_DIR**：serve 的 env 里**没有**这个变量（实测 /proc/<serve pid>/environ），
   而 msgstore._root() 拿它来解析相对路径 —— 不设就会落到 os.getcwd()，查了个空目录
   还一声不吭。从**脚本自身位置**反推仓库根，和 brain_probe.py 一个路数。
2. **自杀闹钟**：任何 bash 工具调用都不该把大脑卡死。比 --wait 多留 30s 的余量。
"""

import os
import signal
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(_REPO_ROOT, "src"))
os.environ.setdefault("PROJECT_DIR", _REPO_ROOT)

from custom import convq  # noqa: E402


def _alarm(_sig, _frm):
    sys.stderr.write("convq: 超时退出\n")
    sys.exit(convq.EXIT_TIMEOUT)


if __name__ == "__main__":
    argv = sys.argv[1:]
    budget = 60
    for i, v in enumerate(argv):            # --wait N / --wait=N 都认
        if v == "--wait" and i + 1 < len(argv):
            budget = int(argv[i + 1] or 0)
        elif v.startswith("--wait="):
            budget = int(v.split("=", 1)[1] or 0)
    if hasattr(signal, "SIGALRM"):
        signal.signal(signal.SIGALRM, _alarm)
        signal.alarm(max(30, budget) + 30)
    sys.exit(convq.main(argv))
