#!/usr/bin/env python3
"""
image_glicko2_ranker_full.py

Windows-friendly Tkinter app for pairwise image ranking with Glicko-2.

Behavior:
- Choose a folder containing images
- One session shuffles the images and makes random disjoint pairs
- Each image appears at most once per session
- If the image count is odd, one image sits out for that session
- Click left/right image, or use arrow keys:
    Left  = left image wins
    Right = right image wins
    Up    = draw
    Down  = undo last comparison
- After the session, ratings update with Glicko-2
- Files are renamed with a prefixed metadata block so filesystem sorting works
- The same folder remains loaded, and a new session starts immediately

Requirements:
    pip install pillow

Supported formats:
    .jpg .jpeg .png .bmp .gif .webp
"""

from __future__ import annotations

from collections import deque
import hashlib
import importlib
import importlib.util
import json
import logging
import math
import os
import random
import re
import sys
import threading
import time
import traceback
import warnings
import argparse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Tuple

import tkinter as tk
from tkinter import filedialog, messagebox

try:
    from PIL import Image, ImageOps, ImageSequence, ImageTk
except ImportError:
    raise SystemExit("Pillow is required. Install it with: pip install pillow")

try:
    import imageio
    import imageio.v3 as iio
except ImportError:
    imageio = None
    iio = None

VLC_AVAILABLE = importlib.util.find_spec("vlc") is not None
vlc = importlib.import_module("vlc") if VLC_AVAILABLE else None
VLC_INSTANCE_OPTIONS = (
    "--no-audio",
    "--aout=dummy",
)
LOGGER = logging.getLogger("media_glicko2")
VLC_STATE_NAMES = {
    0: "NothingSpecial",
    1: "Opening",
    2: "Buffering",
    3: "Playing",
    4: "Paused",
    5: "Stopped",
    6: "Ended",
    7: "Error",
}
VLC_PLAY_PROBE_DELAYS_MS = (0.15, 0.5, 1.5, 4.0)
UI_HEARTBEAT_INTERVAL_MS = 500
UI_HEARTBEAT_STALE_SECONDS = 3.0
UI_WATCHDOG_POLL_SECONDS = 1.0
MAINTHREAD_CALL_WATCHDOG_DELAYS_SECONDS = (0.25, 1.0, 3.0)
UI_PHASE_HISTORY_LIMIT = 40


def configure_logging(debug: bool = False) -> None:
    level = logging.DEBUG if debug else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s [%(threadName)s] %(message)s",
    )
    LOGGER.debug("Debug logging enabled.")


# =========================
# GLICKO-2 IMPLEMENTATION
# =========================

GLICKO2_SCALE = 173.7178


@dataclass
class Glicko2Player:
    rating: float = 1500.0
    rd: float = 350.0
    sigma: float = 0.06
    matches: List[Tuple[float, float, float]] = field(default_factory=list)
    # each match = (opponent_rating, opponent_rd, score)

    def add_result(self, opponent_rating: float, opponent_rd: float, score: float) -> None:
        self.matches.append((opponent_rating, opponent_rd, score))

    def clear_matches(self) -> None:
        self.matches.clear()


def _to_mu(rating: float) -> float:
    return (rating - 1500.0) / GLICKO2_SCALE


def _to_phi(rd: float) -> float:
    return rd / GLICKO2_SCALE


def _to_rating(mu: float) -> float:
    return mu * GLICKO2_SCALE + 1500.0


def _to_rd(phi: float) -> float:
    return phi * GLICKO2_SCALE


def _g(phi_j: float) -> float:
    return 1.0 / math.sqrt(1.0 + 3.0 * (phi_j ** 2) / (math.pi ** 2))


def _E(mu: float, mu_j: float, phi_j: float) -> float:
    return 1.0 / (1.0 + math.exp(-_g(phi_j) * (mu - mu_j)))


def _compute_v(mu: float, opps: List[Tuple[float, float, float]]) -> float:
    total = 0.0
    for r_j, rd_j, _ in opps:
        mu_j = _to_mu(r_j)
        phi_j = _to_phi(rd_j)
        E_val = _E(mu, mu_j, phi_j)
        g_val = _g(phi_j)
        total += (g_val ** 2) * E_val * (1.0 - E_val)
    return 1.0 / total


def _compute_delta(mu: float, opps: List[Tuple[float, float, float]], v: float) -> float:
    total = 0.0
    for r_j, rd_j, s_j in opps:
        mu_j = _to_mu(r_j)
        phi_j = _to_phi(rd_j)
        total += _g(phi_j) * (s_j - _E(mu, mu_j, phi_j))
    return v * total


def _f(x: float, delta: float, phi: float, v: float, a: float, tau: float) -> float:
    ex = math.exp(x)
    num = ex * (delta * delta - phi * phi - v - ex)
    den = 2.0 * ((phi * phi + v + ex) ** 2)
    return (num / den) - ((x - a) / (tau * tau))


def update_glicko2_player(player: Glicko2Player, tau: float = 0.5) -> None:
    """
    Batch-update a player for one rating period using standard Glicko-2.
    """
    mu = _to_mu(player.rating)
    phi = _to_phi(player.rd)
    sigma = player.sigma

    # No games this period: only RD increases due to inactivity.
    if not player.matches:
        phi_star = math.sqrt(phi * phi + sigma * sigma)
        player.rd = min(_to_rd(phi_star), 350.0)
        return

    v = _compute_v(mu, player.matches)
    delta = _compute_delta(mu, player.matches, v)

    a = math.log(sigma * sigma)
    eps = 1e-6

    A = a
    if delta * delta > phi * phi + v:
        B = math.log(delta * delta - phi * phi - v)
    else:
        k = 1
        while _f(a - k * tau, delta, phi, v, a, tau) < 0:
            k += 1
        B = a - k * tau

    fA = _f(A, delta, phi, v, a, tau)
    fB = _f(B, delta, phi, v, a, tau)

    while abs(B - A) > eps:
        C = A + (A - B) * fA / (fB - fA)
        fC = _f(C, delta, phi, v, a, tau)
        if fC * fB < 0:
            A = B
            fA = fB
        else:
            fA = fA / 2.0
        B = C
        fB = fC

    sigma_prime = math.exp(A / 2.0)

    phi_star = math.sqrt(phi * phi + sigma_prime * sigma_prime)
    phi_prime = 1.0 / math.sqrt((1.0 / (phi_star * phi_star)) + (1.0 / v))

    total = 0.0
    for r_j, rd_j, s_j in player.matches:
        mu_j = _to_mu(r_j)
        phi_j = _to_phi(rd_j)
        total += _g(phi_j) * (s_j - _E(mu, mu_j, phi_j))

    mu_prime = mu + (phi_prime * phi_prime) * total

    player.rating = _to_rating(mu_prime)
    player.rd = min(_to_rd(phi_prime), 350.0)
    player.sigma = sigma_prime


# =========================
# FILE / MEDIA HELPERS
# =========================

SUPPORTED_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp"}
SUPPORTED_VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}
SUPPORTED_EXTS = SUPPORTED_IMAGE_EXTS | SUPPORTED_VIDEO_EXTS
VIDEO_FRAME_CACHE: Dict[Tuple[Path, Tuple[int, int], int], List[Tuple[Image.Image, int]]] = {}
VIDEO_SOURCE_FRAME_CACHE: Dict[Tuple[Path, str], List[Tuple[Image.Image, int]]] = {}
# Per-key events used to deduplicate concurrent loads of the same video.
_FRAME_LOAD_EVENTS: Dict[Tuple, threading.Event] = {}
_FRAME_LOAD_EVENTS_LOCK = threading.Lock()
VIDEO_CACHE_DIR_NAME = ".g2cache"
VIDEO_CACHE_VERSION = 1
VIDEO_COMPILE_SIZE = (1200, 1200)
VIDEO_CACHE_MAX_FRAMES = 90
VIDEO_CACHE_TARGET_FPS = 14.0
VIDEO_PRELOAD_LOOKAHEAD = 8
DISPLAY_SIZE_BUCKET = 64
PHOTOIMAGE_BATCH_SIZE = 6
RENAME_RETRY_ATTEMPTS = 8
RENAME_RETRY_DELAY_SECONDS = 0.15

