#!/bin/bash
# brain_watchdog.sh — 大脑 serve 连接看门狗（零 token 差分检测 + 自愈）
#
# 注册为第 5 个组件（bin/custom/start_funcs.sh 末尾 COMP_NAMES+=），monitor 兜底拉起。
#
# 背景（2026-09-21 事故：08:01–10:11 大脑双路全挂 2h+）：
#   模型网关（内网 IP，走 VPN 路由）夜间闪断。恢复后 curl 直连 2.7ms 通、全新 CLI
#   进程 6s 出结果，但常驻 serve 仍持续 `Cannot connect to API`——闪断期间 serve
#   出站连接池被毒化（Bun 复用半开死连接，毫秒级快速失败），**网络恢复后不会自愈**。
#   期间每条消息「serve HTTP 失败 → CLI 回退」：网关断时 CLI 也挂 → 全发兜底提示；
#   check_brain 探针只告警不修复。重启 serve（reboot.sh）即恢复。
#
# 背景 2（2026-09-25 事故：07:58–08:32 + 10:02–10:10 双路全挂，熔断告警 3 次）：
#   同款 `Cannot connect to API`，但 elapsed 恒定 63~68s（= opencode fetch 超时，
#   不是 09-21 的毫秒级快速失败），serve + CLI 双双挂；网关 curl L1 全程「可达」，
#   实测网关本身健康（恢复后连续 6 次推理 200 / 1.7~5.6s）。两个缺陷暴露：
#     ① L1 判据太弱（不带 key 打 /models，401 即可达）→ 推理层故障误报成
#        opencode_net，告警让人查代理/DNS/防火墙（实际全正常，无 proxy env）；
#     ② 「双路全挂 → 重启无效」被日志证伪：每次都是重启 serve 后立刻恢复
#        （08:32→08:44 重启→08:45 成功；10:10→10:23 重启→10:26 成功）。
#   修复 = 新增 L2 真实推理探测（见下）+ 「L1 通 L2 通 → 重启 serve」自愈路径。
#
# 判据（差分，全部寄生在真实业务流量上，零 token 零额外模型请求）：
#   monitor.log 无条件记录的三个标记（brain.py 保证错误恒记）：
#     - `brain opencode http err:`        serve HTTP 路失败（每条消息先走这条路）
#     - `brain(opencode): CLI 回退失败：`  CLI 全新一次性子进程也失败
#     - `brain(opencode): CLI 回退成功`    CLI 成功（= 网关此刻对全新进程可达）
#   「http err + CLI 回退成功」在窗口内成对出现 ≥ N 次 = **serve 单侧故障实锤**
#   （CLI 是全新进程+全新连接，它能通就证明网关通，只有 serve 自己连不上）。
#
#   辅助差分（两级，用于「双路全挂」时定位故障层）：
#     L1 路由层（零 token）：curl baseURL/models，任何 HTTP 状态码（含 401）= 可达。
#        **L1 通不等于能推理**——LiteLLM 前置进程活着就回 401/200。
#     L2 推理层（近零 token，~70 token/次，仅在双路全挂时触发 + TTL 缓存）：
#        带 apiKey 真发一条 max_tokens=1 的 ping，200/429 = 推理层通。
#        2026-09-25 事故：07:58–08:32、10:02–10:10 双路全挂 63~68s 超时，L1 全程
#        报「可达」→ 推理后端故障被误判成 opencode 外连异常（让人查代理/DNS/防火墙，
#        实测三样都正常）。L2 就是为了堵这个盲区。
#
# 动作：
#   - serve 单侧故障（≥ BRAIN_SERVE_MIN_PAIRS 对新鲜证据）：
#       告警主管 + **只重启 serve**（不动 connect/订阅，比整机 reboot 轻；复用
#       custom start_serve：.serve.pwd 保持稳定，in-flight 请求 401/拒连 → brain
#       自动走 CLI 兜底，用户仍能收到回复）
#   - 双路全挂（按 L1/L2 分级）：
#       L1 挂                → gateway_down：不重启（网关整体不可达，重启无效）
#       L1 通 + L2 挂        → gateway_infer_down：不重启（推理后端故障，重启无效）
#       L1 通 + L2 通        → serve_stale_reboot：**重启 serve 自愈**
#       L1 通 + L2 无法探测  → opencode_net：不重启（保守，人工确认）
#       探测未配置           → both_fail：不重启，人工
#     「L1+L2 均通就重启」由 2026-09-25 事故日志确立：旧策略一律不重启，而实际
#     每次都是重启后立刻恢复（08:32 失败→08:44 重启→08:45 成功 4.06s；
#     10:10 失败→10:23 重启→10:26 成功 5s），白拖 46min / 13min。
#     网关恢复后的下一条消息也会自然产生「CLI 回退成功」→ 转入 serve 单侧自愈路径。
#
# 防抖（状态文件 .brain-serve-stall.state；不在 clean_runtime_state 清理表里，
# reboot 后存活，防重启风暴）：
#   距上次自愈动作 < BRAIN_SERVE_RETRY_INTERVAL → 只告警不重启
#   累计自愈 ≥ BRAIN_SERVE_MAX_RESTARTS 仍异常 → 只告警，等人工
#   窗口内无 http err（serve 健康/无流量）→ 计数清零
#
# 配置（config/constants.local.sh 可覆盖，均有默认值）：
#   BRAIN_SERVE_WATCHDOG        开关（start_funcs.sh 判定，默认开）
#   BRAIN_SERVE_CHECK_INTERVAL  轮询间隔秒（默认 300）
#   BRAIN_SERVE_PAIR_WINDOW     证据计数窗口秒（默认 3600）
#   BRAIN_SERVE_FRESH_WINDOW    触发重启要求最近一对证据的年龄上限秒（默认 900，
#                               防止拿陈旧证据重启一个其实已被修好的 serve）
#   BRAIN_SERVE_MIN_PAIRS       触发重启所需「CLI 回退成功」对数（默认 2，零误报）
#   BRAIN_SERVE_RETRY_INTERVAL  自愈/告警动作最小间隔秒（默认 1800）
#   BRAIN_SERVE_MAX_RESTARTS    一次异常期内自动重启 serve 上限（默认 3）
#   BRAIN_GATEWAY_URL           L1 路由层探测 URL（缺省自动发现 opencode 配置里第一个
#                               baseURL 并补 /models；显式配置原样生效）
#   BRAIN_GATEWAY_TIMEOUT       L1 探测超时秒（默认 4）
#   BRAIN_GATEWAY_INFER_PROBE   L2 推理层探测开关（默认 1；关掉则退回纯 L1 旧行为）
#   BRAIN_GATEWAY_INFER_TIMEOUT L2 探测超时秒（默认 25，须 < opencode 的 63s，
#                               也要 > 网关正常推理耗时，实测 ping 1.7~5.6s）
#   BRAIN_GATEWAY_INFER_CACHE_TTL  L2 结果缓存秒（默认 60，防同轮重复打网关）
#   BRAIN_GATEWAY_APIKEY        L2 用的 apiKey（缺省自动发现 opencode 配置里的 apiKey）
#   BRAIN_GATEWAY_PROBE_MODEL   L2 探测模型（缺省取 AGENT_OPENCODE_MODEL 的 provider/
#                               后半段；必须探主模型才能代表主链路健康度）
#
# 单测：tests/custom/test_brain_watchdog.sh（纯 env 驱动，mock _bw_now /
# _bw_send_alert / _bw_restart_serve / _bw_gateway_ok / _bw_gateway_infer_ok，
# 不碰真实配置/网络/serve）。

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

