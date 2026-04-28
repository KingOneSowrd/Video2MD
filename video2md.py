#!/usr/bin/env python3
"""
video2md.py  —  视频 → 高保真图文 Markdown（portable 版）
支持：YouTube / B站 / 抖音 / 本地视频文件

portable 版改动：
- yt-dlp 改用 Python API（yt_dlp.YoutubeDL），不再依赖外部 Python 进程
- 新增 process_video() 函数供 monitor.py import 并在线程中调用
- _ffmpeg_bin() 支持 PyInstaller 打包后从 sys._MEIPASS 找 ffmpeg/ffprobe
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import yt_dlp

STATUS_FILE = Path.home() / '.video2md_status.json'

_BILI_RE = re.compile(r'bilibili\.com|b23\.tv', re.I)
_YT_RE   = re.compile(r'youtube\.com|youtu\.be', re.I)


def _ydl_opts_base(extra_args: list[str] | None = None) -> dict:
    """把旧式 yt-dlp CLI extra_args 列表转为 YoutubeDL opts dict。"""
    opts: dict = {'quiet': True, 'no_warnings': True}
    it = iter(extra_args or [])
    for k in it:
        if k == '--cookies-from-browser':
            opts['cookiesfrombrowser'] = (next(it), None, None, None)
        elif k == '--cookies':
            opts['cookiefile'] = next(it)
        elif k == '--referer':
            opts.setdefault('http_headers', {})['Referer'] = next(it)
    return opts


_BILI_UA = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
    'AppleWebKit/537.36 (KHTML, like Gecko) '
    'Chrome/131.0.0.0 Safari/537.36'
)

def _apply_bili_headers(opts: dict, url: str) -> None:
    """B站请求注入真实浏览器头，防止 412。"""
    if _BILI_RE.search(url):
        h = opts.setdefault('http_headers', {})
        h['Referer']    = 'https://www.bilibili.com'
        h['Origin']     = 'https://www.bilibili.com'
        h.setdefault('User-Agent', _BILI_UA)

_YT_BOT_RE     = re.compile(r'Sign in to confirm|bot|confirm you.re not', re.I)
_BILI_AUTH_RE  = re.compile(r'login|大会员|需要登录|请先登录|仅限|premium|vip', re.I)
_COOKIE_ERR_RE = re.compile(r'cookie|keyring|could not copy|permission denied|dpapi|decrypt', re.I)


def _running_browsers() -> set[str]:
    """返回当前正在运行的浏览器名称集合（Windows 专用，其他平台返回空集）。"""
    if sys.platform != 'win32':
        return set()
    try:
        out = subprocess.run(
            ['tasklist', '/FO', 'CSV', '/NH'],
            capture_output=True, text=True, timeout=5
        ).stdout.lower()
        return {b for b, exe in [('chrome', 'chrome.exe'),
                                  ('edge',   'msedge.exe'),
                                  ('firefox','firefox.exe')]
                if exe in out}
    except Exception:
        return set()


def _browser_cookie_chain(base_opts: dict) -> list[tuple[dict, str]]:
    """返回按浏览器 Cookie 依次重试的 opts 列表（未运行的浏览器排前面）。"""
    running  = _running_browsers()
    browsers = sorted(['chrome', 'edge', 'firefox'], key=lambda b: b in running)
    return [({**base_opts, 'cookiesfrombrowser': (b, None, None, None)}, f'{b} Cookie')
            for b in browsers]


def _subtitle_fallback_chain(base_opts: dict, url: str) -> list[tuple[dict, str]]:
    """
    字幕提取专用重试链（与下载链顺序不同）：
    - YouTube：移动端优先（直接绕过 bot 检测），再试浏览器 Cookie，最后 web
    - B站：浏览器 Cookie 优先（AI 字幕需要登录态），再试默认
    - 其他：仅默认
    """
    is_yt   = bool(_YT_RE.search(url))
    is_bili = bool(_BILI_RE.search(url))

    if is_yt:
        mobile = {**base_opts,
                  'extractor_args': {'youtube': {'player_client': ['ios', 'android']}}}
        return ([(mobile, '移动端')]
                + _browser_cookie_chain(base_opts)
                + [(base_opts, '默认')])

    if is_bili:
        return (_browser_cookie_chain(base_opts)
                + [(base_opts, '默认')])

    return [(base_opts, '默认')]


def _platform_fallback_chain(base_opts: dict, url: str) -> list[tuple[dict, str]]:
    """
    YouTube / B站 认证失败时的自动重试链：
    1. 默认（无 Cookie）
    2. Chrome / Edge / Firefox Cookie（未运行的浏览器优先，降低 DB 锁概率）
    3. 仅 YouTube：移动端客户端兜底（低画质但无需认证）
    """
    chain: list[tuple[dict, str]] = [(base_opts, '默认')]
    is_yt   = bool(_YT_RE.search(url))
    is_bili = bool(_BILI_RE.search(url))

    if not (is_yt or is_bili):
        return chain

    chain += _browser_cookie_chain(base_opts)

    if is_yt:
        chain.append(({**base_opts,
                       'extractor_args': {'youtube': {'player_client': ['ios', 'android', 'tv_embedded']}}},
                      '备用客户端'))
    return chain


def _cache_dir() -> Path:
    """返回 EXE/脚本同目录下的 cache/ 目录（自动创建）。"""
    base = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).parent
    d = base / 'cache'
    d.mkdir(exist_ok=True)
    return d


def _ffmpeg_bin(name: str) -> str:
    """
    查找 ffmpeg/ffprobe 路径，优先级：
    1. sys._MEIPASS（PyInstaller 捆绑）
    2. EXE 同目录（用户手动放置）
    3. 系统 PATH
    """
    if getattr(sys, 'frozen', False):
        p = Path(sys._MEIPASS) / f'{name}.exe'
        if p.exists():
            return str(p)
        p2 = Path(sys.executable).parent / f'{name}.exe'
        if p2.exists():
            return str(p2)
    return name


# ─────────────────────────── 状态写入 ────────────────────────────

class StatusWriter:
    """向 monitor.py 轮询的 JSON 文件写进度。"""

    def __init__(self, task_id: str, title: str, source: str):
        self.task_id = task_id
        self._data = {
            "id": task_id,
            "title": title,
            "source": source,
            "status": "processing",
            "step": "queued",
            "step_label": "",
            "progress": 0.0,
            "frames": None,
            "segments": None,
            "output_md": None,
            "log": [],
            "added_at": datetime.now().strftime("%H:%M:%S"),
            "finished_at": None,
            "error": None,
        }
        self._flush()

    def update(self, step: str, label: str, progress: float):
        self._data.update(step=step, step_label=label, progress=min(progress, 0.99))
        self._flush()

    def log(self, line: str):
        self._data["log"].append(line)
        if len(self._data["log"]) > 120:
            self._data["log"] = self._data["log"][-120:]
        self._flush()

    def complete(self, frames: int, segments: int, output_md: str):
        self._data.update(
            status="complete", step="complete", progress=1.0,
            frames=frames, segments=segments,
            output_md=output_md,
            finished_at=datetime.now().strftime("%H:%M:%S"),
        )
        self._flush()

    def error(self, msg: str):
        for line in str(msg).splitlines()[-6:]:
            if line.strip():
                self._data["log"].append(f"[错误] {line.strip()}")
        self._data.update(status="error", step="error", error=msg,
                          finished_at=datetime.now().strftime("%H:%M:%S"))
        self._flush()

    def _flush(self):
        try:
            tasks = []
            if STATUS_FILE.exists():
                try:
                    tasks = json.loads(STATUS_FILE.read_text(encoding='utf-8')).get("tasks", [])
                except Exception:
                    tasks = []
            tasks = [t for t in tasks if t.get("id") != self.task_id]
            tasks.append(self._data)
            STATUS_FILE.write_text(json.dumps({"tasks": tasks}, ensure_ascii=False, indent=2),
                                   encoding='utf-8')
        except Exception:
            pass


# ─────────────────────────── 工具函数 ────────────────────────────

def fmt_time(seconds: float) -> str:
    td = timedelta(seconds=int(seconds))
    h = int(td.total_seconds()) // 3600
    m = (int(td.total_seconds()) % 3600) // 60
    s = int(td.total_seconds()) % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def safe_name(text: str, max_len: int = 60) -> str:
    return re.sub(r'[\\/:*?"<>|]', '_', text)[:max_len]


def run(cmd, timeout=600, check=True, capture=True):
    return subprocess.run(
        cmd, capture_output=capture, text=True,
        encoding='utf-8', errors='replace',
        timeout=timeout, check=check
    )


# ─────────────────────────── 视频信息 ────────────────────────────

def get_video_info(src: str, extra_args: list[str] | None = None) -> tuple[str, float]:
    """返回 (title, duration_sec)"""
    if Path(src).exists():
        result = run([_ffmpeg_bin('ffprobe'), '-v', 'quiet', '-show_entries', 'format=duration',
                      '-of', 'default=noprint_wrappers=1:nokey=1', src], check=False)
        duration = float(result.stdout.strip()) if result.returncode == 0 else 0.0
        return Path(src).stem, duration
    opts = _ydl_opts_base(extra_args)
    if _BILI_RE.search(src):
        _apply_bili_headers(opts, src)
    for attempt_opts, label in _platform_fallback_chain(opts, src):
        try:
            with yt_dlp.YoutubeDL(attempt_opts) as ydl:
                info = ydl.extract_info(src, download=False)
                return info.get('title', 'video'), float(info.get('duration', 0))
        except Exception as e:
            s = str(e)
            is_browser = 'Cookie' in label
            is_retryable = bool(_YT_BOT_RE.search(s) or _BILI_AUTH_RE.search(s) or _COOKIE_ERR_RE.search(s))
            if is_browser or is_retryable:
                continue
            break
    return 'video', 0.0


# ─────────────────────────── 字幕获取 ────────────────────────────

def try_platform_subtitles(url: str, work_dir: Path, status=None,
                            extra_args: list[str] | None = None):
    """
    尝试用 yt_dlp 直接拿平台字幕（零 Whisper 消耗）。
    返回 [(start, end, text), ...] 或 None。
    """
    print("[字幕] 尝试平台字幕...", flush=True)
    sub_dir = work_dir / 'subs'
    sub_dir.mkdir(exist_ok=True)

    opts = _ydl_opts_base(extra_args)
    if _BILI_RE.search(url):
        _apply_bili_headers(opts, url)
    opts.update({
        'writesubtitles': True,
        'writeautomaticsub': True,
        'subtitleslangs': ['zh-Hans', 'zh', 'zh-CN', 'ai-zh', 'en'],
        'subtitlesformat': 'vtt/srt/best',
        'skip_download': True,
        'outtmpl': str(sub_dir / '%(title)s'),
    })

    for attempt_opts, label in _subtitle_fallback_chain(opts, url):
        try:
            with yt_dlp.YoutubeDL(attempt_opts) as ydl:
                ydl.download([url])
            # 无异常，但要确认文件真的生成了
            if list(sub_dir.glob('*.vtt')) or list(sub_dir.glob('*.srt')):
                break  # 拿到字幕，停止重试
            # 无文件（可能需要 Cookie 才能访问字幕 API），继续尝试
            if status:
                status.log(f"  [字幕] {label} 未返回字幕文件，继续尝试...")
        except Exception as e:
            s = str(e)
            is_browser = 'Cookie' in label
            is_retryable = bool(_YT_BOT_RE.search(s) or _BILI_AUTH_RE.search(s) or _COOKIE_ERR_RE.search(s))
            if is_browser or is_retryable:
                if label != '默认' and status:
                    status.log(f"  [字幕] {label} 失败，继续尝试...")
                continue
            if status:
                status.log(f"  [字幕] {s[:120]}")
            break

    vtt_files = list(sub_dir.glob('*.vtt')) + list(sub_dir.glob('*.srt'))
    if not vtt_files:
        print("  → 无平台字幕，将使用 Whisper。", flush=True)
        return None

    # 优先取中文字幕
    preferred = next((f for f in vtt_files if any(
        tag in f.name for tag in ('zh', 'cn', 'CN', 'Hans')
    )), vtt_files[0])
    print(f"  → 找到字幕：{preferred.name}", flush=True)
    return _parse_vtt(preferred) if preferred.suffix == '.vtt' else _parse_srt(preferred)


def _ts_to_sec(ts: str) -> float:
    """HH:MM:SS.mmm 或 HH:MM:SS,mmm → float"""
    ts = ts.replace(',', '.')
    parts = ts.split(':')
    h, m, s = int(parts[0]), int(parts[1]), float(parts[2])
    return h * 3600 + m * 60 + s


def _parse_vtt(path: Path):
    text = path.read_text(encoding='utf-8', errors='replace')
    segs = []
    for m in re.finditer(
        r'(\d{2}:\d{2}:\d{2}[.,]\d+)\s*-->\s*(\d{2}:\d{2}:\d{2}[.,]\d+)[^\n]*\n((?:(?!\d{2}:\d{2}).+\n?)*)',
        text
    ):
        start, end = _ts_to_sec(m.group(1)), _ts_to_sec(m.group(2))
        body = re.sub(r'<[^>]+>', '', m.group(3)).strip()
        if body:
            segs.append((start, end, body))
    return _merge_segs(segs)


def _parse_srt(path: Path):
    text = path.read_text(encoding='utf-8', errors='replace')
    segs = []
    for m in re.finditer(
        r'\d+\n(\d{2}:\d{2}:\d{2},\d+)\s*-->\s*(\d{2}:\d{2}:\d{2},\d+)\n((?:(?!\d+\n\d{2}:\d{2}).+\n?)*)',
        text
    ):
        start, end = _ts_to_sec(m.group(1)), _ts_to_sec(m.group(2))
        body = m.group(3).strip()
        if body:
            segs.append((start, end, body))
    return _merge_segs(segs)


def _merge_segs(segs, min_gap=0.3, min_dur=4.0):
    """合并过碎的字幕段"""
    if not segs:
        return segs
    out = [list(segs[0])]
    for s, e, t in segs[1:]:
        gap = s - out[-1][1]
        cur_dur = out[-1][1] - out[-1][0]
        if gap < min_gap and cur_dur < min_dur:
            out[-1][1] = e
            out[-1][2] += ' ' + t
        else:
            out.append([s, e, t])
    return [(s, e, t) for s, e, t in out]


# ─────────────────────────── 视频下载 ────────────────────────────

def download_video(url: str, work_dir: Path, status=None,
                   extra_args: list[str] | None = None) -> Path:
    print("[下载] 下载视频（最高 1080p）...", flush=True)
    out_tmpl = str(work_dir / 'video.%(ext)s')

    def _hook(d: dict):
        if d['status'] == 'downloading' and status:
            total = d.get('total_bytes') or d.get('total_bytes_estimate') or 1
            pct = d.get('downloaded_bytes', 0) / total
            status.update('downloading', f"[下载] {pct:.0%}", 0.04 + pct * 0.20)
            status.log(f"  [下载] {pct:.0%}")
        elif d['status'] == 'finished' and status:
            info = d.get('info_dict', {})
            w, h = info.get('width'), info.get('height')
            fmt = info.get('format', '')
            if w and h:
                status.log(f"  [下载] 完成 {w}×{h}  {fmt[:60]}")

    opts = _ydl_opts_base(extra_args)
    if _BILI_RE.search(url):
        _apply_bili_headers(opts, url)
    opts.update({
        'format': (
            'bestvideo[height<=1080]+bestaudio/'
            'bestvideo[height<=1080]/'
            'best[height<=1080]/'
            'best'
        ),
        'merge_output_format': 'mp4',
        'outtmpl': out_tmpl,
        'progress_hooks': [_hook],
    })

    last_err = None
    for attempt_opts, label in _platform_fallback_chain(opts, url):
        try:
            with yt_dlp.YoutubeDL(attempt_opts) as ydl:
                ydl.download([url])
            last_err = None
            break
        except yt_dlp.utils.DownloadError as e:
            last_err = e
            s = str(e)
            is_browser = 'Cookie' in label
            is_retryable = bool(_YT_BOT_RE.search(s) or _BILI_AUTH_RE.search(s) or _COOKIE_ERR_RE.search(s))
            if is_browser or is_retryable:
                if label != '默认' and status:
                    status.log(f"  [下载] {label} 失败，继续尝试...")
                continue
            break  # 非 bot/cookie 错误，不重试

    if last_err:
        err = str(last_err)
        if status:
            for line in err.splitlines()[-8:]:
                if line.strip():
                    status.log(f"  [下载] {line.strip()}")
        raise RuntimeError(f"yt-dlp 下载失败\n{err[-1200:]}") from last_err

    candidates = list(work_dir.glob('video.*'))
    if not candidates:
        raise RuntimeError("视频下载失败：文件未生成")
    return candidates[0]


# ─────────────────────────── 音频提取 + 转录 ──────────────────────

def extract_audio(video_path: Path, work_dir: Path) -> Path:
    print("[音频] 提取音轨...", flush=True)
    audio = work_dir / 'audio.wav'
    run([_ffmpeg_bin('ffmpeg'), '-i', str(video_path),
         '-acodec', 'pcm_s16le', '-ac', '1', '-ar', '16000',
         str(audio), '-y', '-hide_banner', '-loglevel', 'error'])
    return audio


def transcribe(audio_path: Path, model_size: str, lang: str | None,
               status: 'StatusWriter | None' = None):
    print(f"[转录] faster-whisper ({model_size})...", flush=True)
    if status:
        status.update("transcribing", f"[转录] faster-whisper ({model_size})...", 0.10)
        status.log(f"[转录] faster-whisper ({model_size})...")
    from faster_whisper import WhisperModel
    local = (Path(sys.executable).parent if getattr(sys, 'frozen', False)
             else Path(__file__).parent) / 'models' / f'faster-whisper-{model_size}'
    model_path = str(local) if (local / 'model.bin').exists() else model_size
    model = WhisperModel(model_path, device='cpu', compute_type='int8')
    kwargs = {'beam_size': 5, 'vad_filter': True}
    if lang:
        kwargs['language'] = lang
    segs_iter, info = model.transcribe(str(audio_path), **kwargs)
    lang_msg = f"  → 检测语言：{info.language}（{info.language_probability:.0%}）"
    print(lang_msg, flush=True)
    if status:
        status.log(lang_msg)
    total_dur = info.duration or 1.0
    segs = []
    for s in segs_iter:
        segs.append((s.start, s.end, s.text.strip()))
        if status:
            prog = 0.10 + 0.65 * min(s.end / total_dur, 1.0)
            status.update("transcribing", f"[转录] {s.end:.0f}s / {total_dur:.0f}s", prog)
    return _merge_segs(segs)


# ─────────────────────────── 关键帧提取 ──────────────────────────

def extract_keyframes(video_path: Path, out_dir: Path,
                      threshold: float = 0.5, max_frames: int = 60):
    """
    自适应场景检测：帧数不足时自动降低阈值重试，最终均匀采样到 max_frames。
    """
    out_dir.mkdir(parents=True, exist_ok=True)

    def _detect(t: float) -> list:
        # 清空上次结果
        for f in out_dir.glob('frame_*.jpg'):
            f.unlink(missing_ok=True)
        print(f"[截图] 场景检测关键帧（阈值={t}）...", flush=True)
        cmd = [
            _ffmpeg_bin('ffmpeg'), '-i', str(video_path),
            '-vf', f'select=gt(scene\\,{t}),showinfo,scale=min(iw\\,1920):-2',
            '-vsync', 'vfr',
            '-q:v', '2',
            str(out_dir / 'frame_%05d.jpg'),
            '-hide_banner'
        ]
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding='utf-8', errors='replace', timeout=600)
        timestamps = [
            float(m.group(1))
            for line in result.stdout.split('\n')
            if (m := re.search(r'pts_time:(\d+\.?\d*)', line))
        ]
        files = sorted(out_dir.glob('frame_*.jpg'))
        return [(timestamps[i] if i < len(timestamps) else 0.0, f)
                for i, f in enumerate(files)]

    frames = _detect(threshold)

    # 帧数不足时依次降低阈值重试
    if len(frames) < 15:
        for lower in [0.3, 0.2, 0.12]:
            frames = _detect(lower)
            if len(frames) >= 15:
                break

    # 仍不足则改用间隔截图兜底
    if not frames:
        print("  → 无场景变化，改用间隔截图（每30秒）。", flush=True)
        return _interval_frames(video_path, out_dir)

    # 均匀采样到 max_frames（修正原 step=1 不削减的 bug）
    if len(frames) > max_frames:
        indices = [int(i * len(frames) / max_frames) for i in range(max_frames)]
        frames = [frames[i] for i in indices]

    print(f"  → {len(frames)} 帧", flush=True)
    return frames


def _interval_frames(video_path: Path, out_dir: Path, interval: int = 30):
    """兜底：每 N 秒截一帧"""
    res = run([_ffmpeg_bin('ffprobe'), '-v', 'quiet', '-show_entries', 'format=duration',
               '-of', 'default=noprint_wrappers=1:nokey=1', str(video_path)], check=False)
    duration = float(res.stdout.strip()) if res.returncode == 0 else 0.0

    frames = []
    t, idx = 0.0, 1
    while t < duration:
        fpath = out_dir / f'frame_{idx:05d}.jpg'
        run([_ffmpeg_bin('ffmpeg'), '-ss', str(t), '-i', str(video_path),
             '-vframes', '1', '-q:v', '2', str(fpath),
             '-y', '-hide_banner', '-loglevel', 'error'], check=False)
        if fpath.exists():
            frames.append((t, fpath))
        t += interval
        idx += 1
    return frames


# ─────────────────────────── Markdown 拼合 ───────────────────────

def build_markdown(title: str, source: str,
                   segments: list, frames: list,
                   asset_dir_name: str) -> str:
    lines = [
        f"# {title}",
        f"",
        f"> **来源：** `{source}`  ",
        f"> **生成时间：** {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"",
        "---",
        "",
    ]

    if not frames:
        prev_min = -1
        for start, end, text in segments:
            cur_min = int(start) // 60
            if cur_min != prev_min:
                lines.append(f"## {fmt_time(start)}")
                lines.append("")
                prev_min = cur_min
            lines.append(f"> {text}")
        lines.append("")
        return '\n'.join(lines)

    frame_ts = [ts for ts, _ in frames]
    frame_ts_upper = frame_ts[1:] + [float('inf')]

    seg_idx = 0
    n_segs = len(segments)

    pre_segs = []
    while seg_idx < n_segs and segments[seg_idx][0] < (frame_ts[0] if frame_ts else 0):
        pre_segs.append(segments[seg_idx])
        seg_idx += 1
    if pre_segs:
        lines.append(f"## {fmt_time(0)}")
        lines.append("")
        for _, _, t in pre_segs:
            lines.append(f"> {t}")
        lines.append("")

    for i, (fts, fpath) in enumerate(frames):
        next_ts = frame_ts_upper[i]

        lines.append(f"## {fmt_time(fts)}")
        lines.append(f"")
        lines.append(f"![]({asset_dir_name}/{fpath.name})")
        lines.append(f"")

        section_texts = []
        while seg_idx < n_segs and segments[seg_idx][0] < next_ts:
            section_texts.append(segments[seg_idx][2])
            seg_idx += 1

        if section_texts:
            lines.append(' '.join(section_texts))
        lines.append("")

    return '\n'.join(lines)


# ─────────────────────────── 主处理函数 ──────────────────────────

def process_video(src: str, out_dir: Path, model: str = 'small',
                  lang: str | None = None, threshold: float = 0.5,
                  extra_ydl: list[str] | None = None):
    """
    供 monitor.py import 并在线程中调用。
    out_dir: 输出目录，文件名由视频标题自动生成。
    """
    is_local = Path(src).exists()
    title, duration = get_video_info(src, extra_ydl)

    print(f"\n{'='*40}")
    print(f"标题：{title}")
    print(f"时长：{fmt_time(duration) if duration else '未知'}")
    print(f"{'='*40}\n")

    out_md    = Path(out_dir) / f"{safe_name(title)}.md"
    asset_dir = out_md.parent / f"{out_md.stem}_assets"
    asset_dir_name = asset_dir.name

    sw = StatusWriter(str(uuid.uuid4())[:8], title, src)
    sw.log(f"标题：{title}")
    sw.log(f"时长：{fmt_time(duration) if duration else '未知'}")

    try:
        with tempfile.TemporaryDirectory(prefix='video2md_') as tmp:
            work = Path(tmp)
            segments = None
            video_path = Path(src) if is_local else None

            if not is_local:
                sw.update("fetching_subtitles", "[字幕] 尝试平台字幕...", 0.02)
                sw.log("[字幕] 尝试平台字幕...")
                segments = try_platform_subtitles(src, work, status=sw, extra_args=extra_ydl)

            needs_download = not is_local and (segments is None or True)
            # 帧提取始终需要视频文件（在线视频必须下载）
            if not is_local:
                sw.update("downloading", "[下载] 下载视频...", 0.04)
                sw.log("[下载] 下载视频（最高 1080p）...")
                video_path = download_video(src, work, status=sw, extra_args=extra_ydl)

            # 字幕缓存：统一存到 EXE 同目录 cache/，处理完自动删除
            cache_key = Path(src).stem if is_local else safe_name(title)
            seg_cache = _cache_dir() / f"{cache_key}_segments.json"

            if segments is None and seg_cache.exists():
                sw.update("cached_segments", "[字幕] 加载缓存...", 0.08)
                sw.log("[字幕] 加载缓存字幕...")
                segments = [tuple(s) for s in json.loads(seg_cache.read_text('utf-8'))]
                sw.log(f"  → {len(segments)} 段（缓存）")
                print(f"[字幕] 加载缓存：{len(segments)} 段", flush=True)

            if segments is None:
                sw.update("extracting_audio", "[音频] 提取音轨...", 0.08)
                sw.log("[音频] 提取音轨...")
                audio = extract_audio(video_path, work)
                segments = transcribe(audio, model, lang, status=sw)
                out_md.parent.mkdir(parents=True, exist_ok=True)
                seg_cache.write_text(json.dumps(segments, ensure_ascii=False), encoding='utf-8')
                cache_msg = f"  → 字幕已缓存：{seg_cache.name}"
                print(cache_msg, flush=True)
                sw.log(cache_msg)

            sw.update("extracting_frames", "[截图] 场景检测关键帧...", 0.76)
            sw.log(f"[截图] 场景检测关键帧（阈值={threshold}）...")
            asset_dir.mkdir(parents=True, exist_ok=True)
            frames = extract_keyframes(video_path or Path(src), asset_dir, threshold)
            sw.log(f"  → {len(frames)} 帧")
            sw.update("building_md", "[合成] 生成 Markdown...", 0.92)
            sw.log("[合成] 生成 Markdown...")

            print("\n[合成] 生成 Markdown...", flush=True)
            md = build_markdown(title, src, segments or [], frames, asset_dir_name)
            out_md.parent.mkdir(parents=True, exist_ok=True)
            out_md.write_text(md, encoding='utf-8')

            # 处理成功后删除字幕缓存（失败时保留，供下次断点续用）
            try:
                seg_cache.unlink(missing_ok=True)
            except Exception:
                pass

    except Exception as exc:
        sw.error(str(exc))
        raise

    sw.complete(len(frames), len(segments) if segments else 0, str(out_md))

    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    print(f"\n{'='*40}")
    print(f"[OK] 完成")
    print(f"  文档：{out_md}")
    print(f"  截图：{asset_dir}（{len(frames)} 帧）")
    print(f"  字幕段：{len(segments) if segments else 0}")
    print(f"{'='*40}\n")
    print(str(out_md))


# ─────────────────────────── CLI 入口 ────────────────────────────

def main():
    ap = argparse.ArgumentParser(description='视频 → 高保真图文 Markdown')
    ap.add_argument('input', help='视频 URL 或本地路径')
    ap.add_argument('-o', '--output', default=None, help='输出 .md 路径')
    ap.add_argument('--outdir', default=None,
                    help='输出目录（用视频标题自动命名 .md 文件，与 -o 互斥）')
    ap.add_argument('--model', default='small',
                    choices=['tiny', 'base', 'small', 'medium'],
                    help='Whisper 模型大小（默认 small）')
    ap.add_argument('--lang', default=None, help='语言提示，如 zh / en')
    ap.add_argument('--threshold', default=0.5, type=float,
                    help='场景切换阈值 0~1（默认 0.5，越小截图越多）')
    ap.add_argument('--cookies-from-browser', default=None, metavar='BROWSER',
                    help='从浏览器读取 Cookie（chrome/edge/firefox），解决 B站 412')
    ap.add_argument('--cookies', default=None, metavar='FILE',
                    help='Netscape cookies.txt 文件路径')
    args = ap.parse_args()

    extra_ydl: list[str] = []
    if args.cookies_from_browser:
        extra_ydl += ['--cookies-from-browser', args.cookies_from_browser]
    if args.cookies:
        extra_ydl += ['--cookies', args.cookies]

    if args.output:
        out_dir  = Path(args.output).parent
        out_name = Path(args.output).name
        out_dir.mkdir(parents=True, exist_ok=True)
        # 直接处理指定输出路径
        src = args.input
        is_local = Path(src).exists()
        title, _ = get_video_info(src, extra_ydl)
        out_md = Path(args.output)
        asset_dir = out_md.parent / f"{out_md.stem}_assets"

        sw = StatusWriter(str(uuid.uuid4())[:8], title, src)
        process_video(src, out_md.parent, args.model, args.lang, args.threshold, extra_ydl)
    else:
        out_dir = Path(args.outdir) if args.outdir else Path('.')
        process_video(args.input, out_dir, args.model, args.lang, args.threshold, extra_ydl)


if __name__ == '__main__':
    main()
