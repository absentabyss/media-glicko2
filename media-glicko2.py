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

import hashlib
import json
import math
import random
import re
import threading
import warnings
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
VIDEO_CACHE_DIR_NAME = ".g2cache"
VIDEO_CACHE_VERSION = 1
VIDEO_COMPILE_SIZE = (1200, 1200)
VIDEO_CACHE_MAX_FRAMES = 90
VIDEO_CACHE_TARGET_FPS = 14.0

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
        return cached

    try:
        if suffix == ".gif":
            frames = _load_gif_frames(path, target_size, max_frames=max_frames)
            if frames:
                VIDEO_FRAME_CACHE[cache_key] = frames
                return frames
        elif suffix in SUPPORTED_VIDEO_EXTS:
            if _has_valid_video_cache(path):
                frames = _load_video_frames_from_cache(path, target_size)
                if frames:
                    VIDEO_FRAME_CACHE[cache_key] = frames
                    return frames

            compiled_ok = _compile_video_cache(path)
            if compiled_ok and _has_valid_video_cache(path):
                frames = _load_video_frames_from_cache(path, target_size)
                if frames:
                    VIDEO_FRAME_CACHE[cache_key] = frames
                    return frames

            # Last-resort fallback: direct decode from source.
            video_max_frames = 72 if suffix == ".webm" else max_frames
            frames = _load_video_frames(path, target_size, max_frames=video_max_frames)
            if frames:
                VIDEO_FRAME_CACHE[cache_key] = frames
                return frames

        with Image.open(path) as img:
            fallback_frames = [(_center_on_canvas(img.convert("RGB"), target_size), 100)]
            VIDEO_FRAME_CACHE[cache_key] = fallback_frames
            return fallback_frames
    except Exception:
        fallback = Image.new("RGB", target_size, color=(30, 30, 30))
        fallback_frames = [(fallback, 100)]
        VIDEO_FRAME_CACHE[cache_key] = fallback_frames
        return fallback_frames


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
        self._load_generation = 0

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

        self.left_image_label = tk.Label(self.left_panel, bg="#151515", cursor="hand2")
        self.left_image_label.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        self.left_image_label.bind("<Button-1>", lambda e: self.pick_winner("left"))

        self.right_image_label = tk.Label(self.right_panel, bg="#151515", cursor="hand2")
        self.right_image_label.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
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
        self.compile_folder_videos()
        self._update_progress()
        self.show_current_pair()

    def compile_folder_videos(self) -> None:
        if not self.folder:
            return
        video_paths = [p for p in self.image_paths if p.suffix.lower() in SUPPORTED_VIDEO_EXTS]
        if not video_paths:
            return

        cache_root = self.folder / VIDEO_CACHE_DIR_NAME
        cache_root.mkdir(parents=True, exist_ok=True)

        total = len(video_paths)
        for idx, video_path in enumerate(video_paths, start=1):
            self.status_var.set(f"Compiling video {idx}/{total}: {video_path.name}")
            self.master.update_idletasks()
            try:
                if not _has_valid_video_cache(video_path):
                    _compile_video_cache(video_path)
            except Exception:
                # Keep startup resilient; display-time fallback still exists.
                continue

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

        panel_width = max(self.main_frame.winfo_width() // 2 - 40, 250)
        self.left_label.config(wraplength=panel_width, text=f"Left: {shorten_name(left_path.name, 80)}")
        self.right_label.config(wraplength=panel_width, text=f"Right: {shorten_name(right_path.name, 80)}")

        self._set_media_on_label(left_path, self.left_image_label, side="left")
        self._set_media_on_label(right_path, self.right_image_label, side="right")

        self._update_progress()

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
        widget.update_idletasks()
        w = max(widget.winfo_width(), 200)
        h = max(widget.winfo_height(), 200)
        self._stop_animation(side)
        self._load_generation += 1
        load_generation = self._load_generation

        placeholder = Image.new("RGB", (w, h), color=(24, 24, 24))
        placeholder_photo = ImageTk.PhotoImage(placeholder)
        widget.config(image=placeholder_photo)
        if side == "left":
            self.left_photo = placeholder_photo
        else:
            self.right_photo = placeholder_photo

        def worker() -> None:
            frame_data = load_media_frames(path, (w, h))

            def apply_result() -> None:
                if load_generation != self._load_generation:
                    return

                photos = [ImageTk.PhotoImage(frame) for frame, _ in frame_data]
                delays = [delay for _, delay in frame_data]
                if not photos:
                    return

                if side == "left":
                    self.left_photo = photos[0]
                    self.left_animation_frames = photos
                    self.left_animation_delays = delays
                    self.left_animation_index = 0
                else:
                    self.right_photo = photos[0]
                    self.right_animation_frames = photos
                    self.right_animation_delays = delays
                    self.right_animation_index = 0

                widget.config(image=photos[0])
                if len(photos) > 1:
                    if side == "left":
                        self.left_animation_after_id = self.master.after(
                            delays[0], lambda: self._advance_animation("left")
                        )
                    else:
                        self.right_animation_after_id = self.master.after(
                            delays[0], lambda: self._advance_animation("right")
                        )

            self.master.after(0, apply_result)

        threading.Thread(target=worker, daemon=True).start()

    def pick_winner(self, side: str) -> None:
        if not self.pairs or self.current_index >= len(self.pairs):
            return

        left_path, right_path = self.pairs[self.current_index]
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
        self._stop_animation("left")
        self._stop_animation("right")

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
                src.rename(temp)
                temp_plan.append((temp, final_dst))

            for current_src, final_dst in temp_plan:
                if current_src == final_dst:
                    continue
                current_src.rename(final_dst)
                renamed += 1

        except Exception as exc:
            messagebox.showerror("Rename error", f"Could not rename files.\n\n{exc}")
            skipped = len(self.image_paths) - renamed

        return renamed, skipped


def main() -> None:
    root = tk.Tk()
    app = ImageRankerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
