# FORKING.md — FDE 派生交付指南

本文件面向 **FDE（现场交付工程师）**，说明如何基于本 harness 派生一个交付工程，以及交付过程中哪些能改、哪些不能改。

## 三层目录边界

| 层 | 路径 | FDE 能否改 | 是否 merge 回 upstream |
|----|------|-----------|----------------------|
| **core** | `src/core/` `bin/core/` `tests/core/` | ❌ 不要改 | ✅ bug fix 贡献回 upstream |
| **custom** | `src/custom/` `bin/custom/` `tests/custom/` | ✅ 在这里改 | ❌ 业务特定，不 merge |
| **templates** | `src/templates/` | ❌ 不要改（仅作 diff 参考） | ✅ upstream 演进 |
| **config** | `config/` | ✅ 改 `*.local.*` | ❌ local 文件 gitignored |

**铁律**：FDE 只在 `custom/` 和 `config/*.local.*` 里改代码。core 的 bug 修了请走 PR 贡献回 upstream（见 [CONTRIBUTING.md](./CONTRIBUTING.md)）。

## 派生流程（Step by Step）

### Step 1: 复制 harness

```bash
cp -r /path/to/dingtalk-opencode-tag /path/to/my-delivery/
cd /path/to/my-delivery/
git init && git add -A && git commit -m "init from harness $(cat VERSION)"
```

记录基线版本（写在 git commit message 里），方便后续同步 upstream 修复时对账。

### Step 2: 填配置

```bash
cp config/config.example.json config/config.local.json
cp config/constants.sh config/constants.local.sh
# 编辑 *.local.* 填入真实 robot_code / user_id / profile / 路径 / proxy_key
```

`config.local.json` 和 `constants.local.sh` 已被 `.gitignore` 忽略，填真实凭据不会误提交。

### Step 3: 实现业务 handler

编辑 `src/custom/handler.py`（初始内容复制自 `src/templates/handler_template.py`）。按顺序改：

1. **常量正则**（顶部）：
   - `BUSINESS_MSG_RE` / `MSGID_RE` — 匹配你业务消息的日志格式
   - `_RE_MEDIA_ID` / `_RE_FILE_ID` — 匹配附件 ID 格式

2. **`_classify_message`**：你的业务消息有几种类型？图片/文件/文本/语音/视频？

3. **`fetch_attachments` + `_fetch_image_entry` + `_fetch_file_entry`**：实现自己的附件下载逻辑（替换 dws CLI 调用为对应平台 SDK）。图片识别 prompt 在 `agent_common._proxy_vision` 末句，按业务调整。

4. **`render_prompt` 末句**：默认是 "请基于上述消息内容回应用户。"，改成符合你业务场景的 prompt。

5. **`_predicate`**（在 `handle_message` 的 cleanup 轮询里）：替换 `'msgtype="business-special"'` 为你的业务消息特征字符串。

6. **`make_reply_msgs`**（在 `inject_and_forward` 调用里）：构造自己的通知消息格式。注意：reply 不要被 `_md` 的 `**...**` 包裹。

### Step 4: 注册业务路由

编辑 `src/custom/routes.py`（**不要改 `src/core/event_watcher.py`**）：

```python
def route_reply(user, text, conv_type, raw_line):
    # 在这里实现文本回复的分发
    if text == "[图片]":
        threading.Thread(target=handle_image, args=(time.time(),), daemon=True).start()
    elif match_business_line(raw_line):
        mid, convs = match_business_line(raw_line)
        threading.Thread(target=handle_message, args=(mid, convs), daemon=True).start()
    else:
        handle_reply(user, text)

def route_business_line(line):
    # 默认实现已处理合并转发，按需扩展
    ...
```

`core/event_watcher.py` 的 `log_tail_thread` 会调用这两个函数，FDE 不需要碰 core。

### Step 5: 替换通知后端

`src/core/agent_common.py` 的 `send_notification` 默认用 dws CLI 发钉钉消息。如需替换为 Slack / 企业微信 / 飞书 / 邮件，**不要直接改 core**——在 `src/custom/routes.py` 里覆盖 `send_notification`，或在 `custom/handler.py` 里定义自己的通知函数。

如果通知后端的 bug 在 core 的 `send_notification` 里（如 dws CLI 调用参数错误），请走 PR 贡献回 upstream。

### Step 6: 装 launchd agent

```bash
cp bin/custom/agent-template.plist ~/Library/LaunchAgents/com.<your-org>.<your-agent>.plist
# 编辑 plist：Label / ProgramArguments / StandardOutPath / PATH
launchctl load -w ~/Library/LaunchAgents/com.<your-org>.<your-agent>.plist
```

### Step 7: 跑测试

```bash
bash tests/core/unit_test.sh             # shell 单测（core，不改）
python3 tests/core/test_agent_common.py  # Python 单测（core，不改）
bash tests/custom/e2e_test.sh            # 端到端（按业务调整，需要真实链路）
```

## 同步 upstream 修复

当 upstream（本 harness）发布了 core 的修复，FDE 想同步到交付工程：

```bash
cd /path/to/my-delivery/
git remote add upstream https://github.com/hugozhu/dingtalk-opencode-tag.git
git fetch upstream
# 只合并 core 层（custom 不受影响）
git merge upstream/main -- src/core/ bin/core/ tests/core/ src/templates/
# 解决冲突（通常无冲突，因为 FDE 没改 core）
git commit -m "sync core from upstream $(cat upstream/VERSION 2>/dev/null)"
```

因为 core 路径在 upstream 和 fork 里完全一致，merge 只会带入 core 的变更，**不会污染 custom 的业务定制**。

## 对比 upstream 新的最佳实践

upstream 升级 `src/templates/handler_template.py` 后，FDE 可 diff 看变化，决定要不要 apply 到自己的 `custom/handler.py`：

```bash
diff src/templates/handler_template.py src/custom/handler.py
```

templates 保持纯净，是 FDE diff 的稳定基线。

## 已知限制

- **空回复撤回**：依赖服务触发 abort 后返回空 finalizer 被发到通知渠道时，event-watcher 无法用机器人身份撤回（钉钉 API "仅消息发送者可撤回" + 缺 processQueryKey）。需要依赖服务端配合过滤空回复。
- **macOS 限定**：launchd 托管是 macOS 特性。Linux 用 systemd、Windows 用服务/任务计划器，需自己适配 `bin/core/monitor.sh`。
- **依赖 dws CLI**：`send_notification` / `_run_cli` 默认用 dws。其他平台在 custom 层覆盖。
