# ego-lite 项目分析报告

## 项目概览

**ego-lite** 是一个专为 AI agents 设计的 Chromium 浏览器自动化工具，由 citrolabs 开发。

- **GitHub**: https://github.com/citrolabs/ego-lite
- **License**: MIT
- **Platform**: macOS (Windows/Linux 在路线图中)
- **Version**: 1.2.6

## 核心价值主张

### 1. 人机协作浏览器
- 用户和 AI agent 在同一个浏览器中并行工作
- Agent 在独立的 **Space** 中运行，不干扰用户的标签页
- 继承用户的登录状态，无需额外登录

### 2. 代码优先而非 CLI 优先
- Agent 通过 JavaScript 函数直接操作浏览器（而非传统的 CLI 命令循环）
- 复杂任务速度提升 2.5 倍，token 消耗更少
- 任务成功率更高

### 3. 多任务并行
- 每个 Space 可以运行独立的任务
- 例如：10 个 Space 同时丰富 10 个潜在客户信息
- 不同 Agent 可以同时在不同 Space 工作

## 技术架构

### 核心组件

```
ego-browser (Node.js runtime)
    ↓
globalThis.ego (browser bindings)
    ↓
Chrome DevTools Protocol (CDP)
    ↓
ego lite browser (Chromium-based)
```

### 关键特性

1. **Task Spaces（任务空间）**
   - 独立的浏览上下文
   - 继承用户的登录状态
   - 支持所有权管理（agent/user/delegated）

2. **Snapshot 系统**
   - 高质量的页面快照
   - 支持深层嵌套 iframe
   - 提供语义化的页面树结构
   - 带有 `@N` 引用和 `loc=...` 定位器

3. **三种工作流程**
   - **语义流程**: `snapshotText()` + refs/locators（常规网页）
   - **视觉流程**: `captureScreenshot()` + 坐标/键盘操作（Canvas/富编辑器）
   - **直接 DOM/CDP**: `js()` / `cdp()`（自定义需求）

4. **Site Learnings（网站知识库）**
   - 可重用的站点特定工具和工作流
   - 位于 `skills/ego-browser/learnings/<site>/`
   - 包含 manifest.json + tools + notes

### 技术栈

- **语言**: TypeScript (ESM only)
- **运行时**: Node.js >= 22
- **协议**: Chrome DevTools Protocol (CDP)
- **打包**: esbuild + rollup
- **测试**: Node.js 内置 test runner

## 核心 API

### Task Space 管理
```javascript
const task = await useOrCreateTaskSpace('task-name')
await claimTaskSpace(id)
await handOffTaskSpace(id)
await takeOverTaskSpace(id)
await completeTaskSpace(id, { keep: false })
```

### 导航与状态
```javascript
await openOrReuseTab('https://example.com', { wait: true })
await gotoAndWait(url, { timeout, settle })
await currentTab()
await listTabs()
```

### 观察
```javascript
const snapshot = await snapshotText()
const screenshot = await captureScreenshot()
const info = await pageInfo()
```

### 交互
```javascript
await click('@21', { label: 'click submit' })
await fillInput('@5', 'text')
await typeText('hello')
await scrollBy(900)
```

### 执行
```javascript
const data = await js(`document.title`)
await cdp('Page.navigate', { url: '...' })
```

## 与其他工具对比

| 功能 | ego-lite | Browser-Use | agent-browser | ChatGPT Atlas | Perplexity Comet |
|------|----------|-------------|---------------|---------------|------------------|
| 多任务并行 | ✓ | — | — | — | — |
| 可重用技能 | ✓ | — | — | — | — |
| 继承 Chrome 数据 | ✓ | — | — | ✓ | ✓ |
| 独立工作空间 | ✓ | — | — | — | — |
| 压缩语义输入 | ✓ | — | ✓ | — | — |
| 外部 Agent 控制 | ✓ | ✓ | ✓ | — | — |
| 本地数据存储 | ✓ | ✓ | ✓ | — | — |
| 无登录摩擦 | ✓ | — | — | ✓ | ✓ |
| 日常浏览器 | ✓ | — | — | ✓ | ✓ |
| 免费 | ✓ | ✓ | ✓ | — | — |

