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
4. 处理中的任务可点「×」随时取消（同时删除已生成的中间文件）

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

## B站说明

处理 B站视频时，如需获取 **AI 字幕**或访问登录后才可见的内容，需提供 Cookie 文件。

**导出步骤：**

1. 浏览器安装 [Cookie-Editor](https://cookie-editor.com/) 扩展（支持 Chrome / Edge / Firefox）
2. 登录 B站后，点击扩展图标
3. 右下角点「Export」→ 选择格式 **Netscape**
4. 保存为 `bilibili_cookies.txt`
5. 在工具「Cookie 文件」栏点「◉ 浏览」选择该文件

路径会持久化保存，下次启动自动加载。

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
pip install pyqt6 faster-whisper yt-dlp pyinstaller numpy
python build.py
# 输出：dist/VideoIngestMonitor/
```

---

## 系统要求

- Windows 10 / 11（64 位）
- 首次运行需要网络（下载 ffmpeg 和 Whisper 模型）
- 处理本地视频或无字幕视频时需要 4 GB+ 内存（Whisper medium 推理）
