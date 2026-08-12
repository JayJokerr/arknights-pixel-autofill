# -*- coding: utf-8 -*-
"""
明日方舟 24x24 像素画自动填色工具
--------------------------------
功能：
1. 上传任意图片；
2. 转换为游戏 40 色调色板下的 24x24 像素画；
3. 在窗口内预览；
4. 自动找到标题包含“明日方舟”的 Windows 窗口；
5. 通过鼠标自动选择右侧调色板颜色，并逐格填充中间 24x24 画布。

默认坐标根据 1280x720 游戏客户区制作。
如果游戏 UI 后续改版，只需修改 CONFIG 区域中的坐标。

依赖：
    pip install pillow pywin32

紧急停止：
    按 F8。
"""

import sys
import os
import time
import threading
import ctypes
import json
import re
import urllib.request
import webbrowser
from ctypes import wintypes
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from pathlib import Path


APP_VERSION = "1.4.0"
GITHUB_REPOSITORY = "JayJokerr/arknights-pixel-autofill"
GITHUB_RELEASES_URL = f"https://github.com/{GITHUB_REPOSITORY}/releases"
GITHUB_LATEST_RELEASE_API = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/releases/latest"


def resource_path(*parts):
    """兼容源码运行和 PyInstaller 单文件解包目录。"""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base.joinpath(*parts)

try:
    from PIL import Image, ImageTk, ImageOps, ImageGrab
except ImportError:
    raise SystemExit("缺少 Pillow。请先运行：pip install pillow")

if sys.platform != "win32":
    raise SystemExit("此脚本用于 Windows。")

try:
    import win32gui
    import win32con
    import win32process
except ImportError:
    raise SystemExit("缺少 pywin32。请先运行：pip install pywin32")


# Must be set before Tk creates a window.  PER_MONITOR_AWARE_V2 is important
# when the game is on a secondary display whose scaling differs from the
# display containing this tool.
try:
    _set_dpi_context = ctypes.windll.user32.SetProcessDpiAwarenessContext
    _set_dpi_context.argtypes = (ctypes.c_void_p,)
    _set_dpi_context.restype = wintypes.BOOL
    if not _set_dpi_context(ctypes.c_void_p(-4)):  # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
        raise OSError("SetProcessDpiAwarenessContext failed")
except Exception:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

# Give the process its own Windows shell identity before the first Tk window is
# created.  Besides correct icon grouping, this prevents the packaged program
# from being treated as a transient python/Tk window by the taskbar.
try:
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
        "Tracer.ArknightsPixelAutofill"
    )
except Exception:
    pass


def is_running_as_admin():
    """Return whether the current Windows process has an elevated token."""
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def require_admin_before_startup():
    """Show a native warning and stop before Tk is created when not elevated."""
    if is_running_as_admin():
        return True
    try:
        ctypes.windll.user32.MessageBoxW(
            None,
            "本工具需要管理员权限才能向游戏窗口稳定发送鼠标输入。\n\n"
            "请关闭本提示后，右键程序并选择“以管理员身份运行”。",
            "需要管理员权限",
            0x00000000 | 0x00000030 | 0x00040000 | 0x00010000,
            # MB_OK | MB_ICONWARNING | MB_TOPMOST | MB_SETFOREGROUND
        )
    except Exception:
        pass
    return False


# ---------------------------- 游戏鼠标输入 ----------------------------
# PyAutoGUI 在 Windows 上使用旧式 mouse_event。部分游戏在获得焦点后会切换到
# DirectInput/Raw Input 路径，从而忽略这类事件。这里直接使用 SendInput；它产生的
# 绝对坐标事件更接近真实鼠标驱动送入 Windows 输入队列的方式。
class EmergencyStop(Exception):
    pass


class InputInjectionError(RuntimeError):
    pass


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = (
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouseData", ctypes.c_ulong),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.c_size_t),
    )


class _INPUTUNION(ctypes.Union):
    _fields_ = (("mi", _MOUSEINPUT),)


class _INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = (("type", ctypes.c_ulong), ("u", _INPUTUNION))


class GameMouse:
    INPUT_MOUSE = 0
    MOVE = 0x0001
    LEFTDOWN = 0x0002
    LEFTUP = 0x0004
    WHEEL = 0x0800
    MOVE_NOCOALESCE = 0x2000
    VIRTUALDESK = 0x4000
    ABSOLUTE = 0x8000
    WHEEL_DELTA = 120

    def __init__(self):
        self.user32 = ctypes.windll.user32
        self.user32.SendInput.argtypes = (
            ctypes.c_uint,
            ctypes.POINTER(_INPUT),
            ctypes.c_int,
        )
        self.user32.SendInput.restype = ctypes.c_uint
        self.pause = 0.01
        self.target_hwnd = None
        self.last_position = None
        self.cursor_clipped = False
        self.relative_compat = False

    def set_target(self, hwnd):
        if hwnd is None:
            self.release_cursor()
        self.target_hwnd = hwnd
        # Native PC Unity clients may consume Raw Input and ignore an absolute
        # move.  Emulator/Qt/Chromium render surfaces do not need that
        # workaround; sending a relative delta there makes Windows pointer
        # acceleration visibly overshoot before the absolute correction.
        self.relative_compat = False
        if hwnd:
            try:
                self.relative_compat = win32gui.GetClassName(hwnd) == "UnityWndClass"
            except Exception:
                pass

    def _target_client_rect(self):
        """Return target client bounds in physical virtual-screen pixels."""
        hwnd = self.target_hwnd
        if not hwnd or not win32gui.IsWindow(hwnd):
            return None
        left, top, right, bottom = win32gui.GetClientRect(hwnd)
        screen_left, screen_top = win32gui.ClientToScreen(hwnd, (left, top))
        screen_right, screen_bottom = win32gui.ClientToScreen(hwnd, (right, bottom))
        if screen_right <= screen_left or screen_bottom <= screen_top:
            return None
        return screen_left, screen_top, screen_right, screen_bottom

    def _clamp_to_target(self, x, y):
        bounds = self._target_client_rect()
        if bounds is None:
            return round(x), round(y)
        left, top, right, bottom = bounds
        # Keep a small inset because right/bottom are exclusive Win32 bounds.
        return (
            max(left + 2, min(right - 3, round(x))),
            max(top + 2, min(bottom - 3, round(y))),
        )

    def clip_to_target(self):
        """Confine the real cursor to the selected game client while painting."""
        bounds = self._target_client_rect()
        if bounds is None:
            raise InputInjectionError("无法读取游戏客户区，未启用光标保护。")
        rect = wintypes.RECT(*bounds)
        if not self.user32.ClipCursor(ctypes.byref(rect)):
            raise InputInjectionError("Windows 无法将鼠标约束到游戏窗口。")
        self.cursor_clipped = True

    def release_cursor(self):
        if self.cursor_clipped:
            self.user32.ClipCursor(None)
            self.cursor_clipped = False

    @staticmethod
    def _make_lparam(x, y):
        return (round(x) & 0xFFFF) | ((round(y) & 0xFFFF) << 16)

    def _post_move(self, screen_x, screen_y):
        hwnd = self.target_hwnd
        if not self.relative_compat or not hwnd or not win32gui.IsWindow(hwnd):
            return
        try:
            client_x, client_y = win32gui.ScreenToClient(hwnd, (screen_x, screen_y))
            win32gui.PostMessage(
                hwnd,
                win32con.WM_MOUSEMOVE,
                0,
                self._make_lparam(client_x, client_y),
            )
        except Exception:
            pass

    def _failsafe(self):
        # 不能再使用“左上角停止”：相对输入叠加 Windows 鼠标加速时，脚本自身
        # 可能短暂把系统光标推到左上角，从而误触发。F8 不与绘图输入冲突。
        if self.user32.GetAsyncKeyState(0x77) & 0x8000:  # VK_F8
            raise EmergencyStop

    def _send(self, flags, dx=0, dy=0, data=0):
        self._failsafe()
        event = _INPUT()
        event.type = self.INPUT_MOUSE
        event.mi = _MOUSEINPUT(dx, dy, data & 0xFFFFFFFF, flags, 0, 0)
        sent = self.user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(_INPUT))
        if sent != 1:
            # 已绑定游戏窗口时仍可走 PostMessage 备用路径，不应在这里提前中断。
            if self.target_hwnd and win32gui.IsWindow(self.target_hwnd):
                return False
            raise InputInjectionError(
                "Windows 拒绝注入鼠标输入。请关闭游戏的“以管理员身份运行”，"
                "或也以管理员身份运行本脚本。"
            )
        if self.pause:
            time.sleep(self.pause)
        return True

    def move_to(self, x, y, duration=0.0):
        x, y = self._clamp_to_target(x, y)

        if self.relative_compat:
            # Raw Input compatibility is opt-in for the native Unity surface.
            point = wintypes.POINT()
            if self.user32.GetCursorPos(ctypes.byref(point)):
                dx = x - point.x
                dy = y - point.y
                if dx or dy:
                    self._send(self.MOVE | self.MOVE_NOCOALESCE, dx, dy)

        # 使用虚拟桌面坐标，兼容游戏位于副屏以及副屏在主屏左侧/上方的情况。
        vx = self.user32.GetSystemMetrics(76)  # SM_XVIRTUALSCREEN
        vy = self.user32.GetSystemMetrics(77)  # SM_YVIRTUALSCREEN
        vw = self.user32.GetSystemMetrics(78)  # SM_CXVIRTUALSCREEN
        vh = self.user32.GetSystemMetrics(79)  # SM_CYVIRTUALSCREEN
        nx = round((x - vx) * 65535 / max(1, vw - 1))
        ny = round((y - vy) * 65535 / max(1, vh - 1))
        nx = max(0, min(65535, nx))
        ny = max(0, min(65535, ny))
        self._send(self.MOVE | self.ABSOLUTE | self.VIRTUALDESK, nx, ny)
        # SetCursorPos is an absolute correction and is not affected by mouse
        # acceleration.  It also makes the visible cursor deterministic when
        # an emulator coalesces consecutive SendInput move events.
        self.user32.SetCursorPos(x, y)
        self.last_position = (x, y)
        self._post_move(x, y)
        if duration > self.pause:
            time.sleep(duration - self.pause)

    def click(self, x=None, y=None):
        if x is not None and y is not None:
            self.move_to(x, y)
        self._send(self.LEFTDOWN)
        self._send(self.LEFTUP)

        # 某些 Unity 客户端锁定系统光标，但仍由窗口消息更新 UI 指针。
        hwnd = self.target_hwnd
        if self.relative_compat and hwnd and self.last_position and win32gui.IsWindow(hwnd):
            try:
                cx, cy = win32gui.ScreenToClient(hwnd, self.last_position)
                lp = self._make_lparam(cx, cy)
                win32gui.PostMessage(hwnd, win32con.WM_MOUSEMOVE, 0, lp)
                win32gui.PostMessage(hwnd, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, lp)
                win32gui.PostMessage(hwnd, win32con.WM_LBUTTONUP, 0, lp)
            except Exception:
                pass

    def drag_to(self, x, y, duration=0.48):
        """Drag through real intermediate positions for Unity/Raw Input UIs."""
        if self.last_position is None:
            self.move_to(x, y)
            return
        start_x, start_y = self.last_position
        self._send(self.LEFTDOWN)
        steps = max(8, round(duration / 0.025))
        for step in range(1, steps + 1):
            ratio = step / steps
            eased = ratio * ratio * (3.0 - 2.0 * ratio)
            self.move_to(
                start_x + (x - start_x) * eased,
                start_y + (y - start_y) * eased,
            )
            time.sleep(max(0.0, duration / steps - self.pause))
        self._send(self.LEFTUP)

    def scroll(self, clicks):
        self._send(self.WHEEL, data=int(clicks * self.WHEEL_DELTA))
        hwnd = self.target_hwnd
        if self.relative_compat and hwnd and self.last_position and win32gui.IsWindow(hwnd):
            try:
                sx, sy = self.last_position
                wp = (int(clicks * self.WHEEL_DELTA) & 0xFFFF) << 16
                win32gui.PostMessage(hwnd, win32con.WM_MOUSEWHEEL, wp, self._make_lparam(sx, sy))
            except Exception:
                pass


game_mouse = GameMouse()


# ---------------------------- 配置 ----------------------------
N = 24

# 根据你提供的游戏截图采样的 40 色，顺序与游戏调色板一致：
PALETTE = [
    (34, 34, 34),     (180, 180, 180), (234, 231, 223), (255, 255, 255),
    (211, 47, 54),    (156, 10, 0),     (214, 12, 74),   (230, 150, 141),
    (254, 152, 117),  (247, 208, 192),  (252, 239, 234), (251, 246, 232),
    (220, 210, 200),  (226, 206, 171),  (213, 99, 34),   (212, 140, 66),
    (242, 153, 0),    (249, 201, 51),   (252, 228, 153), (179, 180, 122),
    (194, 218, 114),  (108, 110, 0),    (170, 139, 82),  (169, 143, 116),
    (170, 146, 40),   (63, 43, 18),     (116, 73, 31),   (83, 70, 88),
    (42, 36, 70),     (57, 69, 153),    (90, 69, 157),   (186, 163, 215),
    (182, 188, 223),  (169, 172, 190),  (99, 171, 185),  (180, 210, 220),
    (145, 216, 230),  (71, 174, 160),   (182, 211, 200), (39, 56, 100),
]

# 坐标全部是“游戏客户区坐标”，不是整个 Windows 窗口坐标。
# 基准客户区：1280 x 720。
BASE_W = 1280
BASE_H = 720

CONFIG = {
    # 24x24 画布外边界：左、上、右、下
    # 当前截图中客户区约为 x=295..856, y=119..680
    "grid_left": 295.0,
    "grid_top": 119.0,
    "grid_right": 856.0,
    "grid_bottom": 680.0,

    # 右侧调色板 4 列中心 x
    "palette_cols": (989.0, 1060.0, 1130.0, 1200.0),

    # 调色板位于最上端时，6 行色块中心 y
    "palette_visible_rows": (285.0, 355.0, 425.0, 495.0, 565.0, 635.0),

    # 调色板位于最下端时，第 5~10 行完整色块的中心 y。滚动内容在端点
    # 会比顶部槽位整体低约 14px，因此不能与顶部共用同一组 y。
    "palette_bottom_visible_rows": (299.0, 369.0, 439.0, 509.0, 579.0, 649.0),

    # 鼠标滚轮放在右侧色板区域
    "palette_scroll_anchor": (1100.0, 600.0),

    # 调色板总共 10 行；滚动到底部后，第 5~10 行完整可见
    #（0-based = 4..9），上方还会残留第 4 行的一小截。
    "palette_bottom_first_global_row": 4,
}

# ---------------------------- Core modules ----------------------------
# Image processing and recognition stay importable without Tk or Win32, so
# their tests and future maintenance do not depend on a Windows input session.
from arknights_pixel.image_processing import (
    RESAMPLE_METHODS,
    flatten_alpha,
    import_24_bitmap,
    matrix_to_image,
    quantize_image,
)
from arknights_pixel.layout import calculate_ui_scale, responsive_metrics
from arknights_pixel.vision import (
    GridLayout,
    PaletteLayout,
    detect_canvas_grid,
    detect_canvas_grid_dynamic,
    detect_official_share_grid,
    detect_palette_layout,
)


# ---------------------------- Windows 窗口 ----------------------------
def list_selectable_windows():
    """Return user-facing top-level windows suitable for explicit selection."""
    result = []
    own_pid = os.getpid()

    def callback(hwnd, _):
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return True
            title = win32gui.GetWindowText(hwnd).strip()
            if not title:
                return True
            # Exclude this tool even when another packaged instance is open.
            # Checking only own_pid would leave an older EXE in the picker and
            # its title also contains “明日方舟”.
            if "24×24" in title and ("像素画自动填色" in title or "PIXEL LAB" in title):
                return True
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            if pid == own_pid:
                return True
            _left, _top, right, bottom = win32gui.GetClientRect(hwnd)
            width, height = max(0, right), max(0, bottom)
            if width < 480 or height < 270:
                return True
            class_name = win32gui.GetClassName(hwnd)
            if class_name in ("Shell_TrayWnd", "Progman", "WorkerW"):
                return True
            result.append({
                "hwnd": int(hwnd), "pid": int(pid), "title": title,
                "width": width, "height": height, "class": class_name,
            })
        except Exception:
            pass
        return True

    try:
        win32gui.EnumWindows(callback, None)
    except Exception:
        # Some restricted Windows sessions deny enumeration temporarily.  The
        # UI should still stay usable and allow a later manual refresh.
        return []
    result.sort(key=lambda item: (
        "明日方舟" not in item["title"],
        item["class"] != "UnityWndClass",
        item["title"].lower(),
    ))
    return result


