# -*- coding: utf-8 -*-
"""PySide6 UI for the Arknights 24x24 pixel autofill tool.

The image/vision pipeline and Windows input backend remain shared with the
legacy entry point.  This module owns all visible UI so Qt can provide native
high-DPI layout, live resizing and consistent control geometry.
"""

from __future__ import annotations

import ctypes
import json
import re
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

from PIL import Image, ImageGrab, ImageOps

try:
    from PySide6.QtCore import QPoint, QRect, QSize, Qt, QTimer, Signal, QObject
    from PySide6.QtGui import QColor, QFont, QIcon, QImage, QPainter, QPen, QPixmap
    from PySide6.QtWidgets import (
        QApplication,
        QCheckBox,
        QComboBox,
        QDialog,
        QFileDialog,
        QFrame,
        QGridLayout,
        QHBoxLayout,
        QLabel,
        QMainWindow,
        QMessageBox,
        QProgressBar,
        QPushButton,
        QScrollArea,
        QSizePolicy,
        QSlider,
        QSplitter,
        QVBoxLayout,
        QWidget,
    )
except ImportError as exc:
    raise SystemExit("缺少 PySide6。请运行：pip install -r requirements.txt") from exc

from arknights_pixel.image_processing import (
    RESAMPLE_METHODS,
    import_24_bitmap,
    matrix_to_image,
    quantize_image,
)
from arknights_pixel.palette import N, PALETTE
from arknights_pixel.vision import detect_official_share_grid
from arknights_pixel.automation import (
    AutoPainter,
    list_selectable_windows,
    require_admin_before_startup,
    resource_path,
)


APP_VERSION = "1.4.0"
GITHUB_REPOSITORY = "JayJokerr/arknights-pixel-autofill"
GITHUB_RELEASES_URL = f"https://github.com/{GITHUB_REPOSITORY}/releases"
GITHUB_LATEST_RELEASE_API = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/releases/latest"


def pil_to_pixmap(image: Image.Image) -> QPixmap:
    rgba = image.convert("RGBA")
    data = rgba.tobytes("raw", "RGBA")
    qimage = QImage(data, rgba.width, rgba.height, rgba.width * 4, QImage.Format_RGBA8888)
    return QPixmap.fromImage(qimage.copy())


def elide_name(name: str, limit: int = 28) -> str:
    if len(name) <= limit:
        return name
    return f"{name[: limit // 2]}…{name[-(limit // 2 - 1):]}"


class UiBridge(QObject):
    status = Signal(str, bool)
    progress = Signal(int, int, int)
    updateAvailable = Signal(object)


class PainterHost:
    """Thread-safe adapter expected by the existing AutoPainter backend."""

    def __init__(self, bridge: UiBridge):
        self.bridge = bridge

    def thread_status(self, text, error=False):
        self.bridge.status.emit(str(text), bool(error))

    def thread_progress(self, done, total, color_index):
        self.bridge.progress.emit(int(done), int(total), int(color_index))


class PixelCanvas(QWidget):
    colorPicked = Signal(int)
    matrixChanging = Signal()
    matrixEdited = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.matrix = [[3] * N for _ in range(N)]
        self.selected = 0
        self.show_numbers = False
        self._painting = False
        self._stroke_before = None
        self._stroke_changed = False
        self.last_stroke_before = None
        self.setMinimumSize(300, 300)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setCursor(Qt.CrossCursor)

    def set_matrix(self, matrix):
        self.matrix = [row[:] for row in matrix]
        self.update()

    def set_selected(self, index):
        self.selected = max(0, min(len(PALETTE) - 1, int(index)))

    def set_show_numbers(self, enabled):
        self.show_numbers = bool(enabled)
        self.update()

    def _geometry(self):
        guide = 30 if self.show_numbers else 4
        side = max(24, min(self.width() - guide - 4, self.height() - guide - 4))
        side = max(24, side - side % N)
        left = (self.width() - side + guide) // 2
        top = (self.height() - side + guide) // 2
        return QRect(left, top, side, side), guide

    @staticmethod
    def _text_color(rgb):
        luminance = 0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]
        return QColor("#14222b" if luminance > 155 else "#ffffff")

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.TextAntialiasing, True)
        painter.fillRect(self.rect(), QColor("#122331"))
        board, guide = self._geometry()
        cell = board.width() / N
        number_font = QFont("Segoe UI", max(6, min(10, int(cell * 0.35))))
        for row in range(N):
            for col in range(N):
                index = self.matrix[row][col]
                rgb = PALETTE[index]
                rect = QRect(
                    round(board.left() + col * cell),
                    round(board.top() + row * cell),
                    max(1, round(cell + 0.5)),
                    max(1, round(cell + 0.5)),
                )
                painter.fillRect(rect, QColor(*rgb))
                painter.setPen(QPen(QColor("#b7c5ca"), 1))
                painter.drawRect(rect)
                if self.show_numbers and cell >= 14:
                    painter.setFont(number_font)
                    painter.setPen(self._text_color(rgb))
                    painter.drawText(rect, Qt.AlignCenter, str(index + 1))
        if self.show_numbers:
            painter.setFont(QFont("Segoe UI", max(6, min(9, int(cell * 0.32)))))
            for position in range(N):
                value = position + 1
                color = QColor("#f4a23a" if value % 12 == 0 else "#a9c3ce")
                painter.setPen(color)
                x = round(board.left() + (position + 0.5) * cell)
                y = round(board.top() + (position + 0.5) * cell)
                painter.drawText(QRect(x - 11, board.top() - guide, 22, guide), Qt.AlignCenter, str(value))
                painter.drawText(QRect(board.left() - guide, y - 11, guide, 22), Qt.AlignCenter, str(value))
            for multiple in (0, 12, 24):
                pos_x = round(board.left() + multiple * cell)
                pos_y = round(board.top() + multiple * cell)
                painter.setPen(QPen(QColor("#228eb0" if multiple == 12 else "#5f8799"), 2))
                painter.drawLine(pos_x, board.top(), pos_x, board.bottom())
                painter.drawLine(board.left(), pos_y, board.right(), pos_y)

    def _cell_at(self, point):
        board, _guide = self._geometry()
        if not board.contains(point):
            return None
        col = min(N - 1, int((point.x() - board.left()) * N / board.width()))
        row = min(N - 1, int((point.y() - board.top()) * N / board.height()))
        return col, row

    def mousePressEvent(self, event):
        cell = self._cell_at(event.position().toPoint())
        if cell is None:
            return
        col, row = cell
        if event.button() == Qt.RightButton:
            self.colorPicked.emit(self.matrix[row][col])
            return
        if event.button() == Qt.LeftButton:
            self._painting = True
            self._stroke_before = [line[:] for line in self.matrix]
            self._stroke_changed = self.matrix[row][col] != self.selected
            if self._stroke_changed:
                self.matrix[row][col] = self.selected
                self.update()
                self.matrixChanging.emit()

    def mouseMoveEvent(self, event):
        if not self._painting:
            return
        cell = self._cell_at(event.position().toPoint())
        if cell:
            col, row = cell
            if self.matrix[row][col] != self.selected:
                self.matrix[row][col] = self.selected
                self._stroke_changed = True
                self.update()
                self.matrixChanging.emit()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self._painting:
            self._painting = False
            self.last_stroke_before = self._stroke_before if self._stroke_changed else None
            self._stroke_before = None
            if self._stroke_changed:
                self.matrixEdited.emit()
            self._stroke_changed = False


