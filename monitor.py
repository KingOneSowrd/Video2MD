#!/usr/bin/env python3
"""
monitor.py — Video Ingest Queue Monitor（portable 版）
Retro HUD · PyQt6 · CRT dot-matrix animated operator

portable 版改动：
- import video2md，_spawn() 改为 threading.Thread 调用 video2md.process_video()
- RAW_SOURCES 默认改为 ~/Videos/VideoIngest（从 config 读取，不再硬编码 wiki 路径）
- OutputPanel._commit() 保存输出目录到 config
- _git_commit() 动态查找 .git 目录，不依赖硬编码 WIKI_ROOT
"""

import json
import math
import os
import random
import re
import shutil
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import Qt, QTimer, QPointF, QRectF, QUrl, pyqtSignal, QObject
from PyQt6.QtGui import (QColor, QFont, QPainter, QPen, QBrush,
                          QPalette, QPainterPath, QIcon, QPixmap, QImage,
                          QLinearGradient, QRadialGradient, QAction)
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget,
                              QVBoxLayout, QHBoxLayout, QLabel,
                              QScrollArea, QFrame, QTextEdit,
                              QGraphicsDropShadowEffect, QPushButton,
                              QLineEdit, QMenu, QSystemTrayIcon,
                              QFileDialog)
from PyQt6.QtMultimedia import QMediaPlayer, QAudioOutput, QVideoSink, QVideoFrame

import webbrowser

import video2md  # portable: in-process processing

STATUS_FILE  = Path.home() / '.video2md_status.json'
MONITOR_CFG  = Path.home() / '.video2md_monitor.json'

def _cfg_load() -> dict:
    try:
        return json.loads(MONITOR_CFG.read_text(encoding='utf-8'))
    except Exception:
        return {}

def _cfg_save(key: str, value: str):
    cfg = _cfg_load()
    cfg[key] = value
    try:
        MONITOR_CFG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding='utf-8')
    except Exception:
        pass

# ── Paths ─────────────────────────────────────────────────
RAW_SOURCES = Path(_cfg_load().get('output_dir',
                   str(Path.home() / 'Videos' / 'VideoIngest')))
PEON_CFG    = Path.home() / '.claude/hooks/peon-ping/config.json'
PEON_PACKS  = Path.home() / '.claude/hooks/peon-ping/packs'

# ── Character source image / video (configurable) ────────
def _asset(name: str) -> Path:
    base = Path(sys._MEIPASS) if getattr(sys, 'frozen', False) else Path(__file__).parent
    return base / 'assets' / name

CHAR_IMG   = _asset('Karby.png')
CHAR_VIDEO = _asset('Karby_Video.mp4')

# ── Palette ──────────────────────────────────────────────
C_BG      = QColor(6,  2,  0)
C_GRID    = QColor(82, 24,  0)
C_PRIMARY = QColor(255, 88,  0)
C_BRIGHT  = QColor(255, 165,  0)
C_DIM     = QColor(110, 30,  0)
C_SUCCESS = QColor(190, 130,  0)
C_ERROR   = QColor(255,  40, 40)
C_BORDER  = QColor(180,  50,  0)

def _css(c): return f"rgb({c.red()},{c.green()},{c.blue()})"
def _mono(sz, bold=False):
    f = QFont("Courier New", sz)
    f.setFamilies(["Courier New", "Source Han Sans CN"])
    f.setBold(bold)
    return f
def _glow(c, r=12):
    fx = QGraphicsDropShadowEffect()
    fx.setBlurRadius(r); fx.setColor(c); fx.setOffset(0, 0)
    return fx


# ── Window icon (programmatic) ────────────────────────────
def make_icon() -> QIcon:
    pm = QPixmap(64, 64)
    pm.fill(C_BG)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(QPen(C_PRIMARY, 2)); p.setBrush(QBrush(QColor(12, 3, 0)))
    p.drawRoundedRect(5, 5, 54, 42, 3, 3)
    p.setPen(Qt.PenStyle.NoPen)
    for row in range(5):
        for col in range(8):
            b = 0.2 + 0.8 * abs(math.sin((row * 2 + col) * 0.8))
            c = QColor(C_PRIMARY); c.setAlphaF(b)
            p.setBrush(QBrush(c))
            p.drawEllipse(QPointF(14 + col * 6, 15 + row * 6), b * 2.2, b * 2.2)
    p.setPen(QPen(C_PRIMARY, 2))
    p.drawLine(32, 47, 32, 56); p.drawLine(22, 56, 42, 56)
    p.end()
    return QIcon(pm)


# ── Sound Player ──────────────────────────────────────────
class SoundPlayer(QObject):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._player = QMediaPlayer(self)
        self._audio  = QAudioOutput(self)
        self._player.setAudioOutput(self._audio)
        self._sounds: dict[str, list[Path]] = self._load()
        vol = 0.8
        try:
            cfg = json.loads(PEON_CFG.read_text(encoding='utf-8'))
            vol = float(cfg.get('volume', 0.8))
        except Exception:
            pass
        self._audio.setVolume(vol)

    def _load(self) -> dict:
        try:
            cfg  = json.loads(PEON_CFG.read_text(encoding='utf-8'))
            pack = cfg.get('default_pack', 'peon')
            mf_p = PEON_PACKS / pack / 'openpeon.json'
            mf   = json.loads(mf_p.read_text(encoding='utf-8'))
            out: dict[str, list[Path]] = {}
            for cat, d in mf.get('categories', {}).items():
                key = cat.replace('.', '_')
                candidates = [PEON_PACKS / pack / s['file']
                              for s in d.get('sounds', [])]
                out[key] = [p for p in candidates if p.exists()]
            return out
        except Exception:
            return {}

    def play(self, category: str):
        files = self._sounds.get(category, [])
        if not files:
            return
        path = random.choice(files)
        self._player.setSource(QUrl.fromLocalFile(str(path)))
        self._player.play()


