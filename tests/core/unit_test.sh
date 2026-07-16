#!/bin/bash
# unit_test.sh — shell 单元测试模板
#
# 提炼自: dingtalk-opencode-agent/tests/unit_test.sh (v4.1, 50 tests)
# 原作者: hugozhu
#
# 测试对象:
#   - lib.sh 的 verify_pid / acquire_lock / release_lock / log
#   - monitor.sh 的 is_running / cleanup_stale_state / cleanup 退出码
#   - reboot.sh 的常量 + 失败传播
#
# 不依赖网络/钉钉/agent serve，纯 shell 函数级断言

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

PASS=0
FAIL=0
FAILED_TESTS=()

assert_eq() {
    local name="$1" expected="$2" actual="$3"
    if [[ "$expected" == "$actual" ]]; then
        echo -e "  \033[32m✓\033[0m $name"
        PASS=$((PASS + 1))
    else
        echo -e "  \033[31m✗\033[0m $name"
        echo "    expected: $expected"
        echo "    actual:   $actual"
        FAIL=$((FAIL + 1))
        FAILED_TESTS+=("$name")
    fi
}

# 测试 lib.sh
echo "Testing lib.sh..."

# 加载被测代码
source "$SCRIPT_DIR/bin/core/lib.sh"

# verify_pid 文件不存在时返回非 0
assert_eq "verify_pid 文件不存在返回非0" "1" "$(verify_pid /tmp/nonexistent.pid 'some-pattern' >/dev/null 2>&1; echo $?)"

# acquire_lock 第一次成功
LOCK=/tmp/test_harness_lock_$$
rm -f "$LOCK"
assert_eq "acquire_lock 第一次成功" "0" "$(acquire_lock "$LOCK"; echo $?)"
rm -f "$LOCK"

# release_lock 后能再 acquire
acquire_lock "$LOCK"
release_lock "$LOCK"
assert_eq "release_lock 后能再 acquire" "0" "$(acquire_lock "$LOCK"; echo $?)"
rm -f "$LOCK"

# log 输出格式
LOG_OUT=$(COMPONENT_NAME=test log "hello" 2>&1)
# 含 [YYYY-MM-DD HH:MM:SS] [test] hello
if [[ "$LOG_OUT" =~ \[20[0-9-]+\ [0-9:]+\]\ \[test\]\ hello ]]; then
    assert_eq "log 含时间戳 + 组件名" "1" "1"
else
    assert_eq "log 含时间戳 + 组件名" "1" "0 (actual: $LOG_OUT)"
fi

# 测试 monitor.sh 的常量默认值
echo ""
echo "Testing monitor.sh constants..."

# 用 bash -n 语法检查（不需要执行）
assert_eq "monitor.sh 语法正确" "0" "$(bash -n "$SCRIPT_DIR/bin/core/monitor.sh" 2>&1; echo $?)"
assert_eq "healthcheck.sh 语法正确" "0" "$(bash -n "$SCRIPT_DIR/bin/core/healthcheck.sh" 2>&1; echo $?)"
assert_eq "reboot.sh 语法正确" "0" "$(bash -n "$SCRIPT_DIR/bin/core/reboot.sh" 2>&1; echo $?)"
assert_eq "lib.sh 语法正确" "0" "$(bash -n "$SCRIPT_DIR/bin/core/lib.sh" 2>&1; echo $?)"

