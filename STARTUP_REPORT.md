# 服务启动报告功能

## 功能概述

数字员工服务启动后，自动生成详细的服务状态报告并发送给相关人员（订阅用户或其主管）。

## 实现的功能

### 1. 自动收集信息
- **数字员工身份**：姓名、用户ID、组织、企业ID、部门
- **订阅配置**：群聊订阅、@我订阅、单聊订阅状态
- **订阅用户信息**：查询每个订阅用户的详细信息及其主管
- **组件运行状态**：opencode serve、dws connect、event_watcher 的进程状态
- **健康检查结果**：运行 healthcheck.sh 获取系统健康状态
- **大脑配置**：类型、文本模型、视觉模型、回复模式

### 2. 智能发送策略
- **优先单聊**：首先尝试向目标用户发送单聊消息
- **降级群发**：单聊失败或不存在时，发送到订阅的群聊并 @ 目标用户
- **接收者选择**：
  - 如果订阅用户有主管，向主管发送报告
  - 如果订阅用户无主管（如管理员），向用户本人发送报告

### 3. 报告内容示例

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📋 数字员工服务启动报告
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

🕐 启动时间: 2026-07-21 17:48:38

👤 数字员工身份:
  • 姓名: opencode
  • 用户ID: 287179924
  • 组织: Raspberry Pi
  • 企业ID: dinga626d60c1128d449
  • 部门: 数字员工

🔔 订阅配置:
  • 群聊订阅: 已启用 (cidnc8d/OOBEqTow...)
  • @我订阅: 已启用
  • 单聊订阅: 已启用 (1 个用户)

👥 订阅用户详情:
  • hugozhu (ID: 0420506555)
    └─ 主管: 无上级主管

⚙️ 组件状态:
  • opencode serve: ✅ 运行中 (PID: 12345)
  • dws connect: ✅ 运行中 (PID: 12346)
  • event_watcher: ✅ 运行中 (PID: 12347)

🏥 健康检查:
✅ 全部通过

🧠 大脑配置:
  • 类型: opencode
  • 文本模型: opencode/deepseek-v4-flash-free
  • 视觉模型: opencode/mimo-v2.5-free
  • 回复模式: user

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
✅ 服务已就绪，随时为您服务
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

## 文件修改

### 新增文件
- `src/custom/capabilities/startup_report.py` - 启动报告能力模块

### 修改文件
- `src/core/event_watcher.py` - 在 main() 函数中添加启动报告调用
- `config/constants.sh` - 添加 `CAP_STARTUP_REPORT_ENABLED` 配置项

## 配置说明

### 环境变量

```bash
# 启动报告开关（默认启用）
export CAP_STARTUP_REPORT_ENABLED=1  # 1=启用, 0=禁用
```

在 `config/constants.local.sh` 中设置。

## 使用方法

### 自动触发
服务启动后 10 秒自动发送启动报告。

### 手动触发
```bash
bash -c '
source config/constants.local.sh
python3 -c "
import sys
sys.path.insert(0, \"src\")
from custom.capabilities.startup_report import send_startup_report
send_startup_report()
"
'
```

### 禁用功能
在 `config/constants.local.sh` 中设置：
```bash
export CAP_STARTUP_REPORT_ENABLED=0
```

## 工作流程

1. **event_watcher 启动**
   - 主线程启动 log-tail 和 SSE 连接
   - 后台线程延迟 10 秒启动报告任务

2. **收集信息**
   - 调用 `dws contact user get-self` 获取数字员工信息
   - 读取环境变量获取订阅配置
   - 调用 `dws contact user get` 获取订阅用户及主管信息
   - 检查 PID 文件获取组件运行状态
   - 运行 `healthcheck.sh` 获取健康状态

3. **生成报告**
   - 格式化所有收集的信息
   - 确定报告接收者（主管或订阅用户本人）

4. **发送报告**
   - 尝试通过单聊发送
   - 失败则通过群聊发送并 @ 目标用户

## 日志示例

```
[2026-07-21 17:49:09] [agent] [startup_report] 开始生成启动报告...
[2026-07-21 17:49:12] [agent] [startup_report] 报告已生成，将发送给 1 位接收者
[2026-07-21 17:49:12] [agent] [startup_report] 正在发送给 hugozhu (订阅用户, ID: 0420506555)
[2026-07-21 17:49:16] [agent] [startup_report] ✅ 报告已通过群聊发送给用户 0420506555
[2026-07-21 17:49:17] [agent] [startup_report] 启动报告发送完成: 成功 1/1
```

## 技术细节

### 依赖模块
- `core.agent_common` - 提供 DWS CLI 调用工具、日志、PROFILE 配置
- `subprocess` - 执行健康检查和消息发送
- `json` - 解析 DWS 返回的 JSON 数据
- `datetime` - 生成启动时间戳

### 关键函数
- `_get_current_user()` - 获取数字员工自身信息
- `_get_user_info(user_id)` - 获取指定用户信息
- `_check_process_status(pid_file)` - 检查进程运行状态
- `_get_component_status()` - 获取所有组件状态
- `_get_healthcheck_summary()` - 运行健康检查
- `_build_report()` - 生成完整报告
- `_send_to_user(user_id, report_text)` - 发送报告给用户
- `send_startup_report()` - 主入口函数

### 错误处理
- 所有网络调用都有超时和异常捕获
- 获取信息失败不会中断整个流程
- 发送失败会记录详细日志
- 使用 best-effort 策略，尽可能完成发送

## 测试结果

✅ 成功获取数字员工身份信息  
✅ 成功获取订阅用户信息  
✅ 成功检测组件运行状态  
✅ 成功运行健康检查  
✅ 成功生成完整报告  
✅ 成功通过群聊发送报告  

## 未来改进方向

1. **支持更多通知渠道**：邮件、企业微信、Slack 等
2. **报告模板定制**：允许用户自定义报告格式
3. **定期状态报告**：除启动外，支持定时发送状态报告
4. **异常检测告警**：检测到异常时主动发送告警
5. **历史报告查询**：保存历史报告供查询