# ── CRT Wireframe Globe Widget ────────────────────────────
class CRTCharWidget(QWidget):
    SZ      = 156
    FPS_MS  = 50
    N_FRAME = 72

    _CG = QColor(255,  50,  15)
    _CS = QColor(  0, 210, 160)
    _CC = QColor(255, 165,   0)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(self.SZ, self.SZ)
        self.setStyleSheet("background: transparent;")
        self._idx = 0
        self._frames: list[QPixmap] = []
        self._prerender()
        t = QTimer(self); t.timeout.connect(self._tick); t.start(self.FPS_MS)

    def _prerender(self):
        for fi in range(self.N_FRAME):
            self._frames.append(self._draw_frame(fi))

    def _draw_frame(self, fi: int) -> QPixmap:
        pm = QPixmap(self.SZ, self.SZ)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setBrush(Qt.BrushStyle.NoBrush)

        SZ = self.SZ
        cx = cy = SZ // 2
        R  = 54
        KY = 0.62
        KR = 0.34
        rot = fi / self.N_FRAME * 2 * math.pi

        def proj(phi, lon):
            sx = cx + R * math.cos(phi) * math.cos(lon)
            sy = cy - R * (math.sin(phi) * KY
                           + math.cos(phi) * math.sin(lon) * KR)
            return sx, sy

        def _path(lon, n=56) -> QPainterPath:
            path = QPainterPath()
            for j in range(n + 1):
                phi = -math.pi / 2 + j * math.pi / n
                x, y = proj(phi, lon)
                path.moveTo(x, y) if j == 0 else path.lineTo(x, y)
            return path

        def glow_draw(draw_fn, col, w_thin, w_glow=None, a_glow=55):
            gc = QColor(col); gc.setAlpha(a_glow)
            p.setPen(QPen(gc, w_glow or w_thin * 4.5,
                         Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            draw_fn()
            p.setPen(QPen(col, w_thin,
                         Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            draw_fn()

        BL = 17
        p.setPen(QPen(self._CC, 1.5))
        for bx, by, sx, sy in ((9,9,1,1),(SZ-9,9,-1,1),
                                (9,SZ-9,1,-1),(SZ-9,SZ-9,-1,-1)):
            p.drawLine(bx, by, bx + sx*BL, by)
            p.drawLine(bx, by, bx, by + sy*BL)

        for lat_deg in (-50, -25, 0, 25, 50):
            phi = math.radians(lat_deg)
            ey  = cy - R * math.sin(phi) * KY
            rx  = R * math.cos(phi)
            ry  = rx * KR
            rect = QRectF(cx - rx, ey - ry, rx * 2, ry * 2)
            w = 1.1 if lat_deg == 0 else 0.7
            glow_draw(lambda r=rect: p.drawEllipse(r), self._CG, w)

        for i in range(8):
            lon = rot + i * math.pi / 8
            path = _path(lon)
            glow_draw(lambda path=path: p.drawPath(path), self._CG, 0.7)

        beam_a = rot * 0.55
        half   = SZ * 0.74
        for perp in (0.0, math.pi / 2):
            a  = beam_a + perp
            dx = math.cos(a) * half
            dy = math.sin(a) * half
            glow_draw(
                lambda dx=dx, dy=dy: p.drawLine(
                    QPointF(cx - dx, cy - dy),
                    QPointF(cx + dx, cy + dy),
                ),
                self._CS, 1.4, w_glow=6, a_glow=62
            )

        deg = int(rot / (2 * math.pi) * 360) % 360
        p.setPen(QPen(self._CC))
        p.setFont(_mono(6))
        p.drawText(QRectF(0, SZ - 16, SZ, 14),
                   Qt.AlignmentFlag.AlignCenter, f"旋转  {deg:03d}°")
        p.end()
        return pm

    def _tick(self):
        self._idx = (self._idx + 1) % self.N_FRAME
        self.update()

    def paintEvent(self, _):
        if not self._frames: return
        p = QPainter(self)
        p.drawPixmap(0, 0, self._frames[self._idx])
        p.end()


# ── CRT Character Portrait Widget ─────────────────────────
class CRTCharImageWidget(QWidget):
    SZ      = 156
    FPS_MS  = 66
    N_FRAME = 32

    _CC = QColor(255, 165, 0)

    def __init__(self, img_path: Path, parent=None):
        super().__init__(parent)
        self.setFixedSize(self.SZ, self.SZ)
        self.setStyleSheet("background: transparent;")
        self._idx = 0
        self._base_pm = self._make_base(img_path)
        self._frames  = [self._draw_frame(i) for i in range(self.N_FRAME)]
        t = QTimer(self); t.timeout.connect(self._tick); t.start(self.FPS_MS)

    def _make_base(self, img_path: Path) -> QPixmap | None:
        if not img_path.exists():
            return None
        src = QImage(str(img_path))
        if src.isNull():
            return None
        sz    = min(src.width(), src.height())
        x0    = (src.width()  - sz) // 2
        y0    = (src.height() - sz) // 2
        inner = self.SZ - 20
        scaled = src.copy(x0, y0, sz, sz).scaled(
            inner, inner,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.FastTransformation,
        )
        argb = scaled.convertToFormat(QImage.Format.Format_ARGB32)
        w, h = argb.width(), argb.height()
        ptr = argb.bits(); ptr.setsize(w * h * 4)
        buf = bytearray(ptr)
        self._flood_remove_bg_buf(buf, w, h, threshold=230)
        self._amber_tint_buf(buf, w, h)
        result = QImage(bytes(buf), w, h, w * 4, QImage.Format.Format_ARGB32).copy()
        return QPixmap.fromImage(result)

    @staticmethod
    def _flood_remove_bg_buf(buf: bytearray, w: int, h: int, threshold: int = 230) -> None:
        from collections import deque
        stride = w * 4

        def is_bg(off: int) -> bool:
            return (buf[off + 3] > 0 and
                    buf[off + 2] > threshold and
                    buf[off + 1] > threshold and
                    buf[off    ] > threshold)

        visited: bytearray = bytearray(w * h)
        queue: deque = deque()
        for sx, sy in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)):
            flat = sy * w + sx
            if not visited[flat] and is_bg(sy * stride + sx * 4):
                visited[flat] = 1
                queue.append((sx, sy))

        while queue:
            x, y = queue.popleft()
            off = y * stride + x * 4
            buf[off] = buf[off + 1] = buf[off + 2] = buf[off + 3] = 0
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nx, ny = x + dx, y + dy
                if 0 <= nx < w and 0 <= ny < h:
                    nflat = ny * w + nx
                    if not visited[nflat] and is_bg(ny * stride + nx * 4):
                        visited[nflat] = 1
                        queue.append((nx, ny))

    @staticmethod
    def _amber_tint_buf(buf: bytearray, w: int, h: int) -> None:
        try:
            import numpy as np
            arr  = np.frombuffer(buf, dtype=np.uint8).reshape(h, w, 4).copy()
            mask = arr[:, :, 3] > 0
            lum  = ((76  * arr[:, :, 2].astype(np.uint16) +
                     150 * arr[:, :, 1].astype(np.uint16) +
                     29  * arr[:, :, 0].astype(np.uint16)) >> 8).astype(np.uint8)
            arr[:, :, 2] = np.where(mask, lum, 0)
            arr[:, :, 1] = np.where(mask, (lum.astype(np.uint16) * 148 >> 8).astype(np.uint8), 0)
            arr[:, :, 0] = np.where(mask, (lum.astype(np.uint16) *  24 >> 8).astype(np.uint8), 0)
            buf[:] = arr.tobytes()
        except ImportError:
            for i in range(0, w * h * 4, 4):
                if buf[i + 3] == 0:
                    continue
                lum        = (76 * buf[i + 2] + 150 * buf[i + 1] + 29 * buf[i]) >> 8
                buf[i + 2] = lum
                buf[i + 1] = lum * 148 >> 8
                buf[i    ] = lum *  24 >> 8

    def _draw_frame(self, fi: int) -> QPixmap:
        pm = QPixmap(self.SZ, self.SZ)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        SZ = self.SZ
        screen = QRectF(8, 8, SZ - 16, SZ - 16)

        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor(3, 1, 0)))
        p.drawRoundedRect(screen, 6, 6)

        if self._base_pm:
            clip = QPainterPath()
            clip.addRoundedRect(screen, 6, 6)
            p.setClipPath(clip)
            iw = self._base_pm.width()
            ih = self._base_pm.height()
            ix  = int(screen.left() + (screen.width()  - iw) / 2)
            iy  = int(screen.top()  + (screen.height() - ih) / 2)
            p.drawPixmap(ix, iy, self._base_pm)
            p.setClipping(False)

        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor(0, 0, 0, 68)))
        for y in range(int(screen.top()), int(screen.bottom()), 3):
            p.drawRect(int(screen.left()), y, int(screen.width()), 1)

        beam_y = int(screen.top() + (fi / self.N_FRAME) * screen.height())
        beam_h = 14
        grad = QLinearGradient(0, beam_y, 0, beam_y + beam_h)
        def _a(c, alpha):
            x = QColor(c); x.setAlpha(alpha); return x
        grad.setColorAt(0.0, _a(C_PRIMARY,  0))
        grad.setColorAt(0.3, _a(C_PRIMARY, 42))
        grad.setColorAt(0.7, _a(C_PRIMARY, 42))
        grad.setColorAt(1.0, _a(C_PRIMARY,  0))
        p.setBrush(QBrush(grad))
        p.drawRect(int(screen.left()), beam_y, int(screen.width()), beam_h)
        sc = QColor(C_BRIGHT); sc.setAlpha(95)
        p.setPen(QPen(sc, 1.0)); p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawLine(int(screen.left()), beam_y, int(screen.right()), beam_y)

        cx = int(screen.center().x()); cy = int(screen.center().y())
        vg = QRadialGradient(cx, cy, int(screen.width() * 0.60))
        vg.setColorAt(0.35, QColor(0, 0, 0,   0))
        vg.setColorAt(1.00, QColor(0, 0, 0, 165))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(vg))
        p.drawRoundedRect(screen, 6, 6)

        bc = QColor(C_PRIMARY); bc.setAlpha(160)
        p.setPen(QPen(bc, 1.0)); p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(screen, 6, 6)

        BL = 17
        p.setPen(QPen(self._CC, 1.5))
        for bx, by, sx, sy in ((9,9,1,1),(SZ-9,9,-1,1),(9,SZ-9,1,-1),(SZ-9,SZ-9,-1,-1)):
            p.drawLine(bx, by, bx + sx*BL, by)
            p.drawLine(bx, by, bx, by + sy*BL)

        p.setPen(QPen(self._CC))
        p.setFont(_mono(6))
        no_signal = self._base_pm is None
        label = "无  信号" if no_signal else "身份已确认"
        p.drawText(QRectF(0, SZ - 16, SZ, 14), Qt.AlignmentFlag.AlignCenter, label)
        p.end()
        return pm

    def _tick(self):
        self._idx = (self._idx + 1) % self.N_FRAME
        self.update()

    def paintEvent(self, _):
        if not self._frames: return
        p = QPainter(self)
        p.drawPixmap(0, 0, self._frames[self._idx])
        p.end()


