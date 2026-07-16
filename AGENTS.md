# AGENTS.md — 给 agent 看的项目说明

## 项目用途

这是一个**数字员工项目 harness 模板**（FDE 交付 AI 项目的基础工程），从 `dingtalk-opencode-agent`（生产环境打磨 4.1+ 版本）提炼出 13 个可复用最佳实践，为新项目做基础。

**三层目录架构**（AI agent 修改代码前必须识别边界）：
- `src/core/` `bin/core/` `tests/core/` — **@core harness 核心，不要改**，bug fix 走 PR 贡献回 upstream
- `src/custom/` `bin/custom/` `tests/custom/` — **@custom FDE 在这里改**，业务特定，不 merge 回 upstream
- `src/templates/` — **@template 纯净参考**，FDE 的 diff 基线，不要改
- `config/*.local.*` — **@config 真实凭据**，被 .gitignore 忽略

详细派生 + 同步 + 贡献流程见 [FORKING.md](./FORKING.md) 和 [CONTRIBUTING.md](./CONTRIBUTING.md)。

## 如何基于本 harness 启动新项目

### Step 1: 复制 + 配置

```bash
cp -r . /path/to/my-agent/
cd /path/to/my-agent/
cp config/config.example.json config/config.local.json
cp config/constants.sh config/constants.local.sh
# 编辑 *.local.* 填入真实身份/路径
```

### Step 2: 实现业务 handler

打开 `src/custom/handler.py`（初始复制自 `src/templates/handler_template.py`），按以下顺序修改：

1. **改常量正则**（顶部）:
   - `BUSINESS_MSG_RE` / `MSGID_RE` — 匹配你自己业务消息的日志格式
   - `_RE_MEDIA_ID` / `_RE_FILE_ID` — 匹配附件 ID 的格式

2. **改 `_classify_message`**:
   - 你的业务消息有几种类型？图片/文件/文本/语音/视频？调整分类逻辑

3. **改 `fetch_attachments` + `_fetch_image_entry` + `_fetch_file_entry`**:
   - 实现自己的附件下载逻辑（替换 dws CLI 调用为对应平台 SDK）
   - 图片识别 prompt 在 `core.agent_common._proxy_vision` 末句，按业务调整

4. **改 `render_prompt` 末句**:
   - 默认是 "请基于上述消息内容回应用户。"
   - 改成符合你业务场景的 prompt

5. **改 `_predicate`**:
   - 在 `handle_message` 的 cleanup 轮询里，`_predicate(msg)` 用来识别依赖服务转发的原始消息
   - 替换 `'msgtype="business-special"'` 为你的业务消息特征字符串

6. **改 `make_reply_msgs`**:
   - 在 `inject_and_forward` 调用里，构造自己的通知消息格式（标题 + 正文）
   - 注意：reply 不要被 `_md` 的 `**...**` 包裹（避免 agent 返回 `## 标题` 时变成 `**## 标题**`）

### Step 3: 注册业务路由

打开 `src/custom/routes.py`（**不要改 `src/core/event_watcher.py`**），在 hook 函数里注册自己的业务分发：

```python
def route_reply(user, text, conv_type, raw_line):
    # 文本回复路由：图片/语音/自定义指令/默认回复
    if text == "[图片]":
        threading.Thread(target=handle_image, args=(time.time(),), daemon=True).start()
    elif match_business_line(raw_line):
        mid, convs = match_business_line(raw_line)
        threading.Thread(target=handle_message, args=(mid, convs), daemon=True).start()
    else:
        handle_reply(user, text)

def route_business_line(line):
    # 默认已处理合并转发，按需扩展其他业务消息格式
    ...
```

`core/event_watcher.py` 的 `log_tail_thread` / `format_and_forward` 会调用这些 hook：
- `route_reply(user, text, conv_type, raw_line)` — 普通文本回复（已排除 /reboot）
- `route_business_line(line)` — 业务消息行
- `route_sse_event(event, port, password)` — SSE 事件（可选拦截，返回 False 走 core 默认转发）
- `route_cleanup_state(event, cleanup_state, cleanup_lock)` — spurious 多余轮次 cleanup 状态机（core 只做 TTL 兜底，状态机在 custom 实现）

### Step 4: 替换通知后端（可选）

`core/agent_common.py` 的 `send_notification` 默认用 dws CLI 发钉钉消息。如需替换为 Slack / 企业微信 / 飞书 / 邮件，在 `src/custom/routes.py` 里覆盖，或自定义通知函数。**不要直接改 core**。

### Step 5: 装 launchd agent

```bash
cp bin/custom/agent-template.plist ~/Library/LaunchAgents/com.<your-org>.<your-agent>.plist
# 编辑 plist 中的 Label / ProgramArguments / StandardOutPath / PATH
launchctl load -w ~/Library/LaunchAgents/com.<your-org>.<your-agent>.plist
```