def resolve_selected_window(target):
    """Resolve the user-selected shell; visual surface selection happens later."""
    if not target:
        return None, None
    hwnd = int(target.get("hwnd", 0))
    if hwnd and win32gui.IsWindow(hwnd):
        return hwnd, win32gui.GetWindowText(hwnd) or target.get("title", "")

    candidates = []
    target_pid = int(target.get("pid", 0))

    def callback(candidate, _):
        try:
            _, pid = win32process.GetWindowThreadProcessId(candidate)
            if pid != target_pid:
                return True
            title = win32gui.GetWindowText(candidate)
            left, top, right, bottom = win32gui.GetClientRect(candidate)
            area = max(0, right - left) * max(0, bottom - top)
            candidates.append((
                win32gui.GetClassName(candidate) == "UnityWndClass",
                win32gui.IsWindowVisible(candidate), area, candidate, title,
            ))
        except Exception:
            pass
        return True

    try:
        win32gui.EnumWindows(callback, None)
    except Exception:
        return None, None
    if not candidates:
        return None, None
    candidates.sort(reverse=True)
    return candidates[0][3], candidates[0][4]


def list_visual_surface_candidates(host_hwnd):
    """Collect generic client surfaces related to a selected game window.

    This deliberately contains no emulator brand, class-name or fixed toolbar
    assumptions.  It includes the selected client itself, all descendants
    (including cross-process render children), and overlapping same-process or
    owned top-level surfaces.  Pixels decide which candidate is the game.
    """
    if not host_hwnd or not win32gui.IsWindow(host_hwnd):
        return []
    try:
        host_ox, host_oy, host_w, host_h = get_client_info(host_hwnd)
        _, host_pid = win32process.GetWindowThreadProcessId(host_hwnd)
    except Exception:
        return []
    host_rect = (host_ox, host_oy, host_ox + host_w, host_oy + host_h)
    handles = [host_hwnd]

    def add_descendant(candidate, _):
        if candidate not in handles:
            handles.append(candidate)
        return True

    try:
        win32gui.EnumChildWindows(host_hwnd, add_descendant, None)
    except Exception:
        pass

    def add_related_top_level(candidate, _):
        try:
            if candidate in handles or not win32gui.IsWindowVisible(candidate):
                return True
            _, pid = win32process.GetWindowThreadProcessId(candidate)
            owner = win32gui.GetWindow(candidate, win32con.GW_OWNER)
            if pid == host_pid or owner == host_hwnd:
                handles.append(candidate)
        except Exception:
            pass
        return True

    try:
        win32gui.EnumWindows(add_related_top_level, None)
    except Exception:
        pass

    result = []
    for hwnd in handles:
        try:
            ox, oy, width, height = get_client_info(hwnd)
            if width < 640 or height < 360:
                continue
            rect = (ox, oy, ox + width, oy + height)
            overlap_w = max(0, min(host_rect[2], rect[2]) - max(host_rect[0], rect[0]))
            overlap_h = max(0, min(host_rect[3], rect[3]) - max(host_rect[1], rect[1]))
            overlap = overlap_w * overlap_h / max(1, width * height)
            if hwnd != host_hwnd and overlap < 0.55:
                continue
            result.append({
                "hwnd": int(hwnd), "origin_x": ox, "origin_y": oy,
                "width": width, "height": height, "overlap": overlap,
            })
        except Exception:
            continue
    # Stable order makes diagnostics and tests deterministic; it does not
    # choose the winner.  Visual validation below does that.
    result.sort(key=lambda item: (item["hwnd"] != host_hwnd, -item["width"] * item["height"]))
    return result


def get_client_info(hwnd):
    left, top, right, bottom = win32gui.GetClientRect(hwnd)
    w = right - left
    h = bottom - top
    sx, sy = win32gui.ClientToScreen(hwnd, (0, 0))
    return sx, sy, w, h


def focus_window(hwnd):
    def is_target_foreground():
        try:
            foreground = win32gui.GetForegroundWindow()
            if foreground == hwnd or win32gui.IsChild(hwnd, foreground):
                return True
            _, foreground_pid = win32process.GetWindowThreadProcessId(foreground)
            _, target_pid = win32process.GetWindowThreadProcessId(hwnd)
            return foreground_pid == target_pid
        except Exception:
            return False

    try:
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            time.sleep(0.3)
        win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
        win32gui.BringWindowToTop(hwnd)
        win32gui.SetForegroundWindow(hwnd)
        time.sleep(0.15)
        if is_target_foreground():
            return True
    except Exception:
        pass

    # Windows foreground-lock rules can ignore SetForegroundWindow without
    # raising.  Temporarily attach this thread's input queue to the current
    # foreground and target queues, bring the target forward, then verify.
    user32 = ctypes.windll.user32
    current_thread = ctypes.windll.kernel32.GetCurrentThreadId()
    foreground = win32gui.GetForegroundWindow()
    foreground_thread = user32.GetWindowThreadProcessId(foreground, None) if foreground else 0
    target_thread = user32.GetWindowThreadProcessId(hwnd, None)
    attached = []
    try:
        for thread_id in {foreground_thread, target_thread}:
            if thread_id and thread_id != current_thread:
                if user32.AttachThreadInput(current_thread, thread_id, True):
                    attached.append(thread_id)
        win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
        win32gui.BringWindowToTop(hwnd)
        win32gui.SetForegroundWindow(hwnd)
        try:
            win32gui.SetFocus(hwnd)
        except Exception:
            pass
        time.sleep(0.2)
        return is_target_foreground()
    except Exception:
        return False
    finally:
        for thread_id in reversed(attached):
            user32.AttachThreadInput(current_thread, thread_id, False)