# ── CRT Video Portrait Widget ─────────────────────────────
class CRTCharVideoWidget(QWidget):
    SZ     = 156
    FPS_MS = 50

    _CC = QColor(255, 165, 0)

    def __init__(self, video_path: Path, parent=None):
        super().__init__(parent)
        self.setFixedSize(self.SZ, self.SZ)
        self.setStyleSheet("background: transparent;")
        self._frame_pm: QPixmap | None = None
        self._beam_pos = 0.0

        self._player = QMediaPlayer(self)
        self._audio  = QAudioOutput(self)
        self._player.setAudioOutput(self._audio)
        self._audio.setVolume(0.0)  # muted — video portrait is silent
        self._sink   = QVideoSink(self)
        self._player.setVideoSink(self._sink)
        self._sink.videoFrameChanged.connect(self._on_video_frame)

        if video_path.exists():
            self._player.setSource(QUrl.fromLocalFile(str(video_path)))
            self._player.setLoops(-1)
            self._player.play()

        t = QTimer(self); t.timeout.connect(self._tick); t.start(self.FPS_MS)

    def _on_video_frame(self, vframe: QVideoFrame):
        if not vframe.isValid():
            return
        img = vframe.toImage()
        if img.isNull():
            return
        self._frame_pm = self._process_frame(img)
        self.update()

    def _process_frame(self, src: QImage) -> QPixmap:
        sz    = min(src.width(), src.height())
        x0    = (src.width()  - sz) // 2
        y0    = (src.height() - sz) // 2
        inner = self.SZ - 20
        scaled = src.copy(x0, y0, sz, sz).scaled(
            inner, inner,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.FastTransformation,
        )
        argb = scaled.convertToFormat(QImage.Format.Format_ARGB32)
        w, h = argb.width(), argb.height()
        ptr  = argb.bits(); ptr.setsize(w * h * 4)
        buf  = bytearray(ptr)
        CRTCharImageWidget._amber_tint_buf(buf, w, h)
        result = QImage(bytes(buf), w, h, w * 4, QImage.Format.Format_ARGB32).copy()
        return QPixmap.fromImage(result)

    def _tick(self):
        self._beam_pos = (self._beam_pos + 0.02) % 1.0
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        SZ     = self.SZ
        screen = QRectF(8, 8, SZ - 16, SZ - 16)
        R      = 3

        def _a(c, alpha): x = QColor(c); x.setAlpha(alpha); return x

        ow = QRectF(0.5, 0.5, SZ - 1, SZ - 1)
        p.setBrush(Qt.BrushStyle.NoBrush)
        for pw, alpha in ((10, 12), (5, 30), (2, 60)):
            p.setPen(QPen(_a(C_BORDER, alpha), pw))
            p.drawRect(ow)
        p.setPen(QPen(C_BORDER, 1.0))
        p.drawRect(ow)

        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor(3, 1, 0)))
        p.drawRoundedRect(screen, R, R)

        if self._frame_pm:
            clip = QPainterPath()
            clip.addRoundedRect(screen, R, R)
            p.setClipPath(clip)
            iw = self._frame_pm.width()
            ih = self._frame_pm.height()
            ix = int(screen.left() + (screen.width()  - iw) / 2)
            iy = int(screen.top()  + (screen.height() - ih) / 2)
            p.drawPixmap(ix, iy, self._frame_pm)
            p.setClipping(False)

        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor(0, 0, 0, 68)))
        for y in range(int(screen.top()), int(screen.bottom()), 3):
            p.drawRect(int(screen.left()), y, int(screen.width()), 1)

        beam_y = int(screen.top() + self._beam_pos * screen.height())
        beam_h = 16
        grad   = QLinearGradient(0, beam_y, 0, beam_y + beam_h)
        grad.setColorAt(0.0, _a(C_PRIMARY,  0))
        grad.setColorAt(0.3, _a(C_PRIMARY, 50))
        grad.setColorAt(0.7, _a(C_PRIMARY, 50))
        grad.setColorAt(1.0, _a(C_PRIMARY,  0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(grad))
        p.drawRect(int(screen.left()), beam_y, int(screen.width()), beam_h)
        p.setPen(QPen(_a(C_BRIGHT, 110), 1.0))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawLine(int(screen.left()), beam_y, int(screen.right()), beam_y)

        cx_ = int(screen.center().x()); cy_ = int(screen.center().y())
        vg  = QRadialGradient(cx_, cy_, int(screen.width() * 0.58))
        vg.setColorAt(0.30, QColor(0, 0, 0,   0))
        vg.setColorAt(1.00, QColor(0, 0, 0, 190))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(vg))
        p.drawRoundedRect(screen, R, R)

        for pw, alpha in ((7, 22), (3, 55), (1, 180)):
            p.setPen(QPen(_a(C_PRIMARY, alpha), pw))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(screen, R, R)

        BL = 17
        p.setPen(QPen(self._CC, 1.5))
        for bx, by, sx, sy in ((9,9,1,1),(SZ-9,9,-1,1),(9,SZ-9,1,-1),(SZ-9,SZ-9,-1,-1)):
            p.drawLine(bx, by, bx + sx*BL, by)
            p.drawLine(bx, by, bx, by + sy*BL)

        p.setFont(_mono(6))
        p.setPen(QPen(self._CC))
        p.drawText(QRectF(4, 1, 70, 7),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   "FEED·A" if self._frame_pm else "NO SIG")
        p.setPen(QPen(_a(self._CC, 130)))
        p.drawText(QRectF(SZ - 42, 1, 38, 7),
                   Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                   f"0×{int(self._beam_pos * 255):02X}")
        p.setPen(QPen(self._CC))
        p.drawText(QRectF(0, SZ - 9, SZ, 8),
                   Qt.AlignmentFlag.AlignCenter,
                   "机魂已部署" if self._frame_pm else "无  信号")
        p.end()


# ── Background ────────────────────────────────────────────
class HUDBackground(QWidget):
    _GS = 28

    def __init__(self, parent=None):
        super().__init__(parent)
        self._off = 0.0
        t = QTimer(self); t.timeout.connect(self._tick); t.start(60)

    def _tick(self):
        self._off = (self._off + 0.4) % self._GS
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        w, h = self.width(), self.height()

        bg = QLinearGradient(0, 0, 0, h)
        bg.setColorAt(0.00, QColor( 9,  3,  0))
        bg.setColorAt(0.15, QColor(11,  4,  0))
        bg.setColorAt(0.32, QColor(14,  5,  0))
        bg.setColorAt(0.52, QColor(20,  7,  1))
        bg.setColorAt(0.72, QColor(28, 10,  1))
        bg.setColorAt(0.88, QColor(36, 13,  2))
        bg.setColorAt(1.00, QColor(44, 16,  2))
        p.fillRect(self.rect(), QBrush(bg))

        br = QRadialGradient(w * 0.88, h * 0.94, max(w, h) * 0.62)
        br.setColorAt(0.00, QColor(68, 24,  3, 190))
        br.setColorAt(0.30, QColor(40, 14,  1, 100))
        br.setColorAt(0.65, QColor(16,  5,  0,  40))
        br.setColorAt(1.00, QColor( 0,  0,  0,   0))
        p.fillRect(self.rect(), QBrush(br))

        bl = QRadialGradient(w * 0.10, h * 0.96, max(w, h) * 0.45)
        bl.setColorAt(0.00, QColor(48, 17,  2, 120))
        bl.setColorAt(0.50, QColor(20,  7,  0,  40))
        bl.setColorAt(1.00, QColor( 0,  0,  0,   0))
        p.fillRect(self.rect(), QBrush(bl))

        gs  = self._GS
        off = int(self._off)
        for y in range(-gs + off, h + gs, gs):
            t = max(0.0, min(1.0, y / h))
            alpha = int(18 + t * 72)
            p.setPen(QPen(QColor(105, 38, 6, alpha), 1))
            p.drawLine(0, y, w, y)
        for x in range(-gs + off, w + gs, gs):
            tx = max(0.0, min(1.0, x / w))
            alpha = int(14 + tx * 55)
            p.setPen(QPen(QColor(105, 38, 6, alpha), 1))
            p.drawLine(x, 0, x, h)

        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor(0, 0, 0, 44)))
        for y in range(0, h, 4):
            p.drawRect(0, y, w, 2)
        p.end()


# ── Helpers ───────────────────────────────────────────────
def glabel(text, size=9, bold=False, color=None, glow_r=12):
    lbl = QLabel(text)
    lbl.setFont(_mono(size, bold))
    c = color or C_PRIMARY
    lbl.setStyleSheet(f"color: {_css(c)}; background: transparent;")
    lbl.setGraphicsEffect(_glow(c, glow_r))
    return lbl

class Panel(QFrame):
    def __init__(self, title="", parent=None):
        super().__init__(parent)
        self._title = title
        self.setStyleSheet("QFrame { border: none; background: rgba(10, 2, 0, 198); }")

    def paintEvent(self, e):
        super().paintEvent(e)
        p = QPainter(self)
        p.setBrush(Qt.BrushStyle.NoBrush)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        for pw, alpha in ((8, 28), (4, 52)):
            gc = QColor(C_BORDER); gc.setAlpha(alpha)
            p.setPen(QPen(gc, pw))
            p.drawRect(r)
        p.setPen(QPen(C_BORDER, 1.5))
        p.drawRect(r)
        if self._title:
            p.setFont(_mono(8))
            p.setPen(QPen(C_DIM))
            p.drawText(10, 13, f"── {self._title} ──")
        p.end()

