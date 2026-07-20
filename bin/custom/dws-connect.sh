#!/bin/bash
# dws-connect.sh — connect 组件包装：dws event consume → bridge → CONNECT_LOG
#
# 由 start_connect（bin/custom/start_funcs.sh）拉起。作为独立命名脚本存在，使进程
# cmdline 签名稳定可被 verify_pid 匹配（模式 'dws-connect.sh'），且能承载管道
# （_spawn 只能跑单条命令，管道需要包在脚本里）。
#
# 订阅群消息 / 单聊(o2o) / @我的消息(at)，转成 connect-log 格式喂给 event_watcher 的
# log-tail。**敏感值不写死在本文件**，从环境变量读取，真实值放在 gitignored 的
# config/constants.local.sh：
#   DWS_EVENT_KEY        群消息事件类型（默认 user_im_message_receive_group）
#   DWS_EVENT_GROUP      群 openConversationId（订阅群消息时必填）—— 敏感
#   DWS_EVENT_O2O_USERS  订阅单聊时：对端 userId 列表（逗号分隔）。留空=不订阅单聊。
#                        钉钉 o2o 事件只能按“对端 userId”订阅（每个对端一条订阅），
#                        故这里为每个 userId 起一个 o2o consumer。
#   DWS_EVENT_AT         订阅“@我的消息”（当前数字员工账号被 @ 的消息，跨所有群）。
#                        1/true/yes/on=开。事件类型 user_im_message_receive_at，
#                        rule_type=at 无需 group/user 参数（个人级订阅）。用于只在被
#                        @ 时才响应、且不想逐个配置群/对端的场景。留空=不订阅。
#   DWS_PROFILE          组织 profile（数字员工账号）—— 敏感
#
# 三种可任意组合同时开：分别起 consumer，都把输出汇到同一个 bridge 管道 → CONNECT_LOG。
# 至少要开一种（群 / 单聊 / @我），否则报错退出。
# 注意：同一条消息若同时命中多个订阅（如被订阅群里 @ 你，group+at 都会投递），bridge
# 产出的行含相同 msgId，能力层按 msgId 去重（text_reply/forward 的 _seen），不会重复处理。
#
# 调试：DWS_CONNECT_DRY_RUN=1 时只打印“计划订阅哪些 consumer”（敏感 group/user 脱敏为
# 长度）并退出 0，不连接 bus，便于单测订阅选择/校验逻辑。

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

# 加载本地敏感常量（gitignored）。start_connect 经 monitor 已 source 过，这里兜底再 source
# 一次，便于直接手工运行本脚本调试。DWS_CONNECT_SKIP_LOCAL=1 可跳过（单测用纯 env 驱动
# 订阅选择逻辑，不让本地真实配置干扰）。
if [[ -z "${DWS_CONNECT_SKIP_LOCAL:-}" && -f "$SCRIPT_DIR/config/constants.local.sh" ]]; then
    # shellcheck disable=SC1091
    source "$SCRIPT_DIR/config/constants.local.sh"
fi

: "${DWS_EVENT_KEY:=user_im_message_receive_group}"
: "${DWS_EVENT_GROUP:=}"
: "${DWS_EVENT_O2O_USERS:=}"
: "${DWS_EVENT_AT:=}"
: "${DWS_PROFILE:=}"
: "${CONNECT_LOG:=$SCRIPT_DIR/agent-connect.log}"

if [[ -z "$DWS_PROFILE" ]]; then
    echo "[connect] ERROR: DWS_PROFILE 未设置（请在 config/constants.local.sh 填）" >> "$CONNECT_LOG"
    exit 1
fi

# _is_on：把常见真值（1/true/yes/on，大小写不敏感）判为“开”。
_is_on() {
    case "$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')" in
        1|true|yes|on) return 0 ;;
        *) return 1 ;;
    esac
}

# 判定要开哪些订阅
_want_group=0
[[ "$DWS_EVENT_KEY" == *group* && -n "$DWS_EVENT_GROUP" ]] && _want_group=1
_want_o2o=0
[[ -n "$DWS_EVENT_O2O_USERS" ]] && _want_o2o=1
_want_at=0
_is_on "$DWS_EVENT_AT" && _want_at=1

if [[ "$DWS_EVENT_KEY" == *group* && -z "$DWS_EVENT_GROUP" \
      && "$_want_o2o" -eq 0 && "$_want_at" -eq 0 ]]; then
    echo "[connect] ERROR: 群订阅需要 DWS_EVENT_GROUP（或改用 DWS_EVENT_O2O_USERS 单聊 / DWS_EVENT_AT @我）" >> "$CONNECT_LOG"
    exit 1
