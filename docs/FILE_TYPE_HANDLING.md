# 文件类型精细化处理文档

## 概述

issue #68 实现了文件消息的精细化处理：按文件类型（扩展名）分派到不同解析器，每类文件在**独立 session** 解析后包裹提示词注入**复用的主会话**（保留多轮上下文），最后回复来源会话。

## 支持的文件类型

| 类型 | 扩展名 | 解析方式 | 产出 | 依赖库 |
|------|--------|----------|------|--------|
| **文本** | .txt, .md, .csv, .json, .log, .py, .js, .yaml, etc. | 直接读前 N 字节 | 原始正文（截断标注） | 无 |
| **图片** | .png, .jpg, .jpeg, .gif, .bmp, .webp, .svg | vision 多模态识别（独立 session） | 逐字文字提取 + 客观描述 | 无（使用 opencode serve vision） |
| **PDF** | .pdf | 文本层提取优先；扫描版逐页转图走 vision | 正文文本 / 逐页识别 | pdfplumber（文本）+ pdf2image（OCR 回退） |
| **Office** | .docx, .xlsx, .pptx | 转纯文本/结构化文本（表格保留行列） | 文本化内容 | python-docx, openpyxl, python-pptx |
| **视频** | .mp4, .avi, .mov, .mkv, etc. | 抽关键帧走 vision | 帧描述（开头、1/4、1/2、3/4、结尾） | opencv-python |
| **其它** | .zip, .exe, etc. | 明确告知读不了 | 提示信息 | 无 |

## 架构设计

### 1. 独立 Session 解析 + 复用主会话回复

**模式**（对齐 `image.py` 的范式）：

```
用户发文件 
  → 下载到 tmpdir（受控）
  → 按类型分派解析器
    → 解析器在**独立 session** 运行（如 vision 识别）
    → 临时 session 登记到抑制名单（避免 SSE 事件刷屏）
    → 解析完成后 DELETE 临时 session
  → 解析结果包裹提示词（说明来源、任务）
  → 注入**复用的主会话**（generate_reply(raw=True, ctx={...})）
  → 回复发回来源群
  → 删除 tmpdir
```

**关键实现**：

- **独立 session 解析**：`_recognize_via_serve()` 创建临时 session，POST 含 file+text 的 message，取 assistant 文本，用完 DELETE。
- **临时 session 抑制**：`_register_textreply_sid(sid)` 登记到 brain 抑制名单，避免其 SSE 事件触发业务通知。
- **复用主会话注入**：`generate_reply(user, prompt, raw=True, ctx={conv_id, msg_id, ...})` 传递会话上下文，保留多轮记忆。

### 2. 受控下载到 tmpdir

- harness **主动** `dws drive download` 到临时目录（`tempfile.mkdtemp()`），不是项目工作目录。
- 解析完成后 `shutil.rmtree(tmp_dir, ignore_errors=True)` 删除，不留临时文件。
- 避免 agent 自主 bash 下载/执行的不可控 + 安全问题（见 #40）。

### 3. 包裹提示词

每类解析结果套一段 footer 说明：

- 这是 X 类型文件
- 内容由系统解析得到
- 你看不到原件但据此理解意图

环境变量可覆盖：`CAP_FILE_<TYPE>_PROMPT_FOOTER`（如 `CAP_FILE_IMAGE_PROMPT_FOOTER`）。

### 4. 解析器插件化

每类文件对应一个 `_parse_<type>(path)` 函数，返回 `(content_text, success)`：

- `_parse_text(path)` — 读前 N 字节文本
- `_parse_image(path)` — 调用 `_recognize_via_serve()` vision 识别
- `_parse_pdf(path)` — pdfplumber 提取文本层；失败回退 `_parse_pdf_ocr()`（pdf2image + vision）
- `_parse_office(path)` — 根据扩展名调用 `_parse_docx/xlsx/pptx()`
- `_parse_video(path)` — opencv 抽关键帧 + vision 识别

### 5. 优雅降级

- 依赖库缺失时记日志 `log("file: PDF 解析器不可用（缺 pdfplumber）")`，返回 `("", False)`。
- 上层收到 `success=False` 后给用户明确提示"解析失败"，不静默、不硬塞乱码。

## 配置

### 环境变量

```bash
# 文件能力总开关（默认开）
export CAP_FILE_ENABLED=1

# 各类型解析器开关（默认全开，缺依赖时优雅降级）
export CAP_FILE_IMAGE_ENABLED=1
export CAP_FILE_PDF_ENABLED=1
export CAP_FILE_OFFICE_ENABLED=1
export CAP_FILE_VIDEO_ENABLED=1

# 文本读取字节上限（防超大文件撑爆 prompt，默认 16KB）
export CAP_FILE_MAX_BYTES=16384

# Vision 配置（复用 image 能力）
export AGENT_VISION_MODEL="opencode/mimo-v2.5-free"  # 免费多模态模型
export CAP_IMAGE_VISION_TIMEOUT=90  # 识别超时秒数

# 各类型 prompt footer（可选覆盖）
export CAP_FILE_TEXT_PROMPT_FOOTER="以上是用户发送的文件内容..."
export CAP_FILE_IMAGE_PROMPT_FOOTER="以上「图片识别内容」由多模态模型提取..."
export CAP_FILE_PDF_PROMPT_FOOTER="以上是从 PDF 文件中提取的内容..."
export CAP_FILE_OFFICE_PROMPT_FOOTER="以上是从 Office 文档中提取的内容..."
export CAP_FILE_VIDEO_PROMPT_FOOTER="以上是从视频文件中提取的关键帧内容..."
```

