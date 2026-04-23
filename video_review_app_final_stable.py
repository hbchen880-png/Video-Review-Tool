import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from array import array
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QPoint, QRect, QSize, QThread, Qt, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QAction, QIcon, QKeySequence, QPainter, QPen, QPixmap, QShortcut
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QCheckBox,
    QComboBox,
    QKeySequenceEdit,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QLayout,
    QLayoutItem,
    QFrame,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
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
PRODUCT_LIBRARY_DIR_NAME = "产品库"
PERSON_LIBRARY_DIR_NAME = "人物库"
COPY_LIBRARY_DIR_NAME = "文案库"
FINISHED_REPOSITORY_DIR_NAME = "成品仓库"
CONFIG_FILE_NAME = "review_config.json"
LOGO_FILE_NAME = "app_logo.png"
REFERENCE_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".webp"}
COPY_LIBRARY_EXTENSIONS = {".txt", ".md"}

DEFAULT_SHORTCUTS = {
    "pass": "V",
    "fail": "N",
    "previous": "Y",
    "end": "E",
    "pause": "Space",
    "trim_in": "I",
    "trim_out": "O",
    "cycle_reference": "Z",
}


PERSONALITY_PROFILE_IDS = ["profile_1", "profile_2", "profile_3"]


def default_personality_profile_name(profile_id: str) -> str:
    try:
        index = PERSONALITY_PROFILE_IDS.index(profile_id) + 1
    except ValueError:
        index = 1
    return f"个性设置{index}"


def get_ffmpeg_exe() -> str:
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


def get_hidden_subprocess_kwargs() -> dict:
    kwargs: dict = {}
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        if creationflags:
            kwargs["creationflags"] = creationflags
        startupinfo_cls = getattr(subprocess, "STARTUPINFO", None)
        if startupinfo_cls is not None:
            startupinfo = startupinfo_cls()
            startupinfo.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 0)
            startupinfo.wShowWindow = 0
            kwargs["startupinfo"] = startupinfo
    return kwargs


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
        self.setMinimumHeight(92)
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
        rect = self.rect().adjusted(1, 1, -1, -1)
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

        inner = rect.adjusted(6, 6, -6, -6)
        if inner.width() <= 0 or inner.height() <= 0:
            return

        if self.in_fraction is not None and self.out_fraction is not None and self.out_fraction > self.in_fraction:
            start_x = inner.left() + int(self.in_fraction * inner.width())
            end_x = inner.left() + int(self.out_fraction * inner.width())
            painter.fillRect(start_x, inner.top(), max(1, end_x - start_x), inner.height(), Qt.GlobalColor.cyan)

        if self.peaks is None:
            painter.setPen(QPen(Qt.GlobalColor.darkGray, 1))
            painter.drawText(inner, Qt.AlignmentFlag.AlignCenter, self.status_text)
            return

        if not self.peaks:
            painter.setPen(QPen(Qt.GlobalColor.darkGray, 1))
            painter.drawText(inner, Qt.AlignmentFlag.AlignCenter, self.status_text or "当前视频没有音频波形")
            return

        count = len(self.peaks)
        width = max(1, inner.width())
        height = max(1, inner.height())
        center_y = inner.top() + height / 2
        played_index = int(self.playhead_fraction * max(1, count - 1))
        pen_width = max(1, int(width / max(1, count) + 0.4))
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


class FileWarmupThread(QThread):
    finished_warmup = Signal(str)

    def __init__(self, video_path: Path, head_bytes: int = 524288, tail_bytes: int = 262144) -> None:
        super().__init__()
        self.video_path = video_path
        self.head_bytes = max(65536, head_bytes)
        self.tail_bytes = max(65536, tail_bytes)

    def run(self) -> None:
        try:
            with open(self.video_path, "rb") as fh:
                fh.read(self.head_bytes)
                try:
                    file_size = fh.seek(0, os.SEEK_END)
                except Exception:
                    file_size = 0
                if file_size > self.tail_bytes:
                    fh.seek(max(0, file_size - self.tail_bytes), os.SEEK_SET)
                    fh.read(self.tail_bytes)
        except Exception:
            pass
        self.finished_warmup.emit(str(self.video_path))


class SettingsDialog(QDialog):
    def __init__(
        self,
        parent: QWidget,
        base_dir: Path,
        personality_profiles: dict[str, dict],
        active_profile_id: str,
        logo_path: Optional[Path],
    ) -> None:
        super().__init__(parent)
        self.base_dir = base_dir
        self.setWindowTitle("设置")
        self.resize(920, 560)
        self.personality_profiles = self._normalize_profile_store(personality_profiles)
        self.active_profile_id = active_profile_id if active_profile_id in PERSONALITY_PROFILE_IDS else PERSONALITY_PROFILE_IDS[0]
        self.profile_switch_loading = False

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        self.profile_buttons: dict[str, QPushButton] = {}

        profile_row = QHBoxLayout()
        profile_row.setContentsMargins(0, 0, 0, 0)
        profile_row.setSpacing(10)
        profile_title = QLabel("个性设置")
        profile_title.setStyleSheet("font-weight:700;")
        profile_buttons_widget = QWidget()
        profile_buttons_layout = QHBoxLayout(profile_buttons_widget)
        profile_buttons_layout.setContentsMargins(0, 0, 0, 0)
        profile_buttons_layout.setSpacing(8)
        for profile_id in PERSONALITY_PROFILE_IDS:
            button = QPushButton(default_personality_profile_name(profile_id))
            button.setCheckable(True)
            button.setMinimumHeight(34)
            button.setMinimumWidth(150)
            button.clicked.connect(lambda _checked=False, pid=profile_id: self.on_profile_button_clicked(pid))
            self.profile_buttons[profile_id] = button
            profile_buttons_layout.addWidget(button)
        profile_buttons_layout.addStretch(1)
        profile_row.addWidget(profile_title)
        profile_row.addWidget(profile_buttons_widget, 1)
        root.addLayout(profile_row)

        profile_name_row = QHBoxLayout()
        profile_name_row.setContentsMargins(0, 0, 0, 0)
        profile_name_row.setSpacing(8)
        profile_name_label = QLabel("当前方案名称")
        self.profile_name_edit = QLineEdit()
        self.profile_name_edit.setPlaceholderText("给当前个性设置命名")
        self.profile_name_edit.setMinimumWidth(260)
        profile_name_row.addWidget(profile_name_label)
        profile_name_row.addWidget(self.profile_name_edit, 1)
        root.addLayout(profile_name_row)

        profile_hint = QLabel("共 3 组。点击上方按钮即可切换方案；每组都可分别保存目录、快捷键和默认播放速度。")
        profile_hint.setStyleSheet("color:#666;")
        profile_hint.setWordWrap(True)
        root.addWidget(profile_hint)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(12)

        self.source_edit, source_row = self._build_path_row("")
        self.pass_edit, pass_row = self._build_path_row("")
        self.fail_edit, fail_row = self._build_path_row("")
        self.product_library_edit, product_library_row = self._build_path_row("")
        self.logo_edit, logo_row = self._build_file_row(str(logo_path) if logo_path else "")

        form.addRow("源文件目录", source_row)
        form.addRow("通过目录", pass_row)
        form.addRow("不通过目录", fail_row)
        form.addRow("产品库目录", product_library_row)
        self.rename_to_finished_repo_check = QCheckBox("启用")
        self.rename_to_finished_repo_check.setToolTip("开启后，审核通过的视频会按根目录下“文案库”的文案重命名，并放入根目录下“成品仓库”的对应文件夹。")
        form.addRow("是否重命名后放入成品仓库", self.rename_to_finished_repo_check)
        self.default_playback_speed_spin = QDoubleSpinBox()
        self.default_playback_speed_spin.setRange(0.25, 3.0)
        self.default_playback_speed_spin.setSingleStep(0.05)
        self.default_playback_speed_spin.setDecimals(2)
        self.default_playback_speed_spin.setSuffix(" x")
        form.addRow("默认播放速度", self.default_playback_speed_spin)
        form.addRow("Logo 图片", logo_row)

        shortcut_widget = QWidget()
        shortcut_layout = QGridLayout(shortcut_widget)
        shortcut_layout.setContentsMargins(0, 0, 0, 0)
        shortcut_layout.setHorizontalSpacing(18)
        shortcut_layout.setVerticalSpacing(10)

        self.pass_key_edit = QKeySequenceEdit()
        self.fail_key_edit = QKeySequenceEdit()
        self.previous_key_edit = QKeySequenceEdit()
        self.end_key_edit = QKeySequenceEdit()
        self.pause_key_edit = QKeySequenceEdit()
        self.trim_in_key_edit = QKeySequenceEdit()
        self.trim_out_key_edit = QKeySequenceEdit()
        self.cycle_reference_key_edit = QKeySequenceEdit()

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
        shortcut_layout.addWidget(QLabel("切换参考图"), 3, 2)
        shortcut_layout.addWidget(self.cycle_reference_key_edit, 3, 3)

        form.addRow("快捷键", shortcut_widget)
        root.addLayout(form)

        tip = QLabel("个性设置会保存：源文件目录、通过目录、不通过目录、产品库目录、是否重命名后放入成品仓库、全部快捷键、默认播放速度。首页也可以快速切换个性设置。")
        tip.setWordWrap(True)
        tip.setStyleSheet("color: #666;")
        root.addWidget(tip)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        self.reset_button = QPushButton("恢复默认")
        self.cancel_button = QPushButton("取消")
        self.save_button = QPushButton("保存")
        self.reset_button.clicked.connect(self.reset_defaults)
        self.cancel_button.clicked.connect(self.reject)
        self.save_button.clicked.connect(self.validate_and_accept)
        btn_row.addWidget(self.reset_button)
        btn_row.addWidget(self.cancel_button)
        btn_row.addWidget(self.save_button)
        root.addLayout(btn_row)

        self.profile_name_edit.textChanged.connect(self.on_profile_name_changed)
        self.refresh_profile_buttons()
        self.set_active_profile(self.active_profile_id)

    @staticmethod
    def default_profile_settings(base_dir: Path) -> dict:
        return {
            "source_dir": str(base_dir / SOURCE_DIR_NAME),
            "pass_dir": str(base_dir / PASS_DIR_NAME),
            "fail_dir": str(base_dir / FAIL_DIR_NAME),
            "product_library_dir": str(base_dir / PRODUCT_LIBRARY_DIR_NAME),
            "rename_to_finished_repo": False,
            "playback_speed": 1.0,
            "shortcuts": DEFAULT_SHORTCUTS.copy(),
        }

    def _normalize_profile_store(self, profile_store: dict[str, dict]) -> dict[str, dict]:
        result: dict[str, dict] = {}
        default_settings = self.default_profile_settings(self.base_dir)
        profile_store = profile_store or {}
        for profile_id in PERSONALITY_PROFILE_IDS:
            raw_entry = profile_store.get(profile_id) if isinstance(profile_store, dict) else None
            name = default_personality_profile_name(profile_id)
            raw_settings = None
            if isinstance(raw_entry, dict):
                raw_name = str(raw_entry.get("name", "")).strip()
                if raw_name:
                    name = raw_name
                raw_settings = raw_entry.get("settings") if isinstance(raw_entry.get("settings"), dict) else raw_entry
            result[profile_id] = {
                "name": name,
                "settings": self._normalize_settings(raw_settings, default_settings),
            }
        return result

    @staticmethod
    def _normalize_settings(raw_settings: Optional[dict], fallback: dict) -> dict:
        fallback = {
            **fallback,
            "shortcuts": dict(fallback.get("shortcuts") or DEFAULT_SHORTCUTS),
        }
        raw_settings = raw_settings or {}
        try:
            playback_speed = max(0.25, min(3.0, float(raw_settings.get("playback_speed", fallback["playback_speed"]))))
        except (TypeError, ValueError):
            playback_speed = float(fallback["playback_speed"])
        shortcuts = dict(fallback.get("shortcuts") or DEFAULT_SHORTCUTS)
        raw_shortcuts = raw_settings.get("shortcuts") if isinstance(raw_settings.get("shortcuts"), dict) else {}
        for key, default_value in DEFAULT_SHORTCUTS.items():
            value = raw_shortcuts.get(key)
            shortcuts[key] = str(value).strip() if value else shortcuts.get(key, default_value)
            if not shortcuts[key]:
                shortcuts[key] = default_value
        return {
            "source_dir": str(raw_settings.get("source_dir") or fallback["source_dir"]),
            "pass_dir": str(raw_settings.get("pass_dir") or fallback["pass_dir"]),
            "fail_dir": str(raw_settings.get("fail_dir") or fallback["fail_dir"]),
            "product_library_dir": str(raw_settings.get("product_library_dir") or fallback["product_library_dir"]),
            "rename_to_finished_repo": bool(raw_settings.get("rename_to_finished_repo", fallback["rename_to_finished_repo"])),
            "playback_speed": playback_speed,
            "shortcuts": shortcuts,
        }

    @staticmethod
    def profile_button_style(selected: bool) -> str:
        if selected:
            return (
                "QPushButton {background:#eaf3ff; border:1px solid #4a90e2; border-radius:8px;"
                "padding:6px 14px; font-weight:700; color:#1f4f8f;}"
            )
        return (
            "QPushButton {background:#f7f7f7; border:1px solid #d9d9d9; border-radius:8px;"
            "padding:6px 14px; color:#444;}"
            "QPushButton:hover {border-color:#4a90e2;}"
        )

    def refresh_profile_buttons(self) -> None:
        current_id = self.current_profile_id()
        for profile_id in PERSONALITY_PROFILE_IDS:
            entry = self.personality_profiles.get(profile_id) or {}
            name = str(entry.get("name") or default_personality_profile_name(profile_id))
            button = self.profile_buttons.get(profile_id)
            if button is None:
                continue
            button.blockSignals(True)
            button.setText(name)
            button.setChecked(profile_id == current_id)
            button.setStyleSheet(self.profile_button_style(profile_id == current_id))
            button.blockSignals(False)

    def current_profile_id(self) -> str:
        return self.active_profile_id if self.active_profile_id in PERSONALITY_PROFILE_IDS else PERSONALITY_PROFILE_IDS[0]

    def set_active_profile(self, profile_id: str) -> None:
        if profile_id not in PERSONALITY_PROFILE_IDS:
            profile_id = PERSONALITY_PROFILE_IDS[0]
        self.profile_switch_loading = True
        self.active_profile_id = profile_id
        self.refresh_profile_buttons()
        self.load_profile_into_form(profile_id)
        self.profile_switch_loading = False

    def load_profile_into_form(self, profile_id: str) -> None:
        entry = self.personality_profiles.get(profile_id) or {}
        settings = entry.get("settings") or self.default_profile_settings(self.base_dir)
        self.profile_name_edit.blockSignals(True)
        self.profile_name_edit.setText(str(entry.get("name") or default_personality_profile_name(profile_id)))
        self.profile_name_edit.blockSignals(False)
        self.source_edit.setText(settings["source_dir"])
        self.pass_edit.setText(settings["pass_dir"])
        self.fail_edit.setText(settings["fail_dir"])
        self.product_library_edit.setText(settings["product_library_dir"])
        self.rename_to_finished_repo_check.setChecked(bool(settings["rename_to_finished_repo"]))
        self.default_playback_speed_spin.setValue(float(settings["playback_speed"]))
        shortcuts = settings.get("shortcuts") or DEFAULT_SHORTCUTS
        self.pass_key_edit.setKeySequence(QKeySequence(shortcuts.get("pass", DEFAULT_SHORTCUTS["pass"])))
        self.fail_key_edit.setKeySequence(QKeySequence(shortcuts.get("fail", DEFAULT_SHORTCUTS["fail"])))
        self.previous_key_edit.setKeySequence(QKeySequence(shortcuts.get("previous", DEFAULT_SHORTCUTS["previous"])))
        self.end_key_edit.setKeySequence(QKeySequence(shortcuts.get("end", DEFAULT_SHORTCUTS["end"])))
        self.pause_key_edit.setKeySequence(QKeySequence(shortcuts.get("pause", DEFAULT_SHORTCUTS["pause"])))
        self.trim_in_key_edit.setKeySequence(QKeySequence(shortcuts.get("trim_in", DEFAULT_SHORTCUTS["trim_in"])))
        self.trim_out_key_edit.setKeySequence(QKeySequence(shortcuts.get("trim_out", DEFAULT_SHORTCUTS["trim_out"])))
        self.cycle_reference_key_edit.setKeySequence(QKeySequence(shortcuts.get("cycle_reference", DEFAULT_SHORTCUTS["cycle_reference"])))

    def sync_form_to_profile(self, profile_id: Optional[str] = None) -> None:
        profile_id = profile_id or self.current_profile_id()
        if profile_id not in PERSONALITY_PROFILE_IDS:
            return
        entry = self.personality_profiles.setdefault(profile_id, {
            "name": default_personality_profile_name(profile_id),
            "settings": self.default_profile_settings(self.base_dir),
        })
        name = self.profile_name_edit.text().strip() or default_personality_profile_name(profile_id)
        entry["name"] = name
        entry["settings"] = self._normalize_settings({
            "source_dir": self.source_edit.text().strip(),
            "pass_dir": self.pass_edit.text().strip(),
            "fail_dir": self.fail_edit.text().strip(),
            "product_library_dir": self.product_library_edit.text().strip(),
            "rename_to_finished_repo": self.rename_to_finished_repo_check.isChecked(),
            "playback_speed": self.default_playback_speed_spin.value(),
            "shortcuts": {
                "pass": self.pass_key_edit.keySequence().toString(QKeySequence.SequenceFormat.PortableText),
                "fail": self.fail_key_edit.keySequence().toString(QKeySequence.SequenceFormat.PortableText),
                "previous": self.previous_key_edit.keySequence().toString(QKeySequence.SequenceFormat.PortableText),
                "end": self.end_key_edit.keySequence().toString(QKeySequence.SequenceFormat.PortableText),
                "pause": self.pause_key_edit.keySequence().toString(QKeySequence.SequenceFormat.PortableText),
                "trim_in": self.trim_in_key_edit.keySequence().toString(QKeySequence.SequenceFormat.PortableText),
                "trim_out": self.trim_out_key_edit.keySequence().toString(QKeySequence.SequenceFormat.PortableText),
                "cycle_reference": self.cycle_reference_key_edit.keySequence().toString(QKeySequence.SequenceFormat.PortableText),
            },
        }, self.default_profile_settings(self.base_dir))

    def on_profile_button_clicked(self, profile_id: str) -> None:
        if self.profile_switch_loading:
            return
        if profile_id not in PERSONALITY_PROFILE_IDS:
            return
        previous_profile_id = self.active_profile_id
        if previous_profile_id in PERSONALITY_PROFILE_IDS:
            self.sync_form_to_profile(previous_profile_id)
        self.active_profile_id = profile_id
        self.profile_switch_loading = True
        self.refresh_profile_buttons()
        self.load_profile_into_form(profile_id)
        self.profile_switch_loading = False

    def on_profile_name_changed(self, text: str) -> None:
        profile_id = self.current_profile_id()
        if profile_id not in PERSONALITY_PROFILE_IDS:
            return
        entry = self.personality_profiles.setdefault(profile_id, {
            "name": default_personality_profile_name(profile_id),
            "settings": self.default_profile_settings(self.base_dir),
        })
        entry["name"] = text.strip() or default_personality_profile_name(profile_id)
        self.refresh_profile_buttons()

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
        current_name = self.profile_name_edit.text().strip() or default_personality_profile_name(self.current_profile_id())
        default_settings = self.default_profile_settings(self.base_dir)
        self.source_edit.setText(default_settings["source_dir"])
        self.pass_edit.setText(default_settings["pass_dir"])
        self.fail_edit.setText(default_settings["fail_dir"])
        self.product_library_edit.setText(default_settings["product_library_dir"])
        self.rename_to_finished_repo_check.setChecked(False)
        self.default_playback_speed_spin.setValue(float(default_settings["playback_speed"]))
        self.pass_key_edit.setKeySequence(QKeySequence(DEFAULT_SHORTCUTS["pass"]))
        self.fail_key_edit.setKeySequence(QKeySequence(DEFAULT_SHORTCUTS["fail"]))
        self.previous_key_edit.setKeySequence(QKeySequence(DEFAULT_SHORTCUTS["previous"]))
        self.end_key_edit.setKeySequence(QKeySequence(DEFAULT_SHORTCUTS["end"]))
        self.pause_key_edit.setKeySequence(QKeySequence(DEFAULT_SHORTCUTS["pause"]))
        self.trim_in_key_edit.setKeySequence(QKeySequence(DEFAULT_SHORTCUTS["trim_in"]))
        self.trim_out_key_edit.setKeySequence(QKeySequence(DEFAULT_SHORTCUTS["trim_out"]))
        self.cycle_reference_key_edit.setKeySequence(QKeySequence(DEFAULT_SHORTCUTS["cycle_reference"]))
        self.profile_name_edit.setText(current_name)

    def get_values(self) -> dict:
        self.sync_form_to_profile(self.current_profile_id())
        return {
            "active_profile_id": self.current_profile_id(),
            "personality_profiles": self.personality_profiles,
            "logo_path": self.logo_edit.text().strip(),
            "current_profile_settings": self.personality_profiles[self.current_profile_id()]["settings"],
        }

    def validate_and_accept(self) -> None:
        values = self.get_values()
        current_settings = values["current_profile_settings"]
        if any(not current_settings[key] for key in ("source_dir", "pass_dir", "fail_dir", "product_library_dir")):
            QMessageBox.warning(self, "设置无效", "源文件目录、通过目录、不通过目录、产品库目录都不能为空。")
            return

        shortcut_values = current_settings["shortcuts"]
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
            "cycle_reference": "切换参考图",
        }
        for key, value in shortcut_values.items():
            normalized = value.upper()
            if normalized in seen:
                QMessageBox.warning(
                    self,
                    "快捷键冲突",
                    f"“{labels[key]}” 与 “{labels[seen[normalized]]}” 使用了相同快捷键：{value}",
                )
                return
            seen[normalized] = key

        self.accept()



