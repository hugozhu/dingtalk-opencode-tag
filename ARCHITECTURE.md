# Architecture — 数字员工项目架构

## 整体架构图

```
launchd agent (com.example.agent-connect)
  └── monitor.sh --foreground (守护进程，KeepAlive={SuccessfulExit:false} + RunAtLoad=true)
        ├── cleanup_stale_state: 启动时清理失效的 PID 文件
        ├── start_all: 拉起 3 个组件（nohup+disown，脱离进程树独立存活；已有同种进程则跳过）
        │     ├── connect 进程（你的数字员工核心连接，如 dws dev connect / 自定义 bridge）
        │     ├── serve-watcher.sh（监控 connect 日志，发首次启动通知）
        │     └── event-watcher.py（SSE 事件流监听 + log-tail 业务路由 + 状态机 cleanup）
        ├── warmup: 触发依赖服务首次启动 + 提取凭据（pid/port/pwd → .serve.* 文件）
        └── 主循环：每 CHECK_INTERVAL 秒 → healthcheck.sh
              ├── 健康 → 重置失败计数 + 兜底拉起 watchers（30 分钟内自愈）
              └── 不健康 → 累加失败计数；未达熔断阈值 → 全量重启
                    └── 连续 MAX_FAILURES 次失败 → 熔断：notify_alert + exit 0
                          （exit 0 → launchd 不再拉起，等人工；
                           崩溃 exit 非零 / SIGTERM cleanup exit 1 → launchd 自动恢复）

healthcheck.sh (monitor 调用) — 7 项检查（5 硬 + 2 告警）
  ├── 检查1: connect 进程存活（PID 文件 + kill -0 + cmdline 签名，硬失败）
  ├── 检查2: 日志活跃度（35 分钟内有活动，仅告警）
  ├── 检查3: 日志尾部致命错误（grep FATAL/panic，硬失败）
  ├── 检查4: event-watcher 进程活跃（仅告警）
  ├── 检查5: serve 进程存活（PID + kill -0，硬失败）
  └── 检查6: serve HTTP /session 响应（凭据自刷新，硬失败）
  └── 检查7: 大脑真实自检（失败计数超阈值才真发一次模型调用，硬失败）

event-watcher.py (3 个独立线程 + 主线程)
  ├── 主线程: connect_sse() — 无限重试 + 退避（3s→30s）连 opencode serve /event
  │     └── 收到 SSE 事件 → format_and_forward() → send_notification
  ├── log_tail_thread: tail connect 日志
  │     ├── 解析 "[connect] 收到 @user: text (convType=...)" 行
  │     ├── 跨行格式状态机 + 线程安全 dedup
  │     ├── 路由到业务 handler（handle_reply / handle_image / handle_forward）
  │     ├── /reboot 指令 → 派生 reboot.sh + os._exit(0)
  │     └── 撤回空回复（监听 "agent 已生成回复" + "普通消息已发送" → recall）
  └── 状态机 cleanup_state: 处理 spurious 多余轮次
        ├── awaiting_spurious → 等多余消息出现（原轮次事件正常放行）
        ├── cleaning → DELETE + abort 多余 user/assistant 消息
        └── idle 时比较 deleted_user vs expected_count，未达标重置 awaiting_spurious

agent_common.py — 共享 Python 工具（被 event-watcher + handler 共用）
  ├── 常量（ROBOT_CODE/USER_ID/PROFILE/PROXY_URL/VISION_MODEL）
  ├── log / send_notification / _md
  ├── _run_cli (dws CLI 包装)
  ├── find_serve_credentials (进程表 + 环境变量提取)
  ├── serve_request (opencode serve HTTP 唯一出口：凭据 + Basic auth + 调试日志)
  ├── _clean_session_title (session title 归一：折行 + 限长)
  └── _proxy_vision (多模态识别，逐字提取原文不总结)

  注：会话的建/删/复用**不在这里**——那是后端策略，由 custom/brain.py 按 conv 维护
      （见最佳实践 8）。core 只提供凭据发现与 HTTP 出口。

handler.py — 业务 handler（FDE 在 src/custom/handler.py 改造，模板在 src/templates/handler_template.py）
  ├── match_business_line (log-tail 调用，跨行检测 + 线程安全 dedup)
  ├── fetch_attachments (I/O 阶段：图片下载+vision / 文件下载+读正文)
  ├── render_prompt (纯函数零 I/O，单测无需 mock)
  ├── _lookup_senders_batch (一次 list-by-ids 批量反查 N 个 sender)
  └── _fetch_senders (补齐缺失 sender，DingTalk summary 不完整时兜底)

  注：只管「消息 → prompt」；生成回复由能力调 core.brain.generate_reply
```