def _divider():
    f = QFrame(); f.setFrameShape(QFrame.Shape.HLine)
    f.setFixedHeight(1); f.setStyleSheet(f"background: {_css(C_GRID)}; border: none;")
    return f

def _retro_btn(text: str, color=None) -> QPushButton:
    c = color or C_DIM
    btn = QPushButton(text)
    btn.setFont(_mono(8))
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    btn.setFixedHeight(20)
    btn.setStyleSheet(f"""
        QPushButton {{
            color: {_css(c)};
            background: transparent;
            border: 1px solid {_css(c)};
            padding: 0 6px;
        }}
        QPushButton:hover {{
            color: {_css(C_PRIMARY)};
            border-color: {_css(C_PRIMARY)};
        }}
        QPushButton:pressed {{
            background: rgba(255,88,0,40);
        }}
    """)
    return btn


# ── Input panel (drag-drop + URL entry) ───────────────────
class InputPanel(Panel):
    """Drop a local video file or paste a URL to dispatch a new job."""

    submitted = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__("投喂队列", parent)
        self.setAcceptDrops(True)
        self.setFixedHeight(52)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 16, 10, 8)
        lay.setSpacing(8)

        self._input = QLineEdit()
        self._input.setFont(_mono(8))
        self._input.setPlaceholderText("拖入视频文件 · 粘贴 URL · 回车发射")
        self._input.setStyleSheet(f"""
            QLineEdit {{
                color: {_css(C_PRIMARY)};
                background: rgba(20,5,0,200);
                border: 1px solid {_css(C_DIM)};
                padding: 2px 6px;
                selection-background-color: {_css(C_PRIMARY)};
            }}
            QLineEdit:focus {{
                border-color: {_css(C_PRIMARY)};
            }}
        """)
        self._input.returnPressed.connect(self._dispatch)
        lay.addWidget(self._input, stretch=1)

        btn = _retro_btn("▶ DISPATCH", C_PRIMARY)
        btn.clicked.connect(self._dispatch)
        lay.addWidget(btn)

    def _dispatch(self):
        text = self._input.text().strip()
        if text:
            self.submitted.emit(text)
            self._input.clear()

    # ── Drag-drop ─────────────────────────────────────────
    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls() or e.mimeData().hasText():
            e.acceptProposedAction()

    def dragMoveEvent(self, e):
        e.acceptProposedAction()

    def dropEvent(self, e):
        if e.mimeData().hasUrls():
            for url in e.mimeData().urls():
                src = url.toLocalFile() or url.toString()
                if src:
                    self.submitted.emit(src)
        elif e.mimeData().hasText():
            self.submitted.emit(e.mimeData().text().strip())


# ── Output directory panel ───────────────────────────────
class OutputPanel(Panel):
    """Directory picker for output location."""

    changed = pyqtSignal(str)

    def __init__(self, default_path: Path, parent=None):
        super().__init__("输出目录", parent)
        self.setAcceptDrops(True)
        self.setFixedHeight(52)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 16, 10, 8)
        lay.setSpacing(8)

        self._input = QLineEdit()
        self._input.setFont(_mono(8))
        self._input.setPlaceholderText("输出目录（拖入文件夹 · 手动输入 · 回车确认）")
        self._input.setText(str(default_path))
        self._input.setStyleSheet(f"""
            QLineEdit {{
                color: {_css(C_PRIMARY)};
                background: rgba(20,5,0,200);
                border: 1px solid {_css(C_DIM)};
                padding: 2px 6px;
                selection-background-color: {_css(C_PRIMARY)};
            }}
            QLineEdit:focus {{
                border-color: {_css(C_PRIMARY)};
            }}
        """)
        self._input.returnPressed.connect(self._commit)
        self._input.editingFinished.connect(self._commit)
        lay.addWidget(self._input, stretch=1)

        btn = _retro_btn("◉ 浏览", C_DIM)
        btn.clicked.connect(self._browse)
        lay.addWidget(btn)

    def _browse(self):
        start = self._input.text().strip() or str(Path.home())
        d = QFileDialog.getExistingDirectory(self, "选择输出目录", start)
        if d:
            self._input.setText(d)
            _cfg_save('output_dir', d)
            self.changed.emit(d)

    def _commit(self):
        p = self._input.text().strip()
        if p:
            _cfg_save('output_dir', p)
            self.changed.emit(p)

    def current_path(self) -> Path:
        t = self._input.text().strip()
        return Path(t) if t else RAW_SOURCES

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dragMoveEvent(self, e):
        e.acceptProposedAction()

    def dropEvent(self, e):
        for url in e.mimeData().urls():
            p = url.toLocalFile()
            if p and Path(p).is_dir():
                self._input.setText(p)
                _cfg_save('output_dir', p)
                self.changed.emit(p)
                break


# ── Cookies file panel ───────────────────────────────────
class CookiesPanel(Panel):
    """Optional cookies.txt file picker (avoids browser DB lock)."""

    def __init__(self, parent=None):
        super().__init__("Cookie 文件（仅Bilibili需要）", parent)
        self.setAcceptDrops(True)
        self.setFixedHeight(52)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 16, 10, 8)
        lay.setSpacing(8)

        self._input = QLineEdit()
        self._input.setFont(_mono(8))
        self._input.setPlaceholderText("cookies.txt 路径（留空则不使用 Cookie，B站登录内容须填写）")
        self._input.setStyleSheet(f"""
            QLineEdit {{
                color: {_css(C_PRIMARY)};
                background: rgba(20,5,0,200);
                border: 1px solid {_css(C_DIM)};
                padding: 2px 6px;
                selection-background-color: {_css(C_PRIMARY)};
            }}
            QLineEdit:focus {{
                border-color: {_css(C_PRIMARY)};
            }}
        """)
        saved = _cfg_load().get('cookies_path', '')
        if saved:
            self._input.setText(saved)
        lay.addWidget(self._input, stretch=1)

        btn = _retro_btn("◉ 浏览", C_DIM)
        btn.clicked.connect(self._browse)
        lay.addWidget(btn)

    def _browse(self):
        start = self._input.text().strip() or str(Path.home())
        f, _ = QFileDialog.getOpenFileName(
            self, "选择 cookies.txt", start, "文本文件 (*.txt);;所有文件 (*)"
        )
        if f:
            self._input.setText(f)
            _cfg_save('cookies_path', f)

    def cookies_args(self) -> list[str]:
        """返回传给 video2md.process_video() 的 extra_ydl 参数列表。"""
        p = self._input.text().strip()
        if p and Path(p).exists():
            return ['--cookies', p]
        return []


# ── Retro progress bar ────────────────────────────────────
class RetroBar(QWidget):
    def __init__(self, value: float = 0.0, indeterminate: bool = False,
                 complete: bool = False, parent=None):
        super().__init__(parent)
        self._v     = max(0.0, min(1.0, value))
        self._indet = indeterminate
        self._done  = complete
        self._phase = 0.0
        self.setFixedHeight(11)
        if indeterminate:
            t = QTimer(self); t.timeout.connect(self._tick); t.start(50)

    def _tick(self):
        self._phase = (self._phase + 0.045) % 1.0
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        w, h = self.width(), self.height()
        inner = QRectF(1, 1, w - 2, h - 2)

        p.setPen(Qt.PenStyle.NoPen)
        p.fillRect(QRectF(0, 0, w, h), QColor(20, 5, 0))

        if self._indet:
            cx = self._phase * (w + 80) - 40
            g  = QLinearGradient(cx - 40, 0, cx + 40, 0)
            ac = C_PRIMARY
            g.setColorAt(0.0, QColor(ac.red(), ac.green(), ac.blue(), 0))
            g.setColorAt(0.5, QColor(ac.red(), ac.green(), ac.blue(), 155))
            g.setColorAt(1.0, QColor(ac.red(), ac.green(), ac.blue(), 0))
            p.fillRect(inner, QBrush(g))
        elif self._v > 0 or self._done:
            fw = inner.width() if self._done else max(4.0, inner.width() * self._v)
            g  = QLinearGradient(inner.left(), 0, inner.left() + fw, 0)
            if self._done:
                g.setColorAt(0.0,  QColor(160,  60,  0))
                g.setColorAt(0.45, QColor(230, 110,  0))
                g.setColorAt(1.0,  QColor(255, 170, 30))
            else:
                g.setColorAt(0.0,  QColor(180,  15,  0))
                g.setColorAt(0.35, QColor(255,  50,  0))
                g.setColorAt(0.70, QColor(255, 105,  0))
                g.setColorAt(1.0,  QColor(255, 170, 20))
            p.fillRect(QRectF(inner.left(), inner.top(), fw, inner.height()), QBrush(g))

        p.setPen(QPen(QColor(0, 0, 0, 85), 1))
        for frac in (0.25, 0.5, 0.75):
            x = inner.left() + inner.width() * frac
            p.drawLine(QPointF(x, inner.top()), QPointF(x, inner.bottom()))

        bc2 = QColor(C_DIM if self._done else C_PRIMARY); bc2.setAlpha(100)
        p.setPen(QPen(bc2, 1)); p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(QRectF(0.5, 0.5, w - 1, h - 1))
        p.end()


