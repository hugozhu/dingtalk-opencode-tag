# 大脑 serve 连接看门狗：判定与自愈决策逻辑

> 备忘：`bin/custom/brain_watchdog.sh`（第 5 组件，2026-09-21 事故后上线）。
> 记录「何时该重启 serve、何时不该」的完整推理链，供后续调参/排障/贡献回 upstream 参考。

## 背景（2026-09-21 事故）

模型网关（内网 IP 走 VPN）凌晨闪断 2h+。恢复后：

- curl 直连网关：2.7ms 通
- 全新 `opencode run` CLI 进程：6s 出结果
- **常驻 serve：仍持续 `Cannot connect to API`**——闪断期间出站连接池被毒化（Bun 复用池里的半开死连接，毫秒级快速失败），**网络恢复后不会自愈**

表象：每条消息「serve HTTP 失败（~65s 超时）→ CLI 回退」；网关断期间 CLI 也挂 → 全发兜底提示。`check_brain` 只告警不修复，人工 reboot 才恢复。

## 证据源（三个无条件日志标记，零 token）

检测全部寄生在真实业务流量上，不发任何额外模型请求。`monitor.log` 无条件记录：

| 标记 | 含义 | 产生时机 |
|---|---|---|
| `brain opencode http err` | serve HTTP 路失败 | 每条消息先走 serve，失败即记（brain.py 恒记） |
| `CLI 回退成功` | **全新进程**连上了网关 | 只在 serve 失败后才调 CLI，成功即记 |
| `CLI 回退失败` | 全新进程也连不上 | 同上，失败即记 |

关键洞察：**「CLI 回退成功」是网关健康的铁证**——CLI 是一次性子进程、全新连接，它能通就证明网关通、网络通，唯一连不上的就是 serve 自己池子里的死连接。

辅助差分（零 token）：`curl <网关 baseURL>/models`，任何 HTTP 状态码（含 401）= 可达，000/超时 = 不可达。URL 自动发现自 opencode 配置的第一个 `baseURL`。

## 决策树（`_bw_check_once` 完整分支）

```
无失败记录                        → unknown       静默等待
失败全部陈旧（>1h 窗口）           → ok            计数清零
──────────────────────────────────────────────────────────
配对 ≥2 且最新一对新鲜（≤15min）   → poisoned_reboot ✅ 唯一重启路径
│    ├─ 冷却期内（<30min）         → poisoned_wait  只告警，不重复重启
│    └─ 已重启 ≥3 次               → poisoned_giveup 只告警，等人工
配对 =1（或配对陈旧但 http 失败新鲜）→ suspect       告警观察，凑够 2 对才动手
──────────────────────────────────────────────────────────
双路全挂（新鲜证据，无 CLI 成功）   → 不重启，curl 差分分级告警：
│    ├─ curl 不可达                → gateway_down   网关/网络层问题，等恢复
│    └─ curl 可达(含 401)          → opencode_net   外连异常，人工检查
│    └─ 探测未配置                 → both_fail      人工检查
证据陈旧（>15min）+ curl 可达      → recovered      静默（防旧事故刷屏）
证据陈旧 + curl 不可达             → gateway_down_stale 持续告警
```

「配对」= 窗口（默认 1h）内「CLI 回退成功」条数（CLI 只在 serve 失败后被调用，每条成功天然构成一对差分证据）。

## 为什么只有一条重启路径

**重启 serve 的充要条件：网关确定可达 + serve 确定连不上。**

- 网关可达的证明只能来自「CLI 回退成功」（或 curl，但 curl 只用于排除性判定，不作重启依据——防止 curl 与 serve 走不同网络路径的边缘情况误杀）
- serve 连不上的证明：http err 持续出现

反过来，**CLI 也失败时重启纯属浪费**：新进程的新连接照样连不上（问题在网关/网络层），重启只会打断 in-flight 请求。所以双路全挂只告警不动手——网关恢复后的下一条消息自然产生「CLI 回退成功」证据，自动转入自愈路径。这也是「不基于误判重启」哲学的延续（与 event_freshness_watchdog 的 DWS 交叉验证同款）。

