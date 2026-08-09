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
from ctypes import wintypes
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from pathlib import Path


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

    def set_target(self, hwnd):
        self.target_hwnd = hwnd

    @staticmethod
    def _make_lparam(x, y):
        return (round(x) & 0xFFFF) | ((round(y) & 0xFFFF) << 16)

    def _post_move(self, screen_x, screen_y):
        hwnd = self.target_hwnd
        if not hwnd or not win32gui.IsWindow(hwnd):
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
        x = round(x)
        y = round(y)

        # Unity 客户端聚焦后可能只读取 Raw Input 的相对位移，单纯发送
        # MOUSEEVENTF_ABSOLUTE 会表现为“游戏外能动，游戏内不动”。先发送一次
        # 相对增量给游戏，再用绝对事件校准 Windows 系统光标，兼容两种路径。
        point = wintypes.POINT()
        if self.user32.GetCursorPos(ctypes.byref(point)):
            dx = x - point.x
            dy = y - point.y
            if dx or dy:
                self._send(self.MOVE | self.MOVE_NOCOALESCE, dx, dy)
                # 相对位移会受到 Windows 指针加速影响，立即把系统光标拉回目标；
                # 游戏的 Raw Input 已经收到上面的原始增量。
                self.user32.SetCursorPos(x, y)

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
        if hwnd and self.last_position and win32gui.IsWindow(hwnd):
            try:
                cx, cy = win32gui.ScreenToClient(hwnd, self.last_position)
                lp = self._make_lparam(cx, cy)
                win32gui.PostMessage(hwnd, win32con.WM_MOUSEMOVE, 0, lp)
                win32gui.PostMessage(hwnd, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, lp)
                win32gui.PostMessage(hwnd, win32con.WM_LBUTTONUP, 0, lp)
            except Exception:
                pass

    def scroll(self, clicks):
        self._send(self.WHEEL, data=int(clicks * self.WHEEL_DELTA))
        hwnd = self.target_hwnd
        if hwnd and self.last_position and win32gui.IsWindow(hwnd):
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

# ---------------------------- 图像算法 ----------------------------
def color_distance(c1, c2):
    """CompuPhase RGB perceptual-ish distance."""
    r1, g1, b1 = c1
    r2, g2, b2 = c2
    rmean = (r1 + r2) / 2.0
    dr = r1 - r2
    dg = g1 - g2
    db = b1 - b2
    return (2.0 + rmean / 256.0) * dr * dr + 4.0 * dg * dg + (2.0 + (255.0 - rmean) / 256.0) * db * db


def nearest_palette_index(rgb):
    best_i = 0
    best_d = float("inf")
    for i, c in enumerate(PALETTE):
        d = color_distance(rgb, c)
        if d < best_d:
            best_d = d
            best_i = i
    return best_i


def flatten_alpha(img):
    img = img.convert("RGBA")
    bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
    bg.alpha_composite(img)
    return bg.convert("RGB")


def resize_to_24(img, mode="crop"):
    img = flatten_alpha(img)

    if mode == "stretch":
        return img.resize((N, N), Image.Resampling.LANCZOS)

    if mode == "contain":
        out = Image.new("RGB", (N, N), (255, 255, 255))
        tmp = img.copy()
        tmp.thumbnail((N, N), Image.Resampling.LANCZOS)
        x = (N - tmp.width) // 2
        y = (N - tmp.height) // 2
        out.paste(tmp, (x, y))
        return out

    # crop：保持比例，居中铺满
    return ImageOps.fit(
        img,
        (N, N),
        method=Image.Resampling.LANCZOS,
        centering=(0.5, 0.5),
    )


def quantize_image(img, mode="crop", dither=False):
    src = resize_to_24(img, mode)
    work = [[[float(v) for v in src.getpixel((x, y))] for x in range(N)] for y in range(N)]
    result = [[3 for _ in range(N)] for _ in range(N)]

    def add_error(x, y, er, eg, eb, factor):
        if 0 <= x < N and 0 <= y < N:
            p = work[y][x]
            p[0] = max(0.0, min(255.0, p[0] + er * factor))
            p[1] = max(0.0, min(255.0, p[1] + eg * factor))
            p[2] = max(0.0, min(255.0, p[2] + eb * factor))

    for y in range(N):
        for x in range(N):
            old = tuple(int(round(v)) for v in work[y][x])
            idx = nearest_palette_index(old)
            result[y][x] = idx

            if dither:
                nr, ng, nb = PALETTE[idx]
                er = old[0] - nr
                eg = old[1] - ng
                eb = old[2] - nb
                add_error(x + 1, y,     er, eg, eb, 7 / 16)
                add_error(x - 1, y + 1, er, eg, eb, 3 / 16)
                add_error(x,     y + 1, er, eg, eb, 5 / 16)
                add_error(x + 1, y + 1, er, eg, eb, 1 / 16)

    return result


