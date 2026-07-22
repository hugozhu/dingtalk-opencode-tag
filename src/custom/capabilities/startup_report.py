"""startup_report — 服务启动报告能力（custom 插件）

服务启动后，自动向订阅单聊用户的主管发送详细的服务状态报告，包括：
- 服务启动时间
- 当前用户信息（数字员工身份）
- 订阅的单聊用户列表及其主管
- 各组件运行状态
- 配置概要
- 健康检查结果

设计要点：
- **启动时触发**：利用 on_startup hook（需在 event_watcher 启动时调用）
- **获取主管信息**：通过 dws contact user get 查询订阅用户的主管
- **发送详细报告**：使用 dws chat message send 发送到主管的单聊

开关：CAP_STARTUP_REPORT_ENABLED（默认开）。
"""

import json
import os
import subprocess
import time
from datetime import datetime

from core.agent_common import PROFILE, log, _run_cli
from core.capabilities import Capability, register

_ENABLED = os.environ.get("CAP_STARTUP_REPORT_ENABLED", "1") in ("1", "true", "yes", "on")


def _get_user_info(user_id):
    """获取用户详细信息，包括主管"""
    try:
        rc, stdout = _run_cli([
            "contact", "user", "get",
            "--user-ids", user_id,
            "--format", "json"
        ])
        if rc == 0 and stdout:
            data = json.loads(stdout)
            if data.get("success") and data.get("result"):
                return data["result"][0]
    except Exception as e:
        log(f"[startup_report] 获取用户信息失败 user_id={user_id}: {e}")
    return None


def _get_current_user():
    """获取当前数字员工的用户信息"""
    try:
        rc, stdout = _run_cli([
            "contact", "user", "get-self",
            "--format", "json"
        ])
        if rc == 0 and stdout:
            data = json.loads(stdout)
            if data.get("success") and data.get("result"):
                return data["result"][0]
    except Exception as e:
        log(f"[startup_report] 获取当前用户信息失败: {e}")
    return None


def _get_group_name(group_id):
    """获取群聊名称

    由于无法直接通过 group_id 查询群名，这里使用以下策略：
    1. 尝试从环境变量 DWS_EVENT_GROUP_NAME 读取（如果用户手动配置）
    2. 如果没有配置，返回简化的 ID 显示
    """
    try:
        # 策略1：从环境变量读取（用户可选配置）
        group_name = os.environ.get("DWS_EVENT_GROUP_NAME", "").strip()
        if group_name:
            return group_name

        # 策略2：返回简化的 ID 显示
        return f"群聊 ({group_id[:10]}...)"
    except Exception as e:
        log(f"[startup_report] 获取群聊名称失败 group_id={group_id}: {e}")
        return f"群聊 ({group_id[:10]}...)"


def _check_process_status(pid_file):
    """检查进程是否运行"""
    try:
        if not os.path.exists(pid_file):
            return "❌ 未启动"

        with open(pid_file) as f:
            pid = f.read().strip()

        if not pid:
            return "❌ PID 文件为空"

        # 检查进程是否存在
        try:
            # 发送信号 0 不会真正杀死进程，只是检查进程是否存在
            os.kill(int(pid), 0)
            return f"✅ 运行中 (PID: {pid})"
        except OSError:
            return f"⚠️ PID 文件存在但进程未运行 (PID: {pid})"
        except ValueError:
            return f"❌ PID 文件内容无效: {pid}"
    except Exception as e:
        return f"❌ 状态检查失败: {str(e)[:50]}"


def _get_component_status():
    """获取各组件运行状态"""
    script_dir = os.environ.get("PROJECT_DIR", os.getcwd())
    components = {
        "opencode serve": f"{script_dir}/.serve.pid",
        "dws connect": f"{script_dir}/.connect.pid",
        "event_watcher": f"{script_dir}/.event-watcher.pid"
    }

    status = []
    for name, pid_file in components.items():
        state = _check_process_status(pid_file)
        status.append(f"  • {name}: {state}")

    return "\n".join(status)


