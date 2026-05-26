from __future__ import annotations

import ctypes
import json
import math
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

import win32con
import win32gui
import win32process
from PIL import Image, ImageDraw, ImageFont, ImageGrab
from PySide6.QtCore import QEvent, QPoint, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QIcon, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)


APP_NAME = "GridLens"
APP_VERSION = "0.3.0"
DEFAULT_GRID_PX = 20
DEFAULT_MAJOR_GRID_PX = 100
DEFAULT_ZOOM = 2.0
CAPTURES_DIR = Path(__file__).resolve().parents[1] / "captures"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
ICON_PATH = PROJECT_ROOT / "assets" / "icon.ico"


@dataclass(frozen=True)
class WindowInfo:
    hwnd: int
    title: str
    rect: tuple[int, int, int, int]

    @property
    def width(self) -> int:
        return max(0, self.rect[2] - self.rect[0])

    @property
    def height(self) -> int:
        return max(0, self.rect[3] - self.rect[1])

    @property
    def label(self) -> str:
        size = f"{self.width}x{self.height}"
        return f"{self.title}  [{size}]"


@dataclass(frozen=True)
class CaptureConfig:
    save_dir: Path
    grid_px: int = DEFAULT_GRID_PX
    major_grid_px: int = DEFAULT_MAJOR_GRID_PX
    zoom_scale: float = DEFAULT_ZOOM
    copy_to_clipboard: bool = True


@dataclass(frozen=True)
class CapturePaths:
    folder: Path
    full: Path
    issue_analysis: Path
    ai_analysis: Path
    metadata: Path


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


def enable_dpi_awareness() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def resource_path(relative_path: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", PROJECT_ROOT))
    return base / relative_path


def app_icon() -> QIcon:
    icon_path = resource_path("assets/icon.ico")
    if icon_path.exists():
        return QIcon(str(icon_path))
    if ICON_PATH.exists():
        return QIcon(str(ICON_PATH))
    return QIcon()


def get_extended_window_rect(hwnd: int) -> tuple[int, int, int, int]:
    rect = RECT()
    try:
        result = ctypes.windll.dwmapi.DwmGetWindowAttribute(
            ctypes.wintypes.HWND(hwnd),
            ctypes.wintypes.DWORD(9),
            ctypes.byref(rect),
            ctypes.sizeof(rect),
        )
        if result == 0:
            return (rect.left, rect.top, rect.right, rect.bottom)
    except Exception:
        pass

    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    return (int(left), int(top), int(right), int(bottom))


def window_process_id(hwnd: int) -> int | None:
    try:
        _thread_id, process_id = win32process.GetWindowThreadProcessId(hwnd)
    except Exception:
        return None
    return int(process_id)


def is_own_window(hwnd: int) -> bool:
    return window_process_id(hwnd) == os.getpid()


def window_info_from_hwnd(hwnd: int) -> WindowInfo | None:
    if not hwnd or not win32gui.IsWindow(hwnd):
        return None
    if not win32gui.IsWindowVisible(hwnd):
        return None
    if is_own_window(hwnd):
        return None
    title = win32gui.GetWindowText(hwnd).strip()
    if not title or title == APP_NAME or title.startswith(f"{APP_NAME} "):
        return None
    rect = get_extended_window_rect(hwnd)
    width = rect[2] - rect[0]
    height = rect[3] - rect[1]
    if width < 80 or height < 80:
        return None
    return WindowInfo(hwnd=hwnd, title=title, rect=rect)


def list_windows() -> list[WindowInfo]:
    windows: list[WindowInfo] = []

    def callback(hwnd: int, _extra: object) -> bool:
        info = window_info_from_hwnd(hwnd)
        if info is not None:
            windows.append(info)
        return True

    win32gui.EnumWindows(callback, None)
    return sorted(windows, key=lambda item: item.title.lower())


def current_foreground_hwnd() -> int | None:
    hwnd = int(win32gui.GetForegroundWindow())
    if hwnd and win32gui.IsWindow(hwnd):
        return hwnd
    return None


def capture_window(hwnd: int) -> tuple[Image.Image, tuple[int, int, int, int]]:
    if not win32gui.IsWindow(hwnd):
        raise RuntimeError("The selected window is no longer available.")

    if win32gui.IsIconic(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        time.sleep(0.25)

    try:
        win32gui.SetForegroundWindow(hwnd)
    except Exception:
        pass
    time.sleep(0.2)

    bbox = get_extended_window_rect(hwnd)
    if bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
        raise RuntimeError("The selected window has an invalid capture area.")

    image = ImageGrab.grab(
        bbox=bbox,
        include_layered_windows=True,
        all_screens=True,
    )
    return image.convert("RGB"), bbox


def pil_to_qpixmap(image: Image.Image) -> QPixmap:
    rgba = image.convert("RGBA")
    data = rgba.tobytes("raw", "RGBA")
    qimage = QImage(data, rgba.width, rgba.height, QImage.Format.Format_RGBA8888)
    return QPixmap.fromImage(qimage.copy())


def safe_slug(text: str, fallback: str = "window") -> str:
    slug = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "-", text.strip())
    slug = re.sub(r"\s+", " ", slug)
    slug = slug.strip(" ._-")
    if not slug:
        return fallback
    reserved_names = {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        "COM1",
        "COM2",
        "COM3",
        "COM4",
        "COM5",
        "COM6",
        "COM7",
        "COM8",
        "COM9",
        "LPT1",
        "LPT2",
        "LPT3",
        "LPT4",
        "LPT5",
        "LPT6",
        "LPT7",
        "LPT8",
        "LPT9",
    }
    if slug.upper() in reserved_names:
        slug = f"{slug}-window"
    return slug[:80].rstrip(" ._-") or fallback


def create_capture_folder(save_dir: Path, window_title: str, created_at: datetime) -> tuple[Path, int]:
    save_dir.mkdir(parents=True, exist_ok=True)
    date_part = created_at.strftime("%Y%m%d")
    slug = safe_slug(window_title)
    base = f"{slug}_{date_part}"
    for serial in range(1, 10000):
        folder = save_dir / f"{base}_{serial:03d}"
        try:
            folder.mkdir()
        except FileExistsError:
            continue
        return folder, serial
    raise RuntimeError(f"Could not create a capture folder under {save_dir}")


def clamp_int(value: float, minimum: int, maximum: int) -> int:
    return max(minimum, min(maximum, int(round(value))))


def image_rect_from_points(
    start: tuple[int, int],
    end: tuple[int, int],
    width: int,
    height: int,
) -> tuple[int, int, int, int]:
    x1 = clamp_int(min(start[0], end[0]), 0, width)
    y1 = clamp_int(min(start[1], end[1]), 0, height)
    x2 = clamp_int(max(start[0], end[0]), 0, width)
    y2 = clamp_int(max(start[1], end[1]), 0, height)
    return (x1, y1, max(0, x2 - x1), max(0, y2 - y1))


def load_font(size: int, *, bold: bool = False, mono: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates: list[Path]
    if mono:
        candidates = [
            Path("C:/Windows/Fonts/consola.ttf"),
            Path("C:/Windows/Fonts/BIZ-UDGothicR.ttc"),
            Path("C:/Windows/Fonts/NotoSansJP-VF.ttf"),
            Path("C:/Windows/Fonts/YuGothM.ttc"),
            Path("C:/Windows/Fonts/meiryo.ttc"),
        ]
    elif bold:
        candidates = [
            Path("C:/Windows/Fonts/YuGothB.ttc"),
            Path("C:/Windows/Fonts/meiryob.ttc"),
            Path("C:/Windows/Fonts/BIZ-UDGothicB.ttc"),
            Path("C:/Windows/Fonts/NotoSansJP-VF.ttf"),
            Path("C:/Windows/Fonts/segoeuib.ttf"),
        ]
    else:
        candidates = [
            Path("C:/Windows/Fonts/YuGothM.ttc"),
            Path("C:/Windows/Fonts/meiryo.ttc"),
            Path("C:/Windows/Fonts/BIZ-UDGothicR.ttc"),
            Path("C:/Windows/Fonts/NotoSansJP-VF.ttf"),
            Path("C:/Windows/Fonts/segoeui.ttf"),
        ]
    for path in candidates:
        if path.exists():
            try:
                return ImageFont.truetype(str(path), size=size)
            except Exception:
                continue
    return ImageFont.load_default()


def text_size(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont) -> tuple[int, int]:
    bbox = draw.textbbox((0, 0), text, font=font)
    return (bbox[2] - bbox[0], bbox[3] - bbox[1])


def fit_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)] + "..."


