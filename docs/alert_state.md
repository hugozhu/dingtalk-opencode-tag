# 监控告警状态跟踪

按 docs/EVENT_HANDLING_POLICY.md 的升级规则（同设备+同服务连续 ≥6h 未恢复 → 升级提醒）跟踪。
恢复后再报警 → 计时重置，删除对应行。

| 设备 | 服务 | 首次报警 | 最近报警 | 状态 |
|------|------|----------|----------|------|
| pi9-bookworm (36.24.201.230 / 36.27.111.82) | tailscale 容器 + socks5://192.168.1.12:1080 出口代理 | 2026-08-07 08:48（慢性自 2026-07-21） | **2026-09-13 05:30:01 UTC（13:30 北京）watchdog 重启 unhealthy 容器 tailscale** | 报警中·未恢复（**第八次升级通报已于 2026-09-13 05:30 UTC 发出**；慢性 **54 天**，容器累计重启 **36+ 次**无一次持久恢复）；冷却线 09-12 02:30 UTC 早已过期，其间 **09-11 18:30 UTC**（09-12 02:30 CST，冷却内→抑制正确但理由仍写「未达 6h」）与 **09-12 12:00:01 UTC**（09-12 20:00 CST，**已过冷却线却全群无任何回复 = 第四次跟踪断档/漏报**）两次重启；出口探测 09-12 02:02 CST ~ 09-13 10:01 CST 共 **DOWN 34 次 / UP 33 次**持续抖动（hugozhu.site DOWN 14、Google DOWN 10、Web Sites DOWN 10；每次约 10min 后翻转回 UP，无一次持续恢复）；09-12 07:30 CST pi9 `reconnected to l2tp on cn.hugozhu.site`（L2TP 重连，未带来 healthy）；proxy `not working after 3 attempts` 探测自 09-06 后**仍无输出（已 7 天）**，观测缺口未闭合；cn3 exit-node 线自 09-11 23:50 CST 后**无复报**（冷却线 09-12 15:50 UTC 已过 → 下次复报即升级）；下次提醒条件：恢复后新发作 或 24h 冷却后下次报警（=**2026-09-14 05:30 UTC / 13:30 CST**） |
| pi9-bookworm | frp status "us_azu1_ss" is missing | 2026-08-11 22:45（08-12 16:27 再发，公网 IP 字段为空） | **2026-09-12 22:25 CST（14:25 UTC）再发** | 09-08 后静默 4 天，**09-12 22:25 CST 复报**（距 09-12 20:00 CST tailscale 容器重启仅 2.4h，延续「frp 依赖 tailscale 出口、容器重启后隧道不自动重连」特征）；不单独计时，随 tailscale 线合并跟踪，已并入 09-13 第八次通报 |
| cn3.hugozhu.site | tailscale exit-node 自动重置脚本（期望出口 IP 8.148.201.142，未命中即切回 hk1） | 2026-09-04 20:20 CST | **2026-09-13 19:02 CST（11:02 UTC）resetting exit-node to hk1 >8.148.201.142** | 报警中·自愈动作反复触发未根治（**第二次升级通报已于 2026-09-13 11:02 UTC 发出**；慢性自 09-04 共 **9 天**）；09-11 单日 5 次（08:00/09:00/10:34/17:00/23:50）→ 09-11 23:50 后静默 ~43h → **09-13 19:02 CST 复报**；冷却线 09-12 15:50 UTC 已过期 19h → 按本文件「下次复报即升级」续算，发第二次升级通报；与 pi9 慢性线（54 天/第八次通报）同指 hk1 exit node，疑共同根因（hk1 侧 auth/订阅 或 exit-node 设置在 tailscale 重启后丢失，自动切回 hk1 从不持久恢复）；冷却重置至 **2026-09-14 11:02 UTC（19:02 CST）**（恢复后新发作除外） |

## 历史记录