# ── Task row ──────────────────────────────────────────────
STEP_LABEL = {
    "queued":            "排队中",
    "fetching_subtitles":"获取字幕",
    "downloading":       "下载中",
    "extracting_audio":  "提取音频",
    "cached_segments":   "缓存命中",
    "transcribing":      "转录中",
    "extracting_frames": "提取帧",
    "building_md":       "生成文档",
    "complete":          "完成",
    "error":             "错误",
}

STATUS_LABEL = {
    "queued":     "排队中",
    "processing": "处理中",
    "complete":   "完成",
    "error":      "错误",
}

class TaskRow(QWidget):
    def __init__(self, task, on_remove=None, on_retry=None, on_open=None, parent=None):
        super().__init__(parent)
        self.setStyleSheet("background: transparent;")
        status = task.get("status", "queued")
        color_map = {"queued": C_DIM, "processing": C_PRIMARY,
                     "complete": C_SUCCESS, "error": C_ERROR}
        icon_map  = {"queued": "◈", "processing": "▶", "complete": "✓", "error": "✕"}
        c  = color_map.get(status, C_DIM)
        ic = icon_map.get(status, "◈")

        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 5, 10, 3); lay.setSpacing(3)

        r1 = QHBoxLayout(); r1.setSpacing(8)
        icon_lbl = QLabel(ic); icon_lbl.setFont(_mono(9)); icon_lbl.setFixedWidth(14)
        icon_lbl.setStyleSheet(f"color: {_css(c)}; background: transparent;")
        if status == "processing": icon_lbl.setGraphicsEffect(_glow(C_PRIMARY, 18))
        r1.addWidget(icon_lbl)
        tc = C_BRIGHT if status == "processing" else c
        r1.addWidget(glabel(task.get("title","?")[:52].upper(), 9,
                             bold=(status=="processing"), color=tc,
                             glow_r=12 if status=="processing" else 6))
        r1.addStretch()
        r1.addWidget(glabel(f"[ {STATUS_LABEL.get(status, status)} ]", 8, color=c, glow_r=6))

        if status == "complete":
            tid      = task.get("id")
            out_md   = task.get("output_md")
            if on_open and out_md:
                open_btn = _retro_btn("打开", C_SUCCESS)
                open_btn.setFixedWidth(38)
                open_btn.clicked.connect(lambda checked=False, p=out_md: on_open(p))
                r1.addWidget(open_btn)
            if on_remove:
                rm_btn = _retro_btn("×", C_DIM)
                rm_btn.setFixedWidth(22)
                rm_btn.clicked.connect(lambda checked=False, i=tid: on_remove(i))
                r1.addWidget(rm_btn)
        elif status == "error":
            tid = task.get("id")
            if on_retry:
                btn = _retro_btn("⟳", C_ERROR)
                btn.setFixedWidth(22)
                btn.clicked.connect(lambda checked=False, t=task: on_retry(t))
                r1.addWidget(btn)
            if on_remove:
                rm_btn = _retro_btn("×", C_DIM)
                rm_btn.setFixedWidth(22)
                rm_btn.clicked.connect(lambda checked=False, i=tid: on_remove(i))
                r1.addWidget(rm_btn)

        lay.addLayout(r1)

        if status in ("processing", "complete", "error"):
            r2 = QHBoxLayout(); r2.setSpacing(8)
            pv = task.get("progress", 0.0)
            pb = RetroBar(
                value=pv,
                indeterminate=(status == "processing" and pv == 0),
                complete=(status == "complete"),
            )
            r2.addWidget(pb, 3)
            sk = task.get("step", status)
            st = STEP_LABEL.get(sk, sk.upper())
            if status == "processing" and pv > 0:
                st += f"  {int(pv*100)}%"
            r2.addWidget(glabel(st, 8,
                                 color=C_DIM if status=="complete" else C_PRIMARY,
                                 glow_r=7), 2)
            lay.addLayout(r2)

        if status == "complete":
            parts = []
            if task.get("frames")   is not None: parts.append(f"{task['frames']} 帧")
            if task.get("segments") is not None: parts.append(f"{task['segments']} 段")
            out_md = task.get("output_md")
            if out_md:
                short = Path(out_md).name
                parts.append(f"→ {short}")
            if parts:
                lay.addWidget(glabel("  " + " · ".join(parts), 8, color=C_DIM, glow_r=5))

        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"background:{_css(C_GRID)}; border:none;"); sep.setFixedHeight(1)
        lay.addWidget(sep)


# ── Dependency checks ────────────────────────────────────

def check_ffmpeg() -> bool:
    """ffmpeg 是否可用（PATH / EXE 同目录 / sys._MEIPASS）。"""
    if shutil.which('ffmpeg'):
        return True
    if getattr(sys, 'frozen', False):
        exe_dir = Path(sys.executable).parent
        if (exe_dir / 'ffmpeg.exe').exists():
            return True
    return False


def check_whisper_model(model_size: str = 'small') -> bool:
    """Whisper 模型是否已在本机缓存。"""
    import os
    hf_home = Path(os.environ.get('HF_HOME',
                   str(Path.home() / '.cache' / 'huggingface')))
    model_dir = hf_home / 'hub' / f'models--Systran--faster-whisper-{model_size}'
    if not model_dir.exists():
        return False
    for p in model_dir.rglob('model.bin'):
        try:
            if p.stat().st_size > 100_000:
                return True
        except Exception:
            pass
    return False


# ── Setup window ─────────────────────────────────────────