def draw_label(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    text: str,
    *,
    font: ImageFont.ImageFont,
    fill: tuple[int, int, int] = (15, 23, 42),
    bg: tuple[int, int, int] | None = None,
) -> None:
    if bg is not None:
        width, height = text_size(draw, text, font)
        x, y = xy
        draw.rounded_rectangle(
            (x - 6, y - 4, x + width + 6, y + height + 5),
            radius=4,
            fill=bg,
        )
    draw.text(xy, text, font=font, fill=fill)


def resize_with_limit(image: Image.Image, max_width: int, max_height: int | None = None) -> tuple[Image.Image, float]:
    width, height = image.size
    scale = min(1.0, max_width / max(1, width))
    if max_height is not None:
        scale = min(scale, max_height / max(1, height))
    if scale >= 0.999:
        return image.copy(), 1.0
    resized = image.resize(
        (max(1, round(width * scale)), max(1, round(height * scale))),
        Image.Resampling.LANCZOS,
    )
    return resized, scale


def crop_issue(full_image: Image.Image, issue_rect: tuple[int, int, int, int]) -> Image.Image:
    x, y, width, height = issue_rect
    return full_image.crop((x, y, x + width, y + height))


def build_full_context_panel(
    full_image: Image.Image,
    issue_rect: tuple[int, int, int, int],
    window_title: str,
) -> Image.Image:
    pad = 24
    header_h = 72
    preview, scale = resize_with_limit(full_image, max_width=1500, max_height=900)
    panel_w = preview.width + pad * 2
    panel_h = header_h + preview.height + pad * 2

    panel = Image.new("RGB", (panel_w, panel_h), (248, 250, 252))
    draw = ImageDraw.Draw(panel)
    title_font = load_font(24, bold=True)
    body_font = load_font(16)
    mono_font = load_font(15, mono=True)

    draw_label(
        draw,
        (pad, 18),
        "Full window context",
        font=title_font,
        fill=(15, 23, 42),
    )
    subtitle = (
        f"{fit_text(window_title, 70)} | image={full_image.width}x{full_image.height}px "
        f"| preview scale={scale:.3g}x"
    )
    draw_label(draw, (pad, 48), subtitle, font=body_font, fill=(71, 85, 105))

    image_x = pad
    image_y = header_h + pad
    panel.paste(preview, (image_x, image_y))
    draw.rectangle(
        (image_x, image_y, image_x + preview.width, image_y + preview.height),
        outline=(30, 41, 59),
        width=2,
    )

    x, y, width, height = issue_rect
    rx1 = image_x + round(x * scale)
    ry1 = image_y + round(y * scale)
    rx2 = image_x + round((x + width) * scale)
    ry2 = image_y + round((y + height) * scale)

    overlay = Image.new("RGBA", panel.size, (0, 0, 0, 0))
    overlay_draw = ImageDraw.Draw(overlay)
    line_w = max(3, round(4 * max(1.0, scale)))
    overlay_draw.rectangle(
        (rx1, ry1, rx2, ry2),
        outline=(239, 68, 68, 255),
        width=line_w,
    )
    overlay_draw.rectangle((rx1, ry1, rx2, ry2), fill=(239, 68, 68, 32))
    label = f"issue rect: x={x}, y={y}, w={width}, h={height}"
    label_w, label_h = text_size(overlay_draw, label, mono_font)
    image_right = image_x + preview.width
    image_bottom = image_y + preview.height
    label_candidates = [
        (rx1, ry1 - label_h - 16),
        (rx1, ry2 + 12),
        (rx1 - label_w - 18, ry1),
        (rx2 + 12, ry1),
    ]
    label_x = rx1
    label_y = max(image_y + 8, ry1 - label_h - 16)
    for candidate_x, candidate_y in label_candidates:
        if (
            candidate_x >= image_x + 8
            and candidate_y >= image_y + 8
            and candidate_x + label_w + 14 <= image_right - 8
            and candidate_y + label_h + 10 <= image_bottom - 8
        ):
            label_x = candidate_x
            label_y = candidate_y
            break
    label_x = max(image_x + 8, min(label_x, image_right - label_w - 14))
    label_y = max(image_y + 8, min(label_y, image_bottom - label_h - 10))
    overlay_draw.rounded_rectangle(
        (label_x - 7, label_y - 5, label_x + label_w + 7, label_y + label_h + 6),
        radius=5,
        fill=(127, 29, 29, 220),
    )
    overlay_draw.text((label_x, label_y), label, font=mono_font, fill=(255, 255, 255, 255))
    panel = Image.alpha_composite(panel.convert("RGBA"), overlay).convert("RGB")
    return panel