def _get_healthcheck_summary():
    """运行健康检查并返回摘要"""
    try:
        script_dir = os.environ.get("PROJECT_DIR", os.getcwd())
        result = subprocess.run(
            ["bash", f"{script_dir}/bin/core/healthcheck.sh"],
            capture_output=True,
            text=True,
            timeout=30
        )
        if result.returncode == 0:
            return "✅ 全部通过"
        else:
            lines = result.stdout.split('\n')
            failed = [l for l in lines if '❌' in l or 'FAIL' in l]
            if failed:
                return f"⚠️ 部分失败:\n  " + "\n  ".join(failed[:3])
            return "⚠️ 检查未完全通过"
    except Exception as e:
        return f"❌ 检查失败: {str(e)[:50]}"


def _build_report():
    """构建详细的服务状态报告"""
    startup_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 获取当前数字员工信息
    current_user = _get_current_user()
    if not current_user:
        return None

    org_model = current_user.get("orgEmployeeModel", {})
    user_name = org_model.get("orgUserName", "未知")
    user_id = org_model.get("userId", "未知")
    org_name = org_model.get("orgName", "未知")
    corp_id = org_model.get("corpId", "未知")

    # 获取数字员工的主管信息
    supervisor_name = org_model.get("orgMasterDisplayName", "")
    supervisor_id = org_model.get("orgMasterUserId", "")

    depts = org_model.get("depts", [])
    dept_info = depts[0] if depts else {}
    dept_name = dept_info.get("deptName", "未知")

    # 获取订阅的单聊用户列表
    o2o_users = os.environ.get("DWS_EVENT_O2O_USERS", "").strip()
    o2o_user_ids = [u.strip() for u in o2o_users.split(",") if u.strip()]

    # 构建 Markdown 格式报告（使用安全的 emoji）
    report_lines = [
        f"# 数字员工服务启动报告",
        "",
        f"> 启动时间：**{startup_time}**",
        "",
        "---",
        "",
        "## 数字员工信息",
        "",
        f"- **姓名**：{user_name}",
        f"- **用户ID**：`{user_id}`",
        f"- **所属组织**：{org_name}",
        f"- **企业ID**：`{corp_id}`",
        f"- **所在部门**：{dept_name}",
    ]

    # 显示数字员工的主管信息
    if supervisor_id:
        report_lines.append(f"- **汇报主管**：{supervisor_name} (`{supervisor_id}`)")
    else:
        report_lines.append(f"- **汇报主管**：无")

    report_lines.extend([
        "",
        "---",
        "",
        "## 订阅配置",
        "",
    ])

    # 群订阅
    group_id = os.environ.get("DWS_EVENT_GROUP", "").strip()
    if group_id:
        group_name = _get_group_name(group_id)
        report_lines.append(f"- **群聊订阅**：已启用")
        report_lines.append(f"  - 群聊名称：**{group_name}**")
        report_lines.append(f"  - 群聊ID：`{group_id}`")
    else:
        report_lines.append(f"- **群聊订阅**：未启用")

    # @我订阅
    at_enabled = os.environ.get("DWS_EVENT_AT", "").strip() in ("1", "true", "yes", "on")
    if at_enabled:
        report_lines.append(f"- **@我订阅**：已启用")
    else:
        report_lines.append(f"- **@我订阅**：未启用")

    # 单聊订阅详情
    if o2o_user_ids:
        report_lines.append(f"- **单聊订阅**：已启用（{len(o2o_user_ids)} 个用户）")
        report_lines.append("")
        report_lines.append("### 订阅用户列表")
        report_lines.append("")

        for uid in o2o_user_ids:
            user_info = _get_user_info(uid)
            if user_info:
                org_emp = user_info.get("orgEmployeeModel", {})
                name = org_emp.get("orgUserName", uid)
                report_lines.append(f"- {name} (`{uid}`)")
            else:
                report_lines.append(f"- 用户 `{uid}` (信息获取失败)")
    else:
        report_lines.append(f"- **单聊订阅**：未启用")

    report_lines.extend([
        "",
        "---",
        "",
        "## 组件运行状态",
        "",
    ])

    # 组件状态
    script_dir = os.environ.get("PROJECT_DIR", os.getcwd())
    components = {
        "OpenCode Serve": f"{script_dir}/.serve.pid",
        "DWS Connect": f"{script_dir}/.connect.pid",
        "Event Watcher": f"{script_dir}/.event-watcher.pid"
    }

    for name, pid_file in components.items():
        status = _check_process_status(pid_file)
        report_lines.append(f"- **{name}**：{status}")

    report_lines.extend([
        "",
        "---",
        "",
        "## 健康检查",
        "",
    ])

    # 健康检查
    health_result = _get_healthcheck_summary()
    report_lines.append(f"**{health_result}**")

    report_lines.extend([
        "",
        "---",
        "",
        "## AI 大脑配置",
        "",
        f"- **类型**：`{os.environ.get('AGENT_BRAIN', 'echo')}`",
        f"- **文本模型**：`{os.environ.get('AGENT_OPENCODE_MODEL', '未配置')}`",
        f"- **视觉模型**：`{os.environ.get('AGENT_VISION_MODEL', '未配置')}`",
        f"- **回复模式**：`{os.environ.get('AGENT_REPLY_MODE', 'log')}`",
        "",
        "---",
        "",
        "> **服务已就绪，随时为您服务！**",
    ])

    # 报告接收者：数字员工自己的主管
    supervisor_recipient = None
    if supervisor_id and supervisor_name:
        supervisor_recipient = {
            "user_id": supervisor_id,
            "name": supervisor_name,
            "role": f"{user_name} 的主管"
        }

    return "\n".join(report_lines), supervisor_recipient


