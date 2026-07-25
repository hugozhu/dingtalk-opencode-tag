#!/bin/bash
# reboot.sh — 远程重启指令执行体
#
# 提炼自: dingtalk-opencode-agent/reboot.sh (v4.1)
# 原作者: hugozhu
#
# 用途: 用户通过聊天发 /reboot 指令时，主进程派生本脚本（脱离进程组）+ os._exit(0)，
# 本脚本 1s 后调用 stop.sh 停止服务，再调用 start.sh 启动服务。
#
# 重构 (v4.2):
#   - 停止逻辑提取到 bin/core/stop.sh
#   - 启动逻辑提取到 bin/core/start.sh
#   - 本脚本作为薄编排层：sleep 1（避免杀刚派生的自己）→ stop → start
#   - 失败传播保留：start.sh 失败时 exit 1（非零让 launchd 拉起，双保险）

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
source "$SCRIPT_DIR/bin/core/lib.sh"

COMPONENT_NAME="reboot"

# 加载常量（env/PATH + REBOOT_RESTART_MODE / LAUNCHD_LABEL 等，供 stop/start 继承）
if [[ -f "$SCRIPT_DIR/config/constants.local.sh" ]]; then
    # shellcheck disable=SC1091
    source "$SCRIPT_DIR/config/constants.local.sh"
elif [[ -f "$SCRIPT_DIR/config/constants.sh" ]]; then
    # shellcheck disable=SC1091
    source "$SCRIPT_DIR/config/constants.sh"
fi

# 等待主进程退出（避免 stop.sh 的 pkill 误杀刚派生的本脚本）
sleep 1

# 用**干净环境**跑 stop/start（#71）：本脚本由老 event_watcher 派生，继承了老 monitor
# 启动时的全部 env。若直接透传，config 里 `export VAR="${VAR:-新值}"` 风格的赋值会被
# 继承的旧值压住——改完 config/constants.local.sh 后 /reboot 不生效，新 monitor / serve
# 仍带旧 env。env -i 只保留身份/路径基本量，stop/start 自己 source config，行为与
# 「开新终端手工全停全起」完全一致。
_env_clean() {
    env -i HOME="$HOME" USER="${USER:-$(id -un)}" LOGNAME="${LOGNAME:-$(id -un)}" \
        SHELL="${SHELL:-/bin/bash}" TMPDIR="${TMPDIR:-/tmp}" LANG="${LANG:-en_US.UTF-8}" \
        PATH="/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin" \
        "$@"
}

log "重启服务：调用 stop.sh..."
if ! _env_clean bash "$SCRIPT_DIR/bin/core/stop.sh"; then
    log "⚠️ stop.sh 返回非零，继续尝试启动"
fi

sleep 2

log "重启服务：调用 start.sh..."
if _env_clean bash "$SCRIPT_DIR/bin/core/start.sh"; then
    log "重启完成"
    exit 0
fi

log "⚠️ start.sh 失败"
exit 1  # 非零让 launchd 拉起（双保险）