def build_issue_analysis_panel(
    issue_image: Image.Image,
    issue_rect: tuple[int, int, int, int],
    config: CaptureConfig,
) -> tuple[Image.Image, float]:
    requested_zoom = max(1.0, float(config.zoom_scale))
    max_zoomed_width = 1350
    max_zoomed_height = 1200
    effective_zoom = min(
        requested_zoom,
        max_zoomed_width / max(1, issue_image.width),
        max_zoomed_height / max(1, issue_image.height),
    )
    effective_zoom = max(1.0, effective_zoom)

    zoomed_w = max(1, round(issue_image.width * effective_zoom))
    zoomed_h = max(1, round(issue_image.height * effective_zoom))
    zoomed = issue_image.resize((zoomed_w, zoomed_h), Image.Resampling.LANCZOS)
    clean_preview, clean_scale = resize_with_limit(issue_image, max_width=420, max_height=640)

    title_font = load_font(24, bold=True)
    body_font = load_font(16)
    mono_font = load_font(15, mono=True)
    small_mono_font = load_font(13, mono=True)

    ruler_l = 72
    ruler_t = 104
    pad_r = 24
    pad_b = 96
    column_gap = 34
    x, y, width, height = issue_rect
    title = "Issue crop analysis"
    meta = (
        f"origin=(0,0) | crop={issue_image.width}x{issue_image.height}px | "
        f"rect-in-window: x={x}, y={y}, w={width}, h={height}"
    )
    footer_lines = [
        f"grid={config.grid_px}px | major={config.major_grid_px}px | requested zoom={requested_zoom:.2f}x | effective zoom={effective_zoom:.3g}x",
        "Read positions in original crop pixels. Top-left of the crop is x=0, y=0.",
    ]
    probe = Image.new("RGB", (1, 1), (255, 255, 255))
    probe_draw = ImageDraw.Draw(probe)
    min_text_w = max(
        text_size(probe_draw, meta, body_font)[0] + 36,
        text_size(probe_draw, footer_lines[0], mono_font)[0] + 68,
        text_size(probe_draw, footer_lines[1], body_font)[0] + 68,
        760,
    )
    clean_title = "Clean crop (no grid)"
    clean_meta = f"scale={clean_scale:.3g}x | same crop"
    clean_col_w = max(
        clean_preview.width,
        text_size(probe_draw, clean_title, body_font)[0],
        text_size(probe_draw, clean_meta, small_mono_font)[0],
    )
    panel_w = max(ruler_l + zoomed_w + column_gap + clean_col_w + pad_r, min_text_w)
    content_h = max(zoomed_h, 50 + clean_preview.height)
    panel_h = ruler_t + content_h + pad_b

    panel = Image.new("RGBA", (panel_w, panel_h), (255, 255, 255, 255))
    draw = ImageDraw.Draw(panel, "RGBA")
    draw.text((18, 14), title, font=title_font, fill=(15, 23, 42, 255))
    draw.text((18, 44), meta, font=body_font, fill=(71, 85, 105, 255))

    image_x = ruler_l
    image_y = ruler_t
    panel.alpha_composite(zoomed.convert("RGBA"), (image_x, image_y))
    clean_x = image_x + zoomed_w + column_gap
    clean_y = image_y + 50
    draw.text((clean_x, image_y - 4), clean_title, font=body_font, fill=(15, 23, 42, 255))
    draw.text((clean_x, image_y + 22), clean_meta, font=small_mono_font, fill=(71, 85, 105, 255))
    panel.alpha_composite(clean_preview.convert("RGBA"), (clean_x, clean_y))
    draw.rectangle(
        (clean_x, clean_y, clean_x + clean_preview.width, clean_y + clean_preview.height),
        outline=(30, 41, 59, 255),
        width=2,
    )

    grid_px = max(1, int(config.grid_px))
    major_px = max(grid_px, int(config.major_grid_px))
    minor_color = (14, 165, 233, 70)
    major_color = (37, 99, 235, 140)
    axis_color = (225, 29, 72, 190)
    border_color = (15, 23, 42, 255)

    for source_x in range(0, issue_image.width + 1, grid_px):
        sx = image_x + round(source_x * effective_zoom)
        is_major = source_x % major_px == 0
        color = major_color if is_major else minor_color
        line_w = 2 if is_major else 1
        draw.line((sx, image_y, sx, image_y + zoomed_h), fill=color, width=line_w)

    for source_y in range(0, issue_image.height + 1, grid_px):
        sy = image_y + round(source_y * effective_zoom)
        is_major = source_y % major_px == 0
        color = major_color if is_major else minor_color
        line_w = 2 if is_major else 1
        draw.line((image_x, sy, image_x + zoomed_w, sy), fill=color, width=line_w)

    draw.line((image_x, image_y, image_x + zoomed_w, image_y), fill=axis_color, width=3)
    draw.line((image_x, image_y, image_x, image_y + zoomed_h), fill=axis_color, width=3)
    draw.rectangle(
        (image_x, image_y, image_x + zoomed_w, image_y + zoomed_h),
        outline=border_color,
        width=2,
    )

    for source_x in range(0, issue_image.width + 1, major_px):
        sx = image_x + round(source_x * effective_zoom)
        label_text = str(source_x)
        label_w, _label_h = text_size(draw, label_text, small_mono_font)
        label_x = min(max(sx + 4, image_x), image_x + zoomed_w - label_w)
        draw.text((label_x, image_y - 26), label_text, font=small_mono_font, fill=(30, 41, 59, 255))
        draw.line((sx, image_y - 8, sx, image_y), fill=(30, 41, 59, 255), width=2)

    for source_y in range(0, issue_image.height + 1, major_px):
        sy = image_y + round(source_y * effective_zoom)
        label_text = str(source_y)
        _label_w, label_h = text_size(draw, label_text, small_mono_font)
        label_y = min(max(sy - 8, image_y), image_y + zoomed_h - label_h)
        draw.text((14, label_y), label_text, font=small_mono_font, fill=(30, 41, 59, 255))
        draw.line((image_x - 8, sy, image_x, sy), fill=(30, 41, 59, 255), width=2)

    draw.rounded_rectangle(
        (18, panel_h - 78, panel_w - 18, panel_h - 18),
        radius=8,
        fill=(241, 245, 249, 255),
        outline=(203, 213, 225, 255),
        width=1,
    )
    draw.text((34, panel_h - 66), footer_lines[0], font=mono_font, fill=(15, 23, 42, 255))
    draw.text((34, panel_h - 42), footer_lines[1], font=body_font, fill=(71, 85, 105, 255))
    return panel.convert("RGB"), effective_zoom


