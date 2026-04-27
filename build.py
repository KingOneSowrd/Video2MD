"""
build.py -- PyInstaller build script for VideoIngestMonitor (standalone EXE)

Usage:
    python build.py

Detects ffmpeg/ffprobe in PATH and bundles them into the EXE automatically.
Output: dist/VideoIngestMonitor.exe
"""

import shutil
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
ASSETS_DIR = SCRIPT_DIR / 'assets'
OUT_DIR    = SCRIPT_DIR / 'dist'
BUILD_DIR  = SCRIPT_DIR / 'build'


def find_binary(name: str) -> str | None:
    path = shutil.which(name)
    if path:
        print(f"  [found] {name}: {path}")
    else:
        print(f"  [warn]  {name} not found in PATH")
    return path


def main():
    print("=" * 52)
    print("  Video Ingest Monitor -- PyInstaller Build")
    print("=" * 52)

    # Clean previous build
    print("\n[1] Cleaning previous build...")
    for d in (BUILD_DIR, OUT_DIR):
        if d.exists():
            shutil.rmtree(d)
            print(f"  removed: {d}")

    # Detect ffmpeg/ffprobe
    print("\n[2] Detecting ffmpeg/ffprobe...")
    ffmpeg  = find_binary('ffmpeg')
    ffprobe = find_binary('ffprobe')

    # Build PyInstaller command
    print("\n[3] Building EXE...")
    cmd = [
        sys.executable, '-m', 'PyInstaller',
        '--onefile',
        '--windowed',
        '--name',         'VideoIngestMonitor',
        '--icon',         str(ASSETS_DIR / 'Karby.png'),
        '--add-data',     str(ASSETS_DIR / 'Karby.png')      + ';assets',
        '--add-data',     str(ASSETS_DIR / 'Karby_Video.mp4') + ';assets',
        '--add-data',     str(ASSETS_DIR / 'Karby.gif')       + ';assets',
        '--collect-all',  'faster_whisper',
        '--collect-all',  'yt_dlp',
        '--collect-all',  'ctranslate2',
        '--hidden-import','PyQt6.QtMultimedia',
        '--hidden-import','video2md',
        '--distpath',     str(OUT_DIR),
        '--workpath',     str(BUILD_DIR),
        '--specpath',     str(SCRIPT_DIR),
        str(SCRIPT_DIR / 'monitor.py'),
    ]

    if ffmpeg:
        cmd += ['--add-binary', f'{ffmpeg};.']
    if ffprobe:
        cmd += ['--add-binary', f'{ffprobe};.']

    result = subprocess.run(cmd, cwd=str(SCRIPT_DIR))

    if result.returncode != 0:
        print("\n[ERROR] Build failed. Check output above.")
        sys.exit(1)

    exe = OUT_DIR / 'VideoIngestMonitor.exe'
    print("\n" + "=" * 52)
    print(f"  Done!  {exe}")
    if not ffmpeg:
        print("  NOTE: ffmpeg was NOT bundled.")
        print("  Target users must install ffmpeg and add it to PATH.")
        print("  Download: https://www.gyan.dev/ffmpeg/builds/")
    else:
        print("  ffmpeg bundled -- users need nothing extra.")
    print("=" * 52)


if __name__ == '__main__':
    main()