def import_24_bitmap(img):
    """Map an exact 24x24 bitmap to the game palette without resampling."""
    if img.size != (N, N):
        raise ValueError(f"位图尺寸必须为 {N}×{N}，当前为 {img.width}×{img.height}。")
    src = flatten_alpha(img)
    return [
        [nearest_palette_index(src.getpixel((x, y))) for x in range(N)]
        for y in range(N)
    ]


# ---------------------------- Windows 窗口 ----------------------------
def find_game_window(keyword):
    windows = []
    own_pid = os.getpid()

    def enum_cb(hwnd, _):
        try:
            title = win32gui.GetWindowText(hwnd)
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            visible = bool(win32gui.IsWindowVisible(hwnd))
            iconic = bool(win32gui.IsIconic(hwnd))
            left, top, right, bottom = win32gui.GetClientRect(hwnd)
            area = max(0, right - left) * max(0, bottom - top)
            class_name = win32gui.GetClassName(hwnd)
        except Exception:
            return True

        windows.append({
            "hwnd": hwnd,
            "title": title,
            "pid": pid,
            "visible": visible,
            "iconic": iconic,
            "area": area,
            "class": class_name,
        })
        return True

    win32gui.EnumWindows(enum_cb, None)
    needle = keyword.strip().lower()

    def is_tool_window(item):
        title = item["title"]
        return item["pid"] == own_pid or (
            item["class"] == "TkTopLevel"
            and "24×24" in title
            and "自动填色" in title
        )

    # 工具自身标题也包含“明日方舟”，必须排除自身进程。游戏客户端可能用一个
    # 隐藏的 UnityWndClass 保存中文标题，而真正可见的渲染窗口标题为 Arknights；
    # 因此先由任意标题命中的窗口确定游戏 PID，再选择同进程的可见窗口。
    related_pids = {
        item["pid"]
        for item in windows
        if not is_tool_window(item)
        and needle
        and needle in item["title"].lower()
    }

    matches = [
        item
        for item in windows
        if not is_tool_window(item)
        and (
            item["visible"]
            or item["iconic"]
            or item["title"].strip().lower() == "arknights"
        )
        and (item["area"] > 0 or item["iconic"])
        and (
            item["pid"] in related_pids
            or (needle and needle in item["title"].lower())
            or item["title"].strip().lower() == "arknights"
        )
    ]

    if not matches:
        return None, None

    # 同进程可能同时存在 Qt 辅助窗口和真正的 Unity 渲染窗口。最小化时
    # Unity 客户区会暂时成为 0×0，所以先按窗口类别、再按面积选择。
    matches.sort(
        key=lambda item: (
            item["class"] == "UnityWndClass",
            item["visible"],
            item["area"],
        ),
        reverse=True,
    )
    best = matches[0]
    return best["hwnd"], best["title"]


def get_client_info(hwnd):
    left, top, right, bottom = win32gui.GetClientRect(hwnd)
    w = right - left
    h = bottom - top
    sx, sy = win32gui.ClientToScreen(hwnd, (0, 0))
    return sx, sy, w, h