def build_ai_analysis_image(
    full_image: Image.Image,
    issue_rect: tuple[int, int, int, int],
    window_title: str,
    config: CaptureConfig,
) -> tuple[Image.Image, Image.Image, float]:
    issue_image = crop_issue(full_image, issue_rect)
    context_panel = build_full_context_panel(full_image, issue_rect, window_title)
    issue_panel, effective_zoom = build_issue_analysis_panel(issue_image, issue_rect, config)

    outer = 28
    gap = 26
    header_h = 130
    canvas_w = max(context_panel.width, issue_panel.width) + outer * 2
    canvas_h = header_h + context_panel.height + issue_panel.height + outer * 2 + gap

    canvas = Image.new("RGB", (canvas_w, canvas_h), (226, 232, 240))
    draw = ImageDraw.Draw(canvas)
    title_font = load_font(30, bold=True)
    body_font = load_font(17)
    mono_font = load_font(15, mono=True)

    draw.text((outer, 24), "GridLens AI Analysis PNG", font=title_font, fill=(15, 23, 42))
    x, y, width, height = issue_rect
    line1 = f"window={full_image.width}x{full_image.height}px | issue rect in window: x={x}, y={y}, w={width}, h={height}"
    line2 = f"crop origin is local: x=0, y=0 | grid={config.grid_px}px | major={config.major_grid_px}px | zoom={effective_zoom:.3g}x"
    draw.text((outer, 62), line1, font=mono_font, fill=(30, 41, 59))
    draw.text((outer, 88), line2, font=body_font, fill=(71, 85, 105))

    y_cursor = header_h
    canvas.paste(context_panel, (outer, y_cursor))
    y_cursor += context_panel.height + gap
    canvas.paste(issue_panel, (outer, y_cursor))
    return canvas, issue_panel, effective_zoom