# 加载本地配置（与 event_freshness_watchdog.sh 同款兜底；BRAIN_WATCHDOG_SKIP_LOCAL=1
# 供单测跳过）
if [[ -z "${BRAIN_WATCHDOG_SKIP_LOCAL:-}" && -f "$SCRIPT_DIR/config/constants.local.sh" ]]; then
    # shellcheck disable=SC1091
    source "$SCRIPT_DIR/config/constants.local.sh"
fi

: "${BRAIN_SERVE_CHECK_INTERVAL:=300}"
: "${BRAIN_SERVE_PAIR_WINDOW:=3600}"
: "${BRAIN_SERVE_FRESH_WINDOW:=900}"
: "${BRAIN_SERVE_MIN_PAIRS:=2}"
: "${BRAIN_SERVE_RETRY_INTERVAL:=1800}"
: "${BRAIN_SERVE_MAX_RESTARTS:=3}"
: "${BRAIN_GATEWAY_URL:=}"
: "${BRAIN_GATEWAY_TIMEOUT:=4}"
: "${BRAIN_GATEWAY_INFER_PROBE:=1}"
: "${BRAIN_GATEWAY_INFER_TIMEOUT:=25}"
: "${BRAIN_GATEWAY_INFER_CACHE_TTL:=60}"
: "${BRAIN_GATEWAY_APIKEY:=}"
: "${BRAIN_GATEWAY_PROBE_MODEL:=}"
: "${AGENT_OPENCODE_MODEL:=}"
: "${MONITOR_LOG:=$SCRIPT_DIR/monitor.log}"
: "${BRAIN_SERVE_STATE:=$SCRIPT_DIR/.brain-serve-stall.state}"
: "${DWS_EVENT_O2O_USERS:=}"
: "${DWS_PROFILE:=}"

