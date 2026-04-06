import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QPointF, QSize, QThread, Qt, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QAction, QCloseEvent, QColor, QIcon, QKeySequence, QPainter, QPainterPath, QPen, QPixmap, QShortcut
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QKeySequenceEdit,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSlider,
    QSplitter,
    QStatusBar,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".m4v", ".wmv", ".flv", ".webm"}
SOURCE_DIR_NAME = "分镜生成"
PASS_DIR_NAME = "审核通过"
FAIL_DIR_NAME = "不合格视频"
CONFIG_FILE_NAME = "review_config.json"
LOGO_FILE_NAME = "app_logo.png"

DEFAULT_SHORTCUTS = {
    "pass": "V",
    "fail": "N",
    "previous": "Y",
    "end": "E",
    "pause": "Space",
    "trim_in": "I",
    "trim_out": "O",
}

DEFAULT_SPLITTER_SIZES = {
    "body_horizontal": [280, 980, 280],
    "center_vertical": [720, 150],
}

MIN_LEFT_PANEL_WIDTH = 160
MIN_RIGHT_PANEL_WIDTH = 220
MIN_VIDEO_HEIGHT = 240
MIN_TIMELINE_HEIGHT = 108
MIN_TIMELINE_HEIGHT_COLLAPSED = 44


@dataclass
class VideoItem:
    source_path: Path
    relative_path: Path
    status: Optional[str] = None  # None / pass / fail / trim_pass
    duration_ms: int = 0
    trim_in_ms: Optional[int] = None
    trim_out_ms: Optional[int] = None
    clip_output_path: Optional[Path] = None


class WaveformWidget(QWidget):
    seek_requested = Signal(float)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(88)
        self.peaks: Optional[list[float]] = None
        self.playhead_fraction = 0.0
        self.in_fraction: Optional[float] = None
        self.out_fraction: Optional[float] = None
        self.status_text = "等待加载波形"

    def clear(self, text: str = "等待加载波形") -> None:
        self.peaks = None
        self.playhead_fraction = 0.0
        self.in_fraction = None
        self.out_fraction = None
        self.status_text = text
        self.update()

    def set_peaks(self, peaks: list[float], text: str = "") -> None:
        self.peaks = peaks
        self.status_text = text
        self.update()

    def set_status(self, text: str) -> None:
        self.peaks = []
        self.status_text = text
        self.update()

    def set_playhead_fraction(self, fraction: float) -> None:
        self.playhead_fraction = max(0.0, min(1.0, fraction))
        self.update()

    def set_selection(self, in_fraction: Optional[float], out_fraction: Optional[float]) -> None:
        self.in_fraction = None if in_fraction is None else max(0.0, min(1.0, in_fraction))
        self.out_fraction = None if out_fraction is None else max(0.0, min(1.0, out_fraction))
        self.update()

    def _fraction_from_x(self, x: int) -> float:
        rect = self.rect().adjusted(8, 8, -8, -8)
        if rect.width() <= 1:
            return 0.0
        return max(0.0, min(1.0, (x - rect.left()) / rect.width()))

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.seek_requested.emit(self._fraction_from_x(int(event.position().x())))
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if event.buttons() & Qt.MouseButton.LeftButton:
            self.seek_requested.emit(self._fraction_from_x(int(event.position().x())))
        super().mouseMoveEvent(event)

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        rect = self.rect().adjusted(0, 0, -1, -1)
        painter.fillRect(rect, Qt.GlobalColor.white)
        painter.setPen(QPen(Qt.GlobalColor.lightGray, 1))
        painter.drawRect(rect)

        inner = rect.adjusted(8, 8, -8, -8)
        if inner.width() <= 0 or inner.height() <= 0:
            return

        if self.peaks is None:
            painter.setPen(QPen(Qt.GlobalColor.darkGray, 1))
            painter.drawText(inner, Qt.AlignmentFlag.AlignCenter, self.status_text)
            return

        if self.in_fraction is not None and self.out_fraction is not None and self.out_fraction > self.in_fraction:
            start_x = inner.left() + int(self.in_fraction * inner.width())
            end_x = inner.left() + int(self.out_fraction * inner.width())
            painter.fillRect(start_x, inner.top(), max(1, end_x - start_x), inner.height(), Qt.GlobalColor.cyan)

        if not self.peaks:
            painter.setPen(QPen(Qt.GlobalColor.darkGray, 1))
            painter.drawText(inner, Qt.AlignmentFlag.AlignCenter, self.status_text or "当前视频没有可显示的音频")
            return

        count = len(self.peaks)
        width = max(1, inner.width())
        height = max(1, inner.height())
        center_y = inner.top() + height / 2
        played_index = int(self.playhead_fraction * max(1, count - 1))
        pen_width = max(1, int(width / max(1, count) + 0.45))
        played_pen = QPen(Qt.GlobalColor.darkCyan, pen_width)
        future_pen = QPen(Qt.GlobalColor.gray, pen_width)

        for i, value in enumerate(self.peaks):
            x = inner.left() + int((i + 0.5) * width / count)
            amplitude = max(1, int(value * (height * 0.45)))
            painter.setPen(played_pen if i <= played_index else future_pen)
            painter.drawLine(x, int(center_y - amplitude), x, int(center_y + amplitude))

        if self.in_fraction is not None:
            in_x = inner.left() + int(self.in_fraction * width)
            painter.setPen(QPen(Qt.GlobalColor.blue, 2))
            painter.drawLine(in_x, inner.top(), in_x, inner.bottom())

        if self.out_fraction is not None:
            out_x = inner.left() + int(self.out_fraction * width)
            painter.setPen(QPen(Qt.GlobalColor.darkYellow, 2))
            painter.drawLine(out_x, inner.top(), out_x, inner.bottom())

        playhead_x = inner.left() + int(self.playhead_fraction * width)
        painter.setPen(QPen(Qt.GlobalColor.red, 2))
        painter.drawLine(playhead_x, inner.top(), playhead_x, inner.bottom())


class WaveformThread(QThread):
    finished_waveform = Signal(str, object, str)

    def __init__(self, video_path: Path) -> None:
        super().__init__()
        self.video_path = video_path

    def run(self) -> None:
        try:
            peaks = extract_waveform_peaks(self.video_path)
            self.finished_waveform.emit(str(self.video_path), peaks, "")
        except Exception as exc:  # pragma: no cover
            self.finished_waveform.emit(str(self.video_path), [], str(exc))