## 13 个最佳实践提炼

### 1. launchd 守护（KeepAlive + RunAtLoad + 熔断）
**文件**: `bin/core/monitor.sh` + `bin/custom/agent-template.plist`
**关键设计**:
- `KeepAlive={SuccessfulExit:false}` — exit 0 不拉起（等人工），崩溃/被杀会拉起
- `RunAtLoad=true` — 开机/登录自启
- `ThrottleInterval=10` — 10 秒内不重复拉起（防崩溃循环）
- 连续 `MAX_FAILURES` 次失败 → 熔断：发告警 + exit 0
- SIGTERM/SIGINT → cleanup exit 1（非零让 launchd 拉起，覆盖系统重启场景）

### 2. N 项健康检查（硬失败/告警分级）
**文件**: `bin/core/healthcheck.sh`
**关键设计**:
- 硬失败（进程死、HTTP 无响应）→ 不健康，触发全量重启
- 仅告警（日志活跃度、非关键子组件）→ 不健康，记日志但不触发重启
- 输出 JSON: `{healthy, message, checks: {...}}`
- serve 凭据失效时从进程表 + 日志刷新

### 3. verify_pid（PID 文件 + kill -0 + cmdline 签名 + pgrep 兜底）
**文件**: `bin/core/lib.sh`
**关键设计**:
- PID 文件 + kill -0 检测进程存活
- cmdline 签名校验防 PID 复用（进程死了，新进程复用同 PID）
- pgrep -fi ^锚定兜底（PID 文件丢失时仍能检测，^锚定排除 send-by-bot 转发进程）
- 抽到 lib.sh 共享给 monitor + healthcheck，消除两处逻辑漂移

### 4. cleanup_stale_state + 去重拉起
**文件**: `bin/core/monitor.sh`
**关键设计**:
- 启动时对每个组件 PID 文件做失效检测（PID 死/被复用 → 删除）
- start_* 前 is_running 检查，已有则跳过（避免重复拉起两个 watcher 并行）
- 主循环健康通过后兜底拉起 watchers（30 分钟内自愈）

### 5. SSE 事件流重连 + 退避 + 端口切换
**文件**: `src/core/event_watcher.py` 的 `connect_sse()`
**关键设计**:
- 无限重试 + 退避（`MIN_RECONNECT_INTERVAL`→`MAX_RECONNECT_INTERVAL`，3s→30s）
- serve 未启动时等待而非退出（interval 累加但不超过 MAX）
- 端口变更自动切换（凭据每次实时从 `find_serve_credentials()` 获取）
- 启动顺序不再敏感——event-watcher 比 serve 先启动也能自动恢复

### 6. log-tail 监听 + 跨行状态机 + 线程安全 dedup
**文件**: `src/core/event_watcher.py` 的 `log_tail_thread()`
**关键设计**:
- `tail -F` + inode 检测轮转（文件 inode 变化或变小则重开）
- 跨行格式检测状态机（行 1 含 msgtype，行 2 含 msgId → 用 `_pending_cross_line` 暂存）
- 线程安全 dedup（`_seen` 集合 + `_state_lock`）

### 7. 状态机 cleanup（awaiting_spurious → cleaning）
**文件**: core `src/core/event_watcher.py` 提供 TTL 兜底 + `route_cleanup_state` hook；
状态机实现在 custom `src/custom/routes.py`（业务特定，FDE 可编辑）
**关键设计**:
- core 只负责 TTL 过期兜底防僵死（`CLEANUP_TTL=40s`）+ 调用 hook
- awaiting_spurious → 等多余消息出现（原轮次事件正常放行）
- cleaning → DELETE + abort 多余 user/assistant 消息
- 多选/连发场景下累积删除 `expected_count` 条
- idle 时不立即 pop：比较 `deleted_user` vs `expected_count`，未达标重置 awaiting_spurious

### 8. 会话复用：按 conversation，不按目录
**文件**: `src/custom/brain.py` 的 `_conv_sessions` / `_http_reuse()`
**关键设计**:
- **粒度是 conv_id**（DingTalk 的 openConversationId），不是工作目录——一个目录下会有
  上百个 session，"最近活跃的那个"未必属于当前这场对话，按目录复用会串上下文
