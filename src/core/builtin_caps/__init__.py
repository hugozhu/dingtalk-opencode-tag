"""builtin_caps — core 自带的通用能力原语（平台无关，可直接复用）

这些能力**零平台耦合**（只依赖 core.brain / core.replier / core.capabilities），作为
harness 开箱即用的基础能力放在 core：
  - text_reply  文本回复：收到文本 → 大脑生成 → 发回来源会话（brain→replier 默认骨架）
  - question    Question 交互：agent 调 question 工具 → 渲染发群 → 群里作答 → 提交回 serve
  - aggregation 群消息聚合：时间窗缓冲 → 到点合并成一次摘要回复

**core 不自动注册**这些能力（保持 core 被动）。由 custom 层决定启用哪些：
`custom/capabilities/__init__.py` 里 import 对应模块即注册（同 custom 能力一样，受各自
CAP_<NAME>_ENABLED 开关控制）。这样 FDE 仍能选配/关闭，且能干净 merge upstream。
"""