def build_eye_icon(visible: bool, size: int = 18) -> QIcon:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

    pen = QPen(QColor("#4b5563"), 1.8)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)

    left = 2.0
    top = 4.0
    right = size - 2.0
    bottom = size - 4.0
    mid_x = size / 2.0
    mid_y = size / 2.0

    path = QPainterPath()
    path.moveTo(left, mid_y)
    path.quadTo(mid_x, top, right, mid_y)
    path.quadTo(mid_x, bottom, left, mid_y)
    path.closeSubpath()
    painter.drawPath(path)

    pupil_radius = max(2.0, size * 0.12)
    if visible:
        painter.setBrush(QColor("#4b5563"))
        painter.drawEllipse(QPointF(mid_x, mid_y), pupil_radius, pupil_radius)
    else:
        painter.drawEllipse(QPointF(mid_x, mid_y), pupil_radius * 0.8, pupil_radius * 0.8)
        slash_pen = QPen(QColor("#ef4444"), 1.9)
        slash_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(slash_pen)
        painter.drawLine(int(size * 0.24), int(size * 0.8), int(size * 0.78), int(size * 0.22))

    painter.end()
    return QIcon(pixmap)


class SettingsDialog(QDialog):
    def __init__(
        self,
        parent: QWidget,
        base_dir: Path,
        source_dir: Path,
        pass_dir: Path,
        fail_dir: Path,
        shortcuts: dict[str, str],
        logo_path: Optional[Path],
    ) -> None:
        super().__init__(parent)
        self.base_dir = base_dir
        self.setWindowTitle("设置")
        self.resize(880, 460)

        root = QVBoxLayout(self)
        form = QFormLayout()
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(12)

        self.source_edit, source_row = self._build_path_row(str(source_dir))
        self.pass_edit, pass_row = self._build_path_row(str(pass_dir))
        self.fail_edit, fail_row = self._build_path_row(str(fail_dir))
        self.logo_edit, logo_row = self._build_file_row(str(logo_path) if logo_path else "")

        form.addRow("源文件目录", source_row)
        form.addRow("通过目录", pass_row)
        form.addRow("不通过目录", fail_row)
        form.addRow("Logo 图片", logo_row)

        shortcut_widget = QWidget()
        shortcut_layout = QGridLayout(shortcut_widget)
        shortcut_layout.setContentsMargins(0, 0, 0, 0)
        shortcut_layout.setHorizontalSpacing(18)
        shortcut_layout.setVerticalSpacing(10)

        self.pass_key_edit = QKeySequenceEdit(QKeySequence(shortcuts.get("pass", DEFAULT_SHORTCUTS["pass"])))
        self.fail_key_edit = QKeySequenceEdit(QKeySequence(shortcuts.get("fail", DEFAULT_SHORTCUTS["fail"])))
        self.previous_key_edit = QKeySequenceEdit(QKeySequence(shortcuts.get("previous", DEFAULT_SHORTCUTS["previous"])))
        self.end_key_edit = QKeySequenceEdit(QKeySequence(shortcuts.get("end", DEFAULT_SHORTCUTS["end"])))
        self.pause_key_edit = QKeySequenceEdit(QKeySequence(shortcuts.get("pause", DEFAULT_SHORTCUTS["pause"])))
        self.trim_in_key_edit = QKeySequenceEdit(QKeySequence(shortcuts.get("trim_in", DEFAULT_SHORTCUTS["trim_in"])))
        self.trim_out_key_edit = QKeySequenceEdit(QKeySequence(shortcuts.get("trim_out", DEFAULT_SHORTCUTS["trim_out"])))

        shortcut_layout.addWidget(QLabel("通过"), 0, 0)
        shortcut_layout.addWidget(self.pass_key_edit, 0, 1)
        shortcut_layout.addWidget(QLabel("不通过"), 0, 2)
        shortcut_layout.addWidget(self.fail_key_edit, 0, 3)
        shortcut_layout.addWidget(QLabel("返回上一条"), 1, 0)
        shortcut_layout.addWidget(self.previous_key_edit, 1, 1)
        shortcut_layout.addWidget(QLabel("结束审核"), 1, 2)
        shortcut_layout.addWidget(self.end_key_edit, 1, 3)
        shortcut_layout.addWidget(QLabel("暂停/继续"), 2, 0)
        shortcut_layout.addWidget(self.pause_key_edit, 2, 1)
        shortcut_layout.addWidget(QLabel("起点 I"), 2, 2)
        shortcut_layout.addWidget(self.trim_in_key_edit, 2, 3)
        shortcut_layout.addWidget(QLabel("终点 O"), 3, 0)
        shortcut_layout.addWidget(self.trim_out_key_edit, 3, 1)

        form.addRow("快捷键", shortcut_widget)
        root.addLayout(form)

        tip = QLabel("I：设置裁剪起点；O：将 I 到 O 的区间直接裁剪到通过目录。若未设置 I，则默认从 0 秒裁到 O。")
        tip.setWordWrap(True)
        tip.setStyleSheet("color:#666;")
        root.addWidget(tip)

        btn_row = QHBoxLayout()
        layout_reset_btn = QPushButton("重置布局比例")
        btn_row.addWidget(layout_reset_btn)
        btn_row.addStretch(1)
        reset_btn = QPushButton("恢复默认")
        cancel_btn = QPushButton("取消")
        save_btn = QPushButton("保存")
        layout_reset_btn.clicked.connect(self.reset_layout_from_dialog)
        reset_btn.clicked.connect(self.reset_defaults)
        cancel_btn.clicked.connect(self.reject)
        save_btn.clicked.connect(self.validate_and_accept)
        btn_row.addWidget(reset_btn)
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(save_btn)
        root.addLayout(btn_row)

    def _build_path_row(self, value: str) -> tuple[QLineEdit, QWidget]:
        row_widget = QWidget()
        row_layout = QHBoxLayout(row_widget)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(8)
        edit = QLineEdit(value)
        browse_btn = QPushButton("浏览")
        browse_btn.clicked.connect(lambda: self._choose_directory(edit))
        row_layout.addWidget(edit, 1)
        row_layout.addWidget(browse_btn)
        return edit, row_widget

    def _build_file_row(self, value: str) -> tuple[QLineEdit, QWidget]:
        row_widget = QWidget()
        row_layout = QHBoxLayout(row_widget)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(8)
        edit = QLineEdit(value)
        browse_btn = QPushButton("选择图片")
        browse_btn.clicked.connect(lambda: self._choose_file(edit))
        row_layout.addWidget(edit, 1)
        row_layout.addWidget(browse_btn)
        return edit, row_widget

    def _choose_directory(self, target_edit: QLineEdit) -> None:
        start_dir = target_edit.text().strip() or str(self.base_dir)
        selected = QFileDialog.getExistingDirectory(self, "选择目录", start_dir)
        if selected:
            target_edit.setText(selected)

    def _choose_file(self, target_edit: QLineEdit) -> None:
        start_dir = target_edit.text().strip() or str(self.base_dir)
        selected, _ = QFileDialog.getOpenFileName(
            self,
            "选择图片",
            start_dir,
            "Images (*.png *.jpg *.jpeg *.bmp *.webp *.ico)",
        )
        if selected:
            target_edit.setText(selected)

    def reset_defaults(self) -> None:
        self.source_edit.setText(str(self.base_dir / SOURCE_DIR_NAME))
        self.pass_edit.setText(str(self.base_dir / PASS_DIR_NAME))
        self.fail_edit.setText(str(self.base_dir / FAIL_DIR_NAME))
        self.logo_edit.setText(str(self.base_dir / LOGO_FILE_NAME))
        self.pass_key_edit.setKeySequence(QKeySequence(DEFAULT_SHORTCUTS["pass"]))
        self.fail_key_edit.setKeySequence(QKeySequence(DEFAULT_SHORTCUTS["fail"]))
        self.previous_key_edit.setKeySequence(QKeySequence(DEFAULT_SHORTCUTS["previous"]))
        self.end_key_edit.setKeySequence(QKeySequence(DEFAULT_SHORTCUTS["end"]))
        self.pause_key_edit.setKeySequence(QKeySequence(DEFAULT_SHORTCUTS["pause"]))
        self.trim_in_key_edit.setKeySequence(QKeySequence(DEFAULT_SHORTCUTS["trim_in"]))
        self.trim_out_key_edit.setKeySequence(QKeySequence(DEFAULT_SHORTCUTS["trim_out"]))

    def reset_layout_from_dialog(self) -> None:
        parent = self.parent()
        if parent is None or not hasattr(parent, "reset_layout_proportions"):
            QMessageBox.information(self, "重置布局", "当前窗口不支持布局重置。")
            return
        reply = QMessageBox.question(
            self,
            "重置布局比例",
            "将左侧、视频区、底部时间轴、右侧记录区恢复为默认比例。是否继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        parent.reset_layout_proportions()
        QMessageBox.information(self, "重置布局", "布局比例已恢复默认，并已自动保存。")

    def get_values(self) -> dict:
        return {
            "source_dir": self.source_edit.text().strip(),
            "pass_dir": self.pass_edit.text().strip(),
            "fail_dir": self.fail_edit.text().strip(),
            "logo_path": self.logo_edit.text().strip(),
            "shortcuts": {
                "pass": self.pass_key_edit.keySequence().toString(QKeySequence.SequenceFormat.PortableText),
                "fail": self.fail_key_edit.keySequence().toString(QKeySequence.SequenceFormat.PortableText),
                "previous": self.previous_key_edit.keySequence().toString(QKeySequence.SequenceFormat.PortableText),
                "end": self.end_key_edit.keySequence().toString(QKeySequence.SequenceFormat.PortableText),
                "pause": self.pause_key_edit.keySequence().toString(QKeySequence.SequenceFormat.PortableText),
                "trim_in": self.trim_in_key_edit.keySequence().toString(QKeySequence.SequenceFormat.PortableText),
                "trim_out": self.trim_out_key_edit.keySequence().toString(QKeySequence.SequenceFormat.PortableText),
            },
        }

    def validate_and_accept(self) -> None:
        values = self.get_values()
        if any(not values[key] for key in ("source_dir", "pass_dir", "fail_dir")):
            QMessageBox.warning(self, "设置无效", "源文件目录、通过目录、不通过目录都不能为空。")
            return

        shortcut_values = values["shortcuts"]
        if any(not value for value in shortcut_values.values()):
            QMessageBox.warning(self, "设置无效", "所有快捷键都必须设置。")
            return

        seen: dict[str, str] = {}
        labels = {
            "pass": "通过",
            "fail": "不通过",
            "previous": "返回上一条",
            "end": "结束审核",
            "pause": "暂停/继续",
            "trim_in": "起点 I",
            "trim_out": "终点 O",
        }
        for key, value in shortcut_values.items():
            normalized = value.upper()
            if normalized in seen:
                QMessageBox.warning(self, "快捷键冲突", f"“{labels[key]}” 与 “{labels[seen[normalized]]}” 使用了相同快捷键：{value}")
                return
            seen[normalized] = key

        self.accept()


class ReviewWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.base_dir = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
        self.resource_dir = Path(getattr(sys, "_MEIPASS", self.base_dir))
        self.config_path = self.base_dir / CONFIG_FILE_NAME

        self.source_dir = self.base_dir / SOURCE_DIR_NAME
        self.pass_dir = self.base_dir / PASS_DIR_NAME
        self.fail_dir = self.base_dir / FAIL_DIR_NAME
        self.logo_path: Optional[Path] = None
        self.shortcuts_map = DEFAULT_SHORTCUTS.copy()
        self.splitter_sizes = json.loads(json.dumps(DEFAULT_SPLITTER_SIZES))
        self.waveform_visible = True

        self.items: list[VideoItem] = []
        self.current_index = -1
        self.review_active = False
        self.moved_after_finish = False
        self.is_slider_dragging = False
        self.was_playing_before_drag = False
        self.waveform_thread: Optional[WaveformThread] = None
        self.waveform_request_path: Optional[str] = None
        self.shortcut_objects: dict[str, QShortcut] = {}

        self._load_config()

        self.setWindowTitle("视频审核工具")
        self.resize(1500, 920)
        self._apply_logo()
        self._build_ui()
        self._build_player()
        self._bind_shortcuts()
        self.refresh_settings_display()
        self._restore_splitter_sizes()
        self._load_videos()

    def _load_config(self) -> None:
        if not self.config_path.exists():
            self.logo_path = self._default_logo_path()
            return
        try:
            data = json.loads(self.config_path.read_text(encoding="utf-8"))
        except Exception:
            self.logo_path = self._default_logo_path()
            return

        self.source_dir = self._resolve_saved_path(data.get("source_dir"), self.base_dir / SOURCE_DIR_NAME)
        self.pass_dir = self._resolve_saved_path(data.get("pass_dir"), self.base_dir / PASS_DIR_NAME)
        self.fail_dir = self._resolve_saved_path(data.get("fail_dir"), self.base_dir / FAIL_DIR_NAME)
        self.logo_path = self._resolve_saved_path(data.get("logo_path"), self._default_logo_path())
        self.shortcuts_map = DEFAULT_SHORTCUTS | data.get("shortcuts", {})
        saved_splitters = data.get("splitter_sizes", {})
        for key, default_value in DEFAULT_SPLITTER_SIZES.items():
            raw_value = saved_splitters.get(key, default_value)
            if isinstance(raw_value, list) and all(isinstance(x, int) and x > 0 for x in raw_value):
                self.splitter_sizes[key] = raw_value
        self.waveform_visible = bool(data.get("waveform_visible", True))
        geometry = data.get("window_geometry")
        if isinstance(geometry, dict):
            x = geometry.get("x")
            y = geometry.get("y")
            w = geometry.get("width")
            h = geometry.get("height")
            if all(isinstance(v, int) for v in (x, y, w, h)):
                self.setGeometry(x, y, max(900, w), max(640, h))

    def save_config(self) -> None:
        data = {
            "source_dir": str(self.source_dir),
            "pass_dir": str(self.pass_dir),
            "fail_dir": str(self.fail_dir),
            "logo_path": str(self.logo_path) if self.logo_path else "",
            "shortcuts": self.shortcuts_map,
            "splitter_sizes": self.splitter_sizes,
            "waveform_visible": self.waveform_visible,
            "window_geometry": {
                "x": self.x(),
                "y": self.y(),
                "width": self.width(),
                "height": self.height(),
            },
        }
        try:
            self.config_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _resolve_saved_path(self, raw_value: Optional[str], fallback: Path) -> Path:
        if not raw_value:
            return fallback
        path = Path(raw_value)
        if path.is_absolute():
            return path
        return (self.base_dir / path).resolve()

    def _default_logo_path(self) -> Optional[Path]:
        candidates = [self.base_dir / LOGO_FILE_NAME, self.resource_dir / LOGO_FILE_NAME]
        for candidate in candidates:
            if candidate.exists():
                return candidate
        return None

    def _apply_logo(self) -> None:
        if self.logo_path and self.logo_path.exists():
            self.setWindowIcon(QIcon(str(self.logo_path)))

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        page = QVBoxLayout(central)
        page.setContentsMargins(8, 8, 8, 8)
        page.setSpacing(6)

        self.header_region = self._build_header_region()
        self.header_region.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        page.addWidget(self.header_region)

        self.body_horizontal_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.body_horizontal_splitter.setChildrenCollapsible(False)
        self.body_horizontal_splitter.setHandleWidth(8)
        self.body_horizontal_splitter.splitterMoved.connect(lambda *_: self._remember_splitter_sizes("body_horizontal", self.body_horizontal_splitter))
        page.addWidget(self.body_horizontal_splitter, 1)

        self.left_region = self._build_left_region()
        self.left_region.setMinimumWidth(MIN_LEFT_PANEL_WIDTH)
        self.body_horizontal_splitter.addWidget(self.left_region)

        self.center_vertical_splitter = QSplitter(Qt.Orientation.Vertical)
        self.center_vertical_splitter.setChildrenCollapsible(False)
        self.center_vertical_splitter.setHandleWidth(8)
        self.center_vertical_splitter.splitterMoved.connect(lambda *_: self._remember_splitter_sizes("center_vertical", self.center_vertical_splitter))
        self.body_horizontal_splitter.addWidget(self.center_vertical_splitter)

        self.video_region = self._build_video_region()
        self.video_region.setMinimumHeight(MIN_VIDEO_HEIGHT)
        self.center_vertical_splitter.addWidget(self.video_region)

        self.timeline_region = self._build_timeline_region()
        self.timeline_region.setMinimumHeight(MIN_TIMELINE_HEIGHT)
        self.center_vertical_splitter.addWidget(self.timeline_region)

        self.right_region = self._build_right_region()
        self.right_region.setMinimumWidth(MIN_RIGHT_PANEL_WIDTH)
        self.body_horizontal_splitter.addWidget(self.right_region)

        self.body_horizontal_splitter.setStretchFactor(0, 0)
        self.body_horizontal_splitter.setStretchFactor(1, 1)
        self.body_horizontal_splitter.setStretchFactor(2, 0)
        self.center_vertical_splitter.setStretchFactor(0, 1)
        self.center_vertical_splitter.setStretchFactor(1, 0)

        self.setStatusBar(QStatusBar())
        self._build_menu()

    def _build_header_region(self) -> QWidget:
        box = QWidget()
        box.setFixedHeight(42)
        layout = QHBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self.btn_settings = QPushButton("设置")
        self.btn_settings.setFixedHeight(32)
        self.btn_settings.setMinimumWidth(64)
        self.btn_settings.clicked.connect(self.open_settings_dialog)

        layout.addWidget(self.btn_settings, 0, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        layout.addStretch(1)
        return box

    def _build_left_region(self) -> QWidget:
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        title = QLabel("待审核队列")
        title.setStyleSheet("font-size:18px; font-weight:700;")
        self.summary_label = QLabel("未开始")
        self.summary_label.setWordWrap(True)
        self.queue_list = QListWidget()
        self.queue_list.setMinimumWidth(MIN_LEFT_PANEL_WIDTH - 16)
        self.queue_list.setAlternatingRowColors(True)
        self.queue_list.itemDoubleClicked.connect(self.jump_to_item)

        layout.addWidget(title)
        layout.addWidget(self.summary_label)
        layout.addWidget(self.queue_list, 1)
        return box

    def _build_video_region(self) -> QWidget:
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        self.video_title = QLabel("当前视频：")
        self.video_title.setStyleSheet("font-size:18px; font-weight:700;")
        self.video_widget = QVideoWidget()
        self.video_widget.setStyleSheet("background:#111;")
        self.video_widget.setMinimumSize(180, 180)

        layout.addWidget(self.video_title)
        layout.addWidget(self.video_widget, 1)
        return box

    def _build_timeline_region(self) -> QWidget:
        box = QWidget()
        box.setMinimumHeight(MIN_TIMELINE_HEIGHT)
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        self.time_label = QLabel("00:00 / 00:00")
        self.time_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.time_label.setStyleSheet("color:#666; font-size:12px; padding-bottom:1px;")
        layout.addWidget(self.time_label)

        self.position_slider = QSlider(Qt.Orientation.Horizontal)
        self.position_slider.setRange(0, 0)
        self.position_slider.setSingleStep(100)
        self.position_slider.sliderPressed.connect(self.on_slider_pressed)
        self.position_slider.sliderMoved.connect(self.on_slider_moved)
        self.position_slider.sliderReleased.connect(self.on_slider_released)
        layout.addWidget(self.position_slider)

        self.waveform_wrap = QWidget()
        self.waveform_wrap.setMinimumHeight(64)
        wave_grid = QGridLayout(self.waveform_wrap)
        wave_grid.setContentsMargins(0, 0, 0, 0)
        wave_grid.setSpacing(0)

        self.waveform_widget = WaveformWidget()
        self.waveform_widget.setMinimumHeight(64)
        self.waveform_widget.seek_requested.connect(self.on_waveform_seek_requested)
        wave_grid.addWidget(self.waveform_widget, 0, 0)

        self.btn_toggle_waveform = QPushButton()
        self.btn_toggle_waveform.setFixedSize(28, 22)
        self.btn_toggle_waveform.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_toggle_waveform.setStyleSheet(
            "QPushButton{border:1px solid #d1d5db;border-radius:6px;background:rgba(255,255,255,0.92);padding:0 2px;margin:0 4px 4px 0;}"
            "QPushButton:hover{background:#f5f7fa;}"
            "QPushButton:pressed{background:#eef2f7;}"
        )
        self.btn_toggle_waveform.clicked.connect(self.toggle_waveform_visible)
        self.btn_toggle_waveform.setToolTip("隐藏波形")
        wave_grid.addWidget(
            self.btn_toggle_waveform,
            0,
            0,
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignBottom,
        )
        layout.addWidget(self.waveform_wrap, 1)

        self.path_info = QLabel()
        self.path_info.setWordWrap(True)
        self.path_info.setStyleSheet("color:#666;")
        return box

    def _build_right_region(self) -> QWidget:
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        title = QLabel("审核记录")
        title.setStyleSheet("font-size:18px; font-weight:700;")
        self.log_text = QTextEdit()
        self.log_text.setMinimumWidth(MIN_RIGHT_PANEL_WIDTH - 16)
        self.log_text.setReadOnly(True)
        self.log_text.setPlaceholderText("这里会显示审核记录与结束后的处理结果。")

        layout.addWidget(title)
        layout.addWidget(self.path_info)
        layout.addWidget(self.log_text, 1)
        return box

    def _build_menu(self) -> None:
        menu = self.menuBar().addMenu("文件")
        action_open = QAction("打开程序目录", self)
        action_open.triggered.connect(self.open_base_dir)
        menu.addAction(action_open)
        action_settings = QAction("设置", self)
        action_settings.triggered.connect(self.open_settings_dialog)
        menu.addAction(action_settings)

    def _build_player(self) -> None:
        self.player = QMediaPlayer(self)
        self.audio_output = QAudioOutput(self)
        self.audio_output.setVolume(1.0)
        self.player.setAudioOutput(self.audio_output)
        self.player.setVideoOutput(self.video_widget)
        self.player.mediaStatusChanged.connect(self.on_media_status_changed)
        self.player.errorOccurred.connect(self.on_player_error)
        self.player.positionChanged.connect(self.on_position_changed)
        self.player.durationChanged.connect(self.on_duration_changed)

    def _restore_splitter_sizes(self) -> None:
        QTimer.singleShot(0, self._apply_splitter_sizes)

    def _apply_splitter_sizes(self) -> None:
        body_sizes = self.splitter_sizes.get("body_horizontal", DEFAULT_SPLITTER_SIZES["body_horizontal"])
        center_sizes = self.splitter_sizes.get("center_vertical", DEFAULT_SPLITTER_SIZES["center_vertical"])
        self.body_horizontal_splitter.setSizes(body_sizes)
        self.center_vertical_splitter.setSizes(center_sizes)
        self._update_waveform_visibility_ui()

    def _remember_splitter_sizes(self, key: str, splitter: QSplitter) -> None:
        sizes = [int(x) for x in splitter.sizes() if int(x) > 0]
        if sizes:
            self.splitter_sizes[key] = sizes
            self.save_config()

    def reset_layout_proportions(self) -> None:
        self.splitter_sizes = json.loads(json.dumps(DEFAULT_SPLITTER_SIZES))
        self._apply_splitter_sizes()
        self.save_config()
        self.log("布局比例已恢复默认。")

    def _bind_shortcuts(self) -> None:
        bindings = {
            "pass": self.mark_pass,
            "fail": self.mark_fail,
            "previous": self.go_previous,
            "end": self.finish_review,
            "pause": self.toggle_pause,
            "trim_in": self.mark_trim_in,
            "trim_out": self.mark_trim_out_and_save,
        }
        for key, callback in bindings.items():
            shortcut = self.shortcut_objects.get(key)
            if shortcut is None:
                shortcut = QShortcut(self)
                shortcut.activated.connect(callback)
                self.shortcut_objects[key] = shortcut
            shortcut.setKey(QKeySequence(self.shortcuts_map[key]))
        self.refresh_shortcut_texts()

    def refresh_shortcut_texts(self) -> None:
        native = lambda key: QKeySequence(self.shortcuts_map[key]).toString(QKeySequence.SequenceFormat.NativeText)
        self.shortcut_summary_text = (
            f"快捷键：{native('pause')}=暂停/继续，{native('trim_in')}=起点，{native('trim_out')}=裁剪通过，"
            f"{native('pass')}=通过，{native('fail')}=不通过，{native('previous')}=返回上一条，{native('end')}=结束审核"
        )
        self.statusBar().showMessage(self.shortcut_summary_text, 8000)

    def refresh_settings_display(self) -> None:
        self.path_info.setText(
            f"源目录：{self.source_dir}\n"
            f"通过目录：{self.pass_dir}\n"
            f"不通过目录：{self.fail_dir}\n"
            f"说明：普通通过/不通过在结束审核后统一处理；I/O 裁剪通过会即时输出到通过目录。"
        )
        self.refresh_shortcut_texts()

    def _update_logo_preview(self) -> None:
        if not hasattr(self, "logo_preview"):
            return
        pix = QPixmap()
        if self.logo_path and self.logo_path.exists():
            pix.load(str(self.logo_path))
        if not pix.isNull():
            self.logo_preview.setPixmap(pix.scaled(self.logo_preview.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        else:
            self.logo_preview.setText("Logo")

    def _load_videos(self) -> None:
        self.items.clear()
        self.queue_list.clear()
        self.current_index = -1
        self.review_active = False
        self.moved_after_finish = False
        self.player.stop()
        self.position_slider.setRange(0, 0)
        self.position_slider.setValue(0)
        self.time_label.setText("00:00 / 00:00")
        self.waveform_widget.clear()
        self.log_text.clear()

        if not self.source_dir.exists():
            self.summary_label.setText(f"未找到待审核目录：{self.source_dir}")
            self.log(f"未找到待审核目录：{self.source_dir}")
            self._refresh_bottom_status(None)
            return

        files = self._collect_video_files(self.source_dir)
        for file_path in files:
            relative_path = file_path.relative_to(self.source_dir)
            self.items.append(VideoItem(source_path=file_path, relative_path=relative_path))
            self.queue_list.addItem(QListWidgetItem(str(relative_path)))

        if not self.items:
            self.summary_label.setText("没有找到可审核视频。")
            self.log("待审核目录中没有找到视频文件。")
            self._refresh_bottom_status(None)
            return

        self.review_active = True
        self.current_index = 0
        self.update_queue_view()
        self.load_current_video()
        self.log(f"已加载 {len(self.items)} 个视频，准备开始审核。")

    def _collect_video_files(self, directory: Path) -> list[Path]:
        files = [p for p in directory.rglob("*") if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS]
        return sorted(files, key=lambda p: self.natural_sort_key(str(p.relative_to(directory))))

    @staticmethod
    def natural_sort_key(text: str):
        import re
        return [int(token) if token.isdigit() else token.lower() for token in re.split(r"(\d+)", text)]

    def update_queue_view(self) -> None:
        pending = sum(1 for x in self.items if x.status is None)
        passed = sum(1 for x in self.items if x.status == "pass")
        failed = sum(1 for x in self.items if x.status == "fail")
        trimmed = sum(1 for x in self.items if x.status == "trim_pass")
        total = len(self.items)
        current = self.current_index + 1 if 0 <= self.current_index < total else 0
        self.summary_label.setText(
            f"总数：{total}｜当前：{current}/{total}｜待审：{pending}｜通过：{passed}｜截断通过：{trimmed}｜不通过：{failed}"
        )
        for idx, review_item in enumerate(self.items):
            item = self.queue_list.item(idx)
            if item is None:
                continue
            prefix = "[待审]"
            if review_item.status == "pass":
                prefix = "[通过]"
            elif review_item.status == "fail":
                prefix = "[不通过]"
            elif review_item.status == "trim_pass":
                prefix = "[截断通过]"
            item.setText(f"{prefix} {review_item.relative_path}")
            item.setSelected(idx == self.current_index)
            if idx == self.current_index:
                self.queue_list.scrollToItem(item)

    def load_current_video(self) -> None:
        if not (0 <= self.current_index < len(self.items)):
            self.video_title.setText("当前视频：无")
            self.player.stop()
            self.position_slider.setRange(0, 0)
            self.position_slider.setValue(0)
            self.time_label.setText("00:00 / 00:00")
            self.waveform_widget.clear("没有视频")
            self._update_trim_selection_ui(None)
            self._refresh_bottom_status(None)
            return

        item = self.items[self.current_index]
        self.video_title.setText(f"当前视频：{item.relative_path.name}")
        state_text = {
            None: "待审核",
            "pass": "已标记通过",
            "fail": "已标记不通过",
            "trim_pass": "已裁剪并通过",
        }[item.status]
        self.position_slider.setRange(0, 0)
        self.position_slider.setValue(0)
        self.time_label.setText("00:00 / 00:00")
        self.waveform_widget.clear("波形加载中...")
        self.start_waveform_loading(item.source_path)
        self.player.setSource(QUrl.fromLocalFile(str(item.source_path)))
        self.player.play()
        self._update_trim_selection_ui(item)
        self.update_queue_view()
        self._refresh_bottom_status(item, state_text)

    def start_waveform_loading(self, source_path: Path) -> None:
        if self.waveform_thread and self.waveform_thread.isRunning():
            self.waveform_thread.finished_waveform.disconnect(self.on_waveform_ready)
            self.waveform_thread.requestInterruption()
        self.waveform_request_path = str(source_path)
        self.waveform_thread = WaveformThread(source_path)
        self.waveform_thread.finished_waveform.connect(self.on_waveform_ready)
        self.waveform_thread.start()

    @Slot(str, object, str)
    def on_waveform_ready(self, source_path: str, peaks_obj: object, error_text: str) -> None:
        if source_path != self.waveform_request_path:
            return
        if error_text:
            self.waveform_widget.set_status("波形加载失败")
            self.log(f"波形加载失败：{error_text}")
            return
        peaks = peaks_obj if isinstance(peaks_obj, list) else []
        if peaks:
            self.waveform_widget.set_peaks(peaks)
        else:
            self.waveform_widget.set_status("当前视频没有可显示的音频波形")
        if 0 <= self.current_index < len(self.items):
            self._update_trim_selection_ui(self.items[self.current_index])

    def next_unreviewed_or_next(self) -> None:
        next_idx = self.current_index + 1
        if next_idx < len(self.items):
            self.current_index = next_idx
            self.load_current_video()
            return
        self.finish_review(auto_finished=True)

    @Slot()
    def mark_pass(self) -> None:
        if not self._has_current_item():
            return
        item = self.items[self.current_index]
        item.status = "pass"
        self.log(f"通过：{item.relative_path}")
        self.next_unreviewed_or_next()

    @Slot()
    def mark_fail(self) -> None:
        if not self._has_current_item():
            return
        item = self.items[self.current_index]
        item.status = "fail"
        self.log(f"不通过：{item.relative_path}")
        self.next_unreviewed_or_next()

    @Slot()
    def mark_trim_in(self) -> None:
        if not self._has_current_item():
            return
        item = self.items[self.current_index]
        trim_in = max(0, self.player.position())
        item.trim_in_ms = trim_in
        if item.trim_out_ms is not None and item.trim_out_ms <= trim_in:
            item.trim_out_ms = None
        self._update_trim_selection_ui(item)
        self.log(f"设置起点 I：{item.relative_path} -> {format_ms(trim_in)}")

    @Slot()
    def mark_trim_out_and_save(self) -> None:
        if not self._has_current_item():
            return
        item = self.items[self.current_index]
        trim_out = max(0, self.player.position())
        trim_in = item.trim_in_ms or 0
        if trim_out <= trim_in + 80:
            QMessageBox.warning(self, "裁剪失败", "终点必须晚于起点。")
            return

        output_path = self._make_trim_output_path(item)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            trim_video_segment(item.source_path, output_path, trim_in / 1000.0, trim_out / 1000.0)
        except Exception as exc:
            QMessageBox.critical(self, "裁剪失败", str(exc))
            self.log(f"裁剪失败：{item.relative_path}，原因：{exc}")
            return

        item.trim_out_ms = trim_out
        item.clip_output_path = output_path
        item.status = "trim_pass"
        self._update_trim_selection_ui(item)
        self.log(
            f"裁剪并通过：{item.relative_path} -> {output_path.name}，区间 {format_ms(trim_in)} - {format_ms(trim_out)}"
        )
        self.next_unreviewed_or_next()

    def _make_trim_output_path(self, item: VideoItem) -> Path:
        target = self.pass_dir / item.relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            target = self._make_unique_path(target)
        return target

    @Slot()
    def toggle_pause(self) -> None:
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    @Slot()
    def go_previous(self) -> None:
        if not self.items:
            return
        if self.current_index <= 0:
            self.statusBar().showMessage("已经是第一条，无法返回。", 3000)
            return
        self.current_index -= 1
        prev_item = self.items[self.current_index]
        if prev_item.status == "trim_pass" and prev_item.clip_output_path and prev_item.clip_output_path.exists():
            try:
                prev_item.clip_output_path.unlink()
            except Exception:
                pass
        prev_item.status = None
        prev_item.trim_in_ms = None
        prev_item.trim_out_ms = None
        prev_item.clip_output_path = None
        self.log(f"返回上一条重审：{prev_item.relative_path}")
        self.load_current_video()

    @Slot()
    def finish_review(self, auto_finished: bool = False) -> None:
        if self.moved_after_finish:
            self.close()
            return
        self.review_active = False
        self.player.stop()
        moved_count = self.apply_moves()
        self.moved_after_finish = True
        self.update_queue_view()

        pending = sum(1 for x in self.items if x.status is None)
        passed = sum(1 for x in self.items if x.status == "pass")
        trimmed = sum(1 for x in self.items if x.status == "trim_pass")
        failed = sum(1 for x in self.items if x.status == "fail")

        msg = (
            f"审核结束。\n\n"
            f"通过：{passed}\n"
            f"截断通过：{trimmed}\n"
            f"不通过：{failed}\n"
            f"未处理：{pending}\n"
            f"已处理文件：{moved_count}"
        )
        QMessageBox.information(self, "审核完成" if auto_finished else "审核结束", msg)
        self.statusBar().showMessage("审核已结束。", 5000)
        self._refresh_bottom_status(None)

    def apply_moves(self) -> int:
        moved = 0
        for item in self.items:
            if item.status not in {"pass", "fail"}:
                continue
            target_root = self.pass_dir if item.status == "pass" else self.fail_dir
            target_path = target_root / item.relative_path
            target_path.parent.mkdir(parents=True, exist_ok=True)
            if target_path.exists():
                target_path = self._make_unique_path(target_path)
            if not item.source_path.exists():
                self.log(f"跳过（源文件不存在）：{item.relative_path}")
                continue
            try:
                shutil.move(str(item.source_path), str(target_path))
                self.log(f"已移动：{item.relative_path} -> {target_path}")
                moved += 1
                self.cleanup_empty_parents(item.source_path.parent, self.source_dir)
            except Exception as exc:
                self.log(f"处理失败：{item.relative_path}，原因：{exc}")
        return moved

    @staticmethod
    def cleanup_empty_parents(start_dir: Path, stop_dir: Path) -> None:
        current = start_dir
        stop_dir = stop_dir.resolve()
        while current.exists() and current.resolve() != stop_dir:
            try:
                current.rmdir()
            except OSError:
                break
            current = current.parent

    @staticmethod
    def _make_unique_path(path: Path) -> Path:
        stem = path.stem
        suffix = path.suffix
        parent = path.parent
        idx = 1
        candidate = path
        while candidate.exists():
            candidate = parent / f"{stem}_{idx}{suffix}"
            idx += 1
        return candidate

    @Slot()
    def on_media_status_changed(self, status) -> None:
        if status == QMediaPlayer.MediaStatus.EndOfMedia and self._has_current_item():
            self.player.setPosition(0)
            self.player.play()

    @Slot(int)
    def on_position_changed(self, position: int) -> None:
        if not self.is_slider_dragging:
            self.position_slider.setValue(position)
        duration = max(0, self.position_slider.maximum())
        current_value = self.position_slider.value() if self.is_slider_dragging else position
        self.time_label.setText(f"{format_ms(current_value)} / {format_ms(duration)}")
        fraction = 0.0 if duration <= 0 else current_value / duration
        self.waveform_widget.set_playhead_fraction(fraction)

    @Slot(int)
    def on_duration_changed(self, duration: int) -> None:
        self.position_slider.setRange(0, max(0, duration))
        self.time_label.setText(f"{format_ms(self.player.position())} / {format_ms(duration)}")
        if self._has_current_item():
            item = self.items[self.current_index]
            item.duration_ms = duration
            self._update_trim_selection_ui(item)

    @Slot()
    def on_slider_pressed(self) -> None:
        self.is_slider_dragging = True
        self.was_playing_before_drag = self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
        self.player.pause()

    @Slot(int)
    def on_slider_moved(self, value: int) -> None:
        duration = max(0, self.position_slider.maximum())
        self.time_label.setText(f"{format_ms(value)} / {format_ms(duration)}")
        fraction = 0.0 if duration <= 0 else value / duration
        self.waveform_widget.set_playhead_fraction(fraction)
        self.player.setPosition(value)
        if self._has_current_item():
            self._refresh_bottom_status(self.items[self.current_index])

    @Slot()
    def on_slider_released(self) -> None:
        self.player.setPosition(self.position_slider.value())
        self.is_slider_dragging = False
        if self._has_current_item():
            self._refresh_bottom_status(self.items[self.current_index])
        # 保持暂停，以便用户看当前帧；是否继续播放由空格键控制

    @Slot(float)
    def on_waveform_seek_requested(self, fraction: float) -> None:
        duration = self.position_slider.maximum()
        if duration <= 0:
            return
        self.player.pause()
        value = int(max(0.0, min(1.0, fraction)) * duration)
        self.position_slider.setValue(value)
        self.player.setPosition(value)
        if self._has_current_item():
            self._refresh_bottom_status(self.items[self.current_index])

    @Slot(object, str)
    def on_player_error(self, error, error_string) -> None:
        if error_string:
            self.log(f"播放器错误：{error_string}")
            self.statusBar().showMessage(f"播放器错误：{error_string}", 5000)

    def jump_to_item(self, item: QListWidgetItem) -> None:
        row = self.queue_list.row(item)
        if row < 0 or row >= len(self.items):
            return
        self.current_index = row
        self.log(f"跳转到：{self.items[row].relative_path}")
        self.load_current_video()

    def reload_and_reset(self) -> None:
        if self.items and self.review_active and not self.moved_after_finish:
            reply = QMessageBox.question(
                self,
                "重新加载",
                "重新加载会清空当前审核记录，是否继续？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return
        self.log("重新加载目录并重置审核记录。")
        self._load_videos()

    def open_settings_dialog(self) -> None:
        if self.items and self.review_active and not self.moved_after_finish:
            reply = QMessageBox.question(
                self,
                "修改设置",
                "修改目录或快捷键后需要重新加载，当前审核记录会清空。是否继续？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return
        dialog = SettingsDialog(self, self.base_dir, self.source_dir, self.pass_dir, self.fail_dir, self.shortcuts_map, self.logo_path)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        values = dialog.get_values()
        self.source_dir = self._resolve_saved_path(values["source_dir"], self.base_dir / SOURCE_DIR_NAME)
        self.pass_dir = self._resolve_saved_path(values["pass_dir"], self.base_dir / PASS_DIR_NAME)
        self.fail_dir = self._resolve_saved_path(values["fail_dir"], self.base_dir / FAIL_DIR_NAME)
        self.logo_path = self._resolve_saved_path(values["logo_path"], self._default_logo_path() or (self.base_dir / LOGO_FILE_NAME))
        self.shortcuts_map = dict(values["shortcuts"])
        self._apply_logo()
        self._update_logo_preview()
        self._bind_shortcuts()
        self.refresh_settings_display()
        self.save_config()
        self.reload_and_reset()

    def open_base_dir(self) -> None:
        if sys.platform.startswith("win"):
            os.startfile(str(self.base_dir))
        elif sys.platform == "darwin":
            subprocess.run(["open", str(self.base_dir)], check=False)
        else:
            subprocess.run(["xdg-open", str(self.base_dir)], check=False)

    def _update_trim_selection_ui(self, item: Optional[VideoItem]) -> None:
        if item is None:
            self.waveform_widget.set_selection(None, None)
            return
        duration = item.duration_ms or self.position_slider.maximum()
        in_fraction = None if item.trim_in_ms is None or duration <= 0 else item.trim_in_ms / duration
        out_fraction = None if item.trim_out_ms is None or duration <= 0 else item.trim_out_ms / duration
        self.waveform_widget.set_selection(in_fraction, out_fraction)

    def _refresh_bottom_status(self, item: Optional[VideoItem], state_text: Optional[str] = None) -> None:
        if item is None:
            self.statusBar().showMessage(getattr(self, "shortcut_summary_text", ""), 8000)
            return
        start_text = format_ms(item.trim_in_ms) if item.trim_in_ms is not None else "起点未设置"
        end_text = format_ms(item.trim_out_ms) if item.trim_out_ms is not None else "终点未设置"
        state_text = state_text or {None: "待审核", "pass": "已标记通过", "fail": "已标记不通过", "trim_pass": "已裁剪并通过"}[item.status]
        self.statusBar().showMessage(f"状态：{state_text} ｜ 裁剪区间：{start_text} ～ {end_text} ｜ {getattr(self, 'shortcut_summary_text', '')}")

    def toggle_waveform_visible(self) -> None:
        self.waveform_visible = not self.waveform_visible
        self._update_waveform_visibility_ui()
        self.refresh_settings_display()
        self.save_config()

    def _update_waveform_visibility_ui(self) -> None:
        visible = getattr(self, "waveform_visible", True)
        self.waveform_widget.setVisible(visible)
        if hasattr(self, "waveform_wrap"):
            self.waveform_wrap.setMinimumHeight(64 if visible else 28)
        self.timeline_region.setMinimumHeight(MIN_TIMELINE_HEIGHT if visible else MIN_TIMELINE_HEIGHT_COLLAPSED)
        self.btn_toggle_waveform.setIcon(build_eye_icon(visible))
        self.btn_toggle_waveform.setIconSize(QSize(16, 16))
        self.btn_toggle_waveform.setToolTip("隐藏波形" if visible else "显示波形")

    def log(self, message: str) -> None:
        self.log_text.append(message)

    def _has_current_item(self) -> bool:
        return self.review_active and 0 <= self.current_index < len(self.items)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self.save_config()

    def moveEvent(self, event) -> None:  # noqa: N802
        super().moveEvent(event)
        self.save_config()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        self.save_config()
        if self.items and not self.moved_after_finish and self.review_active:
            reply = QMessageBox.question(
                self,
                "退出",
                "当前仍有审核记录未执行处理。是否立即结束审核并按记录处理文件？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Yes,
            )
            if reply == QMessageBox.StandardButton.Yes:
                self.finish_review(auto_finished=False)
                event.accept()
                return
            if reply == QMessageBox.StandardButton.No:
                event.accept()
                return
            event.ignore()
            return
        event.accept()


def get_ffmpeg_exe() -> str:
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


def extract_waveform_peaks(video_path: Path, peak_count: int = 520) -> list[float]:
    ffmpeg_exe = get_ffmpeg_exe()
    cmd = [
        ffmpeg_exe,
        "-i",
        str(video_path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "8000",
        "-f",
        "f32le",
        "-",
    ]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if proc.returncode != 0 or not proc.stdout:
        return []
    import array
    samples = array.array("f")
    samples.frombytes(proc.stdout)
    if not samples:
        return []
    total = len(samples)
    bucket_size = max(1, total // peak_count)
    peaks: list[float] = []
    for start in range(0, total, bucket_size):
        chunk = samples[start:start + bucket_size]
        if not chunk:
            continue
        peak = max(abs(v) for v in chunk)
        peaks.append(float(min(1.0, peak)))
    if len(peaks) > peak_count:
        step = len(peaks) / peak_count
        peaks = [peaks[min(len(peaks) - 1, int(i * step))] for i in range(peak_count)]
    return peaks


def trim_video_segment(source_path: Path, output_path: Path, start_sec: float, end_sec: float) -> None:
    duration = max(0.0, end_sec - start_sec)
    if duration <= 0.05:
        raise RuntimeError("裁剪区间过短，未生成文件。")
    ffmpeg_exe = get_ffmpeg_exe()

    copy_cmd = [
        ffmpeg_exe,
        "-y",
        "-ss",
        f"{start_sec:.3f}",
        "-i",
        str(source_path),
        "-t",
        f"{duration:.3f}",
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        str(output_path),
    ]
    result = subprocess.run(copy_cmd, capture_output=True, text=True)
    if result.returncode == 0 and output_path.exists() and output_path.stat().st_size > 0:
        return

    reencode_cmd = [
        ffmpeg_exe,
        "-y",
        "-ss",
        f"{start_sec:.3f}",
        "-i",
        str(source_path),
        "-t",
        f"{duration:.3f}",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "18",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-movflags",
        "+faststart",
        str(output_path),
    ]
    result2 = subprocess.run(reencode_cmd, capture_output=True, text=True)
    if result2.returncode != 0 or not output_path.exists() or output_path.stat().st_size == 0:
        stderr_text = (result2.stderr or result.stderr or "").strip()
        raise RuntimeError(stderr_text or "ffmpeg 裁剪失败")


def format_ms(ms: Optional[int]) -> str:
    if ms is None:
        return "--:--"
    total_seconds = max(0, int(ms // 1000))
    minutes = total_seconds // 60
    seconds = total_seconds % 60
    return f"{minutes:02d}:{seconds:02d}"


def main() -> None:
    app = QApplication(sys.argv)
    window = ReviewWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
