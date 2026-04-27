# Video Ingest Monitor

将视频（B站 / YouTube / 本地文件）转为**图文并茂的 Markdown**，一键导出截图 + 字幕，适合知识整理、课程笔记、内容归档。

---

## 下载

在 [Releases 页面](https://github.com/KingOneSowrd/Video2MD/releases/latest) 下载最新版 `VideoIngestMonitor.exe`，双击运行即可，**无需安装 Python 或任何环境**。

---

## 首次运行

首次打开时，程序会自动检测依赖并提示下载：

| 依赖 | 用途 | 大小 | 操作 |
|------|------|------|------|
| **ffmpeg** | 视频帧提取 | ~70 MB | 点「自动下载」，程序自动完成 |
| **Whisper 模型** | 无字幕视频转录 | ~250 MB | 点「下载模型」，程序自动完成 |

> Whisper 模型**仅在视频没有平台字幕时使用**。处理有字幕的 B站 / YouTube 视频无需等待模型下载。

---

## 使用方法

1. 打开 `VideoIngestMonitor.exe`
2. 将视频 URL 或本地文件拖入输入框，回车 或 点「DISPATCH」
3. 等待处理完成，点「打开」查看输出的 Markdown 文件

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

处理 B站视频需要提供 Cookie（登录态），否则返回 412 错误。

在 GUI 的「Cookie 文件」栏填写 Netscape 格式的 `cookies.txt` 路径即可。
导出方法：浏览器安装 [Get cookies.txt LOCALLY](https://chrome.google.com/webstore/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc) 扩展，在 B站页面导出。

---

## 输出目录

默认输出到 `~/Videos/VideoIngest/`，可在 GUI 的「输出目录」栏修改，下次启动自动记忆。

---

## 从源码构建

需要 Python 3.10+：

```bash
pip install pyqt6 faster-whisper yt-dlp pyinstaller numpy
python build.py
# 输出：dist/VideoIngestMonitor.exe
```

---

## 系统要求

- Windows 10 / 11（64位）
- 首次运行需要网络（下载 ffmpeg 和 Whisper 模型）
- 处理本地视频或无字幕视频时需要 4GB+ 内存（Whisper 推理）
