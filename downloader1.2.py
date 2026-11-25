#!/usr/bin/env python3
"""
4K Downloader - Phase 3
A minimalist dark-mode desktop video downloader with a download queue.

New in Phase 3:
  * Clipboard monitor (any http/https link), browser-cookie support
  * Embed subtitles / cover art, SponsorBlock, yt-dlp engine updater
  * Persistent config.json and packaging-ready FFmpeg / yt-dlp handling

Phase 2 features:
  * Multi-video queue with per-item thumbnail, title, resolution, status badge and progress bar
  * Pause / Resume / Cancel / Retry / Remove / Clear Completed / Open Folder
  * Playlist + batch link detection with a "pick which videos" dialog (Select All)
  * 1, 2 or 3 simultaneous downloads

Requirements:
    pip install customtkinter pillow "yt-dlp[default]"
    FFmpeg on your PATH or next to the app (4K merging / MP3 extraction).
    A JavaScript runtime such as Deno is recommended for YouTube (see yt-dlp's EJS wiki).

Run:
    python downloader.py
"""

from __future__ import annotations

import importlib.machinery
import io
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.request
import zipfile
from collections import Counter, deque
from dataclasses import dataclass, field
from pathlib import Path
from tkinter import filedialog, messagebox
from urllib.parse import parse_qs, urlparse


# --------------------------------------------------------------------------- #
# Paths, frozen-app helpers and optional yt-dlp engine override
# --------------------------------------------------------------------------- #
def app_base_dir() -> Path:
    """Folder of the .exe when frozen by PyInstaller, otherwise of this script."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def bundle_dir() -> Path:
    """Where PyInstaller unpacks bundled files (onefile: temp dir, onedir: _internal)."""
    return Path(getattr(sys, "_MEIPASS", app_base_dir()))


def user_data_dir() -> Path:
    if sys.platform.startswith("win"):
        base = Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    return base / "4KDownloader"


def version_tuple(version: str | None) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", version or ""))


# Windowed (no-console) PyInstaller builds have no stdout/stderr; give libraries a sink.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

# Make bundled / side-by-side tools (ffmpeg, deno, ...) discoverable. Appended, so system tools win.
os.environ["PATH"] = os.pathsep.join(
    [p for p in (os.environ.get("PATH", ""), str(app_base_dir()), str(bundle_dir())) if p]
)

ENGINE_ZIP = user_data_dir() / "engine" / "yt-dlp.zip"


def zip_engine_version(path: Path) -> str | None:
    try:
        with zipfile.ZipFile(path) as zf:
            source = zf.read("yt_dlp/version.py").decode("utf-8", "replace")
        match = re.search(r"__version__\s*=\s*['\"]([^'\"]+)['\"]", source)
        return match.group(1) if match else None
    except Exception:
        return None


def _purge_modules(prefix: str) -> None:
    for name in [m for m in sys.modules if m == prefix or m.startswith(prefix + ".")]:
        del sys.modules[name]


def _use_engine_override() -> None:
    """Frozen builds only: prefer a newer yt-dlp downloaded by the in-app updater over the copy
    baked into the executable. Any problem silently falls back to the bundled copy."""
    if not (getattr(sys, "frozen", False) and ENGINE_ZIP.is_file()):
        return
    finder_added = False
    try:
        import yt_dlp.version as bundled  # the copy inside the executable
        override = zip_engine_version(ENGINE_ZIP)
        if not override or version_tuple(override) <= version_tuple(bundled.__version__):
            return
        _purge_modules("yt_dlp")
        sys.path.insert(0, str(ENGINE_ZIP))
        sys.meta_path.insert(0, importlib.machinery.PathFinder)
        finder_added = True
        import yt_dlp  # noqa: F401  (resolved from the zip now)
        if not str(getattr(yt_dlp, "__file__", "")).startswith(str(ENGINE_ZIP)):
            raise ImportError("engine override did not take effect")
    except Exception:
        _purge_modules("yt_dlp")
        if str(ENGINE_ZIP) in sys.path:
            sys.path.remove(str(ENGINE_ZIP))
        if finder_added:
            sys.meta_path.remove(importlib.machinery.PathFinder)


# --------------------------------------------------------------------------- #
# Dependency checks (Python packages)
# --------------------------------------------------------------------------- #
def _fatal(message: str) -> None:
    root = tk.Tk()
    root.withdraw()
    messagebox.showerror("Missing dependency", message)
    root.destroy()
    sys.exit(1)


try:
    import customtkinter as ctk
except ImportError:
    _fatal("CustomTkinter is not installed.\n\nRun:  pip install customtkinter")

_use_engine_override()
try:
    import yt_dlp
except ImportError:
    _fatal("yt-dlp is not installed.\n\nRun:  pip install \"yt-dlp[default]\"")
try:
    from yt_dlp.version import __version__ as YTDLP_VERSION
except Exception:
    YTDLP_VERSION = "unknown"

try:
    from PIL import Image, ImageDraw, ImageOps
except ImportError:
    _fatal("Pillow is not installed.\n\nRun:  pip install pillow")


# --------------------------------------------------------------------------- #
# Constants / theme
# --------------------------------------------------------------------------- #
APP_NAME = "4K Downloader"
LEGACY_CONFIG_PATH = Path.home() / ".ytdl_4k_config.json"  # Phase 1/2 location (migrated)

BG = "#0e0e11"
CARD = "#17171c"
ROW = "#1e1e25"
FIELD = "#252530"
FIELD_HOVER = "#30303c"
ACCENT = "#7c5cff"
ACCENT_HOVER = "#6847f0"
TEXT = "#f2f2f5"
MUTED = "#8b8b99"
DANGER = "#ef4444"
DANGER_HOVER = "#dc2626"
OK = "#22c55e"
WARN = "#f59e0b"

# Queue item states
S_QUEUED = "Queued"
S_DL = "Downloading"
S_MERGE = "Merging"
S_DONE = "Done"
S_ERROR = "Error"
S_PAUSED = "Paused"
S_CANCELLED = "Cancelled"
ACTIVE_STATES = (S_DL, S_MERGE)

BADGE_COLORS = {
    S_QUEUED: "#3f3f4b",
    S_DL: ACCENT,
    S_MERGE: "#d97706",
    S_DONE: "#16a34a",
    S_ERROR: "#dc2626",
    S_PAUSED: "#475569",
    S_CANCELLED: "#52525b",
}
BAR_COLORS = {S_DONE: OK, S_ERROR: DANGER, S_PAUSED: "#64748b", S_CANCELLED: "#52525b"}

# label -> max height (None = no cap) or audio codec string
QUALITY_OPTIONS: dict[str, int | str | None] = {
    "Best Available (up to 8K)": None,
    "2160p (4K)": 2160,
    "1440p (2K)": 1440,
    "1080p": 1080,
    "720p": 720,
    "Audio Only (MP3)": "mp3",
    "Audio Only (M4A)": "m4a",
}
CONTAINERS = ["MP4", "MKV"]
COOKIE_BROWSERS = ["None", "Chrome", "Edge", "Firefox", "Brave", "Opera"]
SUBTITLE_LANGS = ["en.*", "-live_chat"]  # English (manual or auto); edit for other languages
SPONSORBLOCK_CATEGORIES = ["sponsor", "intro", "outro", "selfpromo", "interaction"]

DEFAULT_CONFIG: dict = {
    "last_dir": "",
    "quality": "Best Available (up to 8K)",
    "container": "MP4",
    "concurrency": 2,
    "cookies_browser": "None",
    "embed_subs": False,
    "embed_thumb": False,
    "sponsorblock": False,
    "monitor_clipboard": False,
}

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
URL_RE = re.compile(
    r"(https?://[^\s,;]+|(?:www\.|m\.)?(?:youtube\.com|youtu\.be)/[^\s,;]+)", re.I
)
THUMB_SIZE = (128, 72)


class DownloadStopped(Exception):
    """Raised inside yt-dlp hooks to stop a running download (pause or cancel)."""


class _QuietLogger:
    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #
@dataclass
class QueueItem:
    id: int
    url: str
    title: str
    channel: str = ""
    duration: int | None = None
    video_id: str = ""
    max_height: int = 0
    quality_label: str = "Best Available (up to 8K)"
    container: str = "MP4"          # empty for audio-only items
    folder: str = ""
    thumb_url: str = ""
    thumb_ctk: object = None        # CTkImage, built on the main thread
    cookies_browser: str = ""       # "" = none, else chrome / edge / firefox / brave / opera
    embed_subs: bool = False
    embed_thumb: bool = False
    sponsorblock: bool = False

    status: str = S_QUEUED
    progress: float = 0.0
    speed: float | None = None
    eta: int | None = None
    stage: str = ""
    message: str = ""
    filepath: str | None = None

    run_id: int = 0
    stop_event: threading.Event = field(default_factory=threading.Event)
    stop_reason: str | None = None  # "pause" | "cancel"
    stopping: bool = False


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _dir_writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write_test"
        probe.write_text("ok")
        probe.unlink()
        return True
    except OSError:
        return False


def resolve_config_path() -> Path:
    """config.json next to the app (portable) when that folder is writable,
    otherwise in the per-user data folder (e.g. when installed under Program Files)."""
    portable_ok = not (getattr(sys, "frozen", False) and sys.platform == "darwin")
    if portable_ok and _dir_writable(app_base_dir()):
        return app_base_dir() / "config.json"
    folder = user_data_dir()
    folder.mkdir(parents=True, exist_ok=True)
    return folder / "config.json"


CONFIG_PATH = resolve_config_path()


def load_config() -> dict:
    """Load config.json (migrating the old Phase 1/2 file once) and validate every key."""
    raw: dict = {}
    for path in (CONFIG_PATH, LEGACY_CONFIG_PATH):
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                raw = loaded
                break
        except Exception:
            continue

    cfg = dict(DEFAULT_CONFIG)
    cfg.update({k: v for k, v in raw.items() if k in DEFAULT_CONFIG})
    if cfg["quality"] not in QUALITY_OPTIONS:
        cfg["quality"] = DEFAULT_CONFIG["quality"]
    if cfg["container"] not in CONTAINERS:
        cfg["container"] = DEFAULT_CONFIG["container"]
    if cfg["cookies_browser"] not in COOKIE_BROWSERS:
        cfg["cookies_browser"] = "None"
    try:
        cfg["concurrency"] = min(max(int(cfg["concurrency"]), 1), 3)
    except (TypeError, ValueError):
        cfg["concurrency"] = DEFAULT_CONFIG["concurrency"]
    if not isinstance(cfg["last_dir"], str):
        cfg["last_dir"] = ""
    for key in ("embed_subs", "embed_thumb", "sponsorblock", "monitor_clipboard"):
        cfg[key] = bool(cfg[key])
    return cfg


def save_config(data: dict) -> None:
    try:
        tmp = CONFIG_PATH.with_name(CONFIG_PATH.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(CONFIG_PATH)  # atomic: never leaves a half-written file
    except Exception:
        pass  # non-fatal


def find_ffmpeg_dir() -> str | None:
    """Folder containing a bundled / side-by-side ffmpeg (and ideally ffprobe), else None (use PATH)."""
    exe = "ffmpeg.exe" if sys.platform.startswith("win") else "ffmpeg"
    for folder in (bundle_dir(), app_base_dir(), bundle_dir() / "ffmpeg", app_base_dir() / "ffmpeg"):
        if (folder / exe).is_file():
            return str(folder)
    return None


def ffmpeg_available() -> bool:
    return find_ffmpeg_dir() is not None or shutil.which("ffmpeg") is not None


def ffmpeg_install_hint() -> str:
    if sys.platform.startswith("win"):
        return "Windows:  winget install Gyan.FFmpeg\n(or download from https://ffmpeg.org and add the 'bin' folder to PATH)"
    if sys.platform == "darwin":
        return "macOS:  brew install ffmpeg"
    return "Linux:  sudo apt install ffmpeg   (or your distro's equivalent)"


def format_duration(seconds) -> str:
    if not seconds:
        return ""
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def format_eta(seconds) -> str:
    if seconds is None:
        return "--:--"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def is_audio_quality(label: str) -> bool:
    return isinstance(QUALITY_OPTIONS.get(label), str)


def round_corners(img: Image.Image, radius: int) -> Image.Image:
    img = img.convert("RGBA")
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, *img.size), radius=radius, fill=255)
    img.putalpha(mask)
    return img


def make_thumb(img: Image.Image) -> ctk.CTkImage:
    fitted = round_corners(ImageOps.fit(img, (THUMB_SIZE[0] * 2, THUMB_SIZE[1] * 2)), 20)
    return ctk.CTkImage(light_image=fitted, dark_image=fitted, size=THUMB_SIZE)


def fetch_image(url: str, timeout: int = 8) -> Image.Image | None:
    if not url:
        return None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return Image.open(io.BytesIO(resp.read())).convert("RGB")
    except Exception:
        return None


def clean_error(exc: Exception) -> str:
    msg = ANSI_RE.sub("", str(exc)).strip().replace("ERROR: ", "")
    low = msg.lower()
    if "ffmpeg" in low and ("not found" in low or "not installed" in low):
        return "FFmpeg was not found. Install it and make sure it is on your PATH."
    if "private video" in low:
        return "This video is private."
    if "sign in to confirm" in low or "confirm your age" in low:
        return ("This video needs a signed-in session (age or bot check). Open Settings and pick "
                "your browser under 'Cookies from Browser', then retry.")
    if "video unavailable" in low or "this video is not available" in low:
        return "This video is unavailable (removed, region-locked, or private)."
    if "unable to download" in low or "urlopen error" in low or "getaddrinfo" in low:
        return "Network error. Check your internet connection and try again."
    if ("cookie" in low and any(k in low for k in ("could not copy", "database", "decrypt", "permission", "locked"))) \
            or "dpapi" in low:
        return ("Couldn't read cookies from the selected browser. Close that browser completely and retry, "
                "or pick a different one (Firefox is usually the most reliable).")
    if "unsupported url" in low:
        return "That doesn't look like a supported video URL."
    return truncate(msg, 300) or "An unknown error occurred."


def extract_urls(text: str) -> list[str]:
    """Pull every link out of free-form text (one per line, comma separated, etc.)."""
    seen: set[str] = set()
    urls: list[str] = []
    for match in URL_RE.findall(text):
        url = match.rstrip(").,]")
        if not re.match(r"https?://", url, re.I):
            url = "https://" + url
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def wants_playlist(url: str) -> bool:
    """True for /playlist pages and watch links carrying a real playlist id.
    Auto-generated 'Mix' lists (RD...) are treated as single videos."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.path.startswith("/playlist"):
        return True
    list_id = parse_qs(parsed.query).get("list", [""])[0]
    return bool(list_id) and not list_id.startswith(("RD", "UL", "TL"))