- `_conv_sessions` 是 `OrderedDict`：TTL 过期（`AGENT_SESSION_TTL`）+ LRU 逐出
  （`AGENT_SESSION_MAX`，被挤掉的会话连远端一起删）
- `_conv_lock(conv_id)` 单会话串行，不同 conv 并行不受影响
- 自愈两路：复用的 sid 404（serve 重启/GC）→ 重建一次重试；复用的 sid 回空
  （疑似后端 stream-error 把回合 completed 成空）→ 丢弃 + 删远端 + 新建重试
- 重置关键词（`AGENT_SESSION_RESET_KEYWORDS`）让用户主动开新话题
- `_inflight` 登记在跑任务（conv_id → sid/title/started），供 `/cancel` 直接 abort
  和 `/reboot` 通知展示"这一停会打断什么"；经 `core.brain.register_inflight` 注册给 core
- **所有能力共用这一套**：文本回复、合并转发、图片、文件都调
  `core.brain.generate_reply(ctx={"conv_id": ...})`，所以转发完再追问能接上上下文

### 9. serve 访问收口
**文件**: `src/core/agent_common.py`
**关键设计**:
- `find_serve_credentials` — 优先读 `.serve.port`/`.serve.pwd`，回退进程表扫描；带短 TTL 缓存
- `serve_request` — opencode serve HTTP 的**唯一出口**：凭据、Basic auth、可开关的
  REQ/RESP 调试日志集中一处，避免模板在各处被拷贝出漂移

### 10. 渲染/IO 分层
**文件**: `src/templates/handler_template.py`（FDE 复制到 `src/custom/handler.py` 改造）
**关键设计**:
- `fetch_attachments` — I/O 集中（图片下载+vision / 文件下载+读正文）
- `render_prompt` — 纯函数零 I/O，单测无需 mock
- `build_prompt` 兼容 wrapper 串联两者（保留旧 API）

### 11. 批量反查 + 轮询等待
**文件**: `src/templates/handler_template.py`（FDE 复制到 `src/custom/handler.py` 改造）
**关键设计**:
- `_lookup_senders_batch(msg_ids)` — 一次 list-by-ids 批量反查 N 个 sender
  - 比 list --group 鲁棒（不依赖群权限）
  - 比逐个查询快（一次调用取回所有缺失 msgId 的 sender）
- 轮询等待依赖服务转发完成：
  - `POLL_MAX_SECONDS` / `POLL_INTERVAL` 可配置常量
  - 测试 patch 为 0 避免真实 sleep
  - do-while 风格保证至少调一次

### 12. 测试结构（shell + python + e2e）
**文件**: `tests/`（core 测试在 `tests/core/`，custom 测试在 `tests/custom/`）
**关键设计**:
- shell 单测：`bash -n` 语法检查 + 函数级断言（不依赖网络/钉钉）
- Python 单测：`unittest` + `patch.object(<module>, "<func>")`
- e2e 测试：实际触发 + 日志验证 + `dws chat message list` 验证消息流
- 测试 patch 轮询常量为 0 避免真实 sleep

### 13. 诊断日志（数量不匹配时记 raw 头 N 字符）
**文件**: `src/custom/handler.py` 的 `_fetch_senders()`（模板版在 `src/templates/handler_template.py`）
**关键设计**:
- summary 行数 vs messages 数量不一致时，记 raw content 头 300 字符
- 便于排查外部 API 格式变化（如 DingTalk summary 从 `sender:content` 格式改成 AI 总结性描述）
- 失败/成功/命中分别记日志
- timestamp + 组件前缀

## 关键状态文件

| 文件 | 用途 |
|------|------|
| `.connect.pid` | connect 进程 PID |
| `.serve.pid` | opencode serve 进程 PID |
| `.serve.port` | opencode serve 监听端口 |
| `.serve.pwd` | opencode serve 的 OPENCODE_SERVER_PASSWORD |
| `.watcher.pid` | serve-watcher 后台进程 PID |
| `.event-watcher.pid` | event-watcher 后台进程 PID |
| `/tmp/agent-monitor.lock` | monitor 守护进程单实例锁（含 PID） |
| `.next-check` | 下次自检的 Unix 时间戳 |
| `.opencode-connect-status.json` | 最近一次健康检查结果 |

均运行时文件，不提交 git。