# ---------------------------- 自动绘制 ----------------------------
class AutoPainter:
    def __init__(self, app):
        self.app = app
        self.stop_event = threading.Event()
        self.coordinate_calibration = None
        self.grid_x_lines = None
        self.grid_y_lines = None
        self.palette_layout = None
        self.paint_lock = threading.Lock()
        self.active_surface = None

    def stop(self):
        self.stop_event.set()
        game_mouse.release_cursor()

    def _surface_is_stable(self):
        if not self.active_surface:
            return False
        try:
            hwnd, ox, oy, width, height = self.active_surface
            return win32gui.IsWindow(hwnd) and get_client_info(hwnd) == (ox, oy, width, height)
        except Exception:
            return False

    def _ensure_surface_stable(self):
        if not self._surface_is_stable():
            raise RuntimeError("游戏窗口的位置或分辨率已经变化，已停止以避免点击画布外。")

    def _viewport(self, w, h):
        """Return the centered 16:9 game viewport inside the Unity client."""
        scale = min(w / BASE_W, h / BASE_H)
        viewport_w = BASE_W * scale
        viewport_h = BASE_H * scale
        return (w - viewport_w) / 2.0, (h - viewport_h) / 2.0, scale

    def _uncalibrated_scaled(self, x, y, w, h):
        viewport_x, viewport_y, scale = self._viewport(w, h)
        return viewport_x + x * scale, viewport_y + y * scale

    def _scaled(self, x, y, w, h):
        if self.coordinate_calibration is not None:
            offset_x, offset_y, scale_x, scale_y = self.coordinate_calibration
            return offset_x + x * scale_x, offset_y + y * scale_y
        return self._uncalibrated_scaled(x, y, w, h)

    @staticmethod
    def _expected_canvas_bounds(w, h):
        scale = min(w / BASE_W, h / BASE_H)
        viewport_x = (w - BASE_W * scale) / 2.0
        viewport_y = (h - BASE_H * scale) / 2.0
        return (
            viewport_x + CONFIG["grid_left"] * scale,
            viewport_y + CONFIG["grid_top"] * scale,
            viewport_x + CONFIG["grid_right"] * scale,
            viewport_y + CONFIG["grid_bottom"] * scale,
        )

    @classmethod
    def _recognize_surface_image(cls, image):
        """Require both editor structures before accepting an input surface."""
        w, h = image.size
        expected = cls._expected_canvas_bounds(w, h)
        palette = detect_palette_layout(image)
        grid_candidates = []
        errors = []
        for detector in (detect_canvas_grid_dynamic, detect_canvas_grid):
            try:
                candidate = detector(image, expected)
                if candidate.bounds not in {item.bounds for item in grid_candidates}:
                    grid_candidates.append(candidate)
            except Exception as error:
                errors.append(str(error))

        accepted = []
        short_edge = min(w, h)
        expected_side = ((expected[2] - expected[0]) + (expected[3] - expected[1])) / 2.0
        for grid in grid_candidates:
            grid_side_x = grid.x_lines[-1] - grid.x_lines[0]
            grid_side_y = grid.y_lines[-1] - grid.y_lines[0]
            board_ratio = ((grid_side_x + grid_side_y) / 2.0) / short_edge
            if not 0.55 <= board_ratio <= 0.83:
                errors.append(f"画布占屏比例异常：{board_ratio:.0%}")
                continue
            if grid.x_lines[-1] >= palette.bounds[0]:
                errors.append("画布与调色板相对位置不符合编辑界面")
                continue
            geometry = 1.0 - abs(grid_side_x - grid_side_y) / max(grid_side_x, grid_side_y)
            proximity = sum(abs(a - b) for a, b in zip(grid.bounds, expected)) / max(1, expected_side)
            # The 25-line detector can have a modest contrast confidence on a
            # completely white board, but its exact periodic fit and expected
            # location are strong evidence.  Compare every valid candidate;
            # never let the first successful outer-frame hypothesis win by
            # itself.
            score = (
                palette.confidence * 2.0
                + grid.confidence
                + geometry
                - proximity * 0.45
            )
            if palette.confidence >= 0.08 and grid.confidence >= 0.25:
                accepted.append((score, grid))

        if not accepted:
            raise RuntimeError("；".join(errors[:4]) or "未识别到有效24×24画布")
        accepted.sort(reverse=True, key=lambda item: item[0])
        score, grid = accepted[0]
        return grid, palette, score

    def _select_visual_surface(self, host_hwnd):
        """Choose the game surface with a cheap screen probe then one full scan."""
        candidates = list_visual_surface_candidates(host_hwnd)
        if not candidates:
            raise RuntimeError("所选窗口没有可截图的客户区")
        # Multiple emulator HWNDs often expose the exact same screen rectangle.
        # One screenshot/recognition per physical rectangle is sufficient.
        unique = []
        seen_rectangles = set()
        for candidate in candidates:
            rectangle = (
                candidate["origin_x"], candidate["origin_y"],
                candidate["width"], candidate["height"],
            )
            if rectangle in seen_rectangles:
                continue
            seen_rectangles.add(rectangle)
            unique.append(candidate)
        # This is only an evaluation order, never an acceptance rule.  Most
        # game render surfaces are 16:9 while emulator shells add toolbars;
        # trying likely viewports first avoids spending a full probe on the
        # shell.  A non-16:9 surface is still accepted when its pixels pass.
        unique.sort(key=lambda item: (
            abs(item["width"] / item["height"] - BASE_W / BASE_H),
            -item["overlap"],
        ))

        accepted = []
        errors = []
        for candidate in unique:
            try:
                ox, oy = candidate["origin_x"], candidate["origin_y"]
                w, h = candidate["width"], candidate["height"]
                image = ImageGrab.grab(
                    bbox=(ox, oy, ox + w, oy + h), all_screens=True,
                ).convert("RGB")
                # Cap the long edge during candidate screening.  Recognition
                # cost otherwise grows sharply at 2K/4K and repeated emulator
                # surfaces waste seconds.  Coordinates are refined once at
                # native resolution for the best probe only.
                # 1280 keeps a 24x24 cell pitch large enough for unambiguous
                # outer-frame detection, while still capping 2K/4K scan cost.
                probe_scale = min(1.0, 1280 / max(w, h))
                probe = image if probe_scale == 1.0 else image.resize(
                    (round(w * probe_scale), round(h * probe_scale)),
                    # Palette recognition compares actual game swatch colours;
                    # nearest-neighbour keeps them exact during the probe.
                    Image.Resampling.NEAREST,
                )
                grid, palette, vision_score = self._recognize_surface_image(probe)
                scale_x = w / probe.width
                scale_y = h / probe.height
                native_grid = GridLayout(
                    tuple(value * scale_x for value in grid.x_lines),
                    tuple(value * scale_y for value in grid.y_lines),
                    grid.confidence,
                )
                native_palette = PaletteLayout(
                    tuple(value * scale_x for value in palette.columns),
                    tuple(value * scale_y for value in palette.rows),
                    palette.first_global_row,
                    palette.confidence,
                )
                accepted.append((
                    vision_score + candidate["overlap"] * 0.05,
                    w * h, candidate, native_grid, native_palette,
                ))
                # Both structures are already near-certain.  Candidate order
                # prefers the selected client and largest overlapping surfaces,
                # so another full screenshot cannot materially improve safety.
                if grid.confidence >= 0.94 and palette.confidence >= 0.80:
                    break
            except Exception as error:
                errors.append(f"{candidate['width']}×{candidate['height']}: {error}")
        if not accepted:
            detail = "；".join(errors[:3])
            raise RuntimeError(
                "未在所选窗口的候选画面中同时识别到24×24画布和4列调色板"
                + (f"（{detail}）" if detail else "")
            )
        accepted.sort(reverse=True, key=lambda item: (item[0], item[1]))
        _score, _area, selected, grid, palette = accepted[0]
        return selected, grid, palette

    @staticmethod
    def _optimize_cell_order(points):
        """Serpentine scan minimizes visible long jumps for one colour."""
        rows = {}
        for x, y in points:
            rows.setdefault(y, []).append(x)
        ordered = []
        for y in sorted(rows):
            xs = sorted(rows[y], reverse=bool(y % 2))
            ordered.extend((x, y) for x in xs)
        return ordered

    @staticmethod
    def _detect_grid_axis(gray, expected_bounds, vertical):
        """Find the 25 grid lines near the proportional coordinate estimate."""
        from statistics import median

        pixels = gray.load()
        image_w, image_h = gray.size
        left, top, right, bottom = expected_bounds
        expected_start = left if vertical else top
        expected_end = right if vertical else bottom
        expected_pitch = (expected_end - expected_start) / N
        axis_size = image_w if vertical else image_h

        search_start = max(2, round(expected_start - expected_pitch * 1.7))
        search_end = min(axis_size - 3, round(expected_end + expected_pitch * 1.7))
        scores = [0] * axis_size

        if vertical:
            samples = range(max(2, round(top - expected_pitch)),
                            min(image_h - 2, round(bottom + expected_pitch)), 3)
            for pos in range(search_start, search_end + 1):
                scores[pos] = sum(
                    abs(2 * pixels[pos, q] - pixels[pos - 2, q] - pixels[pos + 2, q])
                    + abs(pixels[pos - 2, q] - pixels[pos + 2, q])
                    for q in samples
                )
        else:
            samples = range(max(2, round(left - expected_pitch)),
                            min(image_w - 2, round(right + expected_pitch)), 3)
            for pos in range(search_start, search_end + 1):
                scores[pos] = sum(
                    abs(2 * pixels[q, pos] - pixels[q, pos - 2] - pixels[q, pos + 2])
                    + abs(pixels[q, pos - 2] - pixels[q, pos + 2])
                    for q in samples
                )

        local_scores = [0] * axis_size
        for pos in range(search_start, search_end + 1):
            local_scores[pos] = max(scores[max(0, pos - 4):min(axis_size, pos + 5)])
        baseline_values = [local_scores[pos] for pos in range(search_start, search_end + 1)
                           if local_scores[pos] > 0]
        if not baseline_values:
            raise RuntimeError("未检测到网格线")
        baseline = median(baseline_values)

        best = None
        start_min = max(2, round(expected_start - expected_pitch * 1.5))
        start_max = min(axis_size - 3, round(expected_start + expected_pitch * 1.5))
        pitch_min = round(expected_pitch * 0.88 * 20)
        pitch_max = round(expected_pitch * 1.12 * 20)
        for pitch_step in range(pitch_min, pitch_max + 1):
            pitch = pitch_step / 20.0
            for start in range(start_min, start_max + 1):
                predicted = [round(start + i * pitch) for i in range(N + 1)]
                if predicted[-1] >= axis_size - 2:
                    continue
                values = [local_scores[pos] for pos in predicted]
                proximity = (
                    abs(start - expected_start) / expected_pitch
                    + abs((start + N * pitch) - expected_end) / expected_pitch
                    + 4.0 * abs(pitch - expected_pitch) / expected_pitch
                )
                # A periodic grid has many phase-equivalent fits.  Weight the
                # expected outer bounds strongly enough to avoid mistaking the
                # second grid line for the first one.
                fit_score = sum(values) - baseline * 60.0 * proximity
                if best is None or fit_score > best[0]:
                    best = fit_score, start, pitch, values

        if best is None:
            raise RuntimeError("网格线拟合失败")
        _, start, pitch, values = best
        positions = []
        for i in range(N + 1):
            predicted = round(start + i * pitch)
            lo = max(2, predicted - 4)
            hi = min(axis_size - 2, predicted + 5)
            positions.append(max(range(lo, hi), key=lambda pos: scores[pos]))

        if any(b <= a for a, b in zip(positions, positions[1:])):
            raise RuntimeError("网格线顺序异常")
        detected_pitch = (positions[-1] - positions[0]) / N
        residual = max(
            abs(pos - (positions[0] + i * detected_pitch))
            for i, pos in enumerate(positions)
        )
        if median(values) < baseline * 0.75 or residual > max(4.0, detected_pitch * 0.25):
            raise RuntimeError(
                f"网格识别置信度不足(score={median(values) / max(1, baseline):.2f}, "
                f"residual={residual:.1f})"
            )
        return positions

    def _calibrate_coordinates(self, origin_x, origin_y, w, h):
        """Capture the visible game client and calibrate against its real grid."""
        self.coordinate_calibration = None
        self.grid_x_lines = None
        self.grid_y_lines = None
        expected_left, expected_top = self._uncalibrated_scaled(
            CONFIG["grid_left"], CONFIG["grid_top"], w, h
        )
        expected_right, expected_bottom = self._uncalibrated_scaled(
            CONFIG["grid_right"], CONFIG["grid_bottom"], w, h
        )
        screenshot = ImageGrab.grab(
            bbox=(origin_x, origin_y, origin_x + w, origin_y + h),
            all_screens=True,
        ).convert("RGB")
        expected = (expected_left, expected_top, expected_right, expected_bottom)
        try:
            # The long outer frame is authoritative and prevents a periodic
            # 24-line fit from shifting the whole canvas by one emulator row.
            layout = detect_canvas_grid_dynamic(screenshot, expected)
        except RuntimeError:
            layout = detect_canvas_grid(screenshot, expected)
        x_lines = list(layout.x_lines)
        y_lines = list(layout.y_lines)

        scale_x = (x_lines[-1] - x_lines[0]) / (CONFIG["grid_right"] - CONFIG["grid_left"])
        scale_y = (y_lines[-1] - y_lines[0]) / (CONFIG["grid_bottom"] - CONFIG["grid_top"])
        offset_x = x_lines[0] - CONFIG["grid_left"] * scale_x
        offset_y = y_lines[0] - CONFIG["grid_top"] * scale_y
        self.coordinate_calibration = (offset_x, offset_y, scale_x, scale_y)
        self.grid_x_lines = x_lines
        self.grid_y_lines = y_lines
        try:
            self.palette_layout = detect_palette_layout(screenshot)
        except Exception:
            self.palette_layout = None
        return x_lines, y_lines

    def _refresh_palette_layout(self, origin_x, origin_y, w, h):
        screenshot = ImageGrab.grab(
            bbox=(origin_x, origin_y, origin_x + w, origin_y + h),
            all_screens=True,
        ).convert("RGB")
        self.palette_layout = detect_palette_layout(screenshot)
        return self.palette_layout

    def _grid_center(self, col, row, w, h):
        if self.grid_x_lines is not None and self.grid_y_lines is not None:
            return (
                (self.grid_x_lines[col] + self.grid_x_lines[col + 1]) / 2.0,
                (self.grid_y_lines[row] + self.grid_y_lines[row + 1]) / 2.0,
            )
        gl, gt = self._scaled(CONFIG["grid_left"], CONFIG["grid_top"], w, h)
        gr, gb = self._scaled(CONFIG["grid_right"], CONFIG["grid_bottom"], w, h)
        cw = (gr - gl) / N
        ch = (gb - gt) / N
        return gl + (col + 0.5) * cw, gt + (row + 0.5) * ch

    def _scroll_palette(self, origin_x, origin_y, w, h, to_bottom):
        self._ensure_surface_stable()
        if self.palette_layout is not None:
            left, top, right, bottom = self.palette_layout.bounds
            ax, ay = (left + right) / 2, (top + bottom) / 2
        else:
            ax, ay = CONFIG["palette_scroll_anchor"]
            ax, ay = self._scaled(ax, ay, w, h)
        game_mouse.move_to(origin_x + ax, origin_y + ay, duration=0.08)

        # Unity 可能把一个 mouseData=35*WHEEL_DELTA 的大事件只当成一次滚动。
        # 改成多个独立刻度并留出帧间隔，确保真正到达最上/最下端；到达端点后
        # 多余事件会被调色板自然忽略。
        direction = -1 if to_bottom else 1
        for _ in range(24):
            if self.stop_event.is_set():
                return
            # On mixed-DPI multi-monitor desktops Windows can change the
            # physical cursor mapping after focus/DPI transitions.  Re-anchor
            # every wheel notch so Unity always sees the palette as hovered.
            game_mouse.move_to(origin_x + ax, origin_y + ay)
            game_mouse.scroll(direction)
            time.sleep(0.025)
        time.sleep(0.35)
        try:
            layout = self._refresh_palette_layout(origin_x, origin_y, w, h)
        except Exception:
            # Retain the previous dynamic layout or fixed-coordinate fallback.
            layout = None

        expected_first = CONFIG["palette_bottom_first_global_row"] if to_bottom else 0
        if layout is not None and layout.first_global_row != expected_first:
            # Some Unity/emulator builds ignore synthetic wheel messages but
            # accept a physical drag of the white scrollbar handle.  Its two
            # endpoints are derived from the detected swatch pitch, not fixed
            # 1280x720 coordinates.
            pitch = median(b - a for a, b in zip(layout.rows, layout.rows[1:]))
            center_x = (layout.columns[0] + layout.columns[-1]) / 2
            track_top = max(8, layout.rows[0] - pitch * 1.75)
            track_bottom = min(h - 8, layout.rows[-1] + pitch * 1.05)
            # The game's decorative scrollbar is inverted: the white marker
            # sits at the bottom for palette rows 1..6 and at the top for
            # rows 5..10.
            start_y, end_y = ((track_bottom, track_top) if to_bottom
                              else (track_top, track_bottom))
            game_mouse.move_to(origin_x + center_x, origin_y + start_y, duration=0.08)
            game_mouse.drag_to(origin_x + center_x, origin_y + end_y, duration=0.55)
            time.sleep(0.45)
            layout = self._refresh_palette_layout(origin_x, origin_y, w, h)

        if layout is not None and layout.first_global_row != expected_first:
            raise RuntimeError(
                f"调色板未滚动到{'底部' if to_bottom else '顶部'}："
                f"识别到起始行为 {layout.first_global_row + 1}。"
                "请确认游戏已获得焦点且本工具以管理员身份运行。"
            )

    def _palette_click_pos(self, color_index, origin_x, origin_y, w, h, bottom_mode):
        row, col = divmod(color_index, 4)

        if self.palette_layout is not None:
            visible_row = row - self.palette_layout.first_global_row
            if not (0 <= visible_row < len(self.palette_layout.rows)):
                raise RuntimeError(
                    f"色号 {color_index + 1} 不在当前动态识别的调色板行中"
                )
            return (
                origin_x + self.palette_layout.columns[col],
                origin_y + self.palette_layout.rows[visible_row],
            )

        if bottom_mode:
            first = CONFIG["palette_bottom_first_global_row"]
            visible_row = row - first
            visible_rows = CONFIG["palette_bottom_visible_rows"]
        else:
            visible_row = row
            visible_rows = CONFIG["palette_visible_rows"]

        if not (0 <= visible_row < len(visible_rows)):
            raise RuntimeError(f"色号 {color_index + 1} 当前滚动状态下不可见")

        px, py = self._scaled(CONFIG["palette_cols"][col], visible_rows[visible_row], w, h)
        return origin_x + px, origin_y + py

    def paint(self, matrix, window_target, click_delay, skip_white):
        if not self.paint_lock.acquire(blocking=False):
            self.app.thread_status("已有自动填充任务正在运行，请先停止并等待结束。", error=True)
            return
        try:
            self._paint_locked(matrix, window_target, click_delay, skip_white)
        finally:
            self.active_surface = None
            game_mouse.release_cursor()
            game_mouse.set_target(None)
            self.paint_lock.release()

    def _paint_locked(self, matrix, window_target, click_delay, skip_white):
        self.stop_event.clear()
        game_mouse.set_target(None)
        self.coordinate_calibration = None
        self.grid_x_lines = None
        self.grid_y_lines = None
        self.palette_layout = None

        # 客户端在前后台切换时会销毁/重建 Unity 窗口；隐藏的 Arknights Qt
        # 宿主窗口句柄则保持不变。激活宿主后重新查找，直到拿到稳定渲染窗口。
        host_hwnd = hwnd = title = None
        ox = oy = w = h = 0
        for _ in range(5):
            candidate, candidate_title = resolve_selected_window(window_target)
            if not candidate:
                time.sleep(0.2)
                continue
            if not focus_window(candidate):
                time.sleep(0.2)
                continue
            time.sleep(0.45)

            refreshed, refreshed_title = resolve_selected_window(window_target)
            if refreshed and refreshed != candidate:
                candidate, candidate_title = refreshed, refreshed_title
                focus_window(candidate)
                time.sleep(0.35)

            try:
                ox, oy, w, h = get_client_info(candidate)
            except Exception:
                time.sleep(0.2)
                continue
            host_hwnd, title = candidate, candidate_title
            if w >= 800 and h >= 450:
                break

        if not host_hwnd:
            self.app.thread_status("所选游戏窗口已失效，请返回工具刷新并重新选择窗口。", error=True)
            return

        if w < 800 or h < 450:
            self.app.thread_status(f"游戏客户区尺寸异常：{w}x{h}", error=True)
            return

        # Select the true game surface from visual evidence.  This supports
        # direct PC clients plus emulator shells, child render controls and
        # separate top-level render surfaces without emulator-specific rules.
        try:
            surface, visual_grid, visual_palette = self._select_visual_surface(host_hwnd)
            hwnd = surface["hwnd"]
            ox, oy = surface["origin_x"], surface["origin_y"]
            w, h = surface["width"], surface["height"]
            self.grid_x_lines = list(visual_grid.x_lines)
            self.grid_y_lines = list(visual_grid.y_lines)
            self.palette_layout = visual_palette
            self.active_surface = (hwnd, ox, oy, w, h)
        except Exception as surface_error:
            self.app.thread_status(f"游戏编辑界面自动定位失败：{surface_error}", error=True)
            return

        # 允许按比例缩放，但对 1280x720 最准确
        ratio_err = abs((w / h) - (BASE_W / BASE_H))
        if ratio_err > 0.08:
            self.app.thread_status(
                f"检测到非 16:9 游戏客户区 {w}x{h}，将按居中 16:9 视口换算坐标。",
            )

        # 绑定经过筛选和恢复后的 Unity 渲染窗口，启用窗口消息备用输入路径。
        game_mouse.set_target(hwnd)

        # Calibrate from the pixels currently rendered by the game.  This is
        # more reliable than trusting a nominal 1280x720 setting on mixed-DPI
        # systems, where the Win32 client rectangle can use a different scale.
        detected_w = self.grid_x_lines[-1] - self.grid_x_lines[0]
        detected_h = self.grid_y_lines[-1] - self.grid_y_lines[0]
        calibration_summary = f"视觉校准 {detected_w:.0f}×{detected_h:.0f}px"
        self.app.thread_status(
            f"已从候选画面动态定位编辑界面：画布 {detected_w:.0f}×{detected_h:.0f}px；"
            f"调色板置信度 {self.palette_layout.confidence:.0%}；输入客户区 {w}×{h}"
        )

        # 按颜色分组，减少切换色板次数
        groups = {i: [] for i in range(len(PALETTE))}
        for y in range(N):
            for x in range(N):
                idx = matrix[y][x]
                if skip_white and idx == 3:
                    continue
                groups[idx].append((x, y))

        for color_index in groups:
            groups[color_index] = self._optimize_cell_order(groups[color_index])

        used_colors = [i for i, pts in groups.items() if pts]
        total_cells = sum(len(groups[i]) for i in used_colors)

        # Only lock the cursor for the actual input phase.  From this point on
        # every exit path is covered by the finally block below.
        try:
            game_mouse.clip_to_target()
        except Exception as cursor_error:
            game_mouse.set_target(None)
            self.app.thread_status(f"无法启用游戏窗口鼠标保护：{cursor_error}", error=True)
            return

        try:
            done = 0

            # 第一阶段：顶部 6 行（颜色 1~24）
            self._scroll_palette(ox, oy, w, h, to_bottom=False)
            for color_idx in range(24):
                if self.stop_event.is_set():
                    self.app.thread_status("已停止。")
                    return
                pts = groups[color_idx]
                if not pts:
                    continue

                px, py = self._palette_click_pos(color_idx, ox, oy, w, h, bottom_mode=False)
                self._ensure_surface_stable()
                game_mouse.click(px, py)
                time.sleep(max(0.08, click_delay))

                for x, y in pts:
                    if self.stop_event.is_set():
                        self.app.thread_status("已停止。")
                        return
                    gx, gy = self._grid_center(x, y, w, h)
                    self._ensure_surface_stable()
                    game_mouse.click(ox + gx, oy + gy)
                    done += 1
                    self.app.thread_progress(done, total_cells, color_idx)
                    time.sleep(click_delay)

            # 第二阶段：底部颜色 25~40
            if any(groups[i] for i in range(24, 40)):
                self._scroll_palette(ox, oy, w, h, to_bottom=True)

            for color_idx in range(24, 40):
                if self.stop_event.is_set():
                    self.app.thread_status("已停止。")
                    return
                pts = groups[color_idx]
                if not pts:
                    continue

                px, py = self._palette_click_pos(color_idx, ox, oy, w, h, bottom_mode=True)
                self._ensure_surface_stable()
                game_mouse.click(px, py)
                time.sleep(max(0.08, click_delay))

                for x, y in pts:
                    if self.stop_event.is_set():
                        self.app.thread_status("已停止。")
                        return
                    gx, gy = self._grid_center(x, y, w, h)
                    self._ensure_surface_stable()
                    game_mouse.click(ox + gx, oy + gy)
                    done += 1
                    self.app.thread_progress(done, total_cells, color_idx)
                    time.sleep(click_delay)

            self.app.thread_status(
                f"完成：已填充 {done} 个格子，使用 {len(used_colors)} 种颜色；"
                f"定位：{calibration_summary}。"
            )
            self.app.thread_progress(total_cells, total_cells, -1)

            try:
                import winsound
                winsound.MessageBeep(winsound.MB_OK)
            except Exception:
                pass

        except EmergencyStop:
            self.app.thread_status("已按 F8 紧急停止。")
        except Exception as e:
            self.app.thread_status(f"自动填充失败：{e}", error=True)