def focus_window(hwnd):
    try:
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            time.sleep(0.3)
        win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
        win32gui.SetForegroundWindow(hwnd)
        time.sleep(0.5)
        return True
    except Exception:
        # Windows 有时禁止后台程序抢焦点，尝试鼠标点击客户区顶部空白处
        try:
            ox, oy, w, h = get_client_info(hwnd)
            game_mouse.click(ox + w // 2, oy + 30)
            time.sleep(0.4)
            return True
        except Exception:
            return False


# ---------------------------- 自动绘制 ----------------------------
class AutoPainter:
    def __init__(self, app):
        self.app = app
        self.stop_event = threading.Event()
        self.coordinate_calibration = None
        self.grid_x_lines = None
        self.grid_y_lines = None

    def stop(self):
        self.stop_event.set()

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
        ).convert("L")
        expected = (expected_left, expected_top, expected_right, expected_bottom)
        x_lines = self._detect_grid_axis(screenshot, expected, vertical=True)
        y_lines = self._detect_grid_axis(screenshot, expected, vertical=False)

        scale_x = (x_lines[-1] - x_lines[0]) / (CONFIG["grid_right"] - CONFIG["grid_left"])
        scale_y = (y_lines[-1] - y_lines[0]) / (CONFIG["grid_bottom"] - CONFIG["grid_top"])
        offset_x = x_lines[0] - CONFIG["grid_left"] * scale_x
        offset_y = y_lines[0] - CONFIG["grid_top"] * scale_y
        self.coordinate_calibration = (offset_x, offset_y, scale_x, scale_y)
        self.grid_x_lines = x_lines
        self.grid_y_lines = y_lines
        return x_lines, y_lines

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

    def _palette_click_pos(self, color_index, origin_x, origin_y, w, h, bottom_mode):
        row, col = divmod(color_index, 4)

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

    def paint(self, matrix, keyword, click_delay, skip_white):
        self.stop_event.clear()
        game_mouse.set_target(None)

        # 客户端在前后台切换时会销毁/重建 Unity 窗口；隐藏的 Arknights Qt
        # 宿主窗口句柄则保持不变。激活宿主后重新查找，直到拿到稳定渲染窗口。
        hwnd = title = None
        ox = oy = w = h = 0
        for _ in range(5):
            candidate, candidate_title = find_game_window(keyword)
            if not candidate:
                time.sleep(0.2)
                continue
            if not focus_window(candidate):
                time.sleep(0.2)
                continue
            time.sleep(0.45)

            refreshed, refreshed_title = find_game_window(keyword)
            if refreshed and refreshed != candidate:
                candidate, candidate_title = refreshed, refreshed_title
                focus_window(candidate)
                time.sleep(0.35)

            try:
                ox, oy, w, h = get_client_info(candidate)
            except Exception:
                time.sleep(0.2)
                continue
            hwnd, title = candidate, candidate_title
            if w >= 800 and h >= 450:
                break

        if not hwnd:
            self.app.thread_status("未找到游戏窗口。请确认游戏已打开，并检查窗口标题关键字。", error=True)
            return

        if w < 800 or h < 450:
            self.app.thread_status(f"游戏客户区尺寸异常：{w}x{h}", error=True)
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
        calibration_summary = "比例坐标"
        try:
            x_lines, y_lines = self._calibrate_coordinates(ox, oy, w, h)
            detected_w = x_lines[-1] - x_lines[0]
            detected_h = y_lines[-1] - y_lines[0]
            calibration_summary = f"自动校准 {detected_w}×{detected_h}px"
            self.app.thread_status(
                f"已自动校准 24×24 网格：{detected_w}×{detected_h} px，客户区 {w}×{h}"
            )
        except Exception as calibration_error:
            self.coordinate_calibration = None
            self.grid_x_lines = None
            self.grid_y_lines = None
            self.app.thread_status(
                f"自动网格校准未通过，改用比例坐标：{calibration_error}"
            )

        # 按颜色分组，减少切换色板次数
        groups = {i: [] for i in range(len(PALETTE))}
        for y in range(N):
            for x in range(N):
                idx = matrix[y][x]
                if skip_white and idx == 3:
                    continue
                groups[idx].append((x, y))

        used_colors = [i for i, pts in groups.items() if pts]
        total_cells = sum(len(groups[i]) for i in used_colors)

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
                game_mouse.click(px, py)
                time.sleep(max(0.08, click_delay))

                for x, y in pts:
                    if self.stop_event.is_set():
                        self.app.thread_status("已停止。")
                        return
                    gx, gy = self._grid_center(x, y, w, h)
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
                game_mouse.click(px, py)
                time.sleep(max(0.08, click_delay))

                for x, y in pts:
                    if self.stop_event.is_set():
                        self.app.thread_status("已停止。")
                        return
                    gx, gy = self._grid_center(x, y, w, h)
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


