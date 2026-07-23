---
name: e2e-smoke
description: 端到端冒烟——以真人身份给数字员工发一条文本，双校验（日志 + 钉钉实际会话）回复正确。当用户想验证数字员工「收→大脑→发」闭环是否打通、或改动后要确认线上真实链路正常时使用。
allowed-tools: Bash
---

# /e2e-smoke — 文本回复真实链路端到端冒烟

以真人身份私聊发一条带唯一校验码的算式，验证数字员工经完整链路
（dws 订阅 → bridge → event_watcher → brain(opencode serve) → replier）
收到并回复了**正确答案**，用日志 + 钉钉实际会话双校验。

## 怎么做

1. 直接跑封装好的脚本（身份自动探测、V1-V4 双校验、SKIP 友好）：

   ```bash
   bash tests/custom/e2e_text_reply_test.sh
   ```

   - 群聊链路：`E2E_TARGET=group bash tests/custom/e2e_text_reply_test.sh`
   - 慢环境放宽超时：`E2E_WAIT=90 bash tests/custom/e2e_text_reply_test.sh`
   - 指定发送方：`E2E_SENDER_PROFILE="<corpId>:<真人userId>" bash …`

2. 读退出码与末行判定：
   - `✅ 文本回复真实链路端到端测试通过（V1-V4）` → 链路正常，把 `校验码 → 回复` 一行回报用户。
   - `⏭️ SKIP …` → 前置不满足（无 dws / 未登录 / 服务未跑）。按提示让用户先
     `bash bin/core/start.sh` 或登录，再重试。
   - `❌ … 存在失败项` → 看是哪个 V 挂：
     - **V2 未见入站**：多半是订阅投递停滞（AGENTS.md 坑#3）。先
       `bash bin/core/reboot.sh` 重建订阅，等 ~20s warmup，再跑一次。
     - **V3/V4 挂而 V2 过**：brain / serve / replier 侧问题，
       `tail -n 40 monitor.log opencode.log` 定位。

## 注意

- 这条会**真实发消息**到钉钉，仅用于用户明确要做端到端确认时。
- 不改任何代码；纯读 + 发一条测试消息。
- 校验用 `dws chat message list --group <convId>`（o2o 私聊回复 list-by-sender 索引不到，
  见 AGENTS.md 坑#1）。