class PaletteWidget(QWidget):
    selected = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.index = 0
        self.show_numbers = False
        self.setMinimumWidth(170)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def set_selected(self, index):
        self.index = max(0, min(39, int(index)))
        self.update()

    def set_show_numbers(self, enabled):
        self.show_numbers = bool(enabled)
        self.update()

    def _layout(self):
        gap = max(4, round(min(self.width(), self.height()) * 0.018))
        cell = min((self.width() - gap * 5) / 4, (self.height() - gap * 11) / 10)
        cell = max(8.0, cell)
        total_w = cell * 4 + gap * 3
        total_h = cell * 10 + gap * 9
        return (self.width() - total_w) / 2, (self.height() - total_h) / 2, cell, gap

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.TextAntialiasing, True)
        painter.fillRect(self.rect(), QColor("#122331"))
        x0, y0, cell, gap = self._layout()
        for index, rgb in enumerate(PALETTE):
            row, col = divmod(index, 4)
            rect = QRect(round(x0 + col * (cell + gap)), round(y0 + row * (cell + gap)), round(cell), round(cell))
            painter.fillRect(rect, QColor(*rgb))
            painter.setPen(QPen(QColor("#66d9ef" if index == self.index else "#203947"), 3 if index == self.index else 1))
            painter.drawRect(rect.adjusted(1, 1, -1, -1))
            if self.show_numbers and cell >= 25:
                painter.setFont(QFont("Segoe UI", max(7, round(cell * 0.22)), QFont.Bold))
                luminance = 0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]
                painter.setPen(QColor("#14222b" if luminance > 155 else "#ffffff"))
                painter.drawText(rect, Qt.AlignCenter, str(index + 1))

    def mousePressEvent(self, event):
        x0, y0, cell, gap = self._layout()
        x, y = event.position().x() - x0, event.position().y() - y0
        col, row = int(x / (cell + gap)), int(y / (cell + gap))
        if 0 <= col < 4 and 0 <= row < 10:
            local_x, local_y = x - col * (cell + gap), y - row * (cell + gap)
            if local_x <= cell and local_y <= cell:
                self.selected.emit(row * 4 + col)