def save_capture_set(
    full_image: Image.Image,
    issue_rect: tuple[int, int, int, int],
    window_title: str,
    window_screen_rect: tuple[int, int, int, int],
    config: CaptureConfig,
) -> CapturePaths:
    created_at = datetime.now().astimezone()
    capture_folder, serial = create_capture_folder(config.save_dir, window_title, created_at)

    full_path = capture_folder / "full.png"
    issue_path = capture_folder / "issue-ai.png"
    ai_path = capture_folder / "ai-analysis.png"
    meta_path = capture_folder / "meta.json"

    ai_image, issue_panel, effective_zoom = build_ai_analysis_image(
        full_image,
        issue_rect,
        window_title,
        config,
    )

    full_image.save(full_path, format="PNG", optimize=True, compress_level=6)
    issue_panel.save(issue_path, format="PNG", optimize=True, compress_level=6)
    ai_image.save(ai_path, format="PNG", optimize=True, compress_level=6)

    x, y, width, height = issue_rect
    left, top, right, bottom = window_screen_rect
    metadata = {
        "app": APP_NAME,
        "app_version": APP_VERSION,
        "created_at": created_at.isoformat(timespec="seconds"),
        "capture_folder": str(capture_folder),
        "capture_serial": serial,
        "window_title": window_title,
        "window_image": {
            "width": full_image.width,
            "height": full_image.height,
        },
        "window_screen_rect": {
            "left": left,
            "top": top,
            "right": right,
            "bottom": bottom,
            "width": right - left,
            "height": bottom - top,
        },
        "issue_rect_in_window": {
            "x": x,
            "y": y,
            "width": width,
            "height": height,
        },
        "issue_origin": "top-left of issue crop is x=0, y=0",
        "grid_px": config.grid_px,
        "major_grid_px": config.major_grid_px,
        "requested_zoom": config.zoom_scale,
        "effective_zoom": effective_zoom,
        "files": {
            "full": str(full_path),
            "issue_analysis": str(issue_path),
            "ai_analysis": str(ai_path),
            "metadata": str(meta_path),
        },
    }
    meta_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")

    return CapturePaths(
        folder=capture_folder,
        full=full_path,
        issue_analysis=issue_path,
        ai_analysis=ai_path,
        metadata=meta_path,
    )


