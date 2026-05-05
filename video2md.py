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
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import yt_dlp

STATUS_FILE = Path.home() / '.video2md_status.json'

_BILI_RE = re.compile(r'bilibili\.com|b23\.tv', re.I)
_YT_RE   = re.compile(r'youtube\.com|youtu\.be', re.I)

_cancel_events: dict  = {}   # task_id -> threading.Event
_pause_events: dict   = {}   # task_id -> threading.Event (set means paused)
_status_writers: dict = {}   # task_id -> StatusWriter
_status_lock = threading.Lock()


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


def _apply_youtube_js_runtime(opts: dict, url: str) -> None:
    """YouTube needs a JS runtime plus yt-dlp-ejs to solve n challenges."""
    if not _YT_RE.search(url):
        return
    if not shutil.which('node'):
        return
    runtimes = opts.setdefault('js_runtimes', {})
    if isinstance(runtimes, dict):
        runtimes.setdefault('node', {})


def _apply_download_resilience(opts: dict, url: str) -> None:
    """Make long media transfers more tolerant of transient stream failures."""
    opts.setdefault('continuedl', True)
    opts.setdefault('retries', 15)
    opts.setdefault('fragment_retries', 25)
    opts.setdefault('file_access_retries', 5)
    opts.setdefault('extractor_retries', 5)
    opts.setdefault('socket_timeout', 30)
    if _YT_RE.search(url):
        # Smaller ranged HTTP chunks resume more cleanly when YouTube closes a stream early.
        opts.setdefault('http_chunk_size', 10 * 1024 * 1024)


def _subtitle_langs(url: str) -> list[str]:
    if _YT_RE.search(url):
        return [
            'en',
            'en-orig',
            '-live_chat',
        ]
    return ['all', '-live_chat']

_YT_BOT_RE     = re.compile(r'Sign in to confirm|bot|confirm you.re not', re.I)
_BILI_AUTH_RE  = re.compile(r'login|大会员|需要登录|请先登录|仅限|premium|vip', re.I)
_COOKIE_ERR_RE = re.compile(r'cookie|keyring|could not copy|permission denied|dpapi|decrypt', re.I)
_NET_RE        = re.compile(
    r'bytes read,\s*\d+\s*more expected|timed?\s*out|connection (?:reset|aborted)|'
    r'broken pipe|temporarily unavailable|remote end closed|incomplete read|'
    r'http error 5\d\d|server error',
    re.I,
)


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


def _has_cookie_opts(opts: dict) -> bool:
    return bool(opts.get('cookiefile') or opts.get('cookiesfrombrowser'))


def _cached_cookie_file(domain: str) -> 'Path | None':
    cached = _cache_dir() / f'cookies_{domain.replace(".", "_")}.txt'
    if cached.exists() and cached.stat().st_size > 0:
        return cached
    return None


def _cookie_file_chain(base_opts: dict, domain: str, label: str,
                       refresh: bool = False) -> list[tuple[dict, str]]:
    if _has_cookie_opts(base_opts):
        return []
    cookie_file = _cached_cookie_file(domain)
    if not cookie_file and refresh:
        cookie_file = _extract_edge_cookies_to_file(domain)
    if not cookie_file:
        return []
    return [({**base_opts, 'cookiefile': str(cookie_file)}, label)]


def _normalized_user_cookie_chain(base_opts: dict) -> list[tuple[dict, str]]:
    cookie_file = base_opts.get('cookiefile')
    if not cookie_file:
        return []
    normalized = _normalize_cookie_file(str(cookie_file))
    if normalized == cookie_file:
        return []
    return [({**base_opts, 'cookiefile': normalized}, '规范化Cookie')]


def _cookie_domain_needles(domain: str) -> list[str]:
    if domain == 'youtube.com':
        return ['youtube', 'google']
    return [domain.split('.')[0]]


