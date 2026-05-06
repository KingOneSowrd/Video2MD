# Video Ingest Monitor

将视频（B站 / YouTube / 本地文件）转为**图文并茂的 Markdown**，一键导出截图 + 字幕，适合知识整理、课程笔记、内容归档。

> 输出的为 Raw 数据，本意是作为轻量化中间态工具，输出后再给 Agent 分析入 Wiki。

---

## 下载

在 [Releases 页面](https://github.com/KingOneSowrd/Video2MD/releases/latest) 下载最新版 `VideoIngestMonitor_vX.X.X.zip`，解压后双击 `VideoIngestMonitor.exe` 运行，**无需安装 Python 或任何环境**。

---

## 首次运行

首次打开时，程序会自动检测依赖并提示下载：

| 依赖 | 用途 | 大小 | 操作 |
|------|------|------|------|
| **ffmpeg** | 视频帧提取 | ~70 MB | 点「自动下载」，程序自动完成 |
| **Whisper medium 模型** | 无字幕视频语音转录 | ~1.5 GB | 点「下载模型」，程序自动完成 |

> Whisper 模型**仅在视频没有平台字幕时使用**。处理有字幕的 B站 / YouTube 视频无需等待模型下载。

---

## 使用方法

1. 打开 `VideoIngestMonitor.exe`
2. 将视频 URL 或本地文件拖入输入框，回车 或 点「DISPATCH」
3. 等待处理完成，点「打开」查看输出的 Markdown 文件
4. 处理中的任务可点「Ⅱ」暂停、「▶」继续，点「×」取消（同时删除已生成的中间文件）

**支持来源：**
- B站视频链接（含分享文本格式 `标题 + URL`）
- YouTube 视频链接
- 本地视频文件（拖拽）

**输出格式：**

```
输出目录/
├── 视频标题.md          ← 图文 Markdown（截图 + 字幕）
└── 视频标题_assets/     ← 关键帧截图
```

Markdown 示例：

```markdown
## 00:01:23
![](视频标题_assets/frame_00005.jpg)

这里是该时间段的字幕内容...
```

---

## Cookie 说明

处理 B站 / YouTube 视频时，如需获取登录态字幕或遇到平台验证，需提供 Cookie。GUI 支持填写一个 Cookie 文件夹，并按网站自动选择对应文件。

请特别注意：**浏览器里显示已登录，不代表已经导出的 Cookie 文件仍然有效**。程序和 yt-dlp 只会使用你选择的 `cookies.txt` 文件；如果这个文件旧了、导出错了、来自另一个浏览器 Profile，B站仍会返回未登录。

**导出步骤：**

1. 浏览器安装 [Cookie-Editor](https://cookie-editor.com/) 扩展（支持 Chrome / Edge / Firefox）
2. 登录 B站或 YouTube 后，点击扩展图标
3. 右下角点「Export」→ 选择格式 **Netscape**
4. 分别保存为容易识别的文件名，例如 `bilibili.txt`、`Youtube.txt`
5. 在工具「Cookie 文件夹」栏点「◉ 浏览」选择这些文件所在的文件夹

路径会持久化保存，下次启动自动加载。

**文件夹自动匹配：**

- B站：文件名包含 `bilibili`、`bili` 或 `b23`，例如 `bilibili.txt`
- YouTube：文件名包含 `youtube`、`youtu` 或 `google`，例如 `Youtube.txt`
- 仍兼容单个 Cookie 文件，但推荐选择文件夹，避免在 B站和 YouTube 之间手动切换

**B站 AI 字幕排错：**

- 日志出现 `登录态: 已登录`，才说明当前 Cookie 文件被 B站接口认可
- 日志出现 `登录态: 未登录(-101)`，说明 Cookie 文件无效，即使浏览器页面仍显示已登录
- `字幕: [] | 自动字幕: []` 通常表示该视频没有可用平台字幕，或 Cookie 未通过登录校验
- 遇到 `未登录(-101)` 时，重新打开 B站确认登录状态，然后重新用 Cookie-Editor 导出 **Netscape** 格式并覆盖旧文件
- 不要复用很久以前导出的 Cookie；B站会让旧的 `SESSDATA` 失效

---

## 输出目录

默认输出到 `~/Videos/VideoIngest/`，可在 GUI 的「输出目录」栏修改，下次启动自动记忆。

---

## 语音转录说明

- 使用 **Whisper medium** 模型，支持中文、英文及多语言自动检测
- 语言自动识别：中文视频输出中文，英文视频输出英文，无需手动指定
- 如视频有平台字幕（YouTube / B站），优先使用平台字幕，不启动 Whisper

---

## 从源码构建

需要 Python 3.10+：

```bash
pip install pyqt6 faster-whisper "yt-dlp[default]" yt-dlp-ejs pyinstaller numpy
python build.py
# 输出：dist/VideoIngestMonitor/
```

---

## 系统要求

- Windows 10 / 11（64 位）
- 首次运行需要网络（下载 ffmpeg 和 Whisper 模型）
- 处理本地视频或无字幕视频时需要 4 GB+ 内存（Whisper medium 推理）