- 2026-08-07 20:25 tailscale proxy socks5://192.168.1.12:1080 not working after 3 attempts, exit-node IP:（值为空）
- 2026-08-07 20:50 同上（重复报警，持续中）
- 2026-08-08 00:50 同上（重复报警，未恢复；重复告警抑制，不重复打扰用户）
- 2026-08-08 13:07 同上（重复报警，未恢复；重复告警抑制，不重复打扰用户）
- 2026-08-08 13:15 watchdog 自动重启 unhealthy 容器 tailscale（系统自动处置中，等待下一轮探测确认是否恢复）
- 2026-08-09 18:51 同上 not working after 3 attempts, exit-node IP:（值为空）（重复报警，未恢复；距上次升级 2026-08-09 01:52 未满 24h，重复告警抑制，不重复打扰用户）
- 2026-08-09 22:15 watchdog 自动重启 unhealthy 容器 tailscale（系统自动处置中，重复告警抑制，等待下一轮探测确认是否恢复）
- 2026-08-10 05:15 watchdog 自动重启 unhealthy 容器 tailscale；05:26 仍 not working after 3 attempts, exit-node IP:（值为空）——重启无效，未恢复；距上次升级（2026-08-09 01:52）已超 24h，于 05:26 再次升级提醒用户
- 2026-08-10 05:30 watchdog 再次自动重启 unhealthy 容器 tailscale（距 05:15 仅 15 分钟又判不健康；系统自动处置中，05:26 刚升级过，重复告警抑制，不打扰用户）
- 2026-08-10 13:45 watchdog 再次自动重启 unhealthy 容器 tailscale（系统自动处置中，重复告警抑制；等待下一轮探测确认是否恢复，下次升级条件仍为 恢复后新发作 或 24h 无更新=2026-08-11T05:26）
- 2026-08-10 14:00 新问题：frp status "us_azu1_ss" is missing（监控告警·代理缺失，与 tailscale-proxy 同设备 pi9）；首次记录，未满 6h 暂不升级，持续至 2026-08-10 20:00 未恢复则升级
- 2026-08-10 21:45 watchdog 再次重启 unhealthy 容器 tailscale，随后探测仍 not working after 3 attempts, exit-node IP:（值为空）——未恢复；tailscale 距上次升级（05:26）未满 24h，重复告警抑制
- 2026-08-10 22:58 ~ 2026-08-11 02:48 PiBot 出口探测多次 DOWN（Google TLS 断连/超时、Web Sites Child inaccessible、hugozhu.site 超时），佐证 pi9 出口网络不稳定
- 2026-08-11 03:00 watchdog 再次重启 unhealthy 容器 tailscale；03:45 探测仍 not working after 3 attempts（重启无效）；重复告警抑制，未打扰用户
- 2026-08-11 04:03 本次报警：tailscale proxy socks5://192.168.1.12:1080 not working after 3 attempts, exit-node IP:（值为空，且本次公网 IP 字段也为空，探测抖动/出口不稳加剧）；tailscale 距上次升级 22h37m 未满 24h，本不单独升级
- 2026-08-11 04:03 但 frp "us_azu1_ss" missing 已于 08-10 20:00 满 6h 阈值且无恢复信号（逾期未升级），本次补发升级提醒，并把 tailscale 最新状态合并通报；两条线冷却均重置至 2026-08-12 04:03（恢复后新发作除外）。注意 frp 自 08-10 14:00 后无重复报警，可能已静默恢复，已请用户确认
- 2026-08-11 04:15 watchdog 再次重启 unhealthy 容器 tailscale（系统自动处置中）；04:03 刚升级过，冷却期（至 2026-08-12 04:03）内重复告警抑制，不打扰用户，等待下一轮探测确认重启是否有效
- 2026-08-11 22:45 watchdog 重启 unhealthy 容器 tailscale（状态通知·系统自动处置，重复告警冷却期内，静默）
- 2026-08-11 22:45 frp status "us_azu1_ss" is missing 再次出现：距上次报警（08-10 14:00）间隔 32h+，判定为恢复后新发作，计时重置；恰在 tailscale 容器重启后出现，疑似瞬时关联；重复告警抑制，不打扰用户；若持续至 2026-08-12 04:45 未恢复则升级
- 2026-08-12 00:00 frp status "us_azu1_ss" is missing 重复报警（新发作已持续 1h15m，未见恢复信号）；未满 6h 阈值，重复告警抑制，不打扰用户；升级条件不变：持续至 2026-08-12 04:45 未恢复
- 2026-08-12 00:00~16:27 frp 告警静默 16.5h；期间同设备同脚本其他探测持续在发（tailscale proxy 05:00/08:25/09:50 失败、容器重启 04:15/06:00/15:45、SSH 封禁、BTC/股价/新闻推送、PiBot 站点探测），判定 frp 探测本身存活、隧道疑似曾短暂恢复；注：05:00~09:50 期间会话上下文丢失，tailscale 重复告警被误按「首次出现」静默处理，未按本状态文件续算（04:03 冷却已到期），本次一并补升级
- 2026-08-12 16:27 frp status "us_azu1_ss" is missing 再发（公网 IP 字段为空，出口不稳特征）；恰在 15:45 tailscale 容器 watchdog 重启、16:08 hugozhu.site 探测 DOWN（16:18 恢复）之后——与 22:45 发作同样紧随 tailscale 重启，指向 frp 隧道依赖 tailscale 出口/重启后未自动重连
- 2026-08-12 16:28 frp 本轮（22:45 起）已过 04:45 升级线且 16:27 再发、tailscale 冷却（04:03）到期后 05:00/08:25/09:50 持续报警未恢复——两条线合并升级通报用户；冷却均重置至 2026-08-13 16:28（恢复后新发作除外）
- 2026-08-30 16:30（08:30 UTC）watchdog 再次重启 unhealthy 容器 tailscale；距 08-19 02:30 第三次通报已静默 11.5 天再报，冷却线（08-20 02:30）早已过期，按状态文件续算满足再升级条件，已发第四次升级通报（慢性自 07-21 已 40 天，watchdog 重启历来无持久恢复、exit-node IP 恒为空，疑似 exit node 认证/订阅问题，需人工介入）；冷却重置至 2026-08-31 16:30（恢复后新发作除外）；留意 frp missing 历史上易在 tailscale 重启后跟随出现
- 2026-09-06 09:30 UTC（17:30 北京）新设备 dev2 / Dev1（42.120.72.73）watchdog 自动重启 unhealthy 容器 openclaw-gateway（监控告警·系统自动处置）；本状态文件无该设备/服务历史，判为首次发作，记录起算时间；未满 6h 不升级，静默观察，升级线 2026-09-06 15:30 UTC（23:30 北京）；期间若反复重启或后续探测仍报 unhealthy → 满 6h 升级通报；若恢复则删除跟踪行、计时重置
- 2026-09-01 ~ 2026-09-06 pi9 tailscale 线持续报警未恢复：watchdog 重启 unhealthy 容器 tailscale ≥16 次（09-01×2 / 09-02×4 / 09-03×6 / 09-04×1 / 09-05×2 / 09-06×1），并多次 `tailscale proxy socks5://192.168.1.12:1080 not working after 3 attempts, exit-node IP:`（IP 字段恒空；期间公网 IP 在 36.27.111.82 / 36.24.201.230 / 空 之间跳变）；08-30 第四次通报设的冷却线 2026-08-31 16:30 早已过期，但这一周未再向用户通报——**跟踪断档**（部分轮次把 watchdog 重启误按「状态通知·系统自动处置」静默，未按本状态文件续算「24h 冷却后下次报警即升级」）
- 2026-09-04 另见 cn3.hugozhu.site 自动动作 `resetting exit-node to hk1 on tailscale >8.148.201.142`——已有脚本在自动切 exit node，但 pi9 侧容器仍反复 unhealthy，佐证问题在 exit node 认证/订阅而非本地容器
- 2026-09-06 15:30 UTC（23:30 北京）watchdog 再次重启 unhealthy 容器 tailscale；冷却线（08-31 16:30）过期 + 一周持续未恢复 + 09-06 22:58/23:08 CST PiBot 探测 `[DOWN] Google timeout` / `[DOWN] Web Sites Child inaccessible`（23:08/23:18 恢复）佐证出口抖动 → 补发**第五次升级通报**（慢性自 07-21 已 48 天，watchdog 重启 25+ 次无持久恢复，需人工介入查 exit node）；冷却重置至 2026-09-07 15:30 UTC（恢复后新发作除外）
- 2026-09-06 15:30 UTC dev2 / openclaw-gateway：09:30 UTC 首次发作后至升级线（15:30 UTC）满 6h 内**无任何重复报警**（期间同群其他设备探测正常在发），判定 watchdog 重启后已自愈 → 删除跟踪行、计时重置；后续若再发作按新发作起算
- 2026-09-07 09:00 UTC watchdog 重启 unhealthy 容器 tailscale；在第 5 次通报冷却期（至 09-07 15:30 UTC）内 → 重复告警抑制，静默正确
- 2026-09-07 04:18 UTC ~ 2026-09-08 19:28 UTC（12:18 CST 09-07 ~ 03:28 CST 09-09）PiBot 出口探测**连续 ~39h DOWN/UP 抖动**：`[DOWN] Google > timeout of 48000ms exceeded`、`[DOWN] Web Sites > Child inaccessible`、`[DOWN] hugozhu.site > timeout`，每 10~60 分钟翻转一次，**无一次持续恢复**——pi9 出口链路整体不稳，不再是单纯 tailscale 容器问题
- 2026-09-07 18:00 UTC（09-08 02:00 CST）watchdog 再次重启 unhealthy 容器 tailscale；冷却线（09-07 15:30 UTC）**已过期** → 按规则应升级，却被误判「监控类事件·系统已自处置」静默 = **第二次跟踪断档**
- 2026-09-08 02:30 UTC watchdog 再次重启 unhealthy 容器 tailscale；同样已过冷却线，仍被静默（当时回复「若同一容器连续 ≥6h 反复重启未恢复再告警」，忽略了本状态文件里已慢性 48 天、且规则是「24h 冷却后下次报警即升级」）
- 2026-09-08 ~04:45 UTC（12:45 CST）watchdog 第三次重启 unhealthy 容器 tailscale；紧随其后 frp `"us_azu1_ss" is missing` 多次再现（延续 frp 依赖 tailscale 出口特征）
- 2026-09-08 20:00 UTC watchdog 第四次重启 unhealthy 容器 tailscale（本次报警）；距 09-07 18:00 UTC 首次过冷却线已 26h、期间 4 次重启 + 39h 出口探测抖动 + frp 反复缺失 → 补发**第六次升级通报**（慢性自 07-21 已 49 天，容器累计重启 30+ 次）；冷却重置至 2026-09-09 20:00 UTC（恢复后新发作除外）
- 2026-09-08 观测缺口：proxy `socks5://192.168.1.12:1080 not working after 3 attempts, exit-node IP:` 探测自 09-06 后**再无输出**（此前每日多次）——要么探测脚本停摆/被改，要么出口已换路径；已在通报中请用户确认，避免"没有坏消息"被误读为恢复
- 2026-09-09 13:30 UTC watchdog 再次重启 unhealthy 容器 tailscale；在第 6 次通报冷却期（至 09-09 20:00 UTC）内 → 抑制结果正确，但当轮回复理由为「未达 6h 连续升级阈值」（未查本文件的慢性续算规则），属判断依据错误、结论碰对
- 2026-09-09 21:48 CST ~ 2026-09-11 08:51 CST PiBot 出口探测持续抖动：**DOWN 46 次 / UP 46 次**（Google timeout 19、Web Sites Child inaccessible 17、hugozhu.site timeout 11；09-10 单日 34 次、频率由隔 3h 加速到隔 30min），无一次持续恢复
- 2026-09-10 07:30 UTC watchdog 再次重启 unhealthy 容器 tailscale；冷却线（09-09 20:00 UTC）**已过期** → 按规则应发第七次通报，实际**无任何回复** = 第三次跟踪断档（漏报）
- 2026-09-11 01:00 UTC watchdog 再次重启 unhealthy 容器 tailscale；同样已过冷却线，被回复为「状态通知·已自愈，按规范静默」并另起「持续到 14:00 满 6h 再升级」的新线 → 无视慢性 52 天历史与本文件续算规则，断档延续
- 2026-09-11 ~07:00 CST cn3.hugozhu.site 又一次自动 `resetting exit-node to hk1 on tailscale >8.148.201.142`（同 09-04，历次自动切 exit node 均未让 pi9 侧恢复 healthy）
- 2026-09-11 02:30 UTC（10:30 北京）新设备/新服务：dev2 · Dev1（42.120.72.73）watchdog 自动重启 unhealthy 容器 **dwh-assistant-router-run-c6537d41116a**（监控告警·系统已自动处置）；本文件无该服务历史，dev2 上一次为 09-06 openclaw-gateway 单次后自愈 → 判首次发作，新建跟踪行，未满 6h 不升级，升级线 2026-09-11 08:30 UTC（16:30 北京）
- 2026-09-11 02:30 UTC 同轮补发 pi9 tailscale **第七次升级通报**（慢性自 07-21 已 52 天，累计重启 33+ 次，48h 出口探测 46 次 DOWN 无持久恢复，socks5 proxy 探测已 5 天无输出）；冷却重置至 2026-09-12 02:30 UTC（恢复后新发作除外）
- 2026-09-11 流程改进：本轮起，收到 watchdog/监控类事件**先读本文件续算**再决定静默或升级，禁止用「单次告警未达 6h」作为已有慢性跟踪行的理由（三次断档同源）
- 2026-09-11 全天 cn3.hugozhu.site 自动 `resetting exit-node to hk1 on tailscale >8.148.201.142` **共 5 次**：08:00 / 09:00 / 10:34 / 17:00 / 23:50 CST（DWS 拉群逐条核对）；08:00、17:00 两轮被判「状态通知·已自愈」静默，23:50 本轮按本文件续算：同设备+同服务跨 ~16h 反复触发、自动切换从不持久 → **满足 ≥6h 升级条件，发首次升级通报**，新建 cn3 跟踪行，冷却至 2026-09-12 15:50 UTC
- 2026-09-11 同期 PiBot 出口探测抖动（佐证 cn3/pi9 出口链路不稳）：hugozhu.site DOWN 08:42 / 14:42 / 17:42 / 22:02（各约 10min 后 UP）、Google DOWN 00:12 / 14:42 / 20:02、Web Sites Child inaccessible 00:21 / 20:11
- 2026-09-11 23:50 CST 复核 dev2 · dwh-assistant-router-run-c6537d41116a：首次发作（02:30 UTC）后至升级线 08:30 UTC 及此后 **无任何复报**（期间同群其他设备探测正常在发）→ 判 watchdog 重启后自愈，**删除跟踪行、计时重置**；后续再发作按新发作起算
- 2026-09-13 09:59 CST 本轮仅收到 pi9-bookworm **行情数据推送**（黄金 942 元/克 / BTC ~$77,000），非监控告警，按数据推送规则处理（环比见 docs/price_state.md，未达 5% → 静默）；推送能送达说明 pi9 在线且出口可达，但**不等于 tailscale 容器 healthy / socks5 代理恢复**，两条慢性线状态不变；其冷却线均已过期（pi9 tailscale = 2026-09-12 02:30 UTC、cn3 exit-node = 2026-09-12 15:50 UTC）→ **下次同类报警即升级**，不得再以「单次未达 6h」静默
- 2026-09-12 02:30 CST（= 09-11 18:30 UTC）watchdog 重启 unhealthy 容器 tailscale；当时冷却线为 09-12 02:30 UTC（未到期）→ 抑制结果正确，但回复理由仍写「未达 6h 连续升级阈值」，未引用本文件续算口径（第四次同源判断依据错误）
- 2026-09-12 02:02 ~ 12:51 CST PiBot 出口探测 DOWN 15 次 / UP 14 次（hugozhu.site DOWN 5、Google DOWN 5、Web Sites DOWN 5）；群内 Claude Code 侧统计「第 55~60 起」闭环，05:22 CST 达 **60 起里程碑**（hugozhu.site 17 / Google 21 / Web Sites 22，跨 ~50h）
- 2026-09-12 07:30 CST pi9-bookworm `reconnected to l2tp on cn.hugozhu.site`（L2TP 重连事件，紧随其后并无 healthy 信号）
- 2026-09-12 12:00:01 UTC（20:00 CST）watchdog 再次重启 unhealthy 容器 tailscale；**冷却线 09-12 02:30 UTC 已过期 9.5h → 按规则应发第八次通报，实际全群无任何回复 = 第四次跟踪断档（漏报）**
- 2026-09-12 22:25 CST（14:25 UTC）frp status `"us_azu1_ss" is missing` 复报（09-08 后静默 4 天）；距 20:00 CST tailscale 容器重启仅 2.4h，延续「frp 隧道依赖 tailscale 出口、容器重启后不自动重连」特征
- 2026-09-12 13:02 ~ 2026-09-13 10:01 CST PiBot 出口探测 DOWN 19 次 / UP 19 次（hugozhu.site DOWN 9、Google DOWN 5、Web Sites DOWN 5）；与上一窗口合计 09-12 02:02 CST 起 **DOWN 34 次**，无一次持续恢复
- 2026-09-13 02:18~02:20 CST **SSH 暴力破解爆发：104.105.203.71 在 2.5 分钟内被封禁 16 次**（另有 154.201.86.249 / 203.3.114.90 / 218.94.16.158 各 1 次）→ 命中 EVENT_HANDLING_POLICY「同一来源 IP 被封禁 ≥10 次/天 → 每日汇总提醒一次」例外条款，已并入本轮通报（fail2ban 已自动处置，无需人工干预动作）
- 2026-09-13 05:30:01 UTC（13:30 CST）watchdog 再次重启 unhealthy 容器 tailscale（**本次报警**）；按本文件续算：冷却线（09-12 02:30 UTC）过期 27h、其间 09-12 12:00 UTC 已漏报一次、34 次出口探测 DOWN 无持久恢复、socks5 proxy 探测 7 天无输出、frp 隧道 09-12 复报 → **发第八次升级通报**（慢性自 07-21 共 54 天，容器累计重启 36+ 次）；冷却重置至 **2026-09-14 05:30 UTC（13:30 CST）**（恢复后新发作除外）
- 2026-09-13 cn3.hugozhu.site exit-node 自动重置线：自 09-11 23:50 CST 后**无复报**（DWS 拉群核对 09-12 00:00 ~ 09-13 10:01 CST 全量消息，无 `resetting exit-node` 条目）；冷却线 09-12 15:50 UTC 已过期 → 下次复报即升级，跟踪行保留
- 2026-09-13 19:02 CST（11:02 UTC）cn3.hugozhu.site `resetting exit-node to hk1 on tailscale >8.148.201.142` **复报**（本次报警）；按本文件续算：09-11 23:50 CST 后静默 ~43h、冷却线 09-12 15:50 UTC 已过期 19h → 命中「下次复报即升级」→ **发 cn3 线第二次升级通报**（慢性自 09-04 共 9 天，自动切回 hk1 从不持久恢复，与 pi9 tailscale 慢性线 54 天/第八次通报同指 hk1 exit node 共同根因）；冷却重置至 2026-09-14 11:02 UTC（19:02 CST，恢复后新发作除外）