def _subtitle_fallback_chain(base_opts: dict, url: str) -> list[tuple[dict, str]]:
    """
    字幕提取专用重试链（少即是多，避免触发 429）：
    - YouTube：移动端(ios/android) → web 默认（共 2 次）
    - B站：浏览器 Cookie → 默认（AI 字幕需要登录态）
    - 其他：仅默认
    """
    is_yt   = bool(_YT_RE.search(url))
    is_bili = bool(_BILI_RE.search(url))

    if is_yt:
        mobile = {**base_opts,
                  'extractor_args': {'youtube': {'player_client': ['ios', 'android']}}}
        normalized_cookie_chain = _normalized_user_cookie_chain(base_opts)
        if normalized_cookie_chain:
            return [(base_opts, '默认'), (mobile, '移动端')] + normalized_cookie_chain
        yt_cookie_chain = _cookie_file_chain(base_opts, 'youtube.com', 'Edge YouTube Cookie')
        if yt_cookie_chain:
            return yt_cookie_chain + [(base_opts, '默认'), (mobile, '移动端')]
        return [(base_opts, '默认'), (mobile, '移动端')]

    if is_bili:
        chain = []
        bili_cookies = get_bili_cookies_file()
        if bili_cookies:
            chain.append(({**base_opts, 'cookiefile': str(bili_cookies)}, 'Edge本地Cookie'))
        chain += _normalized_user_cookie_chain(base_opts)
        chain += _browser_cookie_chain(base_opts)
        chain.append((base_opts, '默认'))
        return chain

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

    if is_bili:
        bili_cookies = get_bili_cookies_file()
        if bili_cookies:
            chain.insert(0, ({**base_opts, 'cookiefile': str(bili_cookies)}, 'Edge本地Cookie'))

    chain += _normalized_user_cookie_chain(base_opts)

    if is_yt:
        yt_cookie_chain = _cookie_file_chain(base_opts, 'youtube.com', 'Edge YouTube Cookie')
        if yt_cookie_chain:
            chain = yt_cookie_chain + chain
        else:
            chain += _cookie_file_chain(base_opts, 'youtube.com', 'Edge YouTube Cookie', refresh=True)

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


def _runtime_cache_dir() -> Path:
    """返回运行期临时文件目录：cache/tmp/。"""
    d = _cache_dir() / 'tmp'
    d.mkdir(exist_ok=True)
    return d


def cleanup_runtime_cache(max_age_hours: float = 24.0) -> None:
    """清理异常退出遗留的运行期临时目录；正常任务由 TemporaryDirectory 自动删除。"""
    root = _runtime_cache_dir()
    cutoff = time.time() - max_age_hours * 3600
    for path in root.glob('video2md_*'):
        try:
            if path.is_dir() and path.stat().st_mtime < cutoff:
                shutil.rmtree(path, ignore_errors=True)
        except Exception:
            pass


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


def _decode_cookie_text(raw: bytes) -> str:
    for enc in ('utf-8-sig', 'utf-16', 'utf-16-le', 'utf-16-be'):
        try:
            text = raw.decode(enc)
            if '\x00' not in text[:100]:
                return text
        except UnicodeDecodeError:
            pass
    return raw.decode('utf-8', errors='replace')


def _json_cookie_to_netscape_line(cookie: dict) -> str | None:
    domain = cookie.get('domain') or cookie.get('host') or cookie.get('host_key')
    name = cookie.get('name')
    value = cookie.get('value', '')
    if not domain or not name:
        return None
    path = cookie.get('path') or '/'
    secure = 'TRUE' if cookie.get('secure') else 'FALSE'
    include_subdomains = 'TRUE' if str(domain).startswith('.') else 'FALSE'
    expires = cookie.get('expirationDate', cookie.get('expires', cookie.get('expires_at', 0)))
    try:
        expires_s = str(int(float(expires or 0)))
    except (TypeError, ValueError):
        expires_s = '0'
    if cookie.get('httpOnly') or cookie.get('http_only'):
        domain = '#HttpOnly_' + str(domain)
    return '\t'.join([str(domain), include_subdomains, str(path), secure, expires_s,
                      str(name), str(value)])


def _normalize_cookie_file(cookie_path: str) -> str:
    """Rewrite user-selected cookies into a conservative Netscape file for yt-dlp."""
    src = Path(cookie_path).expanduser()
    if not src.exists():
        return cookie_path
    try:
        text = _decode_cookie_text(src.read_bytes()).replace('\r\n', '\n').replace('\r', '\n')
        lines: list[str] = ['# Netscape HTTP Cookie File']
        stripped = text.lstrip()
        if stripped.startswith(('[', '{')):
            data = json.loads(stripped)
            cookies = data.get('cookies', data) if isinstance(data, dict) else data
            for cookie in cookies if isinstance(cookies, list) else []:
                if isinstance(cookie, dict):
                    line = _json_cookie_to_netscape_line(cookie)
                    if line:
                        lines.append(line)
        else:
            for raw_line in text.split('\n'):
                line = raw_line.strip('\ufeff')
                if not line:
                    continue
                if line.startswith('#HttpOnly_'):
                    fields = line.split('\t')
                elif line.startswith('#'):
                    continue
                else:
                    fields = line.split('\t')
                if len(fields) == 7:
                    lines.append('\t'.join(fields))
        if len(lines) == 1:
            return cookie_path
        digest = hashlib.sha1(str(src.resolve()).encode('utf-8', errors='replace')).hexdigest()[:12]
        out_path = _cache_dir() / f'user_cookies_{digest}.txt'
        out_path.write_text('\n'.join(lines) + '\n', encoding='utf-8', newline='\n')
        return str(out_path)
    except Exception:
        return cookie_path