# ---------------------------- GUI ----------------------------
class App(tk.Tk):
    BASE_PREVIEW_SIZE = 408
    BASE_WINDOW_W = 1180
    BASE_WINDOW_H = 760
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
        self.minsize(self.WINDOW_W, self.WINDOW_H)
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
        self.source_file_name = ""
        self.matrix = [[3] * N for _ in range(N)]
        self.original_matrix = [row[:] for row in self.matrix]
        self.edit_history = []
        self._active_stroke = []
        self._stroke_cells = set()
        self.selected_color_index = 0
        self.preview_photo = None
        self.painter = AutoPainter(self)

        self.window_keyword = tk.StringVar(value="明日方舟")
        self.fit_mode = tk.StringVar(value="crop")
        self.dither = tk.BooleanVar(value=False)
        self.click_delay = tk.DoubleVar(value=0.060)
        self.skip_white = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value="请选择一张图片。")
        self.file_var = tk.StringVar(value="尚未选择图片")
        self.palette_info_var = tk.StringVar(value="40 色游戏调色板")
        self.progress_var = tk.DoubleVar(value=0.0)
        self.tracer_logo_photo = self._load_tracer_logo()
        self.visual_assets = self._load_visual_assets()
        self.selected_color_var = tk.StringVar(value="")
        self._drag_offset = (0, 0)
        self._native_hwnd = None
        self._responsive_after = None

        self._build_ui()
        self._render_preview()
        self._draw_palette()
        self._select_palette_color(0)
        self.update_idletasks()
        self._apply_native_window_geometry()
        self.after(80, self._configure_taskbar_window)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.bind("<Map>", self._restore_borderless)
        self.bind("<Configure>", self._on_window_configure)

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
        dpi_scale = max(0.75, min(2.5, dpi / 96.0))
        resolution_scale = min(work_w / 1920.0, work_h / 1040.0)
        desired_scale = max(dpi_scale, min(2.0, max(0.85, resolution_scale)))
        fit_scale = min(
            desired_scale,
            max(0.72, (work_w - 24) / cls.BASE_WINDOW_W),
            max(0.72, (work_h - 24) / cls.BASE_WINDOW_H),
        )
        return fit_scale

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
        style.configure("Card.TCheckbutton", background=self.CARD, foreground=self.TEXT,
                        indicatorcolor=self.CARD_ALT, padding=(0, 4))
        style.map("Card.TCheckbutton", background=[("active", self.CARD)],
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
        title_text = tk.Label(titlebar, text="ARKNIGHTS  /  24×24 PIXEL LAB",
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

        # 旅行主题 Hero，使用 Angelina 素材
        hero = tk.Canvas(root, height=self._u(90), bg=self.HERO_BG, highlightthickness=0)
        hero.pack(fill="x")
        hero.create_rectangle(0, self._u(64), self.WINDOW_W, self._u(90), fill="#286d91", outline="")
        hero.create_text(self._u(24), self._u(18), text="ARKNIGHTS  /  PIXEL STUDIO", anchor="w",
                         fill="#d9f5ff", font=("Segoe UI", 9, "bold"))
        hero.create_text(self._u(24), self._u(49), text="24×24 像素画工坊", anchor="w",
                         fill="white", font=("Microsoft YaHei UI", 21, "bold"))
        hero.create_text(self._u(380), self._u(52), text="上传 · 量化 · 手动修整 · 自动绘制", anchor="w",
                         fill="#cde9f4", font=("Microsoft YaHei UI", 10))
        if self.visual_assets.get("stars"):
            hero.create_image(self._u(700), self._u(44), image=self.visual_assets["stars"])
        if self.visual_assets.get("orange_title"):
            hero.create_image(self._u(812), self._u(45), image=self.visual_assets["orange_title"])
        if self.tracer_logo_photo is not None:
            hero.create_image(self._u(980), self._u(45), image=self.tracer_logo_photo)
        if self.visual_assets.get("paperplane"):
            hero.create_image(self._u(1110), self._u(55), image=self.visual_assets["paperplane"])

        # 底部固定免责声明，避免窗口缩放时被主体区域挤出
        legal = tk.Frame(root, bg="#102431", height=self._u(48))
        legal.pack(side="bottom", fill="x")
        legal.pack_propagate(False)
        tk.Label(
            legal,
            text=("免责声明｜本工具为用户上传并于B站发布，请自行甄别、谨慎使用。严禁制作、上传或传播任何与国家法律法规及政策相抵触的内容；\n"
                  "使用者自行承担使用风险及后果。  ·  建议游戏客户区 1280×720  ·  F8 紧急停止"),
            bg="#102431", fg="#b8d1dc", justify="left",
            font=("Microsoft YaHei UI", 8), padx=self._u(16),
        ).pack(side="left", fill="y")

        body = tk.Frame(root, bg=self.BODY_BG, padx=self._u(14), pady=self._u(10))
        body.pack(fill="both", expand=True)

        left = ttk.Frame(body, style="Card.TFrame", padding=self._u(13), width=self._u(270))
        left.pack(side="left", fill="y")
        left.pack_propagate(False)

        ttk.Label(left, text="01  图片处理", style="Section.TLabel").pack(anchor="w")
        ttk.Label(left, textvariable=self.file_var, style="Muted.TLabel").pack(
            anchor="w", fill="x", pady=(2, 6))
        image_buttons = ttk.Frame(left, style="Card.TFrame")
        image_buttons.pack(fill="x", pady=(0, 7))
        ttk.Button(image_buttons, text="＋  选择图片", style="Secondary.TButton",
                   command=self.open_image).pack(side="left", fill="x", expand=True)
        self.crop_button = ttk.Button(
            image_buttons,
            text="✂  裁切",
            style="Secondary.TButton",
            command=self.open_crop_editor,
            state="disabled",
        )
        self.crop_button.pack(side="left", padx=(self._u(6), 0))

        self.bitmap_button = ttk.Button(
            left,
            text="▦  导入24×24 PNG位图",
            style="Secondary.TButton",
            command=self.open_24_bitmap,
        )
        self.bitmap_button.pack(fill="x", pady=(0, 7))

        ttk.Label(left, text="缩放方式", style="Muted.TLabel").pack(anchor="w")
        self.mode_box = ttk.Combobox(
            left,
            textvariable=self.fit_mode,
            state="readonly",
            values=("crop", "contain", "stretch"),
            style="Dark.TCombobox",
        )
        self.mode_box.pack(fill="x", pady=(2, 3))
        self.mode_box.bind("<<ComboboxSelected>>", lambda e: self.reprocess())

        self.dither_check = ttk.Checkbutton(
            left,
            text="Floyd–Steinberg 抖动",
            variable=self.dither,
            command=self.reprocess,
            style="Card.TCheckbutton",
        )
        self.dither_check.pack(anchor="w")

        ttk.Separator(left, style="Dark.TSeparator").pack(fill="x", pady=9)

        ttk.Label(left, text="02  自动化设置", style="Section.TLabel").pack(anchor="w")
        ttk.Label(left, text="游戏窗口标题关键字", style="Muted.TLabel").pack(anchor="w", pady=(4, 0))
        ttk.Entry(left, textvariable=self.window_keyword, style="Dark.TEntry").pack(fill="x", pady=(2, 6))

        speed_line = ttk.Frame(left, style="Card.TFrame")
        speed_line.pack(fill="x")
        ttk.Label(speed_line, text="每格点击间隔", style="Muted.TLabel").pack(side="left")
        self.delay_label = ttk.Label(speed_line, text="", style="Badge.TLabel")
        self.delay_label.pack(side="right")
        delay = ttk.Scale(left, from_=0.04, to=0.12, variable=self.click_delay,
                          orient="horizontal", style="Dark.Horizontal.TScale")
        delay.pack(fill="x", pady=(3, 2))
        self.click_delay.trace_add("write", lambda *_: self._update_delay_label())
        self._update_delay_label()

        ttk.Checkbutton(
            left,
            text="跳过白色格（仅空白画布建议）",
            variable=self.skip_white,
            style="Card.TCheckbutton",
        ).pack(anchor="w", pady=(2, 7))

        self.start_button = ttk.Button(left, text="▶  开始自动填充", style="Primary.TButton",
                                       command=self.start_paint)
        self.start_button.pack(fill="x", pady=(0, 5))
        ttk.Button(left, text="■  停止", style="Danger.TButton",
                   command=self.stop_paint).pack(fill="x")

        ttk.Label(left, text="建议 1280×720  ·  紧急停止 F8", style="Muted.TLabel").pack(
            anchor="center", pady=(7, 0))

        palette_panel = ttk.Frame(body, style="Card.TFrame", padding=self._u(12), width=self._u(214))
        palette_panel.pack(side="right", fill="y")
        palette_panel.pack_propagate(False)
        ttk.Label(palette_panel, text="游戏调色板", style="Section.TLabel").pack(anchor="w")
        ttk.Label(palette_panel, text="40 COLORS · 点击选择", style="Muted.TLabel").pack(
            anchor="w", pady=(1, 7))
        self.palette_canvas = tk.Canvas(palette_panel, width=self._u(176), height=self._u(370),
                                        bg=self.CARD, highlightthickness=0, cursor="hand2")
        self.palette_canvas.pack(anchor="center", fill="both", expand=True)
        self.palette_canvas.bind("<Button-1>", self._palette_click)
        self.palette_canvas.bind("<Configure>", lambda _event: self._draw_palette())
        color_info = ttk.Frame(palette_panel, style="CardAlt.TFrame", padding=(8, 7))
        color_info.pack(fill="x", pady=(5, 0))
        ttk.Label(color_info, text="当前颜色", style="Hint.TLabel").pack(anchor="w")
        ttk.Label(color_info, textvariable=self.selected_color_var, style="Badge.TLabel").pack(
            anchor="w", pady=(3, 0))
        ttk.Label(palette_panel, text="左键/拖动涂色 · 右键吸色", style="Muted.TLabel").pack(
            anchor="center", pady=(7, 0))

        center = ttk.Frame(body, style="Card.TFrame", padding=self._u(12))
        center.pack(side="left", fill="both", expand=True, padx=self._u(12))
        self.center_panel = center
        preview_head = ttk.Frame(center, style="Card.TFrame")
        self.preview_head = preview_head
        preview_head.pack(fill="x", pady=(0, 7))
        ttk.Label(preview_head, text="实时预览", style="Section.TLabel").pack(side="left")
        ttk.Button(preview_head, text="恢复转换结果", style="Compact.TButton",
                   command=self._reset_manual_edits).pack(side="right")
        ttk.Button(preview_head, text="撤销", style="Compact.TButton",
                   command=self._undo_edit).pack(side="right", padx=(0, 6))
        ttk.Label(preview_head, textvariable=self.palette_info_var, style="Badge.TLabel").pack(side="right")

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

        self.progress = ttk.Progressbar(info, variable=self.progress_var, maximum=100,
                                        style="Accent.Horizontal.TProgressbar")
        self.progress.pack(fill="x")
        ttk.Label(info, textvariable=self.status_var, style="Muted.TLabel",
                  wraplength=self._u(560)).pack(anchor="w", pady=(self._u(6), 0))

    def _load_tracer_logo(self):
        try:
            with Image.open(resource_path("assets", "tracer_logo.png")) as logo:
                logo = logo.convert("RGBA")
                logo.thumbnail((self._u(110), self._u(68)), Image.Resampling.LANCZOS)
                return ImageTk.PhotoImage(logo)
        except Exception:
            return None

    def _load_visual_assets(self):
        specs = {
            "paperplane": ("angelina_paperplane.png", (self._u(130), self._u(130)), 235),
            "stars": ("angelina_stars.png", (self._u(240), self._u(78)), 135),
            "orange_title": ("angelina_orange_title.png", (self._u(190), self._u(60)), 235),
        }
        result = {}
        for key, (name, max_size, opacity) in specs.items():
            try:
                with Image.open(resource_path("assets", name)) as source:
                    image = source.convert("RGBA")
                    image.thumbnail(max_size, Image.Resampling.LANCZOS)
                    if opacity < 255:
                        alpha = image.getchannel("A").point(lambda value: value * opacity // 255)
                        image.putalpha(alpha)
                    result[key] = ImageTk.PhotoImage(image)
            except Exception:
                result[key] = None
        return result

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

        reserved_h = (
            self.preview_head.winfo_height()
            + self.preview_info.winfo_height()
            + self._u(46)
        )
        available_w = center_w - self._u(34)
        available_h = center_h - reserved_h
        target = min(available_w, available_h)
        target = max(self._u(self.BASE_PREVIEW_SIZE), target)
        # An exact multiple of 24 keeps cell boundaries and pointer hit-testing
        # identical at every window size.
        target = max(N * 8, (int(target) // N) * N)
        if target != self.PREVIEW_SIZE:
            self.PREVIEW_SIZE = target
            self.canvas.configure(width=target, height=target)
            self._render_preview()
        self._draw_palette()

    def _minimize_window(self):
        self.update_idletasks()
        hwnd = self._native_hwnd or self._outer_hwnd()
        ctypes.windll.user32.ShowWindow(hwnd, 6)  # SW_MINIMIZE

    def _restore_borderless(self, _event=None):
        if self.state() == "normal":
            self.after(10, self._configure_taskbar_window)

    # ---------------------------- 手动像素编辑 ----------------------------
    def _draw_palette(self):
        if not hasattr(self, "palette_canvas"):
            return
        canvas = self.palette_canvas
        canvas.delete("all")
        self.palette_hitboxes = []
        canvas_w = max(canvas.winfo_width(), canvas.winfo_reqwidth())
        canvas_h = max(canvas.winfo_height(), canvas.winfo_reqheight())
        margin = self._u(5)
        gap_x = self._u(7)
        gap_y = self._u(4)
        swatch = max(
            self._u(16),
            min(
                (canvas_w - 2 * margin - 3 * gap_x) // 4,
                (canvas_h - 2 * margin - 9 * gap_y) // 10,
            ),
        )
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
        cell = self.PREVIEW_SIZE / N
        col = int(event.x / cell)
        row = int(event.y / cell)
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
                self.source_image = ImageOps.exif_transpose(im).copy()
            file_name = Path(path).name
            self.source_file_name = file_name
            self.crop_box = None
            self.direct_bitmap_mode = False
            display_name = file_name if len(file_name) <= 30 else f"{file_name[:15]}…{file_name[-12:]}"
            self.file_var.set(display_name)
            self.status_var.set(f"已载入：{file_name}")
            self.crop_button.configure(state="normal")
            self.mode_box.configure(state="readonly")
            self.dither_check.state(["!disabled"])
            self.reprocess()
            self.after(60, self.open_crop_editor)
        except Exception as e:
            messagebox.showerror("读取失败", str(e))

    def open_24_bitmap(self):
        path = filedialog.askopenfilename(
            title="导入24×24 PNG位图",
            filetypes=[("PNG位图", "*.png")],
        )
        if not path:
            return

        try:
            with Image.open(path) as im:
                if im.format != "PNG":
                    raise ValueError("位图直导仅支持 PNG 文件。")
                bitmap = ImageOps.exif_transpose(im).copy()
            matrix = import_24_bitmap(bitmap)
        except Exception as e:
            messagebox.showerror("位图导入失败", str(e))
            return

        file_name = Path(path).name
        self.source_image = bitmap
        self.source_file_name = file_name
        self.crop_box = None
        self.direct_bitmap_mode = True
        self.matrix = matrix
        self.original_matrix = [row[:] for row in matrix]
        self.edit_history.clear()
        display_name = file_name if len(file_name) <= 24 else f"{file_name[:12]}…{file_name[-9:]}"
        self.file_var.set(f"{display_name}  ·  位图直导")
        self.crop_button.configure(state="disabled")
        self.mode_box.configure(state="disabled")
        self.dither_check.state(["disabled"])
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
            self.matrix = import_24_bitmap(self.source_image)
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
            dither=self.dither.get(),
        )
        self.original_matrix = [row[:] for row in self.matrix]
        self.edit_history.clear()
        self._render_preview()

        used = len({v for row in self.matrix for v in row})
        self._update_matrix_info(f"转换完成：24×24，共使用 {used} 种游戏颜色；可在预览中继续修改。")

    def _render_preview(self):
        size = self.PREVIEW_SIZE
        cell = size / N
        self.canvas.delete("all")
        self.cell_items = [[None for _ in range(N)] for _ in range(N)]

        for y in range(N):
            for x in range(N):
                idx = self.matrix[y][x]
                c = PALETTE[idx]
                color = f"#{c[0]:02x}{c[1]:02x}{c[2]:02x}"
                x0 = x * cell
                y0 = y * cell
                x1 = (x + 1) * cell
                y1 = (y + 1) * cell
                item_id = self.canvas.create_rectangle(
                    x0, y0, x1, y1,
                    fill=color,
                    outline="#b6c4c9",
                    width=1,
                )
                self.cell_items[y][x] = item_id

    def check_window(self):
        keyword = self.window_keyword.get().strip()
        hwnd, title = find_game_window(keyword)
        if not hwnd:
            messagebox.showwarning("未找到", f"未找到标题包含“{keyword}”的窗口。")
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
        keyword = self.window_keyword.get().strip()
        delay = max(0.04, float(self.click_delay.get()))
        skip_white = bool(self.skip_white.get())

        t = threading.Thread(
            target=self.painter.paint,
            args=(matrix_copy, keyword, delay, skip_white),
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
    app = App()
    app.mainloop()