def entry_to_dict(entry: dict | None) -> dict | None:
    """Normalise a flat playlist entry; returns None for unavailable videos."""
    if not entry:
        return None
    vid = entry.get("id") or ""
    title = entry.get("title") or ""
    raw_url = entry.get("webpage_url") or entry.get("url") or ""
    if title in ("[Private video]", "[Deleted video]") or not (raw_url or vid):
        return None
    url = raw_url if raw_url.startswith("http") else f"https://www.youtube.com/watch?v={vid or raw_url}"
    if vid and "youtu" in url:
        thumb = f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg"
    else:
        thumbs = entry.get("thumbnails") or []
        thumb = (thumbs[-1].get("url") if thumbs else None) or entry.get("thumbnail") or ""
    return {
        "id": vid,
        "url": url,
        "title": title or url,
        "channel": entry.get("channel") or entry.get("uploader") or "",
        "duration": entry.get("duration"),
        "thumb_url": thumb,
        "max_height": 0,
    }


def cleanup_partials(folder: str, video_id: str) -> None:
    """Delete leftover partial / intermediate files of a cancelled download."""
    if not video_id:
        return
    tag = f"[{video_id}]"
    stream_re = re.compile(re.escape(tag) + r"\.f[\w-]+\.")
    try:
        for path in Path(folder).iterdir():
            name = path.name
            if tag not in name:
                continue
            if name.endswith((".part", ".ytdl", ".temp")) or ".part-Frag" in name or stream_re.search(name):
                try:
                    path.unlink()
                except OSError:
                    pass
    except OSError:
        pass


def reveal_in_file_manager(path: str | None, save_folder: str) -> None:
    """Show a finished file in the OS file manager, selected where supported.
    If the exact file can't be found, open the folder it was saved to instead."""
    try:
        norm_path = os.path.normpath(path) if path else ""
        if norm_path and os.path.isfile(norm_path):
            if sys.platform.startswith("win"):
                # Backslash path + separate argv items. A single "/select,<path>" argument gets quoted
                # as a whole when the path has spaces, and Explorer then falls back to Documents.
                subprocess.Popen(["explorer", "/select,", norm_path])
                return
            if sys.platform == "darwin":
                subprocess.Popen(["open", "-R", norm_path])
                return
            subprocess.Popen(["xdg-open", os.path.dirname(norm_path)])
            return

        folder = os.path.normpath(save_folder)
        if sys.platform.startswith("win"):
            os.startfile(folder)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", folder])
        else:
            subprocess.Popen(["xdg-open", folder])
    except Exception as exc:
        messagebox.showerror("Error", f"Could not open folder:\n{exc}")


def resolve_saved_file(hook_path: str | None, info: dict | None) -> str | None:
    """Best existing, normalised path of the finished file: the path seen by yt-dlp's
    postprocessor hooks first, then the paths in yt-dlp's final result."""
    candidates = [hook_path]
    for entry in (info or {}).get("requested_downloads") or []:
        candidates.append(entry.get("filepath"))
    candidates.append((info or {}).get("filepath"))
    for candidate in candidates:
        if candidate:
            norm = os.path.normpath(candidate)
            if os.path.isfile(norm):
                return norm
    return None


STRICT_URL_RE = re.compile(r"^https?://\S+$", re.I)


def clipboard_urls(text: str, limit: int = 25) -> list[str]:
    """http(s) URLs found in clipboard text, or [] unless the text is *only* URLs (one per line)."""
    if not text or len(text) > 8000:
        return []
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    if not lines or len(lines) > limit:
        return []
    urls: list[str] = []
    for line in lines:
        if not STRICT_URL_RE.match(line):
            return []
        try:
            host = urlparse(line).hostname or ""
        except ValueError:
            return []
        if "." not in host:
            return []
        if line not in urls:
            urls.append(line)
    return urls


