# Agent Harness — 数字员工项目脚手架

提炼自 `dingtalk-opencode-agent`（4.1+ 版本），把生产环境打磨过的 13 个最佳实践抽象成可复用模板，为新的数字员工项目做基础。

当前版本见 [VERSION](./VERSION)（单一真相源；引用时请以该文件为准，勿在文档里硬编码）。

## 为什么需要这个 harness

从零搭一个"长连接数字员工 + 事件流监听 + 多模态处理"项目，要解决：

- 进程怎么不挂（守护 + 自愈 + 熔断）
- 长连接断了怎么自动重连（SSE 退避 + 端口切换）
- 多个 handler 怎么不重复写"找会话→注入→取回复"模板
- 业务消息处理后怎么清理 spurious 多余轮次
- 测试怎么不依赖网络/钉钉跑通

这些坑 `dingtalk-opencode-agent` 都踩过、修过、写测试覆盖了。harness 把它们提炼出来，**去掉业务特定代码**（钉钉 API、合并转发、question 交互），**保留可复用结构**。

## 三层目录架构

FDE 在交付时通过 **core / custom / templates 三层物理隔离** 实现"改得动 + merge 得回"：

| 层 | 路径 | FDE 能否改 | merge 回 upstream |
|----|------|-----------|------------------|
| **core** | `src/core/` `bin/core/` `tests/core/` | ❌ 不改 | ✅ bug fix 贡献回 upstream |
| **custom** | `src/custom/` `bin/custom/` `tests/custom/` | ✅ 在这里改 | ❌ 业务特定，不 merge |
| **templates** | `src/templates/` | ❌ 不改（diff 基线） | ✅ upstream 演进 |
| **config** | `config/*.local.*` | ✅ 填真实值 | ❌ gitignored |

详见 [FORKING.md](./FORKING.md)（FDE 派生指南）和 [CONTRIBUTING.md](./CONTRIBUTING.md)（贡献回 upstream 流程）。

## 快速开始

```bash
# 1. 复制 harness 到新项目
cp -r . /path/to/my-agent/
cd /path/to/my-agent/

# 2. 改配置
cp config/config.example.json config/config.local.json
cp config/constants.sh config/constants.local.sh
# 编辑 *.local.* 填入真实值（被 .gitignore 忽略）

# 3. 实现业务 handler（编辑 src/custom/handler.py）
#   - 改 _classify_message 分类逻辑
#   - 改 render_prompt 末句 prompt
#   - 改 _predicate 匹配自己业务消息特征
#   - 改 make_reply_msgs 通知消息格式

# 4. 注册业务路由（编辑 src/custom/routes.py，不要改 src/core/event_watcher.py）
#   - 在 route_reply 里实现文本回复分发
#   - 在 route_business_line 里扩展业务消息检测

# 4b. 实现组件启动命令（编辑 bin/custom/start_funcs.sh）
#   - 必须实现 start_connect（数字员工核心连接进程的真实命令）
#   - 可选覆盖 start_watcher；start_event_watcher 已有 core 默认实现

# 5. 装 launchd agent（macOS）
cp bin/custom/agent-template.plist ~/Library/LaunchAgents/com.<your-org>.<your-agent>.plist
# 编辑 plist：Label / ProgramArguments / StandardOutPath / PATH
launchctl load -w ~/Library/LaunchAgents/com.<your-org>.<your-agent>.plist

# 6. 跑测试
bash tests/core/unit_test.sh                 # shell 单测
python3 tests/core/test_agent_common.py      # Python 单测
bash tests/custom/e2e_test.sh                 # 端到端（需要真实链路）
```

## 文件结构