# Example prefix:
# "[G2_R1500.0_RD200.3_S0.0600] "
STATS_PREFIX_RE = re.compile(r"^\[G2_R(-?\d+(?:\.\d+)?)_RD(\d+(?:\.\d+)?)_S(\d+(?:\.\d+)?)\]\s*")


def strip_existing_prefix(stem: str) -> str:
    return STATS_PREFIX_RE.sub("", stem)


def format_prefix(player: Glicko2Player) -> str:
    return f"[G2_R{player.rating:.1f}_RD{player.rd:.1f}_S{player.sigma:.4f}] "


def player_from_path(path: Path) -> Glicko2Player:
    m = STATS_PREFIX_RE.match(path.stem)
    if not m:
        return Glicko2Player()
    rating = float(m.group(1))
    rd = float(m.group(2))
    sigma = float(m.group(3))
    return Glicko2Player(rating=rating, rd=rd, sigma=sigma)


def _rename_with_retry(src: Path, dst: Path) -> None:
    """
    Rename a file while tolerating brief Windows sharing violations
    (WinError 32) that can happen right after media playback stops.
    """
    last_exc: Exception | None = None
    for attempt in range(RENAME_RETRY_ATTEMPTS):
        try:
            src.rename(dst)
            return
        except PermissionError as exc:
            last_exc = exc
            if attempt == RENAME_RETRY_ATTEMPTS - 1:
                raise
            time.sleep(RENAME_RETRY_DELAY_SECONDS)
        except OSError as exc:
            last_exc = exc
            is_windows_lock = getattr(exc, "winerror", None) == 32
            if (not is_windows_lock) or attempt == RENAME_RETRY_ATTEMPTS - 1:
                raise
            time.sleep(RENAME_RETRY_DELAY_SECONDS)
    if last_exc is not None:
        raise last_exc


def load_images(folder: Path) -> List[Path]:
    files = [
        p
        for p in folder.iterdir()
        if p.name != VIDEO_CACHE_DIR_NAME and p.is_file() and p.suffix.lower() in SUPPORTED_EXTS
    ]
    return sorted(files, key=lambda p: p.name.lower())


def _center_on_canvas(img: Image.Image, canvas_size: Tuple[int, int]) -> Image.Image:
    canvas_w, canvas_h = canvas_size
    fitted = ImageOps.contain(img, (max(canvas_w - 20, 1), max(canvas_h - 20, 1)))
    canvas = Image.new("RGB", (canvas_w, canvas_h), color=(21, 21, 21))
    x = (canvas_w - fitted.width) // 2
    y = (canvas_h - fitted.height) // 2
    canvas.paste(fitted, (x, y))
    return canvas


def _bucket_dimension(value: int, bucket: int = DISPLAY_SIZE_BUCKET) -> int:
    if value <= 0:
        return bucket
    return max(bucket, int(round(value / bucket) * bucket))


def _bucket_size(size: Tuple[int, int], bucket: int = DISPLAY_SIZE_BUCKET) -> Tuple[int, int]:
    return (_bucket_dimension(size[0], bucket=bucket), _bucket_dimension(size[1], bucket=bucket))


def _load_gif_frames(path: Path, target_size: Tuple[int, int], max_frames: int = 240) -> List[Tuple[Image.Image, int]]:
    frames: List[Tuple[Image.Image, int]] = []
    with Image.open(path) as img:
        for index, frame in enumerate(ImageSequence.Iterator(img)):
            if index >= max_frames:
                break
            duration = int(frame.info.get("duration", img.info.get("duration", 100)) or 100)
            frame_rgb = frame.convert("RGB")
            frames.append((_center_on_canvas(frame_rgb, target_size), max(duration, 16)))
    return frames


def _load_video_frames(
    path: Path, target_size: Tuple[int, int], max_frames: int = 240, target_fps: float | None = None
) -> List[Tuple[Image.Image, int]]:
    plugin_candidates = ["pyav", "ffmpeg", None]
    frame_delay_ms = 41  # ~24 fps fallback; avoids an expensive metadata pass for every load.

    if iio is not None:
        for plugin in plugin_candidates:
            kwargs = {} if plugin is None else {"plugin": plugin}
            source_fps = 24.0
            frame_step = 1
            if target_fps and target_fps > 0:
                try:
                    with warnings.catch_warnings():
                        warnings.simplefilter("ignore")
                        meta = iio.immeta(path, **kwargs) or {}
                        source_fps = float(meta.get("fps", 24.0) or 24.0)
                except Exception:
                    source_fps = 24.0
                frame_step = max(int(round(source_fps / target_fps)), 1)

            frames: List[Tuple[Image.Image, int]] = []
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    for index, ndarray_frame in enumerate(iio.imiter(path, **kwargs)):
                        if frame_step > 1 and index % frame_step != 0:
                            continue
                        if len(frames) >= max_frames:
                            break
                        frame_img = Image.fromarray(ndarray_frame).convert("RGB")
                        frames.append((_center_on_canvas(frame_img, target_size), frame_delay_ms))
            except Exception:
                frames = []

            if frames:
                return frames

    # Windows-focused fallback: imageio.v2 ffmpeg reader handles many files that
    # fail with v3 plugin autodetection but still play in desktop players.
    if imageio is None:
        return []

    try:
        reader = imageio.get_reader(str(path), format="ffmpeg")
    except Exception:
        return []

    try:
        meta = reader.get_meta_data() or {}
        fps = float(meta.get("fps", 24) or 24)
        if fps <= 0:
            fps = 24.0
        if target_fps and target_fps > 0:
            frame_step = max(int(round(fps / target_fps)), 1)
            effective_fps = max(fps / frame_step, 1.0)
        else:
            frame_step = 1
            effective_fps = fps
        frame_delay_ms = max(int(1000 / effective_fps), 16)

        frames: List[Tuple[Image.Image, int]] = []
        for index, ndarray_frame in enumerate(reader):
            if frame_step > 1 and index % frame_step != 0:
                continue
            if len(frames) >= max_frames:
                break
            frame_img = Image.fromarray(ndarray_frame).convert("RGB")
            frames.append((_center_on_canvas(frame_img, target_size), frame_delay_ms))
        return frames
    except Exception:
        return []
    finally:
        try:
            reader.close()
        except Exception:
            pass


def _video_compile_settings(compile_size: Tuple[int, int] = VIDEO_COMPILE_SIZE) -> Dict[str, object]:
    return {
        "cache_version": VIDEO_CACHE_VERSION,
        "compile_size": [compile_size[0], compile_size[1]],
        "max_frames": VIDEO_CACHE_MAX_FRAMES,
        "target_fps": VIDEO_CACHE_TARGET_FPS,
    }