class ImageSelectionWidget(QWidget):
    selection_changed = Signal(object)

    def __init__(self, image: Image.Image) -> None:
        super().__init__()
        self.image = image
        self.pixmap = pil_to_qpixmap(image)
        self._display_rect = QRect()
        self._scale = 1.0
        self._drag_start: tuple[int, int] | None = None
        self._drag_current: tuple[int, int] | None = None
        self._dragging = False
        self.setMinimumSize(760, 480)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMouseTracking(True)

    def selection_rect(self) -> tuple[int, int, int, int] | None:
        if self._drag_start is None or self._drag_current is None:
            return None
        rect = image_rect_from_points(
            self._drag_start,
            self._drag_current,
            self.image.width,
            self.image.height,
        )
        if rect[2] < 4 or rect[3] < 4:
            return None
        return rect

    def paintEvent(self, _event: object) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(15, 23, 42))

        margin = 12
        available_w = max(1, self.width() - margin * 2)
        available_h = max(1, self.height() - margin * 2)
        self._scale = min(
            available_w / max(1, self.image.width),
            available_h / max(1, self.image.height),
            2.0,
        )
        draw_w = max(1, round(self.image.width * self._scale))
        draw_h = max(1, round(self.image.height * self._scale))
        x = (self.width() - draw_w) // 2
        y = (self.height() - draw_h) // 2
        self._display_rect = QRect(x, y, draw_w, draw_h)

        painter.drawPixmap(self._display_rect, self.pixmap)
        painter.setPen(QPen(QColor(203, 213, 225), 1))
        painter.drawRect(self._display_rect)

        selection = self.selection_rect()
        if selection is not None:
            sx, sy, sw, sh = selection
            widget_rect = QRect(
                x + round(sx * self._scale),
                y + round(sy * self._scale),
                max(1, round(sw * self._scale)),
                max(1, round(sh * self._scale)),
            )
            painter.fillRect(widget_rect, QColor(239, 68, 68, 44))
            painter.setPen(QPen(QColor(239, 68, 68), 3))
            painter.drawRect(widget_rect)

            painter.setPen(QColor(255, 255, 255))
            painter.fillRect(
                widget_rect.left(),
                max(0, widget_rect.top() - 26),
                260,
                22,
                QColor(127, 29, 29, 230),
            )
            painter.drawText(
                widget_rect.left() + 7,
                max(16, widget_rect.top() - 10),
                f"x={sx}, y={sy}, w={sw}, h={sh}",
            )

    def mousePressEvent(self, event: object) -> None:
        if not hasattr(event, "position"):
            return
        if hasattr(event, "button") and event.button() != Qt.MouseButton.LeftButton:
            return
        point = event.position().toPoint()
        if not self._display_rect.contains(point):
            return
        image_point = self._point_to_image(point)
        self._dragging = True
        self._drag_start = image_point
        self._drag_current = image_point
        self.selection_changed.emit(self.selection_rect())
        self.update()

    def mouseMoveEvent(self, event: object) -> None:
        if not self._dragging or self._drag_start is None or not hasattr(event, "position"):
            return
        point = event.position().toPoint()
        self._drag_current = self._point_to_image(point)
        self.selection_changed.emit(self.selection_rect())
        self.update()

    def mouseReleaseEvent(self, event: object) -> None:
        if not self._dragging or self._drag_start is None or not hasattr(event, "position"):
            return
        if hasattr(event, "button") and event.button() != Qt.MouseButton.LeftButton:
            return
        point = event.position().toPoint()
        self._drag_current = self._point_to_image(point)
        self._dragging = False
        self.selection_changed.emit(self.selection_rect())
        self.update()

    def _point_to_image(self, point: QPoint) -> tuple[int, int]:
        x = (point.x() - self._display_rect.left()) / max(self._scale, 0.001)
        y = (point.y() - self._display_rect.top()) / max(self._scale, 0.001)
        return (
            clamp_int(x, 0, self.image.width),
            clamp_int(y, 0, self.image.height),
        )