class FlowLayout(QLayout):
    def __init__(self, parent: Optional[QWidget] = None, margin: int = 0, hspacing: int = 8, vspacing: int = 8) -> None:
        super().__init__(parent)
        self._items: list[QLayoutItem] = []
        self._hspacing = hspacing
        self._vspacing = vspacing
        self.setContentsMargins(margin, margin, margin, margin)

    def addItem(self, item: QLayoutItem) -> None:
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int) -> Optional[QLayoutItem]:
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def takeAt(self, index: int) -> Optional[QLayoutItem]:
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def expandingDirections(self) -> Qt.Orientations:
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._do_layout(QRect(0, 0, width, 0), True)

    def setGeometry(self, rect: QRect) -> None:
        super().setGeometry(rect)
        self._do_layout(rect, False)

    def sizeHint(self) -> QSize:
        return self.minimumSize()

    def minimumSize(self) -> QSize:
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        size += QSize(margins.left() + margins.right(), margins.top() + margins.bottom())
        return size

    def _do_layout(self, rect: QRect, test_only: bool) -> int:
        margins = self.contentsMargins()
        effective_rect = rect.adjusted(+margins.left(), +margins.top(), -margins.right(), -margins.bottom())
        x = effective_rect.x()
        y = effective_rect.y()
        line_height = 0

        for item in self._items:
            widget = item.widget()
            space_x = self._hspacing
            space_y = self._vspacing
            item_size = item.sizeHint()
            next_x = x + item_size.width() + space_x
            if next_x - space_x > effective_rect.right() and line_height > 0:
                x = effective_rect.x()
                y = y + line_height + space_y
                next_x = x + item_size.width() + space_x
                line_height = 0

            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), item_size))

            x = next_x
            line_height = max(line_height, item_size.height())

        return y + line_height - rect.y() + margins.bottom()