_bw_log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] [brain-wd] $*" >> "$MONITOR_LOG" 2>/dev/null || true
}

# 当前 epoch（单测覆盖此函数注入固定时间）
_bw_now() { date +%s; }

# 扫描 monitor.log 的差分标记。输出 "poisoned\tcli_fail\tlast_http\tlast_poison"：
#   poisoned  窗口内「CLI 回退成功」条数（= serve 失败且 CLI 通的配对数）
#   cli_fail  窗口内「CLI 回退失败」条数
#   last_http 最后一条 http err 的 epoch（0=无记录；不受窗口限制，供新鲜度判定）
#   last_poison 最后一条「CLI 回退成功」的 epoch（0=无）
# 日志缺失/解析失败由 _bw_check_once 的正则守卫兜成全 0（unknown）。
_bw_scan() {
    local now="$1" window="$2"
    # 注意：grep 无匹配退出码 1（pipefail 会传导），故不挂 || 兜底输出（会追加
    # 第二行污染 read）；python 对空输入天然输出全 0。
    grep -aE 'brain opencode http err|CLI 回退失败|CLI 回退成功' "$MONITOR_LOG" 2>/dev/null | tail -400 | \
    NOW="$now" WINDOW="$window" \
    python3 -c '
import sys, os, re, time
now = int(os.environ.get("NOW", "0"))
window = int(os.environ.get("WINDOW", "3600"))
ts_re = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]")
poisoned = cli_fail = last_http = last_poison = 0
for line in sys.stdin:
    m = ts_re.match(line)
    if not m:
        continue
    try:
        ts = int(time.mktime(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")))
    except ValueError:
        continue
    if "brain opencode http err" in line:
        last_http = ts
    elif "CLI 回退成功" in line:
        # CLI 只在 serve 失败后才会被调用，成功即构成一对差分证据
        last_poison = ts
        if now - ts <= window:
            poisoned += 1
    elif "CLI 回退失败" in line:
        if now - ts <= window:
            cli_fail += 1
print("%d\t%d\t%d\t%d" % (poisoned, cli_fail, last_http, last_poison))
' 2>/dev/null
}

# 从 opencode 配置里 grep 出第一个 "key": "value"（baseURL / apiKey 共用）。
_bw_cfg_field() {
    local field="$1" f v
    for f in "$HOME/.config/opencode/opencode.json" "$HOME/.config/opencode/opencode.jsonc" \
             "$SCRIPT_DIR/opencode.json" "$SCRIPT_DIR/opencode.jsonc"; do
        [[ -f "$f" ]] || continue
        v="$(grep -oE "\"${field}\"[[:space:]]*:[[:space:]]*\"[^\"]+\"" "$f" 2>/dev/null | head -1 \
             | sed -E "s/.*\"${field}\"[[:space:]]*:[[:space:]]*\"([^\"]+)\".*/\1/")" || true
        [[ -n "$v" ]] && { echo "$v"; return 0; }
    done
    return 0
}

# 网关 baseURL（不含 /models）。显式 BRAIN_GATEWAY_URL 优先（自动剥掉尾部 /models），
# 否则从 opencode 配置发现第一个 baseURL。结果按进程缓存。空 = 未配置。
_BW_GW_BASE=""
_bw_gateway_base() {
    if [[ -n "$_BW_GW_BASE" ]]; then
        echo "$_BW_GW_BASE"; return 0
    fi
    local url=""
    if [[ -n "$BRAIN_GATEWAY_URL" ]]; then
        url="$BRAIN_GATEWAY_URL"
    else
        url="$(_bw_cfg_field baseURL)"
    fi
    [[ -z "$url" ]] && return 0
    url="${url%/}"
    url="${url%/models}"    # 显式配到 .../v1/models 也能还原出 base
    _BW_GW_BASE="$url"
    echo "$_BW_GW_BASE"
}

# 网关探测 URL（显式 BRAIN_GATEWAY_URL 原样返回，保持旧语义；否则 base + /models）。
_bw_gateway_url() {
    if [[ -n "$BRAIN_GATEWAY_URL" ]]; then
        echo "$BRAIN_GATEWAY_URL"; return 0
    fi
    local base
    base="$(_bw_gateway_base)"
    [[ -z "$base" ]] && return 0
    echo "${base}/models"
}