class SetupWindow(QMainWindow):
    """首次运行依赖检测窗口，依赖满足后发出 proceed_signal，主窗口再显示。"""

    proceed_signal = pyqtSignal()

    def __init__(self, ffmpeg_ok: bool, whisper_ok: bool, parent=None):
        super().__init__(parent)
        self._ffmpeg_ok      = ffmpeg_ok
        self._whisper_ok     = whisper_ok
        self._ffmpeg_running = False
        self._whisper_running = False

        self.setWindowTitle("VideoIngest · 环境检测")
        self.setWindowIcon(make_icon())
        self.resize(620, 400)
        self.setMinimumSize(520, 340)
        p = self.palette()
        p.setColor(QPalette.ColorRole.Window, C_BG)
        self.setPalette(p)
        self._build_ui()

    # ── UI ────────────────────────────────────────────────
    def _build_ui(self):
        bg = HUDBackground(self)
        self.setCentralWidget(bg)
        root = QVBoxLayout(bg)
        root.setContentsMargins(14, 12, 14, 10)
        root.setSpacing(8)

        # Header
        hdr = QHBoxLayout()
        hdr.addWidget(glabel("◈  环境检测", 13, bold=True, color=C_PRIMARY, glow_r=22))
        hdr.addStretch()
        root.addLayout(hdr)
        root.addWidget(_divider())

        # Dependency panel
        dp = Panel("依赖状态")
        dl = QVBoxLayout(dp)
        dl.setContentsMargins(14, 22, 14, 12)
        dl.setSpacing(14)
        dl.addLayout(self._build_ffmpeg_row())
        dl.addWidget(_divider())
        dl.addLayout(self._build_whisper_row())
        root.addWidget(dp, stretch=1)

        # Note
        note = glabel(
            "注：点击「自动下载」后程序将自动完成安装，无需手动操作。"
            " Whisper 模型仅在视频无平台字幕时使用，B站 / YouTube 有字幕视频可直接跳过。",
            8, color=C_DIM, glow_r=4)
        note.setWordWrap(True)
        root.addWidget(note)

        root.addWidget(_divider())

        # Proceed button
        bot = QHBoxLayout()
        bot.addStretch()
        btn = _retro_btn("▶  进入主界面", C_PRIMARY)
        btn.setFixedHeight(26)
        btn.clicked.connect(self.proceed_signal.emit)
        bot.addWidget(btn)
        root.addLayout(bot)

    def _build_ffmpeg_row(self) -> QHBoxLayout:
        row = QHBoxLayout(); row.setSpacing(12)

        c  = C_SUCCESS if self._ffmpeg_ok else C_ERROR
        ic = "✓" if self._ffmpeg_ok else "✗"
        self._ffmpeg_icon = glabel(ic, 11, bold=True, color=c, glow_r=10)
        row.addWidget(self._ffmpeg_icon)

        col = QVBoxLayout(); col.setSpacing(3)
        col.addWidget(glabel("ffmpeg  ·  视频帧提取", 9, bold=True,
                              color=C_BRIGHT, glow_r=8))
        msg = "已就绪" if self._ffmpeg_ok else "未找到"
        self._ffmpeg_status = glabel(msg, 8, color=c, glow_r=5)
        col.addWidget(self._ffmpeg_status)
        self._ffmpeg_log = glabel("", 7, color=C_DIM, glow_r=4)
        col.addWidget(self._ffmpeg_log)
        row.addLayout(col)
        row.addStretch()

        if not self._ffmpeg_ok:
            self._ffmpeg_btn = _retro_btn("自动下载", C_DIM)
            self._ffmpeg_btn.clicked.connect(self._download_ffmpeg)
            row.addWidget(self._ffmpeg_btn)

        return row

    def _build_whisper_row(self) -> QHBoxLayout:
        row = QHBoxLayout(); row.setSpacing(12)

        c  = C_SUCCESS if self._whisper_ok else C_ERROR
        ic = "✓" if self._whisper_ok else "✗"
        self._w_icon = glabel(ic, 11, bold=True, color=c, glow_r=10)
        row.addWidget(self._w_icon)

        col = QVBoxLayout(); col.setSpacing(3)
        col.addWidget(glabel("Whisper small  ·  语音转录（~250MB）", 9,
                              bold=True, color=C_BRIGHT, glow_r=8))
        msg = "已缓存" if self._whisper_ok else "未找到，点右侧按钮下载"
        self._w_status = glabel(msg, 8, color=c, glow_r=5)
        col.addWidget(self._w_status)
        self._w_log = glabel("", 7, color=C_DIM, glow_r=4)
        col.addWidget(self._w_log)
        self._w_bar = RetroBar(indeterminate=True)
        self._w_bar.setVisible(False)
        col.addWidget(self._w_bar)

        if not self._whisper_ok:
            mr = QHBoxLayout(); mr.setSpacing(6)
            mr.addWidget(glabel("镜像:", 7, color=C_DIM, glow_r=4))
            self._w_mirror = QLineEdit()
            self._w_mirror.setText('https://hf-mirror.com')
            self._w_mirror.setFont(_mono(7))
            self._w_mirror.setFixedHeight(18)
            self._w_mirror.setStyleSheet(f"""
                QLineEdit {{
                    color: {_css(C_DIM)};
                    background: rgba(20,5,0,180);
                    border: 1px solid {_css(C_GRID)};
                    padding: 1px 4px;
                }}
                QLineEdit:focus {{ border-color: {_css(C_DIM)}; }}
            """)
            mr.addWidget(self._w_mirror)
            col.addLayout(mr)

        row.addLayout(col)
        row.addStretch()

        if not self._whisper_ok:
            self._dl_btn = _retro_btn("下载模型", C_DIM)
            self._dl_btn.clicked.connect(self._download_whisper)
            row.addWidget(self._dl_btn)

        return row

    # ── Actions ───────────────────────────────────────────
    def _download_ffmpeg(self):
        if self._ffmpeg_running:
            return
        self._ffmpeg_running = True
        self._ffmpeg_btn.setEnabled(False)
        self._ffmpeg_status.setText("下载中...")
        self._ffmpeg_log.setText("正在连接...")

        exe_dir = (Path(sys.executable).parent
                   if getattr(sys, 'frozen', False)
                   else Path(__file__).parent)

        def run():
            try:
                import io, urllib.request, zipfile
                url = 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip'
                QTimer.singleShot(0, lambda: self._ffmpeg_log.setText("下载 ffmpeg (~70MB)..."))
                with urllib.request.urlopen(url, timeout=120) as resp:
                    total = int(resp.headers.get('Content-Length', 0))
                    buf = b''; chunk = 65536; done = 0
                    while True:
                        part = resp.read(chunk)
                        if not part:
                            break
                        buf += part; done += len(part)
                        if total:
                            pct = done / total * 100
                            QTimer.singleShot(0,
                                lambda p=pct: self._ffmpeg_log.setText(f"下载中... {p:.0f}%"))
                QTimer.singleShot(0, lambda: self._ffmpeg_log.setText("解压中..."))
                with zipfile.ZipFile(io.BytesIO(buf)) as zf:
                    for name in zf.namelist():
                        base = Path(name).name
                        if base in ('ffmpeg.exe', 'ffprobe.exe'):
                            with zf.open(name) as src:
                                (exe_dir / base).write_bytes(src.read())
                QTimer.singleShot(0, lambda: self._on_ffmpeg_done(True))
            except Exception as e:
                QTimer.singleShot(0, lambda err=str(e)[:120]: self._on_ffmpeg_done(False, err))

        threading.Thread(target=run, daemon=True).start()

    def _on_ffmpeg_done(self, success: bool, err: str = ''):
        self._ffmpeg_running = False
        if success:
            self._ffmpeg_ok = True
            self._ffmpeg_icon.setText("✓")
            self._ffmpeg_icon.setStyleSheet(f"color:{_css(C_SUCCESS)}; background:transparent;")
            self._ffmpeg_icon.setGraphicsEffect(_glow(C_SUCCESS, 10))
            self._ffmpeg_status.setText("已就绪")
            self._ffmpeg_status.setStyleSheet(f"color:{_css(C_SUCCESS)}; background:transparent;")
            self._ffmpeg_log.setText("ffmpeg.exe / ffprobe.exe 已放置到 EXE 同目录")
        else:
            self._ffmpeg_status.setText("下载失败")
            self._ffmpeg_log.setText(err or "请检查网络连接后重试")
            self._ffmpeg_btn.setEnabled(True)

    def _download_whisper(self):
        if self._whisper_running:
            return
        self._whisper_running = True
        self._dl_btn.setEnabled(False)
        self._w_status.setText("下载中...")
        self._w_bar.setVisible(True)
        self._w_current_file = ''

        mirror = getattr(self, '_w_mirror', None)
        mirror_url = (mirror.text().strip().rstrip('/') if mirror else '') \
                     or 'https://hf-mirror.com'

        self._w_poll_timer = QTimer(self)
        self._w_poll_timer.timeout.connect(self._poll_whisper_size)
        self._w_poll_timer.start(1000)

        def run():
            import os
            import time
            from huggingface_hub import hf_hub_download, list_repo_files

            repo_id = 'Systran/faster-whisper-small'
            attempts = [
                (mirror_url, 1),
                (mirror_url, 2),
                ('https://huggingface.co', 1),
            ]
            last_err = ''
            for endpoint, try_n in attempts:
                try:
                    os.environ['HF_ENDPOINT'] = endpoint
                    host = endpoint.replace('https://', '')
                    self._w_current_file = f"连接 {host}（第 {try_n} 次）..."

                    files = [f for f in list_repo_files(repo_id)
                             if not f.startswith('.')]
                    total = len(files)

                    for i, fname in enumerate(files, 1):
                        self._w_current_file = f"文件 {i}/{total}: {fname}"
                        hf_hub_download(repo_id, fname)

                    QTimer.singleShot(0, lambda: self._on_whisper_done(True))
                    return
                except Exception as e:
                    last_err = str(e)[:80]
                    self._w_current_file = f"失败：{last_err}"
                    if (endpoint, try_n) != attempts[-1]:
                        time.sleep(3)

            QTimer.singleShot(0, lambda: self._on_whisper_done(
                False, f"多次重试均失败：{last_err}"))

        threading.Thread(target=run, daemon=True).start()

    def _poll_whisper_size(self):
        import os
        hf_home = Path(os.environ.get('HF_HOME',
                       str(Path.home() / '.cache' / 'huggingface')))
        blobs_dir = hf_home / 'hub' / 'models--Systran--faster-whisper-small' / 'blobs'
        file_info = getattr(self, '_w_current_file', '')
        if not blobs_dir.exists():
            if file_info:
                self._w_log.setText(file_info)
            return
        try:
            total = sum(f.stat().st_size for f in blobs_dir.iterdir() if f.is_file())
            mb = total / 1_048_576
            if file_info:
                self._w_log.setText(f"{file_info}  ({mb:.0f} MB 已缓存)")
            else:
                self._w_log.setText(f"已缓存 {mb:.0f} MB")
        except Exception:
            if file_info:
                self._w_log.setText(file_info)

    def _on_whisper_done(self, success: bool, err: str = ''):
        if hasattr(self, '_w_poll_timer'):
            self._w_poll_timer.stop()
        self._w_bar.setVisible(False)
        self._w_current_file = ''
        self._whisper_running = False
        if success:
            self._whisper_ok = True
            self._w_icon.setText("✓")
            self._w_icon.setStyleSheet(f"color:{_css(C_SUCCESS)}; background:transparent;")
            self._w_icon.setGraphicsEffect(_glow(C_SUCCESS, 10))
            self._w_status.setText("已缓存")
            self._w_status.setStyleSheet(f"color:{_css(C_SUCCESS)}; background:transparent;")
            self._w_log.setText("下载完成")
        else:
            self._w_status.setText("下载失败")
            self._w_log.setText(err or "请检查网络连接后重试")
            self._dl_btn.setEnabled(True)