```
.
├── VERSION                              ← 语义化版本（FDE 派生时记录基线）
├── README.md                            ← 本文件
├── ARCHITECTURE.md                      ← 架构图 + 13 个最佳实践提炼
├── AGENTS.md                            ← 给 AI agent 看的项目说明 + 边界
├── FORKING.md                           ← FDE 派生指南（哪些改/哪些不改/同步流程）
├── CONTRIBUTING.md                       ← 贡献回 upstream 的流程
├── LICENSE
│
├── src/
│   ├── core/                            ← @core harness 核心（DO NOT EDIT）
│   │   ├── __init__.py
│   │   ├── agent_common.py              ← 共享工具（日志/通知/serve 访问/inject_and_forward）
│   │   └── event_watcher.py              ← 事件流监听主进程（SSE 重连 + log-tail + 状态机，调用 custom.routes）
│   ├── custom/                          ← @custom FDE 改这里
│   │   ├── __init__.py
│   │   ├── handler.py                   ← 业务 handler（从 templates/handler_template.py 复制改造）
│   │   └── routes.py                    ← 业务路由注册表（route_reply / route_business_line / route_sse_event）
│   └── templates/                       ← @template 纯净参考（diff 基线，DO NOT EDIT）
│       ├── __init__.py
│       └── handler_template.py          ← 完整 handler 范例
│
├── bin/
│   ├── core/                            ← @core 守护/健康检查（DO NOT EDIT）
│   │   ├── lib.sh                       ← 共享 shell 工具（verify_pid / acquire_lock / log）
│   │   ├── monitor.sh                   ← 守护进程（cleanup_stale_state + start_all + 主循环 + 熔断）
│   │   ├── healthcheck.sh               ← 6 项健康检查（硬失败/告警分级）
│   │   └── reboot.sh                    ← /reboot 指令执行体（带退避重试 + 告警）
│   └── custom/                          ← @custom FDE 改这里
│       └── agent-template.plist          ← launchd plist 模板
│
├── tests/
│   ├── core/                            ← @core 核心测试（DO NOT EDIT）
│   │   ├── unit_test.sh                 ← shell 单测（bash -n + 函数断言）
│   │   └── test_agent_common.py         ← Python 单测（unittest + patch.object）
│   └── custom/                          ← @custom FDE 改这里
│       └── e2e_test.sh                  ← 端到端测试（触发 + 日志 + dws list 验证）
│
└── config/                              ← @config
    ├── config.example.json              ← 配置示例（占位符）
    ├── constants.sh                     ← 可配置常量模板（占位符）
    ├── config.local.json                ← 真实凭据（gitignored，FDE 填）
    └── constants.local.sh               ← 真实常量（gitignored，FDE 填）
```

## 核心设计原则

1. **launchd 托管，不依赖应用持续在线**——开机/登录自启，应用崩溃 launchd 拉起
2. **进程检测 PID 文件 + cmdline 签名**——绕开 pgrep -f 误匹配（send-by-bot 转发进程）
3. **渲染/IO 分层**——fetch_xxx（I/O 集中）vs render_xxx（纯函数零 I/O），独立测试
4. **公共注入模板**——inject_and_forward 被 N 个 handler 共用，差异点用 callable 参数化
5. **批量反查 + 轮询等待**——一次 list-by-ids 批量取回 N 个字段，比逐个快且不依赖额外权限
6. **状态机 cleanup**——awaiting_spurious → cleaning，处理多选/连发场景下的 spurious 多余轮次
7. **诊断日志**——数量不匹配时记 raw 输入头 N 字符，便于排查外部 API 格式变化
8. **三层物理隔离 + 插件化路由**——core / custom / templates 分层，FDE 只改 custom + routes.py，core 路径在 upstream/fork 一致便于干净 merge

详细架构图 + 13 个最佳实践提炼见 [ARCHITECTURE.md](./ARCHITECTURE.md)。

## 已知限制

- **空回复撤回**：依赖服务（如 dws dev connect）触发 abort 后返回空 finalizer 被发到通知渠道时，event-watcher 无法用机器人身份撤回（钉钉 API "仅消息发送者可撤回" + 缺 processQueryKey）。需要依赖服务端配合过滤空回复。
- **macOS 限定**：launchd 托管是 macOS 特性。Linux 用 systemd、Windows 用服务/任务计划器，需自己适配 `bin/core/monitor.sh`。
- **依赖 dws CLI**：send_notification / _run_cli 都用 dws。其他平台需在 custom 层替换为对应 SDK。

## License

MIT