# 网关 apiKey（显式 BRAIN_GATEWAY_APIKEY 优先，否则从 opencode 配置发现）。
# L2 推理探测必须带 key：不带 key 的 401 无法区分「鉴权层活着」和「推理层活着」。
_BW_GW_KEY=""
_bw_gateway_key() {
    if [[ -n "$BRAIN_GATEWAY_APIKEY" ]]; then
        echo "$BRAIN_GATEWAY_APIKEY"; return 0
    fi
    if [[ -n "$_BW_GW_KEY" ]]; then
        echo "$_BW_GW_KEY"; return 0
    fi
    _BW_GW_KEY="$(_bw_cfg_field apiKey)"
    [[ -n "$_BW_GW_KEY" ]] && echo "$_BW_GW_KEY"
    return 0
}

# L2 探测用的模型 id（BRAIN_GATEWAY_PROBE_MODEL 优先；否则取 AGENT_OPENCODE_MODEL
# 的 provider/ 后半段——必须探主模型，探别的模型不能代表主链路健康度）。
_bw_probe_model() {
    if [[ -n "$BRAIN_GATEWAY_PROBE_MODEL" ]]; then
        echo "$BRAIN_GATEWAY_PROBE_MODEL"; return 0
    fi
    local m="$AGENT_OPENCODE_MODEL"
    [[ -z "$m" ]] && return 0
    echo "${m#*/}"
}

# L1 路由层探测（零 token：只到路由/鉴权层，不产生推理）。返回码：
#   0 = 可达（任何 HTTP 状态码，含 401/404）；1 = 不可达（000/超时）；2 = 未配置
# 注意：L1 通**不等于**能推理——LiteLLM 前置进程活着就会回 401/200，推理后端挂死
# 时 L1 依然「可达」。判「该不该重启 serve」必须再看 L2。
_bw_gateway_ok() {
    local url status
    url="$(_bw_gateway_url)"
    [[ -z "$url" ]] && return 2
    status="$(curl -s -m "$BRAIN_GATEWAY_TIMEOUT" -o /dev/null -w '%{http_code}' \
              "$url" 2>/dev/null || echo 000)"
    [[ -n "$status" && "$status" != "000" ]] && return 0
    return 1
}

# L2 推理层探测（近零 token：一条 max_tokens=1 的 ping，实测 ~70 token/次）。
#
# 为什么必须有 L2（2026-09-25 事故）：07:58–08:32、10:02–10:10 两个时段 opencode
# 调模型全部 `Cannot connect to API` 63~68s 超时、serve+CLI 双路全挂，而 L1 全程报
# gw_rc=0「可达」→ 看门狗把推理层故障误分类成 opencode_net，告警让人去查代理/DNS/
# 防火墙（三样都没问题），且按「重启无效」一律不动手，白拖 46min / 13min。
#
# 返回码：
#   0 = 推理层通（200；429 也算通——网关在处理请求，限流不是 serve 的问题）
#   1 = 路由通但推理层无响应（5xx / 000 / 超时）→ 重启 serve 无效，等网关恢复
#   2 = 无法探测（开关关闭，或 baseURL/apiKey/model 缺失，或 401/403/404 配置问题）
_BW_INFER_TS=0
_BW_INFER_RC=2
_bw_gateway_infer_ok() {
    [[ "$BRAIN_GATEWAY_INFER_PROBE" == "1" ]] || return 2
    local now base key model status body
    now="$(_bw_now)"
    # 结果缓存：TTL 内重复调用不再打网关（省 token + 避免同轮多次探测互相干扰）
    if (( _BW_INFER_TS > 0 && (now - _BW_INFER_TS) < BRAIN_GATEWAY_INFER_CACHE_TTL )); then
        return "$_BW_INFER_RC"
    fi
    base="$(_bw_gateway_base)"
    key="$(_bw_gateway_key)"
    model="$(_bw_probe_model)"
    if [[ -z "$base" || -z "$key" || -z "$model" ]]; then
        _BW_INFER_TS="$now"; _BW_INFER_RC=2; return 2
    fi
    body="$(printf '{"model":"%s","messages":[{"role":"user","content":"ping"}],"max_tokens":1,"stream":false}' "$model")"
    status="$(curl -s -m "$BRAIN_GATEWAY_INFER_TIMEOUT" -o /dev/null -w '%{http_code}' \
              -X POST "${base}/chat/completions" \
              -H 'Content-Type: application/json' \
              -H "Authorization: Bearer ${key}" \
              -d "$body" 2>/dev/null || echo 000)"
    _BW_INFER_TS="$now"
    case "$status" in
        200|429)          _BW_INFER_RC=0 ;;
        401|403|404|405)  _BW_INFER_RC=2 ;;   # 探测配置问题，不能当成推理层故障
        *)                _BW_INFER_RC=1 ;;
    esac
    return "$_BW_INFER_RC"
}

