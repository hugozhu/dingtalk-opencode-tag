# Issue #117 — 提示词触发的 flash 模型切换

## 你在 issue 上定的设计

| 决策点 | 你的选择 |
|---|---|
| 触发方式 | 任务消息里带「用flash模型」→ 触发 |
| 配置 | `AGENT_OPENCODE_MODEL_FLASH = local/deepseek-v4-flash` |
| 可观测性 | 不用显式告知用户 |
| 失败回退 | 不自动升回贵模型，直接失败让用户重发 |

## 落地要点

模型是 per-message 传的（brain.py:866），不是 per-session，所以开着
`AGENT_SESSION_REUSE=1` 也能在同一 session 里逐轮切模型，不需要重建会话。

当前 `_OPENCODE_MODEL` 是模块级常量，在 4 处被读：`_http_oneshot`(945)、
`_http_reuse`(973)、`_brain_opencode_cli`(1064 的 `--model`)、以及各处 `_oc_log`。
改法是**把 model 作为参数从 `_brain_opencode` 一路透传下去**，默认值 `None` 时
回落到 `_OPENCODE_MODEL` —— 这样现有直接调 `_post_message` 的单测不受影响。

与 cancel / reset 关键词的区别：那两个是**整句严格匹配**（`text in _KEYWORDS`），
因为它们本身就是完整指令。flash 触发词是**跟在真实任务前面的修饰语**
（「用flash模型 打开浏览器抓一下股价」），所以必须用**子串匹配**，并且命中后
**把触发词从 prompt 里摘掉**，否则模型会看到一句它无法执行的指令、可能回
「好的，我将使用 flash 模型」这种噪音。

## 改动清单

### 1. `config/constants.sh`
在 `AGENT_OPENCODE_MODEL` 那行下面加两个变量：

```bash
# 便宜模型（#117）：任务消息带触发词时本轮改用它，适合浏览器自动化这类
# 「多轮工具调用、token 量大、但推理深度要求不高」的任务。
# 留空 = 关闭本特性（所有消息都走 AGENT_OPENCODE_MODEL）。
export AGENT_OPENCODE_MODEL_FLASH="${AGENT_OPENCODE_MODEL_FLASH:-}"
# 触发词（**子串**匹配，大小写不敏感；与 CANCEL/RESET 的整句匹配不同），逗号分隔。
export AGENT_OPENCODE_FLASH_KEYWORDS="${AGENT_OPENCODE_FLASH_KEYWORDS:-用flash模型,用flash,flash模型}"
```

模板里默认留空 = 特性关闭，fork 本项目的人不会莫名其妙被切模型。

### 2. `config/constants.local.sh`（gitignored，只在你机器上生效）
```bash
export AGENT_OPENCODE_MODEL_FLASH="local/deepseek-v4-flash"
```

### 3. `src/custom/brain.py`

**模块常量**（挨着 `_OPENCODE_MODEL` 放）：
```python
_OPENCODE_MODEL_FLASH = os.environ.get("AGENT_OPENCODE_MODEL_FLASH", "")
_FLASH_KEYWORDS = [k.strip().lower() for k in
                   os.environ.get("AGENT_OPENCODE_FLASH_KEYWORDS", "用flash模型,用flash,flash模型").split(",")
                   if k.strip()]
```

**新函数 `_pick_model(text)`** → 返回 `(model, cleaned_text)`：
未配置 flash 模型 or 未命中触发词 → `(_OPENCODE_MODEL, text)` 原样返回；
命中 → `(_OPENCODE_MODEL_FLASH, 摘掉触发词并 strip 后的 text)`。

**`_brain_opencode`（573）**：在现有 reset 判定之后、`prompt = ...`（597）之前
调 `_pick_model`，用返回的 cleaned_text 拼 prompt，把 model 传给下面两条路。

**透传链**（都加 `model=None` 参数，`None` → `_OPENCODE_MODEL`）：
- `_brain_opencode_http(prompt, ctx=None, model=None)` (919)
- `_http_oneshot(port, pwd, prompt, ctx, model=None)` (944)
- `_http_reuse(port, pwd, conv_id, prompt, ctx, model=None)` (972)
- `_brain_opencode_cli(prompt, model=None)` (1055) — **CLI 回退路径必须跟上**，
  否则 serve 挂掉时会静默变回贵模型
- 各处 `_oc_log(...)` 改成记**本轮生效的** model（而非常量）

`_post_message` 签名不动（它已经收 provider/model_id）—— 现有单测直接调它，不受影响。

### 4. `src/custom/capabilities/startup_report.py:281`
「文本模型」下面加一行 flash 模型（配了才显示）。这是启动时的**配置**播报，
不是你说的「每轮告知用户降级」，不冲突。

### 5. `tests/custom/test_brain_flash_model.py`（新增）
全程 patch `_serve_request`，不依赖网络，覆盖：
1. 命中触发词 → POST body 的 `model.providerID/modelID` 是 flash，且 prompt 里
   已摘掉触发词
2. 未命中 → 默认模型，文本原样
3. 命中但 `AGENT_OPENCODE_MODEL_FLASH` 未配置 → 默认模型，文本**不**摘（特性关闭）
4. 大小写不敏感 + 触发词在句中/句首都能命中
5. CLI 回退路径 `--model` 带的是 flash
6. 会话复用下同一 session 逐轮切模型（第一轮 flash、第二轮默认），验证
   **不会**因为换模型而重建 session

## 明确不做

- 不自动升回贵模型重试（你的决策：直接失败让用户重发）
- 不在 ack / task_stats 里告诉用户「本次用了便宜模型」（你的决策：不用显式）
- 不动 `AGENT_VISION_MODEL`（图片识别是独立链路，不在本 issue 范围）

## 验证

```bash
python3 tests/custom/test_brain_flash_model.py
python3 tests/custom/test_brain_turn_error.py    # 回归：透传改动没破坏既有路径
python3 tests/custom/test_brain_timeout.py
python3 tests/custom/test_digital_employee.py
```

改动走新分支 + `gh pr create`（按你一贯的习惯），PR 里 `Closes #117`。