# ---------------------------- 交互式裁切 ----------------------------
class CropDialog(tk.Toplevel):
    """Square crop editor whose result is expressed in source-image pixels."""

    def __init__(self, parent, image, initial_box=None):
        super().__init__(parent)
        self.parent = parent
        self.source = flatten_alpha(image)
        self.result = None
        self.ui_scale = parent.ui_scale
        self.title("裁切图片")
        self.configure(bg=parent.CARD)
        self.resizable(False, False)
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self._cancel)

        screen_w = max(800, parent.work_area[2] - parent.work_area[0])
        screen_h = max(600, parent.work_area[3] - parent.work_area[1])
        max_w = min(round(820 * self.ui_scale), screen_w - round(100 * self.ui_scale))
        max_h = min(round(560 * self.ui_scale), screen_h - round(180 * self.ui_scale))
        self.display_scale = min(max_w / self.source.width, max_h / self.source.height)
        self.display_scale = max(0.05, self.display_scale)
        self.display_w = max(180, round(self.source.width * self.display_scale))
        self.display_h = max(140, round(self.source.height * self.display_scale))

        header = tk.Frame(self, bg=parent.CARD, padx=parent._u(16), pady=parent._u(12))
        header.pack(fill="x")
        tk.Label(header, text="裁切图片", bg=parent.CARD, fg=parent.TEXT,
                 font=("Microsoft YaHei UI", 13, "bold")).pack(anchor="w")
        tk.Label(
            header,
            text="拖动框内移动 · 拖动四角调整 · 滚轮缩放裁切框 · 固定 1:1",
            bg=parent.CARD,
            fg=parent.MUTED,
            font=("Microsoft YaHei UI", 9),
        ).pack(anchor="w", pady=(parent._u(3), 0))

        shell = tk.Frame(self, bg=parent.BORDER, padx=1, pady=1)
        shell.pack(padx=parent._u(16), fill="both")
        self.canvas = tk.Canvas(
            shell,
            width=self.display_w,
            height=self.display_h,
            bg="#111820",
            highlightthickness=0,
            cursor="crosshair",
        )
        self.canvas.pack()
        preview = self.source.resize((self.display_w, self.display_h), Image.Resampling.LANCZOS)
        self.photo = ImageTk.PhotoImage(preview)
        self.canvas.create_image(0, 0, anchor="nw", image=self.photo, tags=("source",))

        controls = tk.Frame(self, bg=parent.CARD, padx=parent._u(16), pady=parent._u(12))
        controls.pack(fill="x")
        ttk.Button(controls, text="重置裁切", style="Secondary.TButton",
                   command=self._reset).pack(side="left")
        ttk.Button(controls, text="取消", style="Secondary.TButton",
                   command=self._cancel).pack(side="right")
        ttk.Button(controls, text="应用裁切", style="Primary.TButton",
                   command=self._confirm).pack(side="right", padx=(0, parent._u(8)))

        self.handle_radius = max(7, parent._u(7))
        self.min_side = max(28, parent._u(28))
        self.drag_mode = None
        self.drag_handle = None
        self.drag_offset = (0.0, 0.0)
        self.crop = self._box_to_display(initial_box) if initial_box else self._default_crop()
        self._redraw_overlay()

        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<MouseWheel>", self._on_wheel)
        self.bind("<Escape>", lambda _event: self._cancel())
        self.bind("<Return>", lambda _event: self._confirm())

        self.update_idletasks()
        x = parent.winfo_rootx() + max(0, (parent.winfo_width() - self.winfo_width()) // 2)
        y = parent.winfo_rooty() + max(0, (parent.winfo_height() - self.winfo_height()) // 2)
        self.geometry(f"+{x}+{y}")
        self.grab_set()
        self.focus_force()

    def _default_crop(self):
        side = min(self.display_w, self.display_h)
        return [
            (self.display_w - side) / 2.0,
            (self.display_h - side) / 2.0,
            (self.display_w + side) / 2.0,
            (self.display_h + side) / 2.0,
        ]

    def _box_to_display(self, box):
        left, top, right, bottom = box
        left *= self.display_w / self.source.width
        right *= self.display_w / self.source.width
        top *= self.display_h / self.source.height
        bottom *= self.display_h / self.source.height
        side = min(right - left, bottom - top)
        return self._clamp_box(left, top, side)

    def _clamp_box(self, left, top, side):
        side = max(self.min_side, min(float(side), self.display_w, self.display_h))
        left = max(0.0, min(float(left), self.display_w - side))
        top = max(0.0, min(float(top), self.display_h - side))
        return [left, top, left + side, top + side]

    def _redraw_overlay(self):
        self.canvas.delete("crop_overlay")
        left, top, right, bottom = self.crop
        shade = {"fill": "#000000", "stipple": "gray50", "outline": "", "tags": ("crop_overlay",)}
        self.canvas.create_rectangle(0, 0, self.display_w, top, **shade)
        self.canvas.create_rectangle(0, bottom, self.display_w, self.display_h, **shade)
        self.canvas.create_rectangle(0, top, left, bottom, **shade)
        self.canvas.create_rectangle(right, top, self.display_w, bottom, **shade)
        self.canvas.create_rectangle(
            left, top, right, bottom,
            outline=self.parent.ACCENT,
            width=max(2, self.parent._u(2)),
            tags=("crop_overlay",),
        )
        radius = self.handle_radius
        for x, y in ((left, top), (right, top), (right, bottom), (left, bottom)):
            self.canvas.create_rectangle(
                x - radius, y - radius, x + radius, y + radius,
                fill=self.parent.ORANGE, outline="#ffffff", width=1,
                tags=("crop_overlay",),
            )

    def _handle_at(self, x, y):
        left, top, right, bottom = self.crop
        handles = {"nw": (left, top), "ne": (right, top), "se": (right, bottom), "sw": (left, bottom)}
        for name, (hx, hy) in handles.items():
            if abs(x - hx) <= self.handle_radius * 1.6 and abs(y - hy) <= self.handle_radius * 1.6:
                return name
        return None

    def _on_press(self, event):
        handle = self._handle_at(event.x, event.y)
        if handle:
            self.drag_mode = "resize"
            self.drag_handle = handle
            left, top, right, bottom = self.crop
            opposite = {"nw": (right, bottom), "ne": (left, bottom),
                        "se": (left, top), "sw": (right, top)}
            self.resize_anchor = opposite[handle]
            return
        left, top, right, bottom = self.crop
        if left <= event.x <= right and top <= event.y <= bottom:
            self.drag_mode = "move"
            self.drag_offset = (event.x - left, event.y - top)
            return
        side = right - left
        self.crop = self._clamp_box(event.x - side / 2.0, event.y - side / 2.0, side)
        self.drag_mode = "move"
        self.drag_offset = (side / 2.0, side / 2.0)
        self._redraw_overlay()

    def _on_drag(self, event):
        if self.drag_mode == "move":
            side = self.crop[2] - self.crop[0]
            self.crop = self._clamp_box(event.x - self.drag_offset[0], event.y - self.drag_offset[1], side)
        elif self.drag_mode == "resize":
            anchor_x, anchor_y = self.resize_anchor
            signs = {"nw": (-1, -1), "ne": (1, -1), "se": (1, 1), "sw": (-1, 1)}
            sign_x, sign_y = signs[self.drag_handle]
            requested = max(abs(event.x - anchor_x), abs(event.y - anchor_y), self.min_side)
            max_x = anchor_x if sign_x < 0 else self.display_w - anchor_x
            max_y = anchor_y if sign_y < 0 else self.display_h - anchor_y
            side = min(requested, max_x, max_y)
            corner_x = anchor_x + sign_x * side
            corner_y = anchor_y + sign_y * side
            self.crop = [min(anchor_x, corner_x), min(anchor_y, corner_y),
                         max(anchor_x, corner_x), max(anchor_y, corner_y)]
        self._redraw_overlay()

    def _on_release(self, _event):
        self.drag_mode = None
        self.drag_handle = None

    def _on_wheel(self, event):
        left, top, right, bottom = self.crop
        center_x = (left + right) / 2.0
        center_y = (top + bottom) / 2.0
        factor = 0.90 if event.delta > 0 else 1.10
        side = (right - left) * factor
        self.crop = self._clamp_box(center_x - side / 2.0, center_y - side / 2.0, side)
        self._redraw_overlay()

    def _reset(self):
        self.crop = self._default_crop()
        self._redraw_overlay()

    def _confirm(self):
        left, top, right, bottom = self.crop
        scale_x = self.source.width / self.display_w
        scale_y = self.source.height / self.display_h
        result = (
            max(0, round(left * scale_x)),
            max(0, round(top * scale_y)),
            min(self.source.width, round(right * scale_x)),
            min(self.source.height, round(bottom * scale_y)),
        )
        if result[2] - result[0] < 2 or result[3] - result[1] < 2:
            messagebox.showwarning("裁切区域过小", "请选择更大的裁切区域。", parent=self)
            return
        self.result = result
        self.destroy()

    def _cancel(self):
        self.result = None
        self.destroy()


class ScreenCaptureDialog(tk.Toplevel):
    """Full virtual-desktop rectangle selector backed by a frozen screenshot."""

    def __init__(self, parent, screenshot, virtual_bounds):
        super().__init__(parent)
        self.result = None
        self.screenshot = screenshot.convert("RGB")
        self.virtual_left, self.virtual_top, virtual_w, virtual_h = virtual_bounds
        self.start = None
        self.selection_id = None
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.geometry(
            f"{virtual_w}x{virtual_h}{self.virtual_left:+d}{self.virtual_top:+d}"
        )
        self.configure(bg="#000000")

        self.canvas = tk.Canvas(
            self,
            width=virtual_w,
            height=virtual_h,
            highlightthickness=0,
            bd=0,
            cursor="crosshair",
        )
        self.canvas.pack(fill="both", expand=True)
        display = self.screenshot
        if display.size != (virtual_w, virtual_h):
            display = display.resize((virtual_w, virtual_h), Image.Resampling.BILINEAR)
        self.photo = ImageTk.PhotoImage(display)
        self.canvas.create_image(0, 0, anchor="nw", image=self.photo)
        self.canvas.create_rectangle(
            18, 18, 515, 64,
            fill="#102431", outline="#66d9ef", width=2,
        )
        self.canvas.create_text(
            34, 41,
            anchor="w",
            text="拖动选择截图区域 · 松开完成 · Esc / 右键取消",
            fill="#edf6f7",
            font=("Microsoft YaHei UI", 12, "bold"),
        )
        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._drag)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        self.canvas.bind("<Button-3>", lambda _event: self._cancel())
        self.bind("<Escape>", lambda _event: self._cancel())
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.grab_set()
        self.focus_force()

    def _press(self, event):
        self.start = (event.x, event.y)
        if self.selection_id is not None:
            self.canvas.delete(self.selection_id)
        self.selection_id = self.canvas.create_rectangle(
            event.x, event.y, event.x, event.y,
            outline="#f4a23a", width=3,
        )

    def _drag(self, event):
        if self.start is None:
            return
        self.canvas.coords(self.selection_id, self.start[0], self.start[1], event.x, event.y)

    def _release(self, event):
        if self.start is None:
            return
        left, right = sorted((self.start[0], event.x))
        top, bottom = sorted((self.start[1], event.y))
        if right - left < 12 or bottom - top < 12:
            self.start = None
            return
        scale_x = self.screenshot.width / max(1, self.winfo_width())
        scale_y = self.screenshot.height / max(1, self.winfo_height())
        box = (
            max(0, round(left * scale_x)),
            max(0, round(top * scale_y)),
            min(self.screenshot.width, round(right * scale_x)),
            min(self.screenshot.height, round(bottom * scale_y)),
        )
        self.result = self.screenshot.crop(box)
        self.destroy()

    def _cancel(self):
        self.result = None
        self.destroy()


# ---------------------------- GUI ----------------------------
class App(tk.Tk):
    BASE_PREVIEW_SIZE = 408
    MIN_PREVIEW_SIZE = 168
    COLOR_GUIDE_MARGIN = 28
    BASE_WINDOW_W = 1180
    BASE_WINDOW_H = 760
    MIN_WINDOW_W = 820
    MIN_WINDOW_H = 600
    BG = "#17384d"
    BODY_BG = "#285f7d"
    HERO_BG = "#2f7fa7"
    CARD = "#122331"
    CARD_ALT = "#1a3444"
    BORDER = "#5f8799"
    TEXT = "#edf6f7"
    MUTED = "#a9c3ce"
    ACCENT = "#66d9ef"
    ACCENT_DARK = "#228eb0"
    ORANGE = "#f4a23a"
    DANGER = "#e0656b"

    def __init__(self):
        super().__init__()
        self.title("明日方舟 24×24 像素画自动填色")
        self.work_area, self.ui_scale = self._calculate_ui_scale()
        # Keep fonts, explicit widget dimensions and the native window on the
        # same scale.  This avoids the high-DPI state where fonts grow but the
        # 1180x760 outer window remains too small and clips the palette.
        self.tk.call("tk", "scaling", (4.0 / 3.0) * self.ui_scale)
        self.PREVIEW_SIZE = self._u(self.BASE_PREVIEW_SIZE)
        self.WINDOW_W = self._u(self.BASE_WINDOW_W)
        self.WINDOW_H = self._u(self.BASE_WINDOW_H)
        # Keep a genuine top-level window.  Tk's overrideredirect mode creates
        # a popup that Windows 10/11 may omit from the taskbar even when
        # WS_EX_APPWINDOW is added later.  The native frame is removed below
        # after the HWND has been created, preserving normal taskbar semantics.
        self.overrideredirect(False)
        self.geometry(f"{self.WINDOW_W}x{self.WINDOW_H}")
        self.resizable(True, True)
        self.minsize(self._u(self.MIN_WINDOW_W), self._u(self.MIN_WINDOW_H))
        self.configure(bg=self.BG)

        icon = tk.PhotoImage(width=32, height=32)
        icon.put(self.BG, to=(0, 0, 32, 32))
        icon.put(self.ACCENT, to=(5, 5, 27, 27))
        icon.put(self.CARD, to=(9, 9, 23, 23))
        icon.put(self.ACCENT, to=(13, 13, 19, 19))
        self.iconphoto(True, icon)
        self._window_icon = icon

        self.source_image = None
        self.crop_box = None
        self.direct_bitmap_mode = False
        self.official_share_mode = False
        self.source_file_name = ""
        self.matrix = [[3] * N for _ in range(N)]
        self.original_matrix = [row[:] for row in self.matrix]
        self.edit_history = []
        self._active_stroke = []
        self._stroke_cells = set()
        self.selected_color_index = 0
        self.preview_photo = None
        self.mini_preview_photo = None
        self.painter = AutoPainter(self)

        self.window_choice = tk.StringVar(value="请点击刷新并选择游戏窗口")
        self.window_choices = {}
        self.fit_mode = tk.StringVar(value="crop")
        self.resample_mode = tk.StringVar(value="Lanczos（原版）")
        self.color_match_mode = tk.StringVar(value="经典RGB（原版）")
        self.resample_strength = tk.DoubleVar(value=100.0)
        self.match_strength = tk.DoubleVar(value=100.0)
        self.structure_strength = tk.DoubleVar(value=0.0)
        self.dither = tk.BooleanVar(value=False)
        self.reduce_transitions = tk.BooleanVar(value=False)
        self.click_delay = tk.DoubleVar(value=0.060)
        self.skip_white = tk.BooleanVar(value=True)
        self.show_color_numbers = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value="请选择一张图片。")
        self.file_var = tk.StringVar(value="尚未选择图片")
        self.palette_info_var = tk.StringVar(value="40 色游戏调色板")
        self.progress_var = tk.DoubleVar(value=0.0)
        self._tracer_logo_source = None
        self.tracer_logo_photo = self._load_tracer_logo()
        self.selected_color_var = tk.StringVar(value="")
        self._drag_offset = (0, 0)
        self._native_hwnd = None
        self._responsive_after = None
        self._reprocess_after = None
        self._compact_layout = None
        self._layout_signature = None
        self._latest_release = None

        self._build_ui()
        self._on_algorithm_slider()
        self._render_preview()
        self._draw_palette()
        self._select_palette_color(0)
        self.update_idletasks()
        self._apply_native_window_geometry()
        self.after(80, self._configure_taskbar_window)
        self.after(180, self.refresh_window_list)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.bind("<Map>", self._restore_borderless)
        self.bind("<Configure>", self._on_window_configure)
        self.after(1400, self._start_update_check)

    def _u(self, value):
        return max(1, round(float(value) * self.ui_scale))

    @staticmethod
    def _primary_work_area():
        rect = wintypes.RECT()
        if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):
            return rect.left, rect.top, rect.right, rect.bottom
        width = ctypes.windll.user32.GetSystemMetrics(0)
        height = ctypes.windll.user32.GetSystemMetrics(1)
        return 0, 0, width, height

    def _calculate_ui_scale(self):
        work_area = self._primary_work_area()
        work_w = max(1, work_area[2] - work_area[0])
        work_h = max(1, work_area[3] - work_area[1])
        try:
            dpi = ctypes.windll.user32.GetDpiForSystem()
        except Exception:
            dpi = 96
        return work_area, self._ui_scale_for(work_w, work_h, dpi)

    @classmethod
    def _ui_scale_for(cls, work_w, work_h, dpi=96):
        return calculate_ui_scale(
            work_w, work_h, dpi, base_size=(cls.BASE_WINDOW_W, cls.BASE_WINDOW_H)
        )

    def _outer_hwnd(self):
        hwnd = int(self.winfo_id())
        parent = ctypes.windll.user32.GetParent(hwnd)
        return int(parent or hwnd)

    def _apply_native_window_geometry(self):
        left, top, right, bottom = self.work_area
        work_w = right - left
        work_h = bottom - top
        x = left + max(0, (work_w - self.WINDOW_W) // 2)
        y = top + max(0, (work_h - self.WINDOW_H) // 2)
        self.geometry(f"{self.WINDOW_W}x{self.WINDOW_H}+{x}+{y}")
        self.update_idletasks()
        hwnd = self._outer_hwnd()
        self._native_hwnd = hwnd
        ctypes.windll.user32.SetWindowPos(
            hwnd,
            0,
            x,
            y,
            self.WINDOW_W,
            self.WINDOW_H,
            0x0004 | 0x0010 | 0x0020,
        )

    def _configure_taskbar_window(self):
        if not self.winfo_exists():
            return
        self.update_idletasks()
        hwnd = self._outer_hwnd()
        self._native_hwnd = hwnd
        user32 = ctypes.windll.user32
        ex_style = win32gui.GetWindowLong(hwnd, -20)  # GWL_EXSTYLE
        ex_style = (ex_style & ~0x00000080) | 0x00040000  # remove TOOLWINDOW, add APPWINDOW
        win32gui.SetWindowLong(hwnd, -20, ex_style)
        style = win32gui.GetWindowLong(hwnd, -16)  # GWL_STYLE
        # Remove only the system-drawn caption.  Keep the sizing frame so the
        # invisible native edges retain Windows resize/maximize behaviour.
        WS_CAPTION = 0x00C00000
        WS_THICKFRAME = 0x00040000
        WS_MAXIMIZEBOX = 0x00010000
        WS_CHILD = 0x40000000
        WS_MINIMIZEBOX = 0x00020000
        WS_SYSMENU = 0x00080000
        style = (
            (style & ~WS_CAPTION & ~WS_CHILD)
            | WS_THICKFRAME
            | WS_MAXIMIZEBOX
            | WS_MINIMIZEBOX
            | WS_SYSMENU
        )
        win32gui.SetWindowLong(hwnd, -16, style)
        # SWP_FRAMECHANGED makes both DWM and the shell re-evaluate the window.
        user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0004 | 0x0010 | 0x0020)

    def _build_ui(self):
        style = ttk.Style(self)
        self.ui_style = style
        style.theme_use("clam")
        style.configure(".", font=("Microsoft YaHei UI", 10))
        style.configure("App.TFrame", background=self.BG)
        style.configure("Body.TFrame", background=self.BODY_BG)
        style.configure("Card.TFrame", background=self.CARD)
        style.configure("CardAlt.TFrame", background=self.CARD_ALT)
        style.configure("Title.TLabel", background=self.CARD, foreground=self.TEXT,
                        font=("Microsoft YaHei UI", 13, "bold"))
        style.configure("Subtitle.TLabel", background=self.BG, foreground=self.MUTED,
                        font=("Microsoft YaHei UI", 9))
        style.configure("Section.TLabel", background=self.CARD, foreground=self.TEXT,
                        font=("Microsoft YaHei UI", 11, "bold"))
        style.configure("Card.TLabel", background=self.CARD, foreground=self.TEXT)
        style.configure("Muted.TLabel", background=self.CARD, foreground=self.MUTED,
                        font=("Microsoft YaHei UI", 9))
        style.configure("Hint.TLabel", background=self.CARD_ALT, foreground=self.MUTED,
                        font=("Microsoft YaHei UI", 9))
        style.configure("Legal.TLabel", background=self.CARD_ALT, foreground="#b8c9cf",
                        font=("Microsoft YaHei UI", 8))
        style.configure("Badge.TLabel", background="#17353a", foreground=self.ACCENT,
                        font=("Segoe UI", 9, "bold"), padding=(10, 5))
        style.configure("Primary.TButton", background=self.ORANGE, foreground="#17212a",
                        borderwidth=0, padding=(14, 11), font=("Microsoft YaHei UI", 10, "bold"))
        style.map("Primary.TButton", background=[("active", "#ffc56d"), ("pressed", "#d98520")])
        style.configure("Secondary.TButton", background=self.CARD_ALT, foreground=self.TEXT,
                        bordercolor=self.BORDER, borderwidth=1, padding=(12, 9))
        style.map("Secondary.TButton", background=[("active", "#27536a"), ("pressed", "#102330")])
        style.configure("Compact.TButton", background=self.CARD_ALT, foreground=self.TEXT,
                        bordercolor=self.BORDER, borderwidth=1, padding=(8, 6),
                        font=("Microsoft YaHei UI", 9))
        style.map("Compact.TButton", background=[("active", "#27536a"), ("pressed", "#102330")])
        style.configure("Danger.TButton", background="#40252c", foreground="#ff9ca1",
                        borderwidth=0, padding=(12, 9))
        style.map("Danger.TButton", background=[("active", "#563038"), ("pressed", "#321c22")])
        style.configure("Update.TButton", background=self.ACCENT, foreground="#102431",
                        borderwidth=0, padding=(10, 6),
                        font=("Microsoft YaHei UI", 9, "bold"))
        style.map("Update.TButton", background=[("active", "#a8f2ff"), ("pressed", "#35bad6")])
        style.configure("Card.TCheckbutton", background=self.CARD, foreground=self.TEXT,
                        indicatorcolor=self.CARD_ALT, padding=(0, 4))
        style.map("Card.TCheckbutton", background=[("active", self.CARD)],
                  indicatorcolor=[("selected", self.ACCENT)])
        style.configure("CardAlt.TCheckbutton", background=self.CARD_ALT, foreground=self.TEXT,
                        indicatorcolor=self.CARD, padding=(0, 2))
        style.map("CardAlt.TCheckbutton", background=[("active", self.CARD_ALT)],
                  indicatorcolor=[("selected", self.ACCENT)])
        style.configure("Dark.TEntry", fieldbackground="#0e171f", foreground=self.TEXT,
                        insertcolor=self.TEXT, bordercolor=self.BORDER, padding=7)
        style.configure("Dark.TCombobox", fieldbackground="#0e171f", background=self.CARD_ALT,
                        foreground=self.TEXT, arrowcolor=self.ACCENT, bordercolor=self.BORDER, padding=5)
        style.map("Dark.TCombobox", fieldbackground=[("readonly", "#0e171f")],
                  foreground=[("readonly", self.TEXT)])
        style.configure("Dark.Horizontal.TScale", background=self.CARD, troughcolor="#0e171f")
        style.configure("Accent.Horizontal.TProgressbar", troughcolor="#0e171f",
                        background=self.ACCENT, borderwidth=0, thickness=8)
        style.configure("Dark.TSeparator", background=self.BORDER)

        self.option_add("*TCombobox*Listbox.background", "#101922")
        self.option_add("*TCombobox*Listbox.foreground", self.TEXT)
        self.option_add("*TCombobox*Listbox.selectBackground", self.ACCENT_DARK)

        root = tk.Frame(self, bg=self.BG, highlightthickness=1,
                        highlightbackground="#8bc7dc")
        root.pack(fill="both", expand=True)

        # 自定义无边框标题栏
        titlebar = tk.Frame(root, bg="#101d28", height=self._u(38))
        titlebar.pack(fill="x")
        titlebar.pack_propagate(False)
        title_icon = tk.Label(titlebar, image=self._window_icon, bg="#101d28")
        title_icon.pack(side="left", padx=(10, 7))
        title_text = tk.Label(titlebar, text=f"ARKNIGHTS  /  24×24 PIXEL LAB  v{APP_VERSION}",
                              bg="#101d28", fg="#d9eef5",
                              font=("Segoe UI", 9, "bold"))
        title_text.pack(side="left")
        tk.Label(titlebar, text="TRACER · B站发布", bg="#101d28", fg=self.ACCENT,
                 font=("Microsoft YaHei UI", 8, "bold")).pack(side="right", padx=(0, 14))
        close_btn = tk.Button(titlebar, text="×", command=self._on_close, bg="#101d28",
                              fg="white", activebackground="#d95863", activeforeground="white",
                              relief="flat", bd=0, font=("Segoe UI", 17), width=3, cursor="hand2")
        close_btn.pack(side="right")
        max_btn = tk.Button(titlebar, text="□", command=self._toggle_maximize, bg="#101d28",
                            fg="white", activebackground="#294b5d", activeforeground="white",
                            relief="flat", bd=0, font=("Segoe UI", 11), width=3, cursor="hand2")
        max_btn.pack(side="right")
        min_btn = tk.Button(titlebar, text="—", command=self._minimize_window, bg="#101d28",
                            fg="white", activebackground="#294b5d", activeforeground="white",
                            relief="flat", bd=0, font=("Segoe UI", 12), width=3, cursor="hand2")
        min_btn.pack(side="right")
        for widget in (titlebar, title_icon, title_text):
            widget.bind("<ButtonPress-1>", self._begin_drag)
            widget.bind("<B1-Motion>", self._drag_window)
            widget.bind("<Double-Button-1>", lambda _event: self._toggle_maximize())

        # 简洁标题区；不再叠加旅行主题图案，避免干扰产品标题。
        hero = tk.Canvas(root, height=self._u(90), bg=self.HERO_BG, highlightthickness=0)
        self.hero_canvas = hero
        hero.pack(fill="x")
        self.hero_band_item = hero.create_rectangle(
            0, self._u(64), self.WINDOW_W, self._u(90), fill="#286d91", outline=""
        )
        self.hero_kicker_item = hero.create_text(
            self._u(24), self._u(18), text="ARKNIGHTS  /  PIXEL STUDIO", anchor="w",
            fill="#d9f5ff", font=("Segoe UI", 9, "bold")
        )
        self.hero_title_item = hero.create_text(
            self._u(24), self._u(49), text="24×24 像素画工坊", anchor="w",
            fill="white", font=("Microsoft YaHei UI", 21, "bold")
        )
        self.hero_subtitle_item = hero.create_text(
            self._u(380), self._u(52), text="上传 · 量化 · 手动修整 · 自动绘制", anchor="w",
            fill="#cde9f4", font=("Microsoft YaHei UI", 10)
        )
        self.hero_logo_item = None
        if self.tracer_logo_photo is not None:
            self.hero_logo_item = hero.create_image(
                max(self._u(140), hero.winfo_width() - self._u(24)),
                self._u(45), image=self.tracer_logo_photo, anchor="e"
            )
        hero.bind("<Configure>", self._on_hero_configure)

        # 底部固定免责声明，避免窗口缩放时被主体区域挤出
        legal = tk.Frame(root, bg="#102431", height=self._u(48))
        self.legal_bar = legal
        legal.pack(side="bottom", fill="x")
        legal.pack_propagate(False)
        self.legal_label = tk.Label(
            legal,
            text=("免责声明｜本工具为用户上传并于B站发布，请自行甄别、谨慎使用。严禁制作、上传或传播任何与国家法律法规及政策相抵触的内容；\n"
                  "使用者自行承担使用风险及后果。  ·  建议游戏客户区 1280×720  ·  F8 紧急停止"),
            bg="#102431", fg="#b8d1dc", justify="left",
            font=("Microsoft YaHei UI", 8), padx=self._u(16),
        )
        self.legal_label.pack(side="left", fill="both", expand=True)
        self.update_button = ttk.Button(
            legal,
            text="发现新版本",
            style="Update.TButton",
            command=self._offer_release_download,
            cursor="hand2",
        )

        body = tk.Frame(root, bg=self.BODY_BG, padx=self._u(14), pady=self._u(10))
        self.body = body
        body.pack(fill="both", expand=True)

        left_shell = ttk.Frame(body, style="Card.TFrame", width=self._u(270))
        self.left_shell = left_shell
        left_shell.pack(side="left", fill="y")
        left_shell.pack_propagate(False)

        # One fixed responsive column: controls and actions share the same
        # proportional scale, so enlarging the window never creates a large
        # artificial gap between click delay and the start button.
        left = ttk.Frame(left_shell, style="Card.TFrame", padding=self._u(10))
        left.pack(fill="both", expand=True)
        self.left_content = left

        ttk.Label(left, text="01  图片处理", style="Section.TLabel").pack(anchor="w")
        self.file_label = ttk.Label(left, textvariable=self.file_var, style="Muted.TLabel")
        self.file_label.pack(anchor="w", fill="x", pady=(2, 6))
        image_buttons = ttk.Frame(left, style="Card.TFrame")
        self.image_buttons = image_buttons
        image_buttons.pack(fill="x", pady=(0, 4))
        image_buttons.columnconfigure(0, weight=1, uniform="image-actions")
        image_buttons.columnconfigure(1, minsize=self._u(6))
        image_buttons.columnconfigure(2, weight=1, uniform="image-actions")
        def equalize_image_actions(event):
            gap = self._u(6)
            width = max(1, (event.width - gap) // 2)
            image_buttons.columnconfigure(0, weight=0, minsize=width)
            image_buttons.columnconfigure(2, weight=0, minsize=width)
        image_buttons.bind("<Configure>", equalize_image_actions)
        ttk.Button(image_buttons, text="＋  选择图片", style="Secondary.TButton",
                   command=self.open_image).grid(row=0, column=0, sticky="ew")
        self.crop_button = ttk.Button(
            image_buttons,
            text="✂  裁切",
            style="Secondary.TButton",
            command=self.open_crop_editor,
            state="disabled",
        )
        self.crop_button.grid(row=0, column=2, sticky="ew")

        import_tools = ttk.Frame(left, style="Card.TFrame")
        import_tools.pack(fill="x", pady=(0, 4))
        self.bitmap_button = ttk.Button(
            import_tools,
            text="▦  24×24位图",
            style="Secondary.TButton",
            command=self.open_24_bitmap,
        )
        import_tools.columnconfigure(0, weight=1, uniform="import-actions")
        import_tools.columnconfigure(1, weight=1, uniform="import-actions")
        import_tools.columnconfigure(2, weight=1, uniform="import-actions")
        self.bitmap_button.grid(row=0, column=0, sticky="ew")
        self.share_button = ttk.Button(
            import_tools,
            text="▦  官方分享",
            style="Secondary.TButton",
            command=self.open_official_share,
        )
        self.share_button.grid(row=0, column=1, sticky="ew", padx=(self._u(4), 0))
        self.capture_button = ttk.Button(
            import_tools,
            text="▣  截图导入",
            style="Secondary.TButton",
            command=self.capture_screen,
        )
        self.capture_button.grid(row=0, column=2, sticky="ew", padx=(self._u(4), 0))

        self.fit_mode_label = ttk.Label(left, text="缩放方式", style="Muted.TLabel")
        self.fit_mode_label.pack(anchor="w")
        self.mode_box = ttk.Combobox(
            left,
            textvariable=self.fit_mode,
            state="readonly",
            values=("crop", "contain", "stretch"),
            style="Dark.TCombobox",
        )
        self.mode_box.pack(fill="x", pady=(2, 3))
        self.mode_box.bind("<<ComboboxSelected>>", lambda e: self.reprocess())

        self.resample_mode_label = ttk.Label(left, text="缩放取样算法", style="Muted.TLabel")
        self.resample_mode_label.pack(anchor="w")
        self.resample_box = ttk.Combobox(
            left,
            textvariable=self.resample_mode,
            state="readonly",
            values=tuple(RESAMPLE_METHODS),
            style="Dark.TCombobox",
        )
        self.resample_box.pack(fill="x", pady=(2, 3))
        self.resample_box.bind("<<ComboboxSelected>>", lambda _event: self.reprocess())

        resample_strength_line = ttk.Frame(left, style="Card.TFrame")
        resample_strength_line.pack(fill="x", pady=(1, 4))
        ttk.Label(resample_strength_line, text="取样平滑比例", style="Muted.TLabel").pack(side="left")
        self.resample_strength_label = ttk.Label(
            resample_strength_line, text="100%", style="Badge.TLabel"
        )
        self.resample_strength_label.pack(side="right")
        self.resample_scale = ttk.Scale(
            resample_strength_line,
            from_=0,
            to=100,
            variable=self.resample_strength,
            command=lambda _value: self._on_algorithm_slider(),
            orient="horizontal",
            style="Dark.Horizontal.TScale",
        )
        self.resample_scale.pack(side="left", fill="x", expand=True, padx=(self._u(5), self._u(5)))

        self.match_mode_label = ttk.Label(left, text="颜色匹配算法", style="Muted.TLabel")
        self.match_mode_label.pack(anchor="w")
        self.match_box = ttk.Combobox(
            left,
            textvariable=self.color_match_mode,
            state="readonly",
            values=("经典RGB（原版）", "OKLab（感知）"),
            style="Dark.TCombobox",
        )
        self.match_box.pack(fill="x", pady=(2, 3))
        self.match_box.bind("<<ComboboxSelected>>", lambda _event: self.reprocess())

        match_strength_line = ttk.Frame(left, style="Card.TFrame")
        match_strength_line.pack(fill="x", pady=(1, 4))
        ttk.Label(match_strength_line, text="所选匹配算法比例", style="Muted.TLabel").pack(side="left")
        self.match_strength_label = ttk.Label(
            match_strength_line, text="100%", style="Badge.TLabel"
        )
        self.match_strength_label.pack(side="right")
        self.match_scale = ttk.Scale(
            match_strength_line,
            from_=0,
            to=100,
            variable=self.match_strength,
            command=lambda _value: self._on_algorithm_slider(),
            orient="horizontal",
            style="Dark.Horizontal.TScale",
        )
        self.match_scale.pack(side="left", fill="x", expand=True, padx=(self._u(5), self._u(5)))

        self.dither_check = ttk.Checkbutton(
            left,
            text="Floyd–Steinberg 抖动",
            variable=self.dither,
            command=self.reprocess,
            style="Card.TCheckbutton",
        )
        self.dither_check.pack(anchor="w")

        self.transition_check = ttk.Checkbutton(
            left,
            text="减少过渡色（最多16色）",
            variable=self.reduce_transitions,
            command=self._toggle_reduce_transitions,
            style="Card.TCheckbutton",
        )
        self.transition_check.pack(anchor="w")

        structure_line = ttk.Frame(left, style="Card.TFrame")
        structure_line.pack(fill="x", pady=(3, 1))
        ttk.Label(structure_line, text="结构增强 / 去阴影", style="Muted.TLabel").pack(side="left")
        self.structure_strength_label = ttk.Label(
            structure_line, text="0%", style="Badge.TLabel"
        )
        self.structure_strength_label.pack(side="right")
        self.structure_scale = ttk.Scale(
            structure_line,
            from_=0,
            to=100,
            variable=self.structure_strength,
            command=lambda _value: self._on_algorithm_slider(),
            orient="horizontal",
            style="Dark.Horizontal.TScale",
        )
        self.structure_scale.pack(
            side="left", fill="x", expand=True, padx=(self._u(5), self._u(5))
        )

        ttk.Separator(left, style="Dark.TSeparator").pack(fill="x", pady=5)

        automation_header = ttk.Frame(left, style="Card.TFrame")
        automation_header.pack(fill="x")
        ttk.Label(
            automation_header, text="02  自动化设置", style="Section.TLabel"
        ).pack(side="left")
        self.window_picker_label = ttk.Label(
            automation_header,
            text="选择游戏窗口",
            style="Muted.TLabel",
        )
        self.window_picker_label.pack(side="right")
        window_line = ttk.Frame(left, style="Card.TFrame")
        window_line.pack(fill="x", pady=(4, 6))
        self.window_box = ttk.Combobox(
            window_line,
            textvariable=self.window_choice,
            state="readonly",
            style="Dark.TCombobox",
        )
        self.window_box.pack(side="left", fill="x", expand=True)
        self.window_box.bind("<<ComboboxSelected>>", self._on_window_selected)
        ttk.Button(
            window_line,
            text="刷新",
            command=self.refresh_window_list,
            style="Compact.TButton",
        ).pack(side="left", padx=(self._u(5), 0))

        speed_line = ttk.Frame(left, style="Card.TFrame")
        speed_line.pack(fill="x")
        ttk.Label(speed_line, text="每格点击间隔", style="Muted.TLabel").pack(side="left")
        self.delay_label = ttk.Label(speed_line, text="", style="Badge.TLabel")
        self.delay_label.pack(side="right")
        delay = ttk.Scale(speed_line, from_=0.04, to=0.12, variable=self.click_delay,
                          orient="horizontal", style="Dark.Horizontal.TScale")
        delay.pack(side="left", fill="x", expand=True, padx=(self._u(5), self._u(5)))
        self.click_delay.trace_add("write", lambda *_: self._update_delay_label())
        self._update_delay_label()

        left_action_bar = ttk.Frame(left, style="CardAlt.TFrame", padding=self._u(6))
        self.left_action_bar = left_action_bar
        left_action_bar.pack(fill="x", pady=(self._u(6), 0))
        self.skip_white_check = ttk.Checkbutton(
            left_action_bar,
            text="跳过白色格",
            variable=self.skip_white,
            style="CardAlt.TCheckbutton",
        )
        self.skip_white_check.pack(anchor="w", pady=(0, self._u(5)))
        action_button_row = ttk.Frame(left_action_bar, style="CardAlt.TFrame")
        self.action_button_row = action_button_row
        action_button_row.pack(fill="x")
        self.start_button = ttk.Button(
            action_button_row,
            text="▶  开始自动填充",
            style="Primary.TButton",
            command=self.start_paint,
        )
        self.start_button.pack(side="left", fill="x", expand=True)
        self.stop_button = ttk.Button(
            action_button_row, text="■  停止", style="Danger.TButton", command=self.stop_paint
        )
        self.stop_button.pack(side="left", padx=(self._u(5), 0))

        palette_panel = ttk.Frame(body, style="Card.TFrame", padding=self._u(12), width=self._u(214))
        self.palette_panel = palette_panel
        palette_panel.pack(side="right", fill="y")
        palette_panel.pack_propagate(False)
        palette_header = ttk.Frame(palette_panel, style="Card.TFrame")
        self.palette_header = palette_header
        palette_header.pack(side="top", fill="x")
        ttk.Label(palette_header, text="游戏调色板", style="Section.TLabel").pack(anchor="w")
        ttk.Label(palette_header, text="40 COLORS · 点击选择", style="Muted.TLabel").pack(
            anchor="w", pady=(1, 7))

        # Keep colour identity and usage help in a fixed footer; the swatch
        # canvas alone consumes the flexible middle height.
        palette_footer = ttk.Frame(palette_panel, style="Card.TFrame")
        self.palette_footer = palette_footer
        palette_footer.pack(side="bottom", fill="x")
        color_info = ttk.Frame(palette_footer, style="CardAlt.TFrame", padding=(8, 5))
        self.palette_color_info = color_info
        color_info.pack(fill="x", pady=(5, 0))
        self.current_color_caption = ttk.Label(color_info, text="当前颜色", style="Hint.TLabel")
        self.current_color_caption.pack(anchor="w")
        self.selected_color_label = ttk.Label(
            color_info, textvariable=self.selected_color_var, style="Badge.TLabel"
        )
        self.selected_color_label.pack(
            anchor="w", pady=(3, 0))
        self.palette_usage_hint = ttk.Label(
            palette_footer, text="左键/拖动涂色 · 右键吸色", style="Muted.TLabel"
        )
        self.palette_usage_hint.pack(
            anchor="center", pady=(7, 0))

        self.palette_canvas = tk.Canvas(palette_panel, width=self._u(176), height=self._u(370),
                                        bg=self.CARD, highlightthickness=0, cursor="hand2")
        self.palette_canvas.pack(side="top", anchor="center", fill="both", expand=True)
        self.palette_canvas.bind("<Button-1>", self._palette_click)
        self.palette_canvas.bind("<Configure>", lambda _event: self._draw_palette())

        center = ttk.Frame(body, style="Card.TFrame", padding=self._u(12))
        center.pack(side="left", fill="both", expand=True, padx=self._u(12))
        self.center_panel = center
        preview_head = ttk.Frame(center, style="Card.TFrame")
        self.preview_head = preview_head
        preview_head.pack(fill="x", pady=(0, 7))
        ttk.Label(preview_head, text="实时预览", style="Section.TLabel").pack(side="left")
        ttk.Checkbutton(
            preview_head,
            text="显示色号",
            variable=self.show_color_numbers,
            command=self._toggle_color_numbers,
            style="Card.TCheckbutton",
        ).pack(side="left", padx=(self._u(10), 0))
        ttk.Button(preview_head, text="恢复转换结果", style="Compact.TButton",
                   command=self._reset_manual_edits).pack(side="right")
        ttk.Button(preview_head, text="撤销", style="Compact.TButton",
                   command=self._undo_edit).pack(side="right", padx=(0, 6))
        self.palette_info_label = ttk.Label(
            preview_head,
            textvariable=self.palette_info_var,
            style="Badge.TLabel",
        )
        self.palette_info_label.pack(side="right")

        canvas_shell = tk.Frame(center, bg="#b8e8f5", padx=1, pady=1)
        canvas_shell.pack(anchor="center", expand=True)
        self.canvas_shell = canvas_shell
        self.canvas = tk.Canvas(canvas_shell, width=self.PREVIEW_SIZE,
                                height=self.PREVIEW_SIZE, bg="#f8f8f6",
                                highlightthickness=0, bd=0, cursor="crosshair")
        self.canvas.pack()
        self.canvas.bind("<ButtonPress-1>", self._start_preview_stroke)
        self.canvas.bind("<B1-Motion>", self._drag_preview_stroke)
        self.canvas.bind("<ButtonRelease-1>", self._finish_preview_stroke)
        self.canvas.bind("<Button-3>", self._pick_preview_color)

        info = ttk.Frame(center, style="Card.TFrame")
        self.preview_info = info
        info.pack(fill="x", pady=(8, 0))

        mini_shell = tk.Frame(info, bg=self.ACCENT, padx=1, pady=1)
        mini_shell.pack(side="left", padx=(0, self._u(9)))
        self.mini_preview_canvas = tk.Canvas(
            mini_shell,
            width=self._u(70),
            height=self._u(70),
            bg="#f8f8f6",
            highlightthickness=0,
            bd=0,
        )
        self.mini_preview_canvas.pack()

        info_text = ttk.Frame(info, style="Card.TFrame")
        info_text.pack(side="left", fill="both", expand=True)
        self.progress = ttk.Progressbar(info_text, variable=self.progress_var, maximum=100,
                                        style="Accent.Horizontal.TProgressbar")
        self.progress.pack(fill="x")
        ttk.Label(info_text, textvariable=self.status_var, style="Muted.TLabel",
                  wraplength=self._u(560)).pack(anchor="w", pady=(self._u(6), 0))

    def _load_tracer_logo(self):
        try:
            with Image.open(resource_path("assets", "tracer_logo.png")) as logo:
                self._tracer_logo_source = logo.convert("RGBA").copy()
                prepared = self._tracer_logo_source.copy()
                prepared.thumbnail((self._u(110), self._u(68)), Image.Resampling.LANCZOS)
                return ImageTk.PhotoImage(prepared)
        except Exception:
            return None

    def _resize_tracer_logo(self, layout_scale):
        if self._tracer_logo_source is None or self.hero_logo_item is None:
            return
        prepared = self._tracer_logo_source.copy()
        prepared.thumbnail(
            (
                self._u(max(78, round(110 * layout_scale))),
                self._u(max(48, round(68 * layout_scale))),
            ),
            Image.Resampling.LANCZOS,
        )
        self.tracer_logo_photo = ImageTk.PhotoImage(prepared)
        self.hero_canvas.itemconfigure(self.hero_logo_item, image=self.tracer_logo_photo)

    # ---------------------------- 无边框窗口 ----------------------------
    def _begin_drag(self, event):
        hwnd = self._native_hwnd or self._outer_hwnd()
        user32 = ctypes.windll.user32
        if user32.IsZoomed(hwnd):
            title_width = max(1, event.widget.winfo_toplevel().winfo_width())
            cursor_in_window = event.x_root - self.winfo_rootx()
            horizontal_ratio = max(0.0, min(1.0, cursor_in_window / title_width))
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            self.update_idletasks()
            offset_x = round(self.winfo_width() * horizontal_ratio)
            offset_y = min(event.y, self._u(36))
            x = event.x_root - offset_x
            y = event.y_root - offset_y
            user32.SetWindowPos(hwnd, 0, x, y, 0, 0, 0x0001 | 0x0004 | 0x0010)
            self._drag_offset = (offset_x, offset_y)
        else:
            self._drag_offset = (event.x_root - self.winfo_x(), event.y_root - self.winfo_y())

    def _drag_window(self, event):
        x = event.x_root - self._drag_offset[0]
        y = event.y_root - self._drag_offset[1]
        hwnd = self._native_hwnd or self._outer_hwnd()
        # Move the real top-level HWND.  Updating only Tk's geometry can leave
        # the native frame and packed content one event behind during dragging.
        ctypes.windll.user32.SetWindowPos(
            hwnd, 0, x, y, 0, 0, 0x0001 | 0x0004 | 0x0010
        )  # NOSIZE | NOZORDER | NOACTIVATE

    def _toggle_maximize(self):
        self.update_idletasks()
        hwnd = self._native_hwnd or self._outer_hwnd()
        user32 = ctypes.windll.user32
        user32.ShowWindow(hwnd, 9 if user32.IsZoomed(hwnd) else 3)  # RESTORE / MAXIMIZE
        self.after(30, self._configure_taskbar_window)

    def _on_window_configure(self, event):
        if event.widget is not self:
            return
        if self._responsive_after is not None:
            try:
                self.after_cancel(self._responsive_after)
            except tk.TclError:
                pass
        self._responsive_after = self.after(35, self._apply_responsive_layout)

    def _apply_responsive_layout(self):
        self._responsive_after = None
        if not hasattr(self, "center_panel") or not self.winfo_exists():
            return
        center_w = self.center_panel.winfo_width()
        center_h = self.center_panel.winfo_height()
        if center_w < 100 or center_h < 100:
            return

        metrics = responsive_metrics(self.winfo_width(), self.winfo_height(), self.ui_scale)
        self._apply_layout_density(metrics)

        reserved_h = (
            self.preview_head.winfo_height()
            + self.preview_info.winfo_height()
            + self._u(46)
        )
        available_w = center_w - self._u(34)
        available_h = center_h - reserved_h
        guide_margin = self._color_guide_margin()
        target = min(available_w, available_h) - guide_margin
        target = max(self._u(self.MIN_PREVIEW_SIZE), target)
        # An exact multiple of 24 keeps cell boundaries and pointer hit-testing
        # identical at every window size.
        target = max(N * 6, (int(target) // N) * N)
        if target != self.PREVIEW_SIZE:
            self.PREVIEW_SIZE = target
            self.canvas.configure(width=target, height=target)
            self._render_preview()
        self._draw_palette()

    def _apply_layout_density(self, metrics):
        """Resize fonts, buttons and side columns as one responsive system."""
        signature = (metrics.density, metrics.layout_scale)
        if signature == self._layout_signature:
            return
        self._layout_signature = signature
        self._compact_layout = metrics.density
        style = self.ui_style
        style.configure(".", font=("Microsoft YaHei UI", metrics.font_size))
        style.configure(
            "Section.TLabel", font=("Microsoft YaHei UI", metrics.section_font_size, "bold")
        )
        style.configure("Muted.TLabel", font=("Microsoft YaHei UI", metrics.muted_font_size))
        compact_vertical = max(3, metrics.button_padding[1] - 2)
        responsive_padding = (metrics.button_padding[0], compact_vertical)
        style.configure("Secondary.TButton", padding=responsive_padding)
        style.configure("Primary.TButton", padding=responsive_padding)
        style.configure("Danger.TButton", padding=responsive_padding)
        style.configure(
            "Compact.TButton", padding=metrics.compact_button_padding,
            font=("Microsoft YaHei UI", metrics.muted_font_size),
        )
        style.configure("Card.TCheckbutton", padding=(0, max(1, metrics.button_padding[1] // 2)))
        style.configure("CardAlt.TCheckbutton", padding=(0, max(1, metrics.button_padding[1] // 3)))
        style.configure("Dark.TEntry", padding=max(3, metrics.button_padding[1]))
        style.configure("Dark.TCombobox", padding=max(2, metrics.compact_button_padding[1]))
        style.configure(
            "Badge.TLabel", padding=metrics.compact_button_padding,
            font=("Segoe UI", metrics.muted_font_size, "bold"),
        )
        self.body.configure(padx=self._u(metrics.body_padding), pady=self._u(max(6, metrics.body_padding - 4)))
        self.left_shell.configure(width=self._u(metrics.sidebar_width))
        self.left_content.configure(padding=self._u(max(8, round(10 * metrics.layout_scale))))
        self.palette_panel.configure(width=self._u(metrics.palette_width))
        self.center_panel.pack_configure(padx=self._u(metrics.panel_gap))
        self.hero_canvas.configure(height=self._u(metrics.hero_height))
        self.legal_bar.configure(height=self._u(metrics.legal_height))
        self.hero_canvas.itemconfigure(
            self.hero_kicker_item,
            font=("Segoe UI", metrics.muted_font_size, "bold"),
        )
        self.hero_canvas.itemconfigure(
            self.hero_title_item,
            font=("Microsoft YaHei UI", max(17, round(21 * metrics.layout_scale)), "bold"),
        )
        self.hero_canvas.itemconfigure(
            self.hero_subtitle_item,
            font=("Microsoft YaHei UI", metrics.font_size),
        )
        self._resize_tracer_logo(metrics.layout_scale)
        if metrics.density != "regular":
            self.legal_label.configure(
                text="免责声明｜用户上传并于B站发布，请自行甄别、谨慎使用；严禁传播违法违规内容。  ·  1280×720  ·  F8停止",
                font=("Microsoft YaHei UI", max(7, metrics.muted_font_size)),
            )
            self.palette_info_label.pack_forget()
            self.palette_usage_hint.pack_forget()
            self.fit_mode_label.pack_forget()
            self.resample_mode_label.pack_forget()
            self.match_mode_label.pack_forget()
            if metrics.density == "tight":
                self.file_label.pack_forget()
                self.current_color_caption.pack_forget()
                self.selected_color_label.pack_configure(pady=0)
        else:
            self.legal_label.configure(
                text=("免责声明｜本工具为用户上传并于B站发布，请自行甄别、谨慎使用。严禁制作、上传或传播任何与国家法律法规及政策相抵触的内容；\n"
                      "使用者自行承担使用风险及后果。  ·  建议游戏客户区 1280×720  ·  F8 紧急停止"),
                font=("Microsoft YaHei UI", 8),
            )
            if not self.palette_info_label.winfo_manager():
                self.palette_info_label.pack(side="right")
            if not self.current_color_caption.winfo_manager():
                self.current_color_caption.pack(anchor="w", before=self.selected_color_label)
            self.selected_color_label.pack_configure(pady=(3, 0))
            if not self.palette_usage_hint.winfo_manager():
                self.palette_usage_hint.pack(anchor="center", pady=(7, 0))
        if metrics.density != "tight":
            if not self.file_label.winfo_manager():
                self.file_label.pack(
                    anchor="w", fill="x", pady=(2, 6), before=self.image_buttons
                )
        if metrics.density == "regular":
            if not self.fit_mode_label.winfo_manager():
                self.fit_mode_label.pack(anchor="w", before=self.mode_box)
            if not self.resample_mode_label.winfo_manager():
                self.resample_mode_label.pack(anchor="w", before=self.resample_box)
            if not self.match_mode_label.winfo_manager():
                self.match_mode_label.pack(anchor="w", before=self.match_box)
        if metrics.density != "tight":
            if not self.current_color_caption.winfo_manager():
                self.current_color_caption.pack(anchor="w", before=self.selected_color_label)
            self.selected_color_label.pack_configure(pady=(3, 0))
        self.update_idletasks()

    def _on_hero_configure(self, event):
        """Keep Hero geometry and the Tracer logo anchored to live edges."""
        width = max(1, event.width)
        height = max(1, event.height)
        layout_scale = max(0.78, min(1.30, min(
            width / max(1, self._u(1180)),
            height / max(1, self._u(90)),
        )))
        left = self._u(round(24 * layout_scale))
        band_top = round(height * 0.72)
        self.hero_canvas.coords(self.hero_band_item, 0, band_top, width, height)
        self.hero_canvas.coords(self.hero_kicker_item, left, round(height * 0.20))
        self.hero_canvas.coords(self.hero_title_item, left, round(height * 0.55))
        self.hero_canvas.coords(
            self.hero_subtitle_item,
            self._u(round(380 * layout_scale)),
            round(height * 0.58),
        )
        if self.hero_logo_item is not None:
            self.hero_canvas.coords(
                self.hero_logo_item,
                width - self._u(24),
                height // 2,
            )

    def _minimize_window(self):
        self.update_idletasks()
        hwnd = self._native_hwnd or self._outer_hwnd()
        ctypes.windll.user32.ShowWindow(hwnd, 6)  # SW_MINIMIZE

    def _restore_borderless(self, _event=None):
        if self.state() == "normal":
            self.after(10, self._configure_taskbar_window)

    # ---------------------------- 手动像素编辑 ----------------------------
    @staticmethod
    def _contrast_text_color(color):
        r, g, b = color
        luminance = 0.2126 * r + 0.7152 * g + 0.0722 * b
        return "#17212a" if luminance >= 150 else "#f7fbfc"

    def _color_guide_margin(self):
        return self._u(self.COLOR_GUIDE_MARGIN) if self.show_color_numbers.get() else 0

    def _toggle_color_numbers(self):
        self._apply_responsive_layout()
        self._render_preview()
        self._draw_palette()

    def _draw_palette(self):
        if not hasattr(self, "palette_canvas"):
            return
        canvas = self.palette_canvas
        canvas.delete("all")
        self.palette_hitboxes = []
        canvas_w = canvas.winfo_width() if canvas.winfo_width() > 1 else canvas.winfo_reqwidth()
        canvas_h = canvas.winfo_height() if canvas.winfo_height() > 1 else canvas.winfo_reqheight()
        margin = self._u(5)
        gap_x = self._u(7)
        gap_y = self._u(4)
        swatch = min(
            (canvas_w - 2 * margin - 3 * gap_x) // 4,
            (canvas_h - 2 * margin - 9 * gap_y) // 10,
        )
        # Never enforce a minimum larger than the available height: doing so
        # pushed the last palette column/row outside the Canvas at tight DPI.
        swatch = max(1, int(swatch))
        grid_w = 4 * swatch + 3 * gap_x
        grid_h = 10 * swatch + 9 * gap_y
        start_x = max(margin, (canvas_w - grid_w) // 2)
        start_y = max(margin, (canvas_h - grid_h) // 2)
        for index, color in enumerate(PALETTE):
            row, col = divmod(index, 4)
            x0 = start_x + col * (swatch + gap_x)
            y0 = start_y + row * (swatch + gap_y)
            x1 = x0 + swatch
            y1 = y0 + swatch
            fill = f"#{color[0]:02x}{color[1]:02x}{color[2]:02x}"
            selected = index == self.selected_color_index
            canvas.create_rectangle(
                x0, y0, x1, y1,
                fill=fill,
                outline="#ffffff" if selected else "#547080",
                width=self._u(3) if selected else self._u(1),
            )
            if selected:
                inset = self._u(4)
                canvas.create_rectangle(x0 + inset, y0 + inset, x1 - inset, y1 - inset,
                                        outline=self.ACCENT, width=self._u(2))
            if self.show_color_numbers.get():
                canvas.create_text(
                    (x0 + x1) / 2,
                    (y0 + y1) / 2,
                    text=str(index + 1),
                    fill=self._contrast_text_color(color),
                    font=("Segoe UI", max(6, min(9, swatch // 4)), "bold"),
                )
            self.palette_hitboxes.append((x0, y0, x1, y1))

    def _palette_click(self, event):
        for index, (x0, y0, x1, y1) in enumerate(self.palette_hitboxes):
            if x0 <= event.x <= x1 and y0 <= event.y <= y1:
                self._select_palette_color(index)
                break

    def _select_palette_color(self, index):
        self.selected_color_index = max(0, min(len(PALETTE) - 1, int(index)))
        r, g, b = PALETTE[self.selected_color_index]
        self.selected_color_var.set(
            f"#{self.selected_color_index + 1:02d}  ·  RGB {r}, {g}, {b}"
        )
        self._draw_palette()

    def _event_cell(self, event):
        margin = self._color_guide_margin()
        cell = self.PREVIEW_SIZE / N
        if event.x < margin or event.y < margin:
            return None
        col = int((event.x - margin) / cell)
        row = int((event.y - margin) / cell)
        if 0 <= col < N and 0 <= row < N:
            return col, row
        return None

    def _set_cell_visual(self, col, row):
        if not getattr(self, "cell_items", None):
            return
        color = PALETTE[self.matrix[row][col]]
        self.canvas.itemconfigure(
            self.cell_items[row][col],
            fill=f"#{color[0]:02x}{color[1]:02x}{color[2]:02x}",
        )
        if self.show_color_numbers.get() and getattr(self, "cell_number_items", None):
            self.canvas.itemconfigure(
                self.cell_number_items[row][col],
                text=str(self.matrix[row][col] + 1),
                fill=self._contrast_text_color(color),
            )
        self._render_mini_preview()

    def _apply_preview_color(self, event):
        if self.source_image is None:
            self.status_var.set("请先上传图片，再进行手动修整。")
            return
        cell = self._event_cell(event)
        if cell is None or cell in self._stroke_cells:
            return
        col, row = cell
        old_index = self.matrix[row][col]
        if old_index == self.selected_color_index:
            return
        self._stroke_cells.add(cell)
        self._active_stroke.append((col, row, old_index))
        self.matrix[row][col] = self.selected_color_index
        self._set_cell_visual(col, row)
        self._update_matrix_info("已手动修改像素；自动填充将使用当前预览。")

    def _start_preview_stroke(self, event):
        self._active_stroke = []
        self._stroke_cells = set()
        self._apply_preview_color(event)

    def _drag_preview_stroke(self, event):
        self._apply_preview_color(event)

    def _finish_preview_stroke(self, _event):
        if self._active_stroke:
            self.edit_history.append(self._active_stroke[:])
        self._active_stroke = []
        self._stroke_cells = set()

    def _pick_preview_color(self, event):
        cell = self._event_cell(event)
        if cell is None:
            return
        col, row = cell
        self._select_palette_color(self.matrix[row][col])
        self.status_var.set(f"已从像素 ({col + 1}, {row + 1}) 吸取颜色。")

    def _undo_edit(self):
        if not self.edit_history:
            self.status_var.set("没有可撤销的手动修改。")
            return
        for col, row, old_index in reversed(self.edit_history.pop()):
            self.matrix[row][col] = old_index
            self._set_cell_visual(col, row)
        self._update_matrix_info("已撤销上一次手动修改。")

    def _reset_manual_edits(self):
        if self.source_image is None:
            self.status_var.set("请先上传图片。")
            return
        self.matrix = [row[:] for row in self.original_matrix]
        self.edit_history.clear()
        self._render_preview()
        self._update_matrix_info("已恢复为当前图片的导入结果。")

    def _update_matrix_info(self, status=None):
        used = len({value for row in self.matrix for value in row})
        self.palette_info_var.set(f"24 × 24  /  {used} COLORS")
        if status:
            self.status_var.set(status)

    def _update_delay_label(self):
        try:
            self.delay_label.config(text=f"{self.click_delay.get():.3f} s")
        except Exception:
            pass

    def _on_algorithm_slider(self):
        try:
            self.resample_strength_label.configure(
                text=f"{round(self.resample_strength.get()):d}%"
            )
            self.match_strength_label.configure(
                text=f"{round(self.match_strength.get()):d}%"
            )
            self.structure_strength_label.configure(
                text=f"{round(self.structure_strength.get()):d}%"
            )
        except Exception:
            return
        if self.source_image is None:
            return
        if self._reprocess_after is not None:
            self.after_cancel(self._reprocess_after)
        self._reprocess_after = self.after(90, self._run_scheduled_reprocess)

    def _run_scheduled_reprocess(self):
        self._reprocess_after = None
        self.reprocess()

    def _cancel_scheduled_reprocess(self):
        if self._reprocess_after is not None:
            self.after_cancel(self._reprocess_after)
            self._reprocess_after = None

    def _sync_algorithm_controls(self):
        if self.direct_bitmap_mode or self.reduce_transitions.get():
            self.dither_check.state(["disabled"])
        else:
            self.dither_check.state(["!disabled"])
        if self.direct_bitmap_mode:
            self.resample_scale.state(["disabled"])
            self.structure_scale.state(["disabled"])
        else:
            self.resample_scale.state(["!disabled"])
            self.structure_scale.state(["!disabled"])
        if self.official_share_mode:
            self.match_scale.state(["disabled"])
        else:
            self.match_scale.state(["!disabled"])

    def _perceptual_enabled(self):
        return self.color_match_mode.get().startswith("OKLab")

    def _toggle_reduce_transitions(self):
        if self.reduce_transitions.get():
            self.dither.set(False)
        self._sync_algorithm_controls()
        self.reprocess()

    def _on_close(self):
        self.painter.stop()
        self.destroy()

    def open_image(self):
        path = filedialog.askopenfilename(
            title="选择图片",
            filetypes=[
                ("图片", "*.png;*.jpg;*.jpeg;*.webp;*.bmp;*.gif"),
                ("所有文件", "*.*"),
            ],
        )
        if not path:
            return

        try:
            with Image.open(path) as im:
                source = ImageOps.exif_transpose(im).copy()
            file_name = Path(path).name
            self._load_regular_source(source, file_name)
            self.after(60, self.open_crop_editor)
        except Exception as e:
            messagebox.showerror("读取失败", str(e))

    def _load_regular_source(self, image, file_name):
        self._cancel_scheduled_reprocess()
        self.source_image = ImageOps.exif_transpose(image).copy()
        self.source_file_name = file_name
        self.crop_box = None
        self.direct_bitmap_mode = False
        self.official_share_mode = False
        display_name = file_name if len(file_name) <= 30 else f"{file_name[:15]}…{file_name[-12:]}"
        self.file_var.set(display_name)
        self.status_var.set(f"已载入：{file_name}")
        self.crop_button.configure(state="normal")
        self.mode_box.configure(state="readonly")
        self.resample_box.configure(state="readonly")
        self.match_box.configure(state="readonly")
        self.dither_check.state(["!disabled"])
        self.transition_check.state(["!disabled"])
        self._sync_algorithm_controls()
        self.reprocess()

    def open_official_share(self):
        path = filedialog.askopenfilename(
            title="导入官方分享图",
            filetypes=[
                ("官方分享图片", "*.png;*.jpg;*.jpeg;*.webp"),
                ("所有文件", "*.*"),
            ],
        )
        if not path:
            return
        try:
            with Image.open(path) as im:
                source = ImageOps.exif_transpose(im).copy()
            matrix, _bounds, confidence = detect_official_share_grid(source)
        except Exception as exc:
            messagebox.showerror("分享图导入失败", str(exc))
            return

        self._load_official_share_source(source, Path(path).name, matrix, confidence)

    def _load_official_share_source(self, source, file_name, matrix=None, confidence=None):
        self._cancel_scheduled_reprocess()
        if matrix is None or confidence is None:
            matrix, _bounds, confidence = detect_official_share_grid(source)

        bitmap = matrix_to_image(matrix)
        self.source_image = bitmap
        self.source_file_name = file_name
        self.crop_box = None
        self.direct_bitmap_mode = True
        self.official_share_mode = True
        self.matrix = matrix
        self.original_matrix = [row[:] for row in matrix]
        self.edit_history.clear()
        display_name = file_name if len(file_name) <= 20 else f"{file_name[:10]}…{file_name[-7:]}"
        self.file_var.set(f"{display_name}  ·  官方分享图")
        self.crop_button.configure(state="disabled")
        self.mode_box.configure(state="disabled")
        self.resample_box.configure(state="disabled")
        self.match_box.configure(state="disabled")
        self.dither_check.state(["disabled"])
        self.transition_check.state(["disabled"])
        self._sync_algorithm_controls()
        self._render_preview()
        used = len({value for row in matrix for value in row})
        self._update_matrix_info(
            f"官方分享图已恢复：自动定位24×24画布并避开UID文字，"
            f"识别 {used} 种游戏颜色（定位置信度 {confidence:.0%}）。"
        )

    def capture_screen(self):
        self.withdraw()
        self.update_idletasks()
        time.sleep(0.18)
        dialog = None
        try:
            virtual_left = ctypes.windll.user32.GetSystemMetrics(76)  # SM_XVIRTUALSCREEN
            virtual_top = ctypes.windll.user32.GetSystemMetrics(77)   # SM_YVIRTUALSCREEN
            virtual_width = ctypes.windll.user32.GetSystemMetrics(78)  # SM_CXVIRTUALSCREEN
            virtual_height = ctypes.windll.user32.GetSystemMetrics(79)  # SM_CYVIRTUALSCREEN
            screenshot = ImageGrab.grab(all_screens=True)
            dialog = ScreenCaptureDialog(
                self,
                screenshot,
                (virtual_left, virtual_top, virtual_width, virtual_height),
            )
            self.wait_window(dialog)
        except Exception as exc:
            messagebox.showerror("截图失败", str(exc), parent=self)
        finally:
            self.deiconify()
            self.lift()
            self.after(30, self._configure_taskbar_window)

        if dialog is None or dialog.result is None:
            self.status_var.set("已取消截图。")
            return
        stamp = time.strftime("%Y%m%d-%H%M%S")
        self._load_regular_source(dialog.result, f"截图-{stamp}.png")
        self.status_var.set(
            f"截图已导入：{dialog.result.width}×{dialog.result.height} px；"
            "可继续裁切或调整算法比例。"
        )

    def open_24_bitmap(self):
        path = filedialog.askopenfilename(
            title="导入24×24 PNG位图",
            filetypes=[("PNG位图", "*.png")],
        )
        if not path:
            return

        try:
            self._cancel_scheduled_reprocess()
            with Image.open(path) as im:
                if im.format != "PNG":
                    raise ValueError("位图直导仅支持 PNG 文件。")
                bitmap = ImageOps.exif_transpose(im).copy()
            matrix = import_24_bitmap(
                bitmap,
                perceptual=self._perceptual_enabled(),
                match_strength=self.match_strength.get() / 100.0,
            )
        except Exception as e:
            messagebox.showerror("位图导入失败", str(e))
            return

        file_name = Path(path).name
        self.source_image = bitmap
        self.source_file_name = file_name
        self.crop_box = None
        self.direct_bitmap_mode = True
        self.official_share_mode = False
        self.matrix = matrix
        self.original_matrix = [row[:] for row in matrix]
        self.edit_history.clear()
        display_name = file_name if len(file_name) <= 24 else f"{file_name[:12]}…{file_name[-9:]}"
        self.file_var.set(f"{display_name}  ·  位图直导")
        self.crop_button.configure(state="disabled")
        self.mode_box.configure(state="disabled")
        self.resample_box.configure(state="disabled")
        self.match_box.configure(state="readonly")
        self.dither_check.state(["disabled"])
        self.transition_check.state(["disabled"])
        self._sync_algorithm_controls()
        self._render_preview()
        used = len({value for row in matrix for value in row})
        self._update_matrix_info(
            f"位图已逐像素导入：24×24，无尺寸重采样，共使用 {used} 种游戏颜色。"
        )

    def open_crop_editor(self):
        if self.source_image is None:
            messagebox.showinfo("裁切图片", "请先选择一张图片。")
            return
        if self.direct_bitmap_mode:
            messagebox.showinfo("裁切图片", "24×24位图直导模式不进行裁切或尺寸重采样。")
            return
        dialog = CropDialog(self, self.source_image, self.crop_box)
        self.wait_window(dialog)
        if dialog.result is None:
            self.status_var.set("已保留当前裁切设置。")
            return
        self.crop_box = dialog.result
        display_name = self.source_file_name
        if len(display_name) > 24:
            display_name = f"{display_name[:12]}…{display_name[-9:]}"
        self.file_var.set(f"{display_name}  ·  已裁切")
        self.reprocess()
        left, top, right, bottom = self.crop_box
        self.status_var.set(f"已应用裁切：{right - left}×{bottom - top} px。")

    def reprocess(self):
        if self.source_image is None:
            return
        if self.direct_bitmap_mode:
            self.matrix = import_24_bitmap(
                self.source_image,
                perceptual=self._perceptual_enabled(),
                match_strength=self.match_strength.get() / 100.0,
            )
            self.original_matrix = [row[:] for row in self.matrix]
            self.edit_history.clear()
            self._render_preview()
            used = len({value for row in self.matrix for value in row})
            self._update_matrix_info(
                f"位图已逐像素导入：24×24，无尺寸重采样，共使用 {used} 种游戏颜色。"
            )
            return
        source = self.source_image
        if self.crop_box is not None:
            source = source.crop(self.crop_box)
        self.matrix = quantize_image(
            source,
            mode=self.fit_mode.get(),
            resample=self.resample_mode.get(),
            dither=self.dither.get(),
            perceptual=self._perceptual_enabled(),
            reduce_transitions=self.reduce_transitions.get(),
            resample_strength=self.resample_strength.get() / 100.0,
            match_strength=self.match_strength.get() / 100.0,
            structure_strength=self.structure_strength.get() / 100.0,
        )
        self.original_matrix = [row[:] for row in self.matrix]
        self.edit_history.clear()
        self._render_preview()

        used = len({v for row in self.matrix for v in row})
        match_name = "OKLab" if self._perceptual_enabled() else "经典RGB"
        resample_name = self.resample_mode.get().split("（", 1)[0]
        transition_name = "，已减少过渡色" if self.reduce_transitions.get() else ""
        resample_ratio = round(self.resample_strength.get())
        match_ratio = round(self.match_strength.get())
        structure_ratio = round(self.structure_strength.get())
        structure_name = f"＋结构增强 {structure_ratio}%" if structure_ratio else ""
        self._update_matrix_info(
            f"转换完成：{resample_name} {resample_ratio}%＋{match_name} {match_ratio}%"
            f"{structure_name}{transition_name}，"
            f"共使用 {used} 种游戏颜色；可继续修改。"
        )

    def _render_preview(self):
        size = self.PREVIEW_SIZE
        margin = self._color_guide_margin()
        total_size = size + margin
        cell = size / N
        self.canvas.configure(width=total_size, height=total_size)
        self.canvas.delete("all")
        self.cell_items = [[None for _ in range(N)] for _ in range(N)]
        self.cell_number_items = [[None for _ in range(N)] for _ in range(N)]

        for y in range(N):
            for x in range(N):
                idx = self.matrix[y][x]
                c = PALETTE[idx]
                color = f"#{c[0]:02x}{c[1]:02x}{c[2]:02x}"
                x0 = margin + x * cell
                y0 = margin + y * cell
                x1 = margin + (x + 1) * cell
                y1 = margin + (y + 1) * cell
                item_id = self.canvas.create_rectangle(
                    x0, y0, x1, y1,
                    fill=color,
                    outline="#b6c4c9",
                    width=1,
                )
                self.cell_items[y][x] = item_id
                if self.show_color_numbers.get():
                    self.cell_number_items[y][x] = self.canvas.create_text(
                        margin + (x + 0.5) * cell,
                        margin + (y + 0.5) * cell,
                        text=str(idx + 1),
                        fill=self._contrast_text_color(c),
                        font=("Segoe UI", max(5, min(9, int(cell * 0.43))), "bold"),
                    )

        if self.show_color_numbers.get():
            guide_font = ("Segoe UI", max(5, min(9, int(cell * 0.42))))
            guide_bold_font = ("Segoe UI", max(6, min(10, int(cell * 0.46))), "bold")
            for index in range(N):
                number = index + 1
                emphasized = number % 12 == 0
                text_color = self.ORANGE if emphasized else self.MUTED
                text_font = guide_bold_font if emphasized else guide_font
                center = margin + (index + 0.5) * cell
                self.canvas.create_text(
                    center,
                    margin / 2,
                    text=str(number),
                    fill=text_color,
                    font=text_font,
                )
                self.canvas.create_text(
                    margin / 2,
                    center,
                    text=str(number),
                    fill=text_color,
                    font=text_font,
                )

            strong_width = max(2, self._u(2))
            for multiple in (0, 12, 24):
                position = margin + multiple * cell
                self.canvas.create_line(
                    margin,
                    position,
                    margin + size,
                    position,
                    fill=self.ACCENT_DARK if multiple == 12 else self.BORDER,
                    width=strong_width,
                )
                self.canvas.create_line(
                    position,
                    margin,
                    position,
                    margin + size,
                    fill=self.ACCENT_DARK if multiple == 12 else self.BORDER,
                    width=strong_width,
                )

        self._render_mini_preview()

    def _render_mini_preview(self):
        if not hasattr(self, "mini_preview_canvas"):
            return
        preview = matrix_to_image(self.matrix)
        width = max(24, self.mini_preview_canvas.winfo_width(),
                    self.mini_preview_canvas.winfo_reqwidth())
        height = max(24, self.mini_preview_canvas.winfo_height(),
                     self.mini_preview_canvas.winfo_reqheight())
        side = min(width, height)
        preview = preview.resize((side, side), Image.Resampling.NEAREST)
        self.mini_preview_photo = ImageTk.PhotoImage(preview)
        self.mini_preview_canvas.delete("all")
        self.mini_preview_canvas.create_image(
            width / 2,
            height / 2,
            anchor="center",
            image=self.mini_preview_photo,
        )

    # ---------------------------- 在线更新 ----------------------------
    @staticmethod
    def _version_tuple(value):
        numbers = re.findall(r"\d+", str(value))
        return tuple(int(part) for part in numbers[:4]) or (0,)

    def _start_update_check(self):
        threading.Thread(target=self._check_latest_release, daemon=True).start()

    def _check_latest_release(self):
        try:
            request = urllib.request.Request(
                GITHUB_LATEST_RELEASE_API,
                headers={
                    "Accept": "application/vnd.github+json",
                    "User-Agent": f"Arknights-Pixel-Autofill/{APP_VERSION}",
                },
            )
            with urllib.request.urlopen(request, timeout=7) as response:
                release = json.loads(response.read().decode("utf-8"))
            tag = str(release.get("tag_name", "")).strip()
            if self._version_tuple(tag) <= self._version_tuple(APP_VERSION):
                return
            assets = release.get("assets") or []
            exe_asset = next(
                (asset for asset in assets if str(asset.get("name", "")).lower().endswith(".exe")),
                None,
            )
            self._latest_release = {
                "tag": tag,
                "page_url": release.get("html_url") or GITHUB_RELEASES_URL,
                "download_url": (exe_asset or {}).get("browser_download_url"),
            }
            self.after(0, self._show_update_button)
        except Exception:
            # Update checks must never delay or block the local drawing tool.
            return

    def _show_update_button(self):
        if not self.winfo_exists() or not self._latest_release:
            return
        self.update_button.configure(text=f"更新 {self._latest_release['tag']}")
        self.update_button.pack(side="right", padx=(6, self._u(10)), pady=self._u(5))

    def _offer_release_download(self):
        release = self._latest_release
        if not release:
            return
        if not messagebox.askyesno(
            "发现新版本",
            f"当前版本：v{APP_VERSION}\n最新版本：{release['tag']}\n\n是否打开浏览器下载最新版？",
        ):
            return
        webbrowser.open(release.get("download_url") or release["page_url"])

    @staticmethod
    def _window_label(item):
        title = item["title"]
        if len(title) > 30:
            title = title[:29] + "…"
        return f"{title}  [{item['width']}×{item['height']}]  PID {item['pid']}"

    def refresh_window_list(self):
        previous = self.window_choices.get(self.window_choice.get())
        previous_hwnd = previous.get("hwnd") if previous else None
        choices = list_selectable_windows()
        mapping = {}
        for item in choices:
            base = self._window_label(item)
            label = base
            suffix = 2
            while label in mapping:
                label = f"{base} #{suffix}"
                suffix += 1
            mapping[label] = item
        self.window_choices = mapping
        self.window_box.configure(values=tuple(mapping))
        selected = next(
            (label for label, item in mapping.items() if item["hwnd"] == previous_hwnd),
            None,
        )
        if selected is None:
            selected = next(
                (label for label, item in mapping.items()
                 if "明日方舟" in item["title"] or item["class"] == "UnityWndClass"),
                "请选择游戏窗口" if mapping else "未发现可选择窗口",
            )
        self.window_choice.set(selected)

    def _on_window_selected(self, _event=None):
        target = self._selected_window_target(warn=False)
        if target:
            self.status_var.set(
                f"已选择游戏窗口：{target['title']}  "
                f"({target['width']}×{target['height']})"
            )

    def _selected_window_target(self, warn=True):
        target = self.window_choices.get(self.window_choice.get())
        if not target and warn:
            messagebox.showwarning("未选择窗口", "请点击“刷新”，然后从下拉列表选择游戏窗口。")
        return target

    def check_window(self):
        target = self._selected_window_target()
        if not target:
            return
        hwnd, title = resolve_selected_window(target)
        if not hwnd:
            messagebox.showwarning("窗口失效", "所选窗口已关闭或重建，请刷新后重新选择。")
            return

        ox, oy, w, h = get_client_info(hwnd)
        msg = (
            f"已找到：{title}\n"
            f"客户区：{w} × {h}\n"
            f"屏幕原点：({ox}, {oy})\n\n"
        )
        if abs(w - 1280) <= 4 and abs(h - 720) <= 4:
            msg += "客户区为标准 720p；开始时仍会自动识别实际网格线。"
        else:
            msg += "开始时会截取当前游戏客户区，自动识别 24×24 网格并校准坐标。"
        messagebox.showinfo("游戏窗口", msg)

    def start_paint(self):
        if self.source_image is None:
            messagebox.showwarning("没有图片", "请先选择一张图片。")
            return

        target = self._selected_window_target()
        if not target:
            return

        if not messagebox.askokcancel(
            "开始自动填充",
            "将自动切换到游戏窗口并开始点击。\n\n"
            "请确保：\n"
            "• 游戏停留在 24×24 编辑画面；\n"
            "• 游戏画布和右侧调色板完整可见，不要被其他窗口遮挡；\n"
            "• 右侧调色板可正常滚动；\n"
            "• 开始后不要拖动/缩放窗口。\n\n"
            "使用声明：本工具为用户上传，请自行甄别并承担使用风险；\n"
            "严禁用于制作或传播违反国家法律法规及政策的内容。\n\n"
            "紧急停止：按 F8。\n\n"
            "是否开始？",
        ):
            return

        self.progress_var.set(0)
        self.status_var.set("正在启动自动填充…")

        matrix_copy = [row[:] for row in self.matrix]
        delay = max(0.04, float(self.click_delay.get()))
        skip_white = bool(self.skip_white.get())

        t = threading.Thread(
            target=self.painter.paint,
            args=(matrix_copy, dict(target), delay, skip_white),
            daemon=True,
        )
        t.start()

    def stop_paint(self):
        self.painter.stop()
        self.status_var.set("正在停止…")

    def thread_status(self, text, error=False):
        def update():
            self.status_var.set(text)
            if error:
                try:
                    self.bell()
                except Exception:
                    pass
        self.after(0, update)

    def thread_progress(self, done, total, color_idx):
        def update():
            pct = 0 if total <= 0 else done * 100.0 / total
            self.progress_var.set(pct)
            if color_idx >= 0:
                self.status_var.set(
                    f"自动填充中：{done}/{total} 格，当前颜色 #{color_idx + 1}"
                )
        self.after(0, update)


if __name__ == "__main__":
    if not require_admin_before_startup():
        raise SystemExit(1)
    app = App()
    app.mainloop()
