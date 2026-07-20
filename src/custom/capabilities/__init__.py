"""capabilities — 业务能力插件包（custom 层，可组装/可选配）

每个能力是本包下的一个模块，import 时构造一个 `core.capabilities.Capability` 并
`register()`。是否真正生效由各自的 `CAP_<NAME>_ENABLED` 开关决定（default_enabled
在能力里定；开关在 config/constants.local.sh 覆盖）。

在这里 import 一个能力模块 = 让它参与注册。**新增能力：写一个模块 + 在此 import。**
删/停用能力：注掉 import，或设 CAP_<NAME>_ENABLED=0（推荐后者，保留代码）。
"""

# 先注入平台实现：import custom.brain / custom.replier 会 register_brain / register_replier，
# 让能力经 core.brain.generate_reply / core.replier.send_reply 拿到 opencode/dws 实现（#52 P2）。
# 必须在能力 import 之前（能力模块顶层从 core.brain/replier 取函数引用；实现注册是运行时状态，
# 顺序其实不敏感——能力调用发生在运行时——但显式先注册更清晰）。
from custom import brain as _brain    # noqa: F401  注册 opencode/proxy/echo 大脑实现
from custom import replier as _replier  # noqa: F401  注册 dws 发送实现

# 顺序不影响分发（分发按 Capability.priority），只影响注册日志顺序。
from custom.capabilities import ack         # noqa: F401  回执：已读+状态表情（默认开）
from custom.capabilities import text_reply  # noqa: F401  文本回复（brain→replier）
from custom.capabilities import forward     # noqa: F401  合并转发（chatRecord）
from custom.capabilities import image       # noqa: F401  图片识别（vision 兜底）
from custom.capabilities import file        # noqa: F401  文档/文件处理（受控下载+注入）
from custom.capabilities import question    # noqa: F401  Question 交互（钉钉端答 agent 提问）
from custom.capabilities import aggregation  # noqa: F401  群消息聚合（默认关）

__all__ = ["ack", "text_reply", "forward", "image", "file", "question", "aggregation"]