class SelectionDialog(QDialog):
    def __init__(
        self,
        full_image: Image.Image,
        window_title: str,
        window_screen_rect: tuple[int, int, int, int],
        config: CaptureConfig,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"{APP_NAME} - Select Issue Area")
        self.setWindowIcon(app_icon())
        self.resize(1180, 820)
        self.full_image = full_image
        self.window_title = window_title
        self.window_screen_rect = window_screen_rect
        self.config = config
        self.output_paths: CapturePaths | None = None

        layout = QVBoxLayout(self)
        intro = QLabel(
            "Drag over the problematic UI area. Save creates a combined AI analysis PNG, "
            "an issue analysis PNG, the full PNG, and metadata JSON."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.selector = ImageSelectionWidget(full_image)
        self.selector.selection_changed.connect(self._on_selection_changed)
        layout.addWidget(self.selector, stretch=1)

        self.selection_label = QLabel("Selection: none")
        layout.addWidget(self.selection_label)

        buttons = QDialogButtonBox()
        self.save_button = buttons.addButton("Save Analysis Set", QDialogButtonBox.ButtonRole.AcceptRole)
        self.save_button.setEnabled(False)
        buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _on_selection_changed(self, rect: object) -> None:
        if rect is None:
            self.selection_label.setText("Selection: none")
            self.save_button.setEnabled(False)
            return
        x, y, width, height = rect
        self.selection_label.setText(f"Selection: x={x}, y={y}, w={width}, h={height}")
        self.save_button.setEnabled(True)

    def _save(self) -> None:
        rect = self.selector.selection_rect()
        if rect is None:
            QMessageBox.warning(self, APP_NAME, "Select an issue area first.")
            return
        try:
            self.output_paths = save_capture_set(
                self.full_image,
                rect,
                self.window_title,
                self.window_screen_rect,
                self.config,
            )
            if self.config.copy_to_clipboard:
                qimage = QImage(str(self.output_paths.ai_analysis))
                QApplication.clipboard().setImage(qimage)
        except Exception as exc:
            QMessageBox.critical(self, APP_NAME, f"Failed to save capture set:\n{exc}")
            return

        copied = "The AI analysis PNG was also copied to the clipboard." if self.config.copy_to_clipboard else ""
        QMessageBox.information(
            self,
            APP_NAME,
            f"Saved analysis set folder:\n{self.output_paths.folder}\n\nAI analysis PNG:\n{self.output_paths.ai_analysis}\n\n{copied}",
        )
        self.accept()


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(app_icon())
        self.resize(760, 360)
        self.windows: list[WindowInfo] = []
        self.last_external_hwnd: int | None = None
        self.last_external_title: str | None = None

        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)

        heading = QLabel("GridLens")
        heading.setStyleSheet("font-size: 24px; font-weight: 700;")
        layout.addWidget(heading)

        description = QLabel(
            "Capture a selected window, mark the UI problem area, and save a "
            "single AI-friendly analysis PNG with coordinates, grid, and zoom."
        )
        description.setWordWrap(True)
        layout.addWidget(description)

        window_row = QHBoxLayout()
        self.window_combo = QComboBox()
        self.window_combo.setMinimumWidth(460)
        window_row.addWidget(self.window_combo, stretch=1)
        refresh_button = QPushButton("Refresh")
        refresh_button.clicked.connect(lambda: self.refresh_windows())
        window_row.addWidget(refresh_button)
        layout.addLayout(window_row)

        form = QFormLayout()
        self.grid_spin = QSpinBox()
        self.grid_spin.setRange(5, 100)
        self.grid_spin.setSingleStep(5)
        self.grid_spin.setValue(DEFAULT_GRID_PX)
        self.grid_spin.setSuffix(" px")
        form.addRow("Grid spacing", self.grid_spin)

        self.major_grid_spin = QSpinBox()
        self.major_grid_spin.setRange(20, 500)
        self.major_grid_spin.setSingleStep(20)
        self.major_grid_spin.setValue(DEFAULT_MAJOR_GRID_PX)
        self.major_grid_spin.setSuffix(" px")
        form.addRow("Major grid spacing", self.major_grid_spin)

        self.zoom_spin = QDoubleSpinBox()
        self.zoom_spin.setRange(1.0, 4.0)
        self.zoom_spin.setSingleStep(0.25)
        self.zoom_spin.setValue(DEFAULT_ZOOM)
        self.zoom_spin.setDecimals(2)
        self.zoom_spin.setSuffix("x")
        form.addRow("Issue zoom", self.zoom_spin)

        save_row = QHBoxLayout()
        self.save_dir_edit = QLineEdit(str(CAPTURES_DIR))
        save_row.addWidget(self.save_dir_edit, stretch=1)
        browse_button = QPushButton("Browse")
        browse_button.clicked.connect(self.browse_save_dir)
        save_row.addWidget(browse_button)
        form.addRow("Save folder", save_row)
        layout.addLayout(form)

        self.copy_checkbox = QCheckBox("Copy AI analysis PNG to clipboard after saving")
        self.copy_checkbox.setChecked(True)
        layout.addWidget(self.copy_checkbox)

        button_row = QHBoxLayout()
        capture_button = QPushButton("Capture Selected Window")
        capture_button.clicked.connect(self.capture_selected_window)
        button_row.addWidget(capture_button)
        open_button = QPushButton("Open Captures Folder")
        open_button.clicked.connect(self.open_captures_folder)
        button_row.addWidget(open_button)
        layout.addLayout(button_row)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        layout.addStretch(1)

        self.foreground_timer = QTimer(self)
        self.foreground_timer.setInterval(150)
        self.foreground_timer.timeout.connect(self.poll_foreground_window)
        self.foreground_timer.start()

        self.refresh_windows()

    def refresh_windows(self, preferred_hwnd: int | None = None, *, show_hint: bool = True) -> None:
        current = self.current_window_info()
        current_hwnd = current.hwnd if current is not None else None
        self.windows = list_windows()
        self.window_combo.clear()
        for info in self.windows:
            self.window_combo.addItem(info.label, info)

        selected: WindowInfo | None = None
        preferred = preferred_hwnd
        for index, info in enumerate(self.windows):
            if preferred is not None and info.hwnd == preferred:
                self.window_combo.setCurrentIndex(index)
                selected = info
                break
        if selected is None and current_hwnd is not None:
            for index, info in enumerate(self.windows):
                if info.hwnd == current_hwnd:
                    self.window_combo.setCurrentIndex(index)
                    selected = info
                    break

        if selected is not None and preferred is not None and selected.hwnd == preferred:
            self.status_label.setText(
                f"Found {len(self.windows)} capturable windows. Auto-selected window active before GridLens: {selected.title}"
            )
        elif selected is not None:
            self.status_label.setText(
                f"Found {len(self.windows)} capturable windows. Selected: {selected.title}"
            )
        elif show_hint:
            self.status_label.setText(
                f"Found {len(self.windows)} capturable windows. Switch to a target window, then return to GridLens to auto-select it."
            )
        else:
            self.status_label.setText(f"Found {len(self.windows)} capturable windows.")

    def poll_foreground_window(self) -> None:
        hwnd = current_foreground_hwnd()
        if hwnd is None:
            return
        info = window_info_from_hwnd(hwnd)
        if info is None:
            return
        self.last_external_hwnd = info.hwnd
        self.last_external_title = info.title

    def changeEvent(self, event: object) -> None:
        super().changeEvent(event)
        if (
            hasattr(event, "type")
            and event.type() == QEvent.Type.ActivationChange
            and self.isActiveWindow()
        ):
            self.select_last_external_window()

    def select_last_external_window(self) -> None:
        if self.last_external_hwnd is None:
            return
        self.refresh_windows(preferred_hwnd=self.last_external_hwnd, show_hint=False)

    def browse_save_dir(self) -> None:
        directory = QFileDialog.getExistingDirectory(
            self,
            "Select Save Folder",
            self.save_dir_edit.text() or str(CAPTURES_DIR),
        )
        if directory:
            self.save_dir_edit.setText(directory)

    def _current_config(self) -> CaptureConfig:
        return CaptureConfig(
            save_dir=Path(self.save_dir_edit.text()).expanduser(),
            grid_px=int(self.grid_spin.value()),
            major_grid_px=int(self.major_grid_spin.value()),
            zoom_scale=float(self.zoom_spin.value()),
            copy_to_clipboard=self.copy_checkbox.isChecked(),
        )

    def current_window_info(self) -> WindowInfo | None:
        data = self.window_combo.currentData()
        return data if isinstance(data, WindowInfo) else None

    def capture_selected_window(self) -> None:
        info = self.current_window_info()
        if info is None:
            QMessageBox.warning(self, APP_NAME, "Select a window first.")
            return

        config = self._current_config()
        self.status_label.setText(f"Capturing: {info.title}")
        self.hide()
        QApplication.processEvents()
        try:
            image, screen_rect = capture_window(info.hwnd)
        except Exception as exc:
            self.show()
            QMessageBox.critical(self, APP_NAME, f"Capture failed:\n{exc}")
            self.status_label.setText("Capture failed.")
            return
        finally:
            self.show()
            self.raise_()
            self.activateWindow()

        dialog = SelectionDialog(image, info.title, screen_rect, config, self)
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.output_paths is not None:
            self.status_label.setText(
                f"Saved: {dialog.output_paths.ai_analysis}"
            )
        else:
            self.status_label.setText("Capture cancelled.")

    def open_captures_folder(self) -> None:
        folder = Path(self.save_dir_edit.text()).expanduser()
        folder.mkdir(parents=True, exist_ok=True)
        try:
            import os

            os.startfile(folder)
        except Exception as exc:
            QMessageBox.warning(self, APP_NAME, f"Could not open folder:\n{exc}")