### 依赖库安装

```bash
# 基础（文本 + 图片，无需额外库）
# 图片走 opencode serve vision（AGENT_VISION_MODEL），无需外部 proxy

# PDF 支持
pip install pdfplumber pdf2image

# Office 支持
pip install python-docx openpyxl python-pptx

# 视频支持
pip install opencv-python

# 一键安装全部
pip install pdfplumber pdf2image python-docx openpyxl python-pptx opencv-python
```

## 使用示例

### 1. 发送文本文件

```bash
# 用户在钉钉群发送 deploy.yaml
# 系统：
#   1. 检测到文件消息（KIND_FILE）
#   2. 提取 fileId + 文件名 "deploy.yaml"
#   3. 分类为 "text"
#   4. drive download 到 tmpdir
#   5. 读前 16KB 正文
#   6. 包裹提示词："用户 hugozhu 发送了一个文件：deploy.yaml\n【文件内容】\n```\n<正文>\n```\n以上是用户发送的文件内容..."
#   7. 注入复用主会话（raw=True, ctx={conv_id, msg_id, ...}）
#   8. brain 生成回复："这是一个 K8s 部署配置，镜像版本是 v1.2.3..."
#   9. 回复发回群
#  10. 删除 tmpdir
```

### 2. 发送图片文件

```bash
# 用户在钉钉群发送 screenshot.png
# 系统：
#   1. 分类为 "image"
#   2. drive download 到 tmpdir
#   3. 调用 _parse_image(path)
#      → 读图片字节
#      → _recognize_via_serve(img_bytes, "image/png")
#        → 创建临时 session（title="agent-file-vision"）
#        → _register_textreply_sid(sid) 登记抑制名单
#        → POST /session/{sid}/message（parts=[file, text]）
#          text="请逐字提取这张图片中的所有文字内容..."
#        → 取 assistant 文本："错误信息：NullPointerException at line 42"
#        → DELETE /session/{sid}
#   4. 包裹提示词："用户 hugozhu 发送了一张图片：screenshot.png\n【图片识别内容】\n```\n错误信息：NullPointerException at line 42\n```\n以上「图片识别内容」由多模态模型提取..."
#   5. 注入复用主会话
#   6. brain 生成回复："这是一个空指针异常，发生在第 42 行..."
#   7. 回复发回群
#   8. 删除 tmpdir
```

### 3. 发送 PDF 文件

```bash
# 用户发送 report.pdf
# 系统：
#   1. 分类为 "pdf"
#   2. drive download 到 tmpdir
#   3. 调用 _parse_pdf(path)
#      → 尝试 pdfplumber 提取文本层
#      → 成功："--- 第 1 页 ---\n第一章 引言\n本文档介绍..."
#      → 失败则回退 _parse_pdf_ocr()（pdf2image + vision 逐页识别）
#   4. 包裹提示词
#   5. 注入复用主会话
#   6. 回复
```

### 4. 发送 Office 文件

```bash
# 用户发送 slides.pptx
# 系统：
#   1. 分类为 "office"
#   2. 调用 _parse_office(path)
#      → 根据扩展名分派：.docx → _parse_docx()
#                           .xlsx → _parse_xlsx()
#                           .pptx → _parse_pptx()
#      → _parse_pptx() 用 python-pptx 提取每页文本
#   3. 包裹提示词
#   4. 注入复用主会话
#   5. 回复
```

### 5. 发送视频文件

```bash
# 用户发送 demo.mp4
# 系统：
#   1. 分类为 "video"
#   2. 调用 _parse_video(path)
#      → opencv 抽 5 帧（开头、1/4、1/2、3/4、结尾）
#      → 每帧转 PNG bytes → _recognize_via_serve() vision 识别
#      → 合并："--- 时间 0.0s ---\n界面截图\n--- 时间 5.2s ---\n点击按钮..."
#   3. 包裹提示词
#   4. 注入复用主会话
#   5. 回复
```

### 6. 不支持类型

```bash
# 用户发送 archive.zip
# 系统：
#   1. 分类为 "unknown"
#   2. 明确回复："收到文件「archive.zip」，但它看起来不是我能处理的文件类型。我可以处理：文本文件（txt/md/csv/json/日志/代码等）、图片、PDF、Office 文档（docx/xlsx/pptx）、视频。"
#   3. 不下载、不解析、不注入
```

## 测试

### 单元测试