# ── Main window ───────────────────────────────────────────
_SAFE_RE = re.compile(r'[\\/:*?"<>|]')

class MainWindow(QMainWindow):
    def __init__(self, op_mode: str = 'video'):
        super().__init__()
        self._op_mode = op_mode
        self.setWindowTitle("Gut's Tool-视频转图文")
        self.setWindowIcon(make_icon())
        self.resize(760, 620); self.setMinimumSize(520, 460)
        p = self.palette(); p.setColor(QPalette.ColorRole.Window, C_BG); self.setPalette(p)

        self.setAcceptDrops(True)
        self._quitting = False
        QApplication.instance().aboutToQuit.connect(self._on_quit)
        self._tick_n   = 0
        self._last_sig = None
        self._task_states: dict[str, str] = {}
        self._active_srcs: set[str] = set()

        self._sounds = SoundPlayer(self)

        self._build_ui()
        self._build_tray()

        self._clear_status_file()
        t = QTimer(self); t.timeout.connect(self._poll); t.start(600)
        self._poll()

    # ── UI construction ───────────────────────────────────
    def _build_ui(self):
        bg = HUDBackground(self)
        self.setCentralWidget(bg)
        root = QVBoxLayout(bg)
        root.setContentsMargins(14, 12, 14, 10); root.setSpacing(6)

        hdr = QHBoxLayout()
        hdr.addWidget(glabel("◈  视频转图文工具", 13, bold=True,
                              color=C_PRIMARY, glow_r=22))
        hdr.addStretch()
        self._session_lbl = glabel("任务: 0000", 8, color=C_DIM, glow_r=5)
        hdr.addWidget(self._session_lbl); hdr.addSpacing(10)
        self._clock_lbl = glabel("00:00:00", 8, color=C_DIM, glow_r=5)
        hdr.addWidget(self._clock_lbl)
        root.addLayout(hdr)
        root.addWidget(_divider())

        self._input_panel = InputPanel()
        self._input_panel.submitted.connect(self._spawn)
        root.addWidget(self._input_panel)

        self._output_panel = OutputPanel(RAW_SOURCES)
        root.addWidget(self._output_panel)

        self._cookies_panel = CookiesPanel()
        root.addWidget(self._cookies_panel)
        root.addWidget(_divider())

        mid = QHBoxLayout(); mid.setSpacing(8)

        qp = Panel("任务队列")
        ql = QVBoxLayout(qp); ql.setContentsMargins(2, 18, 2, 4)
        scroll = QScrollArea(); scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea { border:none; background:transparent; }")
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.verticalScrollBar().setStyleSheet(f"""
            QScrollBar:vertical {{ background:{_css(C_BG)}; width:5px; }}
            QScrollBar::handle:vertical {{ background:{_css(C_DIM)}; min-height:20px; }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height:0px; }}
        """)
        self._q_inner = QWidget(); self._q_inner.setStyleSheet("background:transparent;")
        self._q_lay = QVBoxLayout(self._q_inner)
        self._q_lay.setContentsMargins(0,0,0,0); self._q_lay.setSpacing(0)
        self._q_lay.addStretch()
        scroll.setWidget(self._q_inner)
        ql.addWidget(scroll)
        mid.addWidget(qp, stretch=5)

        op = Panel("操作员")
        ol = QVBoxLayout(op); ol.setContentsMargins(6, 20, 6, 8); ol.setSpacing(6)
        ol.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._char = CRTCharVideoWidget(CHAR_VIDEO)
        ol.addWidget(self._char, alignment=Qt.AlignmentFlag.AlignHCenter)
        ol.addWidget(_divider())
        self._op_status = glabel("状态: 待机", 8, color=C_DIM, glow_r=5)
        ol.addWidget(self._op_status)
        self._op_id = glabel("编号: 加把劲卡比", 8, color=C_DIM, glow_r=5)
        ol.addWidget(self._op_id)
        ol.addStretch()
        op.setFixedWidth(174)
        mid.addWidget(op, stretch=0)
        root.addLayout(mid, stretch=3)

        lp = Panel("日志输出")
        ll = QVBoxLayout(lp); ll.setContentsMargins(2, 18, 2, 4)
        self._log = QTextEdit(); self._log.setReadOnly(True)
        self._log.setFont(_mono(8)); self._log.setMaximumHeight(95)
        self._log.setStyleSheet(f"""
            QTextEdit {{ background:transparent; color:{_css(C_DIM)}; border:none; }}
            QScrollBar:vertical {{ background:{_css(C_BG)}; width:4px; }}
            QScrollBar::handle:vertical {{ background:{_css(C_DIM)}; }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height:0px; }}
        """)
        ll.addWidget(self._log)
        root.addWidget(lp, stretch=1)

        root.addWidget(_divider())
        sb = QHBoxLayout()
        self._stats_lbl = glabel(
            "处理中: 0   排队: 0   完成: 0   错误: 0",
            8, color=C_DIM, glow_r=5)
        sb.addWidget(self._stats_lbl); sb.addStretch()

        clear_btn = _retro_btn("清除完成", C_DIM)
        clear_btn.clicked.connect(self._clear_done)
        sb.addWidget(clear_btn); sb.addSpacing(8)

        self._dot = glabel("●", 9, color=C_DIM, glow_r=8)
        sb.addWidget(self._dot)
        root.addLayout(sb)

    # ── System tray ───────────────────────────────────────
    def _build_tray(self):
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self._tray = QSystemTrayIcon(make_icon(), self)
        self._tray.setToolTip("视频摄入队列")

        menu = QMenu()
        menu.setStyleSheet(f"""
            QMenu {{ background:{_css(QColor(12,3,0))}; color:{_css(C_PRIMARY)};
                     border:1px solid {_css(C_BORDER)}; font-family:Courier New; font-size:9pt; }}
            QMenu::item:selected {{ background:{_css(QColor(40,10,0))}; }}
        """)
        show_act = QAction("◈ 显示窗口", self)
        show_act.triggered.connect(self._tray_show)
        menu.addAction(show_act)
        menu.addSeparator()
        quit_act = QAction("✕ 退出", self)
        quit_act.triggered.connect(QApplication.instance().quit)
        menu.addAction(quit_act)

        self._tray.setContextMenu(menu)
        self._tray.activated.connect(self._on_tray_activated)
        self._tray.show()

    def _tray_show(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self._tray_show()

    # ── Full-window drag-drop ─────────────────────────────
    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls() or e.mimeData().hasText():
            e.acceptProposedAction()

    def dragMoveEvent(self, e):
        e.acceptProposedAction()

    def dropEvent(self, e):
        if e.mimeData().hasUrls():
            for url in e.mimeData().urls():
                src = url.toLocalFile() or url.toString()
                if src:
                    self._spawn(src)
        elif e.mimeData().hasText():
            self._spawn(e.mimeData().text().strip())

    def _on_quit(self):
        self._quitting = True

    def closeEvent(self, e):
        if self._quitting:
            e.accept()
            return
        if hasattr(self, '_tray') and self._tray.isVisible():
            e.ignore()
            self.hide()
            if not getattr(self, '_tray_notified', False):
                self._tray_notified = True
                self._tray.showMessage(
                    "视频摄入队列",
                    "已最小化到系统托盘，双击图标恢复",
                    QSystemTrayIcon.MessageIcon.Information,
                    2500,
                )
        else:
            e.accept()

    # ── Spawn: launch in-process thread ──────────────────
    def _spawn(self, src: str):
        src = src.strip().strip('"').strip("'")
        if not src:
            return

        # B站等平台分享文本格式：「标题 https://...」，提取纯 URL
        if not Path(src).exists():
            m = re.search(r'https?://\S+', src)
            if m:
                src = m.group(0)

        out_dir = self._output_panel.current_path()
        out_dir.mkdir(parents=True, exist_ok=True)

        extra_ydl: list[str] = []
        if re.search(r'bilibili\.com|b23\.tv', src, re.I):
            extra_ydl += self._cookies_panel.cookies_args()

        self._log.append(
            f'<span style="color:{_css(C_PRIMARY)};">'
            f'[{datetime.now().strftime("%H:%M:%S")}] 已投喂: {src[:70]}</span>'
        )
        threading.Thread(
            target=self._worker,
            args=(src, out_dir, extra_ydl),
            daemon=True,
        ).start()

    def _worker(self, src: str, out_dir: Path, extra_ydl: list[str]):
        """在后台线程中调用 video2md.process_video()。"""
        try:
            video2md.process_video(src, out_dir, lang='zh', extra_ydl=extra_ydl)
        except Exception:
            pass  # StatusWriter 已写入 error 状态，monitor 会自动显示

    # ── Startup cleanup ───────────────────────────────────
    def _clear_status_file(self):
        try:
            STATUS_FILE.write_text(
                json.dumps({'tasks': []}, ensure_ascii=False),
                encoding='utf-8',
            )
        except Exception:
            pass

    # ── Task management ───────────────────────────────────
    def _remove_task(self, task_id: str):
        if not STATUS_FILE.exists():
            return
        try:
            data  = json.loads(STATUS_FILE.read_text(encoding='utf-8'))
            tasks = [t for t in data.get('tasks', []) if t.get('id') != task_id]
            STATUS_FILE.write_text(
                json.dumps({'tasks': tasks}, ensure_ascii=False, indent=2),
                encoding='utf-8',
            )
            self._last_sig = None
        except Exception:
            pass

    def _clear_done(self):
        if not STATUS_FILE.exists():
            return
        try:
            data  = json.loads(STATUS_FILE.read_text(encoding='utf-8'))
            tasks = [t for t in data.get('tasks', [])
                     if t.get('status') not in ('complete', 'error')]
            STATUS_FILE.write_text(
                json.dumps({'tasks': tasks}, ensure_ascii=False, indent=2),
                encoding='utf-8',
            )
            self._last_sig = None
        except Exception:
            pass

    def _retry(self, task: dict):
        src = task.get('source', '')
        if not src:
            return
        self._remove_task(task.get('id', ''))
        self._spawn(src)

    def _open_task(self, output_md: str):
        p = Path(output_md)
        if not p.exists():
            self._log.append(
                f'<span style="color:{_css(C_ERROR)};">'
                f'[{datetime.now().strftime("%H:%M:%S")}] 文件不存在: {output_md}</span>'
            )
            return
        if sys.platform == 'win32':
            os.startfile(str(p))
        else:
            subprocess.Popen(['xdg-open', str(p)])

    # ── Completion handler: sound + git commit ────────────
    def _on_task_complete(self, task: dict):
        self._sounds.play('task_complete')
        output_md = task.get('output_md')
        if output_md:
            threading.Thread(
                target=self._git_commit,
                args=(task.get('title', 'video'), output_md),
                daemon=True,
            ).start()

    def _on_task_error(self, task: dict):
        self._sounds.play('task_error')

    def _git_commit(self, title: str, output_md: str):
        """从输出文件所在目录向上查找 .git，找到则 commit；找不到则静默跳过。"""
        try:
            md_path    = Path(output_md)
            assets_dir = md_path.parent / f'{md_path.stem}_assets'
            add_targets = [str(md_path)]
            if assets_dir.exists():
                add_targets.append(str(assets_dir))

            # 向上查找 .git 目录（最多 6 层）
            repo = md_path.parent
            for _ in range(6):
                if (repo / '.git').exists():
                    break
                parent = repo.parent
                if parent == repo:
                    return  # 到达文件系统根，不在 git 仓库内
                repo = parent
            else:
                return

            subprocess.run(
                ['git', '-C', str(repo), 'add'] + add_targets,
                check=True, capture_output=True,
            )
            safe_title = _SAFE_RE.sub('_', title)[:60]
            subprocess.run(
                ['git', '-C', str(repo), 'commit',
                 '-m', f'[视频摄入] {safe_title}'],
                check=True, capture_output=True,
            )
        except Exception:
            pass

    # ── Poll ─────────────────────────────────────────────
    def _poll(self):
        self._tick_n += 1
        self._clock_lbl.setText(datetime.now().strftime("%H:%M:%S"))
        dc = C_PRIMARY if self._tick_n % 2 == 0 else C_DIM
        self._dot.setStyleSheet(f"color:{_css(dc)}; background:transparent;")
        self._dot.setGraphicsEffect(_glow(dc, 10))

        tasks = []
        if STATUS_FILE.exists():
            try:
                tasks = json.loads(STATUS_FILE.read_text(encoding='utf-8')).get("tasks", [])
            except Exception:
                pass

        for t in tasks:
            tid  = t.get('id')
            prev = self._task_states.get(tid)
            curr = t.get('status')
            if prev and prev != curr:
                if curr == 'complete':
                    self._on_task_complete(t)
                elif curr == 'error':
                    self._on_task_error(t)
            self._task_states[tid] = curr

        sig = str([(t.get("id"), t.get("status"), t.get("step"),
                    round(t.get("progress", 0), 2)) for t in tasks])
        if sig == self._last_sig:
            return
        self._last_sig = sig

        self._session_lbl.setText(f"任务: {len(tasks):04d}")
        self._rebuild_queue(tasks)
        self._update_log(tasks)
        self._update_stats(tasks)

        active = any(t.get("status") == "processing" for t in tasks)
        sc = C_PRIMARY if active else C_DIM
        self._op_status.setText("状态: 运行中" if active else "状态: 待机")
        self._op_status.setStyleSheet(f"color:{_css(sc)}; background:transparent;")
        self._op_status.setGraphicsEffect(_glow(sc, 7))

        if hasattr(self, '_tray'):
            n_proc = sum(1 for t in tasks if t.get('status') == 'processing')
            tip = f"视频摄入队列 — {n_proc} 处理中" if n_proc else "视频摄入队列 — 待机"
            self._tray.setToolTip(tip)

    def _rebuild_queue(self, tasks):
        while self._q_lay.count() > 1:
            item = self._q_lay.takeAt(0)
            if item.widget(): item.widget().deleteLater()
        if not tasks:
            lbl = glabel("队列为空", 9, color=C_DIM, glow_r=5)
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._q_lay.insertWidget(0, lbl)
        else:
            for i, t in enumerate(reversed(tasks)):
                self._q_lay.insertWidget(
                    i, TaskRow(t,
                               on_remove=self._remove_task,
                               on_retry=self._retry,
                               on_open=self._open_task))

    def _update_log(self, tasks):
        target = next((t for t in tasks if t.get("status") == "processing"), None)
        if not target and tasks: target = tasks[-1]
        if not target: return
        lines = target.get("log", [])[-30:]
        html = []
        for ln in lines:
            esc = ln.replace('&','&amp;').replace('<','&lt;').replace('>','&gt;')
            c = _css(C_PRIMARY) if ln.startswith('[') else _css(C_DIM)
            html.append(f'<span style="color:{c};">{esc}</span>')
        self._log.setHtml('<br>'.join(html))
        sb = self._log.verticalScrollBar(); sb.setValue(sb.maximum())

    def _update_stats(self, tasks):
        counts = {k: 0 for k in ("processing","queued","complete","error")}
        for t in tasks:
            s = t.get("status","queued")
            if s in counts: counts[s] += 1
        txt = (f"处理中: {counts['processing']}   排队: {counts['queued']}   "
               f"完成: {counts['complete']}   错误: {counts['error']}")
        c = C_PRIMARY if counts["processing"] > 0 else C_DIM
        self._stats_lbl.setText(txt)
        self._stats_lbl.setStyleSheet(f"color:{_css(c)}; background:transparent;")
        self._stats_lbl.setGraphicsEffect(_glow(c, 7))


def main():
    import argparse
    ap = argparse.ArgumentParser(description='Video Ingest Queue Monitor')
    ap.add_argument('--operator', choices=['video'], default='video',
                    help='Operator widget style: video')
    args, _ = ap.parse_known_args()

    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setWindowIcon(make_icon())
    app.setQuitOnLastWindowClosed(False)

    pal = QPalette()
    pal.setColor(QPalette.ColorRole.Window,     C_BG)
    pal.setColor(QPalette.ColorRole.WindowText, C_PRIMARY)
    pal.setColor(QPalette.ColorRole.Base,       QColor(4, 1, 0))
    pal.setColor(QPalette.ColorRole.Text,       C_PRIMARY)
    pal.setColor(QPalette.ColorRole.Button,     QColor(18, 4, 0))
    pal.setColor(QPalette.ColorRole.ButtonText, C_PRIMARY)
    app.setPalette(pal)

    win = MainWindow(op_mode=args.operator)

    ffmpeg_ok  = check_ffmpeg()
    whisper_ok = check_whisper_model()

    if not ffmpeg_ok or not whisper_ok:
        setup = SetupWindow(ffmpeg_ok=ffmpeg_ok, whisper_ok=whisper_ok)
        setup.proceed_signal.connect(lambda: (win.show(), setup.hide()))
        setup.show()
    else:
        win.show()

    sys.exit(app.exec())

if __name__ == '__main__':
    main()