def _compute_video_cache_key(path: Path, compile_size: Tuple[int, int] = VIDEO_COMPILE_SIZE) -> str:
    stat = path.stat()
    payload = {
        "source_filename": path.name,
        "source_size": stat.st_size,
        "source_mtime_ns": stat.st_mtime_ns,
        "settings": _video_compile_settings(compile_size),
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()
    return digest[:16]


def _video_cache_dir(path: Path, compile_size: Tuple[int, int] = VIDEO_COMPILE_SIZE) -> Path:
    key = _compute_video_cache_key(path, compile_size=compile_size)
    cache_root = path.parent / VIDEO_CACHE_DIR_NAME
    safe_stem = re.sub(r"[^A-Za-z0-9._-]", "_", path.stem)[:80] or "video"
    return cache_root / f"{safe_stem}_{key}"


def _video_cache_meta_path(path: Path, compile_size: Tuple[int, int] = VIDEO_COMPILE_SIZE) -> Path:
    return _video_cache_dir(path, compile_size=compile_size) / "meta.json"


def _has_valid_video_cache(path: Path, compile_size: Tuple[int, int] = VIDEO_COMPILE_SIZE) -> bool:
    meta_path = _video_cache_meta_path(path, compile_size=compile_size)
    if not meta_path.exists():
        return False
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        return False

    delays = meta.get("frame_delays_ms")
    if (
        meta.get("cache_version") != VIDEO_CACHE_VERSION
        or meta.get("source_filename") != path.name
        or tuple(meta.get("compile_size", [])) != compile_size
        or not isinstance(delays, list)
        or meta.get("frame_count") != len(delays)
    ):
        return False

    frame_count = int(meta["frame_count"])
    if frame_count <= 0:
        return False
    cache_dir = meta_path.parent
    for idx in range(frame_count):
        if not (cache_dir / f"frame_{idx:04d}.png").exists():
            return False
    return True


def _compile_video_cache(path: Path, compile_size: Tuple[int, int] = VIDEO_COMPILE_SIZE) -> bool:
    cache_dir = _video_cache_dir(path, compile_size=compile_size)
    cache_dir.mkdir(parents=True, exist_ok=True)
    frames = _load_video_frames(
        path,
        compile_size,
        max_frames=VIDEO_CACHE_MAX_FRAMES,
        target_fps=VIDEO_CACHE_TARGET_FPS,
    )
    if not frames:
        return False

    for old_frame in cache_dir.glob("frame_*.png"):
        try:
            old_frame.unlink()
        except Exception:
            pass

    delays: List[int] = []
    for idx, (frame, delay) in enumerate(frames):
        frame.save(cache_dir / f"frame_{idx:04d}.png", format="PNG")
        delays.append(max(int(delay), 16))

    meta = {
        "source_filename": path.name,
        "frame_count": len(delays),
        "frame_delays_ms": delays,
        "compile_size": [compile_size[0], compile_size[1]],
        "cache_version": VIDEO_CACHE_VERSION,
        "cache_key": _compute_video_cache_key(path, compile_size=compile_size),
        "compile_settings": _video_compile_settings(compile_size),
    }
    (cache_dir / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    VIDEO_SOURCE_FRAME_CACHE[(path, meta["cache_key"])] = frames
    return True


def _load_video_frames_from_cache(
    path: Path, target_size: Tuple[int, int], compile_size: Tuple[int, int] = VIDEO_COMPILE_SIZE
) -> List[Tuple[Image.Image, int]]:
    meta_path = _video_cache_meta_path(path, compile_size=compile_size)
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        return []

    cache_key = meta.get("cache_key")
    delays_raw = meta.get("frame_delays_ms", [])
    if not cache_key or not isinstance(delays_raw, list):
        return []
    delays = [max(int(d), 16) for d in delays_raw]
    source_key = (path, cache_key)

    source_frames = VIDEO_SOURCE_FRAME_CACHE.get(source_key)
    if source_frames is None:
        source_frames = []
        frame_count = int(meta.get("frame_count", 0) or 0)
        cache_dir = meta_path.parent
        for idx in range(frame_count):
            frame_path = cache_dir / f"frame_{idx:04d}.png"
            try:
                with Image.open(frame_path) as img:
                    source_frames.append((img.convert("RGB").copy(), delays[idx]))
            except Exception:
                return []
        VIDEO_SOURCE_FRAME_CACHE[source_key] = source_frames

    return [(_center_on_canvas(frame, target_size), delay) for frame, delay in source_frames]


def load_media_frames(path: Path, target_size: Tuple[int, int], max_frames: int = 240) -> List[Tuple[Image.Image, int]]:
    suffix = path.suffix.lower()
    cache_key = (path, target_size, max_frames)
    cached = VIDEO_FRAME_CACHE.get(cache_key)
    if cached is not None:
        LOGGER.debug("Frame cache hit for %s key=%s", path.name, cache_key)
        return cached

    started_at = time.perf_counter()
    # Deduplication: if another thread is already loading the same key, wait for
    # it to finish instead of duplicating the work.  This lets the display thread
    # "inherit" an in-progress preload rather than restarting from scratch.
    with _FRAME_LOAD_EVENTS_LOCK:
        cached = VIDEO_FRAME_CACHE.get(cache_key)
        if cached is not None:
            LOGGER.debug("Frame cache hit during lock acquisition for %s", path.name)
            return cached
        existing = _FRAME_LOAD_EVENTS.get(cache_key)
        if existing is not None:
            wait_event = existing
            is_owner = False
            LOGGER.debug("Waiting for in-progress frame load for %s key=%s", path.name, cache_key)
        else:
            wait_event = threading.Event()
            _FRAME_LOAD_EVENTS[cache_key] = wait_event
            is_owner = True
            LOGGER.debug("Taking ownership of frame load for %s key=%s", path.name, cache_key)

    if not is_owner:
        wait_event.wait(timeout=30)
        waited = time.perf_counter() - started_at
        loaded = VIDEO_FRAME_CACHE.get(cache_key) or []
        LOGGER.debug(
            "Finished waiting for frame load: %s (%.3fs, frames=%d)",
            path.name,
            waited,
            len(loaded),
        )
        return loaded

    try:
        LOGGER.debug("Begin decode for %s suffix=%s target=%s", path.name, suffix, target_size)
        if suffix == ".gif":
            frames = _load_gif_frames(path, target_size, max_frames=max_frames)
            if frames:
                VIDEO_FRAME_CACHE[cache_key] = frames
                LOGGER.debug("Decoded GIF frames for %s frame_count=%d", path.name, len(frames))
                return frames
        elif suffix in SUPPORTED_VIDEO_EXTS:
            # Decode directly from source when VLC playback is unavailable.
            video_max_frames = 72 if suffix == ".webm" else max_frames
            frames = _load_video_frames(path, target_size, max_frames=video_max_frames)
            if frames:
                VIDEO_FRAME_CACHE[cache_key] = frames
                LOGGER.debug("Decoded video frames for %s frame_count=%d", path.name, len(frames))
                return frames

        with Image.open(path) as img:
            fallback_frames = [(_center_on_canvas(img.convert("RGB"), target_size), 100)]
            VIDEO_FRAME_CACHE[cache_key] = fallback_frames
            LOGGER.debug("Using still-image fallback for %s", path.name)
            return fallback_frames
    except Exception:
        fallback = Image.new("RGB", target_size, color=(30, 30, 30))
        fallback_frames = [(fallback, 100)]
        VIDEO_FRAME_CACHE[cache_key] = fallback_frames
        LOGGER.debug("Frame decode failed for %s. Using blank fallback frame.", path, exc_info=True)
        return fallback_frames
    finally:
        with _FRAME_LOAD_EVENTS_LOCK:
            _FRAME_LOAD_EVENTS.pop(cache_key, None)
        wait_event.set()
        LOGGER.debug("Frame load finalized for %s in %.3fs", path.name, time.perf_counter() - started_at)


def build_random_pairs_once(paths: List[Path]) -> List[Tuple[Path, Path]]:
    shuffled = paths[:]
    random.shuffle(shuffled)
    pairs: List[Tuple[Path, Path]] = []
    for i in range(0, len(shuffled) - 1, 2):
        pairs.append((shuffled[i], shuffled[i + 1]))
    return pairs


def shorten_name(name: str, max_len: int = 80) -> str:
    if len(name) <= max_len:
        return name
    keep = max_len - 3
    left = keep // 2
    right = keep - left
    return name[:left] + "..." + name[-right:]


# =========================
# UI APPLICATION
# =========================

class ImageRankerApp:
    def __init__(self, master: tk.Tk):
        self.master = master
        self.master.title("Image Glicko-2 Ranker")
        self.master.geometry("1400x800")
        self.master.minsize(1000, 650)
        self.master.configure(bg="#202020")

        self.folder: Path | None = None
        self.image_paths: List[Path] = []
        self.players: Dict[Path, Glicko2Player] = {}
        self.pairs: List[Tuple[Path, Path]] = []
        self.current_index = 0
        self.history: List[Tuple[Path, Path]] = []

        self.left_photo = None
        self.right_photo = None
        self._vlc_enabled = VLC_AVAILABLE
        self._vlc_instance = self._create_vlc_instance() if VLC_AVAILABLE else None
        self.left_vlc_player = None
        self.right_vlc_player = None
        self.left_vlc_media = None
        self.right_vlc_media = None
        self.left_animation_after_id = None
        self.right_animation_after_id = None
        self.left_animation_frames: List[ImageTk.PhotoImage] = []
        self.right_animation_frames: List[ImageTk.PhotoImage] = []
        self.left_animation_delays: List[int] = []
        self.right_animation_delays: List[int] = []
        self.left_animation_index = 0
        self.right_animation_index = 0
        self._resize_after_id = None
        self._last_root_size = (self.master.winfo_width(), self.master.winfo_height())
        self._load_generation: Dict[str, int] = {"left": 0, "right": 0}
        self._vlc_probe_tokens: Dict[str, int] = {"left": 0, "right": 0}
        self._vlc_media_mrl: Dict[str, str | None] = {"left": None, "right": None}
        self._ui_heartbeat_after_id = None
        self._ui_heartbeat_counter = 0
        self._ui_heartbeat_timestamp = time.perf_counter()
        self._watchdog_stop = threading.Event()
        self._main_thread_ident = threading.get_ident()
        self._ui_phase_lock = threading.Lock()
        self._ui_phase = "startup"
        self._ui_phase_started_at = time.perf_counter()
        self._ui_phase_seq = 0
        self._ui_phase_history = deque(maxlen=UI_PHASE_HISTORY_LIMIT)
        self._preload_lock = threading.Lock()
        self._preload_generation = 0
        self._set_ui_phase("app-init")
        self._start_ui_watchdog()

        self.top_bar = tk.Frame(self.master, bg="#202020")
        self.top_bar.pack(side=tk.TOP, fill=tk.X, padx=10, pady=10)

        self.btn_choose = tk.Button(
            self.top_bar, text="Choose Folder", command=self.choose_folder, font=("Segoe UI", 11)
        )
        self.btn_choose.pack(side=tk.LEFT, padx=(0, 8))

        self.status_var = tk.StringVar(value="Choose a folder with images.")
        self.status_label = tk.Label(
            self.top_bar, textvariable=self.status_var, fg="white", bg="#202020", font=("Segoe UI", 11)
        )
        self.status_label.pack(side=tk.LEFT, padx=8)

        self.progress_var = tk.StringVar(value="")
        self.progress_label = tk.Label(
            self.top_bar, textvariable=self.progress_var, fg="#cccccc", bg="#202020", font=("Segoe UI", 10)
        )
        self.progress_label.pack(side=tk.RIGHT, padx=8)

        self.main_frame = tk.Frame(self.master, bg="#202020")
        self.main_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        self.main_frame.grid_columnconfigure(0, weight=1, uniform="panels")
        self.main_frame.grid_columnconfigure(1, weight=1, uniform="panels")
        self.main_frame.grid_rowconfigure(0, weight=1)

        self.left_panel = tk.Frame(self.main_frame, bg="#151515", bd=1, relief=tk.FLAT)
        self.left_panel.grid(row=0, column=0, sticky="nsew", padx=(0, 5))

        self.right_panel = tk.Frame(self.main_frame, bg="#151515", bd=1, relief=tk.FLAT)
        self.right_panel.grid(row=0, column=1, sticky="nsew", padx=(5, 0))

        self.left_label = tk.Label(
            self.left_panel,
            bg="#151515",
            fg="white",
            text="Left image",
            font=("Segoe UI", 12),
            anchor="w",
            justify="left",
            wraplength=550,
        )
        self.left_label.pack(side=tk.TOP, fill=tk.X, padx=10, pady=8)

        self.right_label = tk.Label(
            self.right_panel,
            bg="#151515",
            fg="white",
            text="Right image",
            font=("Segoe UI", 12),
            anchor="w",
            justify="left",
            wraplength=550,
        )
        self.right_label.pack(side=tk.TOP, fill=tk.X, padx=10, pady=8)

        self.left_media_frame = tk.Frame(self.left_panel, bg="#151515", cursor="hand2")
        self.left_media_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        self.left_media_frame.bind("<Button-1>", lambda e: self.pick_winner("left"))
        self.left_image_label = tk.Label(self.left_media_frame, bg="#151515", cursor="hand2")
        self.left_image_label.pack(fill=tk.BOTH, expand=True)
        self.left_image_label.bind("<Button-1>", lambda e: self.pick_winner("left"))

        self.right_media_frame = tk.Frame(self.right_panel, bg="#151515", cursor="hand2")
        self.right_media_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        self.right_media_frame.bind("<Button-1>", lambda e: self.pick_winner("right"))
        self.right_image_label = tk.Label(self.right_media_frame, bg="#151515", cursor="hand2")
        self.right_image_label.pack(fill=tk.BOTH, expand=True)
        self.right_image_label.bind("<Button-1>", lambda e: self.pick_winner("right"))

        self.bottom_bar = tk.Frame(self.master, bg="#202020")
        self.bottom_bar.pack(side=tk.BOTTOM, fill=tk.X, padx=10, pady=(0, 10))

        self.draw_button = tk.Button(
            self.bottom_bar,
            text="Draw / Tie",
            command=self.record_draw,
            font=("Segoe UI", 10),
        )
        self.draw_button.pack(side=tk.RIGHT, padx=(8, 0))

        self.help_label = tk.Label(
            self.bottom_bar,
            text="Left/Right arrows choose winner. Up = draw. Down = undo. Mouse click still works. Each image appears at most once per session. Esc exits.",
            fg="#cccccc",
            bg="#202020",
            font=("Segoe UI", 10),
        )
        self.help_label.pack(side=tk.LEFT)

        self.master.bind("<Escape>", lambda e: self.master.destroy())
        self.master.bind("<Left>", lambda e: self.pick_winner("left"))
        self.master.bind("<Right>", lambda e: self.pick_winner("right"))
        self.master.bind("<Up>", lambda e: self.record_draw())
        self.master.bind("<Down>", lambda e: self.undo_last())
        self.master.bind("<Configure>", self._on_resize)
        self.master.protocol("WM_DELETE_WINDOW", self._on_close)

    def _create_vlc_instance(self):
        if not self._vlc_enabled:
            return None
        instance_options = list(VLC_INSTANCE_OPTIONS)
        if sys.platform.startswith("win"):
            # Direct3D11 output is known to be unstable on some Windows setups
            # when embedding VLC into GUI widgets and rapidly swapping media.
            # Prefer DirectDraw and disable hardware decode to reduce lockups.
            instance_options.extend(
                [
                    "--vout=directdraw",
                    "--avcodec-hw=none",
                ]
            )
        try:
            return vlc.Instance(*instance_options)
        except Exception:
            return None

    def _on_close(self) -> None:
        self._watchdog_stop.set()
        self._invalidate_vlc_play_probes("left")
        self._invalidate_vlc_play_probes("right")
        if self._ui_heartbeat_after_id is not None:
            self.master.after_cancel(self._ui_heartbeat_after_id)
            self._ui_heartbeat_after_id = None
        self._stop_animation("left")
        self._stop_animation("right")
        self._stop_vlc("left")
        self._stop_vlc("right")
        self.master.destroy()

    def build_session_pairs(self, image_paths: List[Path]) -> List[Tuple[Path, Path]]:
        return build_random_pairs_once(image_paths)

    def choose_folder(self) -> None:
        folder_str = filedialog.askdirectory(title="Choose image folder")
        if not folder_str:
            return

        folder = Path(folder_str)
        image_paths = load_images(folder)

        if len(image_paths) < 2:
            messagebox.showerror("Not enough images", "Need at least 2 supported image files.")
            return

        self.folder = folder
        VIDEO_FRAME_CACHE.clear()
        VIDEO_SOURCE_FRAME_CACHE.clear()
        self.image_paths = image_paths
        self.players = {p: player_from_path(p) for p in image_paths}
        self.pairs = self.build_session_pairs(image_paths)
        self.current_index = 0
        self.history = []

        self.status_var.set(f"Loaded {len(self.image_paths)} images from: {self.folder}")
        self._update_progress()
        self.show_current_pair()

    def _update_progress(self) -> None:
        total_pairs = len(self.pairs)
        done = self.current_index
        images_this_session = total_pairs * 2
        if self.image_paths and len(self.image_paths) % 2 == 1:
            images_this_session += 1
        self.progress_var.set(
            f"Compared: {done}/{total_pairs} | Images in session: {images_this_session}/{len(self.image_paths)}"
        )

    def _on_resize(self, event) -> None:
        # Redraw only when the root window itself changes size.
        if event.widget is not self.master:
            return

        new_size = (event.width, event.height)
        if new_size == self._last_root_size:
            return
        self._last_root_size = new_size

        if self._resize_after_id is not None:
            self.master.after_cancel(self._resize_after_id)

        self._resize_after_id = self.master.after(120, self._redraw_current_pair)

    def _redraw_current_pair(self) -> None:
        self._resize_after_id = None
        if self.pairs and self.current_index < len(self.pairs):
            self.show_current_pair(redraw_only=True)

    def show_current_pair(self, redraw_only: bool = False) -> None:
        if not self.pairs or self.current_index >= len(self.pairs):
            if not redraw_only:
                self.finish_session()
            return

        left_path, right_path = self.pairs[self.current_index]
        self._set_ui_phase(
            "show-current-pair",
            index=self.current_index + 1,
            redraw_only=redraw_only,
            left=left_path.name,
            right=right_path.name,
        )
        LOGGER.debug(
            "Showing pair index=%d/%d redraw_only=%s left=%s right=%s",
            self.current_index + 1,
            len(self.pairs),
            redraw_only,
            left_path.name,
            right_path.name,
        )

        panel_width = max(self.main_frame.winfo_width() // 2 - 40, 250)
        self.left_label.config(wraplength=panel_width, text=f"Left: {shorten_name(left_path.name, 80)}")
        self.right_label.config(wraplength=panel_width, text=f"Right: {shorten_name(right_path.name, 80)}")

        self._set_media_on_label(left_path, self.left_image_label, side="left")
        self._set_media_on_label(right_path, self.right_image_label, side="right")

        self._update_progress()
        self._start_preload_upcoming_videos()

    def _upcoming_video_paths(self, lookahead: int = VIDEO_PRELOAD_LOOKAHEAD) -> List[Path]:
        if not self.pairs or self.current_index >= len(self.pairs) or lookahead <= 0:
            return []

        ordered: List[Path] = []
        seen = set()
        end_index = min(self.current_index + lookahead, len(self.pairs))
        for pair_index in range(self.current_index, end_index):
            left_path, right_path = self.pairs[pair_index]
            for candidate in (left_path, right_path):
                if candidate.suffix.lower() not in SUPPORTED_VIDEO_EXTS:
                    continue
                if candidate in seen:
                    continue
                seen.add(candidate)
                ordered.append(candidate)
        return ordered

    def _start_preload_upcoming_videos(self, lookahead: int = VIDEO_PRELOAD_LOOKAHEAD) -> None:
        if self._vlc_enabled:
            # VLC is the active playback path. Avoid parallel software decoding
            # of upcoming videos, which can contend with VLC's VP8 decode on
            # Windows and cause stalls during rapid media switches.
            return
        if not self.folder or not self.pairs:
            return
        self._preload_generation += 1
        generation = self._preload_generation

        # Skip the current pair — its videos are already being loaded by the
        # display threads.  Start immediately on pair N+1 so it has the most
        # time to finish before the user advances.
        seen: set = set()
        upcoming: List[List[Path]] = []
        start = self.current_index + 1
        end = min(start + lookahead, len(self.pairs))
        for pair_index in range(start, end):
            left_path, right_path = self.pairs[pair_index]
            pair_videos = [
                p for p in (left_path, right_path)
                if p.suffix.lower() in SUPPORTED_VIDEO_EXTS and p not in seen
            ]
            for p in pair_videos:
                seen.add(p)
            if pair_videos:
                upcoming.append(pair_videos)

        if not upcoming:
            return
        LOGGER.debug(
            "Queueing preload generation=%d lookahead=%d upcoming_pairs=%d",
            generation,
            lookahead,
            len(upcoming),
        )

        def worker() -> None:
            for pair_videos in upcoming:
                if generation != self._preload_generation:
                    LOGGER.debug("Cancelling stale preload generation=%d", generation)
                    return
                # Keep decode work serial to avoid decoder lockups observed on
                # some systems when multiple WebM files are decoded at once.
                LOGGER.debug("Preloading pair videos generation=%d files=%s", generation, [p.name for p in pair_videos])
                for path in pair_videos:
                    if generation != self._preload_generation:
                        LOGGER.debug("Cancelling stale preload generation=%d", generation)
                        return
                    self._preload_video_to_ram(path)

        threading.Thread(target=worker, daemon=True).start()

    def _preload_video_to_ram(self, path: Path) -> None:
        if path.suffix.lower() not in SUPPORTED_VIDEO_EXTS:
            return
        target_size = self._current_preload_target_size()
        source_key = (path, target_size)
        with self._preload_lock:
            if source_key in VIDEO_SOURCE_FRAME_CACHE:
                LOGGER.debug("Skipping preload for %s (already in source cache)", path.name)
                return

        start = time.perf_counter()
        LOGGER.debug("Starting preload for %s target=%s", path.name, target_size)
        load_media_frames(path, target_size)
        with self._preload_lock:
            VIDEO_SOURCE_FRAME_CACHE[source_key] = []
        LOGGER.debug("Finished preload for %s in %.3fs", path.name, time.perf_counter() - start)

    def _current_preload_target_size(self) -> Tuple[int, int]:
        # Must match the formula in _set_media_on_label exactly so the preloaded
        # frames land in the same VIDEO_FRAME_CACHE bucket that display looks up.
        w = max(self.left_image_label.winfo_width(), 200)
        h = max(self.left_image_label.winfo_height(), 200)
        return _bucket_size((w, h))

    def _stop_animation(self, side: str) -> None:
        if side == "left":
            if self.left_animation_after_id is not None:
                self.master.after_cancel(self.left_animation_after_id)
                self.left_animation_after_id = None
            self.left_animation_frames = []
            self.left_animation_delays = []
            self.left_animation_index = 0
        else:
            if self.right_animation_after_id is not None:
                self.master.after_cancel(self.right_animation_after_id)
                self.right_animation_after_id = None
            self.right_animation_frames = []
            self.right_animation_delays = []
            self.right_animation_index = 0

    def _media_frame_for_side(self, side: str) -> tk.Frame:
        return self.left_media_frame if side == "left" else self.right_media_frame

    def _vlc_player_for_side(self, side: str):
        return self.left_vlc_player if side == "left" else self.right_vlc_player

    def _set_vlc_player_for_side(self, side: str, player) -> None:
        if side == "left":
            self.left_vlc_player = player
        else:
            self.right_vlc_player = player

    def _set_vlc_media_for_side(self, side: str, media) -> None:
        if side == "left":
            self.left_vlc_media = media
        else:
            self.right_vlc_media = media
        media_mrl = None
        if media is not None:
            try:
                media_mrl = media.get_mrl()
            except Exception:
                media_mrl = None
        self._vlc_media_mrl[side] = media_mrl

    def _set_ui_phase(self, phase: str, **details: object) -> None:
        detail_parts = [f"{key}={details[key]}" for key in sorted(details)]
        now = time.perf_counter()
        thread_name = threading.current_thread().name
        with self._ui_phase_lock:
            previous_phase = self._ui_phase
            previous_started_at = self._ui_phase_started_at
            self._ui_phase = phase
            self._ui_phase_started_at = now
            self._ui_phase_seq += 1
            entry = f"{self._ui_phase_seq}:{phase} thread={thread_name}"
            if detail_parts:
                entry += " " + " ".join(detail_parts)
            self._ui_phase_history.append(entry)
        if LOGGER.isEnabledFor(logging.DEBUG):
            LOGGER.debug(
                "UI phase seq=%d phase=%s prev=%s prev_age=%.3fs %s",
                self._ui_phase_seq,
                phase,
                previous_phase,
                now - previous_started_at,
                " ".join(detail_parts),
            )

    def _dump_main_thread_state(self, reason: str) -> None:
        with self._ui_phase_lock:
            current_phase = self._ui_phase
            phase_age = time.perf_counter() - self._ui_phase_started_at
            history = list(self._ui_phase_history)
        LOGGER.warning(
            "Main thread dump reason=%s phase=%s phase_age=%.3fs pair_index=%d total_pairs=%d load_generation=%s vlc_media_left=%s vlc_media_right=%s",
            reason,
            current_phase,
            phase_age,
            self.current_index,
            len(self.pairs),
            dict(self._load_generation),
            self._vlc_media_mrl["left"],
            self._vlc_media_mrl["right"],
        )
        for entry in history[-10:]:
            LOGGER.warning("UI phase history %s", entry)
        frame = sys._current_frames().get(self._main_thread_ident)
        if frame is None:
            LOGGER.warning("Main thread stack unavailable reason=%s", reason)
            return
        for line in "".join(traceback.format_stack(frame)).rstrip().splitlines():
            LOGGER.warning("Main thread stack %s", line)

    def _run_mainthread_call_with_watchdog(self, label: str, func):
        if threading.get_ident() != self._main_thread_ident or not LOGGER.isEnabledFor(logging.DEBUG):
            return func()

        self._set_ui_phase("mainthread-call-start", label=label)
        started = time.perf_counter()
        stop_event = threading.Event()

        def monitor() -> None:
            last_delay = 0.0
            for delay in MAINTHREAD_CALL_WATCHDOG_DELAYS_SECONDS:
                if stop_event.wait(delay - last_delay):
                    return
                LOGGER.warning("Main-thread call blocked label=%s elapsed=%.3fs", label, delay)
                self._dump_main_thread_state(f"blocked-call:{label}")
                last_delay = delay

        threading.Thread(target=monitor, name="MainThreadCallWatchdog", daemon=True).start()
        try:
            return func()
        finally:
            stop_event.set()
            self._set_ui_phase(
                "mainthread-call-end",
                label=label,
                elapsed_ms=int((time.perf_counter() - started) * 1000),
            )

    def _tick_ui_heartbeat(self) -> None:
        if self._watchdog_stop.is_set():
            self._ui_heartbeat_after_id = None
            return
        self._ui_heartbeat_counter += 1
        self._ui_heartbeat_timestamp = time.perf_counter()
        self._ui_heartbeat_after_id = self.master.after(UI_HEARTBEAT_INTERVAL_MS, self._tick_ui_heartbeat)

    def _start_ui_watchdog(self) -> None:
        self._tick_ui_heartbeat()

        def watchdog_loop() -> None:
            stale_reported = False
            while not self._watchdog_stop.wait(UI_WATCHDOG_POLL_SECONDS):
                age = time.perf_counter() - self._ui_heartbeat_timestamp
                if age >= UI_HEARTBEAT_STALE_SECONDS:
                    if not stale_reported:
                        self._set_ui_phase("ui-heartbeat-stalled", age_ms=int(age * 1000))
                        LOGGER.warning(
                            "UI heartbeat stalled age=%.3fs counter=%d",
                            age,
                            self._ui_heartbeat_counter,
                        )
                        self._dump_main_thread_state("ui-heartbeat-stalled")
                        stale_reported = True
                elif stale_reported:
                    self._set_ui_phase("ui-heartbeat-recovered", age_ms=int(age * 1000))
                    LOGGER.debug(
                        "UI heartbeat recovered age=%.3fs counter=%d",
                        age,
                        self._ui_heartbeat_counter,
                    )
                    stale_reported = False

        threading.Thread(target=watchdog_loop, name="UIWatchdog", daemon=True).start()

    def _invalidate_vlc_play_probes(self, side: str) -> int:
        self._vlc_probe_tokens[side] += 1
        return self._vlc_probe_tokens[side]

    def _log_vlc_event(self, side: str, event_name: str) -> None:
        if not LOGGER.isEnabledFor(logging.DEBUG):
            return
        LOGGER.debug(
            "VLC event side=%s event=%s thread=%s media=%s",
            side,
            event_name,
            threading.current_thread().name,
            self._vlc_media_mrl[side],
        )

    def _schedule_vlc_play_probes(self, side: str, path: Path, load_generation: int) -> None:
        probe_token = self._invalidate_vlc_play_probes(side)
        started = time.perf_counter()
        for delay_seconds in VLC_PLAY_PROBE_DELAYS_MS:
            def probe(delay_seconds: float = delay_seconds, token: int = probe_token) -> None:
                time.sleep(delay_seconds)
                if token != self._vlc_probe_tokens[side]:
                    LOGGER.debug(
                        "Skipping VLC probe side=%s file=%s delay_ms=%d stale_token=%d current=%d",
                        side,
                        path.name,
                        int(delay_seconds * 1000),
                        token,
                        self._vlc_probe_tokens[side],
                    )
                    return
                if load_generation != self._load_generation[side]:
                    LOGGER.debug(
                        "Skipping VLC probe side=%s file=%s delay_ms=%d stale_generation=%d current=%d",
                        side,
                        path.name,
                        int(delay_seconds * 1000),
                        load_generation,
                        self._load_generation[side],
                    )
                    return
                self._log_vlc_state(
                    side,
                    f"probe delay_ms={int(delay_seconds * 1000)} elapsed={time.perf_counter() - started:.3f}s file={path.name}",
                )

            threading.Thread(target=probe, name=f"VLCProbe-{side}", daemon=True).start()

    def _log_vlc_state(self, side: str, prefix: str) -> None:
        if not LOGGER.isEnabledFor(logging.DEBUG):
            return
        player = self._vlc_player_for_side(side)
        if player is None:
            LOGGER.debug("VLC state side=%s %s player=None", side, prefix)
            return
        try:
            state_obj = player.get_state()
            state_val = int(state_obj)
            state_name = VLC_STATE_NAMES.get(state_val, str(state_obj))
        except Exception:
            state_name = "unknown"
        try:
            media = player.get_media()
            media_mrl = media.get_mrl() if media is not None else None
        except Exception:
            media_mrl = None
        try:
            is_playing = bool(player.is_playing())
        except Exception:
            is_playing = False
        LOGGER.debug(
            "VLC state side=%s %s state=%s playing=%s media=%s",
            side,
            prefix,
            state_name,
            is_playing,
            media_mrl,
        )

    def _ensure_vlc_player(self, side: str):
        """Create a fresh VLC player for the given side.

        A new player is created on every call so that the previous player can be
        stopped safely in a background thread without racing against a new play()
        call on the same object.  The old player's set_hwnd(0) + stop() are
        offloaded to a daemon thread so the main thread never blocks.
        """
        if not self._vlc_instance:
            return None

        old_player = self._vlc_player_for_side(side)
        if old_player is not None:
            def _stop_old(p=old_player, s=side) -> None:
                try:
                    if sys.platform.startswith("win"):
                        try:
                            p.set_hwnd(0)
                        except Exception:
                            pass
                    p.stop()
                    LOGGER.debug("Async old-player stop complete side=%s", s)
                except Exception:
                    LOGGER.debug("Async old-player stop error side=%s", s, exc_info=True)
            threading.Thread(target=_stop_old, name=f"VLCStop-{side}", daemon=True).start()

        player = self._vlc_instance.media_player_new()
        em = player.event_manager()
        def _event_log(event_name: str):
            return lambda _event, s=side, n=event_name: self._log_vlc_event(s, n)
        # Looping is handled by the :input-repeat media option; no EndReached
        # callback needed (and no stop() inside _restart_vlc to block on).
        em.event_attach(vlc.EventType.MediaPlayerEncounteredError, _event_log("EncounteredError"))
        em.event_attach(vlc.EventType.MediaPlayerOpening, _event_log("Opening"))
        em.event_attach(vlc.EventType.MediaPlayerBuffering, _event_log("Buffering"))
        em.event_attach(vlc.EventType.MediaPlayerPlaying, _event_log("Playing"))
        em.event_attach(vlc.EventType.MediaPlayerPaused, _event_log("Paused"))
        self._set_vlc_player_for_side(side, player)
        return player

    def _bind_vlc_to_widget(self, player, widget: tk.Widget) -> None:
        self._run_mainthread_call_with_watchdog(
            "widget.update_idletasks bind-vlc",
            widget.update_idletasks,
        )
        handle = widget.winfo_id()
        if sys.platform.startswith("win"):
            self._run_mainthread_call_with_watchdog(
                f"player.set_hwnd handle={handle}",
                lambda: player.set_hwnd(handle),
            )
        elif sys.platform == "darwin":
            self._run_mainthread_call_with_watchdog(
                f"player.set_nsobject handle={handle}",
                lambda: player.set_nsobject(handle),
            )
        else:
            self._run_mainthread_call_with_watchdog(
                f"player.set_xwindow handle={handle}",
                lambda: player.set_xwindow(handle),
            )

    def _stop_vlc(self, side: str) -> None:
        """Asynchronously stop the VLC player for a side.

        player.stop() maps to libvlc_media_player_stop() which blocks the
        calling thread on Windows until the decoder drains.  Moving it off the
        main thread prevents UI freezes during pair transitions.
        """
        self._invalidate_vlc_play_probes(side)
        player = self._vlc_player_for_side(side)
        self._set_vlc_player_for_side(side, None)
        self._set_vlc_media_for_side(side, None)
        if player is not None:
            LOGGER.debug("Queueing async VLC stop side=%s", side)
            def _do_stop(p=player, s=side) -> None:
                try:
                    if sys.platform.startswith("win"):
                        try:
                            p.set_hwnd(0)
                        except Exception:
                            pass
                    p.stop()
                    LOGGER.debug("Async VLC stop complete side=%s", s)
                except Exception:
                    LOGGER.debug("Async VLC stop error side=%s", s, exc_info=True)
            threading.Thread(target=_do_stop, name=f"VLCStop-{side}", daemon=True).start()

    def _play_video_on_frame(self, path: Path, side: str, load_generation: int) -> bool:
        if not self._vlc_enabled or self._vlc_instance is None:
            return False

        media_frame = self._media_frame_for_side(side)
        player = self._ensure_vlc_player(side)
        if player is None:
            return False

        self._set_ui_phase("play-vlc", side=side, file=path.name, load_generation=load_generation)
        LOGGER.debug("Starting VLC playback side=%s file=%s", side, path.name)
        self.left_image_label.lower() if side == "left" else self.right_image_label.lower()
        self._bind_vlc_to_widget(player, media_frame)
        media = self._vlc_instance.media_new_path(os.fspath(path))
        media.add_option(":no-audio")
        media.add_option(":input-repeat=65535")  # loop natively — no Python stop()/play() needed
        self._run_mainthread_call_with_watchdog(
            f"vlc.set_media side={side} file={path.name}",
            lambda: player.set_media(media),
        )
        self._set_vlc_media_for_side(side, media)
        player.audio_set_mute(True)
        play_code = self._run_mainthread_call_with_watchdog(
            f"vlc.play side={side} file={path.name}",
            player.play,
        )
        LOGGER.debug(
            "VLC play() returned side=%s file=%s code=%s media=%s",
            side,
            path.name,
            play_code,
            self._vlc_media_mrl[side],
        )
        self._schedule_vlc_play_probes(side, path, load_generation)
        return True

    def _advance_animation(self, side: str) -> None:
        if side == "left":
            frames = self.left_animation_frames
            delays = self.left_animation_delays
            if not frames:
                return
            self.left_animation_index = (self.left_animation_index + 1) % len(frames)
            self.left_image_label.config(image=frames[self.left_animation_index])
            delay = delays[self.left_animation_index]
            self.left_animation_after_id = self.master.after(delay, lambda: self._advance_animation("left"))
        else:
            frames = self.right_animation_frames
            delays = self.right_animation_delays
            if not frames:
                return
            self.right_animation_index = (self.right_animation_index + 1) % len(frames)
            self.right_image_label.config(image=frames[self.right_animation_index])
            delay = delays[self.right_animation_index]
            self.right_animation_after_id = self.master.after(delay, lambda: self._advance_animation("right"))

    def _set_media_on_label(self, path: Path, widget: tk.Label, side: str) -> None:
        self._set_ui_phase("set-media-start", side=side, file=path.name)
        self._run_mainthread_call_with_watchdog(
            f"widget.update_idletasks side={side}",
            widget.update_idletasks,
        )
        w = max(widget.winfo_width(), 200)
        h = max(widget.winfo_height(), 200)
        target_size = _bucket_size((w, h))
        LOGGER.debug("Setting media side=%s file=%s widget=%sx%s target=%s", side, path.name, w, h, target_size)
        self._stop_animation(side)
        # Do NOT call _stop_vlc here — that would block the main thread on
        # libvlc_media_player_stop().  For video→video transitions, the old
        # player is stopped asynchronously inside _ensure_vlc_player (called
        # from _play_video_on_frame).  For video→image transitions, _stop_vlc
        # is called below after the image branch is taken (it is async).
        self._load_generation[side] += 1
        load_generation = self._load_generation[side]
        self._set_ui_phase(
            "set-media-after-stop",
            side=side,
            file=path.name,
            load_generation=load_generation,
            target=target_size,
        )

        placeholder = Image.new("RGB", (w, h), color=(24, 24, 24))
        placeholder_photo = ImageTk.PhotoImage(placeholder)
        widget.config(image=placeholder_photo)
        if side == "left":
            self.left_photo = placeholder_photo
        else:
            self.right_photo = placeholder_photo
        widget.lift()

        if path.suffix.lower() in SUPPORTED_VIDEO_EXTS and self._play_video_on_frame(path, side, load_generation):
            return

        # Image / GIF path — stop any VLC player that was running on this side.
        # _stop_vlc is now async so this returns immediately.
        self._stop_vlc(side)

        def worker() -> None:
            started = time.perf_counter()
            frame_data = load_media_frames(path, target_size)
            LOGGER.debug(
                "Decoded frame_data side=%s file=%s frames=%d elapsed=%.3fs",
                side,
                path.name,
                len(frame_data),
                time.perf_counter() - started,
            )

            def apply_result() -> None:
                if load_generation != self._load_generation[side]:
                    LOGGER.debug(
                        "Dropping stale media update side=%s file=%s load_generation=%d current=%d",
                        side,
                        path.name,
                        load_generation,
                        self._load_generation[side],
                    )
                    return

                if not frame_data:
                    LOGGER.debug("No frame data returned side=%s file=%s", side, path.name)
                    return

                first_photo = ImageTk.PhotoImage(frame_data[0][0])
                photos = [first_photo]
                delays = [frame_data[0][1]]

                if side == "left":
                    self.left_photo = first_photo
                    self.left_animation_frames = photos
                    self.left_animation_delays = delays
                    self.left_animation_index = 0
                else:
                    self.right_photo = first_photo
                    self.right_animation_frames = photos
                    self.right_animation_delays = delays
                    self.right_animation_index = 0

                widget.config(image=first_photo)
                if len(frame_data) <= 1:
                    return

                animation_started = False

                def convert_batch(start_idx: int) -> None:
                    nonlocal animation_started
                    if load_generation != self._load_generation[side]:
                        return

                    end_idx = min(start_idx + PHOTOIMAGE_BATCH_SIZE, len(frame_data))
                    for idx in range(start_idx, end_idx):
                        frame, delay = frame_data[idx]
                        photos.append(ImageTk.PhotoImage(frame))
                        delays.append(delay)

                    if not animation_started and len(photos) > 1:
                        animation_started = True
                        if side == "left":
                            self.left_animation_after_id = self.master.after(
                                delays[0], lambda: self._advance_animation("left")
                            )
                        else:
                            self.right_animation_after_id = self.master.after(
                                delays[0], lambda: self._advance_animation("right")
                            )

                    if end_idx < len(frame_data):
                        self.master.after(1, lambda: convert_batch(end_idx))

                self.master.after(1, lambda: convert_batch(1))

            self.master.after(0, apply_result)

        threading.Thread(target=worker, daemon=True).start()

    def pick_winner(self, side: str) -> None:
        if not self.pairs or self.current_index >= len(self.pairs):
            return

        left_path, right_path = self.pairs[self.current_index]
        self._set_ui_phase("pick-winner", side=side, left=left_path.name, right=right_path.name)
        left_player = self.players[left_path]
        right_player = self.players[right_path]

        if side == "left":
            left_player.add_result(right_player.rating, right_player.rd, 1.0)
            right_player.add_result(left_player.rating, left_player.rd, 0.0)
        elif side == "right":
            left_player.add_result(right_player.rating, right_player.rd, 0.0)
            right_player.add_result(left_player.rating, left_player.rd, 1.0)
        else:
            return

        self.history.append((left_path, right_path))
        self.current_index += 1
        if self.current_index >= len(self.pairs):
            self.finish_session()
        else:
            self.show_current_pair()

    def record_draw(self) -> None:
        if not self.pairs or self.current_index >= len(self.pairs):
            return

        left_path, right_path = self.pairs[self.current_index]
        self._set_ui_phase("record-draw", left=left_path.name, right=right_path.name)
        left_player = self.players[left_path]
        right_player = self.players[right_path]

        left_player.add_result(right_player.rating, right_player.rd, 0.5)
        right_player.add_result(left_player.rating, left_player.rd, 0.5)

        self.history.append((left_path, right_path))
        self.current_index += 1
        if self.current_index >= len(self.pairs):
            self.finish_session()
        else:
            self.show_current_pair()

    def undo_last(self) -> None:
        if not self.history or self.current_index == 0:
            return

        self._set_ui_phase("undo-last", current_index=self.current_index)
        self.current_index -= 1

        left_path, right_path = self.history.pop()
        left_player = self.players[left_path]
        right_player = self.players[right_path]

        if left_player.matches:
            left_player.matches.pop()
        if right_player.matches:
            right_player.matches.pop()

        self.show_current_pair()

    def finish_session(self) -> None:
        if not self.image_paths:
            return
        self._set_ui_phase("finish-session", image_count=len(self.image_paths))
        self._stop_animation("left")
        self._stop_animation("right")
        self._stop_vlc("left")
        self._stop_vlc("right")

        for path in self.image_paths:
            update_glicko2_player(self.players[path], tau=0.5)

        renamed, skipped = self.rename_files()

        ranked = sorted(
            ((p, self.players[p]) for p in self.image_paths),
            key=lambda item: item[1].rating,
            reverse=True,
        )

        lines = ["Session complete.", "", f"Renamed: {renamed}", f"Skipped: {skipped}", "", "Top results:"]
        for i, (path, player) in enumerate(ranked[:10], start=1):
            lines.append(
                f"{i}. {path.name} | R={player.rating:.1f} RD={player.rd:.1f} S={player.sigma:.4f}"
            )

        summary = "\n".join(lines)
        self.status_var.set("Session complete. Files renamed.")
        self.progress_var.set("")
        messagebox.showinfo("Done", summary)

        # Refresh paths from folder because names changed, then keep the folder loaded.
        self.image_paths = load_images(self.folder)
        self.players = {p: player_from_path(p) for p in self.image_paths}
        self.pairs = self.build_session_pairs(self.image_paths)
        self.current_index = 0
        self.history = []

        if len(self.image_paths) >= 2 and self.pairs:
            self.status_var.set(f"Updated ratings for {len(self.image_paths)} images in: {self.folder}")
            self.show_current_pair()
        else:
            self.status_var.set("Need at least 2 supported image files in the selected folder.")
            self.progress_var.set("")
            self.left_label.config(text="Left image")
            self.right_label.config(text="Right image")
            self.left_image_label.config(image="")
            self.right_image_label.config(image="")

    def rename_files(self) -> Tuple[int, int]:
        renamed = 0
        skipped = 0

        ranked_paths = sorted(self.image_paths, key=lambda p: self.players[p].rating, reverse=True)

        rename_plan: List[Tuple[Path, Path]] = []
        targets_seen = set()

        for path in ranked_paths:
            player = self.players[path]
            clean_stem = strip_existing_prefix(path.stem)
            new_name = format_prefix(player) + clean_stem + path.suffix.lower()
            target = path.with_name(new_name)

            if target in targets_seen:
                suffix_num = 2
                while True:
                    alt = path.with_name(format_prefix(player) + clean_stem + f"_{suffix_num}" + path.suffix.lower())
                    if alt not in targets_seen:
                        target = alt
                        break
                    suffix_num += 1

            targets_seen.add(target)
            rename_plan.append((path, target))

        temp_plan: List[Tuple[Path, Path]] = []
        try:
            for i, (src, final_dst) in enumerate(rename_plan):
                if src == final_dst:
                    temp_plan.append((src, final_dst))
                    continue
                temp = src.with_name(src.name + f".__g2tmp__{i}")
                _rename_with_retry(src, temp)
                temp_plan.append((temp, final_dst))

            for current_src, final_dst in temp_plan:
                if current_src == final_dst:
                    continue
                _rename_with_retry(current_src, final_dst)
                renamed += 1

        except Exception as exc:
            messagebox.showerror("Rename error", f"Could not rename files.\n\n{exc}")
            skipped = len(self.image_paths) - renamed

        return renamed, skipped


def parse_args(argv: List[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Pairwise media ranker with Glicko-2 ratings.")
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable verbose debug logging for media loading and transitions.",
    )
    return parser.parse_args(argv)


def main(argv: List[str] | None = None) -> None:
    args = parse_args(argv)
    configure_logging(debug=args.debug)
    LOGGER.info("Application starting (debug=%s)", args.debug)
    root = tk.Tk()
    app = ImageRankerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