```bash
# 运行所有单元测试（mock 外部依赖）
python3 tests/custom/test_file_capability.py

# 覆盖：
#   - 类型分类（_classify_file）
#   - 各解析器分派（text/image/pdf/office/video）
#   - 路由（防回环/去重声明）
#   - 下载失败/解析失败/不支持类型的兜底
#   - prompt 组装（包裹提示词）
#   - 复用主会话注入（raw=True, ctx 传递）
```

### 端到端测试

```bash
# 真实链路测试（需 DingTalk 配置 + 服务运行）
bash tests/custom/e2e_file_test.sh

# 流程：
#   1. 创建测试文件（text + image）
#   2. 上传到钉钉云盘（获取 fileId）
#   3. 发送文件消息到测试群
#   4. 等待回复（双校验：日志 + 消息列表）
#   5. 验收：按类型解析 + 回复包含关键词
```

## 验收清单（issue #68）

- [x] `handle_file` 按类型分派，图片/PDF/Office 至少三类能解析出内容并正确回复
- [x] 解析走独立 session，用完删除；回复注入复用主会话且多轮上下文延续
- [x] 不支持类型 / 解析失败有明确用户提示
- [x] 单测覆盖各分派分支（mock 下载 + mock 解析器），对齐 `tests/custom/test_file_capability.py` 风格
- [x] 端到端：发一个 PDF/图片文件，断言 vision/解析结果进了回复（对齐 `e2e_text_reply_test.sh` 的双校验范式）

## 实现文件

- `src/custom/capabilities/file.py` — 文件能力主逻辑（类型分派 + 各解析器）
- `tests/custom/test_file_capability.py` — 单元测试（18 个测试用例）
- `tests/custom/e2e_file_test.sh` — 端到端测试脚本
- `docs/FILE_TYPE_HANDLING.md` — 本文档

## 常见问题

### Q1: 为什么图片文件不复用 image.py？

A: 图片**消息**（`[图片消息](mediaId=...)`）和图片**文件**（`[文件] photo.png fileId=...`）是不同的 DingTalk 消息类型：
- 图片消息：KIND_IMAGE，mediaId，走 `download-media`
- 图片文件：KIND_FILE，fileId，走 `drive download`

虽然解析逻辑相同（都走 vision），但下载路径不同，所以 file.py 实现了独立的 `_parse_image()`（内部复用 `_recognize_via_serve()` 的 vision 逻辑）。

### Q2: PDF/Office/视频解析失败怎么办？

A: 检查依赖库是否安装：

```bash
# PDF
pip show pdfplumber pdf2image

# Office
pip show python-docx openpyxl python-pptx

# 视频
pip show opencv-python
```

缺失时能力会记日志并优雅降级，给用户明确提示"解析失败"。

### Q3: vision 识别需要外部 proxy 吗？

A: 不需要。配置 `AGENT_VISION_MODEL="opencode/mimo-v2.5-free"`（免费多模态模型），走 opencode serve 自身的 vision 能力，无需外部 PROXY_URL。

### Q4: 临时 session 会污染主会话吗？

A: 不会。解析用的临时 session：
1. 独立创建（title="agent-file-vision"）
2. 登记到抑制名单（`_register_textreply_sid(sid)`），SSE 事件不触发业务通知
3. 用完立即删除（`DELETE /session/{sid}`）
4. 解析结果通过 `generate_reply(raw=True, ctx={...})` 注入**复用的主会话**，保留多轮上下文

### Q5: 为什么文本文件还是限制 16KB？

A: 防止超大文件撑爆 prompt（LLM context window 有限）。可通过 `CAP_FILE_MAX_BYTES` 环境变量调整：

```bash
export CAP_FILE_MAX_BYTES=32768  # 32KB
```

超出部分会标注"（文件过长，仅读取前 N 字节）"。

### Q6: 如何添加新的文件类型？

A: 按插件化模式扩展：

1. 在 `_classify_file()` 添加扩展名映射
2. 实现 `_parse_<type>(path)` 函数（返回 `(content, success)`）
3. 在 `handle_file()` 的 `if file_type == "..."` 添加分支
4. 添加环境变量 `CAP_FILE_<TYPE>_ENABLED` 和 `CAP_FILE_<TYPE>_PROMPT_FOOTER`
5. 添加单测覆盖

示例（添加 Markdown 预览）：

```python
_MARKDOWN_EXTS = {".md", ".markdown"}

def _classify_file(filename):
    # ...
    if ext in _MARKDOWN_EXTS:
        return "markdown"
    # ...

def _parse_markdown(path):
    """Markdown 转 HTML 预览（可选）。"""
    try:
        import markdown
        with open(path, "r") as f:
            md_text = f.read(_FILE_MAX_BYTES)
        html = markdown.markdown(md_text)
        return f"Markdown 预览：\n{html}", True
    except ImportError:
        # 降级为普通文本
        return _parse_text(path)
```

## 参考

- Issue #68: https://github.com/hugozhu/dingtalk-opencode-tag/issues/68
- `src/custom/capabilities/image.py` — 图片消息识别（独立 session 解析范式）
- `src/custom/capabilities/file.py` — 文件消息处理（type-based dispatch）
- #40 — 受控下载的安全动机