def build_synthetic_ui_image() -> Image.Image:
    image = Image.new("RGB", (900, 560), (244, 247, 250))
    draw = ImageDraw.Draw(image)
    title_font = load_font(28, bold=True)
    body_font = load_font(18)
    small_font = load_font(14)

    draw.rectangle((0, 0, 900, 64), fill=(30, 41, 59))
    draw.text((24, 18), "Synthetic App Window", font=title_font, fill=(255, 255, 255))
    draw.rectangle((24, 92, 268, 508), fill=(255, 255, 255), outline=(203, 213, 225), width=2)
    draw.text((44, 118), "Sidebar", font=body_font, fill=(15, 23, 42))
    for index in range(5):
        y = 158 + index * 54
        draw.rounded_rectangle((44, y, 232, y + 34), radius=6, fill=(226, 232, 240))
        draw.text((58, y + 8), f"Menu {index + 1}", font=small_font, fill=(51, 65, 85))

    draw.rectangle((292, 92, 864, 508), fill=(255, 255, 255), outline=(203, 213, 225), width=2)
    draw.text((318, 118), "Dashboard", font=body_font, fill=(15, 23, 42))
    draw.rounded_rectangle((318, 164, 590, 292), radius=8, fill=(219, 234, 254), outline=(59, 130, 246), width=2)
    draw.text((342, 190), "Card A", font=body_font, fill=(30, 64, 175))
    draw.rounded_rectangle((618, 164, 834, 292), radius=8, fill=(220, 252, 231), outline=(34, 197, 94), width=2)
    draw.text((640, 190), "Card B", font=body_font, fill=(22, 101, 52))
    draw.rounded_rectangle((318, 334, 804, 456), radius=8, fill=(254, 243, 199), outline=(245, 158, 11), width=2)
    draw.text((342, 360), "Problem target", font=body_font, fill=(146, 64, 14))
    draw.rounded_rectangle((692, 304, 850, 364), radius=10, fill=(239, 68, 68), outline=(127, 29, 29), width=3)
    draw.text((718, 324), "Overlap", font=body_font, fill=(255, 255, 255))
    return image


def run_self_test() -> int:
    output_dir = CAPTURES_DIR / "_selftest"
    config = CaptureConfig(
        save_dir=output_dir,
        grid_px=20,
        major_grid_px=100,
        zoom_scale=2.0,
        copy_to_clipboard=False,
    )
    image = build_synthetic_ui_image()
    issue_rect = (646, 282, 224, 112)
    paths = save_capture_set(
        image,
        issue_rect,
        "日本語タイトル GridLens Self Test",
        (10, 10, 910, 570),
        config,
    )
    missing = [
        path for path in [paths.full, paths.issue_analysis, paths.ai_analysis, paths.metadata]
        if not path.exists() or path.stat().st_size <= 0
    ]
    if missing:
        print("SELF_TEST_FAILED")
        for path in missing:
            print(path)
        return 1

    with Image.open(paths.ai_analysis) as ai_image:
        if ai_image.width < 800 or ai_image.height < 800:
            print("SELF_TEST_FAILED: AI analysis image unexpectedly small")
            return 1

    print("SELF_TEST_OK")
    print(f"full={paths.full}")
    print(f"issue_analysis={paths.issue_analysis}")
    print(f"ai_analysis={paths.ai_analysis}")
    print(f"metadata={paths.metadata}")
    return 0


def main(argv: Iterable[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if "--self-test" in args:
        return run_self_test()

    enable_dpi_awareness()
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setWindowIcon(app_icon())
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