fi
if [[ "$_want_group" -eq 0 && "$_want_o2o" -eq 0 && "$_want_at" -eq 0 ]]; then
    echo "[connect] ERROR: 未配置任何订阅（DWS_EVENT_GROUP 群 / DWS_EVENT_O2O_USERS 单聊 / DWS_EVENT_AT @我 至少一个）" >> "$CONNECT_LOG"
    exit 1
fi

BRIDGE="$SCRIPT_DIR/bin/custom/dws_event_bridge.py"

# _run_consumers：把所有要开的 consumer 的 stdout 合流输出（供管道喂 bridge）。
# 每个 consumer 的 stderr（[event] ready / 状态）汇入 CONNECT_LOG 便于健康检查看活跃度。
# 子进程都在本函数的子 shell 里，脚本退出（SIGTERM）时随之被清理。
_run_consumers() {
    local pids=()
    # 群消息 consumer
    if [[ "$_want_group" -eq 1 ]]; then
        dws event consume "$DWS_EVENT_KEY" --group "$DWS_EVENT_GROUP" \
            --profile "$DWS_PROFILE" -f ndjson --quiet 2>>"$CONNECT_LOG" &
        pids+=($!)
    fi
    # 单聊 consumer：每个对端 userId 一个（o2o 只能按对端订阅）
    if [[ "$_want_o2o" -eq 1 ]]; then
        local IFS=','
        local u
        for u in $DWS_EVENT_O2O_USERS; do
            u="${u// /}"   # 去空格
            [[ -z "$u" ]] && continue
            dws event consume user_im_message_receive_o2o --user "$u" \
                --profile "$DWS_PROFILE" -f ndjson --quiet 2>>"$CONNECT_LOG" &
            pids+=($!)
        done
    fi
    # @我 consumer：个人级订阅（rule_type=at，无需 group/user 参数），捕获当前数字员工
    # 账号在任意群被 @ 的消息。bridge 已把 user_im_message_receive_at 映射为 convType=2。
    if [[ "$_want_at" -eq 1 ]]; then
        dws event consume user_im_message_receive_at \
            --profile "$DWS_PROFILE" -f ndjson --quiet 2>>"$CONNECT_LOG" &
        pids+=($!)
    fi
    # 任一 consumer 退出即整体结束，让 monitor 兜底重启（bash 3.2 无 `wait -n`，用轮询：
    # 任一子进程不再存活就 kill 其余并返回）。
    while :; do
        local alive=0 p
        for p in "${pids[@]}"; do
            if kill -0 "$p" 2>/dev/null; then
                alive=$((alive + 1))
            fi
        done
        # 有 consumer 死了（存活数 < 总数）→ 收尾
        if [[ "$alive" -lt "${#pids[@]}" ]]; then
            for p in "${pids[@]}"; do kill "$p" 2>/dev/null; done
            break
        fi
        sleep 5
    done
}

# DWS_CONNECT_DRY_RUN：只打印订阅计划（脱敏）并退出，不连 bus。供单测校验订阅选择逻辑。
if _is_on "${DWS_CONNECT_DRY_RUN:-}"; then
    echo "plan: group=$_want_group o2o=$_want_o2o at=$_want_at"
    [[ "$_want_group" -eq 1 ]] && echo "consumer: $DWS_EVENT_KEY --group <len:${#DWS_EVENT_GROUP}>"
    if [[ "$_want_o2o" -eq 1 ]]; then
        IFS=',' read -ra _users <<< "$DWS_EVENT_O2O_USERS"
        for _u in "${_users[@]}"; do
            _u="${_u// /}"; [[ -z "$_u" ]] && continue
            echo "consumer: user_im_message_receive_o2o --user <len:${#_u}>"
        done
    fi
    [[ "$_want_at" -eq 1 ]] && echo "consumer: user_im_message_receive_at"
    exit 0
fi

# 启动日志（不打敏感 group/users，只记开了哪些）
echo "[connect] dws-connect 启动: group=$_want_group o2o=$_want_o2o at=$_want_at" >> "$CONNECT_LOG"

# 所有 consumer 合流 → bridge → CONNECT_LOG
_run_consumers | python3 "$BRIDGE" >> "$CONNECT_LOG" 2>>"$CONNECT_LOG"