## 三道防误伤闸门

1. **MIN_PAIRS=2**：单对可能是偶发抖动，两对才是持续状态
2. **FRESH_WINDOW=900s**：最新一对必须新鲜——防止重启一个其实已经好了的 serve。
   首启实测踩坑：watchdog 冷启动时读到了 50 分钟前事故的陈旧证据（还在 1h 窗口内），误发「外连异常」告警。修复 = 新鲜度门槛（陈旧失败 + curl 可达 → recovered 静默）
3. **防抖状态机**：30min 冷却 + 3 次上限。状态文件 `.brain-serve-stall.state` **不在 `clean_runtime_state` 清理表里**，跨 reboot 存活，防重启风暴

## 重启动作（`_bw_restart_serve`）为什么这么设计

- **只杀 serve，不动 connect/订阅**：比整机 reboot 轻，订阅连接不受影响
- `verify_pid` 防 PID 复用误杀；TERM → 3s → KILL 兜底
- 复用 canonical 启动链（`setup_components` → custom `start_serve`）：
  - `.serve.pwd` 保持稳定（start_serve 优先复用已有密码文件），in-flight 请求不会 401
  - AGENT_DEBUG 日志参数与正常启动一致
- 杀 serve 期间到达的消息：brain 的 HTTP 路失败 → 自动走 CLI 兜底，用户仍能收到回复（只是慢一点、无多轮上下文）

## 配置（`config/constants.local.sh` 可覆盖）

| 变量 | 默认 | 说明 |
|---|---|---|
| `BRAIN_SERVE_WATCHDOG` | 1 | 开关（0 关闭组件） |
| `BRAIN_SERVE_CHECK_INTERVAL` | 300 | 轮询间隔秒 |
| `BRAIN_SERVE_PAIR_WINDOW` | 3600 | 证据计数窗口秒 |
| `BRAIN_SERVE_FRESH_WINDOW` | 900 | 触发重启要求最近一对证据的年龄上限 |
| `BRAIN_SERVE_MIN_PAIRS` | 2 | 触发重启所需配对数 |
| `BRAIN_SERVE_RETRY_INTERVAL` | 1800 | 自愈/告警动作最小间隔 |
| `BRAIN_SERVE_MAX_RESTARTS` | 3 | 一次异常期内自动重启上限 |
| `BRAIN_GATEWAY_URL` | 自动发现 | 网关探测 URL（显式配置优先） |

## 排障捷径

- 看门狗日志：`grep brain-wd monitor.log`
- 手动单次判定：`source bin/custom/brain_watchdog.sh && _bw_check_once`（输出状态字符串）
- 手动重启 serve（与看门狗同款动作）：`source bin/core/lib.sh && <_bw_restart_serve 函数体>`
- 判定依据原始证据：`grep -aE 'brain opencode http err|CLI 回退' monitor.log | tail -20`
- 单测：`bash tests/custom/test_brain_watchdog.sh`（36 断言，纯 mock 不碰网络/serve）

## 与其他 watchdog 的关系

| 组件 | 检测对象 | 判据 | 动作 |
|---|---|---|---|
| healthcheck `check_brain` | 大脑「调了但失败」 | opencode.log 失败计数跨窗口累计 | 只告警（探针花 token） |
| `event_freshness_watchdog` | 事件流投递停滞 | monitor.log 入站时龄 + DWS 独立拉取交叉验证 | 告警 + 整机 reboot |
| `brain_watchdog`（本组件） | serve 连接池毒化 | http err + CLI 成功**配对差分** + curl 辅助 | 告警 + **只重启 serve** |

三者互补：check_brain 发现「坏」但不知「谁坏」；本组件用差分定位到 serve 并精准修复；event watchdog 管的是订阅侧（投递停滞），与大脑无关。
