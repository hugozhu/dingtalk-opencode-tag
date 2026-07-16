# CONTRIBUTING.md — 贡献回 upstream

本文件面向 **FDE 在交付过程中发现 core bug 并希望贡献回 upstream** 的场景。

## 哪些改动该贡献回 upstream

| 改动位置 | 是否贡献 | 说明 |
|---------|---------|------|
| `src/core/agent_common.py` 的 bug fix | ✅ 贡献 | 如 `_find_session_with_predicate` 的过滤逻辑错误 |
| `src/core/event_watcher.py` 的 bug fix | ✅ 贡献 | 如 SSE 重连退避逻辑错误 |
| `bin/core/*.sh` 的 bug fix | ✅ 贡献 | 如 `verify_pid` 的 cmdline 签名匹配漏了边界 |
| `tests/core/*.sh` / `test_agent_common.py` | ✅ 贡献 | 新增 core 行为的回归测试 |
| `src/custom/handler.py` | ❌ 不贡献 | 业务特定，每个交付不一样 |
| `src/custom/routes.py` | ❌ 不贡献 | 路由注册是业务特定 |
| `config/*.local.*` | ❌ 绝不贡献 | 含真实凭据，已 gitignore |
| `src/templates/handler_template.py` 的最佳实践升级 | ✅ 贡献 | 提炼新的可复用模式 |

## 贡献流程

### 1. 在 fork 里修 core bug

```bash
cd /path/to/my-delivery/
# 改 src/core/agent_common.py（或其他 core 文件）
# 写回归测试到 tests/core/test_agent_common.py（或 unit_test.sh）
git add src/core/agent_common.py tests/core/test_agent_common.py
git commit -m "fix(agent_common): _find_session_with_predicate 时间过滤边界"
```

### 2. 跑 core 测试确认不破坏

```bash
bash tests/core/unit_test.sh
python3 tests/core/test_agent_common.py
```

### 3. 提 PR 到 upstream

```bash
# fork 到自己的 GitHub，push
git remote add myfork git@github.com:<you>/dingtalk-opencode-tag.git
git push myfork main

# 在 GitHub 上提 PR，base = hugozhu/dingtalk-opencode-tag:main
# PR 描述写清：bug 现象 / 复现步骤 / 修复思路 / 测试覆盖
```

因为 core 路径在 upstream 和 fork 里完全一致，PR 的 diff 会很干净，只含 core 变更，不会有 custom 的业务定制干扰 review。

### 4. upstream 合并后，FDE 同步

upstream 合并后，FDE 把 upstream 的 main 拉回 fork（见 [FORKING.md](./FORKING.md) 的"同步 upstream 修复"），这样 fork 的 core 始终和 upstream 一致，避免后续 cherry-pick 冲突。

## 提炼新最佳实践到 templates

如果 FDE 在交付中摸索出一个可复用的模式（如新的附件下载方式、新的 cleanup 状态机），想贡献回 upstream 让其他 FDE 受益：

1. 把通用部分提炼到 `src/templates/handler_template.py`（保持纯净，不含业务特定代码）
2. 必要时把纯工具函数下沉到 `src/core/agent_common.py`
3. 配套写测试到 `tests/core/`
4. 提 PR，描述里写清"最佳实践 N：XXX"，参照 [ARCHITECTURE.md](./ARCHITECTURE.md) 的 13 个最佳实践格式

## 不可贡献的内容

- 任何含真实凭据 / robot_code / user_id / proxy_key 的文件
- 业务特定的 handler 实现（每个交付场景不同）
- 业务特定的路由注册（routes.py 里的具体 handler 调用）
- 业务特定的 e2e 测试（每个交付的触发方式不同）

## 代码风格

- Python：4 空格缩进，模块顶部 docstring 说明职责 + 提炼来源 + 原作者
- Shell：`set -euo pipefail`，`SCRIPT_DIR` 用 `$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)` 取项目根
- 不加无关注释，注释只解释"为什么"不解释"是什么"
- 测试：shell 用 `bash -n` + 函数断言；Python 用 `unittest` + `patch.object`