class CropCanvas(QWidget):
    def __init__(self, image, initial_box=None, parent=None):
        super().__init__(parent)
        self.image = ImageOps.exif_transpose(image).convert("RGB")
        self.pixmap = pil_to_pixmap(self.image)
        side = min(self.image.size)
        self.box = list(initial_box or ((self.image.width - side) // 2, (self.image.height - side) // 2,
                                        (self.image.width + side) // 2, (self.image.height + side) // 2))
        self._drag_start = None
        self._box_start = None
        self.setMinimumSize(640, 460)

    def _image_rect(self):
        size = self.pixmap.size().scaled(self.size(), Qt.KeepAspectRatio)
        return QRect((self.width() - size.width()) // 2, (self.height() - size.height()) // 2,
                     size.width(), size.height())

    def _display_box(self):
        rect = self._image_rect()
        sx, sy = rect.width() / self.image.width, rect.height() / self.image.height
        return QRect(round(rect.left() + self.box[0] * sx), round(rect.top() + self.box[1] * sy),
                     round((self.box[2] - self.box[0]) * sx), round((self.box[3] - self.box[1]) * sy))

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#101922"))
        target = self._image_rect()
        painter.drawPixmap(target, self.pixmap)
        selection = self._display_box()
        painter.fillRect(QRect(target.left(), target.top(), target.width(), max(0, selection.top() - target.top())), QColor(0, 0, 0, 115))
        painter.fillRect(QRect(target.left(), selection.bottom(), target.width(), max(0, target.bottom() - selection.bottom())), QColor(0, 0, 0, 115))
        painter.fillRect(QRect(target.left(), selection.top(), max(0, selection.left() - target.left()), selection.height()), QColor(0, 0, 0, 115))
        painter.fillRect(QRect(selection.right(), selection.top(), max(0, target.right() - selection.right()), selection.height()), QColor(0, 0, 0, 115))
        painter.setPen(QPen(QColor("#66d9ef"), 3))
        painter.drawRect(selection)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self._display_box().contains(event.position().toPoint()):
            self._drag_start = event.position().toPoint()
            self._box_start = self.box[:]

    def mouseMoveEvent(self, event):
        if self._drag_start is None:
            return
        target = self._image_rect()
        delta = event.position().toPoint() - self._drag_start
        dx = round(delta.x() * self.image.width / max(1, target.width()))
        dy = round(delta.y() * self.image.height / max(1, target.height()))
        side = self._box_start[2] - self._box_start[0]
        left = max(0, min(self.image.width - side, self._box_start[0] + dx))
        top = max(0, min(self.image.height - side, self._box_start[1] + dy))
        self.box = [left, top, left + side, top + side]
        self.update()

    def mouseReleaseEvent(self, _event):
        self._drag_start = self._box_start = None

    def wheelEvent(self, event):
        factor = 0.90 if event.angleDelta().y() > 0 else 1.10
        old = self.box[2] - self.box[0]
        side = max(16, min(min(self.image.size), round(old * factor)))
        cx, cy = (self.box[0] + self.box[2]) / 2, (self.box[1] + self.box[3]) / 2
        left = max(0, min(self.image.width - side, round(cx - side / 2)))
        top = max(0, min(self.image.height - side, round(cy - side / 2)))
        self.box = [left, top, left + side, top + side]
        self.update()


class CropDialog(QDialog):
    def __init__(self, image, initial_box=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("裁切图片")
        self.resize(820, 650)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("拖动画框移动裁切区域；滚轮缩放正方形裁切框。"))
        self.canvas = CropCanvas(image, initial_box, self)
        layout.addWidget(self.canvas, 1)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("取消")
        apply_button = QPushButton("应用裁切")
        cancel.clicked.connect(self.reject)
        apply_button.clicked.connect(self.accept)
        buttons.addWidget(cancel)
        buttons.addWidget(apply_button)
        layout.addLayout(buttons)

    @property
    def result_box(self):
        return tuple(self.canvas.box)


class CaptureDialog(QDialog):
    def __init__(self, screenshot, virtual_geometry, parent=None):
        super().__init__(parent)
        self.screenshot = screenshot.convert("RGB")
        self.pixmap = pil_to_pixmap(self.screenshot)
        self.origin = virtual_geometry.topLeft()
        self.start = None
        self.end = None
        self.result_image = None
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setGeometry(virtual_geometry)
        self.setCursor(Qt.CrossCursor)

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.drawPixmap(self.rect(), self.pixmap)
        painter.fillRect(self.rect(), QColor(0, 0, 0, 85))
        if self.start and self.end:
            rect = QRect(self.start, self.end).normalized()
            scale_x = self.pixmap.width() / max(1, self.width())
            scale_y = self.pixmap.height() / max(1, self.height())
            source = QRect(
                round(rect.left() * scale_x),
                round(rect.top() * scale_y),
                max(1, round(rect.width() * scale_x)),
                max(1, round(rect.height() * scale_y)),
            )
            painter.drawPixmap(rect, self.pixmap, source)
            painter.setPen(QPen(QColor("#66d9ef"), 2))
            painter.drawRect(rect)

    def mousePressEvent(self, event):
        if event.button() == Qt.RightButton:
            self.reject()
        elif event.button() == Qt.LeftButton:
            self.start = self.end = event.position().toPoint()
            self.update()

    def mouseMoveEvent(self, event):
        if self.start:
            self.end = event.position().toPoint()
            self.update()

    def mouseReleaseEvent(self, event):
        if event.button() != Qt.LeftButton or not self.start:
            return
        self.end = event.position().toPoint()
        rect = QRect(self.start, self.end).normalized().intersected(self.rect())
        if rect.width() >= 12 and rect.height() >= 12:
            scale_x = self.screenshot.width / max(1, self.width())
            scale_y = self.screenshot.height / max(1, self.height())
            self.result_image = self.screenshot.crop((
                round(rect.left() * scale_x),
                round(rect.top() * scale_y),
                round((rect.right() + 1) * scale_x),
                round((rect.bottom() + 1) * scale_y),
            ))
            self.accept()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.reject()
        else:
            super().keyPressEvent(event)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"明日方舟 24×24 像素画自动填色 v{APP_VERSION}")
        self.setWindowIcon(QIcon(str(resource_path("arknights_pixel.ico"))))
        self.resize(1180, 760)
        self.setMinimumSize(920, 660)

        self.source_image = None
        self.source_file_name = ""
        self.crop_box = None
        self.direct_bitmap_mode = False
        self.official_share_mode = False
        self.matrix = [[3] * N for _ in range(N)]
        self.original_matrix = [row[:] for row in self.matrix]
        self.history = []
        self.window_targets = {}
        self.latest_release = None
        self.selected_color = 0
        self._style_scale = None
        self._reprocess_timer = QTimer(self)
        self._reprocess_timer.setSingleShot(True)
        self._reprocess_timer.setInterval(180)
        self._reprocess_timer.timeout.connect(self._flush_reprocess)

        self.bridge = UiBridge(self)
        self.bridge.status.connect(self._thread_status)
        self.bridge.progress.connect(self._thread_progress)
        self.bridge.updateAvailable.connect(self._receive_update)
        self.painter = AutoPainter(PainterHost(self.bridge))

        self._build_ui()
        self._connect_controls()
        self._apply_style(force=True)
        self._set_selected_color(0)
        self.refresh_windows()
        QTimer.singleShot(1400, self._start_update_check)

    def _build_ui(self):
        central = QWidget()
        central.setObjectName("window")
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.hero = QFrame()
        self.hero.setObjectName("hero")
        hero_layout = QHBoxLayout(self.hero)
        hero_layout.setContentsMargins(22, 10, 22, 10)
        hero_text = QVBoxLayout()
        self.kicker = QLabel("ARKNIGHTS  /  PIXEL STUDIO")
        self.kicker.setObjectName("kicker")
        self.title_label = QLabel("24×24 像素画工坊")
        self.title_label.setObjectName("heroTitle")
        hero_text.addWidget(self.kicker)
        hero_text.addWidget(self.title_label)
        hero_text.setSpacing(1)
        hero_layout.addLayout(hero_text)
        self.subtitle = QLabel("上传 · 量化 · 手动修整 · 自动绘制")
        self.subtitle.setObjectName("subtitle")
        hero_layout.addWidget(self.subtitle, 1, Qt.AlignVCenter)
        self.logo = QLabel()
        self.logo.setObjectName("brandLogo")
        self.logo.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        transparent_logo = resource_path("assets", "tracer_logo_transparent.png")
        logo_path = transparent_logo if transparent_logo.exists() else resource_path("assets", "tracer_logo.png")
        self.logo_source = QPixmap(str(logo_path)) if logo_path.exists() else QPixmap()
        hero_layout.addWidget(self.logo, 0, Qt.AlignRight | Qt.AlignVCenter)
        root.addWidget(self.hero)

        self.splitter = QSplitter(Qt.Horizontal)
        self.splitter.setObjectName("mainSplitter")
        self.splitter.setChildrenCollapsible(False)
        self.left_panel = self._build_left_panel()
        self.center_panel = self._build_center_panel()
        self.palette_panel = self._build_palette_panel()
        self.splitter.addWidget(self.left_panel)
        self.splitter.addWidget(self.center_panel)
        self.splitter.addWidget(self.palette_panel)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setStretchFactor(2, 0)
        self.splitter.setSizes([270, 680, 215])
        root.addWidget(self.splitter, 1)

        footer = QFrame()
        footer.setObjectName("footer")
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(14, 5, 14, 5)
        self.footer_text = QLabel(
            "免责声明｜用户上传并于B站发布，请自行甄别、谨慎使用；严禁传播违法违规内容。"
            "  ·  建议 1280×720 / UI 100%  ·  F8停止"
        )
        footer_layout.addWidget(self.footer_text, 1)
        self.update_button = QPushButton("发现新版本")
        self.update_button.hide()
        footer_layout.addWidget(self.update_button)
        root.addWidget(footer)

    def _panel(self, object_name="panel"):
        panel = QFrame()
        panel.setObjectName(object_name)
        panel.setFrameShape(QFrame.NoFrame)
        return panel

    def _section_label(self, text):
        label = QLabel(text)
        label.setObjectName("section")
        return label

    def _build_left_panel(self):
        panel = self._panel("leftPanel")
        panel.setMinimumWidth(225)
        panel.setMaximumWidth(430)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(5)
        layout.addWidget(self._section_label("01  图片处理"))
        self.file_label = QLabel("尚未选择图片")
        self.file_label.setObjectName("muted")
        layout.addWidget(self.file_label)

        top_actions = QGridLayout()
        top_actions.setHorizontalSpacing(6)
        self.open_button = QPushButton("＋ 选择图片")
        self.crop_button = QPushButton("✂ 裁切")
        for button in (self.open_button, self.crop_button):
            button.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.crop_button.setEnabled(False)
        top_actions.addWidget(self.open_button, 0, 0, Qt.AlignHCenter)
        top_actions.addWidget(self.crop_button, 0, 1, Qt.AlignHCenter)
        top_actions.setColumnStretch(0, 1)
        top_actions.setColumnStretch(1, 1)
        layout.addLayout(top_actions)

        imports = QGridLayout()
        imports.setHorizontalSpacing(4)
        self.bitmap_button = QPushButton("▦ 24×24位图")
        self.share_button = QPushButton("▤ 官方分享")
        self.capture_button = QPushButton("▣ 截图导入")
        for col, button in enumerate((self.bitmap_button, self.share_button, self.capture_button)):
            imports.addWidget(button, 0, col)
            imports.setColumnStretch(col, 1)
        layout.addLayout(imports)

        self.fit_combo = QComboBox()
        self.fit_combo.addItems(("crop", "contain", "stretch"))
        self.resample_combo = QComboBox()
        self.resample_combo.addItems(tuple(RESAMPLE_METHODS))
        self.match_combo = QComboBox()
        self.match_combo.addItems(("经典RGB（原版）", "OKLab（感知）"))
        layout.addWidget(self.fit_combo)
        layout.addWidget(self.resample_combo)
        self.resample_slider, self.resample_value = self._labeled_slider("取样平滑比例", 100)
        layout.addLayout(self.resample_slider)
        layout.addWidget(self.match_combo)
        self.match_slider, self.match_value = self._labeled_slider("所选匹配算法比例", 100)
        layout.addLayout(self.match_slider)

        self.dither_check = QCheckBox("Floyd–Steinberg 抖动")
        self.transition_check = QCheckBox("减少过渡色（最多16色）")
        layout.addWidget(self.dither_check)
        layout.addWidget(self.transition_check)
        self.structure_slider, self.structure_value = self._labeled_slider("结构增强 / 去阴影", 0)
        layout.addLayout(self.structure_slider)

        separator = QFrame()
        separator.setFrameShape(QFrame.HLine)
        separator.setObjectName("separator")
        layout.addWidget(separator)

        header = QHBoxLayout()
        header.addWidget(self._section_label("02  自动化设置"))
        window_hint = QLabel("选择游戏窗口")
        window_hint.setObjectName("muted")
        header.addWidget(window_hint, 1, Qt.AlignRight)
        layout.addLayout(header)
        window_row = QHBoxLayout()
        self.window_combo = QComboBox()
        self.window_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.refresh_button = QPushButton("刷新")
        window_row.addWidget(self.window_combo, 1)
        window_row.addWidget(self.refresh_button)
        layout.addLayout(window_row)
        self.delay_slider, self.delay_value = self._labeled_slider("每格点击间隔", 60, 40, 120, " ms")
        layout.addLayout(self.delay_slider)

        self.action_box = QFrame()
        self.action_box.setObjectName("actionBox")
        action_layout = QVBoxLayout(self.action_box)
        action_layout.setContentsMargins(7, 6, 7, 7)
        action_layout.setSpacing(5)
        self.skip_white = QCheckBox("跳过白色格")
        self.skip_white.setChecked(True)
        action_layout.addWidget(self.skip_white)
        buttons = QGridLayout()
        buttons.setHorizontalSpacing(5)
        self.start_button = QPushButton("▶ 开始自动填充")
        self.start_button.setObjectName("primary")
        self.stop_button = QPushButton("■ 停止")
        self.stop_button.setObjectName("danger")
        buttons.addWidget(self.start_button, 0, 0)
        buttons.addWidget(self.stop_button, 0, 1)
        buttons.setColumnStretch(0, 3)
        buttons.setColumnStretch(1, 2)
        action_layout.addLayout(buttons)
        layout.addWidget(self.action_box)
        layout.addStretch(1)
        return panel

    def _labeled_slider(self, text, value, minimum=0, maximum=100, suffix="%"):
        layout = QHBoxLayout()
        layout.setSpacing(5)
        label = QLabel(text)
        label.setObjectName("muted")
        slider = QSlider(Qt.Horizontal)
        slider.setRange(minimum, maximum)
        slider.setValue(value)
        value_label = QLabel(f"{value}{suffix}")
        value_label.setObjectName("badge")
        value_label.setMinimumWidth(48)
        value_label.setAlignment(Qt.AlignCenter)
        slider.valueChanged.connect(lambda current: value_label.setText(f"{current}{suffix}"))
        layout.addWidget(label)
        layout.addWidget(slider, 1)
        layout.addWidget(value_label)
        layout.slider = slider
        return layout, value_label

    def _build_center_panel(self):
        panel = self._panel("centerPanel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(7)
        header = QHBoxLayout()
        header.addWidget(self._section_label("实时预览"))
        self.number_check = QCheckBox("显示色号")
        self.undo_button = QPushButton("撤销")
        self.reset_button = QPushButton("恢复转换结果")
        header.addWidget(self.number_check)
        header.addStretch(1)
        header.addWidget(self.undo_button)
        header.addWidget(self.reset_button)
        layout.addLayout(header)
        self.canvas = PixelCanvas()
        layout.addWidget(self.canvas, 1)
        bottom = QHBoxLayout()
        self.mini_preview = QLabel()
        self.mini_preview.setObjectName("miniPreview")
        self.mini_preview.setFixedSize(72, 72)
        self.mini_preview.setAlignment(Qt.AlignCenter)
        bottom.addWidget(self.mini_preview)
        status = QVBoxLayout()
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setTextVisible(False)
        self.status_label = QLabel("请选择一张图片。")
        self.status_label.setObjectName("muted")
        self.status_label.setWordWrap(True)
        status.addWidget(self.progress_bar)
        status.addWidget(self.status_label)
        bottom.addLayout(status, 1)
        layout.addLayout(bottom)
        return panel

    def _build_palette_panel(self):
        panel = self._panel("palettePanel")
        panel.setMinimumWidth(175)
        panel.setMaximumWidth(340)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(4)
        layout.addWidget(self._section_label("游戏调色板"))
        hint = QLabel("40 COLORS · 点击选择")
        hint.setObjectName("muted")
        layout.addWidget(hint)
        self.palette = PaletteWidget()
        layout.addWidget(self.palette, 1)
        self.color_label = QLabel()
        self.color_label.setObjectName("colorInfo")
        layout.addWidget(self.color_label)
        return panel

    def _connect_controls(self):
        self.open_button.clicked.connect(self.open_image)
        self.crop_button.clicked.connect(self.open_crop)
        self.bitmap_button.clicked.connect(self.open_bitmap)
        self.share_button.clicked.connect(self.open_official_share)
        self.capture_button.clicked.connect(self.capture_screen)
        self.refresh_button.clicked.connect(self.refresh_windows)
        self.start_button.clicked.connect(self.start_paint)
        self.stop_button.clicked.connect(self.stop_paint)
        self.update_button.clicked.connect(self.offer_update)
        self.palette.selected.connect(self._set_selected_color)
        self.canvas.colorPicked.connect(self._set_selected_color)
        self.canvas.matrixChanging.connect(self._canvas_changing)
        self.canvas.matrixEdited.connect(self._canvas_edited)
        self.number_check.toggled.connect(self._toggle_numbers)
        self.undo_button.clicked.connect(self.undo)
        self.reset_button.clicked.connect(self.restore_conversion)
        self.fit_combo.currentTextChanged.connect(self.reprocess)
        self.resample_combo.currentTextChanged.connect(self.reprocess)
        self.match_combo.currentTextChanged.connect(self.reprocess)
        self.dither_check.toggled.connect(self.reprocess)
        self.transition_check.toggled.connect(self._transition_toggled)
        for slider_layout in (self.resample_slider, self.match_slider, self.structure_slider):
            slider_layout.slider.valueChanged.connect(self._schedule_reprocess)
            slider_layout.slider.sliderReleased.connect(self._flush_reprocess)
        self.window_combo.currentIndexChanged.connect(self._window_changed)

    def _apply_style(self, force=False):
        logical_scale = max(0.84, min(1.28, min(self.width() / 1180, self.height() / 760)))
        bucket = round(logical_scale, 2)
        if not force and bucket == self._style_scale:
            return
        self._style_scale = bucket
        font = max(9, round(10 * logical_scale))
        muted = max(8, round(9 * logical_scale))
        section = max(10, round(11 * logical_scale))
        button_h = max(29, round(34 * logical_scale))
        radius = max(2, round(3 * logical_scale))
        self.setStyleSheet(f"""
            QWidget {{ font-family: 'Microsoft YaHei UI'; font-size: {font}px; color: #edf6f7; }}
            QWidget#window {{ background: #285f7d; }}
            QFrame#hero {{ background: #2f7fa7; border-bottom: {max(8, round(16*logical_scale))}px solid #286d91; }}
            QLabel#kicker {{ color: #d9f5ff; font-weight: 700; font-size: {muted}px; }}
            QLabel#heroTitle {{ color: white; font-weight: 800; font-size: {max(21, round(25*logical_scale))}px; }}
            QLabel#subtitle {{ color: #cde9f4; }}
            QFrame#leftPanel, QFrame#centerPanel, QFrame#palettePanel {{ background: #122331; border: 1px solid #34576a; }}
            QLabel#section {{ font-weight: 800; font-size: {section}px; }}
            QLabel#muted {{ color: #a9c3ce; font-size: {muted}px; }}
            QLabel#badge {{ color: #66d9ef; background: #1a3444; font-weight: 700; padding: 2px; }}
            QLabel#colorInfo, QFrame#actionBox {{ background: #1a3444; padding: {max(4,round(6*logical_scale))}px; }}
            QLabel#miniPreview {{ background: #f8f8f6; border: 1px solid #66d9ef; }}
            QPushButton, QComboBox {{ min-height: {button_h}px; background: #183344; border: 1px solid #8eb9ca; border-radius: {radius}px; padding: 0 {max(6,round(9*logical_scale))}px; }}
            QPushButton:hover, QComboBox:hover {{ background: #214b60; }}
            QPushButton:disabled {{ color: #68818c; border-color: #3b5864; }}
            QPushButton#primary {{ background: #f4a23a; color: #102431; font-weight: 800; border-color: #ffca6a; }}
            QPushButton#danger {{ background: #4d2830; color: #ffc2c6; border-color: #75404a; }}
            QComboBox QAbstractItemView {{ background: #101922; color: #edf6f7; selection-background-color: #228eb0; }}
            QCheckBox {{ spacing: 5px; }}
            QSlider::groove:horizontal {{ height: 4px; background: #0e171f; }}
            QSlider::handle:horizontal {{ width: 12px; margin: -5px 0; background: #66d9ef; border-radius: 6px; }}
            QProgressBar {{ height: 8px; background: #0e171f; border: 1px solid #5f8799; }}
            QProgressBar::chunk {{ background: #66d9ef; }}
            QFrame#footer {{ background: #102431; }}
            QSplitter::handle {{ background: #285f7d; width: {max(5, round(8*logical_scale))}px; }}
        """)
        hero_h = max(82, round(96 * logical_scale))
        self.hero.setFixedHeight(hero_h)
        if not self.logo_source.isNull():
            self.logo.setPixmap(self.logo_source.scaled(
                max(90, round(118 * logical_scale)),
                max(52, round(70 * logical_scale)),
                Qt.KeepAspectRatio,
                Qt.SmoothTransformation,
            ))
        self.left_panel.setMinimumWidth(max(225, round(245 * logical_scale)))
        self.palette_panel.setMinimumWidth(max(175, round(190 * logical_scale)))
        QTimer.singleShot(0, self._equalize_image_buttons)

    def _equalize_image_buttons(self):
        available = max(100, self.left_panel.width() - 32)
        width = max(72, (available - 6) // 2)
        self.open_button.setFixedWidth(width)
        self.crop_button.setFixedWidth(width)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._apply_style()

    def _schedule_reprocess(self, _value=None):
        if self.source_image is not None:
            # Restart one single-shot timer while the handle is moving.  The
            # numeric badge remains immediate, while expensive image
            # quantization runs only after the user pauses or releases it.
            self._reprocess_timer.start()

    def _flush_reprocess(self):
        if self._reprocess_timer.isActive():
            self._reprocess_timer.stop()
        algorithm_sliders = (
            self.resample_slider.slider,
            self.match_slider.slider,
            self.structure_slider.slider,
        )
        if any(slider.isSliderDown() for slider in algorithm_sliders):
            self._reprocess_timer.start()
            return
        if self.source_image is not None:
            self.reprocess()

    def _perceptual(self):
        return self.match_combo.currentText().startswith("OKLab")

    def _transition_toggled(self, enabled):
        if enabled:
            self.dither_check.setChecked(False)
        self.reprocess()

    def _toggle_numbers(self, enabled):
        self.canvas.set_show_numbers(enabled)
        self.palette.set_show_numbers(enabled)

    def _set_selected_color(self, index):
        self.selected_color = int(index)
        self.canvas.set_selected(index)
        self.palette.set_selected(index)
        rgb = PALETTE[index]
        self.color_label.setText(f"当前颜色\n#{index + 1:02d} · RGB {rgb[0]}, {rgb[1]}, {rgb[2]}")

    def _canvas_edited(self):
        if self.canvas.last_stroke_before is not None:
            self.history.append(self.canvas.last_stroke_before)
            self.canvas.last_stroke_before = None
        used = len({value for row in self.matrix for value in row})
        self.status_label.setText(f"手动修改完成：当前使用 {used} 种颜色。")

    def _canvas_changing(self):
        """Mirror each painted cell into the thumbnail before mouse release."""
        self.matrix = [row[:] for row in self.canvas.matrix]
        self._render_mini()

    def _render_matrix(self):
        self.canvas.set_matrix(self.matrix)
        self._render_mini()

    def _render_mini(self):
        image = matrix_to_image(self.matrix).resize((68, 68), Image.Resampling.NEAREST)
        self.mini_preview.setPixmap(pil_to_pixmap(image))

    def open_image(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择图片", "", "图片 (*.png *.jpg *.jpeg *.webp *.bmp *.gif);;所有文件 (*)"
        )
        if not path:
            return
        try:
            with Image.open(path) as source:
                image = ImageOps.exif_transpose(source).copy()
        except Exception as exc:
            QMessageBox.critical(self, "读取失败", str(exc))
            return
        self._load_regular(image, Path(path).name)
        self.open_crop()

    def _load_regular(self, image, file_name):
        self.source_image = ImageOps.exif_transpose(image).copy()
        self.source_file_name = file_name
        self.crop_box = None
        self.direct_bitmap_mode = False
        self.official_share_mode = False
        self.file_label.setText(elide_name(file_name))
        self.crop_button.setEnabled(True)
        for widget in (self.fit_combo, self.resample_combo, self.match_combo, self.dither_check,
                       self.transition_check, self.resample_slider.slider, self.match_slider.slider,
                       self.structure_slider.slider):
            widget.setEnabled(True)
        self.reprocess()

    def open_bitmap(self):
        path, _ = QFileDialog.getOpenFileName(self, "导入24×24 PNG位图", "", "PNG位图 (*.png)")
        if not path:
            return
        try:
            with Image.open(path) as source:
                if source.format != "PNG":
                    raise ValueError("位图直导仅支持PNG文件。")
                image = ImageOps.exif_transpose(source).copy()
            matrix = import_24_bitmap(
                image, perceptual=self._perceptual(),
                match_strength=self.match_slider.slider.value() / 100,
            )
        except Exception as exc:
            QMessageBox.critical(self, "位图导入失败", str(exc))
            return
        self.source_image = image
        self.source_file_name = Path(path).name
        self.crop_box = None
        self.direct_bitmap_mode = True
        self.official_share_mode = False
        self.matrix = matrix
        self.original_matrix = [row[:] for row in matrix]
        self.history.clear()
        self.file_label.setText(f"{elide_name(Path(path).name, 22)} · 位图直导")
        self.crop_button.setEnabled(False)
        self._set_direct_controls(True)
        self._render_matrix()
        self.status_label.setText(f"位图已逐像素导入，共使用 {len(set(sum(matrix, [])))} 种颜色。")

    def open_official_share(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "导入官方分享图", "", "官方分享图片 (*.png *.jpg *.jpeg *.webp)"
        )
        if not path:
            return
        try:
            with Image.open(path) as source:
                image = ImageOps.exif_transpose(source).copy()
            matrix, _bounds, confidence = detect_official_share_grid(image)
        except Exception as exc:
            QMessageBox.critical(self, "分享图导入失败", str(exc))
            return
        self.source_image = matrix_to_image(matrix)
        self.source_file_name = Path(path).name
        self.crop_box = None
        self.direct_bitmap_mode = True
        self.official_share_mode = True
        self.matrix = matrix
        self.original_matrix = [row[:] for row in matrix]
        self.history.clear()
        self.file_label.setText(f"{elide_name(Path(path).name, 20)} · 官方分享")
        self.crop_button.setEnabled(False)
        self._set_direct_controls(True)
        self._render_matrix()
        self.status_label.setText(
            f"官方分享图已恢复：{len(set(sum(matrix, [])))} 种颜色，定位置信度 {confidence:.0%}。"
        )

    def _set_direct_controls(self, direct):
        for widget in (self.fit_combo, self.resample_combo, self.dither_check,
                       self.transition_check, self.resample_slider.slider,
                       self.structure_slider.slider):
            widget.setEnabled(not direct)
        self.match_combo.setEnabled(not self.official_share_mode)
        self.match_slider.slider.setEnabled(not self.official_share_mode)

    def open_crop(self):
        if self.source_image is None:
            QMessageBox.information(self, "裁切图片", "请先选择一张图片。")
            return
        if self.direct_bitmap_mode:
            QMessageBox.information(self, "裁切图片", "位图直导或官方分享模式不进行裁切。")
            return
        dialog = CropDialog(self.source_image, self.crop_box, self)
        if dialog.exec() == QDialog.Accepted:
            self.crop_box = dialog.result_box
            self.file_label.setText(f"{elide_name(self.source_file_name, 22)} · 已裁切")
            self.reprocess()

    def capture_screen(self):
        self.hide()
        QApplication.processEvents()
        time.sleep(0.15)
        try:
            virtual = QApplication.primaryScreen().virtualGeometry()
            screenshot = ImageGrab.grab(all_screens=True)
            dialog = CaptureDialog(screenshot, virtual)
            accepted = dialog.exec() == QDialog.Accepted
            captured = dialog.result_image
        except Exception as exc:
            accepted, captured = False, None
            QMessageBox.critical(self, "截图失败", str(exc))
        finally:
            self.show()
            self.raise_()
            self.activateWindow()
        if accepted and captured is not None:
            self._load_regular(captured, f"截图-{time.strftime('%Y%m%d-%H%M%S')}.png")

    def reprocess(self, *_args):
        if self.source_image is None:
            return
        if self.direct_bitmap_mode:
            if self.official_share_mode:
                return
            self.matrix = import_24_bitmap(
                self.source_image, perceptual=self._perceptual(),
                match_strength=self.match_slider.slider.value() / 100,
            )
        else:
            source = self.source_image.crop(self.crop_box) if self.crop_box else self.source_image
            self.matrix = quantize_image(
                source,
                mode=self.fit_combo.currentText(),
                resample=self.resample_combo.currentText(),
                dither=self.dither_check.isChecked(),
                perceptual=self._perceptual(),
                reduce_transitions=self.transition_check.isChecked(),
                resample_strength=self.resample_slider.slider.value() / 100,
                match_strength=self.match_slider.slider.value() / 100,
                structure_strength=self.structure_slider.slider.value() / 100,
            )
        self.original_matrix = [row[:] for row in self.matrix]
        self.history.clear()
        self._render_matrix()
        self.status_label.setText(
            f"转换完成：使用 {len({value for row in self.matrix for value in row})} 种游戏颜色；可继续修改。"
        )

    def undo(self):
        if not self.history:
            return
        self.matrix = self.history.pop()
        self._render_matrix()

    def restore_conversion(self):
        self.matrix = [row[:] for row in self.original_matrix]
        self.history.clear()
        self._render_matrix()

    @staticmethod
    def _window_label(item):
        title = elide_name(item["title"], 31)
        return f"{title}  [{item['width']}×{item['height']}]  PID {item['pid']}"

    def refresh_windows(self):
        previous = self.window_combo.currentData()
        previous_hwnd = previous.get("hwnd") if isinstance(previous, dict) else None
        choices = list_selectable_windows()
        self.window_combo.blockSignals(True)
        self.window_combo.clear()
        self.window_combo.addItem("请选择游戏窗口", None)
        selected_index = 0
        for item in choices:
            self.window_combo.addItem(self._window_label(item), item)
            if item["hwnd"] == previous_hwnd:
                selected_index = self.window_combo.count() - 1
        self.window_combo.setCurrentIndex(selected_index)
        self.window_combo.blockSignals(False)
        if not choices:
            self.window_combo.setItemText(0, "未发现可选择窗口")

    def _window_changed(self, _index):
        target = self.window_combo.currentData()
        if target:
            self.status_label.setText(
                f"已选择游戏窗口：{target['title']}（{target['width']}×{target['height']}）"
            )

    def start_paint(self):
        if self.source_image is None:
            QMessageBox.warning(self, "没有图片", "请先选择或导入一张图片。")
            return
        target = self.window_combo.currentData()
        if not isinstance(target, dict):
            QMessageBox.warning(self, "未选择窗口", "请点击刷新并选择实际游戏窗口。")
            return
        answer = QMessageBox.question(
            self,
            "开始自动填充",
            "请确认游戏停留在24×24编辑画面，画布和调色板完整可见。\n"
            "开始后不要移动或缩放游戏窗口；F8可紧急停止。\n\n是否开始？",
        )
        if answer != QMessageBox.Yes:
            return
        self.progress_bar.setValue(0)
        self.status_label.setText("正在启动自动填充…")
        threading.Thread(
            target=self.painter.paint,
            args=(
                [row[:] for row in self.matrix],
                dict(target),
                self.delay_slider.slider.value() / 1000,
                self.skip_white.isChecked(),
            ),
            daemon=True,
        ).start()

    def stop_paint(self):
        self.painter.stop()
        self.status_label.setText("正在停止…")

    def _thread_status(self, text, error):
        self.status_label.setText(text)
        if error:
            QApplication.beep()

    def _thread_progress(self, done, total, color_index):
        self.progress_bar.setValue(0 if total <= 0 else round(done * 100 / total))
        if color_index >= 0:
            self.status_label.setText(f"自动填充中：{done}/{total} 格，当前颜色 #{color_index + 1}")

    @staticmethod
    def _version_tuple(value):
        return tuple(int(part) for part in re.findall(r"\d+", str(value))[:4]) or (0,)

    def _start_update_check(self):
        threading.Thread(target=self._check_update, daemon=True).start()

    def _check_update(self):
        try:
            request = urllib.request.Request(
                GITHUB_LATEST_RELEASE_API,
                headers={"Accept": "application/vnd.github+json", "User-Agent": f"Arknights-Pixel/{APP_VERSION}"},
            )
            with urllib.request.urlopen(request, timeout=7) as response:
                release = json.loads(response.read().decode("utf-8"))
            tag = str(release.get("tag_name", ""))
            if self._version_tuple(tag) <= self._version_tuple(APP_VERSION):
                return
            assets = release.get("assets") or []
            exe = next((asset for asset in assets if str(asset.get("name", "")).lower().endswith(".exe")), None)
            release_info = {
                "tag": tag,
                "url": (exe or {}).get("browser_download_url") or release.get("html_url") or GITHUB_RELEASES_URL,
            }
            self.bridge.updateAvailable.emit(release_info)
        except Exception:
            pass

    def _receive_update(self, release_info):
        self.latest_release = release_info
        self.status_label.setText(f"发现新版本 {release_info['tag']}，可在右下角下载。")
        self._show_update()

    def _show_update(self):
        if self.latest_release:
            self.update_button.setText(f"更新 {self.latest_release['tag']}")
            self.update_button.show()

    def offer_update(self):
        if self.latest_release and QMessageBox.question(
            self, "发现新版本", f"是否打开浏览器下载 {self.latest_release['tag']}？"
        ) == QMessageBox.Yes:
            webbrowser.open(self.latest_release["url"])

    def closeEvent(self, event):
        self.painter.stop()
        super().closeEvent(event)


def main():
    if not require_admin_before_startup():
        return 1
    app = QApplication(sys.argv)
    app.setApplicationName("Arknights Pixel Autofill")
    app.setOrganizationName("Tracer")
    app.setWindowIcon(QIcon(str(resource_path("arknights_pixel.ico"))))
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