class ReviewWindow(QMainWindow):
    def __init__(self, startup_review_groups: Optional[set[str]] = None, startup_request_label: str = "") -> None:
        super().__init__()
        self.base_dir = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
        self.resource_dir = Path(getattr(sys, "_MEIPASS", self.base_dir))
        self.config_path = self.base_dir / CONFIG_FILE_NAME

        self.source_dir = self.base_dir / SOURCE_DIR_NAME
        self.pass_dir = self.base_dir / PASS_DIR_NAME
        self.fail_dir = self.base_dir / FAIL_DIR_NAME
        self.product_library_dir = self.base_dir / PRODUCT_LIBRARY_DIR_NAME
        self.person_library_dir = self.base_dir / PERSON_LIBRARY_DIR_NAME
        self.rename_to_finished_repo = False
        self.playback_speed = 1.0
        self.available_review_groups: list[str] = []
        self.selected_review_groups: set[str] = set()
        self.applied_review_groups: set[str] = set()
        self.group_selection_has_saved_state = False
        self.group_checkbox_map: dict[str, QCheckBox] = {}
        self.group_filter_updating = False
        self.logo_path: Optional[Path] = None
        self.shortcuts_map = DEFAULT_SHORTCUTS.copy()
        self.personality_profiles: dict[str, dict] = {}
        self.active_personality_profile = PERSONALITY_PROFILE_IDS[0]
        self.personality_profile_widget_updating = False
        self.reference_selection_map: dict[str, str] = {}
        self.reference_library_images: list[Path] = []
        self.reference_library_records: list[tuple[Path, str, str, str, str]] = []
        self.person_reference_library_images: list[Path] = []
        self.person_reference_library_records: list[tuple[Path, str, str, str, str]] = []
        self.reference_match_cache: dict[tuple[str, ...], tuple[str, list[Path]]] = {}
        self.person_reference_match_cache: dict[tuple[str, ...], tuple[str, list[Path]]] = {}
        self.reference_pixmap_cache: dict[str, QPixmap] = {}
        self.reference_library_error_text = ""
        self.person_reference_library_error_text = ""
        self.current_reference_product_key: Optional[str] = None
        self.current_reference_display_name = ""
        self.current_reference_images: list[Path] = []
        self.current_reference_index = -1
        self.reference_image_original_pixmap: Optional[QPixmap] = None
        self.person_reference_match_cache: dict[tuple[str, ...], tuple[str, list[Path]]] = {}
        self.current_person_reference_name = ""
        self.current_person_reference_images: list[Path] = []
        self.person_reference_image_original_pixmap: Optional[QPixmap] = None

        self.items: list[VideoItem] = []
        self.current_index = -1
        self.review_active = False
        self.moved_after_finish = False

        self.current_trim_in_ms: Optional[int] = None
        self.current_trim_out_ms: Optional[int] = None
        self.user_dragging_slider = False
        self.waveform_thread: Optional[WaveformThread] = None
        self.waveform_threads: dict[str, WaveformThread] = {}
        self.waveform_cache: dict[str, list[float]] = {}
        self.waveform_request_path: Optional[str] = None
        self.file_warm_threads: dict[str, FileWarmupThread] = {}
        self.warmed_video_paths: set[str] = set()
        self.pending_player_request_id = 0
        self.last_queue_index = -1

        self.shortcut_objects: dict[str, QShortcut] = {}
        self.next_video_prefetch_timer = QTimer(self)
        self.next_video_prefetch_timer.setSingleShot(True)
        self.next_video_prefetch_timer.timeout.connect(self.prefetch_next_video_assets)
        self.waveform_delay_timer = QTimer(self)
        self.waveform_delay_timer.setSingleShot(True)
        self.waveform_delay_timer.timeout.connect(self.load_delayed_waveform_for_current)
        self.group_filter_apply_timer = QTimer(self)
        self.group_filter_apply_timer.setSingleShot(True)
        self.group_filter_apply_timer.setInterval(350)
        self.group_filter_apply_timer.timeout.connect(self.apply_pending_group_filter_change)

        self.startup_review_groups_pending = set(startup_review_groups or [])
        self.startup_review_groups_original = set(startup_review_groups or [])
        self.startup_request_label = startup_request_label
        self.startup_review_groups_applied = False

        self._load_config()

        self.setWindowTitle("视频审核工具")
        self.setMinimumSize(820, 560)
        self._resize_for_screen()
        self._apply_logo()
        self._build_ui()
        self.adjust_reference_preview_sizes()
        self._build_player()
        self._bind_shortcuts()
        self.refresh_settings_display()
        self._load_videos()
        if self.startup_request_label and startup_review_groups:
            self.statusBar().showMessage(f"已通过启动接口预选审核分组：{len(startup_review_groups)}组", 5000)

    def _resize_for_screen(self) -> None:
        screen = QApplication.primaryScreen()
        if screen is None:
            self.resize(1280, 820)
            return
        available = screen.availableGeometry()
        width = max(980, min(available.width() - 60, 1400))
        height = max(620, min(available.height() - 80, 900))
        self.resize(width, height)

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)

        page = QVBoxLayout(central)
        page.setContentsMargins(10, 10, 10, 10)
        page.setSpacing(10)

        top_bar = QHBoxLayout()
        self.btn_settings = QPushButton("设置")
        self.btn_settings.setFixedHeight(36)
        self.btn_settings.clicked.connect(self.open_settings_dialog)
        self.btn_open_dir = QPushButton("打开程序目录")
        self.btn_open_dir.setFixedHeight(36)
        self.btn_open_dir.clicked.connect(self.open_base_dir)

        self.logo_preview = QLabel()
        self.logo_preview.setFixedSize(56, 56)
        self.logo_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.logo_preview.setStyleSheet("border:1px solid #ddd;border-radius:10px;background:#fafafa;")

        title_box = QVBoxLayout()
        title_box.setContentsMargins(0, 0, 0, 0)
        title_box.setSpacing(2)
        title = QLabel("视频审核工具")
        title.setStyleSheet("font-size: 22px; font-weight: 800;")
        subtitle = QLabel("支持目录设置、快捷键审核、拖动时间条看帧、I/O 裁剪通过")
        subtitle.setStyleSheet("color:#666;")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)

        top_bar.addWidget(self.btn_settings)
        top_bar.addWidget(self.btn_open_dir)
        top_bar.addWidget(self.logo_preview)
        top_bar.addLayout(title_box, 1)

        self._update_logo_preview()
        page.addLayout(top_bar)

        root_splitter = QSplitter(Qt.Orientation.Horizontal)
        root_splitter.setChildrenCollapsible(False)
        root_splitter.setHandleWidth(8)

        left_panel = QVBoxLayout()
        left_title = QLabel("待审核队列")
        left_title.setStyleSheet("font-size:18px; font-weight:700;")
        self.queue_list = QListWidget()
        self.queue_list.setAlternatingRowColors(True)
        self.queue_list.itemDoubleClicked.connect(self.jump_to_item)
        self.summary_label = QLabel("未开始")
        self.summary_label.setWordWrap(True)
        self.group_filter_title_label = QLabel("请选择审核分组：")
        self.group_filter_title_label.setStyleSheet("font-weight:600;")
        self.group_select_all_button = QPushButton("全选")
        self.group_select_all_button.setCheckable(True)
        self.group_select_all_button.setChecked(True)
        self.group_select_all_button.toggled.connect(self.on_group_select_all_toggled)
        group_filter_header = QHBoxLayout()
        group_filter_header.setContentsMargins(0, 0, 0, 0)
        group_filter_header.setSpacing(8)
        group_filter_header.addWidget(self.group_filter_title_label)
        group_filter_header.addWidget(self.group_select_all_button)
        group_filter_header.addStretch(1)
        self.group_filter_summary_label = QLabel("已选：全部")
        self.group_filter_summary_label.setWordWrap(True)
        self.group_filter_summary_label.setStyleSheet("color:#666;")
        self.group_filter_widget = QWidget()
        self.group_filter_flow_layout = FlowLayout(self.group_filter_widget, margin=0, hspacing=8, vspacing=6)
        self.group_filter_widget.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        left_panel.addWidget(left_title)
        left_panel.addWidget(self.summary_label)
        left_panel.addLayout(group_filter_header)
        left_panel.addWidget(self.group_filter_widget)
        left_panel.addWidget(self.group_filter_summary_label)
        left_panel.addWidget(self.queue_list, 1)
        left_container = QWidget()
        left_container.setLayout(left_panel)
        left_container.setMinimumWidth(200)
        left_container.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)

        center_panel = QVBoxLayout()
        center_header = QHBoxLayout()
        center_header.setContentsMargins(0, 0, 0, 0)
        center_header.setSpacing(8)
        self.video_title = QLabel("当前视频：")
        self.video_title.setStyleSheet("font-size:18px; font-weight:700;")
        center_header.addWidget(self.video_title, 1)

        self.personality_profile_label = QLabel("个性设置")
        self.personality_profile_label.setStyleSheet("color:#444; font-weight:600;")
        self.personality_profile_buttons: dict[str, QPushButton] = {}
        personality_switch_widget = QWidget()
        personality_switch_layout = QHBoxLayout(personality_switch_widget)
        personality_switch_layout.setContentsMargins(0, 0, 0, 0)
        personality_switch_layout.setSpacing(6)
        personality_switch_layout.addWidget(self.personality_profile_label, 0, Qt.AlignmentFlag.AlignVCenter)
        for profile_id in PERSONALITY_PROFILE_IDS:
            button = QPushButton(default_personality_profile_name(profile_id))
            button.setCheckable(True)
            button.setAutoExclusive(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setMinimumHeight(30)
            button.setMinimumWidth(86)
            button.clicked.connect(lambda _checked=False, pid=profile_id: self.on_home_personality_profile_button_clicked(pid))
            self.personality_profile_buttons[profile_id] = button
            personality_switch_layout.addWidget(button)
        center_header.addWidget(personality_switch_widget, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        self.video_widget = QVideoWidget()
        self.video_widget.setStyleSheet("background:#111;")
        self.video_widget.setMinimumSize(320, 200)
        self.video_widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        self.position_slider = QSlider(Qt.Orientation.Horizontal)
        self.position_slider.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.position_slider.setRange(0, 0)
        self.position_slider.setSingleStep(100)
        self.position_slider.sliderPressed.connect(self.on_slider_pressed)
        self.position_slider.sliderMoved.connect(self.on_slider_moved)
        self.position_slider.sliderReleased.connect(self.on_slider_released)

        self.time_label = QLabel("00:00 / 00:00")
        self.time_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.time_label.setStyleSheet("color:#444;")
        self.time_label.setMinimumWidth(88)

        self.waveform_widget = WaveformWidget()
        self.waveform_widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.waveform_widget.seek_requested.connect(self.on_waveform_seek_requested)

        self.playback_speed_label = QLabel("播放速度")
        self.playback_speed_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.playback_speed_label.setStyleSheet("color:#666;")
        self.playback_speed_spin = QDoubleSpinBox()
        self.playback_speed_spin.setRange(0.25, 3.0)
        self.playback_speed_spin.setSingleStep(0.05)
        self.playback_speed_spin.setDecimals(2)
        self.playback_speed_spin.setSuffix(" x")
        self.playback_speed_spin.setFixedWidth(100)
        self.playback_speed_spin.setValue(self.playback_speed)
        self.playback_speed_spin.valueChanged.connect(self.on_playback_speed_changed)
        speed_box = QWidget()
        speed_box_layout = QVBoxLayout(speed_box)
        speed_box_layout.setContentsMargins(0, 0, 0, 0)
        speed_box_layout.setSpacing(4)
        speed_box_layout.addWidget(self.playback_speed_label)
        speed_box_layout.addWidget(self.playback_speed_spin, 0, Qt.AlignmentFlag.AlignHCenter)
        speed_box_layout.addStretch(1)

        controls = QHBoxLayout()
        self.btn_pause = QPushButton()
        self.btn_mark_in = QPushButton()
        self.btn_mark_out = QPushButton()
        self.btn_pass = QPushButton()
        self.btn_fail = QPushButton()
        self.btn_prev = QPushButton()
        self.btn_end = QPushButton()
        self.btn_reload = QPushButton("重新加载目录")

        self.btn_pause.clicked.connect(self.toggle_pause)
        self.btn_mark_in.clicked.connect(self.mark_trim_in)
        self.btn_mark_out.clicked.connect(self.mark_trim_out_and_save)
        self.btn_pass.clicked.connect(self.mark_pass)
        self.btn_fail.clicked.connect(self.mark_fail)
        self.btn_prev.clicked.connect(self.go_previous)
        self.btn_end.clicked.connect(self.finish_review)
        self.btn_reload.clicked.connect(self.reload_and_reset)

        for btn in [self.btn_pause, self.btn_mark_in, self.btn_mark_out, self.btn_pass, self.btn_fail, self.btn_prev, self.btn_end, self.btn_reload]:
            controls.addWidget(btn)

        self.current_status = QLabel("状态：未开始")
        self.current_status.setStyleSheet("font-size:15px;")
        self.selection_label = QLabel("裁剪区间：未设置")
        self.selection_label.setStyleSheet("color:#555;")
        self.shortcut_hint = QLabel()
        self.shortcut_hint.setStyleSheet("color:#666;")

        timeline_grid = QGridLayout()
        timeline_grid.setContentsMargins(0, 0, 0, 0)
        timeline_grid.setHorizontalSpacing(8)
        timeline_grid.setVerticalSpacing(6)
        timeline_grid.addWidget(self.position_slider, 0, 0)
        timeline_grid.addWidget(self.time_label, 0, 1)
        timeline_grid.addWidget(self.waveform_widget, 1, 0)
        timeline_grid.addWidget(speed_box, 1, 1, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignBottom)
        timeline_grid.setColumnStretch(0, 1)

        center_panel.addLayout(center_header)
        center_panel.addWidget(self.video_widget, 1)
        center_panel.addLayout(timeline_grid)
        center_panel.addWidget(self.current_status)
        center_panel.addWidget(self.selection_label)
        center_panel.addWidget(self.shortcut_hint)
        center_panel.addLayout(controls)
        center_container = QWidget()
        center_container.setLayout(center_panel)
        center_container.setMinimumWidth(360)
        center_container.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        right_panel = QVBoxLayout()
        right_panel.setContentsMargins(0, 0, 0, 0)
        right_panel.setSpacing(8)

        right_title = QLabel("参考图片区")
        right_title.setStyleSheet("font-size:18px; font-weight:700;")
        self.reference_dir_label = QLabel()
        self.reference_dir_label.setWordWrap(True)
        self.reference_dir_label.setStyleSheet("color:#666; font-size:12px; line-height:1.35;")

        person_title = QLabel("人物参考图")
        person_title.setStyleSheet("font-size:15px; font-weight:700;")
        self.person_reference_info = QLabel("未匹配到人物参考图")
        self.person_reference_info.setWordWrap(True)
        self.person_reference_info.setStyleSheet("color:#444; font-size:12px; line-height:1.35;")
        self.person_reference_image_label = QLabel("暂无人物参考图")
        self.person_reference_image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.person_reference_image_label.setMinimumSize(220, 124)
        self.person_reference_image_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.person_reference_image_label.setStyleSheet("border:1px solid #ddd; border-radius:10px; background:#fafafa; color:#666;")

        product_title = QLabel("产品参考图")
        product_title.setStyleSheet("font-size:15px; font-weight:700;")
        self.reference_info = QLabel("未匹配到产品参考图")
        self.reference_info.setWordWrap(True)
        self.reference_info.setStyleSheet("color:#444; font-size:12px; line-height:1.35;")
        self.reference_image_label = QLabel("暂无产品参考图")
        self.reference_image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.reference_image_label.setMinimumSize(220, 180)
        self.reference_image_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.reference_image_label.setStyleSheet("border:1px solid #ddd; border-radius:10px; background:#fafafa; color:#666;")

        self.reference_hint = QLabel()
        self.reference_hint.setWordWrap(True)
        self.reference_hint.setStyleSheet("color:#666; font-size:12px; line-height:1.35;")
        self.btn_cycle_reference = QPushButton()
        self.btn_cycle_reference.setFixedHeight(28)
        self.btn_cycle_reference.clicked.connect(self.cycle_reference_image)

        log_title = QLabel("审核记录")
        log_title.setStyleSheet("font-size:16px; font-weight:700;")
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setPlaceholderText("这里会显示审核记录、裁剪结果与结束后的移动结果。")
        self.log_text.setMinimumHeight(120)

        def build_section(title_widget: QLabel, *widgets: QWidget) -> QFrame:
            frame = QFrame()
            frame.setStyleSheet("QFrame {border:1px solid #e3e3e3; border-radius:10px; background:#fcfcfc;}")
            layout = QVBoxLayout(frame)
            layout.setContentsMargins(10, 10, 10, 10)
            layout.setSpacing(6)
            layout.addWidget(title_widget)
            for widget in widgets:
                layout.addWidget(widget)
            return frame

        dir_frame = QFrame()
        dir_frame.setStyleSheet("QFrame {border:1px solid #e3e3e3; border-radius:10px; background:#fcfcfc;}")
        dir_layout = QVBoxLayout(dir_frame)
        dir_layout.setContentsMargins(10, 10, 10, 10)
        dir_layout.setSpacing(6)
        dir_layout.addWidget(right_title)
        dir_layout.addWidget(self.reference_dir_label)

        person_frame = build_section(person_title, self.person_reference_info, self.person_reference_image_label)
        product_frame = build_section(product_title, self.reference_info, self.reference_image_label, self.reference_hint, self.btn_cycle_reference)
        log_frame = build_section(log_title, self.log_text)

        right_panel.addWidget(dir_frame)
        right_panel.addWidget(person_frame)
        right_panel.addWidget(product_frame)
        right_panel.addWidget(log_frame, 1)

        right_scroll_body = QWidget()
        right_scroll_body.setLayout(right_panel)
        right_scroll = QScrollArea()
        right_scroll.setWidgetResizable(True)
        right_scroll.setFrameShape(QFrame.Shape.NoFrame)
        right_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        right_scroll.setWidget(right_scroll_body)

        right_container = QWidget()
        right_container_layout = QVBoxLayout(right_container)
        right_container_layout.setContentsMargins(0, 0, 0, 0)
        right_container_layout.addWidget(right_scroll)
        right_container.setMinimumWidth(270)
        right_container.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)

        root_splitter.addWidget(left_container)
        root_splitter.addWidget(center_container)
        root_splitter.addWidget(right_container)
        root_splitter.setStretchFactor(0, 2)
        root_splitter.setStretchFactor(1, 6)
        root_splitter.setStretchFactor(2, 3)
        root_splitter.setSizes([260, 860, 320])
        page.addWidget(root_splitter, 1)

        self.setStatusBar(QStatusBar())

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
        self.player.setPlaybackRate(self.playback_speed)
        self.player.mediaStatusChanged.connect(self.on_media_status_changed)
        self.player.errorOccurred.connect(self.on_player_error)
        self.player.durationChanged.connect(self.on_duration_changed)
        self.player.positionChanged.connect(self.on_position_changed)

    def _bind_shortcuts(self) -> None:
        mapping = {
            "pass": self.mark_pass,
            "fail": self.mark_fail,
            "previous": self.go_previous,
            "end": self.finish_review,
            "pause": self.toggle_pause,
            "trim_in": self.mark_trim_in,
            "trim_out": self.mark_trim_out_and_save,
            "cycle_reference": self.cycle_reference_image,
        }
        for key, func in mapping.items():
            shortcut = self.shortcut_objects.get(key)
            if shortcut is None:
                shortcut = QShortcut(self)
                shortcut.activated.connect(func)
                self.shortcut_objects[key] = shortcut
            shortcut.setKey(QKeySequence(self.shortcuts_map[key]))
        self.refresh_shortcut_texts()

    def default_profile_settings(self) -> dict:
        return {
            "source_dir": str(self.base_dir / SOURCE_DIR_NAME),
            "pass_dir": str(self.base_dir / PASS_DIR_NAME),
            "fail_dir": str(self.base_dir / FAIL_DIR_NAME),
            "product_library_dir": str(self.base_dir / PRODUCT_LIBRARY_DIR_NAME),
            "rename_to_finished_repo": False,
            "playback_speed": 1.0,
            "shortcuts": DEFAULT_SHORTCUTS.copy(),
        }

    def normalize_profile_settings(self, raw_settings: Optional[dict], fallback: Optional[dict] = None) -> dict:
        fallback = {
            **(fallback or self.default_profile_settings()),
            "shortcuts": dict((fallback or self.default_profile_settings()).get("shortcuts") or DEFAULT_SHORTCUTS),
        }
        raw_settings = raw_settings or {}
        try:
            playback_speed = max(0.25, min(3.0, float(raw_settings.get("playback_speed", fallback["playback_speed"]))))
        except (TypeError, ValueError):
            playback_speed = float(fallback["playback_speed"])
        shortcuts = dict(fallback.get("shortcuts") or DEFAULT_SHORTCUTS)
        raw_shortcuts = raw_settings.get("shortcuts") if isinstance(raw_settings.get("shortcuts"), dict) else {}
        for key, default_value in DEFAULT_SHORTCUTS.items():
            value = raw_shortcuts.get(key)
            shortcuts[key] = str(value).strip() if value else shortcuts.get(key, default_value)
            if not shortcuts[key]:
                shortcuts[key] = default_value
        return {
            "source_dir": str(raw_settings.get("source_dir") or fallback["source_dir"]),
            "pass_dir": str(raw_settings.get("pass_dir") or fallback["pass_dir"]),
            "fail_dir": str(raw_settings.get("fail_dir") or fallback["fail_dir"]),
            "product_library_dir": str(raw_settings.get("product_library_dir") or fallback["product_library_dir"]),
            "rename_to_finished_repo": bool(raw_settings.get("rename_to_finished_repo", fallback["rename_to_finished_repo"])),
            "playback_speed": playback_speed,
            "shortcuts": shortcuts,
        }

    def build_runtime_profile_settings(self) -> dict:
        return {
            "source_dir": str(self.source_dir),
            "pass_dir": str(self.pass_dir),
            "fail_dir": str(self.fail_dir),
            "product_library_dir": str(self.product_library_dir),
            "rename_to_finished_repo": self.rename_to_finished_repo,
            "playback_speed": self.playback_speed,
            "shortcuts": dict(self.shortcuts_map),
        }

    def normalize_personality_profiles(self, raw_profiles: Optional[dict], fallback_settings: Optional[dict] = None) -> dict[str, dict]:
        fallback_settings = self.normalize_profile_settings(fallback_settings or self.default_profile_settings())
        raw_profiles = raw_profiles or {}
        profiles: dict[str, dict] = {}
        for profile_id in PERSONALITY_PROFILE_IDS:
            raw_entry = raw_profiles.get(profile_id) if isinstance(raw_profiles, dict) else None
            name = default_personality_profile_name(profile_id)
            raw_settings = None
            if isinstance(raw_entry, dict):
                raw_name = str(raw_entry.get("name", "")).strip()
                if raw_name:
                    name = raw_name
                raw_settings = raw_entry.get("settings") if isinstance(raw_entry.get("settings"), dict) else raw_entry
            profiles[profile_id] = {
                "name": name,
                "settings": self.normalize_profile_settings(raw_settings, fallback_settings),
            }
        return profiles

    def sync_runtime_into_active_profile(self) -> None:
        profile_id = self.active_personality_profile
        if profile_id not in PERSONALITY_PROFILE_IDS:
            return
        if not self.personality_profiles:
            self.personality_profiles = self.normalize_personality_profiles(None, self.build_runtime_profile_settings())
        entry = self.personality_profiles.setdefault(profile_id, {
            "name": default_personality_profile_name(profile_id),
            "settings": self.default_profile_settings(),
        })
        entry["name"] = str(entry.get("name") or default_personality_profile_name(profile_id))
        entry["settings"] = self.normalize_profile_settings(self.build_runtime_profile_settings(), self.default_profile_settings())

    def activate_personality_profile(
        self,
        profile_id: str,
        *,
        sync_current_before_switch: bool = True,
        persist: bool = True,
        force_apply: bool = False,
        show_message: bool = True,
        request_label: str = "",
    ) -> None:
        if profile_id not in PERSONALITY_PROFILE_IDS:
            return
        if sync_current_before_switch:
            self.sync_runtime_into_active_profile()
        previous_source_dir = self.source_dir
        previous_product_library_dir = self.product_library_dir
        previous_profile_id = self.active_personality_profile
        self.active_personality_profile = profile_id
        entry = self.personality_profiles.get(profile_id) or {
            "name": default_personality_profile_name(profile_id),
            "settings": self.default_profile_settings(),
        }
        settings = self.normalize_profile_settings(entry.get("settings"), self.default_profile_settings())
        self.personality_profiles[profile_id] = {
            "name": str(entry.get("name") or default_personality_profile_name(profile_id)),
            "settings": settings,
        }
        self.source_dir = Path(settings["source_dir"])
        self.pass_dir = Path(settings["pass_dir"])
        self.fail_dir = Path(settings["fail_dir"])
        self.product_library_dir = Path(settings["product_library_dir"])
        self.rename_to_finished_repo = bool(settings["rename_to_finished_repo"])
        self.playback_speed = float(settings["playback_speed"])
        self.shortcuts_map = dict(settings["shortcuts"])
        if hasattr(self, "player") and self.player is not None:
            self.player.setPlaybackRate(self.playback_speed)
        if hasattr(self, "personality_profile_buttons"):
            self.refresh_personality_profile_widgets()
        if hasattr(self, "shortcut_objects"):
            self._bind_shortcuts()
        if hasattr(self, "reference_dir_label"):
            self.refresh_settings_display()
        need_reload = force_apply or previous_source_dir != self.source_dir or previous_product_library_dir != self.product_library_dir
        if need_reload and hasattr(self, "queue_list"):
            self.reload_and_reset()
        elif hasattr(self, "queue_list"):
            self.refresh_reference_library_index()
            if 0 <= self.current_index < len(self.items):
                self.update_reference_preview_for_item(self.items[self.current_index])
        if persist:
            self.save_config()
        if show_message and hasattr(self, "statusBar"):
            profile_name = self.personality_profiles[profile_id]["name"]
            prefix = f"{request_label}：" if request_label else ""
            self.statusBar().showMessage(f"{prefix}已切换到个性设置“{profile_name}”。", 3000)
            if request_label:
                self.log(f"{request_label}：已切换到个性设置“{profile_name}”。")

    def _load_config(self) -> None:
        if not self.config_path.exists():
            self.personality_profiles = self.normalize_personality_profiles(None, self.default_profile_settings())
            self.active_personality_profile = PERSONALITY_PROFILE_IDS[0]
            settings = self.personality_profiles[self.active_personality_profile]["settings"]
            self.source_dir = Path(settings["source_dir"])
            self.pass_dir = Path(settings["pass_dir"])
            self.fail_dir = Path(settings["fail_dir"])
            self.product_library_dir = Path(settings["product_library_dir"])
            self.rename_to_finished_repo = bool(settings["rename_to_finished_repo"])
            self.playback_speed = float(settings["playback_speed"])
            self.shortcuts_map = dict(settings["shortcuts"])
            default_logo = self.base_dir / LOGO_FILE_NAME
            self.logo_path = default_logo if default_logo.exists() else None
            return
        try:
            data = json.loads(self.config_path.read_text(encoding="utf-8"))
        except Exception:
            return

        legacy_settings = self.default_profile_settings()
        legacy_settings = self.normalize_profile_settings({
            "source_dir": str(self.resolve_config_path(data.get("source_dir"), self.base_dir / SOURCE_DIR_NAME)),
            "pass_dir": str(self.resolve_config_path(data.get("pass_dir"), self.base_dir / PASS_DIR_NAME)),
            "fail_dir": str(self.resolve_config_path(data.get("fail_dir"), self.base_dir / FAIL_DIR_NAME)),
            "product_library_dir": str(self.resolve_config_path(data.get("product_library_dir"), self.base_dir / PRODUCT_LIBRARY_DIR_NAME)),
            "rename_to_finished_repo": bool(data.get("rename_to_finished_repo", False)),
            "playback_speed": data.get("playback_speed", 1.0),
            "shortcuts": data.get("shortcuts") or {},
        }, legacy_settings)

        self.personality_profiles = self.normalize_personality_profiles(data.get("personality_profiles"), legacy_settings)
        active_profile_id = str(data.get("active_personality_profile") or PERSONALITY_PROFILE_IDS[0])
        if active_profile_id not in PERSONALITY_PROFILE_IDS:
            active_profile_id = PERSONALITY_PROFILE_IDS[0]
        self.active_personality_profile = active_profile_id
        active_settings = self.personality_profiles[self.active_personality_profile]["settings"]
        self.source_dir = Path(active_settings["source_dir"])
        self.pass_dir = Path(active_settings["pass_dir"])
        self.fail_dir = Path(active_settings["fail_dir"])
        self.product_library_dir = Path(active_settings["product_library_dir"])
        self.rename_to_finished_repo = bool(active_settings["rename_to_finished_repo"])
        self.playback_speed = float(active_settings["playback_speed"])
        self.shortcuts_map = dict(active_settings["shortcuts"])

        if "selected_review_groups" in data:
            self.group_selection_has_saved_state = True
            saved_groups = data.get("selected_review_groups") or []
            self.selected_review_groups = {str(x) for x in saved_groups if str(x).strip()}
        else:
            self.group_selection_has_saved_state = False
            self.selected_review_groups = set()

        logo_value = data.get("logo_path")
        if logo_value:
            self.logo_path = self.resolve_config_path(logo_value, self.base_dir / LOGO_FILE_NAME)
        else:
            default_logo = self.base_dir / LOGO_FILE_NAME
            self.logo_path = default_logo if default_logo.exists() else None

        selection_map = data.get("reference_selection_map") or {}
        self.reference_selection_map = {str(k): str(v) for k, v in selection_map.items() if k and v}

    @staticmethod
    def resolve_config_path(value: Optional[str], default: Path) -> Path:
        if not value:
            return default
        path = Path(value)
        return path if path.is_absolute() else (default.parent / path)

    @staticmethod
    def safe_path_exists(path: Path) -> bool:
        try:
            return path.exists()
        except OSError:
            return False

    @staticmethod
    def safe_path_is_dir(path: Path) -> bool:
        try:
            return path.is_dir()
        except OSError:
            return False

    @staticmethod
    def safe_relative_text(path: Path, base_dir: Path) -> str:
        try:
            return str(path.relative_to(base_dir))
        except Exception:
            return str(path)

    @staticmethod
    def describe_path_issue(path: Path, label: str) -> str:
        try:
            if not path.exists():
                return f"未找到{label}：{path}"
            if not path.is_dir():
                return f"{label}不是文件夹：{path}"
            return ""
        except OSError as exc:
            return f"{label}暂时无法访问：{path}\n{exc}"

    @staticmethod
    def safe_ensure_dir(path: Path) -> None:
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise RuntimeError(f"目录不可用：{path}\n{exc}") from exc

    @staticmethod
    def safe_unlink(path: Path) -> None:
        try:
            path.unlink()
        except OSError as exc:
            raise RuntimeError(f"删除文件失败：{path}\n{exc}") from exc

    def get_product_library_access_error(self) -> str:
        issue = self.describe_path_issue(self.product_library_dir, "产品库目录")
        if issue.startswith("未找到"):
            return ""
        return issue

    def get_source_dir_access_error(self) -> str:
        return self.describe_path_issue(self.source_dir, "源目录")

    def get_pass_dir_access_error(self) -> str:
        return self.describe_path_issue(self.pass_dir, "通过目录")

    def get_fail_dir_access_error(self) -> str:
        return self.describe_path_issue(self.fail_dir, "不通过目录")


    def get_copy_library_dir(self) -> Path:
        return self.base_dir / COPY_LIBRARY_DIR_NAME

    def get_finished_repository_dir(self) -> Path:
        return self.base_dir / FINISHED_REPOSITORY_DIR_NAME

    def get_copy_library_access_error(self) -> str:
        return self.describe_path_issue(self.get_copy_library_dir(), "文案库目录")

    def get_finished_repository_access_error(self) -> str:
        issue = self.describe_path_issue(self.get_finished_repository_dir(), "成品仓库目录")
        if issue.startswith("未找到"):
            return ""
        return issue

    @staticmethod
    def safe_read_text_with_fallback(path: Path) -> str:
        raw = path.read_bytes()
        for encoding in ("utf-8-sig", "utf-8", "gb18030", "gbk"):
            try:
                return raw.decode(encoding)
            except Exception:
                continue
        return raw.decode("utf-8", errors="ignore")

    def save_config(self) -> None:
        self.sync_runtime_into_active_profile()
        active_entry = self.personality_profiles.get(self.active_personality_profile) or {
            "name": default_personality_profile_name(self.active_personality_profile),
            "settings": self.build_runtime_profile_settings(),
        }
        active_settings = self.normalize_profile_settings(active_entry.get("settings"), self.default_profile_settings())
        serialized_profiles = {}
        for profile_id in PERSONALITY_PROFILE_IDS:
            entry = self.personality_profiles.get(profile_id) or {}
            serialized_profiles[profile_id] = {
                "name": str(entry.get("name") or default_personality_profile_name(profile_id)),
                "settings": self.normalize_profile_settings(entry.get("settings"), active_settings if profile_id == self.active_personality_profile else self.default_profile_settings()),
            }
        data = {
            "source_dir": active_settings["source_dir"],
            "pass_dir": active_settings["pass_dir"],
            "fail_dir": active_settings["fail_dir"],
            "product_library_dir": active_settings["product_library_dir"],
            "rename_to_finished_repo": active_settings["rename_to_finished_repo"],
            "playback_speed": active_settings["playback_speed"],
            "shortcuts": active_settings["shortcuts"],
            "active_personality_profile": self.active_personality_profile,
            "personality_profiles": serialized_profiles,
            "selected_review_groups": sorted(self.selected_review_groups),
            "logo_path": str(self.logo_path) if self.logo_path else "",
            "reference_selection_map": self.reference_selection_map,
        }
        self.config_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _apply_logo(self) -> None:
        icon_path = None
        if self.logo_path and self.safe_path_exists(self.logo_path):
            icon_path = self.logo_path
        else:
            bundled = self.resource_dir / LOGO_FILE_NAME
            if self.safe_path_exists(bundled):
                icon_path = bundled
                self.logo_path = bundled
        if icon_path and self.safe_path_exists(icon_path):
            self.setWindowIcon(QIcon(str(icon_path)))

    def _update_logo_preview(self) -> None:
        if self.logo_path and self.safe_path_exists(self.logo_path):
            pixmap = QPixmap(str(self.logo_path))
            if not pixmap.isNull():
                self.logo_preview.setPixmap(pixmap.scaled(48, 48, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
                return
        self.logo_preview.setText("Logo")

    def refresh_shortcut_texts(self) -> None:
        def native(name: str) -> str:
            return QKeySequence(self.shortcuts_map[name]).toString(QKeySequence.SequenceFormat.NativeText)

        self.btn_pause.setText(f"暂停/继续 ({native('pause')})")
        self.btn_mark_in.setText(f"设置起点 ({native('trim_in')})")
        self.btn_mark_out.setText(f"裁剪到通过 ({native('trim_out')})")
        self.btn_pass.setText(f"通过 ({native('pass')})")
        self.btn_fail.setText(f"不通过 ({native('fail')})")
        self.btn_prev.setText(f"返回上一条 ({native('previous')})")
        self.btn_end.setText(f"结束审核 ({native('end')})")
        self.btn_cycle_reference.setText(f"切换参考图 ({native('cycle_reference')})")
        self.shortcut_hint.setText(
            f"快捷键：{native('pause')}=暂停/继续，{native('trim_in')}=起点，{native('trim_out')}=裁剪通过，"
            f"{native('pass')}=通过，{native('fail')}=不通过，{native('previous')}=返回上一条，{native('end')}=结束审核，"
            f"{native('cycle_reference')}=切换参考图"
        )
        self.reference_hint.setText(
            f"按 {native('cycle_reference')} 可切换当前产品参考图；切换后会自动记住，后续同产品视频默认使用这张图。"
        )

    @staticmethod
    def personality_profile_button_style(selected: bool) -> str:
        if selected:
            return (
                "QPushButton {"
                "padding:4px 14px; font-weight:600;}"
            )
        return "QPushButton {padding:4px 14px;}"

    def refresh_personality_profile_widgets(self) -> None:
        if not hasattr(self, "personality_profile_buttons"):
            return
        self.personality_profile_widget_updating = True
        current_profile_id = self.active_personality_profile if self.active_personality_profile in PERSONALITY_PROFILE_IDS else PERSONALITY_PROFILE_IDS[0]
        for profile_id in PERSONALITY_PROFILE_IDS:
            entry = self.personality_profiles.get(profile_id) or {}
            name = str(entry.get("name") or default_personality_profile_name(profile_id))
            button = self.personality_profile_buttons.get(profile_id)
            if button is None:
                continue
            button.blockSignals(True)
            button.setText(name)
            button.setChecked(profile_id == current_profile_id)
            button.setStyleSheet(self.personality_profile_button_style(profile_id == current_profile_id))
            button.blockSignals(False)
        self.personality_profile_widget_updating = False

    def refresh_settings_display(self) -> None:
        self.reference_dir_label.setText(
            f"人物库目录：{self.person_library_dir}\n"
            f"产品库目录：{self.product_library_dir}\n"
            f"源目录：{self.source_dir}\n"
            f"通过目录：{self.pass_dir}\n"
            f"不通过目录：{self.fail_dir}"
        )
        self.refresh_shortcut_texts()
        self.refresh_personality_profile_widgets()
        if hasattr(self, "playback_speed_spin"):
            self.playback_speed_spin.blockSignals(True)
            self.playback_speed_spin.setValue(self.playback_speed)
            self.playback_speed_spin.blockSignals(False)
        self.refresh_group_filter_display()
        self._update_logo_preview()

    def on_home_personality_profile_button_clicked(self, profile_id: str) -> None:
        if self.personality_profile_widget_updating:
            return
        if profile_id not in PERSONALITY_PROFILE_IDS:
            return
        self.activate_personality_profile(profile_id, request_label="首页快捷切换")

    @Slot(float)
    def on_playback_speed_changed(self, value: float) -> None:
        normalized = max(0.25, min(3.0, round(float(value), 2)))
        self.playback_speed = normalized
        if abs(self.playback_speed_spin.value() - normalized) > 0.001:
            self.playback_speed_spin.blockSignals(True)
            self.playback_speed_spin.setValue(normalized)
            self.playback_speed_spin.blockSignals(False)
        if hasattr(self, "player") and self.player is not None:
            self.player.setPlaybackRate(normalized)
        self.sync_runtime_into_active_profile()
        self.save_config()
        self.statusBar().showMessage(f"播放速度已设置为 {normalized:.2f}x，后续视频将按此速度播放。", 2500)

    @staticmethod
    def natural_sort_strings(values: list[str]) -> list[str]:
        def sort_key(text: str):
            return [int(token) if token.isdigit() else token.lower() for token in re.split(r"(\d+)", text)]
        return sorted(values, key=sort_key)

    @staticmethod
    def derive_review_group_name(relative_path: Path) -> str:
        first_text = relative_path.parts[0] if relative_path.parts else relative_path.stem
        if "-" in first_text:
            return first_text.split("-", 1)[0].strip() or first_text.strip()
        if "－" in first_text:
            return first_text.split("－", 1)[0].strip() or first_text.strip()
        return first_text.strip() or relative_path.stem

    def sync_review_group_selection(self, available_groups: list[str]) -> None:
        self.available_review_groups = self.natural_sort_strings(list(dict.fromkeys(available_groups)))
        available_set = set(self.available_review_groups)
        if not self.available_review_groups:
            self.selected_review_groups = set()
            self.applied_review_groups = set()
            self.rebuild_group_filter_checkboxes()
            self.refresh_group_filter_display()
            return

        if self.startup_review_groups_pending and not self.startup_review_groups_applied:
            self.selected_review_groups = self.startup_review_groups_pending & available_set
            self.group_selection_has_saved_state = True
            self.startup_review_groups_applied = True
        elif not self.group_selection_has_saved_state:
            self.selected_review_groups = set(self.available_review_groups)
            self.group_selection_has_saved_state = True
        else:
            self.selected_review_groups &= available_set

        self.rebuild_group_filter_checkboxes()
        self.refresh_group_filter_display()

    def clear_group_filter_widgets(self) -> None:
        if not hasattr(self, "group_filter_flow_layout"):
            return
        while self.group_filter_flow_layout.count():
            item = self.group_filter_flow_layout.takeAt(0)
            if item is None:
                continue
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.group_checkbox_map.clear()

    def rebuild_group_filter_checkboxes(self) -> None:
        if not hasattr(self, "group_filter_flow_layout"):
            return
        self.group_filter_updating = True
        self.clear_group_filter_widgets()
        for group in self.available_review_groups:
            checkbox = QCheckBox(group)
            checkbox.setChecked(group in self.selected_review_groups)
            checkbox.toggled.connect(self.on_group_checkbox_toggled)
            self.group_filter_flow_layout.addWidget(checkbox)
            self.group_checkbox_map[group] = checkbox
        self.group_filter_updating = False

    def refresh_group_filter_display(self) -> None:
        if not hasattr(self, "group_filter_summary_label"):
            return
        total = len(self.available_review_groups)
        selected_count = len(self.selected_review_groups)
        all_selected = total > 0 and selected_count >= total
        if total == 0:
            summary = "已选：暂无分组"
        elif all_selected:
            summary = f"已选：全部（{total}组）"
        elif selected_count == 0:
            summary = f"已选：0/{total}组"
        else:
            selected_names = self.natural_sort_strings(list(self.selected_review_groups))
            preview = "、".join(selected_names[:4])
            if selected_count > 4:
                preview += f" 等{selected_count}组"
            summary = f"已选：{preview}"
        self.group_filter_summary_label.setText(summary)
        self.group_select_all_button.setEnabled(total > 0)
        self.group_filter_updating = True
        self.group_select_all_button.setChecked(all_selected)
        self.group_select_all_button.setText("取消全选" if all_selected else "全选")
        for group, checkbox in self.group_checkbox_map.items():
            should_checked = group in self.selected_review_groups
            if checkbox.isChecked() != should_checked:
                checkbox.setChecked(should_checked)
        self.group_filter_updating = False

    @Slot(bool)
    def on_group_select_all_toggled(self, checked: bool) -> None:
        if self.group_filter_updating:
            return
        if not self.available_review_groups:
            return
        self.group_selection_has_saved_state = True
        self.group_filter_updating = True
        self.selected_review_groups = set(self.available_review_groups) if checked else set()
        for group, checkbox in self.group_checkbox_map.items():
            checkbox.setChecked(group in self.selected_review_groups)
        self.group_filter_updating = False
        self.refresh_group_filter_display()
        self.schedule_group_filter_reload()

    @Slot(bool)
    def on_group_checkbox_toggled(self, checked: bool) -> None:
        if self.group_filter_updating:
            return
        self.group_selection_has_saved_state = True
        self.selected_review_groups = {group for group, checkbox in self.group_checkbox_map.items() if checkbox.isChecked()}
        self.refresh_group_filter_display()
        self.schedule_group_filter_reload()

    def schedule_group_filter_reload(self) -> None:
        if self.selected_review_groups == self.applied_review_groups:
            return
        self.group_filter_apply_timer.start()

    def apply_pending_group_filter_change(self) -> None:
        if self.selected_review_groups == self.applied_review_groups:
            return
        self.save_config()
        self.reload_and_reset()

    def _load_videos(self) -> None:
        self.items.clear()
        self.queue_list.clear()
        self.current_index = -1
        self.review_active = False
        self.moved_after_finish = False
        self.current_trim_in_ms = None
        self.current_trim_out_ms = None
        self.last_queue_index = -1
        self.pending_player_request_id += 1
        self.waveform_cache.clear()
        self.warmed_video_paths.clear()
        self.reference_pixmap_cache.clear()
        self.reference_match_cache.clear()
        self.refresh_reference_library_index()
        self.clear_reference_preview("暂无参考图")

        source_issue = self.get_source_dir_access_error()
        if source_issue:
            self.summary_label.setText(source_issue)
            self.log(source_issue)
            self.current_status.setText("状态：源目录不可用")
            self.waveform_widget.clear("源目录不可用")
            self.reference_info.setText("源目录不可用，无法匹配参考图。")
            self.available_review_groups = []
            self.selected_review_groups = set()
            self.refresh_group_filter_display()
            return

        try:
            all_files = self._collect_video_files(self.source_dir)
        except Exception as exc:
            message = f"扫描源目录失败：{self.source_dir}\n{exc}"
            self.summary_label.setText(message)
            self.log(message)
            self.current_status.setText("状态：扫描源目录失败")
            self.waveform_widget.clear("扫描源目录失败")
            self.reference_info.setText("扫描源目录失败，无法匹配参考图。")
            self.available_review_groups = []
            self.selected_review_groups = set()
            self.refresh_group_filter_display()
            return

        all_group_names = [
            self.derive_review_group_name(Path(self.safe_relative_text(file_path, self.source_dir)))
            for file_path in all_files
        ]
        self.sync_review_group_selection(all_group_names)
        selected_groups = set(self.selected_review_groups) if self.group_selection_has_saved_state else set(self.available_review_groups)
        self.applied_review_groups = set(selected_groups)

        if self.startup_review_groups_original:
            matched_groups = self.startup_review_groups_original & set(self.available_review_groups)
            missing_groups = self.startup_review_groups_original - matched_groups
            if matched_groups:
                self.log(f"启动接口已预选审核分组：{'、'.join(self.natural_sort_strings(list(matched_groups)))}")
            if missing_groups:
                self.log(f"启动接口指定分组未匹配到：{'、'.join(self.natural_sort_strings(list(missing_groups)))}")
            self.startup_review_groups_original = set()

        for file_path in all_files:
            relative_path = Path(self.safe_relative_text(file_path, self.source_dir))
            if self.derive_review_group_name(relative_path) not in selected_groups:
                continue
            item = VideoItem(source_path=file_path, relative_path=relative_path)
            self.items.append(item)
            self.queue_list.addItem(QListWidgetItem(str(relative_path)))

        if not all_files:
            self.summary_label.setText("没有找到可审核视频。")
            self.current_status.setText("状态：没有视频")
            self.waveform_widget.clear("没有视频")
            self.reference_info.setText("当前没有视频。")
            self.log("源目录中没有找到视频文件。")
            return

        if not self.items:
            self.summary_label.setText("当前所选分组下没有可审核视频。")
            self.current_status.setText("状态：筛选后没有视频")
            self.waveform_widget.clear("筛选后没有视频")
            self.reference_info.setText("当前所选分组下没有视频。")
            self.log("当前所选分组下没有可审核视频，请调整分组筛选。")
            return

        self.review_active = True
        self.current_index = 0
        self.update_queue_view(full_refresh=True)
        self.load_current_video()
        self.log(f"已加载 {len(self.items)} 个视频，准备开始审核。")

    def _collect_video_files(self, directory: Path) -> list[Path]:
        import re

        files: list[Path] = []
        for root, dirnames, filenames in os.walk(directory, topdown=True, onerror=lambda e: None):
            safe_dirnames: list[str] = []
            root_path = Path(root)
            for dirname in list(dirnames):
                try:
                    candidate = root_path / dirname
                    if candidate.is_dir():
                        safe_dirnames.append(dirname)
                except OSError:
                    continue
            dirnames[:] = safe_dirnames

            for filename in filenames:
                try:
                    candidate = root_path / filename
                    if candidate.suffix.lower() in VIDEO_EXTENSIONS:
                        files.append(candidate)
                except OSError:
                    continue

        def natural_sort_key(path: Path):
            text = self.safe_relative_text(path, directory)
            return [int(token) if token.isdigit() else token.lower() for token in re.split(r"(\d+)", text)]

        return sorted(files, key=natural_sort_key)

    def update_summary_label(self) -> None:
        total = len(self.items)
        pending = sum(1 for x in self.items if x.status is None)
        passed = sum(1 for x in self.items if x.status == "pass")
        failed = sum(1 for x in self.items if x.status == "fail")
        trimmed = sum(1 for x in self.items if x.status == "trim_pass")
        current = self.current_index + 1 if 0 <= self.current_index < total else 0
        self.summary_label.setText(
            f"总数：{total}｜当前：{current}/{total}｜待审：{pending}｜通过：{passed}｜截断通过：{trimmed}｜不通过：{failed}"
        )

    def _queue_item_prefix(self, review_item: VideoItem) -> str:
        if review_item.status == "pass":
            return "[通过]"
        if review_item.status == "fail":
            return "[不通过]"
        if review_item.status == "trim_pass":
            return "[截断通过]"
        return "[待审]"

    def refresh_queue_row(self, idx: int) -> None:
        if not (0 <= idx < len(self.items)):
            return
        list_item = self.queue_list.item(idx)
        if list_item is None:
            return
        review_item = self.items[idx]
        list_item.setText(f"{self._queue_item_prefix(review_item)} {review_item.relative_path}")
        list_item.setSelected(idx == self.current_index)
        if idx == self.current_index:
            self.queue_list.scrollToItem(list_item)

    def update_queue_view(self, full_refresh: bool = False) -> None:
        self.update_summary_label()
        if full_refresh:
            self.queue_list.setUpdatesEnabled(False)
            try:
                for idx in range(len(self.items)):
                    self.refresh_queue_row(idx)
            finally:
                self.queue_list.setUpdatesEnabled(True)
            self.last_queue_index = self.current_index
            return

        indices = {idx for idx in {self.last_queue_index, self.current_index} if 0 <= idx < len(self.items)}
        for idx in sorted(indices):
            self.refresh_queue_row(idx)
        self.last_queue_index = self.current_index

    def load_current_video(self) -> None:
        if not (0 <= self.current_index < len(self.items)):
            self.video_title.setText("当前视频：无")
            self.current_status.setText("状态：审核结束")
            self.selection_label.setText("裁剪区间：未设置")
            self.position_slider.setRange(0, 0)
            self.time_label.setText("00:00 / 00:00")
            self.waveform_widget.clear("审核结束")
            self.clear_reference_preview("审核结束")
            self.pending_player_request_id += 1
            self.player.stop()
            return

        item = self.items[self.current_index]
        self.current_trim_in_ms = item.trim_in_ms
        self.current_trim_out_ms = item.trim_out_ms
        self.video_title.setText(f"当前视频：{item.relative_path}")
        state_text = "待审核"
        if item.status == "pass":
            state_text = "已标记通过"
        elif item.status == "fail":
            state_text = "已标记不通过"
        elif item.status == "trim_pass":
            state_text = "已截断通过"
        self.current_status.setText(f"状态：{state_text}")

        self.position_slider.blockSignals(True)
        self.position_slider.setRange(0, max(0, item.duration_ms))
        self.position_slider.setValue(0)
        self.position_slider.blockSignals(False)
        self.time_label.setText("00:00 / 00:00")
        self.waveform_delay_timer.stop()
        cached_peaks = self.waveform_cache.get(str(item.source_path))
        if cached_peaks is not None:
            self.waveform_widget.set_peaks(cached_peaks, "当前视频没有可显示的音频波形")
        else:
            self.waveform_widget.clear("波形准备中...")
        self.refresh_selection_label()
        self.update_waveform_selection()
        self.update_queue_view(full_refresh=False)
        self.statusBar().showMessage(f"正在播放：{item.relative_path}")
        self.schedule_waveform_loading(item.source_path)
        self.update_reference_preview_for_item(item)

        self.pending_player_request_id += 1
        request_id = self.pending_player_request_id
        source_path = item.source_path
        QTimer.singleShot(0, lambda request_id=request_id, source_path=source_path: self.apply_pending_video_source(request_id, source_path))
        self.next_video_prefetch_timer.start(260)

    def apply_pending_video_source(self, request_id: int, source_path: Path) -> None:
        if request_id != self.pending_player_request_id:
            return
        if not (0 <= self.current_index < len(self.items)):
            return
        current_item = self.items[self.current_index]
        if current_item.source_path != source_path:
            return
        self.player.setSource(QUrl.fromLocalFile(str(source_path)))
        self.player.setPlaybackRate(self.playback_speed)
        self.player.play()

    def _start_file_warmup_thread(self, video_path: Path) -> None:
        source_text = str(video_path)
        if source_text in self.warmed_video_paths or source_text in self.file_warm_threads:
            return
        thread = FileWarmupThread(video_path)
        thread.finished_warmup.connect(self.on_file_warm_finished)
        thread.finished.connect(lambda source_text=source_text: self._cleanup_file_warm_thread(source_text))
        self.file_warm_threads[source_text] = thread
        thread.start(QThread.Priority.LowPriority)

    def _cleanup_file_warm_thread(self, source_text: str) -> None:
        thread = self.file_warm_threads.pop(source_text, None)
        if thread is not None:
            thread.deleteLater()

    @Slot(str)
    def on_file_warm_finished(self, source_text: str) -> None:
        self.warmed_video_paths.add(source_text)

    def schedule_waveform_loading(self, video_path: Path) -> None:
        source_path = str(video_path)
        self.waveform_request_path = source_path
        if source_path in self.waveform_cache:
            self.waveform_widget.set_peaks(self.waveform_cache[source_path], "当前视频没有可显示的音频波形")
            self.update_waveform_selection()
            return
        self.waveform_delay_timer.stop()
        self.waveform_delay_timer.start(320)

    def load_delayed_waveform_for_current(self) -> None:
        if not (0 <= self.current_index < len(self.items)):
            return
        current_item = self.items[self.current_index]
        self.start_waveform_loading(current_item.source_path)

    def _start_waveform_thread(self, video_path: Path) -> None:
        source_path = str(video_path)
        if source_path in self.waveform_threads or source_path in self.waveform_cache:
            return
        thread = WaveformThread(video_path)
        thread.finished_waveform.connect(self.on_waveform_ready)
        thread.finished.connect(lambda source_path=source_path: self._cleanup_waveform_thread(source_path))
        self.waveform_threads[source_path] = thread
        self.waveform_thread = thread
        thread.start(QThread.Priority.LowPriority)

    def _cleanup_waveform_thread(self, source_path: str) -> None:
        thread = self.waveform_threads.pop(source_path, None)
        if thread is not None:
            thread.deleteLater()

    def start_waveform_loading(self, video_path: Path) -> None:
        source_path = str(video_path)
        self.waveform_request_path = source_path
        cached_peaks = self.waveform_cache.get(source_path)
        if cached_peaks is not None:
            self.waveform_widget.set_peaks(cached_peaks, "当前视频没有可显示的音频波形")
            self.update_waveform_selection()
            return
        self.waveform_widget.clear("波形加载中...")
        self._start_waveform_thread(video_path)

    def prefetch_waveform(self, video_path: Path) -> None:
        self._start_waveform_thread(video_path)

    def prefetch_next_video_assets(self) -> None:
        for offset in (1, 2):
            next_index = self.current_index + offset
            if not (0 <= next_index < len(self.items)):
                continue
            next_item = self.items[next_index]
            self._start_file_warmup_thread(next_item.source_path)
            self.prefetch_reference_for_item(next_item)

    @Slot(str, object, str)
    def on_waveform_ready(self, source_path: str, peaks: object, error_text: str) -> None:
        if error_text:
            if source_path == self.waveform_request_path:
                self.waveform_widget.set_status(f"波形加载失败：{error_text}")
            return
        peaks_list = list(peaks)
        self.waveform_cache[source_path] = peaks_list
        if source_path != self.waveform_request_path:
            return
        self.waveform_widget.set_peaks(peaks_list, "当前视频没有可显示的音频波形")
        self.update_waveform_selection()

    def on_duration_changed(self, duration: int) -> None:
        self.position_slider.setRange(0, max(0, duration))
        if 0 <= self.current_index < len(self.items):
            self.items[self.current_index].duration_ms = duration
        self.update_time_label(self.player.position(), duration)
        self.update_waveform_selection()

    def on_position_changed(self, position: int) -> None:
        if not self.user_dragging_slider:
            self.position_slider.blockSignals(True)
            self.position_slider.setValue(position)
            self.position_slider.blockSignals(False)
        duration = max(0, self.player.duration())
        fraction = 0.0 if duration <= 0 else position / duration
        self.waveform_widget.set_playhead_fraction(fraction)
        self.update_time_label(position, duration)

    def update_time_label(self, position: int, duration: int) -> None:
        self.time_label.setText(f"{format_ms(position)} / {format_ms(duration)}")

    def update_waveform_selection(self) -> None:
        duration = self.player.duration()
        if duration <= 0 and 0 <= self.current_index < len(self.items):
            duration = self.items[self.current_index].duration_ms
        if duration <= 0:
            self.waveform_widget.set_selection(None, None)
            return
        in_fraction = None if self.current_trim_in_ms is None else self.current_trim_in_ms / duration
        out_fraction = None if self.current_trim_out_ms is None else self.current_trim_out_ms / duration
        self.waveform_widget.set_selection(in_fraction, out_fraction)

    def refresh_selection_label(self) -> None:
        in_text = format_ms(self.current_trim_in_ms) if self.current_trim_in_ms is not None else "未设置"
        out_text = format_ms(self.current_trim_out_ms) if self.current_trim_out_ms is not None else "未设置"
        self.selection_label.setText(f"裁剪区间：起点 {in_text} ｜ 终点 {out_text}")

    def next_unreviewed_or_next(self) -> None:
        next_idx = self.current_index + 1
        if next_idx < len(self.items):
            self.current_index = next_idx
            QTimer.singleShot(0, self.load_current_video)
            return
        self.finish_review(auto_finished=True)

    @Slot()
    def mark_pass(self) -> None:
        if not self.review_active or not (0 <= self.current_index < len(self.items)):
            return
        item = self.items[self.current_index]
        item.status = "pass"
        item.trim_in_ms = None
        item.trim_out_ms = None
        self.log(f"通过：{item.relative_path}")
        self.next_unreviewed_or_next()

    @Slot()
    def mark_fail(self) -> None:
        if not self.review_active or not (0 <= self.current_index < len(self.items)):
            return
        item = self.items[self.current_index]
        item.status = "fail"
        item.trim_in_ms = None
        item.trim_out_ms = None
        self.log(f"不通过：{item.relative_path}")
        self.next_unreviewed_or_next()

    @Slot()
    def toggle_pause(self) -> None:
        if not (0 <= self.current_index < len(self.items)):
            return
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
            self.statusBar().showMessage("已暂停。", 2000)
        else:
            self.player.play()
            self.statusBar().showMessage("继续播放。", 2000)

    @Slot()
    def mark_trim_in(self) -> None:
        if not (0 <= self.current_index < len(self.items)):
            return
        self.current_trim_in_ms = self.player.position()
        self.current_trim_out_ms = None if self.current_trim_out_ms is not None and self.current_trim_out_ms <= self.current_trim_in_ms else self.current_trim_out_ms
        self.items[self.current_index].trim_in_ms = self.current_trim_in_ms
        self.items[self.current_index].trim_out_ms = self.current_trim_out_ms
        self.refresh_selection_label()
        self.update_waveform_selection()
        self.log(f"设置裁剪起点：{self.items[self.current_index].relative_path} -> {format_ms(self.current_trim_in_ms)}")
        self.statusBar().showMessage(f"已设置起点：{format_ms(self.current_trim_in_ms)}", 2500)

    @Slot()
    def mark_trim_out_and_save(self) -> None:
        if not self.review_active or not (0 <= self.current_index < len(self.items)):
            return
        item = self.items[self.current_index]
        end_ms = self.player.position()
        start_ms = self.current_trim_in_ms or 0
        if end_ms <= start_ms:
            QMessageBox.warning(self, "裁剪无效", "终点必须大于起点。请先设置 I，再在更靠后的位置按 O。")
            return
        if end_ms - start_ms < 100:
            QMessageBox.warning(self, "裁剪无效", "裁剪区间太短，请至少保留 0.1 秒。")
            return

        self.player.pause()
        self.current_trim_out_ms = end_ms
        item.trim_in_ms = start_ms
        item.trim_out_ms = end_ms
        self.refresh_selection_label()
        self.update_waveform_selection()

        try:
            clip_output_path = self.create_trimmed_clip(item, start_ms, end_ms)
        except Exception as exc:
            QMessageBox.critical(self, "裁剪失败", f"生成裁剪视频失败：\n{exc}")
            self.log(f"裁剪失败：{item.relative_path} -> {exc}")
            return

        item.status = "trim_pass"
        item.clip_output_path = clip_output_path
        self.log(
            f"截断通过：{item.relative_path} -> {clip_output_path} "
            f"(区间 {format_ms(start_ms)} ~ {format_ms(end_ms)})"
        )
        self.next_unreviewed_or_next()

    def create_trimmed_clip(self, item: VideoItem, start_ms: int, end_ms: int) -> Path:
        ffmpeg_exe = get_ffmpeg_exe()
        target_relative = item.relative_path.with_suffix(".mp4")
        target_path = self.pass_dir / target_relative
        self.safe_ensure_dir(target_path.parent)
        if self.safe_path_exists(target_path):
            target_path = self._make_unique_path(target_path)

        start_sec = f"{start_ms / 1000:.3f}"
        duration_sec = f"{max(0.001, (end_ms - start_ms) / 1000):.3f}"
        cmd = [
            ffmpeg_exe,
            "-y",
            "-ss",
            start_sec,
            "-i",
            str(item.source_path),
            "-t",
            duration_sec,
            "-map",
            "0:v:0?",
            "-map",
            "0:a:0?",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "18",
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
            str(target_path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, **get_hidden_subprocess_kwargs())
        if result.returncode != 0:
            error_text = (result.stderr or result.stdout or "未知错误").strip()
            raise RuntimeError(error_text)
        return target_path

    @Slot()
    def go_previous(self) -> None:
        if not self.items:
            return
        if self.current_index <= 0:
            self.statusBar().showMessage("已经是第一条，无法返回。", 3000)
            return
        self.current_index -= 1
        item = self.items[self.current_index]
        self.reset_item_review(item, remove_generated_clip=True)
        self.log(f"返回上一条重审：{item.relative_path}")
        QTimer.singleShot(0, self.load_current_video)

    def reset_item_review(self, item: VideoItem, remove_generated_clip: bool) -> None:
        if remove_generated_clip and item.clip_output_path and self.safe_path_exists(item.clip_output_path):
            try:
                self.safe_unlink(item.clip_output_path)
                self.log(f"已删除旧裁剪结果：{item.clip_output_path}")
            except Exception as exc:
                self.log(f"删除旧裁剪结果失败：{item.clip_output_path} -> {exc}")
        item.status = None
        item.trim_in_ms = None
        item.trim_out_ms = None
        item.clip_output_path = None

    def stop_waveform_thread_safely(self) -> None:
        thread = getattr(self, "waveform_thread", None)
        if thread is None:
            return
        try:
            running = thread.isRunning()
        except RuntimeError:
            self.waveform_thread = None
            return
        if running:
            try:
                thread.finished_waveform.disconnect(self.on_waveform_ready)
            except Exception:
                pass
            try:
                thread.requestInterruption()
            except Exception:
                pass
            try:
                thread.quit()
            except Exception:
                pass
            try:
                thread.wait(120)
            except Exception:
                pass
        self.waveform_thread = None

    def ensure_source_removed_after_success(self, item: VideoItem, target_path: Optional[Path], reason: str) -> None:
        if item.source_path is None:
            return
        if not self.safe_path_exists(item.source_path):
            return
        if target_path is not None and not self.safe_path_exists(target_path):
            return
        try:
            self.release_current_media_file()
        except Exception:
            pass
        last_error = None
        for _ in range(3):
            try:
                self.safe_unlink(item.source_path)
                self.log(f"已补删源文件：{item.relative_path}（{reason}）")
                return
            except Exception as exc:
                last_error = exc
                try:
                    QApplication.processEvents()
                    QThread.msleep(80)
                except Exception:
                    pass
        if last_error is not None:
            self.log(f"补删源文件失败：{item.relative_path}（{reason}） -> {last_error}")

    def release_current_media_file(self) -> None:
        try:
            self.pending_player_request_id += 1
            self.player.pause()
        except Exception:
            pass
        try:
            self.player.stop()
        except Exception:
            pass
        try:
            self.player.setSource(QUrl())
        except Exception:
            pass
        try:
            QApplication.processEvents()
            QThread.msleep(80)
            QApplication.processEvents()
        except Exception:
            pass

    @Slot(bool)
    def finish_review(self, auto_finished: bool = False) -> None:
        if self.moved_after_finish:
            self.close()
            return
        self.review_active = False
        self.next_video_prefetch_timer.stop()
        self.waveform_delay_timer.stop()
        self.stop_waveform_thread_safely()
        self.release_current_media_file()
        moved_count = self.apply_moves()
        self.moved_after_finish = True
        self.update_queue_view(full_refresh=True)

        pending = sum(1 for x in self.items if x.status is None)
        passed = sum(1 for x in self.items if x.status == "pass")
        failed = sum(1 for x in self.items if x.status == "fail")
        trimmed = sum(1 for x in self.items if x.status == "trim_pass")

        msg = (
            f"审核结束｜通过：{passed}｜截断通过：{trimmed}｜不通过：{failed}｜未处理：{pending}｜已移动：{moved_count}"
        )
        self.log(msg)
        self.log("说明：截断通过生成的新视频已直接保存到通过目录；审核结束时，对应源文件会从分镜生成中删除。")
        self.current_status.setText("状态：审核结束，已按记录处理")
        self.statusBar().showMessage(msg, 8000)

    def apply_moves(self) -> int:
        if self.rename_to_finished_repo:
            return self.apply_moves_with_repository_rename()
        return self.apply_moves_standard()

    def apply_moves_standard(self) -> int:
        moved = 0
        for item in self.items:
            if item.status not in {"pass", "fail", "trim_pass"}:
                continue

            if item.status == "trim_pass":
                if not self.safe_path_exists(item.source_path):
                    self.log(f"跳过（源文件不存在或不可访问）：{item.relative_path}")
                    continue
                try:
                    self.safe_unlink(item.source_path)
                except Exception as exc:
                    self.log(f"删除源文件失败：{item.relative_path} -> {exc}")
                    self.ensure_source_removed_after_success(item, item.clip_output_path, "截断通过后统一收尾")
                    if self.safe_path_exists(item.source_path):
                        continue
                moved += 1
                self.log(f"已删除源文件（截断通过已生成新片段）：{item.relative_path}")
                continue

            target_root = self.pass_dir if item.status == "pass" else self.fail_dir
            target_path = target_root / item.relative_path
            try:
                self.safe_ensure_dir(target_path.parent)
            except Exception as exc:
                self.log(f"跳过（目标目录不可用）：{item.relative_path} -> {exc}")
                continue
            if not self.safe_path_exists(item.source_path):
                self.log(f"跳过（源文件不存在或不可访问）：{item.relative_path}")
                continue
            if self.safe_path_exists(target_path):
                target_path = self._make_unique_path(target_path)
            try:
                shutil.move(str(item.source_path), str(target_path))
            except Exception as exc:
                self.log(f"移动失败：{item.relative_path} -> {target_path} -> {exc}")
                continue
            self.ensure_source_removed_after_success(item, target_path, "移动成功后收尾")
            moved += 1
            self.log(f"已移动：{item.relative_path} -> {target_path}")
        return moved

    def collect_existing_video_stems(self, target_dir: Path) -> set[str]:
        used: set[str] = set()
        if not self.safe_path_exists(target_dir) or not self.safe_path_is_dir(target_dir):
            return used
        try:
            for path in target_dir.iterdir():
                if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS:
                    used.add(path.stem.strip())
        except OSError as exc:
            raise RuntimeError(f"读取成品仓库目录失败：{target_dir}\n{exc}") from exc
        return used

    def sanitize_repository_filename(self, value: str) -> str:
        if not value:
            return ""
        translated = value.translate(str.maketrans({
            "<": "＜",
            ">": "＞",
            ":": "：",
            '"': "”",
            "/": "／",
            "\\": "＼",
            "|": "｜",
            "?": "？",
            "*": "＊",
        }))
        translated = re.sub(r"[\x00-\x1f]", " ", translated)
        translated = re.sub(r"\s+", " ", translated).strip().rstrip(". ")
        if len(translated) > 180:
            translated = translated[:180].rstrip(". ")
        return translated

    def load_copy_library_records(self) -> list[tuple[Path, str, str, str, str]]:
        copy_dir = self.get_copy_library_dir()
        records: list[tuple[Path, str, str, str, str]] = []
        if not self.safe_path_exists(copy_dir) or not self.safe_path_is_dir(copy_dir):
            return records
        try:
            for path in copy_dir.rglob("*"):
                if not path.is_file() or path.suffix.lower() not in COPY_LIBRARY_EXTENSIONS:
                    continue
                relative_text = self.safe_relative_text(path, copy_dir)
                relative_lower = relative_text.lower()
                parent_norm = self._normalize_reference_text(path.parent.name)
                stem_norm = self._normalize_reference_text(path.stem)
                path_norm = self._normalize_reference_text(relative_text)
                records.append((path, relative_lower, parent_norm, stem_norm, path_norm))
        except OSError as exc:
            raise RuntimeError(f"扫描文案库失败：{copy_dir}\n{exc}") from exc
        records.sort(key=lambda row: row[1])
        return records

    def find_copy_library_file_for_item(self, item: VideoItem) -> tuple[str, Optional[Path]]:
        candidates = self.derive_reference_candidates(item)
        candidate_pairs = [(candidate, self._normalize_reference_text(candidate)) for candidate in candidates]
        records = self.load_copy_library_records()
        if not records:
            return self.derive_product_key(item), None

        scored: list[tuple[int, str, Path, str]] = []
        for path, relative_lower, parent_norm, stem_norm, path_norm in records:
            score = 0
            matched_name = ""
            for candidate, candidate_norm in candidate_pairs:
                if not candidate_norm:
                    continue
                if stem_norm == candidate_norm or parent_norm == candidate_norm:
                    score = max(score, 120)
                    matched_name = candidate
                elif candidate_norm in stem_norm or candidate_norm in parent_norm:
                    score = max(score, 90)
                    matched_name = candidate
                elif candidate_norm in path_norm:
                    score = max(score, 70)
                    matched_name = candidate
            if score > 0:
                scored.append((score, relative_lower, path, matched_name or self.derive_product_key(item)))

        if not scored:
            return self.derive_product_key(item), None
        scored.sort(key=lambda row: (-row[0], row[1]))
        best = scored[0]
        return best[3], best[2]

    def read_copy_name_candidates(self, text_file: Path) -> list[str]:
        text = self.safe_read_text_with_fallback(text_file)
        names: list[str] = []
        seen: set[str] = set()
        for raw_line in text.splitlines():
            cleaned = self.sanitize_repository_filename(raw_line)
            if not cleaned or cleaned in seen:
                continue
            seen.add(cleaned)
            names.append(cleaned)
        return names

    def move_item_to_standard_pass_dir(self, item: VideoItem) -> int:
        target_path = self.pass_dir / item.relative_path
        source_path = item.source_path
        try:
            self.safe_ensure_dir(target_path.parent)
        except Exception as exc:
            self.log(f"跳过（通过目录不可用）：{item.relative_path} -> {exc}")
            return 0
        if not self.safe_path_exists(source_path):
            self.log(f"跳过（源文件不存在或不可访问）：{item.relative_path}")
            return 0
        if self.safe_path_exists(target_path):
            target_path = self._make_unique_path(target_path)
        try:
            shutil.move(str(source_path), str(target_path))
        except Exception as exc:
            self.log(f"移动失败：{item.relative_path} -> {target_path} -> {exc}")
            return 0
        self.ensure_source_removed_after_success(item, target_path, "回退到审核通过目录后收尾")
        self.log(f"已回退到审核通过目录：{item.relative_path} -> {target_path}")
        return 1

    def keep_trimmed_clip_in_pass_dir_and_delete_source(self, item: VideoItem, reason: str) -> int:
        if item.clip_output_path:
            self.log(f"{reason}，截断片段保留在审核通过目录：{item.clip_output_path}")
        if not self.safe_path_exists(item.source_path):
            self.log(f"跳过（源文件不存在或不可访问）：{item.relative_path}")
            return 0
        try:
            self.safe_unlink(item.source_path)
        except Exception as exc:
            self.log(f"删除源文件失败：{item.relative_path} -> {exc}")
            self.ensure_source_removed_after_success(item, item.clip_output_path, "保留裁剪片段后收尾")
            if self.safe_path_exists(item.source_path):
                return 0
        self.log(f"已删除源文件（截断通过已生成新片段）：{item.relative_path}")
        return 1

    def apply_moves_with_repository_rename(self) -> int:
        copy_dir = self.get_copy_library_dir()
        finished_root = self.get_finished_repository_dir()
        copy_issue = self.get_copy_library_access_error()
        finished_issue = self.get_finished_repository_access_error()
        if copy_issue and not copy_issue.startswith("未找到"):
            self.log(f"文案库不可用，已回退为原始归档逻辑。\n{copy_issue}")
            return self.apply_moves_standard()
        if finished_issue:
            self.log(f"成品仓库不可用，已回退为原始归档逻辑。\n{finished_issue}")
            return self.apply_moves_standard()
        if not self.safe_path_exists(copy_dir) or not self.safe_path_is_dir(copy_dir):
            self.log(f"未找到文案库目录：{copy_dir}，已回退为原始归档逻辑。")
            return self.apply_moves_standard()
        try:
            self.safe_ensure_dir(finished_root)
        except Exception as exc:
            self.log(f"成品仓库目录不可用，已回退为原始归档逻辑。\n{exc}")
            return self.apply_moves_standard()

        reserved_names_by_dir: dict[str, set[str]] = {}
        moved = 0
        for item in self.items:
            if item.status not in {"pass", "fail", "trim_pass"}:
                continue

            if item.status == "fail":
                target_path = self.fail_dir / item.relative_path
                try:
                    self.safe_ensure_dir(target_path.parent)
                except Exception as exc:
                    self.log(f"跳过（不通过目录不可用）：{item.relative_path} -> {exc}")
                    continue
                if not self.safe_path_exists(item.source_path):
                    self.log(f"跳过（源文件不存在或不可访问）：{item.relative_path}")
                    continue
                if self.safe_path_exists(target_path):
                    target_path = self._make_unique_path(target_path)
                try:
                    shutil.move(str(item.source_path), str(target_path))
                except Exception as exc:
                    self.log(f"移动失败：{item.relative_path} -> {target_path} -> {exc}")
                    continue
                self.ensure_source_removed_after_success(item, target_path, "不通过移动后收尾")
                moved += 1
                self.log(f"已移动到不通过目录：{item.relative_path} -> {target_path}")
                continue

            approved_source_path = item.clip_output_path if item.status == "trim_pass" else item.source_path
            if approved_source_path is None or not self.safe_path_exists(approved_source_path):
                self.log(f"跳过（通过文件不存在或不可访问）：{item.relative_path}")
                continue

            product_key, text_file = self.find_copy_library_file_for_item(item)
            if text_file is None:
                reason = f"未在文案库中匹配到产品文案：{product_key}"
                if item.status == "trim_pass":
                    moved += self.keep_trimmed_clip_in_pass_dir_and_delete_source(item, reason)
                else:
                    self.log(reason)
                    moved += self.move_item_to_standard_pass_dir(item)
                continue

            try:
                name_candidates = self.read_copy_name_candidates(text_file)
            except Exception as exc:
                reason = f"读取文案库失败：{text_file} -> {exc}"
                if item.status == "trim_pass":
                    moved += self.keep_trimmed_clip_in_pass_dir_and_delete_source(item, reason)
                else:
                    self.log(reason)
                    moved += self.move_item_to_standard_pass_dir(item)
                continue

            target_dir = finished_root / item.relative_path.parent
            target_dir_key = str(target_dir)
            try:
                self.safe_ensure_dir(target_dir)
            except Exception as exc:
                reason = f"成品仓库目标目录不可用：{target_dir} -> {exc}"
                if item.status == "trim_pass":
                    moved += self.keep_trimmed_clip_in_pass_dir_and_delete_source(item, reason)
                else:
                    self.log(reason)
                    moved += self.move_item_to_standard_pass_dir(item)
                continue

            if target_dir_key not in reserved_names_by_dir:
                try:
                    reserved_names_by_dir[target_dir_key] = self.collect_existing_video_stems(target_dir)
                except Exception as exc:
                    reason = str(exc)
                    if item.status == "trim_pass":
                        moved += self.keep_trimmed_clip_in_pass_dir_and_delete_source(item, reason)
                    else:
                        self.log(reason)
                        moved += self.move_item_to_standard_pass_dir(item)
                    continue

            reserved = reserved_names_by_dir[target_dir_key]
            selected_name = ""
            for candidate in name_candidates:
                if candidate not in reserved:
                    selected_name = candidate
                    break

            if not selected_name:
                reason = f"文案库已没有可用名称：{text_file.name}（成品仓库对应文件夹内名字都已被占用）"
                if item.status == "trim_pass":
                    moved += self.keep_trimmed_clip_in_pass_dir_and_delete_source(item, reason)
                else:
                    self.log(reason)
                    moved += self.move_item_to_standard_pass_dir(item)
                continue

            reserved.add(selected_name)
            target_path = target_dir / f"{selected_name}{approved_source_path.suffix.lower() or '.mp4'}"
            try:
                shutil.move(str(approved_source_path), str(target_path))
            except Exception as exc:
                reserved.discard(selected_name)
                reason = f"移动到成品仓库失败：{approved_source_path} -> {target_path} -> {exc}"
                if item.status == "trim_pass":
                    moved += self.keep_trimmed_clip_in_pass_dir_and_delete_source(item, reason)
                else:
                    self.log(reason)
                    moved += self.move_item_to_standard_pass_dir(item)
                continue

            if item.status == "trim_pass":
                if not self.safe_path_exists(item.source_path):
                    self.log(f"跳过（源文件不存在或不可访问）：{item.relative_path}")
                else:
                    try:
                        self.safe_unlink(item.source_path)
                    except Exception as exc:
                        self.log(f"删除源文件失败：{item.relative_path} -> {exc}")
                        self.ensure_source_removed_after_success(item, target_path, "截断通过放入成品仓库后收尾")
                    else:
                        self.log(f"已删除源文件（截断通过已生成新片段）：{item.relative_path}")
                self.log(f"已按文案重命名并放入成品仓库：{approved_source_path} -> {target_path}（文案文件：{text_file.name}）")
                moved += 1
            else:
                self.ensure_source_removed_after_success(item, target_path, "成品仓库归档后收尾")
                self.log(f"已按文案重命名并放入成品仓库：{item.relative_path} -> {target_path}（文案文件：{text_file.name}）")
                moved += 1
        return moved

    @staticmethod
    def _make_unique_path(path: Path) -> Path:
        stem = path.stem
        suffix = path.suffix
        parent = path.parent
        idx = 1
        candidate = path
        while ReviewWindow.safe_path_exists(candidate):
            candidate = parent / f"{stem}_{idx}{suffix}"
            idx += 1
        return candidate

    @Slot(int)
    def on_media_status_changed(self, status) -> None:
        if status == QMediaPlayer.MediaStatus.EndOfMedia and 0 <= self.current_index < len(self.items) and self.review_active:
            self.player.setPosition(0)
            self.player.play()

    @Slot()
    def on_slider_pressed(self) -> None:
        if not (0 <= self.current_index < len(self.items)):
            return
        self.user_dragging_slider = True
        self.player.pause()

    @Slot(int)
    def on_slider_moved(self, value: int) -> None:
        if not (0 <= self.current_index < len(self.items)):
            return
        self.player.setPosition(value)
        self.update_time_label(value, self.player.duration())
        duration = self.player.duration()
        self.waveform_widget.set_playhead_fraction(0.0 if duration <= 0 else value / duration)

    @Slot()
    def on_slider_released(self) -> None:
        value = self.position_slider.value()
        self.player.setPosition(value)
        self.user_dragging_slider = False
        self.statusBar().showMessage("已定位到当前帧，按空格可继续播放。", 2500)

    @Slot(float)
    def on_waveform_seek_requested(self, fraction: float) -> None:
        duration = self.player.duration()
        if duration <= 0:
            return
        self.player.pause()
        position = int(duration * fraction)
        self.position_slider.setValue(position)
        self.player.setPosition(position)
        self.waveform_widget.set_playhead_fraction(fraction)
        self.update_time_label(position, duration)
        self.statusBar().showMessage("已定位到当前帧，按空格可继续播放。", 2500)

    @Slot()
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
        QTimer.singleShot(0, self.load_current_video)

    def reload_and_reset(self) -> None:
        self.next_video_prefetch_timer.stop()
        self.waveform_delay_timer.stop()
        self.pending_player_request_id += 1
        self.group_filter_apply_timer.stop()
        self.player.stop()
        self.log("重新加载目录并重置审核记录。")
        self._load_videos()

    def detect_person_library_dir(self) -> Path:
        candidates = [
            self.product_library_dir.parent / PERSON_LIBRARY_DIR_NAME,
            self.source_dir.parent / PERSON_LIBRARY_DIR_NAME,
            self.base_dir / PERSON_LIBRARY_DIR_NAME,
        ]
        seen: set[str] = set()
        for candidate in candidates:
            key = str(candidate)
            if key in seen:
                continue
            seen.add(key)
            if self.safe_path_exists(candidate):
                return candidate
        return candidates[0]

    def _build_reference_records_for_dir(self, base_dir: Path) -> tuple[list[Path], list[tuple[Path, str, str, str, str]], str]:
        if not self.safe_path_exists(base_dir):
            return [], [], ""
        try:
            images: list[Path] = []
            records: list[tuple[Path, str, str, str, str]] = []
            for path in base_dir.rglob("*"):
                try:
                    if path.is_file() and path.suffix.lower() in REFERENCE_IMAGE_EXTENSIONS:
                        relative_text = self.safe_relative_text(path, base_dir)
                        images.append(path)
                        records.append((
                            path,
                            relative_text.lower(),
                            self._normalize_reference_text(relative_text),
                            self._normalize_reference_text(path.parent.name),
                            self._normalize_reference_text(path.stem),
                        ))
                except OSError:
                    continue
            combined = sorted(zip(images, records), key=lambda row: row[1][1])
            return [row[0] for row in combined], [row[1] for row in combined], ""
        except OSError as exc:
            return [], [], f"目录暂时无法访问：{base_dir}\n{exc}"

    def refresh_reference_library_index(self) -> None:
        self.person_library_dir = self.detect_person_library_dir()

        self.reference_library_images = []
        self.reference_library_records = []
        self.reference_library_error_text = ""
        self.reference_match_cache.clear()

        self.person_reference_library_images = []
        self.person_reference_library_records = []
        self.person_reference_library_error_text = ""
        self.person_reference_match_cache.clear()

        self.reference_pixmap_cache.clear()

        access_error = self.get_product_library_access_error()
        if access_error:
            self.reference_library_error_text = access_error
        else:
            images, records, error_text = self._build_reference_records_for_dir(self.product_library_dir)
            self.reference_library_images = images
            self.reference_library_records = records
            self.reference_library_error_text = error_text

        person_issue = self.describe_path_issue(self.person_library_dir, "人物库目录")
        if person_issue.startswith("未找到"):
            person_issue = ""
        if person_issue:
            self.person_reference_library_error_text = person_issue
        else:
            images, records, error_text = self._build_reference_records_for_dir(self.person_library_dir)
            self.person_reference_library_images = images
            self.person_reference_library_records = records
            self.person_reference_library_error_text = error_text

    def clear_reference_preview(self, info_text: str) -> None:
        self.current_reference_product_key = None
        self.current_reference_display_name = ""
        self.current_reference_images = []
        self.current_reference_index = -1
        self.reference_image_original_pixmap = None
        self.reference_info.setText(info_text)
        self.reference_image_label.setText("暂无产品参考图")
        self.reference_image_label.setPixmap(QPixmap())

    def clear_person_reference_preview(self, info_text: str) -> None:
        self.current_person_reference_name = ""
        self.current_person_reference_images = []
        self.person_reference_image_original_pixmap = None
        self.person_reference_info.setText(info_text)
        self.person_reference_image_label.setText("暂无人物参考图")
        self.person_reference_image_label.setPixmap(QPixmap())

    @staticmethod
    def _normalize_reference_text(value: str) -> str:
        return re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "", value).lower()

    @staticmethod
    def _split_reference_tokens(value: str) -> list[str]:
        return [token for token in re.split(r"[-_\s]+", value) if token]

    def derive_product_key(self, item: VideoItem) -> str:
        candidates = self.derive_reference_candidates(item)
        if not candidates:
            return item.source_path.stem
        return min(candidates, key=lambda value: (len(self._normalize_reference_text(value)), len(value)))

    def derive_reference_candidates(self, item: VideoItem) -> list[str]:
        raw_names: list[str] = []
        relative_parts = item.relative_path.parts
        if len(relative_parts) >= 2:
            raw_names.append(relative_parts[0])
            raw_names.append(item.relative_path.parent.name)
        else:
            raw_names.append(item.source_path.stem)

        known_region_tokens = {
            "英国", "美国", "德国", "法国", "意大利", "西班牙", "日本", "韩国", "加拿大", "澳大利亚", "澳洲",
            "美区", "英区", "德区", "法区", "意区", "西区", "欧洲", "中东", "uk", "us", "usa", "de", "fr", "it", "es",
        }

        candidates: list[str] = []
        seen: set[str] = set()

        def add_candidate(name: str) -> None:
            cleaned = name.strip().strip("/\\")
            if not cleaned:
                return
            normalized = self._normalize_reference_text(cleaned)
            if not normalized or normalized in seen:
                return
            seen.add(normalized)
            candidates.append(cleaned)

        for raw_name in raw_names:
            add_candidate(raw_name)
            parts = self._split_reference_tokens(raw_name)
            if len(parts) >= 2:
                add_candidate("-".join(parts[:-1]))
                add_candidate(parts[0])
                if parts[-1].lower() in known_region_tokens:
                    add_candidate("-".join(parts[:-1]))
                    add_candidate(parts[0])
            elif parts:
                add_candidate(parts[0])

        return candidates

    def derive_person_reference_candidates(self, item: VideoItem) -> list[str]:
        raw_names: list[str] = []
        relative_parts = item.relative_path.parts
        if len(relative_parts) >= 2:
            raw_names.append(relative_parts[0])
            raw_names.append(item.relative_path.parent.name)
        else:
            raw_names.append(item.source_path.stem)

        candidates: list[str] = []
        seen: set[str] = set()

        def add_candidate(name: str) -> None:
            cleaned = name.strip().strip("/\\")
            if not cleaned:
                return
            normalized = self._normalize_reference_text(cleaned)
            if not normalized or normalized in seen:
                return
            seen.add(normalized)
            candidates.append(cleaned)

        for raw_name in raw_names:
            parts = self._split_reference_tokens(raw_name)
            if len(parts) >= 3:
                add_candidate("-".join(parts[2:]))
                add_candidate(parts[2])
                add_candidate(parts[-1])
            elif len(parts) >= 2:
                add_candidate(parts[-1])

        return candidates

    def _find_library_images_by_candidates(
        self,
        candidates: list[str],
        default_key: str,
        cache: dict[tuple[str, ...], tuple[str, list[Path]]],
    ) -> tuple[str, list[Path]]:
        if not self.reference_library_records:
            return default_key, []

        candidate_pairs = [
            (candidate, self._normalize_reference_text(candidate))
            for candidate in candidates
            if self._normalize_reference_text(candidate)
        ]
        cache_key = tuple(candidate_norm for _, candidate_norm in candidate_pairs)
        if cache_key in cache:
            return cache[cache_key]

        scored: list[tuple[int, str, Path]] = []
        for image_path, relative_text_lower, path_norm, parent_norm, stem_norm in self.reference_library_records:
            score = 0
            for candidate, candidate_norm in candidate_pairs:
                if parent_norm == candidate_norm:
                    score = max(score, 120)
                elif stem_norm == candidate_norm:
                    score = max(score, 100)
                elif candidate_norm in parent_norm:
                    score = max(score, 80)
                elif candidate_norm in path_norm:
                    score = max(score, 60)
            if score > 0:
                scored.append((score, relative_text_lower, image_path))

        if not scored:
            result = (default_key, [])
            cache[cache_key] = result
            return result

        scored.sort(key=lambda row: (-row[0], row[1]))
        images = [row[2] for row in scored]
        display_key = default_key
        for candidate, candidate_norm in candidate_pairs:
            if any(
                candidate_norm in self._normalize_reference_text(path.parent.name)
                or candidate_norm == self._normalize_reference_text(path.parent.name)
                or candidate_norm == self._normalize_reference_text(path.stem)
                for path in images
            ):
                display_key = candidate
                break
        result = (display_key, images)
        cache[cache_key] = result
        return result

    def find_person_reference_images_for_item(self, item: VideoItem) -> tuple[str, list[Path]]:
        candidates = self.derive_person_reference_candidates(item)
        default_key = candidates[0] if candidates else "人物"
        if not self.person_reference_library_records:
            return default_key, []

        candidate_pairs = [
            (candidate, self._normalize_reference_text(candidate))
            for candidate in candidates
            if self._normalize_reference_text(candidate)
        ]
        cache_key = tuple(candidate_norm for _, candidate_norm in candidate_pairs)
        if cache_key in self.person_reference_match_cache:
            return self.person_reference_match_cache[cache_key]

        scored: list[tuple[int, str, Path]] = []
        for image_path, relative_text_lower, path_norm, parent_norm, stem_norm in self.person_reference_library_records:
            score = 0
            for candidate, candidate_norm in candidate_pairs:
                if parent_norm == candidate_norm:
                    score = max(score, 120)
                elif stem_norm == candidate_norm:
                    score = max(score, 100)
                elif candidate_norm in parent_norm:
                    score = max(score, 80)
                elif candidate_norm in path_norm:
                    score = max(score, 60)
            if score > 0:
                scored.append((score, relative_text_lower, image_path))

        if not scored:
            result = (default_key, [])
            self.person_reference_match_cache[cache_key] = result
            return result

        scored.sort(key=lambda row: (-row[0], row[1]))
        images = [row[2] for row in scored]
        person_key = default_key
        for candidate, candidate_norm in candidate_pairs:
            if any(
                candidate_norm in self._normalize_reference_text(path.parent.name)
                or candidate_norm == self._normalize_reference_text(path.parent.name)
                or candidate_norm == self._normalize_reference_text(path.stem)
                for path in images
            ):
                person_key = candidate
                break
        result = (person_key, images)
        self.person_reference_match_cache[cache_key] = result
        return result

    def find_reference_images_for_item(self, item: VideoItem) -> tuple[str, list[Path]]:
        candidates = self.derive_reference_candidates(item)
        default_key = self.derive_product_key(item)
        return self._find_library_images_by_candidates(candidates, default_key, self.reference_match_cache)

    def get_saved_reference_index(self, product_key: str, images: list[Path]) -> int:
        saved_path = self.reference_selection_map.get(product_key, "")
        if saved_path:
            for idx, image_path in enumerate(images):
                if str(image_path) == saved_path:
                    return idx
        return 0

    def prefetch_reference_for_item(self, item: VideoItem) -> None:
        product_key, images = self.find_reference_images_for_item(item)
        if not images:
            return
        selected_index = self.get_saved_reference_index(product_key, images)
        image_path = images[selected_index]
        cache_key = str(image_path)
        if cache_key in self.reference_pixmap_cache:
            return
        pixmap = QPixmap(str(image_path))
        if not pixmap.isNull():
            self.reference_pixmap_cache[cache_key] = pixmap

    def render_pixmap_to_label(self, label: QLabel, pixmap: Optional[QPixmap], padding: int = 12) -> None:
        if pixmap is None or pixmap.isNull():
            return
        target_size = label.size()
        scaled = pixmap.scaled(
            max(1, target_size.width() - padding),
            max(1, target_size.height() - padding),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        label.setPixmap(scaled)

    def adjust_reference_preview_sizes(self) -> None:
        if hasattr(self, "person_reference_image_label"):
            width = max(220, self.person_reference_image_label.width())
            height = max(124, int(width * 9 / 16))
            self.person_reference_image_label.setMinimumHeight(height)
            self.person_reference_image_label.setMaximumHeight(height)
        if hasattr(self, "reference_image_label"):
            width = max(220, self.reference_image_label.width())
            height = max(180, min(280, int(width * 0.88)))
            self.reference_image_label.setMinimumHeight(height)
            self.reference_image_label.setMaximumHeight(height)

    def render_person_reference_pixmap(self) -> None:
        self.render_pixmap_to_label(self.person_reference_image_label, self.person_reference_image_original_pixmap)

    def render_reference_pixmap(self) -> None:
        self.render_pixmap_to_label(self.reference_image_label, self.reference_image_original_pixmap)

    def update_person_reference_preview_for_item(self, item: VideoItem) -> None:
        person_issue = self.describe_path_issue(self.person_library_dir, "人物库目录")
        if person_issue.startswith("未找到"):
            person_issue = ""
        if person_issue:
            self.clear_person_reference_preview(person_issue)
            return
        if not self.safe_path_exists(self.person_library_dir):
            self.clear_person_reference_preview(f"未找到人物库目录：{self.person_library_dir}")
            return
        if self.person_reference_library_error_text:
            self.clear_person_reference_preview(self.person_reference_library_error_text)
            return

        person_key, images = self.find_person_reference_images_for_item(item)
        self.current_person_reference_name = person_key
        self.current_person_reference_images = images

        if not images:
            self.person_reference_image_original_pixmap = None
            self.person_reference_image_label.setPixmap(QPixmap())
            self.person_reference_image_label.setText("暂无人物参考图")
            self.person_reference_info.setText(
                f"当前人物：{person_key}\n未在人物库中匹配到人物参考图。"
            )
            return

        image_path = images[0]
        cache_key = str(image_path)
        pixmap = self.reference_pixmap_cache.get(cache_key)
        if pixmap is None or pixmap.isNull():
            pixmap = QPixmap(str(image_path))
            if not pixmap.isNull():
                self.reference_pixmap_cache[cache_key] = pixmap
        if pixmap.isNull():
            self.person_reference_image_original_pixmap = None
            self.person_reference_image_label.setPixmap(QPixmap())
            self.person_reference_image_label.setText("图片加载失败")
            self.person_reference_info.setText(f"人物参考图加载失败：{image_path}")
            return

        relative_path = image_path
        if self.safe_path_exists(self.person_library_dir):
            try:
                relative_path = image_path.relative_to(self.person_library_dir)
            except ValueError:
                pass

        self.person_reference_image_original_pixmap = pixmap
        self.person_reference_image_label.setText("")
        self.render_person_reference_pixmap()
        self.person_reference_info.setText(
            f"当前人物：{person_key}\n"
            f"文件：{relative_path}"
        )

    def set_reference_image_by_index(self, index: int, persist: bool) -> None:
        if not self.current_reference_images:
            self.clear_reference_preview("未匹配到产品参考图")
            return
        index %= len(self.current_reference_images)
        image_path = self.current_reference_images[index]
        cache_key = str(image_path)
        pixmap = self.reference_pixmap_cache.get(cache_key)
        if pixmap is None or pixmap.isNull():
            pixmap = QPixmap(str(image_path))
            if not pixmap.isNull():
                self.reference_pixmap_cache[cache_key] = pixmap
        if pixmap.isNull():
            self.reference_image_original_pixmap = None
            self.reference_image_label.setPixmap(QPixmap())
            self.reference_image_label.setText("图片加载失败")
            self.reference_info.setText(f"产品参考图加载失败：{image_path}")
            return

        self.current_reference_index = index
        self.reference_image_original_pixmap = pixmap
        self.reference_image_label.setText("")
        self.render_reference_pixmap()

        relative_path = image_path
        if self.safe_path_exists(self.product_library_dir):
            try:
                relative_path = image_path.relative_to(self.product_library_dir)
            except ValueError:
                pass

        self.reference_info.setText(
            f"当前产品：{self.current_reference_display_name}\n"
            f"参考图：{index + 1}/{len(self.current_reference_images)}\n"
            f"文件：{relative_path}"
        )

        if persist and self.current_reference_product_key:
            self.reference_selection_map[self.current_reference_product_key] = str(image_path)
            self.save_config()

    def update_reference_preview_for_item(self, item: VideoItem) -> None:
        self.update_person_reference_preview_for_item(item)

        access_error = self.get_product_library_access_error()
        if access_error:
            self.clear_reference_preview(access_error)
            return
        if not self.safe_path_exists(self.product_library_dir):
            self.clear_reference_preview(f"未找到产品库目录：{self.product_library_dir}")
            return
        if self.reference_library_error_text:
            self.clear_reference_preview(self.reference_library_error_text)
            return

        product_key, images = self.find_reference_images_for_item(item)
        self.current_reference_product_key = product_key
        self.current_reference_display_name = product_key
        self.current_reference_images = images

        if not images:
            self.current_reference_index = -1
            self.reference_image_original_pixmap = None
            self.reference_image_label.setPixmap(QPixmap())
            self.reference_image_label.setText("暂无产品参考图")
            self.reference_info.setText(
                f"当前产品：{product_key}\n未在产品库中匹配到产品参考图。"
            )
            return

        selected_index = self.get_saved_reference_index(product_key, images)
        self.set_reference_image_by_index(selected_index, persist=False)

    @Slot()
    def cycle_reference_image(self) -> None:
        if not self.current_reference_images:
            self.statusBar().showMessage("当前产品没有可切换的参考图。", 3000)
            return
        next_index = (self.current_reference_index + 1) % len(self.current_reference_images)
        self.set_reference_image_by_index(next_index, persist=True)
        self.log(
            f"已切换参考图：{self.current_reference_display_name} -> "
            f"{self.current_reference_images[next_index].name}"
        )
        self.statusBar().showMessage("已切换当前产品参考图。", 2500)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self.adjust_reference_preview_sizes()
        self.render_person_reference_pixmap()
        self.render_reference_pixmap()

    def open_settings_dialog(self) -> None:
        dialog = SettingsDialog(
            self,
            self.base_dir,
            self.personality_profiles,
            self.active_personality_profile,
            self.logo_path,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        values = dialog.get_values()
        self.personality_profiles = values["personality_profiles"]
        self.active_personality_profile = values["active_profile_id"] if values["active_profile_id"] in PERSONALITY_PROFILE_IDS else PERSONALITY_PROFILE_IDS[0]
        logo_value = values["logo_path"].strip()
        self.logo_path = Path(logo_value) if logo_value else None
        self._apply_logo()
        self.activate_personality_profile(
            self.active_personality_profile,
            sync_current_before_switch=False,
            persist=False,
            force_apply=True,
            show_message=False,
        )
        self.save_config()
        self.refresh_settings_display()
        self.log(f"设置已保存：{self.personality_profiles[self.active_personality_profile]['name']}")
        self.statusBar().showMessage("设置已保存并立即生效。", 4000)

    def open_base_dir(self) -> None:
        if sys.platform.startswith("win"):
            os.startfile(str(self.base_dir))
        else:
            QFileDialog.getOpenFileName(self, "程序目录", str(self.base_dir))

    def log(self, message: str) -> None:
        self.log_text.append(message)

    def closeEvent(self, event) -> None:  # noqa: N802
        self.next_video_prefetch_timer.stop()
        self.pending_player_request_id += 1
        if self.items and not self.moved_after_finish and self.review_active:
            reply = QMessageBox.question(
                self,
                "退出",
                "当前仍有审核记录未执行移动。是否立即结束审核并按记录移动文件？",
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


# ---------- helpers ----------

def format_ms(ms: Optional[int]) -> str:
    if ms is None or ms < 0:
        return "00:00"
    total_seconds = int(round(ms / 1000.0))
    minutes, seconds = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def extract_waveform_peaks(video_path: Path, bins: int = 900, sample_rate: int = 8000) -> list[float]:
    ffmpeg_exe = get_ffmpeg_exe()
    cmd = [
        ffmpeg_exe,
        "-v",
        "error",
        "-i",
        str(video_path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        str(sample_rate),
        "-f",
        "s16le",
        "-",
    ]
    process = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **get_hidden_subprocess_kwargs())
    if process.returncode != 0:
        stderr = process.stderr.decode("utf-8", errors="ignore").strip()
        raise RuntimeError(stderr or "ffmpeg 提取音频失败")

    raw = process.stdout
    if not raw:
        return []

    samples = array("h")
    samples.frombytes(raw)
    if sys.byteorder != "little":
        samples.byteswap()
    total = len(samples)
    if total == 0:
        return []

    if total <= bins:
        peaks = [min(1.0, abs(value) / 32768.0) for value in samples]
    else:
        bucket = total / bins
        peaks = []
        for i in range(bins):
            start = int(i * bucket)
            end = int((i + 1) * bucket)
            segment = samples[start:max(start + 1, end)]
            peak = max(abs(v) for v in segment) / 32768.0
            peaks.append(min(1.0, peak))
    return peaks


def parse_group_names_text(text: str) -> list[str]:
    raw = (text or "").strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except Exception:
        parsed = None
    if isinstance(parsed, list):
        return [str(item).strip() for item in parsed if str(item).strip()]
    normalized = raw.replace("；", ";").replace("，", ",").replace("｜", "|")
    parts = re.split(r"[\n\r,;|]+", normalized)
    return [part.strip() for part in parts if part.strip()]


def parse_launch_options(argv: list[str]) -> tuple[list[str], Optional[set[str]], str]:
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--review-group", action="append", dest="review_groups", default=[], help="预选一个审核分组，可重复传入多次")
    parser.add_argument("--review-groups", dest="review_groups_text", default="", help="预选多个审核分组，支持逗号/分号/竖线分隔，或 JSON 数组字符串")
    parser.add_argument("--review-groups-file", dest="review_groups_file", default="", help="从文本或 JSON 文件读取要预选的审核分组")
    args, qt_args = parser.parse_known_args(argv[1:])

    groups: list[str] = []
    groups.extend(str(item).strip() for item in (args.review_groups or []) if str(item).strip())
    if args.review_groups_text:
        groups.extend(parse_group_names_text(args.review_groups_text))
    if args.review_groups_file:
        try:
            content = Path(args.review_groups_file).read_text(encoding="utf-8")
        except UnicodeDecodeError:
            content = Path(args.review_groups_file).read_text(encoding="utf-8-sig")
        groups.extend(parse_group_names_text(content))

    unique_groups = list(dict.fromkeys(group for group in groups if group))
    request_label = ""
    if unique_groups:
        request_label = "启动接口分组"
        return [argv[0], *qt_args], set(unique_groups), request_label
    return [argv[0], *qt_args], None, request_label


def main() -> None:
    qt_argv, startup_review_groups, startup_request_label = parse_launch_options(sys.argv)
    app = QApplication(qt_argv)
    window = ReviewWindow(startup_review_groups=startup_review_groups, startup_request_label=startup_request_label)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