# 清空 L2 探测缓存（重启 serve 后调用：下一轮必须重新实测，不能吃旧结论）
_bw_infer_cache_reset() { _BW_INFER_TS=0; _BW_INFER_RC=2; }

# 防抖状态（三行：last_action_epoch / last_alert_epoch / restart_count）
_BW_LAST_ACTION=0
_BW_LAST_ALERT=0
_BW_RESTART_COUNT=0
_bw_state_load() {
    _BW_LAST_ACTION=0; _BW_LAST_ALERT=0; _BW_RESTART_COUNT=0
    [[ -f "$BRAIN_SERVE_STATE" ]] || return 0
    local l1 l2 l3
    l1="$(sed -n '1p' "$BRAIN_SERVE_STATE" 2>/dev/null)" || true
    l2="$(sed -n '2p' "$BRAIN_SERVE_STATE" 2>/dev/null)" || true
    l3="$(sed -n '3p' "$BRAIN_SERVE_STATE" 2>/dev/null)" || true
    [[ "$l1" =~ ^[0-9]+$ ]] && _BW_LAST_ACTION="$l1"
    [[ "$l2" =~ ^[0-9]+$ ]] && _BW_LAST_ALERT="$l2"
    [[ "$l3" =~ ^[0-9]+$ ]] && _BW_RESTART_COUNT="$l3"
}
_bw_state_save() {
    printf '%s\n%s\n%s\n' "$_BW_LAST_ACTION" "$_BW_LAST_ALERT" "$_BW_RESTART_COUNT" \
        > "$BRAIN_SERVE_STATE" 2>/dev/null || true
}

# 告警（单测覆盖此函数 mock）。必须永不失败（看门狗不能被通知通道故障杀死）。
_bw_send_alert() {
    local text="$1"
    [[ -n "${DWS_PROFILE:-}" && -n "${DWS_EVENT_O2O_USERS:-}" ]] || return 0
    local to="${DWS_EVENT_O2O_USERS%%,*}"
    [[ -n "$to" ]] || return 0
    command -v dws >/dev/null 2>&1 || return 0
    dws chat message send --user "$to" --text "$text" \
        --profile "$DWS_PROFILE" -y >/dev/null 2>&1 || true
    return 0
}

# 发告警（带冷却：距上次告警 >= BRAIN_SERVE_RETRY_INTERVAL 才发）。返回 1 = 冷却期内跳过。
_bw_alert() {
    local text="$1" now="$2"
    if (( _BW_LAST_ALERT > 0 && (now - _BW_LAST_ALERT) < BRAIN_SERVE_RETRY_INTERVAL )); then
        return 1
    fi
    _BW_LAST_ALERT="$now"
    _bw_send_alert "$text"
    return 0
}

# 重启 serve（单测覆盖此函数 mock）。只动 serve，不动 connect/订阅。
# verify_pid 防 PID 复用误杀；复用 canonical 启动链（setup_components → custom
# start_serve）：.serve.pwd 保持稳定（in-flight 401 风险最小）、AGENT_DEBUG 日志参数一致。
_bw_restart_serve() {
    # shellcheck disable=SC1091
    source "$SCRIPT_DIR/bin/core/lib.sh"
    local pid
    if verify_pid "$SCRIPT_DIR/.serve.pid" "opencode serve"; then
        pid="$(cat "$SCRIPT_DIR/.serve.pid" 2>/dev/null || true)"
        kill_tree "$pid" TERM 2>/dev/null || true
        sleep 3
        if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
            kill_tree "$pid" KILL 2>/dev/null || true
        fi
    fi
    rm -f "$SCRIPT_DIR/.serve.pid" 2>/dev/null || true
    # shellcheck disable=SC1091
    setup_components
    start_serve
    _bw_infer_cache_reset    # 重启后下一轮必须重新实测网关，不能吃旧结论
    return 0
}