def latest_release_tag(timeout: int = 15) -> str:
    req = urllib.request.Request(
        "https://api.github.com/repos/yt-dlp/yt-dlp/releases/latest",
        headers={"User-Agent": "4KDownloader", "Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return str(json.loads(resp.read().decode("utf-8"))["tag_name"])


def download_engine(tag: str) -> None:
    """Packaged builds: fetch the official yt-dlp zipapp into the user data folder.
    It is picked up on the next launch (see _use_engine_override)."""
    url = f"https://github.com/yt-dlp/yt-dlp/releases/download/{tag}/yt-dlp"
    ENGINE_ZIP.parent.mkdir(parents=True, exist_ok=True)
    tmp = ENGINE_ZIP.with_suffix(".download")
    req = urllib.request.Request(url, headers={"User-Agent": "4KDownloader"})
    with urllib.request.urlopen(req, timeout=60) as resp, open(tmp, "wb") as fh:
        shutil.copyfileobj(resp, fh)
    version = zip_engine_version(tmp)
    if not version or version_tuple(version) < version_tuple(tag):
        tmp.unlink(missing_ok=True)
        raise RuntimeError("The downloaded engine failed verification.")
    tmp.replace(ENGINE_ZIP)


def _base_ydl_opts(item: QueueItem, progress_hook, pp_hook) -> dict:
    choice = QUALITY_OPTIONS[item.quality_label]
    opts: dict = {
        "outtmpl": str(Path(item.folder) / "%(title).180B [%(id)s].%(ext)s"),
        "noplaylist": True,          # each queue item is exactly one video
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "logger": _QuietLogger(),
        "progress_hooks": [progress_hook],
        "postprocessor_hooks": [pp_hook],
        "concurrent_fragment_downloads": 3,
        "retries": 5,
        "fragment_retries": 5,
        "continuedl": True,          # resume partial files after Pause
    }

    if isinstance(choice, str):  # audio only
        if choice == "mp3":
            opts["format"] = "bestaudio/best"
            opts["postprocessors"] = [
                {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "320"}
            ]
        else:
            opts["format"] = "bestaudio[ext=m4a]/bestaudio/best"
            opts["postprocessors"] = [
                {"key": "FFmpegExtractAudio", "preferredcodec": "m4a", "preferredquality": "0"}
            ]
        return opts

    if choice is None:
        opts["format"] = "bv*+ba/b"
    else:
        opts["format"] = f"bv*[height<={choice}]+ba/b[height<={choice}]"

    container = item.container.lower() or "mp4"
    opts["merge_output_format"] = container
    opts["format_sort"] = ["res", "ext:mp4:m4a"] if container == "mp4" else ["res"]
    return opts


def build_ydl_opts(item: QueueItem, progress_hook, pp_hook) -> dict:
    """Base options plus Phase 3 extras: FFmpeg location, browser cookies, subtitles,
    cover art and SponsorBlock (all via yt-dlp's own post-processors)."""
    opts = _base_ydl_opts(item, progress_hook, pp_hook)
    audio = is_audio_quality(item.quality_label)

    ffmpeg_dir = find_ffmpeg_dir()
    if ffmpeg_dir:
        opts["ffmpeg_location"] = ffmpeg_dir
    if item.cookies_browser:
        opts["cookiesfrombrowser"] = (item.cookies_browser.lower(),)

    early: list[dict] = []  # run before / at the start of the download
    late: list[dict] = []   # run after the download, in this order
    if item.sponsorblock:
        early.append({"key": "SponsorBlock", "categories": list(SPONSORBLOCK_CATEGORIES), "when": "after_filter"})
    if item.embed_thumb:
        opts["writethumbnail"] = True
        early.append({"key": "FFmpegThumbnailsConvertor", "format": "jpg", "when": "before_dl"})
    if item.embed_subs and not audio:
        opts["writesubtitles"] = True
        opts["writeautomaticsub"] = True
        opts["subtitleslangs"] = list(SUBTITLE_LANGS)
        late.append({"key": "FFmpegEmbedSubtitle", "already_have_subtitle": False})
    if item.sponsorblock:
        late.append({"key": "ModifyChapters", "remove_sponsor_segments": list(SPONSORBLOCK_CATEGORIES),
                     "force_keyframes": False})
    if item.embed_thumb:
        late.append({"key": "FFmpegMetadata", "add_chapters": True, "add_metadata": True,
                     "add_infojson": "if_exists"})
        late.append({"key": "EmbedThumbnail", "already_have_thumbnail": False})

    # _base_ydl_opts already put FFmpegExtractAudio (audio mode) in the list; it must run first.
    opts["postprocessors"] = early + list(opts.get("postprocessors", [])) + late
    return opts


# --------------------------------------------------------------------------- #
# Queue row widget
# --------------------------------------------------------------------------- #
class QueueRow(ctk.CTkFrame):
    """One card in the queue: thumbnail, title, meta, progress bar, badge, buttons."""

    # name -> (label, fg, hover, text colour, width)
    BUTTONS = {
        "pause": ("Pause", FIELD_HOVER, "#3d3d4a", TEXT, 72),
        "cancel": ("Cancel", DANGER, DANGER_HOVER, "white", 72),
        "resume": ("Resume", ACCENT, ACCENT_HOVER, "white", 76),
        "retry": ("Retry", ACCENT, ACCENT_HOVER, "white", 72),
        "open": ("Open Folder", FIELD_HOVER, "#3d3d4a", TEXT, 104),
        "remove": ("Remove", FIELD, FIELD_HOVER, MUTED, 72),
        "delete": ("Delete File", DANGER, DANGER_HOVER, "white", 92),
    }

    def __init__(self, master, item: QueueItem, on_action) -> None:
        super().__init__(master, fg_color=ROW, corner_radius=14)
        self.item = item
        self.on_action = on_action
        self._indeterminate = False

        self.grid_columnconfigure(1, weight=1)

        self.thumb = ctk.CTkLabel(
            self, text="", width=THUMB_SIZE[0], height=THUMB_SIZE[1],
            fg_color=FIELD, corner_radius=10,
        )
        self.thumb.grid(row=0, column=0, rowspan=4, padx=12, pady=12)

        self.title_lbl = ctk.CTkLabel(
            self, text="", anchor="w", font=ctk.CTkFont(size=14, weight="bold"), text_color=TEXT
        )
        self.title_lbl.grid(row=0, column=1, sticky="ew", pady=(12, 0))

        self.meta_lbl = ctk.CTkLabel(
            self, text="", anchor="w", font=ctk.CTkFont(size=12), text_color=MUTED
        )
        self.meta_lbl.grid(row=1, column=1, sticky="ew", pady=(2, 0))

        self.bar = ctk.CTkProgressBar(
            self, height=8, corner_radius=4, fg_color=FIELD, progress_color=ACCENT
        )
        self.bar.set(0)
        self.bar.grid(row=2, column=1, sticky="ew", pady=(10, 0), padx=(0, 12))

        self.stats_lbl = ctk.CTkLabel(
            self, text="", anchor="w", justify="left", wraplength=440,
            font=ctk.CTkFont(size=12), text_color=MUTED,
        )
        self.stats_lbl.grid(row=3, column=1, sticky="ew", pady=(4, 12))

        right = ctk.CTkFrame(self, fg_color="transparent")
        right.grid(row=0, column=2, rowspan=4, sticky="nse", padx=(0, 12), pady=12)
        right.grid_rowconfigure(0, weight=1)

        self.badge = ctk.CTkLabel(
            right, text="", corner_radius=10, text_color="white",
            font=ctk.CTkFont(size=11, weight="bold"), height=24,
        )
        self.badge.grid(row=0, column=0, sticky="ne")

        self.btn_frame = ctk.CTkFrame(right, fg_color="transparent")
        self.btn_frame.grid(row=1, column=0, sticky="se")

        self.buttons: dict[str, ctk.CTkButton] = {}
        for name, (label, fg, hover, color, width) in self.BUTTONS.items():
            self.buttons[name] = ctk.CTkButton(
                self.btn_frame, text=label, width=width, height=28, corner_radius=8,
                fg_color=fg, hover_color=hover, text_color=color,
                font=ctk.CTkFont(size=12, weight="bold"),
                command=lambda n=name: self.on_action(self.item.id, n),
            )

        if item.thumb_ctk is not None:
            self.set_thumb(item.thumb_ctk)
        self.refresh()

    # -- helpers --------------------------------------------------------------
    def set_thumb(self, image) -> None:
        self.thumb.configure(image=image, text="")

    def _meta_text(self) -> str:
        it = self.item
        parts = []
        if it.channel:
            parts.append(it.channel)
        dur = format_duration(it.duration)
        if dur:
            parts.append(dur)
        quality = it.quality_label
        if not is_audio_quality(quality) and it.container:
            quality = f"{quality} · {it.container}"
        cap = QUALITY_OPTIONS.get(it.quality_label)
        if isinstance(cap, int) and it.max_height and cap > it.max_height:
            quality += f" (source max {it.max_height}p)"
        extras = []
        if it.embed_subs:
            extras.append("CC")
        if it.embed_thumb:
            extras.append("Cover")
        if it.sponsorblock:
            extras.append("SponsorBlock")
        if extras:
            quality += "  ·  +" + " +".join(extras)
        parts.append(quality)
        return "  ·  ".join(parts)

    def _stats_text(self) -> str:
        it = self.item
        if it.status == S_DL:
            if it.stopping:
                return "Stopping…"
            if not it.stage or it.stage == "Starting…":
                return "Starting…"
            speed = f"{it.speed / (1024 * 1024):.2f} MB/s" if it.speed else "— MB/s"
            return f"{it.progress * 100:.1f}%  ·  {speed}  ·  ETA {format_eta(it.eta)}  ·  {it.stage}"
        if it.status == S_MERGE:
            return it.stage or "Merging with FFmpeg…"
        if it.status == S_QUEUED:
            return "Waiting for a free download slot…"
        if it.status == S_PAUSED:
            return f"Paused at {it.progress * 100:.1f}%  ·  Resume continues from the partial file"
        if it.status == S_CANCELLED:
            return "Cancelled. Partial files were removed."
        return it.message  # Done / Error

    def _layout_buttons(self) -> None:
        it = self.item
        if it.status == S_DL:
            names = [] if it.stopping else ["pause", "cancel"]
        elif it.status == S_MERGE:
            names = []
        elif it.status == S_QUEUED:
            names = ["remove"]
        elif it.status == S_PAUSED:
            names = ["resume", "remove"]
        elif it.status in (S_ERROR, S_CANCELLED):
            names = ["retry", "remove"]
        else:  # Done
            names = ["open", "remove", "delete"]
        for btn in self.buttons.values():
            btn.pack_forget()
        for name in names:
            self.buttons[name].pack(side="left", padx=(6, 0))

    # -- public updates ----------------------------------------------------------
    def update_progress(self) -> None:
        self.bar.set(self.item.progress)
        self.stats_lbl.configure(text=self._stats_text())

    def refresh(self) -> None:
        it = self.item
        badge_text = it.status
        if it.stopping:
            badge_text = "Pausing…" if it.stop_reason == "pause" else "Cancelling…"
        self.badge.configure(text=f"  {badge_text}  ", fg_color=BADGE_COLORS.get(it.status, "#3f3f4b"))
        self.title_lbl.configure(text=truncate(it.title, 90))
        self.meta_lbl.configure(text=self._meta_text())

        if it.status == S_MERGE:
            if not self._indeterminate:
                self.bar.configure(mode="indeterminate", progress_color=WARN)
                self.bar.start()
                self._indeterminate = True
        else:
            if self._indeterminate:
                self.bar.stop()
                self.bar.configure(mode="determinate")
                self._indeterminate = False
            self.bar.configure(progress_color=BAR_COLORS.get(it.status, ACCENT))
            self.bar.set(it.progress)

        color = {S_ERROR: DANGER, S_DONE: OK}.get(it.status, MUTED)
        self.stats_lbl.configure(text=self._stats_text(), text_color=color)
        self._layout_buttons()


# --------------------------------------------------------------------------- #
# Playlist picker dialog
# --------------------------------------------------------------------------- #
class PlaylistDialog(ctk.CTkToplevel):
    """Checklist of playlist videos with a Select All toggle."""

    BATCH = 30  # widgets created per tick so huge playlists don't freeze the UI

    def __init__(self, master, title: str, entries: list[dict], skipped: int, on_close) -> None:
        super().__init__(master)
        self.title("Select videos")
        self.configure(fg_color=BG)
        w, h = 720, 640
        x = master.winfo_rootx() + max((master.winfo_width() - w) // 2, 0)
        y = master.winfo_rooty() + max((master.winfo_height() - h) // 2, 0)
        self.geometry(f"{w}x{h}+{x}+{y}")
        self.minsize(560, 460)
        self.transient(master)

        self._entries = entries
        self._on_close = on_close
        self._closed = False
        self._built = 0
        self._vars = [tk.BooleanVar(master=self, value=True) for _ in entries]
        self._all_var = tk.BooleanVar(master=self, value=True)
        self.protocol("WM_DELETE_WINDOW", self._cancel)

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        head = ctk.CTkFrame(self, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=24, pady=(22, 8))
        ctk.CTkLabel(
            head, text=truncate(title, 70), anchor="w",
            font=ctk.CTkFont(size=20, weight="bold"), text_color=TEXT,
        ).pack(anchor="w")
        sub = f"{len(entries)} videos found"
        if skipped:
            sub += f"  ·  {skipped} unavailable hidden"
        ctk.CTkLabel(head, text=sub, anchor="w", font=ctk.CTkFont(size=13), text_color=MUTED).pack(anchor="w")

        bar = ctk.CTkFrame(self, fg_color=CARD, corner_radius=14)
        bar.grid(row=1, column=0, sticky="ew", padx=24, pady=(4, 8))
        bar.grid_columnconfigure(1, weight=1)
        ctk.CTkCheckBox(
            bar, text="Select All", variable=self._all_var, command=self._toggle_all,
            font=ctk.CTkFont(size=13, weight="bold"), text_color=TEXT,
            fg_color=ACCENT, hover_color=ACCENT_HOVER, border_color=MUTED,
            checkbox_width=20, checkbox_height=20, corner_radius=6,
        ).grid(row=0, column=0, padx=16, pady=12, sticky="w")
        self.count_lbl = ctk.CTkLabel(bar, text="", font=ctk.CTkFont(size=13), text_color=MUTED)
        self.count_lbl.grid(row=0, column=1, padx=16, sticky="e")

        self.list_frame = ctk.CTkScrollableFrame(self, fg_color=CARD, corner_radius=14)
        self.list_frame.grid(row=2, column=0, sticky="nsew", padx=24, pady=(0, 8))

        foot = ctk.CTkFrame(self, fg_color="transparent")
        foot.grid(row=3, column=0, sticky="ew", padx=24, pady=(4, 22))
        foot.grid_columnconfigure(0, weight=1)
        ctk.CTkButton(
            foot, text="Cancel", width=100, height=42, corner_radius=12,
            fg_color=FIELD, hover_color=FIELD_HOVER, text_color=TEXT,
            font=ctk.CTkFont(size=14, weight="bold"), command=self._cancel,
        ).grid(row=0, column=1, padx=(0, 8))
        self.add_btn = ctk.CTkButton(
            foot, text="Add to Queue", width=170, height=42, corner_radius=12,
            fg_color=ACCENT, hover_color=ACCENT_HOVER, text_color="white",
            font=ctk.CTkFont(size=14, weight="bold"), command=self._confirm,
        )
        self.add_btn.grid(row=0, column=2)

        self._update_counts()
        self.after(10, self._populate)
        self.after(150, self._grab)

    def _grab(self) -> None:
        try:
            self.grab_set()
            self.focus_force()
        except tk.TclError:
            pass

    def _populate(self) -> None:
        if self._closed:
            return
        end = min(self._built + self.BATCH, len(self._entries))
        for i in range(self._built, end):
            e = self._entries[i]
            label = f"{i + 1:>3}.  {truncate(e['title'], 74)}"
            dur = format_duration(e.get("duration"))
            if dur:
                label += f"   [{dur}]"
            ctk.CTkCheckBox(
                self.list_frame, text=label, variable=self._vars[i], command=self._on_item_toggle,
                font=ctk.CTkFont(size=13), text_color=TEXT,
                fg_color=ACCENT, hover_color=ACCENT_HOVER, border_color=MUTED,
                checkbox_width=20, checkbox_height=20, corner_radius=6,
            ).pack(anchor="w", fill="x", padx=12, pady=5)
        self._built = end
        if self._built < len(self._entries):
            self.after(15, self._populate)

    def _selected_count(self) -> int:
        return sum(1 for v in self._vars if v.get())

    def _update_counts(self) -> None:
        n = self._selected_count()
        self.count_lbl.configure(text=f"{n} of {len(self._vars)} selected")
        self.add_btn.configure(
            text=f"Add {n} to Queue" if n else "Add to Queue",
            state="normal" if n else "disabled",
        )

    def _toggle_all(self) -> None:
        value = self._all_var.get()
        for var in self._vars:
            var.set(value)
        self._update_counts()

    def _on_item_toggle(self) -> None:
        self._all_var.set(all(v.get() for v in self._vars))
        self._update_counts()

    def _confirm(self) -> None:
        selected = [e for e, v in zip(self._entries, self._vars) if v.get()]
        self._finish(selected)

    def _cancel(self) -> None:
        self._finish([])

    def _finish(self, selected: list[dict]) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.destroy()
        self._on_close(selected)


# --------------------------------------------------------------------------- #
# Settings dialog
# --------------------------------------------------------------------------- #
class SettingsDialog(ctk.CTkToplevel):
    """Non-modal settings window: browser cookies, extra media options, engine updater."""

    def __init__(self, app: "App") -> None:
        super().__init__(app)
        self.app = app
        self._bar_on = False
        self.title("Settings")
        self.configure(fg_color=BG)
        w, h = 580, 720
        x = app.winfo_rootx() + max((app.winfo_width() - w) // 2, 0)
        y = app.winfo_rooty() + 40
        self.geometry(f"{w}x{h}+{x}+{y}")
        self.minsize(540, 660)
        self.transient(app)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            self, text="Settings", anchor="w", text_color=TEXT, font=ctk.CTkFont(size=22, weight="bold")
        ).grid(row=0, column=0, sticky="w", padx=24, pady=(22, 12))

        # -- cookies --------------------------------------------------------
        cookies = self._card(1)
        self._heading(cookies, "COOKIES FROM BROWSER")
        ctk.CTkOptionMenu(
            cookies, values=COOKIE_BROWSERS, variable=app.var_cookies,
            command=lambda _v: app._save_settings(), height=40, corner_radius=12,
            fg_color=FIELD, button_color=FIELD_HOVER, button_hover_color=ACCENT,
            dropdown_fg_color=FIELD, dropdown_hover_color=ACCENT, text_color=TEXT,
            font=ctk.CTkFont(size=13), dropdown_font=ctk.CTkFont(size=13),
        ).pack(fill="x", padx=16, pady=(0, 8))
        self._note(
            cookies,
            "Uses your logged-in browser session for age-restricted or login-walled videos. "
            "Close the browser first; Chrome-family browsers can lock or encrypt their cookies, "
            "so Firefox is usually the most reliable. Applies to links you add from now on.",
        )

        # -- extra media options -------------------------------------------
        extras = self._card(2)
        self._heading(extras, "EXTRA MEDIA OPTIONS")
        for label, var in (
            ("Embed Subtitles (CC)", app.var_subs),
            ("Embed Thumbnail as Cover Art", app.var_thumb),
            ("SponsorBlock (Skip Sponsors/Intros)", app.var_sponsor),
        ):
            ctk.CTkCheckBox(
                extras, text=label, variable=var, command=app._save_settings,
                font=ctk.CTkFont(size=13), text_color=TEXT, fg_color=ACCENT,
                hover_color=ACCENT_HOVER, border_color=MUTED,
                checkbox_width=20, checkbox_height=20, corner_radius=6,
            ).pack(anchor="w", padx=16, pady=6)
        self._note(
            extras,
            "Subtitles: English (manual or auto-generated), embedded into video files. Cover art works "
            "for video and MP3/M4A. SponsorBlock cuts out sponsor, intro, outro, self-promo and "
            "reminder segments (YouTube only). Applies to links you add from now on.",
        )

        # -- engine updater -------------------------------------------------
        engine = self._card(3)
        self._heading(engine, "YT-DLP ENGINE")
        ctk.CTkLabel(
            engine, text=f"Installed version:  {YTDLP_VERSION}", anchor="w",
            text_color=TEXT, font=ctk.CTkFont(size=13),
        ).pack(anchor="w", padx=16, pady=(0, 2))
        self._note(
            engine,
            "Packaged build: updates are downloaded now and applied on the next launch."
            if getattr(sys, "frozen", False)
            else "Python install: updates run 'pip install --upgrade yt-dlp[default]'; restart afterwards.",
        )
        self.update_btn = ctk.CTkButton(
            engine, text="Check for yt-dlp Updates", height=40, corner_radius=12,
            fg_color=ACCENT, hover_color=ACCENT_HOVER, text_color="white",
            font=ctk.CTkFont(size=13, weight="bold"), command=app.start_update_check,
        )
        self.update_btn.pack(fill="x", padx=16, pady=(4, 8))
        self.update_bar = ctk.CTkProgressBar(
            engine, height=6, mode="indeterminate", fg_color=FIELD, progress_color=ACCENT
        )
        self.status_lbl = ctk.CTkLabel(
            engine, text="", anchor="w", justify="left", wraplength=500,
            font=ctk.CTkFont(size=12), text_color=MUTED,
        )
        self.status_lbl.pack(anchor="w", padx=16, pady=(0, 14))
        self.show_update_status(*app._update_state)

    # -- small builders ------------------------------------------------------
    def _card(self, row: int) -> ctk.CTkFrame:
        frame = ctk.CTkFrame(self, fg_color=CARD, corner_radius=16)
        frame.grid(row=row, column=0, sticky="ew", padx=24, pady=(0, 12))
        return frame

    @staticmethod
    def _heading(parent, text: str) -> None:
        ctk.CTkLabel(
            parent, text=text, anchor="w", text_color=MUTED, font=ctk.CTkFont(size=12, weight="bold")
        ).pack(anchor="w", padx=16, pady=(14, 6))

    @staticmethod
    def _note(parent, text: str) -> None:
        ctk.CTkLabel(
            parent, text=text, anchor="w", justify="left", wraplength=500,
            text_color=MUTED, font=ctk.CTkFont(size=12),
        ).pack(anchor="w", padx=16, pady=(0, 12))

    # -- called by the app ------------------------------------------------------
    def show_update_status(self, text: str, color: str, busy: bool) -> None:
        self.status_lbl.configure(text=text, text_color=color)
        if busy:
            self.update_btn.configure(state="disabled", text="Checking…")
            if not self._bar_on:
                self.update_bar.pack(fill="x", padx=16, pady=(0, 8), before=self.status_lbl)
                self.update_bar.start()
                self._bar_on = True
        else:
            self.update_btn.configure(state="normal", text="Check for yt-dlp Updates")
            if self._bar_on:
                self.update_bar.stop()
                self.update_bar.pack_forget()
                self._bar_on = False

    def close(self) -> None:
        self.app._settings_dialog = None
        self.app._save_settings()
        self.destroy()


# --------------------------------------------------------------------------- #
# Application
# --------------------------------------------------------------------------- #
class App(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("dark-blue")

        self.title(APP_NAME)
        self.geometry("1040x900")
        self.minsize(920, 760)
        self.configure(fg_color=BG)

        self.config_data = load_config()
        saved_dir = self.config_data.get("last_dir")
        self.download_dir: str = (
            saved_dir if saved_dir and Path(saved_dir).is_dir() else str(Path.home() / "Downloads")
        )
        try:
            self.concurrency = min(max(int(self.config_data.get("concurrency", 2)), 1), 3)
        except (TypeError, ValueError):
            self.concurrency = 2

        # Phase 3 preferences shared with the settings dialog
        cfg = self.config_data
        self.var_cookies = tk.StringVar(master=self, value=cfg["cookies_browser"])
        self.var_subs = tk.BooleanVar(master=self, value=cfg["embed_subs"])
        self.var_thumb = tk.BooleanVar(master=self, value=cfg["embed_thumb"])
        self.var_sponsor = tk.BooleanVar(master=self, value=cfg["sponsorblock"])
        self.var_monitor = tk.BooleanVar(master=self, value=cfg["monitor_clipboard"])
        self._settings_dialog: SettingsDialog | None = None
        self._notice_after = None
        self._last_clip: str | None = None
        self._updating = False
        self._update_state = ("", MUTED, False)

        # queue state (main thread only)
        self.ui_queue: queue.Queue = queue.Queue()
        self.items: dict[int, QueueItem] = {}
        self.order: list[int] = []
        self.rows: dict[int, QueueRow] = {}
        self._next_id = 1
        self._row_pending: deque[int] = deque()
        self._row_tick_running = False
        self._resolving = 0
        self._playlist_pending: deque = deque()
        self._playlist_dialog: PlaylistDialog | None = None
        self._banner_visible = False
        self._ffmpeg_warned = False
        self._closing = False

        # thumbnail loader threads
        self.thumb_jobs: queue.Queue = queue.Queue()
        for _ in range(3):
            threading.Thread(target=self._thumb_loop, daemon=True).start()

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(60, self._poll_queue)
        self.after(1500, self._periodic)
        self.after(400, self._startup_checks)
        if self.var_monitor.get():
            self._last_clip = self._read_clipboard()  # ignore whatever is already on the clipboard
        self.after(800, self._clip_tick)

    # ------------------------------------------------------------------ UI --
    def _card(self, parent, **kw) -> ctk.CTkFrame:
        return ctk.CTkFrame(parent, fg_color=CARD, corner_radius=18, **kw)

    def _build_ui(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)
        root = ctk.CTkFrame(self, fg_color="transparent")
        root.grid(row=0, column=0, sticky="nsew", padx=28, pady=22)
        root.grid_columnconfigure(0, weight=1)
        root.grid_rowconfigure(5, weight=1)

        # Header
        header = ctk.CTkFrame(root, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", pady=(0, 14))
        header.grid_columnconfigure(0, weight=1)
        titles = ctk.CTkFrame(header, fg_color="transparent")
        titles.grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(
            titles, text="4K Downloader", font=ctk.CTkFont(size=28, weight="bold"), text_color=TEXT
        ).pack(anchor="w")
        ctk.CTkLabel(
            titles, text="Queue videos and playlists. Downloads start automatically.",
            font=ctk.CTkFont(size=13), text_color=MUTED,
        ).pack(anchor="w")

        controls = ctk.CTkFrame(header, fg_color="transparent")
        controls.grid(row=0, column=1, sticky="e")
        self.monitor_switch = ctk.CTkSwitch(
            controls, text="Monitor Clipboard", variable=self.var_monitor,
            command=self._on_monitor_toggle, progress_color=ACCENT, fg_color=FIELD_HOVER,
            button_color=TEXT, button_hover_color="white", text_color=TEXT,
            font=ctk.CTkFont(size=13),
        )
        self.monitor_switch.pack(side="left", padx=(0, 16))
        ctk.CTkButton(
            controls, text="⚙  Settings", width=112, height=36, corner_radius=10,
            fg_color=FIELD, hover_color=FIELD_HOVER, text_color=TEXT,
            font=ctk.CTkFont(size=13, weight="bold"), command=self.open_settings,
        ).pack(side="left")

        # FFmpeg banner
        self.banner = ctk.CTkLabel(
            root, text="", fg_color="#3a1d1d", text_color="#ffb4b4", corner_radius=12,
            font=ctk.CTkFont(size=12), anchor="w", justify="left", padx=14, pady=10,
        )
        self.banner.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        self.banner.grid_remove()

        # Subtle notice bar (clipboard offers, update results)
        self.notice = ctk.CTkFrame(
            root, fg_color="#1a1a2b", corner_radius=12, border_width=1, border_color=ACCENT
        )
        self.notice.grid(row=2, column=0, sticky="ew", pady=(0, 10))
        self.notice.grid_columnconfigure(0, weight=1)
        self.notice_label = ctk.CTkLabel(
            self.notice, text="", anchor="w", justify="left", text_color=TEXT,
            font=ctk.CTkFont(size=13),
        )
        self.notice_label.grid(row=0, column=0, sticky="ew", padx=(14, 8), pady=10)
        self.notice_btns = ctk.CTkFrame(self.notice, fg_color="transparent")
        self.notice_btns.grid(row=0, column=1, padx=(0, 4), pady=6)
        ctk.CTkButton(
            self.notice, text="✕", width=30, height=30, corner_radius=8, fg_color="transparent",
            hover_color=FIELD, text_color=MUTED, command=self._hide_notice,
        ).grid(row=0, column=2, padx=(0, 8))
        self.notice.grid_remove()

        # Add card
        add_card = self._card(root)
        add_card.grid(row=3, column=0, sticky="ew", pady=(0, 12))
        add_card.grid_columnconfigure(0, weight=1)

        self.url_box = ctk.CTkTextbox(
            add_card, height=76, corner_radius=12, border_width=0, fg_color=FIELD,
            text_color=TEXT, font=ctk.CTkFont(size=13), wrap="none",
        )
        self.url_box.grid(row=0, column=0, rowspan=2, sticky="ew", padx=(16, 8), pady=(16, 6))
        self.url_box.bind("<Control-Return>", lambda _e: (self.on_add(), "break")[1])

        self.paste_btn = ctk.CTkButton(
            add_card, text="Paste", width=140, height=34, corner_radius=10,
            fg_color=FIELD, hover_color=FIELD_HOVER, text_color=TEXT,
            font=ctk.CTkFont(size=13, weight="bold"), command=self.on_paste,
        )
        self.paste_btn.grid(row=0, column=1, padx=(0, 16), pady=(16, 4), sticky="n")

        self.add_btn = ctk.CTkButton(
            add_card, text="Add to Queue", width=140, height=34, corner_radius=10,
            fg_color=ACCENT, hover_color=ACCENT_HOVER, text_color="white",
            font=ctk.CTkFont(size=13, weight="bold"), command=self.on_add,
        )
        self.add_btn.grid(row=1, column=1, padx=(0, 16), pady=(0, 6), sticky="s")

        self.add_status = ctk.CTkLabel(
            add_card,
            text="One link per line (or several at once). Playlist links open a picker. Ctrl+Enter to add.",
            anchor="w", justify="left", wraplength=820, font=ctk.CTkFont(size=12), text_color=MUTED,
        )
        self.add_status.grid(row=2, column=0, columnspan=2, sticky="ew", padx=18, pady=(0, 12))

        # Options card
        opt_card = self._card(root)
        opt_card.grid(row=4, column=0, sticky="ew", pady=(0, 12))
        opt_card.grid_columnconfigure((0, 1, 2), weight=1, uniform="opt")

        def field_label(text: str, col: int) -> None:
            ctk.CTkLabel(
                opt_card, text=text, font=ctk.CTkFont(size=12, weight="bold"),
                text_color=MUTED, anchor="w",
            ).grid(row=0, column=col, sticky="w", padx=16, pady=(14, 4))

        field_label("QUALITY", 0)
        field_label("CONTAINER", 1)
        field_label("SIMULTANEOUS DOWNLOADS", 2)

        menu_style = dict(
            height=40, corner_radius=12, fg_color=FIELD, button_color=FIELD_HOVER,
            button_hover_color=ACCENT, dropdown_fg_color=FIELD, dropdown_hover_color=ACCENT,
            text_color=TEXT, font=ctk.CTkFont(size=13), dropdown_font=ctk.CTkFont(size=13),
        )
        self.quality_menu = ctk.CTkOptionMenu(
            opt_card, values=list(QUALITY_OPTIONS.keys()), command=self._on_quality_change, **menu_style
        )
        saved_q = self.config_data.get("quality")
        self.quality_menu.set(saved_q if saved_q in QUALITY_OPTIONS else "Best Available (up to 8K)")
        self.quality_menu.grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 12))

        self.container_menu = ctk.CTkOptionMenu(
            opt_card, values=CONTAINERS, command=self._on_container_change, **menu_style
        )
        saved_c = self.config_data.get("container")
        self.container_menu.set(saved_c if saved_c in CONTAINERS else "MP4")
        self.container_menu.grid(row=1, column=1, sticky="ew", padx=16, pady=(0, 12))
        if is_audio_quality(self.quality_menu.get()):
            self.container_menu.configure(state="disabled")

        self.concurrency_seg = ctk.CTkSegmentedButton(
            opt_card, values=["1", "2", "3"], command=self._on_concurrency, height=40,
            corner_radius=12, fg_color=FIELD, selected_color=ACCENT, selected_hover_color=ACCENT_HOVER,
            unselected_color=FIELD, unselected_hover_color=FIELD_HOVER, text_color=TEXT,
            font=ctk.CTkFont(size=13, weight="bold"),
        )
        self.concurrency_seg.set(str(self.concurrency))
        self.concurrency_seg.grid(row=1, column=2, sticky="ew", padx=16, pady=(0, 12))

        folder_row = ctk.CTkFrame(opt_card, fg_color="transparent")
        folder_row.grid(row=2, column=0, columnspan=3, sticky="ew", padx=16, pady=(0, 16))
        folder_row.grid_columnconfigure(1, weight=1)
        ctk.CTkButton(
            folder_row, text="Choose Folder", width=140, height=40, corner_radius=12,
            fg_color=FIELD, hover_color=FIELD_HOVER, text_color=TEXT,
            font=ctk.CTkFont(size=13, weight="bold"), command=self.on_choose_folder,
        ).grid(row=0, column=0, sticky="w")
        self.folder_label = ctk.CTkLabel(
            folder_row, text=self._short_path(self.download_dir),
            font=ctk.CTkFont(size=12), text_color=MUTED, anchor="w",
        )
        self.folder_label.grid(row=0, column=1, sticky="ew", padx=(12, 0))

        # Queue card
        queue_card = self._card(root)
        queue_card.grid(row=5, column=0, sticky="nsew")
        queue_card.grid_columnconfigure(0, weight=1)
        queue_card.grid_rowconfigure(1, weight=1)

        toolbar = ctk.CTkFrame(queue_card, fg_color="transparent")
        toolbar.grid(row=0, column=0, sticky="ew", padx=16, pady=(14, 6))
        toolbar.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(
            toolbar, text="Queue", font=ctk.CTkFont(size=16, weight="bold"), text_color=TEXT
        ).grid(row=0, column=0, sticky="w")
        self.summary_lbl = ctk.CTkLabel(
            toolbar, text="", font=ctk.CTkFont(size=12), text_color=MUTED, anchor="w"
        )
        self.summary_lbl.grid(row=0, column=1, sticky="w", padx=(14, 0))
        ctk.CTkButton(
            toolbar, text="Clear Completed", width=130, height=32, corner_radius=10,
            fg_color=FIELD, hover_color=FIELD_HOVER, text_color=TEXT,
            font=ctk.CTkFont(size=12, weight="bold"), command=self.on_clear_completed,
        ).grid(row=0, column=2, sticky="e")

        self.queue_frame = ctk.CTkScrollableFrame(queue_card, fg_color="transparent")
        self.queue_frame.grid(row=1, column=0, sticky="nsew", padx=(10, 6), pady=(0, 12))

        self.empty_label = ctk.CTkLabel(
            self.queue_frame,
            text="Your queue is empty.\nPaste one or more links above and click Add to Queue.",
            font=ctk.CTkFont(size=13), text_color=MUTED, justify="center",
        )
        self.empty_label.pack(pady=70)
        self._update_summary()

    # --------------------------------------------------------- misc helpers --
    @staticmethod
    def _short_path(path: str, limit: int = 70) -> str:
        return path if len(path) <= limit else "…" + path[-(limit - 1):]

    def _set_add_status(self, text: str, color: str = MUTED) -> None:
        self.add_status.configure(text=text, text_color=color)

    def _refresh_add_button(self) -> None:
        if self._resolving > 0:
            self.add_btn.configure(state="disabled", text="Analyzing…")
        else:
            self.add_btn.configure(state="normal", text="Add to Queue")

    def _on_quality_change(self, choice: str) -> None:
        self.container_menu.configure(state="disabled" if is_audio_quality(choice) else "normal")
        self._save_settings()

    def _on_container_change(self, choice: str) -> None:
        self._save_settings()

    def _on_concurrency(self, value: str) -> None:
        self.concurrency = int(value)
        self._save_settings()
        self._schedule()  # a higher limit can start waiting items immediately

    # ----------------------------------------------------------- FFmpeg ----
    def _startup_checks(self) -> None:
        if not ffmpeg_available():
            self._show_ffmpeg_warning()

    def _show_ffmpeg_warning(self) -> None:
        if not self._banner_visible:
            self.banner.configure(
                text="⚠  FFmpeg was not found on your PATH. 4K merging and MP3 extraction need it. "
                     "Queued items will start once it is installed.\n" + ffmpeg_install_hint()
            )
            self.banner.grid()
            self._banner_visible = True
        if not self._ffmpeg_warned:
            self._ffmpeg_warned = True
            messagebox.showwarning(
                "FFmpeg not found",
                "FFmpeg is required to merge 4K video + audio streams and to extract MP3/M4A.\n\n"
                + ffmpeg_install_hint()
                + "\n\nQueued downloads will begin automatically after you install it.",
            )

    # ------------------------------------------------------------- actions --
    def on_paste(self) -> None:
        try:
            text = self.clipboard_get().strip()
        except tk.TclError:
            self._set_add_status("Clipboard is empty.", WARN)
            return
        existing = self.url_box.get("1.0", "end").strip()
        if existing:
            self.url_box.insert("end", ("" if self.url_box.get("end-2c", "end-1c") == "\n" else "\n") + text)
        else:
            self.url_box.insert("1.0", text)

    def on_choose_folder(self) -> None:
        chosen = filedialog.askdirectory(initialdir=self.download_dir, title="Choose download folder")
        if chosen:
            self.download_dir = chosen
            self.folder_label.configure(text=self._short_path(chosen))
            self._save_settings()

    def on_add(self) -> None:
        urls = extract_urls(self.url_box.get("1.0", "end"))
        if not urls:
            self._set_add_status("No valid links found. Paste one or more video or playlist URLs.", WARN)
            return

        self.url_box.delete("1.0", "end")
        self._submit_urls(urls)

    def _submit_urls(self, urls: list[str]) -> None:
        """Snapshot the current options, then analyse the links in a background thread."""
        quality = self.quality_menu.get()
        audio = is_audio_quality(quality)
        browser = self.var_cookies.get()
        settings = {
            "quality": quality,
            "container": "" if audio else self.container_menu.get(),
            "folder": self.download_dir,
            "cookies": "" if browser == "None" else browser,
            "subs": bool(self.var_subs.get()) and not audio,
            "thumb": bool(self.var_thumb.get()),
            "sponsorblock": bool(self.var_sponsor.get()),
        }
        self._resolving += len(urls)
        self._refresh_add_button()
        self._set_add_status(f"Analyzing {len(urls)} link{'s' if len(urls) != 1 else ''}…")
        threading.Thread(target=self._resolve_worker, args=(urls, settings), daemon=True).start()

    # ---------------------------------------------------------- settings ----
    def _save_settings(self) -> None:
        """Write every preference to config.json."""
        self.config_data.update({
            "last_dir": self.download_dir,
            "quality": self.quality_menu.get(),
            "container": self.container_menu.get(),
            "concurrency": self.concurrency,
            "cookies_browser": self.var_cookies.get(),
            "embed_subs": bool(self.var_subs.get()),
            "embed_thumb": bool(self.var_thumb.get()),
            "sponsorblock": bool(self.var_sponsor.get()),
            "monitor_clipboard": bool(self.var_monitor.get()),
        })
        save_config(self.config_data)

    def open_settings(self) -> None:
        dlg = self._settings_dialog
        if dlg is not None:
            try:
                dlg.lift()
                dlg.focus_force()
                return
            except tk.TclError:
                self._settings_dialog = None
        self._settings_dialog = SettingsDialog(self)

    # --------------------------------------------------------- notice bar ----
    def _show_notice(self, text: str, actions=(), timeout: int = 12000, color: str = ACCENT) -> None:
        if self._notice_after is not None:
            try:
                self.after_cancel(self._notice_after)
            except Exception:
                pass
            self._notice_after = None
        for child in self.notice_btns.winfo_children():
            child.destroy()
        self.notice.configure(border_color=color)
        self.notice_label.configure(text=text)
        for i, (label, callback) in enumerate(actions):
            ctk.CTkButton(
                self.notice_btns, text=label, height=30, corner_radius=8,
                fg_color=ACCENT if i == 0 else FIELD_HOVER,
                hover_color=ACCENT_HOVER if i == 0 else "#3d3d4a",
                text_color="white", font=ctk.CTkFont(size=12, weight="bold"), command=callback,
            ).pack(side="left", padx=4)
        self.notice.grid()
        if timeout:
            self._notice_after = self.after(timeout, self._hide_notice)

    def _hide_notice(self) -> None:
        if self._notice_after is not None:
            try:
                self.after_cancel(self._notice_after)
            except Exception:
                pass
            self._notice_after = None
        self.notice.grid_remove()

    # ---------------------------------------------------- clipboard watcher ----
    def _read_clipboard(self) -> str | None:
        try:
            return self.clipboard_get()
        except tk.TclError:  # empty, or not text
            return None

    def _on_monitor_toggle(self) -> None:
        self._save_settings()
        if self.var_monitor.get():
            self._last_clip = self._read_clipboard()
            self._set_add_status("Clipboard monitor is on: copy a link and you'll be offered to add it.")
        else:
            self._hide_notice()

    def _clip_tick(self) -> None:
        if self._closing:
            return
        if self.var_monitor.get():
            clip = self._read_clipboard()
            if clip is not None and clip != self._last_clip:
                self._last_clip = clip
                urls = clipboard_urls(clip)
                if urls:
                    self._offer_clipboard_urls(urls)
        self.after(800, self._clip_tick)

    def _offer_clipboard_urls(self, urls: list[str]) -> None:
        queued = {it.url for it in self.items.values()}
        fresh = [u for u in urls if u not in queued]
        if not fresh:
            return
        label = truncate(fresh[0], 70) if len(fresh) == 1 else f"{len(fresh)} links"
        self._show_notice(
            f"🔗  Link copied: {label}",
            actions=[
                ("Add to Queue", lambda: self._use_clipboard_urls(fresh, True)),
                ("Put in URL Bar", lambda: self._use_clipboard_urls(fresh, False)),
            ],
            timeout=15000,
        )

    def _use_clipboard_urls(self, urls: list[str], add_now: bool) -> None:
        self._hide_notice()
        if add_now:
            self._submit_urls(urls)
        else:
            has_text = bool(self.url_box.get("1.0", "end").strip())
            self.url_box.insert("end", ("\n" if has_text else "") + "\n".join(urls))

    # ----------------------------------------------------- engine updater ----
    def _set_update_status(self, text: str, color: str = MUTED, busy: bool = False) -> None:
        self._update_state = (text, color, busy)
        self._updating = busy
        dlg = self._settings_dialog
        if dlg is not None:
            try:
                dlg.show_update_status(text, color, busy)
            except tk.TclError:
                self._settings_dialog = None

    def start_update_check(self) -> None:
        if self._updating:
            return
        self._set_update_status("Checking for updates…", MUTED, busy=True)
        threading.Thread(target=self._update_worker, daemon=True).start()

    def _update_worker(self) -> None:
        post = self.ui_queue.put
        current = YTDLP_VERSION
        try:
            if getattr(sys, "frozen", False):
                # Packaged build: the library lives inside the exe, so fetch a fresh engine to load next launch.
                latest = latest_release_tag()
                if version_tuple(latest) <= version_tuple(current):
                    post(("update_done", True, f"yt-dlp is up to date ({current}).", False))
                    return
                post(("update_progress", f"Downloading yt-dlp {latest}…"))
                download_engine(latest)
                post(("update_done", True, f"Updated yt-dlp {current} → {latest}. Restart the app to use it.", True))
                return

            # Python install: pip is the supported way to update a pip-installed yt-dlp.
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            proc = subprocess.run(
                [sys.executable, "-m", "pip", "install", "--upgrade", "--disable-pip-version-check",
                 "yt-dlp[default]"],
                capture_output=True, text=True, timeout=600, creationflags=flags,
            )
            if proc.returncode != 0:
                err = (proc.stderr or proc.stdout or "").strip()
                if "externally-managed" in err:
                    raise RuntimeError("This Python is externally managed. Run the app inside a virtual environment.")
                raise RuntimeError(err.splitlines()[-1] if err else "pip failed")
            probe = subprocess.run(
                [sys.executable, "-c", "import importlib.metadata as m; print(m.version('yt-dlp'))"],
                capture_output=True, text=True, timeout=60, creationflags=flags,
            )
            new = probe.stdout.strip()
            if new and version_tuple(new) > version_tuple(current):
                post(("update_done", True, f"Updated yt-dlp {current} → {new}. Restart the app to use it.", True))
            else:
                post(("update_done", True, f"yt-dlp is up to date ({current}).", False))
        except Exception as exc:
            post(("update_done", False, f"Update failed: {truncate(str(exc), 200)}", False))

    def on_clear_completed(self) -> None:
        for iid in [i for i in self.order if self.items[i].status == S_DONE]:
            self._remove_item(iid)
        self._update_summary()

    def _on_row_action(self, item_id: int, action: str) -> None:
        it = self.items.get(item_id)
        if it is None:
            return

        if action in ("pause", "cancel"):
            if it.status == S_DL and not it.stopping:
                it.stop_reason = "pause" if action == "pause" else "cancel"
                it.stopping = True
                it.stop_event.set()
                self._refresh_row(it)
        elif action in ("resume", "retry"):
            if it.status in (S_PAUSED, S_ERROR, S_CANCELLED):
                if action == "retry":
                    it.progress = 0.0
                it.status = S_QUEUED
                it.message = ""
                self._refresh_row(it)
                self._schedule()
        elif action == "remove":
            if it.status in ACTIVE_STATES:
                return
            if it.status == S_PAUSED:
                cleanup_partials(it.folder, it.video_id)
            self._remove_item(item_id)
            self._update_summary()
        elif action == "open":
            reveal_in_file_manager(it.filepath, it.folder)
        elif action == "delete":
            self._delete_file(it)

    def _delete_file(self, it: QueueItem) -> None:
        """Delete the saved file from disk (after confirmation) and remove its card."""
        if it.status != S_DONE:
            return
        path = os.path.normpath(it.filepath) if it.filepath else ""
        if not path or not os.path.isfile(path):
            messagebox.showwarning(
                "File not found",
                "The saved file could not be found. It may have been moved or deleted.\n\n"
                "Use Remove to clear this item from the queue.",
                parent=self,
            )
            return
        if not messagebox.askyesno(
            "Delete file",
            f"Permanently delete this file from disk?\n\n{path}\n\nThis cannot be undone.",
            icon="warning", parent=self,
        ):
            return
        try:
            os.remove(path)
        except OSError as exc:
            messagebox.showerror("Delete failed", f"Could not delete the file:\n{exc}", parent=self)
            return
        self._remove_item(it.id)
        self._update_summary()
        self._set_add_status(f"Deleted {Path(path).name}")

    def _on_close(self) -> None:
        self._save_settings()
        active = [it for it in self.items.values() if it.status in ACTIVE_STATES]
        if active and not messagebox.askyesno(
            "Quit",
            f"{len(active)} download(s) are still running.\n\n"
            "Quit anyway? Partial files are kept, so re-adding the same video will resume it.",
        ):
            return
        self._closing = True
        for it in active:
            it.stop_reason = "pause"
            it.stop_event.set()
        self.destroy()

    # ------------------------------------------------ queue management ----
    def _enqueue_entries(self, entries: list[dict], settings: dict) -> None:
        existing = {
            (it.video_id, it.quality_label, it.container) for it in self.items.values() if it.video_id
        }
        added = dupes = 0
        for e in entries:
            key = (e.get("id", ""), settings["quality"], settings["container"])
            if e.get("id") and key in existing:
                dupes += 1
                continue
            it = QueueItem(
                id=self._next_id,
                url=e["url"],
                title=e.get("title") or e["url"],
                channel=e.get("channel") or "",
                duration=e.get("duration"),
                video_id=e.get("id") or "",
                max_height=e.get("max_height") or 0,
                quality_label=settings["quality"],
                container=settings["container"],
                folder=settings["folder"],
                thumb_url=e.get("thumb_url") or "",
                cookies_browser=settings.get("cookies", ""),
                embed_subs=settings.get("subs", False),
                embed_thumb=settings.get("thumb", False),
                sponsorblock=settings.get("sponsorblock", False),
            )
            self._next_id += 1
            self.items[it.id] = it
            self.order.append(it.id)
            self._row_pending.append(it.id)
            existing.add(key)
            added += 1

            pil = e.get("thumb_pil")
            if pil is not None:
                it.thumb_ctk = make_thumb(pil)
            elif it.thumb_url:
                self.thumb_jobs.put((it.id, it.thumb_url))

        msg = f"Added {added} video{'s' if added != 1 else ''} to the queue."
        if dupes:
            msg += f"  {dupes} already in the queue with the same settings were skipped."
        self._set_add_status(msg, OK if added else WARN)

        self._kick_row_builder()
        self._update_summary()
        self._schedule()

    def _remove_item(self, item_id: int) -> None:
        self.items.pop(item_id, None)
        if item_id in self.order:
            self.order.remove(item_id)
        row = self.rows.pop(item_id, None)
        if row is not None:
            row.destroy()
        self._sync_empty_state()

    def _sync_empty_state(self) -> None:
        if self.items:
            self.empty_label.pack_forget()
        elif not self.empty_label.winfo_ismapped():
            self.empty_label.pack(pady=70)

    def _kick_row_builder(self) -> None:
        if not self._row_tick_running:
            self._row_tick_running = True
            self.after(1, self._row_tick)

    def _row_tick(self) -> None:
        """Create row widgets a few at a time so adding a big playlist never freezes the UI."""
        if self._closing:
            return
        built = 0
        while self._row_pending and built < 4:
            iid = self._row_pending.popleft()
            it = self.items.get(iid)
            if it is None or iid in self.rows:
                continue
            row = QueueRow(self.queue_frame, it, self._on_row_action)
            row.pack(fill="x", padx=(0, 6), pady=(0, 10))
            self.rows[iid] = row
            built += 1
        self._sync_empty_state()
        if self._row_pending:
            self.after(15, self._row_tick)
        else:
            self._row_tick_running = False

    def _refresh_row(self, it: QueueItem) -> None:
        row = self.rows.get(it.id)
        if row is not None:
            row.refresh()
        self._update_summary()

    def _update_summary(self) -> None:
        if not self.items:
            self.summary_lbl.configure(text="")
            return
        c = Counter(it.status for it in self.items.values())
        parts = [f"{len(self.items)} total"]
        active = c[S_DL] + c[S_MERGE]
        for count, label in (
            (active, "active"), (c[S_QUEUED], "waiting"), (c[S_PAUSED], "paused"),
            (c[S_DONE], "done"), (c[S_ERROR] + c[S_CANCELLED], "failed/cancelled"),
        ):
            if count:
                parts.append(f"{count} {label}")
        self.summary_lbl.configure(text="  ·  ".join(parts))

    # --------------------------------------------------------- scheduler ----
    def _schedule(self) -> None:
        """Start waiting items until the concurrency limit is reached."""
        if self._closing:
            return
        if not any(self.items[i].status == S_QUEUED for i in self.order):
            return
        if not ffmpeg_available():
            self._show_ffmpeg_warning()
            return
        active = sum(1 for it in self.items.values() if it.status in ACTIVE_STATES)
        for iid in list(self.order):
            if active >= self.concurrency:
                break
            it = self.items[iid]
            if it.status == S_QUEUED:
                self._start_item(it)
                active += 1

    def _start_item(self, it: QueueItem) -> None:
        it.run_id += 1
        it.stop_event = threading.Event()
        it.stop_reason = None
        it.stopping = False
        it.status = S_DL
        it.message = ""
        it.stage = "Starting…"
        it.speed = None
        it.eta = None
        self._refresh_row(it)
        threading.Thread(
            target=self._download_worker, args=(it, it.run_id, it.stop_event), daemon=True
        ).start()

    def _periodic(self) -> None:
        if self._closing:
            return
        if self._banner_visible and ffmpeg_available():
            self.banner.grid_remove()
            self._banner_visible = False
            self._ffmpeg_warned = False
        self._schedule()
        self.after(1500, self._periodic)

    # ------------------------------------------------ worker: thumbnails ----
    def _thumb_loop(self) -> None:
        while True:
            iid, url = self.thumb_jobs.get()
            img = fetch_image(url)
            if img is not None:
                self.ui_queue.put(("thumb", iid, img))

    # --------------------------------------- worker: analyse pasted links ----
    def _resolve_worker(self, urls: list[str], settings: dict) -> None:
        for url in urls:
            try:
                opts = {
                    "quiet": True, "no_warnings": True, "skip_download": True,
                    "logger": _QuietLogger(),
                    "noplaylist": not wants_playlist(url),
                    "extract_flat": "in_playlist",   # fast listing; no per-video lookups
                }
                if settings.get("cookies"):
                    opts["cookiesfrombrowser"] = (settings["cookies"].lower(),)
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=False)
                if not info:
                    raise ValueError("No information was returned for that URL.")

                if info.get("_type") == "playlist" or info.get("entries") is not None:
                    entries, skipped = [], 0
                    for raw in info.get("entries") or []:
                        d = entry_to_dict(raw)
                        if d:
                            entries.append(d)
                        else:
                            skipped += 1
                    if not entries:
                        raise ValueError("That playlist has no available videos.")
                    self.ui_queue.put(
                        ("playlist", info.get("title") or "Playlist", entries, skipped, settings)
                    )
                else:
                    heights = [
                        f.get("height") or 0
                        for f in (info.get("formats") or [])
                        if f.get("vcodec") not in (None, "none")
                    ]
                    thumb_url = info.get("thumbnail") or ""
                    meta = {
                        "id": info.get("id") or "",
                        "url": info.get("webpage_url") or url,
                        "title": info.get("title") or url,
                        "channel": info.get("channel") or info.get("uploader") or "",
                        "duration": info.get("duration"),
                        "thumb_url": thumb_url,
                        "thumb_pil": fetch_image(thumb_url),
                        "max_height": max(heights) if heights else 0,
                    }
                    self.ui_queue.put(("video", meta, settings))
            except Exception as exc:
                self.ui_queue.put(("resolve_error", url, clean_error(exc)))

    # ----------------------------------------------- worker: download ----
    def _download_worker(self, it: QueueItem, run_id: int, stop_event: threading.Event) -> None:
        post = self.ui_queue.put
        audio_mode = is_audio_quality(it.quality_label)
        last_emit = [0.0]
        stream_done = [False]  # becomes True once a stream finished (post-processing follows)
        final_path = [None]    # newest output path reported by the postprocessor hooks

        def progress_hook(d: dict) -> None:
            if stop_event.is_set():
                raise DownloadStopped()
            status = d.get("status")
            if status == "finished":
                stream_done[0] = True
                return
            if status != "downloading":
                return
            now = time.monotonic()
            if now - last_emit[0] < 0.15:  # throttle UI traffic
                return
            last_emit[0] = now

            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            frac = min(done / total, 1.0) if total else 0.0

            # Map per-stream progress onto one monotonic bar: video 0-90%, audio 90-100%.
            info = d.get("info_dict") or {}
            has_v = info.get("vcodec") not in (None, "none")
            has_a = info.get("acodec") not in (None, "none")
            if audio_mode:
                overall, stage = frac, "Audio"
            elif has_v and not has_a:
                overall, stage = frac * 0.9, "Video stream"
            elif has_a and not has_v:
                overall, stage = 0.9 + frac * 0.1, "Audio stream"
            else:
                overall, stage = frac, "Downloading"
            post(("progress", it.id, run_id, overall, d.get("speed"), d.get("eta"), stage))

        pp_text = {
            "Merger": "Merging video + audio with FFmpeg…",
            "ExtractAudio": "Converting audio with FFmpeg…",
            "EmbedSubtitle": "Embedding subtitles…",
            "ModifyChapters": "Cutting sponsor segments…",
            "Metadata": "Writing metadata…",
            "EmbedThumbnail": "Embedding cover art…",
            "MoveFiles": "Finalizing…",
        }

        def pp_hook(d: dict) -> None:
            # Merge / convert / move steps report the current output file in info_dict.
            reported = (d.get("info_dict") or {}).get("filepath")
            if reported:
                final_path[0] = reported
            # Pre-download steps (SponsorBlock lookup, thumbnail conversion) also report "started";
            # ignore them so the row doesn't flip to "Merging" before any bytes have arrived.
            if d.get("status") != "started" or not stream_done[0]:
                return
            post(("busy", it.id, run_id, pp_text.get(d.get("postprocessor", ""), "Processing with FFmpeg…")))

        try:
            opts = build_ydl_opts(it, progress_hook, pp_hook)
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(it.url, download=True)
            post(("done", it.id, run_id, resolve_saved_file(final_path[0], info)))
        except Exception as exc:
            if stop_event.is_set() or isinstance(exc, DownloadStopped):
                if it.stop_reason == "cancel":
                    cleanup_partials(it.folder, it.video_id)
                post(("stopped", it.id, run_id))
            else:
                post(("error", it.id, run_id, clean_error(exc)))

    # ----------------------------------------- UI queue (main thread only) --
    def _poll_queue(self) -> None:
        if self._closing:
            return
        try:
            while True:
                self._handle_message(self.ui_queue.get_nowait())
        except queue.Empty:
            pass
        self.after(60, self._poll_queue)

    def _current(self, iid: int, run_id: int) -> QueueItem | None:
        """Return the item only if the message belongs to its current run."""
        it = self.items.get(iid)
        return it if it is not None and it.run_id == run_id else None

    def _handle_message(self, msg: tuple) -> None:
        kind = msg[0]

        if kind == "progress":
            _, iid, run_id, frac, speed, eta, stage = msg
            it = self._current(iid, run_id)
            if it is None or it.status != S_DL:
                return
            it.progress = max(it.progress, frac)
            it.speed, it.eta, it.stage = speed, eta, stage
            row = self.rows.get(iid)
            if row is not None and not it.stopping:
                row.update_progress()

        elif kind == "busy":
            _, iid, run_id, text = msg
            it = self._current(iid, run_id)
            if it is None or it.status not in ACTIVE_STATES:
                return
            it.status = S_MERGE
            it.stage = text
            self._refresh_row(it)

        elif kind == "done":
            _, iid, run_id, path = msg
            it = self._current(iid, run_id)
            if it is None:
                return
            it.status = S_DONE
            it.progress = 1.0
            it.stopping = False
            it.filepath = path
            it.message = f"Saved: {Path(path).name}" if path else f"Saved to {it.folder}"
            self._refresh_row(it)
            self._schedule()

        elif kind == "error":
            _, iid, run_id, text = msg
            it = self._current(iid, run_id)
            if it is None:
                return
            it.status = S_ERROR
            it.stopping = False
            it.message = text
            self._refresh_row(it)
            self._schedule()

        elif kind == "stopped":
            _, iid, run_id = msg
            it = self._current(iid, run_id)
            if it is None:
                return
            it.stopping = False
            if it.stop_reason == "cancel":
                it.status = S_CANCELLED
                it.progress = 0.0
            else:
                it.status = S_PAUSED
            self._refresh_row(it)
            self._schedule()

        elif kind == "thumb":
            _, iid, img = msg
            it = self.items.get(iid)
            if it is None:
                return
            it.thumb_ctk = make_thumb(img)
            row = self.rows.get(iid)
            if row is not None:
                row.set_thumb(it.thumb_ctk)

        elif kind == "video":
            _, meta, settings = msg
            self._resolving = max(self._resolving - 1, 0)
            self._refresh_add_button()
            self._enqueue_entries([meta], settings)

        elif kind == "playlist":
            _, title, entries, skipped, settings = msg
            self._resolving = max(self._resolving - 1, 0)
            self._refresh_add_button()
            self._set_add_status(f"Found a playlist with {len(entries)} videos. Choose which to add.")
            self._playlist_pending.append((title, entries, skipped, settings))
            self._show_next_playlist()

        elif kind == "resolve_error":
            _, url, text = msg
            self._resolving = max(self._resolving - 1, 0)
            self._refresh_add_button()
            self._set_add_status(f"Couldn't read {truncate(url, 60)}: {text}", DANGER)

        elif kind == "update_progress":
            self._set_update_status(msg[1], MUTED, busy=True)

        elif kind == "update_done":
            _, ok, text, _restart = msg
            self._set_update_status(text, OK if ok else DANGER, busy=False)
            self._show_notice(("✔  " if ok else "⚠  ") + text, timeout=12000, color=OK if ok else DANGER)

    def _show_next_playlist(self) -> None:
        if self._playlist_dialog is not None or not self._playlist_pending:
            return
        title, entries, skipped, settings = self._playlist_pending.popleft()

        def on_close(selected: list[dict]) -> None:
            self._playlist_dialog = None
            if selected:
                self._enqueue_entries(selected, settings)
            else:
                self._set_add_status("Playlist skipped.", MUTED)
            self._show_next_playlist()

        self._playlist_dialog = PlaylistDialog(self, title, entries, skipped, on_close)


# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    app = App()
    app.mainloop()
