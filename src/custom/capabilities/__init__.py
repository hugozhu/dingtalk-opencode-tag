"""capabilities — 业务能力插件包（custom 层，可组装/可选配）

每个能力是本包下的一个模块，import 时构造一个 `core.capabilities.Capability` 并
`register()`。是否真正生效由各自的 `CAP_<NAME>_ENABLED` 开关决定（default_enabled
在能力里定；开关在 config/constants.local.sh 覆盖）。

在这里 import 一个能力模块 = 让它参与注册。**新增能力：写一个模块 + 在此 import。**
删/停用能力：注掉 import，或设 CAP_<NAME>_ENABLED=0（推荐后者，保留代码）。
"""

# 顺序不影响分发（分发按 Capability.priority），只影响注册日志顺序。
from custom.capabilities import text_reply  # noqa: F401  文本回复（brain→replier）
from custom.capabilities import forward     # noqa: F401  合并转发（chatRecord）
from custom.capabilities import image       # noqa: F401  图片识别（vision 兜底）
from custom.capabilities import question    # noqa: F401  Question 交互（钉钉端答 agent 提问）

__all__ = ["text_reply", "forward", "image", "question"]