## 性能基准测试

与 Vercel 的 agent-browser 对比（4 个复杂任务）：
- **速度**: 快 2.5 倍
- **Token 消耗**: 显著更少
- **任务成功率**: 更高

## 使用方式

### 1. 安装
```bash
# 方法 1: 下载 macOS app
# 下载 .dmg 文件并安装

# 方法 2: 通过 npx 添加 skill
npx skills add citrolabs/ego-lite

# 方法 3: 让 agent 自动设置
# 告诉 agent: "Set up ego lite for me"
```

### 2. 使用示例
```bash
ego-browser nodejs <<'EOF'
const task = await useOrCreateTaskSpace('inspect example')
cliLog('task space id: ' + task.id)

await openOrReuseTab('https://example.com', { wait: true })
cliLog(await snapshotText())
EOF
```

### 3. 在 AI Agent 中调用
```
/ego-browser follow @ego_agent on x.com for me
```

## 应用场景

1. **自动化任务**
   - 填写表单
   - 数据抓取
   - 网页测试
   - 自动登录

2. **并行工作**
   - 多个潜在客户信息丰富
   - 竞品网站批量抓取
   - 批量数据收集

3. **复杂交互**
   - 富文本编辑器操作
   - Canvas 应用交互
   - 需要登录状态的操作

## 与钉钉数字员工的集成潜力

### 可能的集成方式

1. **作为能力插件**
   - 在 `src/custom/capabilities/` 添加 `browser.py`
   - 通过 subprocess 调用 `ego-browser`
   - 处理浏览器自动化请求

2. **应用场景**
   ```
   用户: "帮我查一下这个网站的价格"
   数字员工: [使用 ego-browser 打开网站 → 提取价格 → 回复]
   
   用户: "帮我填写这个表单"
   数字员工: [打开表单 → 根据用户数据填写 → 提交 → 确认]
   ```

3. **技术优势**
   - 无需维护 Selenium/Playwright 环境
   - 继承用户登录状态（重要！）
   - 并行处理多个请求
   - 更好的页面理解能力（高质量 snapshot）

### 实现建议

```python
# src/custom/capabilities/browser.py
from core.capabilities import Capability, register
from core.inbound import KIND_TEXT
import subprocess
import json

def handle_browser_request(msg):
    # 检测是否是浏览器操作请求
    if not is_browser_request(msg['content']):
        return False
    
    # 构建 ego-browser 脚本
    script = generate_browser_script(msg['content'])
    
    # 执行
    result = subprocess.run(
        ['ego-browser', 'nodejs'],
        input=script,
        capture_output=True,
        text=True
    )
    
    # 返回结果
    reply_to_user(msg, result.stdout)
    return True

register(Capability(
    name="browser",
    on_inbound=handle_browser_request,
    handles_kinds={KIND_TEXT},
    priority=45,  # 在 text_reply 之前
    dedup=True,
    loop_guard=True
))
```

## 潜在挑战

1. **平台限制**: 当前仅支持 macOS
2. **依赖管理**: 需要安装 ego-lite 浏览器
3. **安全性**: 需要仔细处理用户登录状态的访问
4. **资源消耗**: 浏览器操作相对消耗资源

## 文档资源

- **官方文档**: https://lite.ego.app/document/
- **Discord 社区**: https://discord.gg/5eGZVvHbTq
- **Twitter**: https://x.com/ego_agent
- **GitHub Discussions**: https://github.com/citrolabs/ego-lite/discussions

## 总结

ego-lite 是一个设计精良的浏览器自动化工具，特别适合 AI agents：

**优势**:
- 代码优先的 API 设计（而非 CLI）
- 真正的人机协作浏览器
- 高质量的页面理解能力
- 继承用户登录状态
- 支持并行任务

**劣势**:
- 目前仅支持 macOS
- 需要单独安装浏览器
- 相对较新的项目

**适用场景**: 需要复杂浏览器自动化、需要登录状态、需要高质量页面理解的场景。

对于钉钉数字员工项目，可以作为一个高级能力插件考虑集成。