# 主判定（单测核心）。返回状态字符串（stdout）：
#   unknown          日志无 http 失败记录（新部署/日志轮转），静默等待
#   ok               窗口内无 http 失败（serve 健康/无流量），计数清零
#   suspect          有 1 对差分证据或证据陈旧：告警观察，不重启
#   poisoned_reboot  serve 单侧故障实锤：告警 + 重启 serve 自愈
#   poisoned_wait    实锤但处于自愈冷却期：仅告警
#   poisoned_giveup  实锤且已达自愈上限：仅告警，等人工
#   gateway_down     L1 路由层不可达：仅告警（网关整体挂，重启无效，等恢复）
#   gateway_infer_down  L1 通但 L2 推理层无响应：仅告警（推理后端故障，重启无效）
#   serve_stale_reboot  L1+L2 均通但双路全挂：故障在 serve 侧 → 告警 + 重启自愈
#   serve_stale_wait    同上但处于自愈冷却期：仅告警
#   serve_stale_giveup  同上且已达自愈上限：仅告警，等人工
#   opencode_net     L1 通但 L2 无法探测（缺 key/模型、探测关闭、401/404）：仅告警
#   both_fail        双路全挂 + 网关探测未配置：仅告警
#   *_quiet          同上但处于告警冷却期（跳过告警）
_bw_check_once() {
    local now="$(_bw_now)"
    local res poisoned cli_fail last_http last_poison
    res="$(_bw_scan "$now" "$BRAIN_SERVE_PAIR_WINDOW")"
    IFS=$'\t' read -r poisoned cli_fail last_http last_poison <<< "$res"
    [[ "$poisoned" =~ ^[0-9]+$ ]] || poisoned=0
    [[ "$cli_fail" =~ ^[0-9]+$ ]] || cli_fail=0
    [[ "$last_http" =~ ^[0-9]+$ ]] || last_http=0
    [[ "$last_poison" =~ ^[0-9]+$ ]] || last_poison=0

    if (( last_http == 0 )); then
        echo "unknown"; return 0
    fi

    local http_age=$(( now - last_http ))
    if (( http_age > BRAIN_SERVE_PAIR_WINDOW )); then
        _bw_state_load
        if (( _BW_RESTART_COUNT > 0 )); then
            _BW_RESTART_COUNT=0; _BW_LAST_ACTION=0; _BW_LAST_ALERT=0
            _bw_state_save
            _bw_log "大脑 serve 近 ${BRAIN_SERVE_PAIR_WINDOW}s 无失败记录，看门狗计数清零"
        fi
        echo "ok"; return 0
    fi

    # ---- serve 单侧故障（CLI 回退成功 = 网关对全新进程可达，只有 serve 连不上）----
    local poison_age=$(( last_poison > 0 ? now - last_poison : 999999999 ))
    if (( poisoned >= BRAIN_SERVE_MIN_PAIRS && poison_age <= BRAIN_SERVE_FRESH_WINDOW )); then
        _bw_state_load
        if (( _BW_RESTART_COUNT >= BRAIN_SERVE_MAX_RESTARTS )); then
            if _bw_alert "🚨 数字员工大脑 serve 连接异常持续（窗口内 ${poisoned} 次 CLI 回退成功佐证），已自动重启 ${_BW_RESTART_COUNT} 次仍未恢复，暂停自愈等人工。" "$now"; then
                _bw_state_save
            fi
            _bw_log "serve 单侧故障 evidence=${poisoned}：已达自愈上限(${BRAIN_SERVE_MAX_RESTARTS})，仅告警"
            echo "poisoned_giveup"; return 0
        fi
        if (( _BW_LAST_ACTION > 0 && (now - _BW_LAST_ACTION) < BRAIN_SERVE_RETRY_INTERVAL )); then
            if _bw_alert "🚨 数字员工大脑 serve 连接异常持续（${poisoned} 次 CLI 回退成功佐证），自动重启冷却中（${BRAIN_SERVE_RETRY_INTERVAL}s 内不重复重启）。期间消息由 CLI 兜底回复。" "$now"; then
                _bw_state_save
            fi
            _bw_log "serve 单侧故障 evidence=${poisoned}：自愈冷却期内，仅告警"
            echo "poisoned_wait"; return 0
        fi
        _BW_RESTART_COUNT=$(( _BW_RESTART_COUNT + 1 ))
        _BW_LAST_ACTION="$now"
        _BW_LAST_ALERT="$now"
        _bw_state_save
        _bw_log "serve 单侧故障 evidence=${poisoned}：告警 + 重启 serve（第 ${_BW_RESTART_COUNT}/${BRAIN_SERVE_MAX_RESTARTS} 次）"
        _bw_send_alert "🚨 数字员工大脑 serve 连接异常（${poisoned} 次 CLI 回退成功佐证网关可达，serve 出站连接池疑似毒化），正在自动重启 serve 自愈（第 ${_BW_RESTART_COUNT} 次，上限 ${BRAIN_SERVE_MAX_RESTARTS}）。期间消息由 CLI 兜底回复。"
        _bw_restart_serve
        echo "poisoned_reboot"; return 0
    fi

    # ---- 证据不足（仅 1 对）：告警观察，不重启 ----
    # 要求证据新鲜（最近毒化对或最近 http 失败任一在 FRESH_WINDOW 内）：全部陈旧时
    # 走下面的恢复判定，防止 reboot/看门狗重启后拿旧事故刷屏（2026-09-21 首启实测踩到）
    local http_fresh=$(( now - last_http <= BRAIN_SERVE_FRESH_WINDOW ? 1 : 0 ))
    if (( poisoned >= 1 && (poison_age <= BRAIN_SERVE_FRESH_WINDOW || http_fresh) )); then
        _bw_state_load
        if _bw_alert "⚠️ 数字员工大脑 serve 疑似连接异常（CLI 回退成功 ${poisoned} 次），继续观察中（新鲜证据达 ${BRAIN_SERVE_MIN_PAIRS} 次将自动重启 serve）。" "$now"; then
            _bw_state_save
            _bw_log "疑似 serve 单侧故障 evidence=${poisoned}：证据不足，告警观察"
            echo "suspect"; return 0
        fi
        _bw_log "疑似 serve 单侧故障 evidence=${poisoned}：证据不足，告警冷却期内跳过"
        echo "suspect_quiet"; return 0
    fi

    # ---- 证据陈旧（最近失败超出新鲜窗口）----
    # 只用 L1 独立确证网关是否还整体挂着（不依赖流量、零 token）：仍不可达 → 持续
    # 告警；可达 → 视为已恢复（旧故障已过，若有新失败会刷新证据重新进上面的分支），
    # 静默。这里**不跑 L2**：无新鲜故障就没有重启决策要做，不必花 token。
    if (( ! http_fresh )); then
        local gw0=0
        _bw_gateway_ok; gw0=$?
        if (( gw0 == 1 )); then
            _bw_state_load
            if _bw_alert "⚠️ 模型网关仍不可达（curl L1 独立确证；最近失败证据 ${http_age}s 前），数字员工大脑降级中。" "$now"; then
                _bw_state_save
                _bw_log "失败证据陈旧(age=${http_age}s)且网关 L1 不可达：仍告警"
                echo "gateway_down_stale"; return 0
            fi
            _bw_log "失败证据陈旧(age=${http_age}s)且网关 L1 不可达：告警冷却期内跳过"
            echo "gateway_down_stale_quiet"; return 0
        fi
        _bw_log "失败证据陈旧(age=${http_age}s)且网关 L1 可达(rc=${gw0})：视为已恢复，静默"
        echo "recovered"; return 0
    fi

    # ---- 双路全挂（新鲜证据）：L1 路由 + L2 推理 两级差分定位 ----
    #
    # 旧实现只有 L1 且一律不重启，2026-09-25 事故证明两处都错：
    #   ① L1（/models 不带 key，401 即判「可达」）只证明 LiteLLM 前置进程活着，
    #      推理后端挂死时 L1 全程 gw_rc=0 → 误分类成 opencode_net，告警让人去查
    #      代理/DNS/防火墙（实测三样都正常，无 proxy env、curl 直连 17ms）。
    #   ② 「双路全挂 → 重启无效」被日志证伪：每次都是重启 serve 后立刻恢复
    #      （08:32:13 最后失败 → 08:44:58 重启 → 08:45:23 成功 4.06s；
    #        10:10:39 最后失败 → 10:23:37 重启 → 10:26:27 成功 5s），
    #      按旧策略白拖 46min / 13min。
    # 新矩阵：L1 挂 → 网关不可达；L1 通 + L2 挂 → 推理后端故障（重启确实无效）；
    #         L1 通 + L2 通 → 网关健康、故障在 serve 侧 → **重启自愈**。
    local gw=0 inf=2
    _bw_gateway_ok; gw=$?
    if (( gw == 0 )); then
        _bw_gateway_infer_ok; inf=$?
    fi

    # L1 通 + L2 通：网关推理层此刻确实能出 token，opencode 双路却都连不上
    # → 故障在 serve/连接层，重启有效（与 poisoned_reboot 同款防抖状态机）
    if (( gw == 0 && inf == 0 )); then
        local ev="双路全挂但网关推理探测通(L1=0,L2=0)：serve 侧故障"
        _bw_state_load
        if (( _BW_RESTART_COUNT >= BRAIN_SERVE_MAX_RESTARTS )); then
            if _bw_alert "🚨 数字员工大脑 serve 与 CLI 双路全挂，但网关推理探测正常（问题在 serve 侧）。已自动重启 ${_BW_RESTART_COUNT} 次仍未恢复，暂停自愈等人工。" "$now"; then
                _bw_state_save
            fi
            _bw_log "${ev}：已达自愈上限(${BRAIN_SERVE_MAX_RESTARTS})，仅告警"
            echo "serve_stale_giveup"; return 0
        fi
        if (( _BW_LAST_ACTION > 0 && (now - _BW_LAST_ACTION) < BRAIN_SERVE_RETRY_INTERVAL )); then
            if _bw_alert "🚨 数字员工大脑 serve 与 CLI 双路全挂，但网关推理探测正常（问题在 serve 侧）。自动重启冷却中（${BRAIN_SERVE_RETRY_INTERVAL}s 内不重复重启），期间消息走兜底提示。" "$now"; then
                _bw_state_save
            fi
            _bw_log "${ev}：自愈冷却期内，仅告警"
            echo "serve_stale_wait"; return 0
        fi
        _BW_RESTART_COUNT=$(( _BW_RESTART_COUNT + 1 ))
        _BW_LAST_ACTION="$now"
        _BW_LAST_ALERT="$now"
        _bw_state_save
        _bw_log "${ev}：告警 + 重启 serve（第 ${_BW_RESTART_COUNT}/${BRAIN_SERVE_MAX_RESTARTS} 次）"
        _bw_send_alert "🚨 数字员工大脑 serve 与 CLI 双路全挂，但网关推理探测正常（L1+L2 均通，故障在 serve 侧而非网关/网络），正在自动重启 serve 自愈（第 ${_BW_RESTART_COUNT} 次，上限 ${BRAIN_SERVE_MAX_RESTARTS}）。期间消息走兜底提示。"
        _bw_restart_serve
        echo "serve_stale_reboot"; return 0
    fi

    local msg st
    case "${gw}:${inf}" in
        # L1 挂：网关整体不可达，重启 serve 无效（等网关恢复）
        1:*)  msg="⚠️ 模型网关不可达（curl L1 路由层失败），数字员工大脑降级 CLI/兜底提示中，自动重启无效。网关恢复后下一条消息将自动触发 serve 自愈。"
              st="gateway_down" ;;
        # L1 通 + L2 挂：网关进程活着但推理后端无响应——重启 serve 同样无效，
        # 但定位完全不同于旧的 opencode_net（不用再查代理/DNS/防火墙）
        0:1)  msg="⚠️ 模型网关路由可达但**推理层无响应**（L2 真实推理探测失败，非鉴权/配置问题）。数字员工大脑降级中，重启 serve 无效，需查网关侧模型后端。恢复后下一条消息自动触发自愈。"
              st="gateway_infer_down" ;;
        # L1 通 + L2 无法探测：退回旧语义（保守不重启，人工确认）
        0:*)  msg="⚠️ 数字员工大脑 serve 与 CLI 双路全挂，网关路由可达但推理层探测无法完成（apiKey/模型未配、探测关闭或 401/404）。请人工确认网关推理是否正常；若正常则重启 serve 可自愈。"
              st="opencode_net" ;;
        *)    msg="⚠️ 数字员工大脑 serve 与 CLI 双路全挂（BRAIN_GATEWAY_URL 未配置，无法差分定位网关），请人工检查。"
              st="both_fail" ;;
    esac
    _bw_state_load
    if _bw_alert "$msg" "$now"; then
        _bw_state_save
        _bw_log "双路全挂 L1=${gw} L2=${inf}：${st}，仅告警（重启无效）"
        echo "$st"; return 0
    fi
    _bw_log "双路全挂 L1=${gw} L2=${inf}：${st}，告警冷却期内跳过"
    echo "${st}_quiet"; return 0
}

# 可被信号打断的 sleep（5s 小块，与 monitor.sh 同款：TERM 最多延迟 ~5s 生效）
_bw_interruptible_sleep() {
    local remaining="$1"
    local step=5
    while (( remaining > 0 )); do
        (( remaining < step )) && step="$remaining"
        sleep "$step"
        remaining=$(( remaining - step ))
    done
}

bw_main_loop() {
    _bw_log "大脑看门狗启动: interval=${BRAIN_SERVE_CHECK_INTERVAL}s window=${BRAIN_SERVE_PAIR_WINDOW}s fresh=${BRAIN_SERVE_FRESH_WINDOW}s min_pairs=${BRAIN_SERVE_MIN_PAIRS} retry=${BRAIN_SERVE_RETRY_INTERVAL}s max_restarts=${BRAIN_SERVE_MAX_RESTARTS}"
    trap 'exit 0' TERM INT
    while :; do
        _bw_check_once >/dev/null 2>&1 || true
        _bw_interruptible_sleep "$BRAIN_SERVE_CHECK_INTERVAL"
    done
}

if [[ "${BASH_SOURCE[0]:-$0}" == "$0" ]]; then
    bw_main_loop
fi