def _send_to_user(user_id, report_text):
    """向指定用户发送报告（单聊）"""
    try:
        # 使用 dws chat message send --user 直接发送单聊消息（Markdown 自动渲染）
        cmd = [
            "dws", "chat", "message", "send",
            "--user", user_id,
            "--text", report_text,
            "--profile", PROFILE,
            "-y"
        ]

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            log(f"[startup_report] ✅ 报告已通过单聊发送给用户 {user_id}")
            return True
        else:
            log(f"[startup_report] ❌ 单聊发送失败: {result.stderr[:300]}")
            return False

    except Exception as e:
        log(f"[startup_report] 发送异常: {e}")
        return False


def _send_to_group_with_at(user_id, report_text):
    """降级方案：通过群聊发送并 @ 用户（已废弃，仅作备份）"""
    try:
        group_id = os.environ.get("DWS_EVENT_GROUP", "").strip()
        if not group_id:
            log(f"[startup_report] ❌ 未配置群聊订阅，无法降级发送")
            return False

        cmd = [
            "dws", "chat", "message", "send",
            "--group", group_id,
            "--text", f"@{user_id} \n\n{report_text}",
            "--profile", PROFILE,
            "-y"
        ]

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if result.returncode == 0:
            log(f"[startup_report] ✅ 报告已通过群聊发送给用户 {user_id}")
            return True
        else:
            log(f"[startup_report] ❌ 群聊发送失败: {result.stderr[:200]}")
            return False

    except Exception as e:
        log(f"[startup_report] 群聊发送异常: {e}")
        return False


def send_startup_report():
    """生成并发送启动报告给数字员工的主管"""
    if not _ENABLED:
        log("[startup_report] 功能未启用，跳过")
        return

    log("[startup_report] 开始生成启动报告...")

    result = _build_report()
    if not result:
        log("[startup_report] 无法生成报告（获取用户信息失败）")
        return

    report_text, supervisor = result

    if not supervisor:
        log("[startup_report] 当前数字员工没有主管，跳过发送")
        return

    log(f"[startup_report] 报告已生成，将发送给主管: {supervisor['name']} ({supervisor['role']}, ID: {supervisor['user_id']})")

    # 发送给主管
    if _send_to_user(supervisor['user_id'], report_text):
        log(f"[startup_report] ✅ 启动报告发送成功")
    else:
        log(f"[startup_report] ❌ 启动报告发送失败")


# 不注册为普通 capability（没有 on_inbound 等 hook），而是提供独立的启动函数
# 由 event_watcher 或 monitor 在启动完成后调用
__all__ = ["send_startup_report"]