### Step 6: 跑测试

```bash
bash tests/core/unit_test.sh                 # shell 单测（core，不改）
python3 tests/core/test_agent_common.py      # Python 单测（core，不改）
bash tests/custom/e2e_test.sh                 # 端到端（需要真实链路）
```

## 关键文件 / 函数索引

| 文件 | 层 | 关键函数 | 用途 |
|------|----|---------|------|
| `bin/core/monitor.sh` | @core | `cleanup_stale_state` / `start_all` / `run_forever` | 守护循环 + 熔断 |
| `bin/core/healthcheck.sh` | @core | `check_connect` / `check_serve_http` | 6 项健康检查 |
| `bin/core/lib.sh` | @core | `verify_pid` / `acquire_lock` | 共享 shell 工具 |
| `bin/core/reboot.sh` | @core | (脚本本身) | /reboot 指令执行体 |
| `bin/custom/agent-template.plist` | @custom | (plist 本身) | launchd 配置模板 |
| `src/core/agent_common.py` | @core | `inject_and_forward` / `_abort_and_clean_session` / `find_serve_credentials` / `_find_session_with_predicate` | 共享 Python 工具 |
| `src/core/event_watcher.py` | @core | `connect_sse` / `log_tail_thread` / `format_and_forward` | 事件流主进程（调用 custom.routes 的 hook） |
| `src/custom/handler.py` | @custom | `handle_message` / `fetch_attachments` / `render_prompt` / `_lookup_senders_batch` | 业务 handler（FDE 改这里） |
| `src/custom/routes.py` | @custom | `route_reply` / `route_business_line` / `route_sse_event` | 业务路由注册（FDE 改这里，不改 core） |
| `src/templates/handler_template.py` | @template | (同 custom/handler.py 的纯净版) | diff 基线，不要改 |
| `tests/core/test_agent_common.py` | @core | `TestInjectAndForward` / `TestAbortAndCleanSession` / `TestFindSessionWithPredicate` | Python 单测 |
| `tests/core/unit_test.sh` | @core | (脚本本身) | shell 单测 |
| `tests/custom/e2e_test.sh` | @custom | (脚本本身) | 端到端测试（FDE 改这里） |

## 常见坑

1. **`_find_bot_session` 按 time.updated 倒序选最新**，不是按 id 字典序——多个 session 共享 directory 时，id 字典序最大不等于最新活跃
2. **asked_ts buffer 设 5s**——依赖服务写日志时刻 vs serve POST 时刻有微小偏差
3. **轮询 do-while 风格**（先调一次再判断）——保证至少调一次，避免常量 patch 为 0 时跳过整个循环
4. **patch.object 第三参数是 `new` 不是 `return_value`**——指定 new 后不传 mock 给测试函数，测试函数不该有对应参数
5. **reply 不要被 `_md` 的 `**...**` 包裹**——避免 agent 返回 `## 标题` 时变成 `**## 标题**`；直接把 reply 作为通知正文
6. **空回复撤回受限**——依赖服务触发 abort 后返回空 finalizer 被发到通知渠道时，event-watcher 无法用机器人身份撤回（钉钉 API "仅消息发送者可撤回" + 缺 processQueryKey）。需要依赖服务端配合过滤空回复。
7. **不要改 core 改 routes**——业务路由注册到 `src/custom/routes.py`，**绝不改 `src/core/event_watcher.py`**。core 的路径在 upstream 和 fork 里必须一致才能干净 merge。

## 测试约定

- shell 单测：`bash -n` 语法检查 + 函数级断言，不依赖网络/钉钉/serve
- Python 单测：`unittest` + `patch.object(<module>, "<func>", return_value=...)`
- 业务 handler 测试：mock `inject_and_forward` 验证 prompt 拼装 + 调用回调，不测模板内部
- e2e：实际触发 + 监控日志 + `dws chat message list` 验证消息流

## 不要做的事

- **不要**改 `src/core/` `bin/core/` `tests/core/` 下的任何文件——bug fix 走 PR 贡献回 upstream（见 [CONTRIBUTING.md](./CONTRIBUTING.md)）
- **不要**用 `pgrep -f` 检测进程（会误匹配 send-by-bot 转发进程）——用 `verify_pid`
- **不要**按 session id 字典序选最新——用 `time.updated` 倒序
- **不要**在 handle_message 一开头就 cleanup——依赖服务可能延迟转发，应轮询等待
- **不要**把 abort 触发的空 finalizer 留在 session history——用 `_abort_and_clean_session` 清理
- **不要**用 `flock`（macOS 不可用）——用 `shlock` 或文件存在性判断
- **不要**用 `tee -a file >&2`（launchd 已重定向 stderr 到同一文件，会双写）——`log()` 只写 stderr
- **不要**把真实凭据写入 `config/config.example.json` 或 `config/constants.sh`——只填 `*.local.*`（已 gitignore）
