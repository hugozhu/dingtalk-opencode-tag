# 会话统计摘要功能

**Issue**: #63  
**功能**: 当 opencode session 完成（对话结束）时，自动向用户发送一条包含本次会话统计信息的摘要消息。

## 功能概述

这个功能为数字员工添加了会话统计追踪和报告能力，帮助用户了解每次对话的资源消耗和性能情况。

## 消息格式

```
Session ID: abc123xyz

⏱️ 耗时: 55s
🤖 模型: opencode/deepseek-v4-flash-free
🔄 轮数: 3
💬 Tokens: 输入 5.4K↑ / 输出 876↓
🧠 推理: 1.2K
📊 窗口: 5.4K/1.0M（0.5%）
```

## 触发时机

统计摘要可以在以下场景自动发送（通过配置控制）：

1. **reset** - Session 被显式关闭时（用户发送 `/new`、`重新开始` 等重置关键词）
2. **ttl** - Session 因 TTL 过期被清理时
3. **lru** - Session 因 LRU 逐出被清理时（可选，可能频繁）
4. **command** - 用户主动请求统计信息时（发送 `/stats` 命令）

## 配置项

在 `config/constants.sh` 或 `config/constants.local.sh` 中配置：

```bash
# 是否启用统计摘要（默认开启）
export AGENT_SESSION_SUMMARY_ENABLED=1

# 统计摘要的触发场景（逗号分隔）
# reset: 用户主动重置会话时
# ttl: TTL 过期时
# lru: LRU 逐出时（可能频繁，建议关闭）
# command: 用户发送 /stats 命令时
export AGENT_SESSION_SUMMARY_TRIGGERS="reset,command"

# 是否仅在单聊中发送（默认 1，群聊不发避免噪音）
export AGENT_SESSION_SUMMARY_O2O_ONLY=1
```

### 配置说明

- **AGENT_SESSION_SUMMARY_ENABLED**: 总开关，设为 `1` 启用统计摘要功能
- **AGENT_SESSION_SUMMARY_TRIGGERS**: 控制在哪些场景发送摘要
  - `reset`: 推荐开启，用户主动重置时发送，最不打扰
  - `command`: 推荐开启，用户主动查询时发送
  - `ttl`: 可选，会话过期时发送，提醒用户会话已结束
  - `lru`: 不推荐，可能频繁触发造成噪音
- **AGENT_SESSION_SUMMARY_O2O_ONLY**: 推荐设为 `1`，避免在群聊中造成噪音

## 使用场景

### 1. 主动查询统计（/stats 命令）

用户可以随时发送 `/stats` 命令查看当前会话的统计信息：

```
用户: /stats
数字员工: 
📊 当前会话统计

Session ID: abc123xyz

⏱️ 耗时: 125s
🤖 模型: opencode/deepseek-v4-flash-free
🔄 轮数: 3
💬 Tokens: 输入 5.4K↑ / 输出 876↓
🧠 推理: 1.2K
📊 窗口: 5.4K/1.0M（0.5%）
```

**注意**: `/stats` 命令始终可用，不受 `AGENT_SESSION_SUMMARY_ENABLED` 开关影响。

### 2. 会话结束时自动发送

当用户发送重置关键词（如 `/new`、`重新开始`）时，数字员工会先发送统计摘要，然后确认重置：

```
用户: /new
数字员工: 
[统计摘要消息]
🆕 已开启新话题，之前的上下文已清空。
```

## 实现细节

### 追踪的统计信息

会话统计在 `src/custom/brain.py` 中的 `_conv_sessions` 字典追踪：

```python
{
    "sid": str,              # Session ID
    "last": float,           # 最后活动时间
    "created": float,        # 创建时间
    "rounds": int,           # 对话轮数
    "input_tokens": int,     # 输入 tokens
    "output_tokens": int,    # 输出 tokens
    "reasoning_tokens": int, # 推理 tokens
}
```

### 统计更新时机

- **Session 创建**: 初始化所有统计字段为 0
- **每次 POST message**: 从 opencode serve 响应中提取 `usage` 信息，累加 tokens
- **轮数递增**: 每次成功发送消息后 `rounds += 1`

### 关键函数

- **`_update_stats(conv_id, input_tokens, output_tokens, reasoning_tokens)`**: 更新统计信息
- **`_get_session_stats(conv_id)`**: 获取会话统计（返回 dict）
- **`_format_session_summary(stats)`**: 格式化统计摘要消息
- **`_send_session_summary(conv_id, conv_type, trigger)`**: 发送统计摘要

## 依赖要求

1. **会话复用必须开启**: 统计功能依赖于会话连续性（#56），需要设置：
   ```bash
   export AGENT_SESSION_REUSE=1
   ```

2. **opencode serve HTTP 接口**: 统计信息从 serve 的响应中提取，CLI 回退路径不支持统计追踪

## 使用建议

### 开发和调试

在开发环境中，建议启用统计摘要以了解资源消耗：

```bash
export AGENT_SESSION_SUMMARY_ENABLED=1
export AGENT_SESSION_SUMMARY_TRIGGERS="reset,command"
export AGENT_SESSION_SUMMARY_O2O_ONLY=1
```

### 生产环境

生产环境中，**默认关闭**自动摘要，避免打扰用户：

```bash
export AGENT_SESSION_SUMMARY_ENABLED=0  # 关闭自动摘要
export AGENT_SESSION_SUMMARY_TRIGGERS="command"  # 但保留 /stats 命令
```

用户可以主动发送 `/stats` 查询统计，而不会收到自动推送。

### 单聊 vs 群聊

- **单聊**: 可以适度启用自动摘要（`AGENT_SESSION_SUMMARY_O2O_ONLY=1`）
- **群聊**: 强烈建议关闭自动摘要，避免噪音（`AGENT_SESSION_SUMMARY_O2O_ONLY=1`）

## 文件清单

### 修改的文件

- `src/custom/brain.py`: 核心统计追踪和摘要发送逻辑
- `config/constants.sh`: 添加配置项
- `src/custom/capabilities/__init__.py`: 注册 stats 能力

### 新增的文件

- `src/custom/capabilities/stats.py`: `/stats` 命令处理能力
- `docs/session-stats.md`: 本文档

## 优先级

**P2 - Medium**: 有用但非紧急的功能

## 相关 Issue

- #56 会话连续性功能（依赖）
- #63 opencode session 完成时发送统计摘要消息（本功能）

## 测试

运行以下命令测试功能：

```bash
# 测试语法
python3 -m py_compile src/custom/brain.py
python3 -m py_compile src/custom/capabilities/stats.py

# 测试功能
python3 -c "
import sys
sys.path.insert(0, 'src')
from custom import brain
from custom.capabilities import stats

# 测试格式化
test_stats = {
    'sid': 'test123',
    'elapsed': 125,
    'model': 'opencode/deepseek-v4-flash-free',
    'rounds': 3,
    'input_tokens': 5432,
    'output_tokens': 876,
    'reasoning_tokens': 1234,
}
print(brain._format_session_summary(test_stats))
"
```

## 未来增强

- 添加成本估算（基于 token 使用量）
- 支持导出统计数据到日志文件
- 会话历史查询（查看过去的会话统计）
- 统计数据可视化（图表展示）