# ─────────────────────────── 浏览器 Cookie 自动提取 ──────────────

def _cdp_ws_send(sock, obj: dict):
    """发送 WebSocket 文本帧（FIN=1, opcode=text, 带掩码，Client→Server）。"""
    import struct as _st
    data = json.dumps(obj).encode()
    n    = len(data)
    hdr  = bytearray([0x81])
    if   n <= 125:   hdr.append(n | 0x80)
    elif n <= 65535: hdr += bytes([126 | 0x80]) + _st.pack('>H', n)
    else:            hdr += bytes([127 | 0x80]) + _st.pack('>Q', n)
    mask = os.urandom(4)
    hdr += mask
    sock.sendall(bytes(hdr) + bytes(data[i] ^ mask[i % 4] for i in range(n)))


def _cdp_ws_recv(sock) -> dict:
    """接收 WebSocket 帧（Server→Client，无掩码），处理分片，返回 JSON dict。"""
    import struct as _st

    def read(n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("WebSocket closed")
            buf += chunk
        return buf

    payload = b""
    while True:
        h      = read(2)
        fin    = bool(h[0] & 0x80)
        op     = h[0] & 0x0f
        masked = bool(h[1] & 0x80)
        length = h[1] & 0x7f
        if length == 126: length = _st.unpack('>H', read(2))[0]
        elif length == 127: length = _st.unpack('>Q', read(8))[0]
        mk    = read(4) if masked else b""
        frame = read(length)
        if masked:
            frame = bytes(frame[i] ^ mk[i % 4] for i in range(length))
        if op in (0, 1, 2):  # continuation / text / binary
            payload += frame
        if fin:
            break
    try:
        return json.loads(payload.decode())
    except Exception:
        return {}


def _cdp_get_all_cookies(ws_url: str) -> list:
    """通过 CDP WebSocket 调用 Network.getAllCookies，返回 cookie 列表。"""
    import socket as _sock, base64 as _b64

    url       = ws_url.removeprefix('ws://')
    host_part, _, rest = url.partition('/')
    host, _, port_s    = host_part.rpartition(':')
    ws_path   = '/' + rest

    s = _sock.socket()
    s.settimeout(10)
    try:
        s.connect((host, int(port_s)))
        key = _b64.b64encode(os.urandom(16)).decode()
        s.sendall((
            f"GET {ws_path} HTTP/1.1\r\nHost: {host_part}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        ).encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = s.recv(4096)
            if not chunk:
                return []
            buf += chunk
        _cdp_ws_send(s, {"id": 1, "method": "Network.getAllCookies"})
        for _ in range(50):
            msg = _cdp_ws_recv(s)
            if msg.get('id') == 1:
                return msg.get('result', {}).get('cookies', [])
        return []
    except Exception:
        return []
    finally:
        s.close()


def _cdp_extract_cookies(domain: str) -> 'Path | None':
    """启动 headless Edge 调试实例，通过 CDP 提取 Cookie（支持 Edge 127+ App-Bound Encryption）。"""
    import socket as _sock, http.client as _http, time as _time

    edge_exe = None
    for pf_key in ('ProgramFiles(x86)', 'ProgramFiles'):
        p = Path(os.environ.get(pf_key, '')) / 'Microsoft/Edge/Application/msedge.exe'
        if p.exists():
            edge_exe = str(p); break
    if not edge_exe:
        return None

    with _sock.socket() as tmp:
        tmp.bind(('127.0.0.1', 0))
        cdp_port = tmp.getsockname()[1]

    user_data = str(Path.home() / 'AppData/Local/Microsoft/Edge/User Data')
    proc = subprocess.Popen(
        [edge_exe,
         f'--remote-debugging-port={cdp_port}',
         '--headless=new',
         f'--user-data-dir={user_data}',
         '--no-first-run', '--no-default-browser-check',
         '--disable-extensions', '--disable-sync',
         '--disable-gpu', '--log-level=3',
         'about:blank'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        page_ws = None
        for _ in range(40):  # 最多等 10 秒
            try:
                c = _http.HTTPConnection('127.0.0.1', cdp_port, timeout=1)
                c.request('GET', '/json')
                targets = json.loads(c.getresponse().read().decode())
                for t in targets:
                    if t.get('type') == 'page' and 'webSocketDebuggerUrl' in t:
                        page_ws = t['webSocketDebuggerUrl']; break
                if page_ws:
                    break
            except Exception:
                pass
            _time.sleep(0.25)
        if not page_ws:
            return None

        all_cookies  = _cdp_get_all_cookies(page_ws)
        needles = _cookie_domain_needles(domain)
        bili_cookies = [c for c in all_cookies
                        if any(n in c.get('domain', '') for n in needles)]
        if not bili_cookies:
            return None

        out_path = _cache_dir() / f'cookies_{domain.replace(".", "_")}.txt'
        lines = ['# Netscape HTTP Cookie File\n']
        for ck in bili_cookies:
            d = ck['domain']
            lines.append('\t'.join([
                d,
                'TRUE' if d.startswith('.') else 'FALSE',
                ck.get('path', '/'),
                'TRUE' if ck.get('secure', False) else 'FALSE',
                str(int(ck.get('expires', -1))),
                ck['name'],
                ck.get('value', ''),
            ]) + '\n')
        out_path.write_text(''.join(lines), encoding='utf-8')
        return out_path

    finally:
        try:
            proc.terminate(); proc.wait(timeout=5)
        except Exception:
            proc.kill()


def _aesgcm_extract_cookies(domain: str) -> 'Path | None':
    """旧式 DPAPI+AESGCM 方案（Edge < 127 备用）。"""
    try:
        import json as _json, base64 as _b64, sqlite3 as _sq
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        import win32crypt
    except ImportError:
        return None
    try:
        edge_base   = Path.home() / 'AppData/Local/Microsoft/Edge/User Data/Default'
        local_state = edge_base.parent / 'Local State'
        if not local_state.exists():
            return None
        ls  = _json.loads(local_state.read_text('utf-8'))
        enc_key = _b64.b64decode(ls['os_crypt']['encrypted_key'])[5:]
        key = win32crypt.CryptUnprotectData(enc_key, None, None, None, 0)[1]
        cookie_db = edge_base / 'Network' / 'Cookies'
        if not cookie_db.exists():
            cookie_db = edge_base / 'Cookies'
        if not cookie_db.exists():
            return None
        needles = _cookie_domain_needles(domain)
        where = ' OR '.join(['host_key LIKE ?'] * len(needles))
        params = tuple(f'%{n}%' for n in needles)
        with _sq.connect(str(cookie_db), timeout=0.3) as conn:
            rows = conn.execute(
                'SELECT host_key, name, path, encrypted_value, expires_utc, is_secure '
                f'FROM cookies WHERE {where}', params
            ).fetchall()
        if not rows:
            return None
        out_path = _cache_dir() / f'cookies_{domain.replace(".", "_")}.txt'
        lines = ['# Netscape HTTP Cookie File\n']
        ok = 0
        for host, name, path_, enc_val, expires, secure in rows:
            try:
                val = AESGCM(key).decrypt(enc_val[3:15], enc_val[15:], b'').decode('utf-8')
                exp = str(int(expires / 1_000_000 - 11_644_473_600)) if expires else '0'
                flag = 'TRUE' if host.startswith('.') else 'FALSE'
                sec  = 'TRUE' if secure else 'FALSE'
                lines.append(f'{host}\t{flag}\t{path_}\t{sec}\t{exp}\t{name}\t{val}\n')
                ok += 1
            except Exception:
                pass
        if ok == 0:
            return None
        out_path.write_text(''.join(lines), encoding='utf-8')
        return out_path
    except Exception:
        return None


def _extract_edge_cookies_to_file(domain: str = 'bilibili.com') -> 'Path | None':
    """
    主入口：CDP 方案优先（支持 Edge 127+ App-Bound Encryption），
    失败则退回旧式 AESGCM（Edge < 127）。Edge 运行时直接返回 None。
    """
    if sys.platform != 'win32':
        return None
    if 'edge' in _running_browsers():
        return None
    return _cdp_extract_cookies(domain) or _aesgcm_extract_cookies(domain)


def refresh_bili_cookies() -> 'Path | None':
    """供外部调用：尝试刷新 B站 Cookie 缓存，返回 cookies.txt 路径。"""
    return _extract_edge_cookies_to_file('bilibili.com')


def refresh_youtube_cookies() -> 'Path | None':
    """供外部调用：尝试刷新 YouTube Cookie 缓存，返回 cookies.txt 路径。"""
    return _extract_edge_cookies_to_file('youtube.com')


def get_bili_cookies_file() -> 'Path | None':
    """返回可用的 B站 cookies.txt（仅读缓存，不触发提取）。"""
    cached = _cache_dir() / 'cookies_bilibili_com.txt'
    if cached.exists() and cached.stat().st_size > 0:
        return cached
    return None


def get_youtube_cookies_file() -> 'Path | None':
    """返回可用的 YouTube cookies.txt（仅读缓存，不触发提取）。"""
    return _cached_cookie_file('youtube.com')


def cancel_task(task_id: str) -> bool:
    """取消正在处理的任务：标记 StatusWriter 不再写入，并设置 cancel 事件。"""
    sw = _status_writers.get(task_id)
    if sw:
        sw.cancelled = True
    pause_ev = _pause_events.get(task_id)
    if pause_ev:
        pause_ev.clear()
    ev = _cancel_events.get(task_id)
    if ev:
        ev.set()
        return True
    return False


def pause_task(task_id: str) -> bool:
    """暂停正在处理的任务。"""
    pause_ev = _pause_events.get(task_id)
    sw = _status_writers.get(task_id)
    if not pause_ev or not sw:
        return False
    pause_ev.set()
    sw.pause()
    return True


def resume_task(task_id: str) -> bool:
    """恢复正在处理的任务。"""
    pause_ev = _pause_events.get(task_id)
    sw = _status_writers.get(task_id)
    if not pause_ev or not sw:
        return False
    pause_ev.clear()
    sw.resume()
    return True


# ─────────────────────────── 状态写入 ────────────────────────────

class StatusWriter:
    """向 monitor.py 轮询的 JSON 文件写进度。"""

    def __init__(self, task_id: str, title: str, source: str):
        self.task_id = task_id
        self.cancelled = False
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

    def pause(self):
        if self._data.get("status") == "paused":
            return
        self._data["_resume_status"] = self._data.get("status", "processing")
        self._data["status"] = "paused"
        self._data["step_label"] = "[暂停] 已暂停"
        self.log("[暂停] 已暂停")

    def resume(self):
        if self._data.get("status") != "paused":
            return
        resume_status = self._data.pop("_resume_status", "processing")
        self._data["status"] = resume_status if resume_status != "paused" else "processing"
        if "_resume_step" in self._data:
            self._data["step"] = self._data.pop("_resume_step")
        if "_resume_label" in self._data:
            self._data["step_label"] = self._data.pop("_resume_label")
        if "_resume_progress" in self._data:
            self._data["progress"] = self._data.pop("_resume_progress")
        self.log("[暂停] 已继续")
        self._flush()

    def wait_if_paused(self, cancel_ev: 'threading.Event | None' = None):
        pause_ev = _pause_events.get(self.task_id)
        if not pause_ev:
            return
        logged = False
        while pause_ev.is_set():
            if cancel_ev and cancel_ev.is_set():
                return
            if not logged:
                self.pause()
                logged = True
            time.sleep(0.2)
        if logged:
            self.resume()

    def update(self, step: str, label: str, progress: float):
        if self._data.get("status") == "paused":
            self._data["_resume_step"] = step
            self._data["_resume_label"] = label
            self._data["_resume_progress"] = min(progress, 0.99)
            return
        self._data["status"] = "processing"
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
        if self.cancelled:
            return
        with _status_lock:
            try:
                tasks = []
                if STATUS_FILE.exists():
                    try:
                        tasks = json.loads(STATUS_FILE.read_text(encoding='utf-8')).get("tasks", [])
                    except Exception:
                        tasks = []
                for i, task in enumerate(tasks):
                    if task.get("id") == self.task_id:
                        tasks[i] = self._data
                        break
                else:
                    tasks.append(self._data)
                STATUS_FILE.write_text(json.dumps({"tasks": tasks}, ensure_ascii=False, indent=2),
                                       encoding='utf-8')
            except Exception:
                pass


# ─────────────────────────── 工具函数 ────────────────────────────

class _SilentLogger:
    """yt-dlp 日志全部静默，用于浏览器 Cookie 尝试避免 DPAPI/DB锁 噪音。"""
    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass


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
    _apply_youtube_js_runtime(opts, src)
    for attempt_opts, label in _platform_fallback_chain(opts, src):
        try:
            run_opts = {**attempt_opts, 'logger': _SilentLogger()} if 'Cookie' in label else attempt_opts
            with yt_dlp.YoutubeDL(run_opts) as ydl:
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
    _apply_youtube_js_runtime(opts, url)
    _apply_download_resilience(opts, url)
    opts.update({
        'writesubtitles': True,
        'writeautomaticsub': True,
        'subtitleslangs': _subtitle_langs(url),
        'subtitlesformat': 'vtt/srt/best',
        'skip_download': True,
        'format': 'best',
        'outtmpl': str(sub_dir / '%(title)s'),
    })

    # B站诊断：先 extract_info 列出可用字幕语言（print 到 stdout，不受 UI 30 行限制）
    if _BILI_RE.search(url):
        try:
            diag_opts = {**opts, 'writesubtitles': False, 'writeautomaticsub': False,
                         'logger': _SilentLogger()}
            bili_ck = get_bili_cookies_file()
            cookie_note = f'cookie={bili_ck.name}' if bili_ck else 'cookie=无'
            if bili_ck:
                diag_opts['cookiefile'] = str(bili_ck)
            with yt_dlp.YoutubeDL(diag_opts) as ydl:
                info = ydl.extract_info(url, download=False)
            subs = list((info or {}).get('subtitles', {}).keys())
            auto = list((info or {}).get('automatic_captions', {}).keys())
            msg = (f"[字幕诊断] {cookie_note} | 字幕: {subs} | 自动字幕: {auto}"
                   + ('' if (subs or auto) else ' ← 均为空'))
            print(msg, flush=True)
            if status:
                status.log(f"  {msg}")
        except Exception as e:
            msg = f"[字幕诊断] 失败: {e}"
            print(msg, flush=True)
            if status:
                status.log(f"  {msg}")

    for attempt_opts, label in _subtitle_fallback_chain(opts, url):
        try:
            run_opts = {**attempt_opts, 'logger': _SilentLogger()}
            with yt_dlp.YoutubeDL(run_opts) as ydl:
                ydl.download([url])
            # 无异常，但要确认文件真的生成了
            if list(sub_dir.glob('*')):
                break  # 拿到任何字幕文件，停止重试
            # 无文件（可能需要 Cookie 才能访问字幕 API），继续尝试
            if status:
                status.log(f"  [字幕] {label} 未返回字幕文件，继续尝试...")
        except Exception as e:
            s = str(e)
            if '429' in s or 'Too Many Requests' in s:
                break  # 限流，静默退出，交给 Whisper
            is_browser = 'Cookie' in label
            is_retryable = bool(_YT_BOT_RE.search(s) or _BILI_AUTH_RE.search(s) or _COOKIE_ERR_RE.search(s))
            if is_browser or is_retryable:
                if label != '默认' and status:
                    status.log(f"  [字幕] {label} 失败，继续尝试...")
                continue
            if status:
                status.log(f"  [字幕] {s[:120]}")
            break

    all_sub_files = [f for f in sub_dir.glob('*') if f.suffix in ('.vtt', '.srt')]
    if status:
        all_downloaded = list(sub_dir.glob('*'))
        if all_downloaded:
            status.log(f"  [字幕] 已下载文件: {[f.name for f in all_downloaded]}")
    if not all_sub_files:
        print("  → 无平台字幕，将使用 Whisper。", flush=True)
        return None

    # 优先取中文字幕
    preferred = next((f for f in all_sub_files if any(
        tag in f.name for tag in ('zh', 'cn', 'CN', 'Hans', 'ai')
    )), all_sub_files[0])
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
        if status:
            status.wait_if_paused(_cancel_events.get(status.task_id))
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
    _apply_youtube_js_runtime(opts, url)
    _apply_download_resilience(opts, url)
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
        for net_attempt in range(1, 4):
            try:
                run_opts = {**attempt_opts, 'logger': _SilentLogger()} if 'Cookie' in label else attempt_opts
                with yt_dlp.YoutubeDL(run_opts) as ydl:
                    ydl.download([url])
                last_err = None
                break
            except yt_dlp.utils.DownloadError as e:
                last_err = e
                s = str(e)
                is_browser = 'Cookie' in label
                is_auth_retryable = bool(_YT_BOT_RE.search(s) or _BILI_AUTH_RE.search(s) or _COOKIE_ERR_RE.search(s))
                is_net_retryable = bool(_NET_RE.search(s))
                if is_net_retryable and net_attempt < 3:
                    if status:
                        status.log(f"  [下载] 网络中断，自动续传重试 {net_attempt}/2...")
                    continue
                if is_browser or is_auth_retryable:
                    if label != '默认' and status:
                        status.log(f"  [下载] {label} 失败，继续尝试...")
                    break
                break  # 非 bot/cookie/网络 错误，不重试
        if last_err is None:
            break
        if _NET_RE.search(str(last_err)):
            break

    if last_err:
        err = str(last_err)
        if _YT_RE.search(url) and _YT_BOT_RE.search(err):
            err += (
                "\n\nYouTube needs logged-in cookies for this request. "
                "Log in to YouTube in Edge/Chrome, close the browser so cookies can be read, "
                "or export a Netscape cookies.txt and select it in the Cookie field."
            )
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
        if status:
            status.wait_if_paused(_cancel_events.get(status.task_id))
        segs.append((s.start, s.end, s.text.strip()))
        if status:
            prog = 0.10 + 0.65 * min(s.end / total_dur, 1.0)
            status.update("transcribing", f"[转录] {s.end:.0f}s / {total_dur:.0f}s", prog)
    return _merge_segs(segs)


# ─────────────────────────── 关键帧提取 ──────────────────────────

SCREENSHOT_FREQUENCY_THRESHOLDS = {
    'Small': 0.5,
    'Medium': 0.4,
    'High': 0.3,
    'Max': 0.12,
}
DEFAULT_SCREENSHOT_FREQUENCY = 'Small'


def screenshot_threshold(frequency: str | None = None, threshold: float | None = None) -> float:
    if threshold is not None:
        return float(threshold)
    if frequency in SCREENSHOT_FREQUENCY_THRESHOLDS:
        return SCREENSHOT_FREQUENCY_THRESHOLDS[frequency]
    return SCREENSHOT_FREQUENCY_THRESHOLDS[DEFAULT_SCREENSHOT_FREQUENCY]


def extract_keyframes(video_path: Path, out_dir: Path,
                      threshold: float = 0.5):
    """
    场景检测关键帧提取。threshold 越小，截图越多。
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

    # 仍不足则改用间隔截图兜底
    if not frames:
        print("  → 无场景变化，改用间隔截图（每30秒）。", flush=True)
        return _interval_frames(video_path, out_dir)

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

def process_video(src: str, out_dir: Path, model: str = 'medium',
                  lang: str | None = None, threshold: float | None = None,
                  extra_ydl: list[str] | None = None,
                  task_id: str | None = None,
                  screenshot_frequency: str | None = None):
    """
    供 monitor.py import 并在线程中调用。
    out_dir: 输出目录，文件名由视频标题自动生成。
    """
    cleanup_runtime_cache()
    is_local = Path(src).exists()
    initial_title = Path(src).stem if is_local else (src[:70] or 'video')
    sw = StatusWriter(task_id or str(uuid.uuid4())[:8], initial_title, src)
    # 注册取消机制，先于网络探测，保证 GUI 中刚启动的任务也能取消。
    cancel_ev = threading.Event()
    pause_ev = threading.Event()
    _cancel_events[sw.task_id] = cancel_ev
    _pause_events[sw.task_id] = pause_ev
    _status_writers[sw.task_id] = sw

    try:
        sw.wait_if_paused(cancel_ev)
        threshold = screenshot_threshold(screenshot_frequency, threshold)
        title, duration = get_video_info(src, extra_ydl)
    except Exception as exc:
        sw.error(str(exc))
        _cancel_events.pop(sw.task_id, None)
        _pause_events.pop(sw.task_id, None)
        _status_writers.pop(sw.task_id, None)
        raise
    sw._data['title'] = title
    sw._flush()

    print(f"\n{'='*40}")
    print(f"标题：{title}")
    print(f"时长：{fmt_time(duration) if duration else '未知'}")
    print(f"{'='*40}\n")

    out_md    = Path(out_dir) / f"{safe_name(title)}.md"
    asset_dir = out_md.parent / f"{out_md.stem}_assets"
    asset_dir_name = asset_dir.name

    # 将输出路径写入状态供取消时清理。
    sw._data['pending_output_md']  = str(out_md)
    sw._data['pending_asset_dir']  = str(asset_dir)
    sw._flush()

    sw.log(f"标题：{title}")
    sw.log(f"时长：{fmt_time(duration) if duration else '未知'}")

    try:
        if cancel_ev.is_set():
            raise RuntimeError('cancelled')
        with tempfile.TemporaryDirectory(prefix='video2md_', dir=_runtime_cache_dir()) as tmp:
            work = Path(tmp)
            segments = None
            video_path = Path(src) if is_local else None

            sw.wait_if_paused(cancel_ev)
            if not is_local:
                sw.update("fetching_subtitles", "[字幕] 尝试平台字幕...", 0.02)
                sw.log("[字幕] 尝试平台字幕...")
                segments = try_platform_subtitles(src, work, status=sw, extra_args=extra_ydl)

            sw.wait_if_paused(cancel_ev)
            if cancel_ev.is_set():
                raise RuntimeError('cancelled')

            needs_download = not is_local and (segments is None or True)
            # 帧提取始终需要视频文件（在线视频必须下载）
            if not is_local:
                sw.update("downloading", "[下载] 下载视频...", 0.04)
                sw.log("[下载] 下载视频（最高 1080p）...")
                video_path = download_video(src, work, status=sw, extra_args=extra_ydl)

            sw.wait_if_paused(cancel_ev)
            if cancel_ev.is_set():
                raise RuntimeError('cancelled')

            # 字幕中转缓存：放在本次运行临时目录内，任务结束自动删除。
            cache_key = Path(src).stem if is_local else safe_name(title)
            seg_cache = work / f"{cache_key}_segments.json"

            if segments is None and seg_cache.exists():
                sw.update("cached_segments", "[字幕] 加载缓存...", 0.08)
                sw.log("[字幕] 加载缓存字幕...")
                segments = [tuple(s) for s in json.loads(seg_cache.read_text('utf-8'))]
                sw.log(f"  → {len(segments)} 段（缓存）")
                print(f"[字幕] 加载缓存：{len(segments)} 段", flush=True)

            if segments is None:
                sw.wait_if_paused(cancel_ev)
                sw.update("extracting_audio", "[音频] 提取音轨...", 0.08)
                sw.log("[音频] 提取音轨...")
                audio = extract_audio(video_path, work)
                sw.wait_if_paused(cancel_ev)
                segments = transcribe(audio, model, lang, status=sw)
                out_md.parent.mkdir(parents=True, exist_ok=True)
                seg_cache.write_text(json.dumps(segments, ensure_ascii=False), encoding='utf-8')
                cache_msg = f"  → 字幕已缓存：{seg_cache.name}"
                print(cache_msg, flush=True)
                sw.log(cache_msg)

            if cancel_ev.is_set():
                raise RuntimeError('cancelled')

            sw.wait_if_paused(cancel_ev)
            sw.update("extracting_frames", "[截图] 场景检测关键帧...", 0.76)
            sw.log(f"[截图] 场景检测关键帧（阈值={threshold}）...")
            asset_dir.mkdir(parents=True, exist_ok=True)
            frames = extract_keyframes(video_path or Path(src), asset_dir, threshold)
            sw.wait_if_paused(cancel_ev)
            sw.log(f"  → {len(frames)} 帧")
            sw.update("building_md", "[合成] 生成 Markdown...", 0.92)
            sw.log("[合成] 生成 Markdown...")

            print("\n[合成] 生成 Markdown...", flush=True)
            md = build_markdown(title, src, segments or [], frames, asset_dir_name)
            out_md.parent.mkdir(parents=True, exist_ok=True)
            out_md.write_text(md, encoding='utf-8')

            # 运行期中转文件在 TemporaryDirectory 退出时统一清理。
            try:
                seg_cache.unlink(missing_ok=True)
            except Exception:
                pass

    except Exception as exc:
        if not cancel_ev.is_set():
            sw.error(str(exc))
        raise
    finally:
        _cancel_events.pop(sw.task_id, None)
        _pause_events.pop(sw.task_id, None)
        _status_writers.pop(sw.task_id, None)

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
    ap.add_argument('--model', default='medium',
                    choices=['tiny', 'base', 'small', 'medium', 'large-v3'],
                    help='Whisper 模型大小（默认 small）')
    ap.add_argument('--lang', default=None, help='语言提示，如 zh / en')
    ap.add_argument('--screenshot-frequency', default=DEFAULT_SCREENSHOT_FREQUENCY,
                    choices=list(SCREENSHOT_FREQUENCY_THRESHOLDS.keys()),
                    help='截图频率档位：Small=0.5, Medium=0.4, High=0.3, Max=0.12')
    ap.add_argument('--threshold', default=None, type=float,
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
        process_video(src, out_md.parent, args.model, args.lang, args.threshold, extra_ydl,
                      screenshot_frequency=args.screenshot_frequency)
    else:
        out_dir = Path(args.outdir) if args.outdir else Path('.')
        process_video(args.input, out_dir, args.model, args.lang, args.threshold, extra_ydl,
                      screenshot_frequency=args.screenshot_frequency)


if __name__ == '__main__':
    main()
