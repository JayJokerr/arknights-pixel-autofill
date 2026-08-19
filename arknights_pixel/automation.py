"""Windows window discovery, mouse injection and automatic painting backend."""

import ctypes
import os
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path
from statistics import median


def resource_path(*parts):
    """兼容源码运行和 PyInstaller 单文件解包目录。"""
    source_root = Path(__file__).resolve().parent.parent
    base = Path(getattr(sys, "_MEIPASS", source_root))
    return base.joinpath(*parts)

try:
    from PIL import Image, ImageGrab
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


# Must be set before Qt creates a window.  PER_MONITOR_AWARE_V2 is important
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

# Give the process its own Windows shell identity before the first Qt window is
# created. Besides correct icon grouping, this prevents the packaged program
# from being treated as a transient Python window by the taskbar.
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


def restart_as_admin():
    """Ask Windows to restart this program with an elevated token."""
    if getattr(sys, "frozen", False):
        executable = sys.executable
        arguments = sys.argv[1:]
    else:
        executable = sys.executable
        arguments = [str(Path(sys.argv[0]).resolve()), *sys.argv[1:]]

    try:
        result = ctypes.windll.shell32.ShellExecuteW(
            None,
            "runas",
            executable,
            subprocess.list2cmdline(arguments),
            os.getcwd(),
            1,  # SW_SHOWNORMAL
        )
        return int(result) > 32
    except Exception:
        return False


def require_admin_before_startup():
    """Elevate before Qt is created and stop the unelevated process."""
    if is_running_as_admin():
        return True
    if restart_as_admin():
        return False
    try:
        ctypes.windll.user32.MessageBoxW(
            None,
            "本工具需要管理员权限才能向游戏窗口稳定发送鼠标输入。\n\n"
            "未能获得管理员权限。请在 UAC 提示中选择“是”，或右键程序并选择"
            "“以管理员身份运行”。",
            "管理员权限请求未完成",
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
# 坐标是动态视觉识别失败前的搜索先验，不会作为识别失败后的点击回退。
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
from arknights_pixel.palette import N, PALETTE
from arknights_pixel.vision import (
    GridLayout,
    PaletteLayout,
    detect_canvas_grid,
    detect_canvas_grid_dynamic,
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