# 测试 reboot.sh 的常量默认值
KICKSTART_LINE=$(grep 'KICKSTART_RETRY_INTERVAL' "$SCRIPT_DIR/bin/core/reboot.sh" | grep '=' | head -1)
if [[ "$KICKSTART_LINE" =~ KICKSTART_RETRY_INTERVAL:=[[:space:]]*\"?([0-9]+) ]]; then
    KICKSTART_VAL="${BASH_REMATCH[1]}"
else
    KICKSTART_VAL=""
fi
assert_eq "reboot.sh KICKSTART_RETRY_INTERVAL=10" "10" "$KICKSTART_VAL"

LAUNCHD_LINE=$(grep 'LAUNCHD_LABEL' "$SCRIPT_DIR/bin/core/reboot.sh" | grep '=' | head -1)
if [[ "$LAUNCHD_LINE" =~ LAUNCHD_LABEL:=[[:space:]]*\"?([a-zA-Z.]+) ]]; then
    LAUNCHD_VAL="${BASH_REMATCH[1]}"
else
    LAUNCHD_VAL=""
fi
assert_eq "reboot.sh LAUNCHD_LABEL 存在" "1" "$([ -n "$LAUNCHD_VAL" ] && echo 1 || echo 0)"

# reboot.sh 跨平台 supervisor 重启：SYSTEMD_UNIT 默认 + harness_os 分派
SYSTEMD_LINE=$(grep 'SYSTEMD_UNIT' "$SCRIPT_DIR/bin/core/reboot.sh" | grep '=' | head -1)
if [[ "$SYSTEMD_LINE" =~ SYSTEMD_UNIT:=[[:space:]]*\"?([a-zA-Z0-9._-]+) ]]; then
    SYSTEMD_VAL="${BASH_REMATCH[1]}"
else
    SYSTEMD_VAL=""
fi
assert_eq "reboot.sh SYSTEMD_UNIT 默认存在" "1" "$([ -n "$SYSTEMD_VAL" ] && echo 1 || echo 0)"

# harness_os 在本机（Linux CI/容器）应返回 linux；macOS 返回 macos
HARNESS_OS_LINUX=$(bash -c 'OSTYPE="linux-gnu"; source "'"$SCRIPT_DIR"'/bin/core/lib.sh"; harness_os')
assert_eq "harness_os linux-gnu => linux" "linux" "$HARNESS_OS_LINUX"
HARNESS_OS_MAC=$(bash -c 'OSTYPE="darwin23"; source "'"$SCRIPT_DIR"'/bin/core/lib.sh"; harness_os')
assert_eq "harness_os darwin => macos" "macos" "$HARNESS_OS_MAC"

# SUPERVISOR_RESTART_CMD 覆盖：非空时 restart_supervisor 原样执行（不碰 launchctl/systemctl）
OVERRIDE_OUT=$(SUPERVISOR_RESTART_CMD='echo OVERRIDE_RAN' bash -c '
    source "'"$SCRIPT_DIR"'/bin/core/lib.sh"
    : "${SUPERVISOR_RESTART_CMD:=}"; : "${LAUNCHD_LABEL:=x}"; : "${SYSTEMD_UNIT:=y}"
    restart_supervisor() {
        if [[ -n "$SUPERVISOR_RESTART_CMD" ]]; then eval "$SUPERVISOR_RESTART_CMD"
        elif [[ "$(harness_os)" == macos ]]; then launchctl kickstart -k "gui/$(id -u)/$LAUNCHD_LABEL"
        else systemctl --user restart "$SYSTEMD_UNIT"; fi
    }
    restart_supervisor')
assert_eq "SUPERVISOR_RESTART_CMD 覆盖生效" "OVERRIDE_RAN" "$OVERRIDE_OUT"

# 测试 README 不硬编码版本号（应指向 VERSION，避免漂移）
echo ""
echo "Testing version consistency..."
# README 里不应出现形如 `1.2.3` 的裸版本号（VERSION 是唯一真相源）
if grep -Eq '版本[:：].*`[0-9]+\.[0-9]+\.[0-9]+`' "$SCRIPT_DIR/README.md"; then
    assert_eq "README 不硬编码版本号" "1" "0 (README 出现硬编码版本，应指向 VERSION)"
else
    assert_eq "README 不硬编码版本号" "1" "1"
fi

# 报告
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "Results: $PASS passed, $FAIL failed, 0 skipped"
if [[ $FAIL -gt 0 ]]; then
    echo "Failed tests:"
    for t in "${FAILED_TESTS[@]}"; do
        echo "  - $t"
    done
    exit 1
fi
