from __future__ import annotations

import asyncio
import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
from dataclasses import replace
from dataclasses import dataclass
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup
from app.generated_html_paths import is_pc_archive_html_candidate
from PyQt6.QtCore import QAbstractTableModel, QByteArray, QDate, QItemSelectionModel, QModelIndex, QObject, QProcess, QProcessEnvironment, QRunnable, QSize, QThread, QThreadPool, QTimer, Qt, QUrl, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QAction, QBrush, QColor, QDesktopServices, QLinearGradient, QPainter, QPixmap
from PyQt6.QtMultimedia import QAudioOutput, QMediaPlayer
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QAbstractScrollArea,
    QApplication,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QGroupBox,
    QHeaderView,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QStyledItemDelegate,
    QSizePolicy,
    QStatusBar,
    QStackedWidget,
    QTableView,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QTabWidget,
    QToolBar,
    QVBoxLayout,
    QWidget,
)
from selenium import webdriver
from selenium.webdriver.chrome.options import Options

import ndgr_realtime
import nicolive_post
from niconico_ids import extract_channel_slug, extract_nicolive_id, extract_user_id
import tracker
from codex_exec_runner import extract_reply_json_value, run_codex_exec
from timeshift_handoff import normalize_local_files as normalize_handoff_local_files
from timeshift_handoff import normalize_urls as normalize_handoff_urls
from timeshift_handoff import send_local_files as send_handoff_local_files
from timeshift_handoff import send_urls as send_handoff_urls
from timeshift_handoff import send_tag_edit_url as send_handoff_tag_edit_url


LOG_LEVELS = {"TRACE": 5, "DEBUG": 10, "INFO": 20, "WARN": 30, "ERROR": 40}
LOG_SINKS: list["LogTab"] = []
REACTION_AI_SESSION_LOCK = threading.Lock()
APP_ROOT = Path(__file__).resolve().parents[1]
UI_STATE_PATH = APP_ROOT / "data" / "ui_state.json"
APP_LOG_DIR = APP_ROOT / "data" / "logs"


class AppLogBridge(QObject):
    log = pyqtSignal(str, str, bool)


def _append_log_line(level: str, text: str, echo: bool) -> None:
    if echo:
        print(text)
    try:
        APP_LOG_DIR.mkdir(parents=True, exist_ok=True)
        log_path = APP_LOG_DIR / f"app_{datetime.now().strftime('%Y%m%d')}.log"
        with log_path.open("a", encoding="utf-8") as fp:
            fp.write(text + "\n")
    except Exception:
        pass
    for sink in list(LOG_SINKS):
        sink.append_log(level, text)


APP_LOG_BRIDGE = AppLogBridge()
APP_LOG_BRIDGE.log.connect(_append_log_line)


def append_app_log(message: str, level: str = "INFO") -> None:
    level = level if level in LOG_LEVELS else "INFO"
    timestamp = datetime.now().strftime("%H:%M:%S")
    text = f"[{timestamp}] [{level}] {message}"
    APP_LOG_BRIDGE.log.emit(level, text, True)


def append_tracker_log(level: str, message: str) -> None:
    level = level if level in LOG_LEVELS else "INFO"
    timestamp = datetime.now().strftime("%H:%M:%S")
    text = f"[{timestamp}] [{level}] {message}"
    APP_LOG_BRIDGE.log.emit(level, text, False)


tracker.add_log_sink(append_tracker_log)


APP_ROLE_ENV = "NICONICO_WATCH_APP_ROLE"


def require_timeshift_process() -> None:
    role = str(os.environ.get(APP_ROLE_ENV) or "").strip().lower()
    if role != "timeshift":
        raise RuntimeError(
            "タイムシフト処理は専用アプリの別プロセスからのみ実行できます"
        )


def broadcaster_rows_to_timeshift_urls(rows: list[dict[str, Any]]) -> list[str]:
    urls: list[str] = []
    for row in rows:
        lv = extract_nicolive_id(str(row.get("lv") or ""))
        if not lv:
            continue
        urls.append(f"https://live.nicovideo.jp/watch/{lv}")
    return normalize_handoff_urls(urls)


def broadcaster_rows_to_lvs(rows: list[dict[str, Any]]) -> list[str]:
    lvs: list[str] = []
    seen: set[str] = set()
    for row in rows:
        lv = extract_nicolive_id(str(row.get("lv") or ""))
        if not lv or lv in seen:
            continue
        seen.add(lv)
        lvs.append(lv)
    return lvs


def broadcaster_rows_to_local_video_paths(
    rows: list[dict[str, Any]],
) -> tuple[list[Path], list[str]]:
    lvs = broadcaster_rows_to_lvs(rows)
    paths_by_lv = tracker.existing_recording_video_paths_by_lv(lvs)
    paths = [path for lv in lvs for path in paths_by_lv.get(lv, [])]
    missing_lvs = [lv for lv in lvs if not paths_by_lv.get(lv)]
    return paths, missing_lvs


def send_urls_to_timeshift_gui(urls: list[str]) -> str:
    normalized = normalize_handoff_urls(urls)
    if not normalized:
        raise ValueError("タイムシフトGUIへ送れるURLがありません")
    if send_handoff_urls(normalized, timeout_ms=300):
        return "sent"

    python_exe = APP_ROOT / ".venv" / "Scripts" / "pythonw.exe"
    if not python_exe.exists():
        python_exe = Path(sys.executable).with_name("pythonw.exe")
    if not python_exe.exists():
        raise RuntimeError("コンソールを開かない pythonw.exe が見つかりません")
    arguments = [str(APP_ROOT / "main.py"), "timeshift"]
    for url in normalized:
        arguments.extend(["--input-url", url])
    started = QProcess.startDetached(str(python_exe), arguments, str(APP_ROOT))
    success = bool(started[0]) if isinstance(started, tuple) else bool(started)
    if not success:
        raise RuntimeError("タイムシフトGUIを起動できません")
    return "started"


def send_local_files_to_processing_gui(paths: list[Path | str]) -> str:
    normalized = normalize_handoff_local_files(paths)
    existing = [path for path in normalized if Path(path).is_file()]
    if not existing:
        raise ValueError("ローカル処理へ送れる動画ファイルがありません")
    if send_handoff_local_files(existing, timeout_ms=300):
        return "sent"

    python_exe = APP_ROOT / ".venv" / "Scripts" / "pythonw.exe"
    if not python_exe.exists():
        python_exe = Path(sys.executable).with_name("pythonw.exe")
    if not python_exe.exists():
        raise RuntimeError("コンソールを開かない pythonw.exe が見つかりません")
    arguments = [str(APP_ROOT / "main.py"), "timeshift"]
    for path in existing:
        arguments.extend(["--input-file", path])
    started = QProcess.startDetached(str(python_exe), arguments, str(APP_ROOT))
    success = bool(started[0]) if isinstance(started, tuple) else bool(started)
    if not success:
        raise RuntimeError("ローカル処理GUIを起動できません")
    return "started"


def send_tag_edit_to_timeshift_gui(url: str) -> str:
    value = str(url or "").strip()
    if send_handoff_tag_edit_url(value, timeout_ms=300):
        return "sent"
    python_exe = APP_ROOT / ".venv" / "Scripts" / "pythonw.exe"
    if not python_exe.exists():
        python_exe = Path(sys.executable).with_name("pythonw.exe")
    started = QProcess.startDetached(str(python_exe), [str(APP_ROOT / "main.py"), "timeshift", "--tag-url", value], str(APP_ROOT))
    success = bool(started[0]) if isinstance(started, tuple) else bool(started)
    if not success:
        raise RuntimeError("タイムシフトGUIを起動できません")
    return "started"


def qdate_from_unix_seconds(value: Any) -> QDate | None:
    try:
        seconds = int(value or 0)
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    dt = datetime.fromtimestamp(seconds)
    return QDate(dt.year, dt.month, dt.day)


def make_date_range_controls(
    *,
    on_changed: Any,
    on_all_period: Any | None = None,
    display_format: str = "yyyy年MM月dd日",
) -> tuple[QWidget, QDateEdit, QDateEdit]:
    container = QWidget()
    layout = QHBoxLayout(container)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(6)
    from_date = QDateEdit()
    from_date.setCalendarPopup(True)
    from_date.setDisplayFormat(display_format)
    to_date = QDateEdit()
    to_date.setCalendarPopup(True)
    to_date.setDisplayFormat(display_format)
    from_date.dateChanged.connect(lambda *_args: on_changed())
    to_date.dateChanged.connect(lambda *_args: on_changed())
    layout.addWidget(QLabel("期間"))
    layout.addWidget(from_date)
    layout.addWidget(QLabel("〜"))
    layout.addWidget(to_date)
    if on_all_period is not None:
        all_period_button = QPushButton("全期間")
        all_period_button.clicked.connect(lambda *_args: on_all_period())
        layout.addWidget(all_period_button)
    layout.addStretch(1)
    return container, from_date, to_date


def set_date_range_controls(
    from_date: QDateEdit,
    to_date: QDateEdit,
    start_date: QDate | None,
    end_date: QDate | None,
) -> None:
    if not start_date or not end_date:
        today = QDate.currentDate()
        start_date = today
        end_date = today
    from_date.blockSignals(True)
    to_date.blockSignals(True)
    from_date.setDate(start_date)
    to_date.setDate(end_date)
    from_date.blockSignals(False)
    to_date.blockSignals(False)


def show_table_text_popup(
    table: QTableView,
    index: QModelIndex,
    *,
    text_key: str,
    title_keys: tuple[str, ...] = (),
    min_size: tuple[int, int] = (520, 260),
) -> None:
    if not index.isValid():
        return
    model = table.model()
    rows = getattr(model, "_rows", None)
    if not isinstance(rows, list) or not (0 <= index.row() < len(rows)):
        return
    row = rows[index.row()]
    if not isinstance(row, dict):
        return
    text = str(row.get(text_key) or "").strip()
    if not text:
        return
    title = ""
    for key in title_keys:
        title = str(row.get(key) or "").strip()
        if title:
            break
    popup = QWidget(table, Qt.WindowType.Popup)
    popup.setMinimumSize(*min_size)
    popup.setMaximumWidth(900)
    layout = QVBoxLayout(popup)
    layout.setContentsMargins(10, 10, 10, 10)
    layout.setSpacing(8)
    if title:
        label = QLabel(title)
        label.setWordWrap(True)
        layout.addWidget(label)
    text_view = QTextEdit()
    text_view.setReadOnly(True)
    text_view.setPlainText(text)
    layout.addWidget(text_view, 1)
    anchor = table.visualRect(index)
    pos = table.viewport().mapToGlobal(anchor.bottomLeft())
    popup.move(pos)
    popup.show()


def filter_rows_by_column_queries(
    rows: list[dict[str, Any]],
    queries: dict[str, str],
) -> list[dict[str, Any]]:
    active = {
        key: str(value or "").strip().lower()
        for key, value in queries.items()
        if str(value or "").strip()
    }
    if not active:
        return [dict(row) for row in rows]
    filtered: list[dict[str, Any]] = []
    for row in rows:
        matched = True
        for key, query in active.items():
            if query not in str(row.get(key) or "").lower():
                matched = False
                break
        if matched:
            filtered.append(dict(row))
    return filtered


def attach_header_column_filter_menu(
    table: QTableView,
    filters: dict[str, str],
    *,
    on_changed: Any,
) -> None:
    header = table.horizontalHeader()
    header.setSectionsClickable(True)
    model = table.model()
    for column_def in getattr(model, "columns", []):
        if isinstance(column_def, tuple) and column_def:
            filters.setdefault(str(column_def[0]), "")

    def column_key_and_label(section: int) -> tuple[str, str]:
        model = table.model()
        columns = getattr(model, "columns", [])
        if section < len(columns) and isinstance(columns[section], tuple):
            return str(columns[section][0]), str(columns[section][1])
        label = str(model.headerData(section, Qt.Orientation.Horizontal, Qt.ItemDataRole.DisplayRole) if model else section)
        return label, label

    def open_filter(section: int) -> None:
        try:
            if section < 0:
                return
            key, label = column_key_and_label(section)
            current = filters.get(key, "")
            text, ok = QInputDialog.getText(
                table,
                f"{label} 検索",
                f"{label} に含む文字。空でこの列の検索を解除:",
                QLineEdit.EchoMode.Normal,
                current,
            )
            if not ok:
                return
            filters[key] = text.strip()
            on_changed()
        except Exception:
            append_app_log(traceback.format_exc(), "ERROR")

    header.sectionDoubleClicked.connect(open_filter)


def context_action_row_numbers(table: QTableView, clicked_row: int) -> list[int]:
    selected_rows = sorted(
        {selected.row() for selected in table.selectionModel().selectedRows()}
    )
    return selected_rows if clicked_row in selected_rows else [clicked_row]


def install_table_copy_menu(table: QTableView) -> None:
    table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)

    def index_text(index: QModelIndex) -> str:
        if not index.isValid():
            return ""
        value = index.data(Qt.ItemDataRole.DisplayRole)
        return "" if value is None else str(value)

    def row_dict(row: int) -> dict[str, Any]:
        model = table.model()
        model_row_at = getattr(model, "row_at", None)
        if callable(model_row_at):
            value = model_row_at(row)
            if isinstance(value, dict):
                return dict(value)
        rows = getattr(model, "_rows", None)
        if isinstance(rows, list) and 0 <= row < len(rows) and isinstance(rows[row], dict):
            return dict(rows[row])
        out: dict[str, Any] = {}
        columns = getattr(model, "columns", [])
        for column in range(model.columnCount()):
            key = None
            if column < len(columns):
                column_def = columns[column]
                if isinstance(column_def, tuple) and column_def:
                    key = str(column_def[0])
            header = model.headerData(column, Qt.Orientation.Horizontal, Qt.ItemDataRole.DisplayRole)
            key = key or str(header or column)
            out[key] = index_text(model.index(row, column))
        return out

    def copy_text(text: str, label: str) -> None:
        QApplication.clipboard().setText(text)
        append_app_log(f"{label}: {text[:160]}", "DEBUG")

    def broadcaster_live_programs_url(broadcaster_id: str) -> str:
        broadcaster_id = str(broadcaster_id or "").strip()
        if broadcaster_id.startswith("ch"):
            slug = broadcaster_id[2:]
            return f"https://ch.nicovideo.jp/{slug}" if slug else "https://ch.nicovideo.jp/"
        return f"https://www.nicovideo.jp/user/{broadcaster_id}/live_programs"

    def broadcast_watch_url(row: dict[str, Any]) -> str:
        url = str(row.get("watch_url") or "").strip()
        if url.startswith("http://") or url.startswith("https://"):
            return url
        lv = str(row.get("lv") or "").strip()
        if lv:
            return f"https://live.nicovideo.jp/watch/{lv}"
        return ""

    def generated_html_path(row: dict[str, Any]) -> Path | None:
        lv = str(row.get("lv") or "").strip()
        html_path_text = str(row.get("html_path") or "").strip()
        html_path: Path | None = None
        if html_path_text:
            html_path = Path(html_path_text)
        if html_path is not None and not html_path.is_absolute():
            target_dir = str(row.get("target_dir") or "").strip()
            if target_dir:
                html_path = Path(target_dir) / html_path

        search_dirs: list[Path] = []
        if html_path is not None:
            search_dirs.append(html_path.parent if html_path.suffix else html_path)
        target_dir = str(row.get("target_dir") or "").strip()
        if target_dir:
            search_dirs.append(Path(target_dir))

        if lv:
            for directory in search_dirs:
                if not directory.exists() or not directory.is_dir():
                    continue
                candidates = [
                    path
                    for path in directory.glob(f"{lv}_*.html")
                    if is_pc_archive_html_candidate(path, lv)
                ]
                if candidates:
                    return max(candidates, key=lambda path: path.stat().st_mtime)

            finished_html = APP_ROOT / "storage" / "html" / lv / "index.html"
            if finished_html.exists():
                return finished_html

        if html_path is None or not html_path.exists():
            return None
        if lv and not is_pc_archive_html_candidate(html_path, lv):
            return None
        return html_path

    def selected_rows_text() -> str:
        model = table.model()
        rows = sorted({index.row() for index in table.selectionModel().selectedRows()})
        lines: list[str] = []
        for row in rows:
            values = [index_text(model.index(row, column)) for column in range(model.columnCount())]
            lines.append("\t".join(values))
        return "\n".join(lines)

    def show_menu(position) -> None:
        model = table.model()
        if model is None:
            return
        index = table.indexAt(position)
        if not index.isValid():
            return
        row = row_dict(index.row())
        menu = QMenu(table)
        cell_header = model.headerData(index.column(), Qt.Orientation.Horizontal, Qt.ItemDataRole.DisplayRole)
        cell_value = index_text(index)
        copy_cell = menu.addAction(f"セルをコピー: {cell_header}")
        copy_cell.triggered.connect(lambda _=False, text=cell_value: copy_text(text, "セルコピー"))

        watch_url = broadcast_watch_url(row)
        if watch_url:
            open_watch = menu.addAction("URLにジャンプ")
            open_watch.triggered.connect(
                lambda _=False, target=watch_url: QDesktopServices.openUrl(QUrl(target))
            )
            copy_watch_url = menu.addAction("放送URLをコピー")
            copy_watch_url.triggered.connect(
                lambda _=False, text=watch_url: copy_text(text, "放送URLコピー")
            )

        user_id = str(row.get("user_id") or "").strip()
        if user_id.isdigit():
            user_url = f"https://www.nicovideo.jp/user/{user_id}"
            open_user = menu.addAction("ユーザーページを開く")
            open_user.triggered.connect(
                lambda _=False, target=user_url: QDesktopServices.openUrl(QUrl(target))
            )

        local_processing_sender = getattr(table, "_local_processing_rows_sender", None)
        if callable(local_processing_sender):
            local_processing_rows: list[dict[str, Any]] = []
            for row_number in context_action_row_numbers(table, index.row()):
                row = row_dict(row_number)
                if extract_nicolive_id(str(row.get("lv") or "")):
                    local_processing_rows.append(row)
            if local_processing_rows:
                menu.addSeparator()
                if len(local_processing_rows) == 1:
                    label = "ローカル処理へ送る"
                else:
                    label = f"選択した{len(local_processing_rows)}行をローカル処理へ送る"
                send_local = menu.addAction(label)
                def send_selected_rows_to_local_processing(
                    _checked: bool = False,
                    *,
                    rows: list[dict[str, Any]] = local_processing_rows,
                    sender=local_processing_sender,
                ) -> None:
                    append_app_log(
                        "確認タブからローカル処理へ送信: "
                        + ",".join(broadcaster_rows_to_lvs(rows)),
                        "INFO",
                    )
                    sender(rows)

                send_local.triggered.connect(send_selected_rows_to_local_processing)

        key_actions = [
            ("broadcaster_id", "配信者IDをコピー"),
            ("broadcaster_name", "配信者名をコピー"),
            ("user_id", "ユーザーIDをコピー"),
            ("label", "名前をコピー"),
            ("lv", "LVをコピー"),
            ("watch_url", "URLをコピー"),
            ("title", "タイトルをコピー"),
            ("target_dir", "作業フォルダをコピー"),
        ]
        added_separator = False
        for key, label in key_actions:
            value = str(row.get(key) or "").strip()
            if not value:
                continue
            if not added_separator:
                menu.addSeparator()
                added_separator = True
            action = menu.addAction(label)
            action.triggered.connect(lambda _=False, text=value, label=label: copy_text(text, label))

        html_path = generated_html_path(row)
        generated_url = str(row.get("generated_url") or "").strip()
        if not generated_url and html_path is not None:
            lv = str(row.get("lv") or "").strip()
            broadcaster_id = str(row.get("broadcaster_id") or "").strip()
            if not broadcaster_id:
                path_parts = list(html_path.parts)
                lowered_parts = [part.lower() for part in path_parts]
                if "niconico" in lowered_parts:
                    niconico_index = len(lowered_parts) - 1 - lowered_parts[::-1].index("niconico")
                    if niconico_index + 1 < len(path_parts):
                        broadcaster_id = path_parts[niconico_index + 1]
            if broadcaster_id and lv:
                generated_url = (
                    f"https://warehouse.bitter.jp/niconico/{quote(broadcaster_id)}/"
                    f"{quote(lv)}/{quote(html_path.name)}"
                )
        if generated_url.startswith(("http://", "https://")):
            menu.addSeparator()
            edit_tags = menu.addAction("タグを編集")
            edit_tags.triggered.connect(
                lambda _=False, target=generated_url: send_tag_edit_to_timeshift_gui(target)
            )
            open_generated_url = menu.addAction("生成ページにジャンプ")
            open_generated_url.triggered.connect(
                lambda _=False, target=generated_url: QDesktopServices.openUrl(QUrl(target))
            )
            copy_generated_url = menu.addAction("生成ページURLをコピー")
            copy_generated_url.triggered.connect(
                lambda _=False, text=generated_url: copy_text(text, "生成ページURLコピー")
            )
        if html_path is not None:
            html_path_text = str(html_path)
            menu.addSeparator()
            open_html = menu.addAction("生成HTMLを開く")
            open_html.triggered.connect(
                lambda _=False, target=html_path_text: QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(target).resolve())))
            )
            copy_html_path = menu.addAction("生成HTMLパスをコピー")
            copy_html_path.triggered.connect(
                lambda _=False, text=html_path_text: copy_text(text, "生成HTMLパスコピー")
            )

        broadcaster_id = str(row.get("broadcaster_id") or "").strip()
        if broadcaster_id:
            url = broadcaster_live_programs_url(broadcaster_id)
            menu.addSeparator()
            stop_recording_sender = getattr(table, "_stop_broadcaster_recording_sender", None)
            if callable(stop_recording_sender):
                stop_recording = menu.addAction("この配信者の録画を停止")
                stop_recording.triggered.connect(
                    lambda _=False, target=broadcaster_id, sender=stop_recording_sender: sender(target)
                )
            if bool(getattr(table, "_broadcaster_timeshift_enabled", False)):
                open_timeshift = menu.addAction("取得可能なタイムシフトをすべて取得")

                def launch_broadcaster_timeshift(
                    _checked: bool = False,
                    target: str = url,
                    name: str = str(row.get("broadcaster_name") or broadcaster_id),
                ) -> None:
                    try:
                        result = send_urls_to_timeshift_gui([target])
                        show_status(table, f"{name}: タイムシフトGUIへ配信一覧を送信 ({result})")
                    except Exception as error:
                        append_app_log(traceback.format_exc(), "ERROR")
                        QMessageBox.critical(table, "タイムシフトGUI", str(error))

                open_timeshift.triggered.connect(launch_broadcaster_timeshift)
            open_broadcaster = menu.addAction("配信者の放送一覧を開く")
            open_broadcaster.triggered.connect(
                lambda _=False, target=url: QDesktopServices.openUrl(QUrl(target))
            )
            copy_broadcaster_url = menu.addAction("配信者放送一覧URLをコピー")
            copy_broadcaster_url.triggered.connect(
                lambda _=False, text=url: copy_text(text, "配信者放送一覧URLコピー")
            )

        menu.addSeparator()
        row_text = "\t".join(index_text(model.index(index.row(), column)) for column in range(model.columnCount()))
        copy_row = menu.addAction("この行をTSVでコピー")
        copy_row.triggered.connect(lambda _=False, text=row_text: copy_text(text, "行コピー"))
        row_json = json.dumps(row, ensure_ascii=False, indent=2)
        copy_json = menu.addAction("この行をJSONでコピー")
        copy_json.triggered.connect(lambda _=False, text=row_json: copy_text(text, "行JSONコピー"))
        selected_text = selected_rows_text()
        copy_selected = menu.addAction("選択行をTSVでコピー")
        copy_selected.setEnabled(bool(selected_text))
        copy_selected.triggered.connect(lambda _=False, text=selected_text: copy_text(text, "選択行コピー"))
        menu.exec(table.viewport().mapToGlobal(position))

    table.customContextMenuRequested.connect(show_menu)


def configure_table_header(table: QTableView, widths: list[int] | None = None, default_width: int = 140) -> None:
    header = table.horizontalHeader()
    header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
    header.setStretchLastSection(False)
    header.setSectionsMovable(True)
    model = table.model()
    if model is None:
        return
    for column in range(model.columnCount()):
        width = widths[column] if widths is not None and column < len(widths) else default_width
        table.setColumnWidth(column, width)


def table_column_widths(table: QTableView) -> list[int]:
    model = table.model()
    if model is None:
        return []
    return [table.columnWidth(column) for column in range(model.columnCount())]


def table_header_state(table: QTableView) -> str:
    return bytes(table.horizontalHeader().saveState()).hex()


def apply_table_column_widths(table: QTableView, widths: object) -> None:
    if not isinstance(widths, list):
        return
    model = table.model()
    if model is None:
        return
    for column, width in enumerate(widths[: model.columnCount()]):
        try:
            value = int(width)
        except (TypeError, ValueError):
            continue
        if value >= 30:
            table.setColumnWidth(column, value)


def apply_table_header_state(table: QTableView, state: object) -> None:
    if not isinstance(state, str) or not state:
        return
    try:
        table.horizontalHeader().restoreState(QByteArray.fromHex(state.encode("ascii")))
    except Exception:
        append_app_log(traceback.format_exc(), "DEBUG")


def stabilize_table_scroll(table: QTableView) -> None:
    table.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
    table.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
    table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
    table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
    table.setWordWrap(False)
    install_table_copy_menu(table)


def show_status(widget: QWidget, message: str, level: str = "INFO") -> None:
    append_app_log(message, level)
    current: QWidget | None = widget
    while current is not None:
        status_bar = getattr(current, "statusBar", None)
        if callable(status_bar):
            status_bar().showMessage(message)
            return
        current = current.parentWidget()
    print(message)


@dataclass
class TrackerSnapshot:
    items: list[dict[str, Any]]
    started_at: datetime
    finished_at: datetime
    recording_results: list[dict[str, Any]]


class TrackerTableModel(QAbstractTableModel):
    columns = [
        ("open_action", "視聴"),
        ("record_action", "録画"),
        ("monitor_action", "監視"),
        ("lv", "LV"),
        ("title", "タイトル"),
        ("broadcaster_name", "放送者"),
        ("broadcaster_id", "放送者ID"),
        ("special_user_count", "スペシャル保有"),
        ("elapsed_minutes", "経過分"),
        ("watch_count", "来場"),
        ("comment_count", "コメント"),
        ("status", "状態"),
        ("watch_url", "URL"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._rows: list[dict[str, Any]] = []

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.columns)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None
        row = self._rows[index.row()]
        if role == Qt.ItemDataRole.BackgroundRole:
            key = self.columns[index.column()][0]
            if row.get("is_recording") and key in {"lv", "title"}:
                eased = 0.5 - 0.5 * math.cos(float(getattr(self, "glow_phase", 0.0)))
                return QBrush(QColor(110 + int(95 * eased), 28 + int(26 * eased), 28 + int(26 * eased)))
            if row.get("is_monitored_broadcaster") and key in {"broadcaster_name", "broadcaster_id"}:
                return QBrush(QColor(92, 45, 45))
            try:
                special_user_count = int(row.get("special_user_count") or 0)
            except (TypeError, ValueError):
                special_user_count = 0
            if special_user_count >= 1 and key == "special_user_count":
                return QBrush(QColor(92, 45, 45))
            return None
        if role == Qt.ItemDataRole.ToolTipRole:
            labels = []
            if row.get("is_recording"):
                labels.append(f"録画中 PID {row.get('recording_pid') or ''}".strip())
            if row.get("is_monitored_broadcaster"):
                labels.append("配信者監視登録あり")
            try:
                special_user_count = int(row.get("special_user_count") or 0)
            except (TypeError, ValueError):
                special_user_count = 0
            if special_user_count >= 1:
                labels.append(f"スペシャルユーザー保有数 {special_user_count}")
            return " / ".join(labels) if labels else None
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        key = self.columns[index.column()][0]
        value = row.get(key)
        if key == "elapsed_minutes" and value is not None:
            try:
                return f"{float(value):.1f}"
            except (TypeError, ValueError):
                return str(value)
        if key in {"watch_count", "comment_count", "special_user_count"} and value is not None:
            try:
                return f"{int(value):,}"
            except (TypeError, ValueError):
                return str(value)
        return "" if value is None else str(value)

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal:
            if section < 0 or section >= len(self.columns):
                return None
            return self.columns[section][1]
        return section + 1

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

    def update_rows(self, rows: list[dict[str, Any]]) -> None:
        self.beginResetModel()
        self._rows = rows
        self.endResetModel()

    def lv_at(self, row: int) -> str | None:
        if row < 0 or row >= len(self._rows):
            return None
        lv = self._rows[row].get("lv")
        return str(lv) if lv else None

    def row_for_lv(self, lv: str) -> int | None:
        for index, row in enumerate(self._rows):
            if row.get("lv") == lv:
                return index
        return None

    def item_for_lv(self, lv: str) -> dict[str, Any] | None:
        for row in self._rows:
            if row.get("lv") == lv:
                return dict(row)
        return None

    def set_recording_state(self, lv: str, is_recording: bool, pid: int | None = None) -> None:
        row_index = self.row_for_lv(lv)
        if row_index is None:
            return
        row = self._rows[row_index]
        row["is_recording"] = is_recording
        row["recording_pid"] = pid or ""
        top_left = self.index(row_index, 0)
        bottom_right = self.index(row_index, self.columnCount() - 1)
        self.dataChanged.emit(
            top_left,
            bottom_right,
            [Qt.ItemDataRole.BackgroundRole, Qt.ItemDataRole.ToolTipRole],
        )

    def advance_recording_glow(self) -> None:
        self.glow_phase = (float(getattr(self, "glow_phase", 0.0)) + 0.42) % (math.pi * 2)
        columns = [column for column in (self.column_for_key("lv"), self.column_for_key("title")) if column is not None]
        if not columns:
            return
        for row_index, row in enumerate(self._rows):
            if not row.get("is_recording"):
                continue
            for column in columns:
                index = self.index(row_index, column)
                self.dataChanged.emit(index, index, [Qt.ItemDataRole.BackgroundRole])

    def column_for_key(self, key: str) -> int | None:
        for index, (column_key, _label) in enumerate(self.columns):
            if column_key == key:
                return index
        return None


class TrackerWorkerSignals(QObject):
    finished = pyqtSignal(object)
    failed = pyqtSignal(str)
    log = pyqtSignal(str, str)


class TrackerGlowDelegate(QStyledItemDelegate):
    def paint(self, painter: QPainter, option, index: QModelIndex) -> None:
        model = index.model()
        if isinstance(model, TrackerTableModel) and index.isValid():
            row = model._rows[index.row()] if 0 <= index.row() < len(model._rows) else {}
            key = model.columns[index.column()][0]
            if row.get("is_recording") and key in {"lv", "title"}:
                eased = 0.5 - 0.5 * math.cos(float(getattr(model, "glow_phase", 0.0)))
                base = QColor(120 + int(105 * eased), 28 + int(30 * eased), 28 + int(30 * eased))
                edge = QColor(base)
                edge.setAlpha(45 + int(65 * eased))
                center = QColor(base)
                center.setAlpha(170 + int(75 * eased))
                rect = option.rect.adjusted(-2, -2, 2, 2)
                gradient = QLinearGradient(rect.left(), rect.center().y(), rect.right(), rect.center().y())
                gradient.setColorAt(0.0, edge)
                gradient.setColorAt(0.18, center)
                gradient.setColorAt(0.5, QColor(235, 74, 74, 230))
                gradient.setColorAt(0.82, center)
                gradient.setColorAt(1.0, edge)
                painter.save()
                painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
                painter.fillRect(option.rect, QBrush(gradient))
                painter.restore()
        super().paint(painter, option, index)


class CommentGlowDelegate(QStyledItemDelegate):
    def paint(self, painter: QPainter, option, index: QModelIndex) -> None:
        model = index.model()
        if isinstance(model, CommentTableModel) and index.isValid():
            row = model._rows[index.row()] if 0 <= index.row() < len(model._rows) else {}
            if row.get("is_special_user"):
                eased = 0.5 - 0.5 * math.cos(float(getattr(model, "glow_phase", 0.0)))
                base = QColor(125 + int(100 * eased), 22 + int(30 * eased), 28 + int(28 * eased))
                edge = QColor(base)
                edge.setAlpha(35 + int(60 * eased))
                center = QColor(base)
                center.setAlpha(160 + int(80 * eased))
                rect = option.rect.adjusted(-2, -2, 2, 2)
                gradient = QLinearGradient(rect.left(), rect.center().y(), rect.right(), rect.center().y())
                gradient.setColorAt(0.0, edge)
                gradient.setColorAt(0.18, center)
                gradient.setColorAt(0.5, QColor(235, 58, 70, 235))
                gradient.setColorAt(0.82, center)
                gradient.setColorAt(1.0, edge)
                painter.save()
                painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
                painter.fillRect(option.rect, QBrush(gradient))
                painter.restore()
        super().paint(painter, option, index)


class TrackerFetchJob(QRunnable):
    ndgr_check_lock = threading.Lock()

    def __init__(self, config: tracker.Config) -> None:
        super().__init__()
        self.config = config
        self.signals = TrackerWorkerSignals()

    def log(self, level: str, message: str) -> None:
        tracker.postprocess_log(None, "tracker_job", level, message)

    def run(self) -> None:
        started = datetime.now()
        started_monotonic = time.monotonic()
        try:
            self.log("TRACE", "トラッカージョブ開始")
            items = tracker.fetch_recent_programs(self.config)
            first_source = str(items[0].get("source") or "selenium_dom") if items else "none"
            self.log(
                "DEBUG",
                f"トラッカー取得方式: {self.config.tracker_fetch_method} / 件数 {len(items)} / source {first_source}",
            )
            with tracker.connect() as conn:
                tracker.persist_broadcasts(conn, items)
                recording_results = tracker.start_recordings_for_monitored_broadcasts(conn, items, self.config)
                recording_results.extend(tracker.start_recordings_for_monitored_broadcaster_api(conn, self.config))
                active_items = tracker.list_active_broadcasts(conn)
                monitored_ids = set(tracker.enabled_monitored_broadcaster_map(conn).keys())
                special_user_counts = tracker.enabled_special_user_count_by_broadcaster(conn)
                recording_jobs = tracker.active_recording_job_map(conn)
            for item in active_items:
                broadcaster_id = str(item.get("broadcaster_id") or "").strip()
                lv = str(item.get("lv") or "").strip()
                item["is_monitored_broadcaster"] = broadcaster_id in monitored_ids
                item["special_user_count"] = int(special_user_counts.get(broadcaster_id, 0))
                recording_job = recording_jobs.get(lv) if lv else None
                item["is_recording"] = bool(recording_job)
                item["recording_pid"] = recording_job.get("pid") if recording_job else ""
            self.log(
                "TRACE",
                (
                    "トラッカー一覧反映準備完了: "
                    f"active={len(active_items)} recording_results={len(recording_results)} "
                    f"elapsed={time.monotonic() - started_monotonic:.1f}s"
                ),
            )
            self.signals.finished.emit(TrackerSnapshot(active_items, started, datetime.now(), recording_results))
            self.log(
                "TRACE",
                (
                    "トラッカージョブfinished通知完了: "
                    f"elapsed={time.monotonic() - started_monotonic:.1f}s / NDGR一括チェックは別枠で続行"
                ),
            )
            if not TrackerFetchJob.ndgr_check_lock.acquire(blocking=False):
                self.log(
                    "TRACE",
                    "NDGR一括チェック開始スキップ: 前回のNDGR一括チェックがまだ実行中",
                )
                return
            ndgr_started = time.monotonic()
            try:
                self.log("TRACE", "NDGR一括チェック開始: 排他ロック取得済み")
                with tracker.connect() as conn:
                    check_results = tracker.check_eligible_broadcasts_for_special_users(conn, self.config)
                self.log(
                    "TRACE",
                    (
                        "NDGR一括チェック完了: "
                        f"results={len(check_results)} elapsed={time.monotonic() - ndgr_started:.1f}s"
                    ),
                )
                for result in check_results:
                    lv = str(result.get("lv") or "")
                    status = str(result.get("result") or "")
                    if status == "error":
                        error = str(result.get("error") or "").replace("\r", " ").replace("\n", " ")
                        self.log("WARN", f"NDGR一括取得エラー {lv}: {error[:500]}")
                    elif status == "special_user_linked":
                        self.log(
                            "INFO",
                            f"スペシャルユーザー検出 {lv}: matches={result.get('matches')} linked={result.get('linked')}",
                        )
                    elif status in {"no_special_user_checked", "no_special_user_deleted"}:
                        self.log("DEBUG", f"スペシャルユーザーなし、探索チェック完了 {lv}")
                    elif status == "ended_deleted_api_precheck":
                        self.log(
                            "DEBUG",
                            f"NDGR前API確認で終了/不在、放送破棄 {lv}: {result.get('reason') or 'unknown'}",
                        )
                    elif status:
                        self.log("DEBUG", f"NDGRチェック {lv}: {status}")
            finally:
                TrackerFetchJob.ndgr_check_lock.release()
                self.log(
                    "TRACE",
                    f"NDGR一括チェック排他ロック解放: elapsed={time.monotonic() - ndgr_started:.1f}s",
                )
        except Exception:
            self.signals.failed.emit(traceback.format_exc())


class NameFetchSignals(QObject):
    finished = pyqtSignal(str, str)
    failed = pyqtSignal(str, str)


class NicovideoUserNameFetchJob(QRunnable):
    def __init__(self, user_id: str, original_value: str = "") -> None:
        super().__init__()
        self.user_id = user_id
        self.original_value = original_value
        self.signals = NameFetchSignals()

    def run(self) -> None:
        try:
            if self.user_id.startswith("ch") or extract_channel_slug(self.original_value):
                channel = fetch_niconico_channel_info(self.original_value or self.user_id)
                self.signals.finished.emit(channel["id"], channel["name"])
                return
            name = fetch_nicovideo_user_name(self.user_id)
            self.signals.finished.emit(self.user_id, name)
        except Exception as exc:
            self.signals.failed.emit(self.user_id, str(exc))


class UserProfileFetchSignals(QObject):
    finished = pyqtSignal(str, str, bytes)
    failed = pyqtSignal(str, str)


class NicovideoUserProfileFetchJob(QRunnable):
    def __init__(self, user_id: str) -> None:
        super().__init__()
        self.user_id = user_id
        self.signals = UserProfileFetchSignals()

    def run(self) -> None:
        try:
            profile = fetch_nicovideo_user_profile(self.user_id)
            icon_data = b""
            icon_url = str(profile.get("icon_url") or "")
            if icon_url:
                response = requests.get(icon_url, headers=NICOVIDEO_BROWSER_HEADERS, timeout=10)
                response.raise_for_status()
                icon_data = response.content
            self.signals.finished.emit(self.user_id, str(profile.get("name") or ""), icon_data)
        except Exception as exc:
            self.signals.failed.emit(self.user_id, str(exc))


class FollowingFetchSignals(QObject):
    finished = pyqtSignal(object)
    failed = pyqtSignal(str)


class FollowingFetchJob(QRunnable):
    def __init__(self, user_id: str) -> None:
        super().__init__()
        self.user_id = user_id
        self.signals = FollowingFetchSignals()

    def run(self) -> None:
        try:
            rows = tracker.scrape_following_users(self.user_id)
            self.signals.finished.emit(rows)
        except Exception:
            self.signals.failed.emit(traceback.format_exc())


class FollowingFetchWorker(QObject):
    finished = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, user_id: str) -> None:
        super().__init__()
        self.user_id = user_id

    def run(self) -> None:
        try:
            self.finished.emit(tracker.scrape_following_users(self.user_id))
        except Exception:
            self.failed.emit(traceback.format_exc())


class BroadcasterInfoFetchSignals(QObject):
    finished = pyqtSignal(object)
    failed = pyqtSignal(str)


class BroadcasterInfoFetchJob(QRunnable):
    def __init__(self, lv: str) -> None:
        super().__init__()
        self.lv = lv
        self.signals = BroadcasterInfoFetchSignals()

    def run(self) -> None:
        try:
            session = nicolive_post.load_latest_user_session()
            page_data = nicolive_post.fetch_page_data(self.lv, session)
            self.signals.finished.emit(page_data)
        except Exception as exc:
            self.signals.failed.emit(str(exc))


class StreamEndCheckSignals(QObject):
    finished = pyqtSignal(str, object)
    failed = pyqtSignal(str, str)


class StreamEndCheckJob(QRunnable):
    def __init__(self, lv: str) -> None:
        super().__init__()
        self.lv = lv
        self.signals = StreamEndCheckSignals()

    @staticmethod
    def _to_unix_timestamp(value: object) -> float | None:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def run(self) -> None:
        try:
            broadcaster_id = ""
            archive_end_time: float | None = None
            with tracker.connect() as conn:
                row = conn.execute(
                    """
                    SELECT broadcaster_id
                    FROM (
                        SELECT broadcaster_id FROM broadcasts WHERE lv = ?
                        UNION ALL
                        SELECT broadcaster_id FROM recording_jobs WHERE lv = ?
                        UNION ALL
                        SELECT broadcaster_id FROM broadcast_archive_meta WHERE lv = ?
                    )
                    WHERE COALESCE(TRIM(broadcaster_id), '') != ''
                    LIMIT 1
                    """,
                    (self.lv, self.lv, self.lv),
                ).fetchone()
                if row:
                    broadcaster_id = str(row["broadcaster_id"] or "").strip()
                meta_row = conn.execute(
                    """
                    SELECT broadcaster_id, end_time
                    FROM broadcast_archive_meta
                    WHERE lv = ?
                    LIMIT 1
                    """,
                    (self.lv,),
                ).fetchone()
                if meta_row:
                    if not broadcaster_id:
                        broadcaster_id = str(meta_row["broadcaster_id"] or "").strip()
                    archive_end_time = self._to_unix_timestamp(meta_row["end_time"])
                if not broadcaster_id and archive_end_time is None:
                    try:
                        meta = tracker.fetch_and_save_broadcast_archive_meta(conn, self.lv)
                        conn.commit()
                        broadcaster_id = str(meta.get("broadcaster_id") or "").strip()
                        archive_end_time = self._to_unix_timestamp(meta.get("end_time"))
                    except Exception:
                        broadcaster_id = ""
            if not broadcaster_id:
                if archive_end_time is not None:
                    now_ts = time.time()
                    if now_ts >= archive_end_time:
                        self.signals.finished.emit(
                            self.lv,
                            {
                                "checked": True,
                                "source": "broadcast_archive_meta",
                                "on_air": False,
                                "reason": "end_time_elapsed",
                                "lv": self.lv,
                                "end_time": archive_end_time,
                            },
                        )
                        return
                    self.signals.finished.emit(
                        self.lv,
                        {
                            "checked": True,
                            "source": "broadcast_archive_meta",
                            "on_air": True,
                            "reason": "end_time_in_future",
                            "lv": self.lv,
                            "end_time": archive_end_time,
                        },
                    )
                    return
                self.signals.finished.emit(
                    self.lv,
                    {"checked": False, "reason": "missing_broadcaster_id"},
                )
                return
            result = tracker.check_live_still_on_air_by_broadcaster_api(self.lv, broadcaster_id)
            self.signals.finished.emit(self.lv, result)
        except Exception:
            self.signals.failed.emit(self.lv, traceback.format_exc())


class DisableMonitorSignals(QObject):
    finished = pyqtSignal(str, str)
    failed = pyqtSignal(str, str)


class DisableMonitorForLiveJob(QRunnable):
    def __init__(self, lv: str) -> None:
        super().__init__()
        self.lv = lv
        self.signals = DisableMonitorSignals()

    def run(self) -> None:
        try:
            broadcaster_id = ""
            with tracker.connect() as conn:
                row = conn.execute(
                    """
                    SELECT broadcaster_id
                    FROM broadcasts
                    WHERE lv = ?
                    UNION
                    SELECT broadcaster_id
                    FROM recording_jobs
                    WHERE lv = ?
                    UNION
                    SELECT broadcaster_id
                    FROM broadcast_archive_meta
                    WHERE lv = ?
                    LIMIT 1
                    """,
                    (self.lv, self.lv, self.lv),
                ).fetchone()
                if row:
                    broadcaster_id = str(row["broadcaster_id"] or "").strip()
                if not broadcaster_id:
                    try:
                        meta = tracker.fetch_and_save_broadcast_archive_meta(conn, self.lv)
                        conn.commit()
                        broadcaster_id = str(meta.get("broadcaster_id") or "").strip()
                    except Exception:
                        broadcaster_id = ""
                if not broadcaster_id:
                    self.signals.failed.emit(self.lv, "放送者IDなし")
                    return
                conn.execute(
                    "UPDATE monitored_broadcasters SET enabled = 0, updated_at = ? WHERE broadcaster_id = ?",
                    (tracker.now(), broadcaster_id),
                )
                conn.commit()
            self.signals.finished.emit(self.lv, broadcaster_id)
        except Exception:
            self.signals.failed.emit(self.lv, traceback.format_exc())


class ReactionPostSignals(QObject):
    posted = pyqtSignal(str, str, str)
    skipped = pyqtSignal(str, str)
    failed = pyqtSignal(str, str)


class ReactionPostJob(QRunnable):
    def __init__(self, lv: str, comment: dict[str, Any], current_count: int = 0) -> None:
        super().__init__()
        self.lv = lv
        self.comment = dict(comment)
        self.current_count = current_count
        self.signals = ReactionPostSignals()

    def run(self) -> None:
        user_id = str(self.comment.get("user_id") or "").strip()
        comment_text = str(self.comment.get("text") or "")
        if not user_id:
            self.signals.skipped.emit("", "user_idなし")
            return
        try:
            session = nicolive_post.load_latest_user_session()
            page_data = nicolive_post.fetch_page_data(self.lv, session)
            broadcaster_id = str(page_data.broadcaster_id or "").strip()
            if not broadcaster_id:
                self.signals.skipped.emit(user_id, "放送者IDなし")
                return
            with tracker.connect() as conn:
                settings = tracker.resolve_reaction_settings(
                    conn,
                    user_id=user_id,
                    broadcaster_id=broadcaster_id,
                    comment_text=comment_text,
                )
            if not settings.get("enabled"):
                self.signals.skipped.emit(user_id, str(settings.get("reason") or "反応設定なし"))
                return
            max_reactions = int(settings.get("max_reactions") or 1)
            if self.current_count >= max_reactions:
                self.signals.skipped.emit(user_id, f"最大反応回数到達: {max_reactions}")
                return
            reaction_type = str(settings.get("reaction_type") or "fixed")
            messages: list[str]
            if reaction_type == "ai":
                engine = str(settings.get("reaction_engine") or "codex_exec")
                if engine not in {"codex_exec", "claude", "grok"}:
                    self.signals.skipped.emit(user_id, f"AI反応担当は未接続: {engine}")
                    return
                provider = "codex" if engine == "codex_exec" else engine
                ai_config = replace(
                    tracker.codex_exec_config(),
                    enabled=True,
                    provider=provider,
                    command={"codex": "codex", "claude": "claude", "grok": "grok"}[provider],
                    model=str(settings.get("reaction_model") or ""),
                    effort=str(settings.get("reaction_effort") or "medium"),
                )
                configured_prompt = str(settings.get("prompt") or "").strip()
                skip_prompt = str(settings.get("reaction_skip_prompt") or "").strip()
                prompt = (
                    f"{configured_prompt}\n\n" if configured_prompt else ""
                )
                prompt += f"コメント: {comment_text}\n"
                prompt += f"最大文字数: {int(settings.get('reaction_max_chars') or 100)}\n"
                if skip_prompt:
                    prompt += f"{skip_prompt}\n"
                prompt += '出力は {"reply":"反応文"} というJSONオブジェクト1個だけにしてください。'
                session_id = ""
                with REACTION_AI_SESSION_LOCK:
                    if provider == "codex":
                        with tracker.connect() as conn:
                            session_row = conn.execute(
                                "SELECT reaction_session_id FROM special_users WHERE user_id = ?",
                                (user_id,),
                            ).fetchone()
                        session_id = str(session_row["reaction_session_id"] or "").strip() if session_row else ""
                    result = run_codex_exec(
                        prompt,
                        config=ai_config,
                        session_id=session_id,
                    )
                    if not result.ok and session_id and provider == "codex":
                        append_app_log(
                            f"AI反応 resume失敗のため新規セッションへ切替: user={user_id}",
                            "WARN",
                        )
                        result = run_codex_exec(prompt, config=ai_config)
                    if result.ok and result.session_id and provider == "codex":
                        with tracker.connect() as conn:
                            conn.execute(
                                "UPDATE special_users SET reaction_session_id = ?, updated_at = ? WHERE user_id = ?",
                                (result.session_id, tracker.now(), user_id),
                            )
                            conn.commit()
                        append_app_log(
                            f"AI反応Codexセッション: user={user_id} session={result.session_id} mode={'resume' if session_id else 'new'}",
                            "INFO",
                        )
                if not result.ok:
                    self.signals.failed.emit(user_id, f"AI反応生成失敗: returncode={result.returncode}")
                    return
                reply = extract_reply_json_value(result.text)
                if not reply:
                    self.signals.skipped.emit(user_id, "AI返答にJSON文字列replyなし（平文は破棄）")
                    return
                if reply.strip().upper() == "SKIP":
                    self.signals.skipped.emit(user_id, "AI判断により無反応: SKIP")
                    return
                messages = [reply]
            elif reaction_type == "fixed":
                messages = [
                    line.strip()
                    for line in str(settings.get("messages") or "").splitlines()
                    if line.strip()
                ]
            else:
                self.signals.skipped.emit(user_id, f"未対応の反応種別: {reaction_type}")
                return
            if not messages:
                self.signals.skipped.emit(user_id, "定型メッセージなし")
                return
            delay = float(settings.get("reaction_delay_seconds") or 0.0)
            if delay > 0:
                time.sleep(delay)
            text = messages[0]
            nicolive_post.post_comment(
                self.lv,
                text,
                user_session=session,
                dry_run=False,
                is_anonymous=True,
            )
            self.signals.posted.emit(user_id, broadcaster_id, text)
        except Exception as exc:
            self.signals.failed.emit(user_id, str(exc))


class StartupLiveScanSignals(QObject):
    live_found = pyqtSignal(object)
    progress = pyqtSignal(str)
    finished = pyqtSignal(int)
    failed = pyqtSignal(str)


class StartupLiveScanJob(QRunnable):
    def __init__(self) -> None:
        super().__init__()
        self.signals = StartupLiveScanSignals()

    def run(self) -> None:
        try:
            config = tracker.load_config()
            with tracker.connect() as conn:
                links = tracker.list_enabled_special_user_broadcasters(conn)
            found: list[dict[str, Any]] = []
            seen_lvs: set[str] = set()
            seen_broadcasters: set[str] = set()
            for link in links:
                broadcaster_id = str(link.get("broadcaster_id") or "").strip()
                if not broadcaster_id or broadcaster_id in seen_broadcasters:
                    continue
                seen_broadcasters.add(broadcaster_id)
                self.signals.progress.emit(f"配信者ページ確認中: {broadcaster_id}")
                try:
                    lives = tracker.scrape_on_air_live_programs(broadcaster_id, config)
                except Exception as exc:
                    self.signals.progress.emit(f"配信者ON_AIR確認失敗: {broadcaster_id}: {type(exc).__name__}: {exc}")
                    continue
                for live in lives:
                    lv = str(live.get("lv") or "").strip()
                    if not lv or lv in seen_lvs:
                        continue
                    seen_lvs.add(lv)
                    row = {
                        **live,
                        "broadcaster_id": broadcaster_id,
                        "broadcaster_name": str(link.get("broadcaster_name") or ""),
                        "auto_open_source": "special_linked",
                    }
                    found.append(row)
                    self.signals.live_found.emit(row)
                time.sleep(1.0)
            self.signals.finished.emit(len(found))
        except Exception:
            self.signals.failed.emit(traceback.format_exc())


class TrackerTab(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.config = tracker.load_config()
        self.thread_pool = QThreadPool.globalInstance()
        self.model = TrackerTableModel()
        self.running = True
        self.fetching = False
        self.last_finished_at: datetime | None = None
        self.next_due_at: datetime | None = None

        self.countdown = QProgressBar()
        self.countdown.setRange(0, max(1, self.config.poll_seconds))
        self.countdown.setTextVisible(True)

        self.summary = QLabel("未取得")
        self.legend = QLabel(
            '<span style="background:#5c2d2d; color:#ffffff; padding:2px 8px;">LV/タイトル発光: 録画中</span> '
            '<span style="background:#5c2d2d; color:#ffffff; padding:2px 8px;">放送者/ID赤: 監視登録</span> '
            '<span style="background:#5c2d2d; color:#ffffff; padding:2px 8px;">スペシャル保有赤: 1以上</span>'
        )
        self.legend.setTextFormat(Qt.TextFormat.RichText)
        self.start_button = QPushButton("トラッカー開始")
        self.start_button.clicked.connect(self.start_tracker)
        self.stop_button = QPushButton("トラッカー停止")
        self.stop_button.clicked.connect(self.stop_tracker)
        self.refresh_button = QPushButton("今すぐ取得")
        self.refresh_button.clicked.connect(self.fetch_now)

        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setItemDelegate(TrackerGlowDelegate(self.table))
        stabilize_table_scroll(self.table)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableView.SelectionMode.ExtendedSelection)
        self.table.setSortingEnabled(False)
        self.table.verticalHeader().setVisible(False)
        configure_table_header(self.table, [70, 70, 70, 110, 360, 180, 120, 110, 80, 80, 90, 100, 220])

        top = QWidget()
        top_layout = QVBoxLayout(top)
        top_layout.setContentsMargins(0, 0, 0, 0)
        control_row = QWidget()
        control_layout = QHBoxLayout(control_row)
        control_layout.setContentsMargins(0, 0, 0, 0)
        control_layout.addWidget(self.start_button)
        control_layout.addWidget(self.stop_button)
        control_layout.addWidget(self.refresh_button)
        control_layout.addStretch(1)
        top_layout.addWidget(self.summary)
        top_layout.addWidget(self.legend)
        top_layout.addWidget(self.countdown)
        top_layout.addWidget(control_row)

        layout = QVBoxLayout(self)
        layout.addWidget(top)
        layout.addWidget(self.table, 1)

        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.on_tick)
        self.timer.start()
        self.glow_timer = QTimer(self)
        self.glow_timer.setInterval(140)
        self.glow_timer.timeout.connect(self.model.advance_recording_glow)
        self.glow_timer.start()
        self.update_tracker_buttons()
        self.fetch_now()

    def fetch_now(self) -> None:
        if self.fetching:
            return
        self.fetching = True
        self.update_tracker_buttons()
        self.summary.setText("取得中...")
        append_app_log("トラッカー取得開始", "DEBUG")
        job = TrackerFetchJob(self.config)
        job.signals.finished.connect(self.on_fetch_finished)
        job.signals.failed.connect(self.on_fetch_failed)
        self.thread_pool.start(job)

    def on_fetch_finished(self, snapshot: TrackerSnapshot) -> None:
        vertical_scroll = self.table.verticalScrollBar().value()
        horizontal_scroll = self.table.horizontalScrollBar().value()
        was_at_bottom = vertical_scroll >= self.table.verticalScrollBar().maximum() - 4
        selected_lvs = {
            self.model.lv_at(index.row())
            for index in self.table.selectionModel().selectedRows()
        }
        selected_lvs.discard(None)
        current_lv = self.model.lv_at(self.table.currentIndex().row())
        self.fetching = False
        self.update_tracker_buttons()
        self.last_finished_at = snapshot.finished_at
        self.next_due_at = snapshot.finished_at.timestamp() + self.config.poll_seconds if self.running else None
        self.model.update_rows(snapshot.items)
        self.restore_selection(selected_lvs, current_lv)
        self.install_row_buttons()
        if was_at_bottom:
            self.table.scrollToBottom()
        else:
            self.table.verticalScrollBar().setValue(vertical_scroll)
        self.table.horizontalScrollBar().setValue(horizontal_scroll)
        QTimer.singleShot(0, lambda value=horizontal_scroll: self.table.horizontalScrollBar().setValue(value))
        elapsed = (snapshot.finished_at - snapshot.started_at).total_seconds()
        self.summary.setText(
            f"最新取得 {snapshot.finished_at.strftime('%H:%M:%S')} / "
            f"{len(snapshot.items)}件 / 取得時間 {elapsed:.1f}秒"
        )
        append_app_log(
            f"トラッカー取得完了: {len(snapshot.items)}件 / {elapsed:.1f}秒",
            "DEBUG",
        )
        for result in snapshot.recording_results:
            lv = result.get("lv") or ""
            broadcaster = result.get("broadcaster_name") or result.get("broadcaster_id") or ""
            if result.get("started"):
                append_app_log(
                    f"自動録画開始: {lv} / {broadcaster} / PID {result.get('pid')}",
                    "INFO",
                )
            elif result.get("reason") == "already_running":
                append_app_log(
                    f"自動録画スキップ: {lv} / 既に起動中 PID {result.get('pid')}",
                    "DEBUG",
                )
            else:
                append_app_log(
                    f"自動録画失敗: {lv} / {broadcaster} / {result.get('reason') or 'unknown'} {result.get('error') or result.get('path') or ''}",
                    "ERROR",
                )
            if result.get("source") == "monitored_broadcaster_api" and lv:
                live = {
                    "lv": str(lv),
                    "watch_url": str(result.get("watch_url") or f"https://live.nicovideo.jp/watch/{lv}"),
                    "status": "ON_AIR",
                    "title": str(result.get("title") or ""),
                    "text": str(result.get("text") or result.get("title") or ""),
                    "broadcaster_id": str(result.get("broadcaster_id") or ""),
                    "broadcaster_name": str(result.get("broadcaster_name") or ""),
                    "auto_open_source": "monitored_broadcaster",
                }
                window = self.window()
                if hasattr(window, "enqueue_auto_live"):
                    window.enqueue_auto_live(live)
        window = self.window()
        if hasattr(window, "open_linked_broadcast_tabs"):
            window.open_linked_broadcast_tabs(snapshot.items)
        if hasattr(window, "reload_broadcaster_monitors"):
            window.reload_broadcaster_monitors()
        self.on_tick()

    def on_fetch_failed(self, detail: str) -> None:
        self.fetching = False
        self.update_tracker_buttons()
        last_line = next((line for line in reversed(detail.splitlines()) if line.strip()), "取得エラー")
        self.summary.setText(f"取得エラー: {last_line}")
        show_status(self, f"トラッカー取得エラー: {last_line}", "ERROR")
        append_app_log(detail, "DEBUG")
        self.next_due_at = datetime.now().timestamp() + self.config.poll_seconds if self.running else None

    def on_tick(self) -> None:
        if self.fetching:
            self.countdown.setFormat("取得中...")
            self.countdown.setValue(0)
            return
        if not self.running:
            self.countdown.setValue(0)
            self.countdown.setFormat("停止中")
            return
        if self.next_due_at is None:
            self.next_due_at = datetime.now().timestamp()
        remaining = max(0, int(self.next_due_at - datetime.now().timestamp()))
        elapsed = self.config.poll_seconds - remaining
        self.countdown.setValue(max(0, min(self.config.poll_seconds, elapsed)))
        self.countdown.setFormat(f"次の一覧取得まで {remaining} 秒")
        if remaining <= 0:
            self.fetch_now()

    def start_tracker(self) -> None:
        if self.running:
            return
        self.running = True
        self.next_due_at = datetime.now().timestamp()
        self.summary.setText("トラッカー開始")
        self.update_tracker_buttons()
        self.on_tick()

    def stop_tracker(self) -> None:
        self.running = False
        self.next_due_at = None
        self.summary.setText("トラッカー停止中" if not self.fetching else "取得中。完了後に停止")
        self.update_tracker_buttons()
        self.on_tick()

    def ui_state(self) -> dict[str, Any]:
        return {
            "version": 3,
            "table_widths": table_column_widths(self.table),
            "table_header": table_header_state(self.table),
        }

    def restore_ui_state(self, state: object) -> None:
        if not isinstance(state, dict):
            return
        if int(state.get("version") or 0) != 3:
            return
        apply_table_column_widths(self.table, state.get("table_widths"))
        apply_table_header_state(self.table, state.get("table_header"))

    def update_tracker_buttons(self) -> None:
        self.start_button.setEnabled(not self.running and not self.fetching)
        self.stop_button.setEnabled(self.running)
        self.refresh_button.setEnabled(not self.fetching)

    def restore_selection(self, selected_lvs: set[str | None], current_lv: str | None) -> None:
        selection = self.table.selectionModel()
        selection.clearSelection()
        for lv in selected_lvs:
            if not lv:
                continue
            row = self.model.row_for_lv(lv)
            if row is None:
                continue
            selection.select(
                self.model.index(row, 0),
                QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows,
            )
        if current_lv:
            row = self.model.row_for_lv(current_lv)
            if row is not None:
                selection.setCurrentIndex(
                    self.model.index(row, 0),
                    QItemSelectionModel.SelectionFlag.NoUpdate,
                )

    def install_row_buttons(self) -> None:
        vertical_scroll = self.table.verticalScrollBar().value()
        horizontal_scroll = self.table.horizontalScrollBar().value()
        for row in range(self.model.rowCount()):
            lv = self.model.lv_at(row)
            if not lv:
                continue
            item = self.model.item_for_lv(lv) or {}
            record_text = "録画停止" if item.get("is_recording") else "録画"
            self.table.setIndexWidget(self.model.index(row, 0), self.row_button("視聴", lv, self.on_open_clicked))
            self.table.setIndexWidget(self.model.index(row, 1), self.row_button(record_text, lv, self.on_record_clicked))
            self.table.setIndexWidget(self.model.index(row, 2), self.row_button("監視", lv, self.on_monitor_clicked))
        self.table.verticalScrollBar().setValue(vertical_scroll)
        self.table.horizontalScrollBar().setValue(horizontal_scroll)
        QTimer.singleShot(0, lambda value=horizontal_scroll: self.table.horizontalScrollBar().setValue(value))

    def row_button(self, text: str, lv: str, callback) -> QWidget:
        box = QWidget()
        layout = QHBoxLayout(box)
        layout.setContentsMargins(4, 2, 4, 2)
        button = QPushButton(text)
        button.setFixedHeight(26)
        button.clicked.connect(lambda _checked=False, target_lv=lv: callback(target_lv))
        layout.addWidget(button)
        return box

    def on_open_clicked(self, lv: str) -> None:
        window = self.window()
        if hasattr(window, "open_comment_tab"):
            window.open_comment_tab(lv)
        show_status(self, f"放送タブを開く: {lv}")

    def on_monitor_clicked(self, lv: str) -> None:
        item = self.model.item_for_lv(lv)
        if not item:
            show_status(self, f"監視登録失敗: 行データなし {lv}", "ERROR")
            return
        broadcaster_id = str(item.get("broadcaster_id") or "").strip()
        broadcaster_name = str(item.get("broadcaster_name") or "").strip()
        if not broadcaster_id:
            show_status(self, f"監視登録失敗: 放送者IDなし {lv}", "ERROR")
            return
        try:
            target_dir = tracker.save_monitored_broadcaster(
                broadcaster_id=broadcaster_id,
                broadcaster_name=broadcaster_name,
                source_lv=lv,
                enabled=True,
            )
            window = self.window()
            if hasattr(window, "reload_broadcaster_monitors"):
                window.reload_broadcaster_monitors()
            show_status(
                self,
                f"配信者監視登録: {broadcaster_id} / {broadcaster_name or broadcaster_id} / {target_dir}",
                "INFO",
            )
            with tracker.connect() as conn:
                result = tracker.start_recording_for_broadcast(conn, item, self.config)
                conn.commit()
            if result.get("started"):
                self.model.set_recording_state(lv, True, int(result.get("pid") or 0))
                self.install_row_buttons()
                show_status(self, f"監視登録して録画開始: {lv} / PID {result.get('pid')}", "INFO")
            elif result.get("reason") == "already_running":
                self.model.set_recording_state(lv, True, int(result.get("pid") or 0))
                self.install_row_buttons()
                show_status(self, f"監視登録済み、録画中: {lv} / PID {result.get('pid')}", "INFO")
            else:
                show_status(
                    self,
                    f"監視登録済み、録画開始失敗: {lv} / {result.get('reason') or 'unknown'}",
                    "ERROR",
                )
        except Exception:
            detail = traceback.format_exc()
            show_status(self, f"監視登録エラー: {lv}", "ERROR")
            append_app_log(detail, "DEBUG")

    def on_record_clicked(self, lv: str) -> None:
        item = self.model.item_for_lv(lv) or {"lv": lv, "watch_url": f"https://live.nicovideo.jp/watch/{lv}"}
        try:
            if item.get("is_recording"):
                result = tracker.stop_recording_for_broadcast(lv, reason="tracker_button")
                self.model.set_recording_state(lv, False)
                self.install_row_buttons()
                finalize = result.get("finalize") or {}
                if finalize.get("finalized"):
                    suffix = " / 放送終了検知、後処理開始"
                elif finalize.get("reason") == "still_on_air":
                    suffix = " / 配信中なので停止のみ"
                else:
                    suffix = ""
                show_status(
                    self,
                    f"録画停止: {lv} / PID {','.join(str(pid) for pid in result.get('killed_pids') or []) or 'none'}{suffix}",
                    "INFO",
                )
                return
            with tracker.connect() as conn:
                result = tracker.start_recording_for_broadcast(conn, item, self.config)
                conn.commit()
            if result.get("started"):
                self.model.set_recording_state(lv, True, int(result.get("pid") or 0))
                self.install_row_buttons()
                show_status(self, f"録画開始: {lv} / PID {result.get('pid')}", "INFO")
            elif result.get("reason") == "already_running":
                self.model.set_recording_state(lv, True, int(result.get("pid") or 0))
                self.install_row_buttons()
                show_status(self, f"録画スキップ: {lv} / 既に起動中 PID {result.get('pid')}", "INFO")
            else:
                show_status(self, f"録画開始失敗: {lv} / {result.get('reason') or 'unknown'}", "ERROR")
        except Exception:
            detail = traceback.format_exc()
            show_status(self, f"録画開始エラー: {lv}", "ERROR")
            append_app_log(detail, "DEBUG")

    def wait_for_workers(self, timeout_ms: int = 30000) -> bool:
        return self.thread_pool.waitForDone(timeout_ms)


class SpecialUsersModel(QAbstractTableModel):
    enabled_changed = pyqtSignal(str, bool)
    columns = [
        ("enabled", "有効"),
        ("user_id", "ユーザーID"),
        ("label", "名前"),
        ("note", "メモ"),
        ("updated_at", "更新日時"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._rows: list[dict[str, Any]] = []

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.columns)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None
        key = self.columns[index.column()][0]
        value = self._rows[index.row()].get(key)
        if key == "enabled" and role == Qt.ItemDataRole.CheckStateRole:
            return Qt.CheckState.Checked if value else Qt.CheckState.Unchecked
        if role not in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole):
            return None
        if key == "enabled":
            return ""
        return "" if value is None else str(value)

    def setData(self, index: QModelIndex, value: Any, role: int = Qt.ItemDataRole.EditRole) -> bool:
        if not index.isValid() or self.columns[index.column()][0] != "enabled" or role != Qt.ItemDataRole.CheckStateRole:
            return False
        enabled = value == Qt.CheckState.Checked.value
        self._rows[index.row()]["enabled"] = int(enabled)
        user_id = str(self._rows[index.row()].get("user_id") or "")
        self.dataChanged.emit(index, index, [Qt.ItemDataRole.CheckStateRole])
        if user_id:
            self.enabled_changed.emit(user_id, enabled)
        return True

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if self.columns[index.column()][0] == "enabled":
            flags |= Qt.ItemFlag.ItemIsUserCheckable
        return flags

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal:
            if section < 0 or section >= len(self.columns):
                return None
            return self.columns[section][1]
        return section + 1

    def update_rows(self, rows: list[dict[str, Any]]) -> None:
        self.beginResetModel()
        self._rows = rows
        self.endResetModel()

    def user_id_at(self, row: int) -> str | None:
        if row < 0 or row >= len(self._rows):
            return None
        value = self._rows[row].get("user_id")
        return str(value) if value else None


class SpecialUsersTab(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.model = SpecialUsersModel()
        self.model.enabled_changed.connect(self.on_enabled_changed)
        self.pending_name_fetches: set[str] = set()

        self.user_id_input = QLineEdit()
        self.user_id_input.setPlaceholderText("スペシャルユーザーID または user URL")
        self.label_input = QLineEdit()
        self.label_input.setPlaceholderText("名前。空なら数値IDから自動取得")
        self.note_input = QLineEdit()
        self.note_input.setPlaceholderText("メモ")
        self.add_button = QPushButton("登録/更新")
        self.add_button.clicked.connect(self.save_user)
        self.delete_button = QPushButton("選択削除")
        self.delete_button.clicked.connect(self.delete_selected)

        form = QWidget()
        form_layout = QHBoxLayout(form)
        form_layout.setContentsMargins(0, 0, 0, 0)
        form_layout.addWidget(self.user_id_input, 2)
        form_layout.addWidget(self.label_input, 2)
        form_layout.addWidget(self.note_input, 3)
        form_layout.addWidget(self.add_button)
        form_layout.addWidget(self.delete_button)

        self.table = QTableView()
        self.table.setModel(self.model)
        stabilize_table_scroll(self.table)
        self.table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableView.SelectionMode.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        configure_table_header(self.table, [60, 180, 180, 360])
        self.table.doubleClicked.connect(self.open_editor)

        layout = QVBoxLayout(self)
        layout.addWidget(form)
        layout.addWidget(self.table, 1)
        self.reload()

    def reload(self) -> None:
        items = tracker.list_special_users()
        self.model.update_rows(items)
        self.fetch_missing_names(items)

    def on_enabled_changed(self, user_id: str, enabled: bool) -> None:
        tracker.update_special_user_enabled(user_id, enabled)
        show_status(self, f"スペシャルユーザー有効切替: {user_id} = {int(enabled)}")

    def save_user(self) -> None:
        user_id = extract_user_id(self.user_id_input.text())
        if not user_id:
            show_status(self, "登録できない: ユーザーID または user URL を入力してくれ")
            return
        label = self.label_input.text().strip()
        note = self.note_input.text().strip()
        save_special_user(user_id=user_id, label=label, note=note)
        self.user_id_input.clear()
        self.label_input.clear()
        self.note_input.clear()
        self.reload()
        show_status(self, f"スペシャルユーザー登録: {user_id}")
        if not label and self.should_fetch_user_name(user_id):
            self.fetch_user_name(user_id)

    def delete_selected(self) -> None:
        index = self.table.currentIndex()
        user_id = self.model.user_id_at(index.row())
        if not user_id:
            return
        with tracker.connect() as conn:
            conn.execute("DELETE FROM special_user_broadcasters WHERE user_id = ?", (user_id,))
            conn.execute("DELETE FROM special_user_triggers WHERE user_id = ?", (user_id,))
            conn.execute("DELETE FROM special_users WHERE user_id = ?", (user_id,))
            conn.commit()
        self.reload()
        show_status(self, f"スペシャルユーザー削除: {user_id}")

    def open_editor(self, index: QModelIndex) -> None:
        user_id = self.model.user_id_at(index.row())
        if not user_id:
            return
        dialog = SpecialUserEditorDialog(user_id, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.reload()
            show_status(self, f"スペシャルユーザー編集: {user_id}")

    def fetch_missing_names(self, rows: list[dict[str, Any]]) -> None:
        for row in rows:
            user_id = str(row.get("user_id") or "").strip()
            label = str(row.get("label") or "").strip()
            if not label and self.should_fetch_user_name(user_id):
                self.fetch_user_name(user_id)

    def should_fetch_user_name(self, user_id: str) -> bool:
        return user_id.isdigit()

    def fetch_user_name(self, user_id: str) -> None:
        if user_id in self.pending_name_fetches:
            return
        self.pending_name_fetches.add(user_id)
        job = NicovideoUserNameFetchJob(user_id)
        job.signals.finished.connect(self.on_user_name_fetched)
        job.signals.failed.connect(self.on_user_name_failed)
        QThreadPool.globalInstance().start(job)

    def on_user_name_fetched(self, user_id: str, name: str) -> None:
        self.pending_name_fetches.discard(user_id)
        with tracker.connect() as conn:
            conn.execute(
                """
                UPDATE special_users
                SET label = ?, updated_at = ?
                WHERE user_id = ? AND (label IS NULL OR label = '')
                """,
                (name, tracker.now(), user_id),
            )
            conn.commit()
        self.reload()
        show_status(self, f"スペシャルユーザー名取得: {user_id} / {name}")

    def on_user_name_failed(self, user_id: str, error: str) -> None:
        self.pending_name_fetches.discard(user_id)
        show_status(self, f"スペシャルユーザー名取得失敗 {user_id}: {error}")


class TriggerTableModel(QAbstractTableModel):
    columns = [
        ("keyword", "トリガーワード"),
        ("action_type", "アクション"),
        ("action_payload", "内容"),
        ("enabled", "有効"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._rows: list[dict[str, Any]] = []

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.columns)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid() or role not in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole):
            return None
        value = self._rows[index.row()].get(self.columns[index.column()][0])
        if self.columns[index.column()][0] == "enabled":
            return "ON" if value else "OFF"
        return "" if value is None else str(value)

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal:
            return self.columns[section][1]
        return section + 1

    def update_rows(self, rows: list[dict[str, Any]]) -> None:
        self.beginResetModel()
        self._rows = rows
        self.endResetModel()

    def row_at(self, row: int) -> dict[str, Any] | None:
        if row < 0 or row >= len(self._rows):
            return None
        return self._rows[row]


class BroadcasterTriggerTableModel(QAbstractTableModel):
    columns = [
        ("enabled", "有効"),
        ("trigger_name", "トリガー名"),
        ("keyword", "キーワード"),
        ("action_type", "応答"),
        ("action_payload", "内容"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._rows: list[dict[str, Any]] = []

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.columns)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None
        key = self.columns[index.column()][0]
        value = self._rows[index.row()].get(key)
        if key == "enabled" and role == Qt.ItemDataRole.CheckStateRole:
            return Qt.CheckState.Checked if value else Qt.CheckState.Unchecked
        if role not in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole):
            return None
        if key == "enabled":
            return ""
        if key == "action_type":
            return "AI生成" if value == "ai" else "定型"
        return "" if value is None else str(value)

    def setData(self, index: QModelIndex, value: Any, role: int = Qt.ItemDataRole.EditRole) -> bool:
        if not index.isValid() or self.columns[index.column()][0] != "enabled" or role != Qt.ItemDataRole.CheckStateRole:
            return False
        self._rows[index.row()]["enabled"] = value == Qt.CheckState.Checked.value
        self.dataChanged.emit(index, index, [Qt.ItemDataRole.CheckStateRole])
        return True

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if self.columns[index.column()][0] == "enabled":
            flags |= Qt.ItemFlag.ItemIsUserCheckable
        return flags

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal:
            return self.columns[section][1]
        return section + 1

    def update_rows(self, rows: list[dict[str, Any]]) -> None:
        self.beginResetModel()
        self._rows = rows
        self.endResetModel()

    def rows(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self._rows]

    def row_at(self, row: int) -> dict[str, Any] | None:
        if row < 0 or row >= len(self._rows):
            return None
        return dict(self._rows[row])

    def add_row(self, values: dict[str, Any]) -> None:
        row = len(self._rows)
        self.beginInsertRows(QModelIndex(), row, row)
        self._rows.append(dict(values))
        self.endInsertRows()

    def update_row(self, row: int, values: dict[str, Any]) -> None:
        if row < 0 or row >= len(self._rows):
            return
        self._rows[row].update(values)
        self.dataChanged.emit(self.index(row, 0), self.index(row, self.columnCount() - 1))

    def delete_row(self, row: int) -> None:
        if row < 0 or row >= len(self._rows):
            return
        self.beginRemoveRows(QModelIndex(), row, row)
        del self._rows[row]
        self.endRemoveRows()


class ServerUploadSettingsDialog(QDialog):
    def __init__(self, values: dict[str, Any], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("サーバー設定")
        self.resize(560, 300)
        self.html_upload_enabled = QCheckBox("HTMLサーバー送信を有効にする")
        self.html_upload_enabled.setChecked(bool(values.get("html_upload_enabled", 0)))
        self.post_server_url = QLineEdit(str(values.get("post_server_url") or ""))
        self.post_server_url.setPlaceholderText("HTTP API URL または ローカル/共有フォルダ")
        self.post_server_api_key = QLineEdit(str(values.get("post_server_api_key") or ""))
        self.post_server_api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.html_base_url = QLineEdit(str(values.get("html_base_url") or ""))
        self.html_base_url.setPlaceholderText("公開URLのベース。未使用なら空")

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(self.html_upload_enabled)
        layout.addWidget(QLabel("送信先"))
        layout.addWidget(self.post_server_url)
        layout.addWidget(QLabel("APIキー"))
        layout.addWidget(self.post_server_api_key)
        layout.addWidget(QLabel("HTMLベースURL"))
        layout.addWidget(self.html_base_url)
        layout.addStretch(1)
        layout.addWidget(buttons)

    def values(self) -> dict[str, Any]:
        return {
            "html_upload_enabled": int(self.html_upload_enabled.isChecked()),
            "post_server_url": self.post_server_url.text().strip(),
            "post_server_api_key": self.post_server_api_key.text(),
            "html_base_url": self.html_base_url.text().strip(),
        }


class SunoModelFetchSignals(QObject):
    finished = pyqtSignal(list)
    failed = pyqtSignal(str)


class SunoModelFetchJob(QRunnable):
    def __init__(self) -> None:
        super().__init__()
        self.signals = SunoModelFetchSignals()

    def run(self) -> None:
        try:
            self.signals.finished.emit(tracker.fetch_suno_models_from_official_docs())
        except Exception as exc:
            self.signals.failed.emit(str(exc))


class NoWheelComboBox(QComboBox):
    def wheelEvent(self, event) -> None:
        event.ignore()


class NoWheelSpinBox(QSpinBox):
    def wheelEvent(self, event) -> None:
        event.ignore()


class NoWheelDoubleSpinBox(QDoubleSpinBox):
    def wheelEvent(self, event) -> None:
        event.ignore()


class ArchiveTagsTable(QTableWidget):
    def moveCursor(self, cursor_action, modifiers):
        if (
            cursor_action == QAbstractItemView.CursorAction.MoveNext
            and self.currentRow() == self.rowCount() - 1
            and self.currentColumn() == self.columnCount() - 1
        ):
            row = self.rowCount()
            self.insertRow(row)
            for column in range(self.columnCount()):
                self.setItem(row, column, QTableWidgetItem(""))
            QTimer.singleShot(0, lambda: self.editItem(self.item(row, 0)))
            return self.model().index(row, 0)
        return super().moveCursor(cursor_action, modifiers)


class SpecialUserEditorDialog(QDialog):
    ACTIONS = [
        ("none", "なし"),
        ("post_to_broadcast", "見つけた放送へ投稿"),
        ("external_app", "外部アプリ連携"),
    ]

    def __init__(self, user_id: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.user_id = user_id
        self.setWindowTitle(f"スペシャルユーザー編集: {user_id}")
        self.resize(900, 620)
        self.setMinimumSize(520, 360)
        self.broadcaster_model = BroadcasterLinkTableModel()
        self.following_thread: QThread | None = None
        self.following_worker: FollowingFetchWorker | None = None

        self.label_input = QLineEdit()
        self.analysis_model = NoWheelComboBox()
        self.analysis_model.addItems(["openai-gpt4o", "google-gemini-2.5-flash"])
        self.analysis_engine = NoWheelComboBox()
        self.setup_ai_engine_combo(self.analysis_engine, "codex_exec")
        self.analysis_engine.currentIndexChanged.connect(
            lambda: self.refresh_ai_model_combo(self.analysis_engine, self.analysis_model)
        )
        self.analysis_api_key = QLineEdit()
        self.analysis_api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.analysis_effort = NoWheelComboBox()
        self.analysis_effort.addItems(["minimal", "low", "medium", "high", "xhigh", "max", "ultra"])
        self.analysis_effort.setCurrentText("medium")
        self.analysis_session_id = QLineEdit()
        self.analysis_session_id.setPlaceholderText("Codexで育てた分析セッションID（UUID）")

        self.reaction_model = NoWheelComboBox()
        self.reaction_model.addItems(["openai-gpt4o", "google-gemini-2.5-flash"])
        self.reaction_engine = NoWheelComboBox()
        self.setup_ai_engine_combo(self.reaction_engine, "codex_exec")
        self.reaction_engine.currentIndexChanged.connect(
            lambda: self.refresh_ai_model_combo(self.reaction_engine, self.reaction_model)
        )
        self.reaction_api_key = QLineEdit()
        self.reaction_api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.reaction_effort = NoWheelComboBox()
        self.reaction_effort.addItems(["minimal", "low", "medium", "high", "xhigh", "max", "ultra"])
        self.reaction_effort.setCurrentText("medium")
        self.reaction_session_id = QLineEdit()
        self.reaction_session_id.setPlaceholderText("Codexで育てたセッションID（UUID）")
        self.reaction_max_chars = NoWheelSpinBox()
        self.reaction_max_chars.setRange(1, 2000)
        self.reaction_split_delay = NoWheelDoubleSpinBox()
        self.reaction_split_delay.setRange(0, 999)
        self.reaction_split_delay.setDecimals(1)
        self.reaction_delay_seconds = NoWheelDoubleSpinBox()
        self.reaction_delay_seconds.setRange(0, 999)
        self.reaction_delay_seconds.setDecimals(1)
        self.max_reactions = NoWheelSpinBox()
        self.max_reactions.setRange(1, 999)
        self.max_reactions.setValue(1)
        self.refresh_ai_model_combo(self.analysis_engine, self.analysis_model)
        self.refresh_ai_model_combo(self.reaction_engine, self.reaction_model)

        self.basic_reaction_enabled = QCheckBox("基本反応設定を使う")
        self.basic_reaction_type = NoWheelComboBox()
        self.basic_reaction_type.addItem("定型メッセージ", "fixed")
        self.basic_reaction_type.addItem("AI生成", "ai")
        self.basic_reaction_messages = QTextEdit()
        self.basic_reaction_messages.setPlaceholderText("定型メッセージ。1行1メッセージ")
        self.basic_reaction_prompt = QTextEdit()
        self.basic_reaction_prompt.setMinimumHeight(120)
        self.basic_reaction_prompt.setPlainText(tracker.DEFAULT_AI_REACTION_PROMPT)
        self.reaction_skip_prompt = QTextEdit()
        self.reaction_skip_prompt.setMinimumHeight(90)
        self.reaction_skip_prompt.setPlainText(tracker.DEFAULT_AI_REACTION_SKIP_PROMPT)
        self.basic_reaction_prompt.setPlaceholderText("AI反応プロンプト")
        self.server_settings: dict[str, Any] = {
            "html_upload_enabled": 0,
            "post_server_url": "",
            "post_server_api_key": "",
            "html_base_url": "",
        }
        self.server_settings_button = QPushButton("サーバー設定")
        self.server_settings_button.clicked.connect(self.open_server_settings)

        self.broadcaster_id_input = QLineEdit()
        self.broadcaster_id_input.setPlaceholderText("配信者ID / user URL / ch URL")
        self.broadcaster_id_input.setMinimumWidth(240)
        self.broadcaster_name_input = QLineEdit()
        self.broadcaster_name_input.setPlaceholderText("配信者名。空なら自動取得")
        self.broadcaster_name_input.setMinimumWidth(180)
        self.fetch_broadcaster_name_button = QPushButton("名前取得")
        self.fetch_broadcaster_name_button.clicked.connect(self.fetch_broadcaster_name)
        self.broadcaster_id_input.editingFinished.connect(self.fetch_broadcaster_name)
        self.add_broadcaster_button = QPushButton("配信者追加")
        self.add_broadcaster_button.clicked.connect(self.add_broadcaster)
        self.delete_broadcaster_button = QPushButton("選択削除")
        self.delete_broadcaster_button.clicked.connect(self.delete_broadcaster)
        self.import_following_button = QPushButton("フォロー中を一括登録")
        self.import_following_button.clicked.connect(self.import_following_broadcasters)

        self.broadcaster_table = QTableView()
        self.broadcaster_table.setModel(self.broadcaster_model)
        stabilize_table_scroll(self.broadcaster_table)
        setattr(self.broadcaster_table, "_broadcaster_timeshift_enabled", True)
        self.broadcaster_table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.broadcaster_table.setSelectionMode(QTableView.SelectionMode.SingleSelection)
        self.broadcaster_table.setAlternatingRowColors(True)
        self.broadcaster_table.verticalHeader().setVisible(False)
        configure_table_header(self.broadcaster_table, [60, 120])
        self.broadcaster_table.doubleClicked.connect(self.open_broadcaster_editor)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.save_and_accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"ユーザーID: {user_id}"))

        splitter = QSplitter()
        left = QWidget()
        left_layout = QVBoxLayout(left)
        right = QWidget()
        right_layout = QVBoxLayout(right)

        basic = QGroupBox("基本情報")
        basic_layout = QVBoxLayout(basic)
        basic_layout.addWidget(QLabel("表示名"))
        basic_layout.addWidget(self.label_input)
        basic_layout.addWidget(self.server_settings_button)

        analysis = QGroupBox("AI分析")
        analysis_layout = QVBoxLayout(analysis)
        analysis_layout.addWidget(QLabel("AI分析担当"))
        analysis_layout.addWidget(self.analysis_engine)
        analysis_layout.addWidget(QLabel("AI分析モデル"))
        analysis_layout.addWidget(self.analysis_model)
        analysis_layout.addWidget(QLabel("推論力"))
        analysis_layout.addWidget(self.analysis_effort)
        analysis_layout.addWidget(QLabel("Codex resumeセッションID"))
        analysis_layout.addWidget(self.analysis_session_id)
        analysis_layout.addWidget(QLabel("AI分析APIキー"))
        analysis_layout.addWidget(self.analysis_api_key)

        reaction = QGroupBox("AI反応")
        reaction_layout = QVBoxLayout(reaction)
        reaction_layout.addWidget(QLabel("AI反応担当"))
        reaction_layout.addWidget(self.reaction_engine)
        reaction_layout.addWidget(QLabel("AI反応モデル"))
        reaction_layout.addWidget(self.reaction_model)
        reaction_layout.addWidget(QLabel("推論力"))
        reaction_layout.addWidget(self.reaction_effort)
        reaction_layout.addWidget(QLabel("Codex resumeセッションID"))
        reaction_layout.addWidget(self.reaction_session_id)
        reaction_layout.addWidget(QLabel("AI反応APIキー"))
        reaction_layout.addWidget(self.reaction_api_key)
        reaction_numbers = QWidget()
        reaction_numbers_layout = QHBoxLayout(reaction_numbers)
        reaction_numbers_layout.setContentsMargins(0, 0, 0, 0)
        reaction_numbers_layout.addWidget(QLabel("最大文字数"))
        reaction_numbers_layout.addWidget(self.reaction_max_chars)
        reaction_numbers_layout.addWidget(QLabel("分割送信遅延"))
        reaction_numbers_layout.addWidget(self.reaction_split_delay)
        reaction_numbers_layout.addWidget(QLabel("反応遅延"))
        reaction_numbers_layout.addWidget(self.reaction_delay_seconds)
        reaction_numbers_layout.addWidget(QLabel("最大反応回数"))
        reaction_numbers_layout.addWidget(self.max_reactions)
        reaction_layout.addWidget(reaction_numbers)

        basic_reaction = QGroupBox("このユーザーに対する基本反応設定")
        basic_reaction_layout = QVBoxLayout(basic_reaction)
        basic_reaction_layout.addWidget(self.basic_reaction_enabled)
        basic_reaction_layout.addWidget(QLabel("応答タイプ"))
        basic_reaction_layout.addWidget(self.basic_reaction_type)
        basic_reaction_layout.addWidget(QLabel("定型メッセージ"))
        basic_reaction_layout.addWidget(self.basic_reaction_messages, 1)
        basic_reaction_layout.addWidget(QLabel("AIプロンプト"))
        basic_reaction_layout.addWidget(self.basic_reaction_prompt)
        basic_reaction_layout.addWidget(QLabel("無反応判断プロンプ"))
        basic_reaction_layout.addWidget(self.reaction_skip_prompt)

        left_layout.addWidget(basic)
        left_layout.addWidget(analysis)
        left_layout.addWidget(reaction)
        left_layout.addWidget(basic_reaction, 1)

        broadcaster_form = QWidget()
        broadcaster_form_layout = QVBoxLayout(broadcaster_form)
        broadcaster_form_layout.setContentsMargins(0, 0, 0, 0)
        broadcaster_form_layout.setSpacing(6)
        broadcaster_inputs = QWidget()
        broadcaster_inputs_layout = QHBoxLayout(broadcaster_inputs)
        broadcaster_inputs_layout.setContentsMargins(0, 0, 0, 0)
        broadcaster_inputs_layout.addWidget(self.broadcaster_id_input, 3)
        broadcaster_inputs_layout.addWidget(self.broadcaster_name_input, 2)
        broadcaster_buttons = QWidget()
        broadcaster_buttons_layout = QHBoxLayout(broadcaster_buttons)
        broadcaster_buttons_layout.setContentsMargins(0, 0, 0, 0)
        broadcaster_buttons_layout.addWidget(self.fetch_broadcaster_name_button)
        broadcaster_buttons_layout.addWidget(self.add_broadcaster_button)
        broadcaster_buttons_layout.addWidget(self.delete_broadcaster_button)
        broadcaster_buttons_layout.addStretch(1)
        broadcaster_buttons_layout.addWidget(self.import_following_button)
        broadcaster_form_layout.addWidget(broadcaster_inputs)
        broadcaster_form_layout.addWidget(broadcaster_buttons)
        right_layout.addWidget(QLabel("紐づいている配信者一覧"))
        right_layout.addWidget(broadcaster_form)
        right_layout.addWidget(self.broadcaster_table, 1)

        left_scroll = QScrollArea()
        left_scroll.setWidgetResizable(True)
        left_scroll.setFrameShape(QFrame.Shape.NoFrame)
        left_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        left_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        left_scroll.setWidget(left)
        splitter.addWidget(left_scroll)
        splitter.addWidget(right)
        splitter.setSizes([620, 420])
        layout.addWidget(splitter, 1)
        layout.addWidget(buttons)
        self.load()

    def load(self) -> None:
        with tracker.connect() as conn:
            user = conn.execute(
                """
                SELECT user_id, label, analysis_model, analysis_api_key,
                       analysis_engine, analysis_use_codex, analysis_effort, analysis_session_id,
                       reaction_model, reaction_api_key, reaction_engine, reaction_use_codex, reaction_effort, reaction_session_id, reaction_skip_prompt, reaction_max_chars,
                       reaction_split_delay, reaction_delay_seconds, max_reactions,
                       basic_reaction_enabled, basic_reaction_type,
                       basic_reaction_messages, basic_reaction_prompt,
                       post_server_url, post_server_api_key,
                       html_upload_enabled, html_base_url
                FROM special_users
                WHERE user_id = ?
                """,
                (self.user_id,),
            ).fetchone()
            broadcasters = conn.execute(
                """
                SELECT id, broadcaster_id, broadcaster_name, enabled,
                       basic_reaction_enabled, basic_reaction_type,
                       basic_reaction_messages, basic_reaction_prompt,
                       max_reactions, reaction_delay_seconds
                FROM special_user_broadcasters
                WHERE user_id = ?
                ORDER BY broadcaster_name, broadcaster_id
                """,
                (self.user_id,),
            ).fetchall()
        if user:
            self.label_input.setText(user["label"] or "")
            self.set_combo_value(self.analysis_engine, user["analysis_engine"] or ("codex_exec" if int(user["analysis_use_codex"] or 0) else "openai"))
            self.refresh_ai_model_combo(self.analysis_engine, self.analysis_model)
            self.set_combo_text(self.analysis_model, user["analysis_model"] or "openai-gpt4o")
            self.set_combo_text(self.analysis_effort, user["analysis_effort"] or "medium")
            self.analysis_session_id.setText(user["analysis_session_id"] or "")
            self.analysis_api_key.setText(user["analysis_api_key"] or "")
            self.set_combo_value(self.reaction_engine, user["reaction_engine"] or ("codex_exec" if int(user["reaction_use_codex"] or 0) else "openai"))
            self.refresh_ai_model_combo(self.reaction_engine, self.reaction_model)
            self.set_combo_text(self.reaction_model, user["reaction_model"] or "openai-gpt4o")
            self.set_combo_text(self.reaction_effort, user["reaction_effort"] or "medium")
            self.reaction_session_id.setText(user["reaction_session_id"] or "")
            self.reaction_api_key.setText(user["reaction_api_key"] or "")
            self.reaction_max_chars.setValue(int(user["reaction_max_chars"] or 100))
            self.reaction_split_delay.setValue(float(user["reaction_split_delay"] or 1.0))
            self.reaction_delay_seconds.setValue(float(user["reaction_delay_seconds"] or 0.0))
            self.max_reactions.setValue(int(user["max_reactions"] or 1))
            self.basic_reaction_enabled.setChecked(bool(user["basic_reaction_enabled"]))
            self.set_combo_value(self.basic_reaction_type, user["basic_reaction_type"] or "fixed")
            self.basic_reaction_messages.setPlainText(user["basic_reaction_messages"] or "")
            self.basic_reaction_prompt.setPlainText(
                user["basic_reaction_prompt"] or tracker.DEFAULT_AI_REACTION_PROMPT
            )
            self.reaction_skip_prompt.setPlainText(
                user["reaction_skip_prompt"] or tracker.DEFAULT_AI_REACTION_SKIP_PROMPT
            )
            self.server_settings = {
                "post_server_url": user["post_server_url"] or "",
                "post_server_api_key": user["post_server_api_key"] or "",
                "html_upload_enabled": int(user["html_upload_enabled"] or 0),
                "html_base_url": user["html_base_url"] or "",
            }
            self.update_server_settings_button_text()
        self.broadcaster_model.update_rows([dict(row) for row in broadcasters])

    def update_server_settings_button_text(self) -> None:
        suffix = "ON" if int(self.server_settings.get("html_upload_enabled") or 0) else "OFF"
        self.server_settings_button.setText(f"サーバー設定 ({suffix})")

    def open_server_settings(self) -> None:
        dialog = ServerUploadSettingsDialog(self.server_settings, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.server_settings = dialog.values()
            self.update_server_settings_button_text()

    def add_trigger(self) -> None:
        keyword = self.keyword_input.text().strip()
        if not keyword:
            return
        action_type = str(self.trigger_action.currentData())
        payload = self.trigger_payload.text().strip()
        current_time = tracker.now()
        with tracker.connect() as conn:
            conn.execute(
                """
                INSERT INTO special_user_triggers
                    (user_id, keyword, action_type, action_payload, enabled, created_at, updated_at)
                VALUES (?, ?, ?, ?, 1, ?, ?)
                """,
                (self.user_id, keyword, action_type, payload, current_time, current_time),
            )
            conn.commit()
        self.keyword_input.clear()
        self.trigger_payload.clear()
        self.load()

    def delete_trigger(self) -> None:
        row = self.trigger_model.row_at(self.trigger_table.currentIndex().row())
        if not row:
            return
        with tracker.connect() as conn:
            conn.execute("DELETE FROM special_user_triggers WHERE id = ?", (row["id"],))
            conn.commit()
        self.load()

    def add_broadcaster(self) -> None:
        original_value = self.broadcaster_id_input.text().strip()
        broadcaster_id = extract_user_id(original_value) or original_value
        if not broadcaster_id:
            return
        broadcaster_name = self.broadcaster_name_input.text().strip()
        if original_value and (extract_channel_slug(original_value) or broadcaster_id.startswith("ch")) and not broadcaster_name:
            try:
                channel = fetch_niconico_channel_info(original_value)
                broadcaster_id = channel["id"]
                broadcaster_name = channel["name"]
            except Exception as exc:
                show_status(self, f"チャンネル名取得失敗: {exc}")
        broadcaster_name = broadcaster_name or broadcaster_id
        self.broadcaster_model.add_row(broadcaster_id, broadcaster_name)
        self.broadcaster_id_input.clear()
        self.broadcaster_name_input.clear()

    def delete_broadcaster(self) -> None:
        self.broadcaster_model.delete_row(self.broadcaster_table.currentIndex().row())

    def import_following_broadcasters(self) -> None:
        if not self.user_id.isdigit():
            show_status(self, "数値ユーザーIDだけフォロー中一括登録できる")
            return
        self.import_following_button.setEnabled(False)
        self.import_following_button.setText("取得中")
        show_status(self, f"フォロー中取得開始: {self.user_id}")
        self.following_worker = FollowingFetchWorker(self.user_id)
        self.following_thread = QThread(self)
        self.following_worker.moveToThread(self.following_thread)
        self.following_thread.started.connect(self.following_worker.run)
        self.following_worker.finished.connect(self.on_following_broadcasters_fetched)
        self.following_worker.failed.connect(self.on_following_broadcasters_failed)
        self.following_worker.finished.connect(self.following_thread.quit)
        self.following_worker.failed.connect(self.following_thread.quit)
        self.following_thread.finished.connect(self.following_worker.deleteLater)
        self.following_thread.finished.connect(self.on_following_thread_finished)
        self.following_thread.start()

    def on_following_thread_finished(self) -> None:
        self.following_worker = None
        self.following_thread = None

    def on_following_broadcasters_fetched(self, rows: object) -> None:
        try:
            following = [dict(row) for row in rows] if isinstance(rows, list) else []
            result = tracker.bulk_link_special_user_broadcasters(self.user_id, following)
            self.load()
            show_status(self, 
                f"フォロー中一括登録: 新規 {result['inserted']} / 更新 {result['updated']} / 取得 {result['total']}"
            )
        except Exception:
            show_status(self, f"フォロー中一括登録エラー: {traceback.format_exc().splitlines()[-1]}")
        finally:
            self.import_following_button.setEnabled(True)
            self.import_following_button.setText("フォロー中を一括登録")

    def on_following_broadcasters_failed(self, detail: str) -> None:
        self.import_following_button.setEnabled(True)
        self.import_following_button.setText("フォロー中を一括登録")
        last_line = next((line for line in reversed(detail.splitlines()) if line.strip()), "取得失敗")
        show_status(self, f"フォロー中取得失敗: {last_line}")

    def open_broadcaster_editor(self, index: QModelIndex) -> None:
        row_index = index.row()
        row = self.broadcaster_model.row_at(row_index)
        if not row:
            return
        dialog = BroadcasterEditorDialog(self.user_id, row, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            updated = dialog.values()
            self.broadcaster_model.update_row(row_index, updated)

    def fetch_broadcaster_name(self) -> None:
        original_value = self.broadcaster_id_input.text().strip()
        broadcaster_id = extract_user_id(original_value) or original_value
        if not broadcaster_id:
            return
        if self.broadcaster_name_input.text().strip():
            return
        self.fetch_broadcaster_name_button.setEnabled(False)
        self.fetch_broadcaster_name_button.setText("取得中")
        job = NicovideoUserNameFetchJob(broadcaster_id, original_value)
        job.signals.finished.connect(self.on_broadcaster_name_fetched)
        job.signals.failed.connect(self.on_broadcaster_name_failed)
        QThreadPool.globalInstance().start(job)

    def on_broadcaster_name_fetched(self, user_id: str, name: str) -> None:
        current_id = extract_user_id(self.broadcaster_id_input.text()) or self.broadcaster_id_input.text().strip()
        if not self.broadcaster_id_input.text().strip().startswith(user_id):
            self.broadcaster_id_input.setText(user_id)
        if current_id in {user_id, self.broadcaster_id_input.text().strip()} and not self.broadcaster_name_input.text().strip():
            self.broadcaster_name_input.setText(name)
        self.fetch_broadcaster_name_button.setEnabled(True)
        self.fetch_broadcaster_name_button.setText("名前取得")

    def on_broadcaster_name_failed(self, user_id: str, error: str) -> None:
        self.fetch_broadcaster_name_button.setEnabled(True)
        self.fetch_broadcaster_name_button.setText("名前取得")
        show_status(self, f"配信者名取得失敗 {user_id}: {error}")

    def save_and_accept(self) -> None:
        current_time = tracker.now()
        with tracker.connect() as conn:
            conn.execute(
                """
                UPDATE special_users
                SET label = ?,
                    analysis_model = ?,
                    analysis_api_key = ?,
                    analysis_engine = ?,
                    analysis_use_codex = ?,
                    analysis_effort = ?,
                    analysis_session_id = ?,
                    reaction_model = ?,
                    reaction_api_key = ?,
                    reaction_engine = ?,
                    reaction_use_codex = ?,
                    reaction_effort = ?,
                    reaction_session_id = ?,
                    reaction_skip_prompt = ?,
                    reaction_max_chars = ?,
                    reaction_split_delay = ?,
                    reaction_delay_seconds = ?,
                    max_reactions = ?,
                    basic_reaction_enabled = ?,
                    basic_reaction_type = ?,
                    basic_reaction_messages = ?,
                    basic_reaction_prompt = ?,
                    post_server_url = ?,
                    post_server_api_key = ?,
                    html_upload_enabled = ?,
                    html_base_url = ?,
                    updated_at = ?
                WHERE user_id = ?
                """,
                (
                    self.label_input.text().strip(),
                    self.analysis_model.currentText(),
                    self.analysis_api_key.text(),
                    str(self.analysis_engine.currentData()),
                    int(str(self.analysis_engine.currentData()) == "codex_exec"),
                    self.analysis_effort.currentText(),
                    self.analysis_session_id.text().strip(),
                    self.reaction_model.currentText(),
                    self.reaction_api_key.text(),
                    str(self.reaction_engine.currentData()),
                    int(str(self.reaction_engine.currentData()) == "codex_exec"),
                    self.reaction_effort.currentText(),
                    self.reaction_session_id.text().strip(),
                    self.reaction_skip_prompt.toPlainText().strip(),
                    self.reaction_max_chars.value(),
                    self.reaction_split_delay.value(),
                    self.reaction_delay_seconds.value(),
                    self.max_reactions.value(),
                    int(self.basic_reaction_enabled.isChecked()),
                    str(self.basic_reaction_type.currentData()),
                    self.basic_reaction_messages.toPlainText().strip(),
                    self.basic_reaction_prompt.toPlainText().strip(),
                    str(self.server_settings.get("post_server_url") or ""),
                    str(self.server_settings.get("post_server_api_key") or ""),
                    int(self.server_settings.get("html_upload_enabled") or 0),
                    str(self.server_settings.get("html_base_url") or ""),
                    current_time,
                    self.user_id,
                ),
            )
            conn.execute("DELETE FROM special_user_broadcasters WHERE user_id = ?", (self.user_id,))
            for row in self.broadcaster_model.rows():
                conn.execute(
                    """
                    INSERT INTO special_user_broadcasters
                        (user_id, broadcaster_id, broadcaster_name, enabled,
                         basic_reaction_enabled, basic_reaction_type,
                         basic_reaction_messages, basic_reaction_prompt,
                         max_reactions, reaction_delay_seconds,
                         created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(user_id, broadcaster_id) DO UPDATE SET
                        broadcaster_name = excluded.broadcaster_name,
                        enabled = excluded.enabled,
                        basic_reaction_enabled = excluded.basic_reaction_enabled,
                        basic_reaction_type = excluded.basic_reaction_type,
                        basic_reaction_messages = excluded.basic_reaction_messages,
                        basic_reaction_prompt = excluded.basic_reaction_prompt,
                        max_reactions = excluded.max_reactions,
                        reaction_delay_seconds = excluded.reaction_delay_seconds,
                        updated_at = excluded.updated_at
                    """,
                    (
                        self.user_id,
                        row.get("broadcaster_id"),
                        row.get("broadcaster_name"),
                        int(bool(row.get("enabled"))),
                        int(bool(row.get("basic_reaction_enabled", 0))),
                        row.get("basic_reaction_type") or "fixed",
                        row.get("basic_reaction_messages"),
                        row.get("basic_reaction_prompt"),
                        int(row.get("max_reactions") or 1),
                        float(row.get("reaction_delay_seconds") or 0.0),
                        current_time,
                        current_time,
                    ),
                )
            conn.commit()
        self.accept()

    def set_combo_value(self, combo: QComboBox, value: str) -> None:
        index = combo.findData(value)
        combo.setCurrentIndex(index if index >= 0 else 0)

    def setup_ai_engine_combo(self, combo: QComboBox, value: str) -> None:
        combo.addItem("Codex exec", "codex_exec")
        combo.addItem("ClaudeCode", "claude")
        combo.addItem("Grok build", "grok")
        combo.addItem("OpenAI API", "openai")
        combo.addItem("Gemini API", "gemini")
        self.set_combo_value(combo, value)

    def refresh_ai_model_combo(self, engine_combo: QComboBox, model_combo: QComboBox) -> None:
        current = model_combo.currentText().strip()
        engine = str(engine_combo.currentData() or "")
        models: list[str]
        if engine == "codex_exec":
            models = []
            cache_path = Path(
                os.environ.get("CODEX_HOME")
                or (Path.home() / ".codex")
            ) / "models_cache.json"
            try:
                payload = json.loads(cache_path.read_text(encoding="utf-8"))
                models = [
                    str(row.get("slug") or "").strip()
                    for row in payload.get("models", [])
                    if isinstance(row, dict)
                    and str(row.get("slug") or "").strip()
                    and str(row.get("visibility") or "list") != "hide"
                ]
            except Exception as exc:
                append_app_log(f"Codexモデル一覧読込失敗: {type(exc).__name__}: {exc}", "WARN")
            if not models:
                models = ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5"]
        elif engine == "claude":
            models = ["sonnet", "opus", "haiku"]
        elif engine == "grok":
            models = ["grok-build"]
        elif engine == "gemini":
            models = ["google-gemini-2.5-flash"]
        else:
            models = ["openai-gpt4o"]
        model_combo.blockSignals(True)
        model_combo.clear()
        model_combo.addItems(models)
        model_combo.setEditable(True)
        if current and current in models:
            model_combo.setCurrentText(current)
        else:
            model_combo.setCurrentIndex(0)
        model_combo.blockSignals(False)

    def set_combo_text(self, combo: QComboBox, value: str) -> None:
        index = combo.findText(value)
        if index >= 0:
            combo.setCurrentIndex(index)
        elif combo.isEditable() and value:
            combo.setCurrentText(value)
        else:
            combo.setCurrentIndex(0)


class BroadcasterLinkTableModel(QAbstractTableModel):
    columns = [
        ("enabled", "有効"),
        ("broadcaster_id", "配信者ID"),
        ("broadcaster_name", "配信者名"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._rows: list[dict[str, Any]] = []

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.columns)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None
        key = self.columns[index.column()][0]
        value = self._rows[index.row()].get(key)
        if key == "enabled" and role == Qt.ItemDataRole.CheckStateRole:
            return Qt.CheckState.Checked if value else Qt.CheckState.Unchecked
        if role not in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole):
            return None
        if key == "enabled":
            return ""
        return "" if value is None else str(value)

    def setData(self, index: QModelIndex, value: Any, role: int = Qt.ItemDataRole.EditRole) -> bool:
        if not index.isValid() or self.columns[index.column()][0] != "enabled" or role != Qt.ItemDataRole.CheckStateRole:
            return False
        self._rows[index.row()]["enabled"] = value == Qt.CheckState.Checked.value
        self.dataChanged.emit(index, index, [Qt.ItemDataRole.CheckStateRole])
        return True

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if self.columns[index.column()][0] == "enabled":
            flags |= Qt.ItemFlag.ItemIsUserCheckable
        return flags

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal:
            return self.columns[section][1]
        return section + 1

    def update_rows(self, rows: list[dict[str, Any]]) -> None:
        self.beginResetModel()
        self._rows = rows
        self.endResetModel()

    def rows(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self._rows]

    def row_at(self, row: int) -> dict[str, Any] | None:
        if row < 0 or row >= len(self._rows):
            return None
        return dict(self._rows[row])

    def update_row(self, row: int, values: dict[str, Any]) -> None:
        if row < 0 or row >= len(self._rows):
            return
        self._rows[row].update(values)
        self.dataChanged.emit(self.index(row, 0), self.index(row, self.columnCount() - 1))

    def add_row(self, broadcaster_id: str, broadcaster_name: str) -> None:
        row = len(self._rows)
        self.beginInsertRows(QModelIndex(), row, row)
        self._rows.append(
            {
                "broadcaster_id": broadcaster_id,
                "broadcaster_name": broadcaster_name,
                "enabled": 1,
            }
        )
        self.endInsertRows()

    def delete_row(self, row: int) -> None:
        if row < 0 or row >= len(self._rows):
            return
        self.beginRemoveRows(QModelIndex(), row, row)
        del self._rows[row]
        self.endRemoveRows()


class BroadcasterTriggerEditorDialog(QDialog):
    def __init__(self, trigger: dict[str, Any] | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("トリガー編集")
        self.resize(520, 380)
        trigger = trigger or {}

        self.trigger_name = QLineEdit(str(trigger.get("trigger_name") or "新しいトリガー"))
        self.enabled = QCheckBox("有効")
        self.enabled.setChecked(bool(trigger.get("enabled", 1)))
        self.keyword = QTextEdit()
        self.keyword.setPlaceholderText("キーワード。1行1キーワード")
        self.keyword.setPlainText(str(trigger.get("keyword") or ""))
        self.action_type = NoWheelComboBox()
        self.action_type.addItem("定型メッセージ", "fixed")
        self.action_type.addItem("AI生成", "ai")
        self.set_combo_value(self.action_type, str(trigger.get("action_type") or "fixed"))
        self.action_payload = QTextEdit()
        self.action_payload.setPlaceholderText("定型メッセージまたはAIプロンプト")
        self.action_payload.setPlainText(str(trigger.get("action_payload") or ""))

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("トリガー名"))
        layout.addWidget(self.trigger_name)
        layout.addWidget(self.enabled)
        layout.addWidget(QLabel("キーワード"))
        layout.addWidget(self.keyword, 1)
        layout.addWidget(QLabel("応答タイプ"))
        layout.addWidget(self.action_type)
        layout.addWidget(QLabel("応答内容"))
        layout.addWidget(self.action_payload, 1)
        layout.addWidget(buttons)

    def values(self) -> dict[str, Any]:
        return {
            "trigger_name": self.trigger_name.text().strip() or "新しいトリガー",
            "keyword": self.keyword.toPlainText().strip(),
            "action_type": str(self.action_type.currentData()),
            "action_payload": self.action_payload.toPlainText().strip(),
            "enabled": int(self.enabled.isChecked()),
        }

    def set_combo_value(self, combo: QComboBox, value: str) -> None:
        index = combo.findData(value)
        combo.setCurrentIndex(index if index >= 0 else 0)


class BroadcasterEditorDialog(QDialog):
    def __init__(self, user_id: str, broadcaster: dict[str, Any], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("配信者編集")
        self.resize(980, 600)
        self.user_id = user_id
        self.original = dict(broadcaster)
        self.trigger_model = BroadcasterTriggerTableModel()

        self.broadcaster_id = QLineEdit(str(broadcaster.get("broadcaster_id") or ""))
        self.broadcaster_name = QLineEdit(str(broadcaster.get("broadcaster_name") or ""))
        self.enabled = QCheckBox("有効")
        self.enabled.setChecked(bool(broadcaster.get("enabled", 1)))

        self.basic_reaction_enabled = QCheckBox("この配信者向け基本反応を使う")
        self.basic_reaction_enabled.setChecked(bool(broadcaster.get("basic_reaction_enabled", 0)))
        self.basic_reaction_type = NoWheelComboBox()
        self.basic_reaction_type.addItem("定型メッセージ", "fixed")
        self.basic_reaction_type.addItem("AI生成", "ai")
        self.set_combo_value(self.basic_reaction_type, str(broadcaster.get("basic_reaction_type") or "fixed"))
        self.basic_reaction_messages = QTextEdit()
        self.basic_reaction_messages.setPlaceholderText("この配信者向けの定型メッセージ。1行1メッセージ")
        self.basic_reaction_messages.setPlainText(str(broadcaster.get("basic_reaction_messages") or ""))
        self.basic_reaction_prompt = QLineEdit(str(broadcaster.get("basic_reaction_prompt") or ""))
        self.basic_reaction_prompt.setPlaceholderText("この配信者向けAIプロンプト")
        self.max_reactions = NoWheelSpinBox()
        self.max_reactions.setRange(1, 999)
        self.max_reactions.setValue(int(broadcaster.get("max_reactions") or 1))
        self.reaction_delay = NoWheelDoubleSpinBox()
        self.reaction_delay.setRange(0, 999)
        self.reaction_delay.setDecimals(1)
        self.reaction_delay.setValue(float(broadcaster.get("reaction_delay_seconds") or 0.0))

        self.trigger_name_input = QLineEdit()
        self.trigger_name_input.setPlaceholderText("トリガー名")
        self.trigger_keyword_input = QLineEdit()
        self.trigger_keyword_input.setPlaceholderText("キーワード")
        self.trigger_enabled_input = QCheckBox("有効")
        self.trigger_enabled_input.setChecked(True)
        self.trigger_action_input = NoWheelComboBox()
        self.trigger_action_input.addItem("定型", "fixed")
        self.trigger_action_input.addItem("AI", "ai")
        self.trigger_payload_input = QLineEdit()
        self.trigger_payload_input.setPlaceholderText("応答内容")
        self.add_trigger_button = QPushButton("トリガー追加")
        self.add_trigger_button.clicked.connect(self.add_trigger)
        self.delete_trigger_button = QPushButton("選択削除")
        self.delete_trigger_button.clicked.connect(self.delete_trigger)
        self.trigger_table = QTableView()
        self.trigger_table.setModel(self.trigger_model)
        stabilize_table_scroll(self.trigger_table)
        self.trigger_table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.trigger_table.setSelectionMode(QTableView.SelectionMode.SingleSelection)
        self.trigger_table.setAlternatingRowColors(True)
        self.trigger_table.verticalHeader().setVisible(False)
        configure_table_header(self.trigger_table, [55, 120, 150, 70])
        self.trigger_table.doubleClicked.connect(self.edit_trigger)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.save_and_accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        splitter = QSplitter()
        left = QWidget()
        left_layout = QVBoxLayout(left)
        right = QWidget()
        right_layout = QVBoxLayout(right)

        basic = QGroupBox("基本情報")
        basic_layout = QVBoxLayout(basic)
        basic_layout.addWidget(QLabel("配信者ID"))
        basic_layout.addWidget(self.broadcaster_id)
        basic_layout.addWidget(QLabel("配信者名"))
        basic_layout.addWidget(self.broadcaster_name)
        basic_layout.addWidget(self.enabled)

        reaction = QGroupBox("この配信者向け基本反応設定")
        reaction_layout = QVBoxLayout(reaction)
        reaction_layout.addWidget(self.basic_reaction_enabled)
        reaction_layout.addWidget(QLabel("応答タイプ"))
        reaction_layout.addWidget(self.basic_reaction_type)
        reaction_layout.addWidget(QLabel("定型メッセージ"))
        reaction_layout.addWidget(self.basic_reaction_messages, 1)
        reaction_layout.addWidget(QLabel("AIプロンプト"))
        reaction_layout.addWidget(self.basic_reaction_prompt)
        numbers = QWidget()
        numbers_layout = QHBoxLayout(numbers)
        numbers_layout.setContentsMargins(0, 0, 0, 0)
        numbers_layout.addWidget(QLabel("最大反応数"))
        numbers_layout.addWidget(self.max_reactions)
        numbers_layout.addWidget(QLabel("反応遅延"))
        numbers_layout.addWidget(self.reaction_delay)
        numbers_layout.addStretch(1)
        reaction_layout.addWidget(numbers)

        left_layout.addWidget(basic)
        left_layout.addWidget(reaction, 1)

        trigger_form = QWidget()
        trigger_form_layout = QVBoxLayout(trigger_form)
        trigger_form_layout.setContentsMargins(0, 0, 0, 0)
        trigger_form_top = QWidget()
        trigger_form_top_layout = QHBoxLayout(trigger_form_top)
        trigger_form_top_layout.setContentsMargins(0, 0, 0, 0)
        trigger_form_top_layout.addWidget(self.trigger_enabled_input)
        trigger_form_top_layout.addWidget(self.trigger_name_input, 1)
        trigger_form_top_layout.addWidget(self.trigger_keyword_input, 1)
        trigger_form_top_layout.addWidget(self.trigger_action_input)
        trigger_form_top_layout.addWidget(self.add_trigger_button)
        trigger_form_top_layout.addWidget(self.delete_trigger_button)
        trigger_form_layout.addWidget(trigger_form_top)
        trigger_form_layout.addWidget(self.trigger_payload_input)

        right_layout.addWidget(QLabel("この配信者のトリガーワード一覧"))
        right_layout.addWidget(trigger_form)
        right_layout.addWidget(self.trigger_table, 1)

        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setSizes([470, 510])
        layout.addWidget(splitter, 1)
        layout.addWidget(buttons)
        self.load_triggers()

    def values(self) -> dict[str, Any]:
        return {
            "broadcaster_id": self.broadcaster_id.text().strip(),
            "broadcaster_name": self.broadcaster_name.text().strip(),
            "enabled": int(self.enabled.isChecked()),
            "basic_reaction_enabled": int(self.basic_reaction_enabled.isChecked()),
            "basic_reaction_type": str(self.basic_reaction_type.currentData()),
            "basic_reaction_messages": self.basic_reaction_messages.toPlainText().strip(),
            "basic_reaction_prompt": self.basic_reaction_prompt.text().strip(),
            "max_reactions": self.max_reactions.value(),
            "reaction_delay_seconds": self.reaction_delay.value(),
        }

    def load_triggers(self) -> None:
        broadcaster_id = self.broadcaster_id.text().strip()
        if not broadcaster_id:
            self.trigger_model.update_rows([])
            return
        self.trigger_model.update_rows(tracker.list_broadcaster_triggers(self.user_id, broadcaster_id))

    def add_trigger(self) -> None:
        keyword = self.trigger_keyword_input.text().strip()
        if not keyword:
            return
        self.trigger_model.add_row(
            {
                "id": None,
                "user_id": self.user_id,
                "broadcaster_id": self.broadcaster_id.text().strip(),
                "trigger_name": self.trigger_name_input.text().strip() or "新しいトリガー",
                "keyword": keyword,
                "action_type": str(self.trigger_action_input.currentData()),
                "action_payload": self.trigger_payload_input.text().strip(),
                "enabled": int(self.trigger_enabled_input.isChecked()),
            }
        )
        self.trigger_name_input.clear()
        self.trigger_keyword_input.clear()
        self.trigger_payload_input.clear()
        self.trigger_enabled_input.setChecked(True)

    def delete_trigger(self) -> None:
        self.trigger_model.delete_row(self.trigger_table.currentIndex().row())

    def edit_trigger(self, index: QModelIndex) -> None:
        row_index = index.row()
        row = self.trigger_model.row_at(row_index)
        if not row:
            return
        dialog = BroadcasterTriggerEditorDialog(row, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            values = dialog.values()
            if not values["keyword"]:
                return
            self.trigger_model.update_row(row_index, values)

    def save_and_accept(self) -> None:
        try:
            self.save_triggers()
        except Exception as exc:
            self.report_status(f"トリガーワード保存失敗: {exc}")
            return
        self.accept()

    def save_triggers(self) -> None:
        old_broadcaster_id = str(self.original.get("broadcaster_id") or "").strip()
        broadcaster_id = self.broadcaster_id.text().strip()
        if not broadcaster_id:
            return
        tracker.replace_broadcaster_triggers(
            self.user_id,
            broadcaster_id,
            self.trigger_model.rows(),
            old_broadcaster_id=old_broadcaster_id,
        )

    def set_combo_value(self, combo: QComboBox, value: str) -> None:
        index = combo.findData(value)
        combo.setCurrentIndex(index if index >= 0 else 0)

    def report_status(self, message: str) -> None:
        window = self.window()
        if hasattr(window, "statusBar"):
            show_status(self, message)


class BroadcastCommentTab(QWidget):
    close_requested = pyqtSignal(str)

    def __init__(
        self,
        lv: str,
        stream_manager: "CommentStreamManager",
        parent: QWidget | None = None,
        *,
        broadcast_title: str = "",
        broadcaster_name: str = "",
        broadcaster_id: str = "",
        origin_text: str = "",
    ) -> None:
        super().__init__(parent)
        self.lv = lv
        self.stream_manager = stream_manager
        self.broadcast_title = str(broadcast_title or "").strip()
        self.broadcaster_name = str(broadcaster_name or "").strip()
        self.broadcaster_id = str(broadcaster_id or "").strip()
        self.origin_text = str(origin_text or "").strip()
        self.model = CommentTableModel()
        self.comment_keys: set[tuple[str, str, str, str, str]] = set()
        self.reaction_inflight_user_ids: set[str] = set()
        self.reaction_counts: dict[str, int] = {}
        self.profile_requested_user_ids: set[str] = set()
        self.stop_requested_by_user = False
        self.has_received_comment = False
        self.html_created = False
        self.closing = False
        self._close_finished_handler = None
        self.end_check_running = False
        self.end_check_timer = QTimer(self)
        self.end_check_timer.setInterval(60_000)
        self.end_check_timer.timeout.connect(self.request_stream_end_check)

        title = QLabel(f"{lv}")
        title.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.status = QLabel("未接続")
        self.context_title_label = QLabel("")
        self.context_meta_label = QLabel("")
        for label in (self.context_title_label, self.context_meta_label):
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            label.setMinimumWidth(0)
            label.setWordWrap(False)
            label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.context_title_label.setStyleSheet(
            "QLabel { color: #e7eefc; font-weight: 600; font-size: 13px; }"
        )
        self.context_meta_label.setStyleSheet("QLabel { color: #93a3bb; font-size: 11px; }")
        self.monitor_broadcaster_button = QPushButton("この放送者を監視")
        self.monitor_broadcaster_button.clicked.connect(self.monitor_broadcaster)
        self.download_all_button = QPushButton("全部取得")
        self.download_all_button.clicked.connect(self.download_all_comments)
        self.start_button = QPushButton("NDGR接続")
        self.start_button.clicked.connect(self.start_stream)
        self.stop_button = QPushButton("停止")
        self.stop_button.clicked.connect(self.stop_stream)
        self.stop_button.setEnabled(False)

        controls = QWidget()
        controls_layout = QHBoxLayout(controls)
        controls_layout.setContentsMargins(0, 0, 0, 0)
        controls_layout.addWidget(title)
        controls_layout.addWidget(self.status)
        controls_layout.addStretch(1)
        controls_layout.addWidget(self.monitor_broadcaster_button)
        controls_layout.addWidget(self.download_all_button)
        controls_layout.addWidget(self.start_button)
        controls_layout.addWidget(self.stop_button)
        controls.setMinimumWidth(0)
        controls.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)

        self.context_bar = QGroupBox("放送情報")
        context_layout = QVBoxLayout(self.context_bar)
        context_layout.setContentsMargins(8, 4, 8, 4)
        context_layout.setSpacing(2)
        context_layout.addWidget(self.context_title_label)
        context_layout.addWidget(self.context_meta_label)
        self.context_bar.setMinimumWidth(0)
        self.context_bar.setMaximumHeight(72)
        self.context_bar.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.context_bar.setStyleSheet(
            """
            QGroupBox {
                color: #9fb2ce;
                border: 1px solid #48566a;
                border-radius: 4px;
                margin-top: 8px;
                padding-top: 8px;
                background: #202429;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 8px;
                padding: 0 4px;
            }
            """
        )
        self.update_context_label()

        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setItemDelegate(CommentGlowDelegate(self.table))
        stabilize_table_scroll(self.table)
        self.table.setMinimumWidth(0)
        self.table.setSizeAdjustPolicy(QAbstractScrollArea.SizeAdjustPolicy.AdjustIgnored)
        self.table.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableView.SelectionMode.ExtendedSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.setIconSize(QSize(32, 32))
        self.table.verticalHeader().setDefaultSectionSize(38)
        configure_table_header(self.table, [46, 70, 170, 180, 180, 520, 90])

        layout = QVBoxLayout(self)
        layout.addWidget(controls)
        layout.addWidget(self.context_bar)
        layout.addWidget(self.table, 1)
        self.stream_manager.comment_received.connect(self.on_manager_comment_received)
        self.stream_manager.status_changed.connect(self.on_manager_status_changed)
        self.stream_manager.failed.connect(self.on_manager_failed)
        self.stream_manager.finished.connect(self.on_manager_finished)
        self.glow_timer = QTimer(self)
        self.glow_timer.setInterval(140)
        self.glow_timer.timeout.connect(self.model.advance_special_glow)
        self.glow_timer.start()
        self.load_archived_comments()

    def set_context(
        self,
        *,
        broadcast_title: str = "",
        broadcaster_name: str = "",
        broadcaster_id: str = "",
        origin_text: str = "",
    ) -> None:
        if broadcast_title:
            self.broadcast_title = str(broadcast_title).strip()
        if broadcaster_name:
            self.broadcaster_name = str(broadcaster_name).strip()
        if broadcaster_id:
            self.broadcaster_id = str(broadcaster_id).strip()
        if origin_text:
            self.origin_text = str(origin_text).strip()
        self.update_context_label()

    def update_context_label(self) -> None:
        def clean_display_value(value: str) -> str:
            value = re.sub(r"\s+", " ", str(value or "")).strip()
            if re.search(r'\b(?:colspan|rowspan|class|href|data-[\w-]+)\s*=', value, re.IGNORECASE):
                return ""
            if "<" in value or ">" in value:
                return ""
            return value

        def shorten(value: str, limit: int) -> str:
            value = clean_display_value(value)
            if len(value) > limit:
                return f"{value[:limit]}..."
            return value

        clean_title = clean_display_value(self.broadcast_title)
        title_text = shorten(clean_title, 72) if clean_title else "タイトル未取得"
        self.context_title_label.setText(title_text)
        self.context_title_label.setToolTip(clean_title)

        clean_name = clean_display_value(self.broadcaster_name)
        clean_id = clean_display_value(self.broadcaster_id)
        if clean_name and clean_id and clean_name != clean_id:
            broadcaster_text = f"{clean_name} ({clean_id})"
        elif clean_name or clean_id:
            broadcaster_text = clean_name or clean_id
        else:
            broadcaster_text = "放送者未取得"

        clean_origin = clean_display_value(self.origin_text)
        origin_tags = [t.strip() for t in clean_origin.split(" / ") if t.strip()]

        from html import escape as _esc

        meta_html = (
            '<span style="color:#7f8ea3;">放送者</span> '
            f'<span style="color:#cdd9ee;">{_esc(shorten(broadcaster_text, 48))}</span>'
        )
        for tag in origin_tags:
            meta_html += (
                '&#160;&#160;<span style="background:#33405a; color:#c6d2e6;">'
                f'&#160;{_esc(shorten(tag, 40))}&#160;</span>'
            )
        self.context_meta_label.setTextFormat(Qt.TextFormat.RichText)
        self.context_meta_label.setText(meta_html)
        self.context_meta_label.setToolTip(
            " / ".join([broadcaster_text, clean_origin]).strip(" /")
        )

        full_text = "  |  ".join(
            [
                f"タイトル: {clean_title}" if clean_title else "",
                (
                    f"放送者: {broadcaster_text}"
                    if broadcaster_text and broadcaster_text != "放送者未取得"
                    else ""
                ),
                clean_origin,
            ]
        ).strip("  |")
        self.context_bar.setToolTip(full_text)

    def prepare_for_close(self) -> None:
        append_app_log(
            f"コメント監視タブ prepare_for_close: {self.lv} / "
            f"closing={self.closing} / stream_running={self.stream_manager.is_running(self.lv)}",
            "DEBUG",
        )
        self.closing = True
        self.end_check_timer.stop()
        self.end_check_running = False
        if hasattr(self, "glow_timer"):
            self.glow_timer.stop()
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(False)
        self.status.setText("終了中...")

    def detach_stream_signals(self) -> None:
        append_app_log(f"コメント監視タブ signal切断開始: {self.lv}", "DEBUG")
        signal_slots = [
            (self.stream_manager.comment_received, self.on_manager_comment_received),
            (self.stream_manager.status_changed, self.on_manager_status_changed),
            (self.stream_manager.failed, self.on_manager_failed),
            (self.stream_manager.finished, self.on_manager_finished),
        ]
        if self._close_finished_handler is not None:
            signal_slots.append((self.stream_manager.finished, self._close_finished_handler))
        for signal, slot in signal_slots:
            try:
                signal.disconnect(slot)
            except Exception:
                pass
        self._close_finished_handler = None
        append_app_log(f"コメント監視タブ signal切断完了: {self.lv}", "DEBUG")

    def start_stream(self) -> None:
        if self.closing:
            return
        if self.stream_manager.is_running(self.lv):
            return
        self.stop_requested_by_user = False
        self.has_received_comment = False
        self.html_created = False
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.end_check_timer.stop()
        self.end_check_running = False
        self.status.setText("接続中...")
        self.stream_manager.start(self.lv)

    def monitor_broadcaster(self) -> None:
        self.monitor_broadcaster_button.setEnabled(False)
        self.monitor_broadcaster_button.setText("取得中")
        job = BroadcasterInfoFetchJob(self.lv)
        job.signals.finished.connect(self.on_broadcaster_info_fetched)
        job.signals.failed.connect(self.on_broadcaster_info_failed)
        QThreadPool.globalInstance().start(job)

    def download_all_comments(self) -> None:
        if self.closing:
            return
        self.download_all_button.setEnabled(False)
        self.download_all_button.setText("取得中")
        self.status.setText("全コメント取得中")
        job = DownloadAllCommentsJob(self.lv)
        job.signals.finished.connect(self.on_all_comments_downloaded)
        job.signals.failed.connect(self.on_all_comments_download_failed)
        QThreadPool.globalInstance().start(job)

    def on_all_comments_downloaded(self, lv: str, count: int) -> None:
        if lv != self.lv or self.closing:
            return
        self.download_all_button.setEnabled(True)
        self.download_all_button.setText("全部取得")
        self.load_archived_comments()
        self.status.setText(f"全コメント取得 {count}件")
        show_status(self, f"全コメント取得完了: {self.lv} / {count}件")

    def on_all_comments_download_failed(self, lv: str, detail: str) -> None:
        if lv != self.lv or self.closing:
            return
        self.download_all_button.setEnabled(True)
        self.download_all_button.setText("全部取得")
        self.status.setText("全取得失敗")
        last_line = next((line for line in reversed(detail.splitlines()) if line.strip()), "全コメント取得失敗")
        show_status(self, f"全コメント取得失敗 {self.lv}: {last_line}", "ERROR")
        append_app_log(detail, "DEBUG")

    def on_broadcaster_info_fetched(self, page_data: object) -> None:
        self.monitor_broadcaster_button.setEnabled(True)
        self.monitor_broadcaster_button.setText("この放送者を監視")
        broadcaster_id = str(getattr(page_data, "broadcaster_id", "") or "").strip()
        broadcaster_name = str(getattr(page_data, "broadcaster_name", "") or "").strip()
        if not broadcaster_id:
            show_status(self, f"放送者ID取得失敗: {self.lv}")
            return
        tracker.save_monitored_broadcaster(
            broadcaster_id=broadcaster_id,
            broadcaster_name=broadcaster_name,
            source_lv=self.lv,
            enabled=True,
        )
        show_status(self, 
            f"放送者を監視登録: {broadcaster_id} / {broadcaster_name or broadcaster_id}"
        )

    def on_broadcaster_info_failed(self, error: str) -> None:
        self.monitor_broadcaster_button.setEnabled(True)
        self.monitor_broadcaster_button.setText("この放送者を監視")
        show_status(self, f"放送者取得失敗 {self.lv}: {error}")

    def stop_stream(self) -> None:
        self.stop_requested_by_user = True
        self.stream_manager.stop(self.lv)
        self.status.setText("停止要求中...")
        self.stop_button.setEnabled(False)
        job = DisableMonitorForLiveJob(self.lv)
        job.signals.finished.connect(self.on_monitor_disabled)
        job.signals.failed.connect(self.on_monitor_disable_failed)
        QThreadPool.globalInstance().start(job)

    def on_monitor_disabled(self, lv: str, broadcaster_id: str) -> None:
        if lv != self.lv:
            return
        show_status(self, f"配信者監視OFF: {broadcaster_id} / {self.lv}")
        window = self.window()
        if hasattr(window, "reload_broadcaster_monitors"):
            window.reload_broadcaster_monitors()

    def on_monitor_disable_failed(self, lv: str, error: str) -> None:
        if lv != self.lv:
            return
        show_status(self, f"配信者監視OFF失敗: {self.lv} / {error}")

    def on_manager_comment_received(self, lv: str, comment: object) -> None:
        if lv != self.lv:
            return
        self.on_comment_received(comment)

    def on_manager_status_changed(self, lv: str, text: str) -> None:
        if lv != self.lv:
            return
        self.on_status_changed(text)

    def on_manager_failed(self, lv: str, detail: str) -> None:
        if lv != self.lv:
            return
        self.on_failed(detail)

    def on_manager_finished(self, lv: str) -> None:
        if lv != self.lv:
            return
        self.on_stream_finished()

    def on_comment_received(self, comment: object) -> None:
        if self.closing:
            return
        row = dict(comment) if isinstance(comment, dict) else {"text": str(comment)}
        self.has_received_comment = True
        display_row = dict(row)
        try:
            with tracker.connect() as conn:
                archive_row = tracker.save_archive_comment_from_ndgr(conn, self.lv, row)
                comment_user_id = str(archive_row.get("user_id") or row.get("user_id") or "").strip()
                is_special_user = tracker.special_user_exists(conn, comment_user_id)
                display_row = self.display_comment_row(archive_row, is_special_user=is_special_user)
                hit = tracker.record_special_user_broadcast_hit_from_comment(conn, self.lv, archive_row)
                conn.commit()
            if is_special_user:
                display_row["special_user_id"] = comment_user_id
            if hit.get("recorded"):
                show_status(
                    self,
                    f"スペシャルユーザー検出: {self.lv} / {hit.get('user_id')} / {hit.get('broadcaster_id')}",
                )
        except Exception:
            append_app_log(traceback.format_exc(), "DEBUG")
            show_status(self, f"NDGRコメント保存失敗: {self.lv}", "ERROR")
        if self.append_comment_to_table(display_row, keep_bottom=True):
            self.maybe_post_basic_reaction(display_row)

    def display_comment_row(self, row: dict[str, Any], *, is_special_user: bool = False) -> dict[str, Any]:
        display_row = dict(row)
        display_row["received_at"] = str(
            display_row.get("posted_at")
            or display_row.get("received_at")
            or display_row.get("created_at")
            or ""
        )
        display_row["is_special_user"] = bool(is_special_user)
        if is_special_user:
            display_row["special_user_id"] = str(display_row.get("user_id") or "").strip()
        return display_row

    def comment_key(self, row: dict[str, Any]) -> tuple[str, str, str, str, str]:
        return (
            str(row.get("comment_id") or ""),
            str(row.get("no") or ""),
            str(row.get("user_id") or row.get("raw_user_id") or row.get("hashed_user_id") or ""),
            str(row.get("posted_at") or row.get("received_at") or row.get("date") or ""),
            str(row.get("text") or ""),
        )

    def append_comment_to_table(self, row: dict[str, Any], *, keep_bottom: bool = False) -> bool:
        key = self.comment_key(row)
        if key in self.comment_keys:
            return False
        self.comment_keys.add(key)
        horizontal_scroll = self.table.horizontalScrollBar().value()
        at_bottom = self.table.verticalScrollBar().value() >= self.table.verticalScrollBar().maximum() - 2
        self.model.append_comment(row)
        self.request_comment_user_profile(row)
        if keep_bottom and at_bottom:
            self.table.scrollToBottom()
        self.install_comment_button(self.model.rowCount() - 1)
        self.table.horizontalScrollBar().setValue(horizontal_scroll)
        QTimer.singleShot(0, lambda value=horizontal_scroll: self.table.horizontalScrollBar().setValue(value))
        return True

    def request_comment_user_profile(self, row: dict[str, Any]) -> None:
        user_id = str(row.get("user_id") or "").strip()
        if not user_id.isdigit() or user_id in self.profile_requested_user_ids:
            return
        self.profile_requested_user_ids.add(user_id)
        job = NicovideoUserProfileFetchJob(user_id)
        job.signals.finished.connect(self.on_comment_user_profile_fetched)
        job.signals.failed.connect(self.on_comment_user_profile_failed)
        QThreadPool.globalInstance().start(job)

    def on_comment_user_profile_fetched(self, user_id: str, name: str, icon_data: bytes) -> None:
        pixmap = QPixmap()
        if icon_data:
            pixmap.loadFromData(icon_data)
            if not pixmap.isNull():
                pixmap = pixmap.scaled(
                    32,
                    32,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
        self.model.update_user_profile(user_id, name, None if pixmap.isNull() else pixmap)

    def on_comment_user_profile_failed(self, user_id: str, error: str) -> None:
        append_app_log(f"コメントユーザー情報取得失敗: {user_id} / {error}", "DEBUG")

    def load_archived_comments(self) -> None:
        try:
            with tracker.connect() as conn:
                rows = conn.execute(
                    """
                    SELECT id, lv, no, comment_id, user_id, raw_user_id, hashed_user_id,
                           user_name, text, date, posted_at, received_at,
                           vpos, broadcast_seconds, source, created_at
                    FROM archive_comments
                    WHERE lv = ?
                    ORDER BY COALESCE(no, id), id
                    LIMIT 5000
                    """,
                    (self.lv,),
                ).fetchall()
                special_user_ids = {
                    str(row["user_id"] or "")
                    for row in conn.execute("SELECT user_id FROM special_users WHERE enabled = 1").fetchall()
                }
                comments = [
                    self.display_comment_row(dict(row), is_special_user=str(row["user_id"] or "") in special_user_ids)
                    for row in rows
                ]
            loaded = 0
            for row in comments:
                if self.append_comment_to_table(row):
                    loaded += 1
            if loaded:
                self.status.setText(f"DB読込 {loaded}件")
                append_app_log(f"コメント監視DB読込: {self.lv} / {loaded}件", "DEBUG")
        except Exception:
            append_app_log(traceback.format_exc(), "DEBUG")
            show_status(self, f"DBコメント読込失敗: {self.lv}", "ERROR")

    def maybe_post_basic_reaction(self, comment: dict[str, Any]) -> None:
        user_id = str(comment.get("user_id") or "").strip()
        if not user_id:
            return
        if user_id in self.reaction_inflight_user_ids:
            return
        with tracker.connect() as conn:
            if not tracker.special_user_exists(conn, user_id):
                return
        self.reaction_inflight_user_ids.add(user_id)
        job = ReactionPostJob(self.lv, comment, self.reaction_counts.get(user_id, 0))
        job.signals.posted.connect(self.on_reaction_posted)
        job.signals.skipped.connect(self.on_reaction_skipped)
        job.signals.failed.connect(self.on_reaction_failed)
        QThreadPool.globalInstance().start(job)

    def on_reaction_posted(self, user_id: str, broadcaster_id: str, text: str) -> None:
        self.reaction_inflight_user_ids.discard(user_id)
        self.reaction_counts[user_id] = self.reaction_counts.get(user_id, 0) + 1
        show_status(self, f"基本反応投稿: {self.lv} / {user_id} -> {broadcaster_id} / {text}")

    def on_reaction_skipped(self, user_id: str, reason: str) -> None:
        if user_id:
            self.reaction_inflight_user_ids.discard(user_id)

    def on_reaction_failed(self, user_id: str, error: str) -> None:
        if user_id:
            self.reaction_inflight_user_ids.discard(user_id)
        show_status(self, f"基本反応投稿失敗 {self.lv} / {user_id}: {error}")

    def on_status_changed(self, text: str) -> None:
        if self.closing:
            return
        self.status.setText(text)

    def on_failed(self, detail: str) -> None:
        if self.closing:
            return
        if self.is_normal_ended_stream_error(detail):
            self.status.setText("終了済み")
            show_status(self, f"終了済み放送のためタブを閉じる: {self.lv}")
            append_app_log(f"コメント監視タブ close_requested予約: {self.lv} / reason=normal_ended_stream_error", "DEBUG")
            QTimer.singleShot(0, lambda lv=self.lv: self.close_requested.emit(lv))
            return
        self.status.setText("エラー")
        last_line = next((line for line in reversed(detail.splitlines()) if line.strip()), "NDGRエラー")
        show_status(self, f"NDGRエラー {self.lv}: {last_line}")

    def is_normal_ended_stream_error(self, detail: str) -> bool:
        text = detail.lower()
        ended_markers = [
            "already ended",
            "has already ended",
            "cannot be streamed",
            "program",
        ]
        if "ndgr comment stream failed" in text and "already ended" in text:
            return True
        if "has already ended" in text and "cannot be streamed" in text:
            return True
        return False

    def on_stream_finished(self) -> None:
        append_app_log(
            f"コメント監視タブ on_stream_finished開始: {self.lv} / "
            f"closing={self.closing} / stop_by_user={self.stop_requested_by_user} / "
            f"status={self.status.text()} / comments={self.model.rowCount()} / "
            f"has_received={self.has_received_comment}",
            "DEBUG",
        )
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        normal_stream_end = (
            not self.stop_requested_by_user
            and not self.closing
            and self.status.text() != "エラー"
        )
        should_create_html = (
            self.has_received_comment
            and normal_stream_end
        )
        if self.status.text() not in {"エラー"}:
            self.status.setText("停止")
        if should_create_html:
            append_app_log(f"コメント監視タブ HTML作成開始予定: {self.lv}", "DEBUG")
            self.create_finished_html()
            append_app_log(f"コメント監視タブ HTML作成完了: {self.lv}", "DEBUG")
        if normal_stream_end:
            self.end_check_timer.stop()
            self.status.setText("終了済み")
            show_status(self, f"NDGRストリーム終了を放送終了として扱い、タブを閉じる: {self.lv}")
            append_app_log(f"コメント監視タブ close_requested予約: {self.lv} / reason=normal_stream_end", "DEBUG")
            QTimer.singleShot(0, lambda lv=self.lv: self.close_requested.emit(lv))

    def request_stream_end_check(self) -> None:
        if self.closing or self.end_check_running or self.stream_manager.is_running(self.lv):
            return
        if self.status.text() == "エラー":
            return
        self.end_check_running = True
        self.status.setText("終了確認中")
        job = StreamEndCheckJob(self.lv)
        job.signals.finished.connect(self.on_stream_end_checked)
        job.signals.failed.connect(self.on_stream_end_check_failed)
        QThreadPool.globalInstance().start(job)

    def on_stream_end_checked(self, lv: str, result: object) -> None:
        if lv != self.lv or self.closing:
            return
        self.end_check_running = False
        data = dict(result) if isinstance(result, dict) else {}
        if data.get("checked") and not data.get("on_air"):
            self.end_check_timer.stop()
            self.status.setText("終了済み")
            show_status(self, f"APIで放送終了確認、タブを閉じる: {self.lv}")
            append_app_log(f"コメント監視タブ close_requested予約: {self.lv} / reason=api_end_check", "DEBUG")
            QTimer.singleShot(0, lambda lv=self.lv: self.close_requested.emit(lv))
            return
        if data.get("checked") and data.get("on_air"):
            self.status.setText("停止")
            if data.get("source") == "broadcast_archive_meta":
                show_status(self, f"NDGR終了後も終了予定時刻前。1分後に再確認: {self.lv}")
            else:
                show_status(self, f"NDGR終了後もAPIでは放送中。1分後に再確認: {self.lv}")
            return
        self.status.setText("停止")
        show_status(self, f"NDGR終了後の終了確認不可。1分後に再確認: {self.lv} / {data.get('reason') or 'unknown'}")

    def on_stream_end_check_failed(self, lv: str, detail: str) -> None:
        if lv != self.lv or self.closing:
            return
        self.end_check_running = False
        self.status.setText("停止")
        last_line = next((line for line in reversed(detail.splitlines()) if line.strip()), "終了確認エラー")
        show_status(self, f"NDGR終了後のAPI確認失敗。1分後に再確認 {self.lv}: {last_line}")

    def create_finished_html(self) -> None:
        if self.html_created:
            return
        self.html_created = True
        out_dir = APP_ROOT / "storage" / "html" / self.lv
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / "index.html"
        rows = self.model.rows()
        body_rows = "\n".join(
            "<tr>"
            f"<td>{escape(str(row.get('no') or ''))}</td>"
            f"<td>{escape(str(row.get('received_at') or ''))}</td>"
            f"<td>{escape(str(row.get('user_id') or ''))}</td>"
            f"<td>{escape(str(row.get('text') or ''))}</td>"
            "</tr>"
            for row in rows
        )
        html = f"""<!doctype html>
<html lang="ja">
<head>
  <meta charset="utf-8">
  <title>{escape(self.lv)} comment summary</title>
  <style>
    body {{ font-family: system-ui, sans-serif; margin: 24px; }}
    table {{ border-collapse: collapse; width: 100%; }}
    th, td {{ border: 1px solid #ccc; padding: 6px 8px; vertical-align: top; }}
    th {{ background: #f4f4f4; }}
  </style>
</head>
<body>
  <h1>{escape(self.lv)}</h1>
  <p>NDGR stream finished: {escape(datetime.now().isoformat(timespec="seconds"))}</p>
  <p>comments: {len(rows)}</p>
  <table>
    <thead><tr><th>No</th><th>Received</th><th>User</th><th>Comment</th></tr></thead>
    <tbody>
{body_rows}
    </tbody>
  </table>
</body>
</html>
"""
        out_path.write_text(html, encoding="utf-8")
        show_status(self, f"放送終了HTML作成: {out_path}")
        try:
            uploads = tracker.upload_html_for_special_user_hits(self.lv, out_path)
            if uploads:
                ok = sum(1 for row in uploads if row.get("status") in {"uploaded", "copied"})
                failed = len(uploads) - ok
                show_status(self, f"スペシャルユーザーHTML送信: 成功 {ok} / 失敗 {failed}")
        except Exception as exc:
            append_app_log(traceback.format_exc(), "DEBUG")
            show_status(self, f"スペシャルユーザーHTML送信失敗: {exc}", "ERROR")

    def close_stream(self) -> None:
        append_app_log(f"コメント監視タブ close_stream: {self.lv}", "DEBUG")
        self.prepare_for_close()
        self.stream_manager.stop(self.lv)

    def force_close_stream(self, timeout_ms: int = 3000) -> None:
        append_app_log(f"コメント監視タブ force_close_stream: {self.lv} / timeout={timeout_ms}", "DEBUG")
        self.prepare_for_close()
        self.stream_manager.force_stop(self.lv, timeout_ms)

    def request_close(self, on_finished) -> bool:
        append_app_log(
            f"コメント監視タブ request_close開始: {self.lv} / "
            f"running={self.stream_manager.is_running(self.lv)} / closing={self.closing}",
            "DEBUG",
        )
        self.prepare_for_close()
        if not self.stream_manager.is_running(self.lv):
            append_app_log(f"コメント監視タブ request_close即時完了: {self.lv} / streamなし", "DEBUG")
            return True

        if self._close_finished_handler is not None:
            try:
                self.stream_manager.finished.disconnect(self._close_finished_handler)
            except Exception:
                pass
            self._close_finished_handler = None

        def handle_finished(finished_lv: str) -> None:
            append_app_log(
                f"コメント監視タブ request_close finished受信: {self.lv} / finished_lv={finished_lv}",
                "DEBUG",
            )
            if finished_lv != self.lv:
                return
            try:
                self.stream_manager.finished.disconnect(handle_finished)
            except Exception:
                pass
            self._close_finished_handler = None
            append_app_log(f"コメント監視タブ request_close on_finished実行直前: {self.lv}", "DEBUG")
            on_finished()

        self._close_finished_handler = handle_finished
        self.stream_manager.finished.connect(handle_finished)
        append_app_log(f"コメント監視タブ request_close stop送信: {self.lv}", "DEBUG")
        self.stream_manager.stop(self.lv)
        return False

    def install_comment_button(self, row: int) -> None:
        horizontal_scroll = self.table.horizontalScrollBar().value()
        comment = self.model.comment_at(row)
        if not comment:
            return
        user_id = str(comment.get("user_id") or "").strip()
        if not user_id:
            return
        self.table.setIndexWidget(
            self.model.index(row, self.model.columnCount() - 1),
            self.row_button("登録", row, self.register_special_user),
        )
        self.table.horizontalScrollBar().setValue(horizontal_scroll)
        QTimer.singleShot(0, lambda value=horizontal_scroll: self.table.horizontalScrollBar().setValue(value))

    def row_button(self, text: str, row: int, callback) -> QWidget:
        box = QWidget()
        layout = QHBoxLayout(box)
        layout.setContentsMargins(4, 2, 4, 2)
        button = QPushButton(text)
        button.setFixedHeight(26)
        button.clicked.connect(lambda _checked=False, target_row=row: callback(target_row))
        layout.addWidget(button)
        return box

    def register_special_user(self, row: int) -> None:
        comment = self.model.comment_at(row)
        if not comment:
            return
        user_id = str(comment.get("user_id") or "").strip()
        if not user_id:
            return
        label = str(comment.get("user_name") or "").strip()
        note = f"{self.lv} no={comment.get('no') or ''} {comment.get('text') or ''}".strip()
        save_special_user(user_id=user_id, label=label, note=note)
        window = self.window()
        if hasattr(window, "reload_special_users"):
            window.reload_special_users()
        show_status(self, f"スペシャルユーザー登録: {user_id}")


class CommentTableModel(QAbstractTableModel):
    columns = [
        ("user_icon", "アイコン"),
        ("no", "No"),
        ("received_at", "受信"),
        ("user_name", "ユーザー名"),
        ("user_id", "ユーザーID"),
        ("text", "コメント"),
        ("special_action", "登録"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._rows: list[dict[str, Any]] = []
        self.glow_phase = 0.0

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.columns)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None
        key = self.columns[index.column()][0]
        row = self._rows[index.row()]
        if key == "user_icon" and role == Qt.ItemDataRole.DecorationRole:
            return row.get("_user_icon_pixmap")
        if role not in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole):
            return None
        value = row.get(key)
        return "" if value is None else str(value)

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal:
            return self.columns[section][1]
        return section + 1

    def append_comment(self, comment: dict[str, Any]) -> None:
        row = len(self._rows)
        self.beginInsertRows(QModelIndex(), row, row)
        self._rows.append(comment)
        self.endInsertRows()

    def update_user_profile(self, user_id: str, name: str, pixmap: QPixmap | None) -> None:
        for row_index, row in enumerate(self._rows):
            if str(row.get("user_id") or "") != user_id:
                continue
            if name:
                row["user_name"] = name
            if pixmap is not None:
                row["_user_icon_pixmap"] = pixmap
            self.dataChanged.emit(
                self.index(row_index, 0),
                self.index(row_index, self.columnCount() - 1),
                [Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.DecorationRole],
            )

    def advance_special_glow(self) -> None:
        self.glow_phase = (float(getattr(self, "glow_phase", 0.0)) + 0.42) % (math.pi * 2)
        if not any(row.get("is_special_user") for row in self._rows):
            return
        last_column = max(0, self.columnCount() - 1)
        for row_index, row in enumerate(self._rows):
            if not row.get("is_special_user"):
                continue
            self.dataChanged.emit(
                self.index(row_index, 0),
                self.index(row_index, last_column),
                [Qt.ItemDataRole.BackgroundRole],
            )

    def comment_at(self, row: int) -> dict[str, Any] | None:
        if row < 0 or row >= len(self._rows):
            return None
        return self._rows[row]

    def rows(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self._rows]


class BroadcasterMonitorTableModel(QAbstractTableModel):
    columns = [
        ("onair", "配信"),
        ("enabled", "監視"),
        ("html_generation_enabled", "HTML生成"),
        ("broadcaster_id", "配信者ID"),
        ("broadcaster_name", "名前"),
        ("thumbnail_10sec_enabled", "10秒サムネ"),
        ("audio_timeline_enabled", "音声TL"),
        ("timeline_enabled", "タイムライン"),
        ("ranking_enabled", "ランキング"),
        ("summary_enabled", "要約"),
        ("ai_conversation_enabled", "ニニ/ココ会話"),
        ("music_enabled", "曲"),
        ("abstract_image_enabled", "抽象画像"),
        ("emotion_score_enabled", "感情"),
        ("word_extract_enabled", "言葉抽出"),
        ("source_lv", "取得元"),
    ]
    checkable = {
        "enabled",
        "html_generation_enabled",
        "thumbnail_10sec_enabled",
        "audio_timeline_enabled",
        "timeline_enabled",
        "ranking_enabled",
        "summary_enabled",
        "ai_conversation_enabled",
        "music_enabled",
        "abstract_image_enabled",
        "emotion_score_enabled",
        "word_extract_enabled",
    }
    setting_changed = pyqtSignal(str, str, bool)

    def __init__(self) -> None:
        super().__init__()
        self._rows: list[dict[str, Any]] = []

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.columns)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None
        key = self.columns[index.column()][0]
        value = self._rows[index.row()].get(key)
        if key in self.checkable and role == Qt.ItemDataRole.CheckStateRole:
            return Qt.CheckState.Checked if value else Qt.CheckState.Unchecked
        if role == Qt.ItemDataRole.BackgroundRole and key == "onair" and value:
            return QBrush(QColor(92, 45, 45))
        if role not in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole):
            return None
        if key in self.checkable:
            return ""
        return "" if value is None else str(value)

    def setData(self, index: QModelIndex, value: Any, role: int = Qt.ItemDataRole.EditRole) -> bool:
        if not index.isValid() or role != Qt.ItemDataRole.CheckStateRole:
            return False
        key = self.columns[index.column()][0]
        if key not in self.checkable:
            return False
        enabled = value == Qt.CheckState.Checked.value
        row = self._rows[index.row()]
        row[key] = int(enabled)
        self.dataChanged.emit(index, index, [Qt.ItemDataRole.CheckStateRole])
        broadcaster_id = str(row.get("broadcaster_id") or "")
        if broadcaster_id:
            self.setting_changed.emit(broadcaster_id, key, enabled)
        return True

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if self.columns[index.column()][0] in self.checkable:
            flags |= Qt.ItemFlag.ItemIsUserCheckable
        return flags

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal:
            return self.columns[section][1]
        return section + 1

    def update_rows(self, rows: list[dict[str, Any]]) -> None:
        self.beginResetModel()
        self._rows = rows
        self.endResetModel()

    def broadcaster_id_at(self, row: int) -> str | None:
        if row < 0 or row >= len(self._rows):
            return None
        value = self._rows[row].get("broadcaster_id")
        return str(value) if value else None

    def row_at(self, row: int) -> dict[str, Any] | None:
        if row < 0 or row >= len(self._rows):
            return None
        return dict(self._rows[row])


class MonitoredBroadcasterEditorDialog(QDialog):
    def __init__(self, broadcaster: dict[str, Any], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.original = dict(broadcaster)
        self.archive_tags_ui_state_path = APP_ROOT / "data" / "archive_tags_table_ui.json"
        self.setWindowTitle("監視配信者編集")
        self.resize(1120, 760)
        self.setMinimumSize(820, 560)

        self.broadcaster_id = QLineEdit(str(broadcaster.get("broadcaster_id") or ""))
        self.broadcaster_id.setReadOnly(True)
        self.broadcaster_name = QLineEdit(str(broadcaster.get("broadcaster_name") or ""))
        self.source_lv = QLineEdit(str(broadcaster.get("source_lv") or ""))
        self.recording_output_dir = QLineEdit(str(broadcaster.get("recording_output_dir") or ""))
        self.archive_tags = QWidget()
        archive_tags_layout = QVBoxLayout(self.archive_tags)
        archive_tags_layout.setContentsMargins(0, 0, 0, 0)
        self.archive_tags_table = ArchiveTagsTable(0, 3)
        self.archive_tags_table.setHorizontalHeaderLabels(
            ["認識支援文字", "正式タグ", "別名（,区切り）"]
        )
        self.archive_tags_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Interactive
        )
        self.archive_tags_table.horizontalHeader().setMinimumSectionSize(80)
        self.archive_tags_table.setColumnWidth(0, 220)
        self.archive_tags_table.setColumnWidth(1, 220)
        self.archive_tags_table.setColumnWidth(2, 420)
        self.archive_tags_table.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOn
        )
        self.archive_tags_table.setHorizontalScrollMode(
            QAbstractItemView.ScrollMode.ScrollPerPixel
        )
        self.restore_archive_tags_table_ui()
        self.archive_tags_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.archive_tags_table.setMinimumHeight(180)
        archive_tags_layout.addWidget(self.archive_tags_table)
        archive_tag_buttons = QHBoxLayout()
        add_archive_tag = QPushButton("行を追加")
        add_archive_tag.clicked.connect(lambda _checked=False: self.add_archive_tag_row())
        remove_archive_tag = QPushButton("選択行を削除")
        remove_archive_tag.clicked.connect(lambda _checked=False: self.remove_archive_tag_rows())
        archive_tag_buttons.addWidget(add_archive_tag)
        archive_tag_buttons.addWidget(remove_archive_tag)
        archive_tag_buttons.addStretch(1)
        archive_tags_layout.addLayout(archive_tag_buttons)
        self.transcription_hotwords_enabled = QCheckBox(
            "認識支援文字を文字起こしのHotwordsに使う"
        )
        self.transcription_hotwords_enabled.setChecked(
            bool(broadcaster.get("transcription_hotwords_enabled", 1))
        )
        archive_tags_layout.addWidget(self.transcription_hotwords_enabled)
        self.person_aliases_path = (
            tracker.broadcaster_target_dir(str(broadcaster.get("broadcaster_id") or ""))
            / "index_person_aliases.json"
        )
        self.person_aliases_payload: dict[str, Any] = {}
        try:
            loaded_aliases = json.loads(
                self.person_aliases_path.read_text(encoding="utf-8-sig")
            )
            if isinstance(loaded_aliases, dict):
                self.person_aliases_payload = loaded_aliases
        except (OSError, json.JSONDecodeError):
            self.person_aliases_payload = {}
        canonical_aliases = self.person_aliases_payload.get("canonical_names")
        if not isinstance(canonical_aliases, dict):
            canonical_aliases = {}
        loaded_canonical_names: set[str] = set()
        for raw_line in str(broadcaster.get("archive_tags") or "").replace("\r", "").split("\n"):
            line = raw_line.strip()
            if not line:
                continue
            if "=>" in line:
                recognition, canonical = (part.strip() for part in line.split("=>", 1))
            else:
                recognition = canonical = line
            aliases = canonical_aliases.get(canonical, [])
            self.add_archive_tag_row(
                recognition,
                canonical,
                ", ".join(str(alias).strip() for alias in aliases if str(alias).strip())
                if isinstance(aliases, list) else "",
            )
            loaded_canonical_names.add(canonical)
        for canonical, aliases in canonical_aliases.items():
            canonical = str(canonical or "").strip()
            if not canonical or canonical in loaded_canonical_names:
                continue
            self.add_archive_tag_row(
                canonical,
                canonical,
                ", ".join(str(alias).strip() for alias in aliases if str(alias).strip())
                if isinstance(aliases, list) else "",
                include_in_archive_tags=False,
            )

        self.enabled = QCheckBox("監視を有効にする")
        self.enabled.setChecked(bool(broadcaster.get("enabled", 1)))
        self.html_generation_enabled = QCheckBox("放送終了後にHTMLを生成する")
        self.html_generation_enabled.setChecked(
            bool(broadcaster.get("html_generation_enabled", 1))
        )
        self.custom_settings_enabled = QCheckBox("この配信者では個別設定を使う")
        self.custom_settings_enabled.setChecked(bool(broadcaster.get("custom_settings_enabled", 0)))
        self.thumbnail_10sec_enabled = QCheckBox("10秒ごとにサムネイルを作る")
        self.audio_timeline_enabled = QCheckBox("音声タイムラインに乗せる")
        self.timeline_enabled = QCheckBox("タイムラインHTMLを作る")
        self.ranking_enabled = QCheckBox("ランキングを作る")
        self.ai_conversation_enabled = QCheckBox("ニニちゃん/ココちゃん会話を作る")
        self.summary_enabled = QCheckBox("要約を作る")
        self.music_enabled = QCheckBox("曲を作る")
        self.abstract_image_enabled = QCheckBox("抽象的要約画像を作る")
        self.emotion_score_enabled = QCheckBox("感情スコアを作る")
        self.word_extract_enabled = QCheckBox("言葉抽出を作る")
        for key in tracker.default_broadcaster_monitor_settings():
            widget = getattr(self, key, None)
            if isinstance(widget, QCheckBox):
                widget.setChecked(bool(broadcaster.get(key, 1)))

        self.summary_engine = NoWheelComboBox()
        self.setup_ai_engine_combo(self.summary_engine, str(broadcaster.get("summary_engine") or "codex_exec"))
        self.ai_conversation_engine = NoWheelComboBox()
        self.setup_ai_engine_combo(self.ai_conversation_engine, str(broadcaster.get("ai_conversation_engine") or "codex_exec"))
        self.special_user_summary_engine = NoWheelComboBox()
        self.setup_ai_engine_combo(self.special_user_summary_engine, str(broadcaster.get("special_user_summary_engine") or "codex_exec"))

        config = tracker.load_config()
        self.summary_prompt = self.make_prompt_editor(broadcaster.get("summary_prompt") or config.summary_prompt)
        self.image_prompt = self.make_prompt_editor(broadcaster.get("image_prompt") or config.image_prompt)
        self.music_prompt = self.make_prompt_editor(
            broadcaster.get("music_prompt") or "配信要約をもとに楽曲を生成してください。"
        )
        self.intro_conversation_prompt = self.make_prompt_editor(
            broadcaster.get("intro_conversation_prompt") or config.intro_conversation_prompt
        )
        self.outro_conversation_prompt = self.make_prompt_editor(
            broadcaster.get("outro_conversation_prompt") or config.outro_conversation_prompt
        )
        self.character1_name = QLineEdit(str(broadcaster.get("character1_name") or config.character1_name or tracker.DEFAULT_CHARACTER1_NAME))
        self.character1_image_url = QLineEdit(str(broadcaster.get("character1_image_url") or config.character1_image_url or tracker.DEFAULT_CHARACTER1_IMAGE_URL))
        self.character1_image_flip = QCheckBox("ニニちゃん画像を左右反転")
        self.character1_image_flip.setChecked(bool(broadcaster.get("character1_image_flip", 0)))
        self.character2_name = QLineEdit(str(broadcaster.get("character2_name") or config.character2_name or tracker.DEFAULT_CHARACTER2_NAME))
        self.character2_image_url = QLineEdit(str(broadcaster.get("character2_image_url") or config.character2_image_url or tracker.DEFAULT_CHARACTER2_IMAGE_URL))
        self.character2_image_flip = QCheckBox("ココちゃん画像を左右反転")
        self.character2_image_flip.setChecked(bool(broadcaster.get("character2_image_flip", 0)))

        self.post_server_url = QLineEdit(str(broadcaster.get("post_server_url") or ""))
        self.post_server_api_key = QLineEdit(str(broadcaster.get("post_server_api_key") or ""))
        self.post_server_api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.html_upload_enabled = QCheckBox("HTMLサーバー送信を有効にする")
        self.html_upload_enabled.setChecked(bool(broadcaster.get("html_upload_enabled", 0)))
        self.html_base_url = QLineEdit(str(broadcaster.get("html_base_url") or ""))

        self.transcription_engine = NoWheelComboBox()
        self.transcription_engine.addItem("FasterWhisper", "faster-whisper")
        self.transcription_engine.addItem("WhisperX", "whisperx")
        self.set_combo_value(self.transcription_engine, "whisperx" if bool(broadcaster.get("whisperx_enabled", 0)) else "faster-whisper")
        self.transcription_engine.currentIndexChanged.connect(self.update_transcription_engine_fields)
        model_choices = ["tiny", "base", "small", "medium", "large-v3"]
        self.faster_whisper_model = NoWheelComboBox()
        self.faster_whisper_model.addItems(model_choices)
        self.set_combo_text(self.faster_whisper_model, str(broadcaster.get("faster_whisper_model") or "medium"))
        self.whisperx_model = NoWheelComboBox()
        self.whisperx_model.addItems(model_choices)
        self.set_combo_text(self.whisperx_model, str(broadcaster.get("whisperx_model") or broadcaster.get("faster_whisper_model") or "medium"))
        self.speaker_diarization_enabled = QCheckBox("話者分離文字起こしをする")
        self.speaker_diarization_enabled.setChecked(bool(broadcaster.get("speaker_diarization_enabled", 0)))
        self.diarization_min_speakers = NoWheelSpinBox()
        self.diarization_min_speakers.setRange(1, 20)
        self.diarization_min_speakers.setValue(int(broadcaster.get("diarization_min_speakers") or 1))
        self.diarization_max_speakers = NoWheelSpinBox()
        self.diarization_max_speakers.setRange(1, 20)
        self.diarization_max_speakers.setValue(int(broadcaster.get("diarization_max_speakers") or 4))
        self.transcription_initial_prompt = QLineEdit(
            str(broadcaster.get("transcription_initial_prompt") or "")
        )
        self.transcription_initial_prompt.setPlaceholderText("これはニコニコ生放送の録画音声です")

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        content = QWidget()
        content_layout = QHBoxLayout(content)
        content_layout.setContentsMargins(8, 8, 8, 8)
        content_layout.setSpacing(12)
        left_col = QWidget()
        left_layout = QVBoxLayout(left_col)
        left_layout.setContentsMargins(0, 0, 0, 0)
        right_col = QWidget()
        right_layout = QVBoxLayout(right_col)
        right_layout.setContentsMargins(0, 0, 0, 0)

        basic_box = QGroupBox("基本")
        basic_layout = QVBoxLayout(basic_box)
        self.add_field(basic_layout, "配信者ID", self.broadcaster_id)
        self.add_field(basic_layout, "配信者名", self.broadcaster_name)
        self.add_field(basic_layout, "取得元lv", self.source_lv)
        self.add_field(basic_layout, "録画保存先", self.recording_output_dir)
        basic_layout.addWidget(self.enabled)
        basic_layout.addWidget(self.html_generation_enabled)
        basic_layout.addWidget(self.custom_settings_enabled)
        left_layout.addWidget(basic_box)

        archive_box = QGroupBox("アーカイブHTML")
        archive_layout = QVBoxLayout(archive_box)
        self.add_field(archive_layout, "使用タグ候補（認識支援文字 => 正式タグ）", self.archive_tags)
        archive_layout.addWidget(QLabel("この配信者の放送だけに使用します。別の配信者とは共有されません。"))
        left_layout.addWidget(archive_box)

        feature_box = QGroupBox("生成する要素")
        feature_layout = QVBoxLayout(feature_box)
        for widget in [
            self.thumbnail_10sec_enabled,
            self.audio_timeline_enabled,
            self.timeline_enabled,
            self.ranking_enabled,
            self.ai_conversation_enabled,
            self.summary_enabled,
            self.music_enabled,
            self.abstract_image_enabled,
            self.emotion_score_enabled,
            self.word_extract_enabled,
        ]:
            feature_layout.addWidget(widget)
        left_layout.addWidget(feature_box)

        ai_box = QGroupBox("AI / 投稿 / HTMLサーバー")
        ai_layout = QVBoxLayout(ai_box)
        self.add_field(ai_layout, "要約担当", self.summary_engine)
        self.add_field(ai_layout, "ニニココ会話担当", self.ai_conversation_engine)
        self.add_field(ai_layout, "スペシャルユーザーまとめ担当", self.special_user_summary_engine)
        self.add_field(ai_layout, "投稿サーバーURL", self.post_server_url)
        self.add_field(ai_layout, "投稿サーバーAPIキー", self.post_server_api_key)
        ai_layout.addWidget(self.html_upload_enabled)
        self.add_field(ai_layout, "HTMLベースURL", self.html_base_url)
        right_layout.addWidget(ai_box)

        prompt_box = QGroupBox("配信者別プロンプト")
        prompt_layout = QVBoxLayout(prompt_box)
        self.add_field(prompt_layout, "要約プロンプト", self.summary_prompt)
        self.add_field(prompt_layout, "抽象画像プロンプト", self.image_prompt)
        self.add_field(prompt_layout, "音楽生成プロンプト", self.music_prompt)
        self.add_field(prompt_layout, "開始前会話プロンプト", self.intro_conversation_prompt)
        self.add_field(prompt_layout, "終了後会話プロンプト", self.outro_conversation_prompt)
        right_layout.addWidget(prompt_box)

        char_box = QGroupBox("ニニちゃん / ココちゃん")
        char_layout = QVBoxLayout(char_box)
        self.add_field(char_layout, "キャラ1名", self.character1_name)
        self.add_field(char_layout, "キャラ1画像URL", self.character1_image_url)
        char_layout.addWidget(self.character1_image_flip)
        self.add_field(char_layout, "キャラ2名", self.character2_name)
        self.add_field(char_layout, "キャラ2画像URL", self.character2_image_url)
        char_layout.addWidget(self.character2_image_flip)
        right_layout.addWidget(char_box)

        whisper_box = QGroupBox("文字起こし")
        whisper_layout = QVBoxLayout(whisper_box)
        self.add_field(whisper_layout, "文字起こしエンジン", self.transcription_engine)
        self.add_field(whisper_layout, "初期プロンプト", self.transcription_initial_prompt)
        whisper_layout.addWidget(QLabel("Hotwordsは使用タグ候補から自動生成します。"))
        self.transcription_stack = QStackedWidget()
        fw_page = QWidget()
        fw_layout = QVBoxLayout(fw_page)
        fw_layout.setContentsMargins(0, 0, 0, 0)
        self.add_field(fw_layout, "FasterWhisperモデル", self.faster_whisper_model)
        x_page = QWidget()
        x_layout = QVBoxLayout(x_page)
        x_layout.setContentsMargins(0, 0, 0, 0)
        self.add_field(x_layout, "WhisperXモデル", self.whisperx_model)
        x_layout.addWidget(self.speaker_diarization_enabled)
        self.add_field(x_layout, "話者数 最小", self.diarization_min_speakers)
        self.add_field(x_layout, "話者数 最大", self.diarization_max_speakers)
        self.transcription_stack.addWidget(fw_page)
        self.transcription_stack.addWidget(x_page)
        whisper_layout.addWidget(self.transcription_stack)
        self.update_transcription_engine_fields()
        right_layout.addWidget(whisper_box)
        left_layout.addStretch(1)
        right_layout.addStretch(1)
        content_layout.addWidget(left_col, 1)
        content_layout.addWidget(right_col, 1)
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)
        layout.addWidget(buttons)

    def add_field(self, layout: QVBoxLayout, label: str, widget: QWidget) -> None:
        layout.addWidget(QLabel(label))
        layout.addWidget(widget)

    def add_archive_tag_row(
        self,
        recognition: str = "",
        canonical: str = "",
        aliases: str = "",
        include_in_archive_tags: bool = True,
    ) -> None:
        row = self.archive_tags_table.rowCount()
        self.archive_tags_table.insertRow(row)
        recognition_item = QTableWidgetItem(str(recognition))
        recognition_item.setData(Qt.ItemDataRole.UserRole, include_in_archive_tags)
        self.archive_tags_table.setItem(row, 0, recognition_item)
        self.archive_tags_table.setItem(row, 1, QTableWidgetItem(str(canonical)))
        self.archive_tags_table.setItem(row, 2, QTableWidgetItem(str(aliases)))
        if not recognition and not canonical and not aliases:
            self.archive_tags_table.setCurrentCell(row, 0)
            self.archive_tags_table.editItem(self.archive_tags_table.item(row, 0))

    def restore_archive_tags_table_ui(self) -> None:
        try:
            state = json.loads(
                self.archive_tags_ui_state_path.read_text(encoding="utf-8")
            )
            apply_table_header_state(self.archive_tags_table, state.get("header"))
            apply_table_column_widths(self.archive_tags_table, state.get("widths"))
            QTimer.singleShot(
                0,
                lambda: self.archive_tags_table.horizontalScrollBar().setValue(
                    int(state.get("horizontal_scroll") or 0)
                ),
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass

    def save_archive_tags_table_ui(self) -> None:
        try:
            self.archive_tags_ui_state_path.parent.mkdir(parents=True, exist_ok=True)
            state = {
                "header": table_header_state(self.archive_tags_table),
                "widths": table_column_widths(self.archive_tags_table),
                "horizontal_scroll": self.archive_tags_table.horizontalScrollBar().value(),
            }
            temporary = self.archive_tags_ui_state_path.with_suffix(".json.tmp")
            temporary.write_text(
                json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            temporary.replace(self.archive_tags_ui_state_path)
        except OSError:
            append_app_log(traceback.format_exc(), "DEBUG")

    def done(self, result: int) -> None:
        self.save_archive_tags_table_ui()
        super().done(result)

    def remove_archive_tag_rows(self) -> None:
        rows = sorted({index.row() for index in self.archive_tags_table.selectedIndexes()}, reverse=True)
        for row in rows:
            self.archive_tags_table.removeRow(row)

    def archive_tag_values(self) -> str:
        lines: list[str] = []
        for row in range(self.archive_tags_table.rowCount()):
            left = self.archive_tags_table.item(row, 0)
            right = self.archive_tags_table.item(row, 1)
            if left and left.data(Qt.ItemDataRole.UserRole) is False:
                continue
            recognition = left.text().strip() if left else ""
            canonical = right.text().strip() if right else ""
            if not recognition and not canonical:
                continue
            recognition = recognition or canonical
            canonical = canonical or recognition
            lines.append(canonical if recognition == canonical else f"{recognition} => {canonical}")
        return "\n".join(lines)

    def save_person_aliases(self) -> None:
        canonical_names: dict[str, list[str]] = {}
        for row in range(self.archive_tags_table.rowCount()):
            canonical_item = self.archive_tags_table.item(row, 1)
            aliases_item = self.archive_tags_table.item(row, 2)
            canonical = canonical_item.text().strip() if canonical_item else ""
            if not canonical:
                continue
            aliases = [
                value.strip()
                for value in str(aliases_item.text() if aliases_item else "").split(",")
                if value.strip()
            ]
            canonical_names[canonical] = list(
                dict.fromkeys([*canonical_names.get(canonical, []), *aliases])
            )
        payload = dict(self.person_aliases_payload)
        payload["canonical_names"] = canonical_names
        self.person_aliases_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.person_aliases_path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.person_aliases_path)

    def make_prompt_editor(self, value: Any) -> QTextEdit:
        editor = QTextEdit()
        editor.setAcceptRichText(False)
        editor.setPlainText(str(value or ""))
        editor.setMinimumHeight(84)
        editor.setMaximumHeight(130)
        return editor

    def setup_ai_engine_combo(self, combo: QComboBox, value: str) -> None:
        combo.addItem("Codex exec", "codex_exec")
        combo.addItem("ClaudeCode", "claude")
        combo.addItem("Grok build", "grok")
        combo.addItem("OpenAI API", "openai")
        combo.addItem("Gemini API", "gemini")
        index = combo.findData(value)
        combo.setCurrentIndex(index if index >= 0 else 0)

    def set_combo_text(self, combo: QComboBox, value: str) -> None:
        index = combo.findText(value)
        combo.setCurrentIndex(index if index >= 0 else 0)

    def set_combo_value(self, combo: QComboBox, value: str) -> None:
        index = combo.findData(value)
        combo.setCurrentIndex(index if index >= 0 else 0)

    def update_transcription_engine_fields(self) -> None:
        is_whisperx = str(self.transcription_engine.currentData() or "") == "whisperx"
        self.transcription_stack.setCurrentIndex(1 if is_whisperx else 0)

    def values(self) -> dict[str, Any]:
        engine = str(self.transcription_engine.currentData() or "faster-whisper")
        whisperx_enabled = engine == "whisperx"
        return {
            "broadcaster_name": self.broadcaster_name.text().strip(),
            "source_lv": self.source_lv.text().strip(),
            "recording_output_dir": self.recording_output_dir.text().strip(),
            "archive_tags": self.archive_tag_values(),
            "enabled": int(self.enabled.isChecked()),
            "html_generation_enabled": int(self.html_generation_enabled.isChecked()),
            "custom_settings_enabled": int(self.custom_settings_enabled.isChecked()),
            "thumbnail_10sec_enabled": int(self.thumbnail_10sec_enabled.isChecked()),
            "audio_timeline_enabled": int(self.audio_timeline_enabled.isChecked()),
            "timeline_enabled": int(self.timeline_enabled.isChecked()),
            "ranking_enabled": int(self.ranking_enabled.isChecked()),
            "ai_conversation_enabled": int(self.ai_conversation_enabled.isChecked()),
            "summary_enabled": int(self.summary_enabled.isChecked()),
            "music_enabled": int(self.music_enabled.isChecked()),
            "abstract_image_enabled": int(self.abstract_image_enabled.isChecked()),
            "emotion_score_enabled": int(self.emotion_score_enabled.isChecked()),
            "word_extract_enabled": int(self.word_extract_enabled.isChecked()),
            "summary_engine": str(self.summary_engine.currentData()),
            "ai_conversation_engine": str(self.ai_conversation_engine.currentData()),
            "special_user_summary_engine": str(self.special_user_summary_engine.currentData()),
            "summary_prompt": self.summary_prompt.toPlainText().strip(),
            "image_prompt": self.image_prompt.toPlainText().strip(),
            "music_prompt": self.music_prompt.toPlainText().strip(),
            "intro_conversation_prompt": self.intro_conversation_prompt.toPlainText().strip(),
            "outro_conversation_prompt": self.outro_conversation_prompt.toPlainText().strip(),
            "character1_name": self.character1_name.text().strip(),
            "character1_image_url": self.character1_image_url.text().strip(),
            "character1_image_flip": int(self.character1_image_flip.isChecked()),
            "character2_name": self.character2_name.text().strip(),
            "character2_image_url": self.character2_image_url.text().strip(),
            "character2_image_flip": int(self.character2_image_flip.isChecked()),
            "post_server_url": self.post_server_url.text().strip(),
            "post_server_api_key": self.post_server_api_key.text(),
            "html_upload_enabled": int(self.html_upload_enabled.isChecked()),
            "html_base_url": self.html_base_url.text().strip(),
            "faster_whisper_model": self.faster_whisper_model.currentText(),
            "whisperx_model": self.whisperx_model.currentText(),
            "whisperx_enabled": int(whisperx_enabled),
            "transcription_initial_prompt": self.transcription_initial_prompt.text().strip(),
            "transcription_hotwords_enabled": int(self.transcription_hotwords_enabled.isChecked()),
            "speaker_diarization_enabled": int(whisperx_enabled and self.speaker_diarization_enabled.isChecked()),
            "diarization_min_speakers": self.diarization_min_speakers.value(),
            "diarization_max_speakers": self.diarization_max_speakers.value(),
        }


class BroadcasterMonitorTab(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.broadcaster_model = BroadcasterMonitorTableModel()
        self.broadcaster_model.setting_changed.connect(self.on_broadcaster_setting_changed)
        self.special_model = SpecialUsersModel()
        self.pending_name_fetches: set[tuple[str, str]] = set()

        self.broadcaster_id_input = QLineEdit()
        self.broadcaster_id_input.setPlaceholderText("配信者ID または user URL")
        self.broadcaster_name_input = QLineEdit()
        self.broadcaster_name_input.setPlaceholderText("名前。空なら自動取得")
        self.add_broadcaster_button = QPushButton("配信者登録")
        self.add_broadcaster_button.clicked.connect(self.add_broadcaster)
        self.delete_broadcaster_button = QPushButton("配信者削除")
        self.delete_broadcaster_button.clicked.connect(self.delete_broadcaster)

        self.special_id_input = QLineEdit()
        self.special_id_input.setPlaceholderText("スペシャルユーザーID または user URL")
        self.special_name_input = QLineEdit()
        self.special_name_input.setPlaceholderText("名前。空なら自動取得")
        self.add_special_button = QPushButton("スペシャル登録")
        self.add_special_button.clicked.connect(self.add_special_user)

        self.broadcaster_table = QTableView()
        self.broadcaster_table.setModel(self.broadcaster_model)
        stabilize_table_scroll(self.broadcaster_table)
        setattr(self.broadcaster_table, "_broadcaster_timeshift_enabled", True)
        setattr(
            self.broadcaster_table,
            "_stop_broadcaster_recording_sender",
            self.stop_broadcaster_recording,
        )
        self.broadcaster_table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.broadcaster_table.setSelectionMode(QTableView.SelectionMode.SingleSelection)
        self.broadcaster_table.setAlternatingRowColors(True)
        self.broadcaster_table.verticalHeader().setVisible(False)
        configure_table_header(
            self.broadcaster_table,
            [70, 55, 80, 120, 160] + [90 for _ in range(10)],
        )
        self.broadcaster_table.doubleClicked.connect(self.open_broadcaster_editor)

        self.special_table = QTableView()
        self.special_table.setModel(self.special_model)
        stabilize_table_scroll(self.special_table)
        self.special_table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.special_table.setSelectionMode(QTableView.SelectionMode.SingleSelection)
        self.special_table.setAlternatingRowColors(True)
        self.special_table.verticalHeader().setVisible(False)
        configure_table_header(self.special_table, [150, 160])

        broadcaster_form = QWidget()
        broadcaster_form_layout = QHBoxLayout(broadcaster_form)
        broadcaster_form_layout.setContentsMargins(0, 0, 0, 0)
        broadcaster_form_layout.addWidget(self.broadcaster_id_input, 2)
        broadcaster_form_layout.addWidget(self.broadcaster_name_input, 2)
        broadcaster_form_layout.addWidget(self.add_broadcaster_button)
        broadcaster_form_layout.addWidget(self.delete_broadcaster_button)

        special_form = QWidget()
        special_form_layout = QHBoxLayout(special_form)
        special_form_layout.setContentsMargins(0, 0, 0, 0)
        special_form_layout.addWidget(self.special_id_input, 2)
        special_form_layout.addWidget(self.special_name_input, 2)
        special_form_layout.addWidget(self.add_special_button)

        splitter = QSplitter()
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.addWidget(QLabel("監視する配信者"))
        left_layout.addWidget(broadcaster_form)
        left_layout.addWidget(self.broadcaster_table, 1)
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.addWidget(QLabel("監視するスペシャルユーザー"))
        right_layout.addWidget(special_form)
        right_layout.addWidget(self.special_table, 1)
        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setSizes([760, 420])

        layout = QVBoxLayout(self)
        layout.addWidget(splitter, 1)
        self.reload()

    def reload(self) -> None:
        broadcasters = tracker.list_monitored_broadcasters()
        with tracker.connect() as conn:
            onair_ids = {
                str(row["broadcaster_id"]).strip()
                for row in conn.execute(
                    """
                    SELECT DISTINCT broadcaster_id
                    FROM broadcasts
                    WHERE COALESCE(status, '') IN ('', 'ON_AIR')
                      AND COALESCE(broadcaster_id, '') != ''
                    """
                ).fetchall()
            }
        for row in broadcasters:
            broadcaster_id = str(row.get("broadcaster_id") or "").strip()
            if not broadcaster_id.isdigit():
                continue
            try:
                lives = tracker.fetch_on_air_user_live_programs(broadcaster_id)
            except Exception as exc:
                app_log("DEBUG", f"配信者ON_AIR API確認失敗: {broadcaster_id}: {type(exc).__name__}: {exc}")
                continue
            if lives:
                onair_ids.add(broadcaster_id)
            else:
                onair_ids.discard(broadcaster_id)
        for row in broadcasters:
            broadcaster_id = str(row.get("broadcaster_id") or "").strip()
            row["onair"] = "ONAIR" if broadcaster_id in onair_ids else ""
        self.broadcaster_model.update_rows(broadcasters)
        specials = tracker.list_special_users()
        self.special_model.update_rows(specials)
        for row in broadcasters:
            broadcaster_id = str(row.get("broadcaster_id") or "").strip()
            name = str(row.get("broadcaster_name") or "").strip()
            if broadcaster_id.isdigit() and not name:
                self.fetch_name("broadcaster", broadcaster_id)
        for row in specials:
            user_id = str(row.get("user_id") or "").strip()
            name = str(row.get("label") or "").strip()
            if user_id.isdigit() and not name:
                self.fetch_name("special", user_id)

    def add_broadcaster(self) -> None:
        original_value = self.broadcaster_id_input.text().strip()
        broadcaster_id = extract_user_id(original_value) or original_value
        if not broadcaster_id:
            show_status(self, "配信者IDを入力してくれ")
            return
        name = self.broadcaster_name_input.text().strip()
        if original_value and (extract_channel_slug(original_value) or broadcaster_id.startswith("ch")) and not name:
            try:
                channel = fetch_niconico_channel_info(original_value)
                broadcaster_id = channel["id"]
                name = channel["name"]
            except Exception as exc:
                show_status(self, f"チャンネル名取得失敗: {exc}")
        tracker.save_monitored_broadcaster(broadcaster_id=broadcaster_id, broadcaster_name=name)
        self.broadcaster_id_input.clear()
        self.broadcaster_name_input.clear()
        self.reload()
        show_status(self, f"配信者監視登録: {broadcaster_id}")
        if (broadcaster_id.isdigit() or broadcaster_id.startswith("ch")) and not name:
            self.fetch_name("broadcaster", broadcaster_id)

    def delete_broadcaster(self) -> None:
        broadcaster_id = self.broadcaster_model.broadcaster_id_at(self.broadcaster_table.currentIndex().row())
        if not broadcaster_id:
            return
        tracker.delete_monitored_broadcaster(broadcaster_id)
        self.reload()
        show_status(self, f"配信者監視削除: {broadcaster_id}")

    def stop_broadcaster_recording(self, broadcaster_id: str) -> None:
        jobs = [
            row
            for row in tracker.running_live_recording_jobs()
            if str(row.get("broadcaster_id") or "").strip() == broadcaster_id
        ]
        if not jobs:
            show_status(self, f"録画停止対象なし: {broadcaster_id}")
            return
        stopped_lvs: list[str] = []
        for job in jobs:
            lv = str(job.get("lv") or "").strip()
            if not lv:
                continue
            tracker.stop_recording_for_broadcast(lv, reason="broadcaster_context_menu")
            stopped_lvs.append(lv)
        self.reload()
        show_status(self, f"録画停止: {broadcaster_id} / {','.join(stopped_lvs)}", "INFO")

    def open_broadcaster_editor(self, index: QModelIndex) -> None:
        row = self.broadcaster_model.row_at(index.row())
        if not row:
            return
        broadcaster_id = str(row.get("broadcaster_id") or "").strip()
        if not broadcaster_id:
            return
        dialog = MonitoredBroadcasterEditorDialog(row, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            tracker.save_monitored_broadcaster_details(broadcaster_id, dialog.values())
            dialog.save_person_aliases()
            self.reload()
            show_status(self, f"監視配信者編集: {broadcaster_id}")

    def add_special_user(self) -> None:
        user_id = extract_user_id(self.special_id_input.text()) or self.special_id_input.text().strip()
        if not user_id:
            show_status(self, "スペシャルユーザーIDを入力してくれ")
            return
        name = self.special_name_input.text().strip()
        save_special_user(user_id=user_id, label=name, note="")
        self.special_id_input.clear()
        self.special_name_input.clear()
        self.reload()
        if hasattr(self.window(), "reload_special_users"):
            self.window().reload_special_users()
        show_status(self, f"スペシャルユーザー登録: {user_id}")
        if user_id.isdigit() and not name:
            self.fetch_name("special", user_id)

    def on_broadcaster_setting_changed(self, broadcaster_id: str, key: str, enabled: bool) -> None:
        tracker.update_monitored_broadcaster_setting(broadcaster_id, key, enabled)
        show_status(self, f"配信者設定更新: {broadcaster_id} / {key}={int(enabled)}")

    def fetch_name(self, kind: str, user_id: str) -> None:
        key = (kind, user_id)
        if key in self.pending_name_fetches:
            return
        self.pending_name_fetches.add(key)
        job = NicovideoUserNameFetchJob(user_id)
        job.signals.finished.connect(
            lambda fetched_id, name, target_kind=kind: self.on_name_fetched(target_kind, fetched_id, name)
        )
        job.signals.failed.connect(
            lambda fetched_id, error, target_kind=kind: self.on_name_failed(target_kind, fetched_id, error)
        )
        QThreadPool.globalInstance().start(job)

    def on_name_fetched(self, kind: str, user_id: str, name: str) -> None:
        self.pending_name_fetches.discard((kind, user_id))
        if kind == "broadcaster":
            tracker.save_monitored_broadcaster(broadcaster_id=user_id, broadcaster_name=name)
        else:
            save_special_user(user_id=user_id, label=name, note="")
            if hasattr(self.window(), "reload_special_users"):
                self.window().reload_special_users()
        self.reload()
        show_status(self, f"名前取得: {user_id} / {name}")

    def on_name_failed(self, kind: str, user_id: str, error: str) -> None:
        self.pending_name_fetches.discard((kind, user_id))
        show_status(self, f"名前取得失敗 {user_id}: {error}")


class NDGRStreamWorker(QObject):
    comment_received = pyqtSignal(object)
    status_changed = pyqtSignal(str)
    failed = pyqtSignal(str)
    finished = pyqtSignal()

    def __init__(self, lv: str) -> None:
        super().__init__()
        self.lv = lv
        self.stop_requested = False

    def stop(self) -> None:
        append_app_log(f"NDGR worker stop要求: {self.lv}", "DEBUG")
        self.stop_requested = True

    def run(self) -> None:
        append_app_log(f"NDGR worker run開始: {self.lv}", "DEBUG")
        try:
            try:
                with tracker.connect() as conn:
                    append_app_log(f"NDGR worker meta取得開始: {self.lv}", "DEBUG")
                    tracker.fetch_and_save_broadcast_archive_meta(conn, self.lv)
                    conn.commit()
                    append_app_log(f"NDGR worker meta取得完了: {self.lv}", "DEBUG")
            except Exception:
                append_app_log(traceback.format_exc(), "DEBUG")
            asyncio.run(self.run_async())
        except Exception:
            append_app_log(f"NDGR worker failed emit直前: {self.lv}", "DEBUG")
            self.failed.emit(traceback.format_exc())
        finally:
            append_app_log(f"NDGR worker finished emit直前: {self.lv}", "DEBUG")
            self.finished.emit()

    async def run_async(self) -> None:
        stop_event = asyncio.Event()
        started_at = time.monotonic()
        comment_count = 0
        last_comment: dict[str, Any] = {}
        end_reason = "iterator_exhausted_without_exception"

        async def watch_stop() -> None:
            while not self.stop_requested:
                await asyncio.sleep(0.2)
            stop_event.set()

        watcher = asyncio.create_task(watch_stop())
        source = ndgr_realtime.NDGRCommentSource()
        self.status_changed.emit("NDGR接続中")
        append_app_log(f"NDGR stream開始: {self.lv}", "INFO")
        try:
            async for comment in source.stream(lv=self.lv, stop_event=stop_event):
                comment_count += 1
                last_comment = dict(comment)
                self.status_changed.emit("受信中")
                self.comment_received.emit(dict(comment))
                if self.stop_requested:
                    end_reason = "stop_requested_by_user"
                    stop_event.set()
                    break
            if stop_event.is_set() and self.stop_requested:
                end_reason = "stop_requested_by_user"
        except Exception as exc:
            end_reason = f"exception:{type(exc).__name__}"
            append_app_log(f"NDGR stream例外: {self.lv} / {type(exc).__name__}: {exc}", "ERROR")
            raise
        finally:
            stop_event.set()
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
            elapsed = time.monotonic() - started_at
            level = "INFO" if end_reason == "stop_requested_by_user" else "WARN"
            if end_reason.startswith("exception:"):
                level = "ERROR"
            append_app_log(
                "NDGR stream終了: "
                f"{self.lv} / reason={end_reason} / comments={comment_count} / "
                f"elapsed={elapsed:.1f}秒 / stop_requested={self.stop_requested} / "
                f"last_no={last_comment.get('no') or ''} / "
                f"last_posted_at={last_comment.get('posted_at') or last_comment.get('received_at') or ''} / "
                f"last_user={last_comment.get('user_id') or last_comment.get('raw_user_id') or last_comment.get('hashed_user_id') or ''} / "
                f"last_text={str(last_comment.get('text') or '')[:120]}",
                level,
            )
            append_app_log(f"NDGR stream finally完了: {self.lv} / reason={end_reason}", "DEBUG")


class DownloadAllCommentsSignals(QObject):
    finished = pyqtSignal(str, int)
    failed = pyqtSignal(str, str)


class DownloadAllCommentsJob(QRunnable):
    def __init__(self, lv: str) -> None:
        super().__init__()
        self.lv = lv
        self.signals = DownloadAllCommentsSignals()

    def run(self) -> None:
        try:
            config = tracker.load_config()
            temp_path = tracker.download_comments(self.lv, config)
            comments = tracker.parse_comments(temp_path)
            saved = 0
            with tracker.connect() as conn:
                try:
                    tracker.fetch_and_save_broadcast_archive_meta(conn, self.lv)
                except Exception:
                    append_app_log(traceback.format_exc(), "DEBUG")
                for comment in comments:
                    tracker.save_archive_comment_from_ndgr(conn, self.lv, comment)
                    saved += 1
                conn.commit()
            try:
                shutil.rmtree(temp_path.parent, ignore_errors=True)
            except Exception:
                pass
            self.signals.finished.emit(self.lv, saved)
        except Exception:
            self.signals.failed.emit(self.lv, traceback.format_exc())


class CommentStreamManager(QObject):
    comment_received = pyqtSignal(str, object)
    status_changed = pyqtSignal(str, str)
    failed = pyqtSignal(str, str)
    finished = pyqtSignal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.streams: dict[str, dict[str, Any]] = {}
        self.process_lvs: dict[int, str] = {}

    def is_running(self, lv: str) -> bool:
        stream = self.streams.get(lv)
        process = stream.get("process") if stream else None
        return bool(process and process.state() != QProcess.ProcessState.NotRunning)

    def start(self, lv: str) -> None:
        lv = str(lv).strip()
        if not lv or self.is_running(lv):
            append_app_log(f"NDGR manager startスキップ: {lv} / running={self.is_running(lv)}", "DEBUG")
            return
        process = QProcess(self)
        process.setProgram(sys.executable)
        child_path = APP_ROOT / "app" / "ndgr_stream_child.py"
        process.setArguments([str(child_path), lv])
        process.setWorkingDirectory(str(APP_ROOT))
        env = QProcessEnvironment.systemEnvironment()
        env.insert("PYTHONUTF8", "1")
        env.insert("PYTHONIOENCODING", "utf-8")
        process.setProcessEnvironment(env)
        process.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)
        self.streams[lv] = {
            "process": process,
            "qprocess_pid": 0,
            "child_pid": 0,
            "stdout_buffer": "",
            "stderr_buffer": "",
            "stop_requested": False,
            "child_finished": False,
            "child_error": False,
            "comments": 0,
            "started_at": time.monotonic(),
        }
        self.process_lvs[id(process)] = lv
        append_app_log(
            f"NDGR manager child start: {lv} / process_id={id(process)} / python={sys.executable}",
            "DEBUG",
        )
        process.readyReadStandardOutput.connect(self._on_process_stdout)
        process.readyReadStandardError.connect(self._on_process_stderr)
        process.errorOccurred.connect(self._on_process_error)
        process.finished.connect(self._on_process_finished)
        process.start()
        if not process.waitForStarted(5000):
            detail = process.errorString()
            append_app_log(f"NDGR child start失敗: {lv} / {detail}", "ERROR")
            self.streams.pop(lv, None)
            self.process_lvs.pop(id(process), None)
            process.deleteLater()
            self.failed.emit(lv, f"NDGR child process failed to start: {detail}")
            self.finished.emit(lv)
            return
        qprocess_pid = self._process_pid(process)
        stream = self.streams.get(lv)
        if stream is not None:
            stream["qprocess_pid"] = qprocess_pid
        append_app_log(
            f"NDGR manager child.start完了: {lv} / qprocess_pid={qprocess_pid} / script={child_path}",
            "DEBUG",
        )

    def _process_pid(self, process: QProcess | None) -> int:
        if not isinstance(process, QProcess):
            return 0
        try:
            return int(process.processId() or 0)
        except Exception:
            return 0

    def _pid_detail(self, stream: dict[str, Any] | None, process: QProcess | None) -> str:
        qprocess_pid = int(stream.get("qprocess_pid") or 0) if stream else self._process_pid(process)
        child_pid = int(stream.get("child_pid") or 0) if stream else 0
        return f"qprocess_pid={qprocess_pid} / child_pid={child_pid}"

    def _sender_process_lv(self) -> str:
        sender = self.sender()
        if sender is None:
            return ""
        return self.process_lvs.get(id(sender), "")

    @pyqtSlot()
    def _on_process_stdout(self) -> None:
        lv = self._sender_process_lv()
        stream = self.streams.get(lv)
        process = self.sender()
        if not lv or not stream or not isinstance(process, QProcess):
            return
        chunk = bytes(process.readAllStandardOutput()).decode("utf-8", errors="replace")
        stream["stdout_buffer"] = str(stream.get("stdout_buffer") or "") + chunk
        lines = str(stream["stdout_buffer"]).splitlines(keepends=True)
        if lines and not lines[-1].endswith(("\n", "\r")):
            stream["stdout_buffer"] = lines.pop()
        else:
            stream["stdout_buffer"] = ""
        for raw_line in lines:
            line = raw_line.strip()
            if line:
                self._handle_child_json_line(lv, line)

    @pyqtSlot()
    def _on_process_stderr(self) -> None:
        lv = self._sender_process_lv()
        stream = self.streams.get(lv)
        process = self.sender()
        if not lv or not stream or not isinstance(process, QProcess):
            return
        chunk = bytes(process.readAllStandardError()).decode("utf-8", errors="replace")
        stream["stderr_buffer"] = str(stream.get("stderr_buffer") or "") + chunk
        for line in chunk.splitlines():
            text = line.strip()
            if text:
                append_app_log(f"NDGR child stderr: {lv} / {text[:500]}", "DEBUG")

    def _handle_child_json_line(self, lv: str, line: str) -> None:
        stream = self.streams.get(lv)
        if not stream:
            return
        try:
            payload = json.loads(line)
        except Exception:
            append_app_log(f"NDGR child stdout非JSON: {lv} / {line[:500]}", "WARN")
            return
        payload_pid = payload.get("pid")
        try:
            child_pid = int(payload_pid or 0)
        except (TypeError, ValueError):
            child_pid = 0
        if child_pid and int(stream.get("child_pid") or 0) != child_pid:
            stream["child_pid"] = child_pid
            process = stream.get("process")
            append_app_log(
                f"NDGR child 実体PID確認: {lv} / {self._pid_detail(stream, process if isinstance(process, QProcess) else None)}",
                "DEBUG",
            )
        event = str(payload.get("event") or "")
        if event == "comment":
            stream["comments"] = int(stream.get("comments") or 0) + 1
            self.comment_received.emit(lv, payload.get("comment") or {})
            return
        if event == "status":
            self.status_changed.emit(lv, str(payload.get("text") or ""))
            return
        if event == "log":
            level = str(payload.get("level") or "DEBUG")
            message = str(payload.get("message") or "")
            append_app_log(f"NDGR child: {lv} / {message}", level)
            return
        if event == "error":
            stream["child_error"] = True
            detail = str(payload.get("traceback") or payload.get("message") or "NDGR child error")
            append_app_log(f"NDGR child error受信: {lv} / {str(payload.get('message') or '')[:500]}", "ERROR")
            self.failed.emit(lv, detail)
            return
        if event == "finished":
            stream["child_finished"] = True
            append_app_log(
                f"NDGR child finished通知: {lv} / comments={payload.get('comments')} / elapsed={payload.get('elapsed')}",
                "DEBUG",
            )
            return
        append_app_log(f"NDGR child unknown event: {lv} / {event} / {line[:500]}", "DEBUG")

    def stop(self, lv: str) -> None:
        stream = self.streams.get(str(lv).strip())
        if not stream:
            append_app_log(f"NDGR manager stopスキップ: {lv} / streamなし", "DEBUG")
            return
        process = stream.get("process")
        stream["stop_requested"] = True
        append_app_log(
            f"NDGR manager child stop要求: {lv} / process={bool(process)} / "
            f"{self._pid_detail(stream, process if isinstance(process, QProcess) else None)}",
            "DEBUG",
        )
        if isinstance(process, QProcess) and process.state() != QProcess.ProcessState.NotRunning:
            process.terminate()
            QTimer.singleShot(2000, lambda proc=process, target_lv=lv: self._kill_if_running(target_lv, proc))

    def force_stop(self, lv: str, timeout_ms: int = 3000) -> None:
        lv = str(lv).strip()
        stream = self.streams.get(lv)
        if not stream:
            append_app_log(f"NDGR manager force_stopスキップ: {lv} / streamなし", "DEBUG")
            return
        process = stream.get("process")
        stream["stop_requested"] = True
        append_app_log(
            f"NDGR manager child force_stop開始: {lv} / process={bool(process)} / "
            f"running={bool(isinstance(process, QProcess) and process.state() != QProcess.ProcessState.NotRunning)} / "
            f"timeout={timeout_ms} / {self._pid_detail(stream, process if isinstance(process, QProcess) else None)}",
            "DEBUG",
        )
        if isinstance(process, QProcess) and process.state() != QProcess.ProcessState.NotRunning:
            process.terminate()
            if not process.waitForFinished(timeout_ms):
                append_app_log(f"NDGR child 強制kill: {lv} / {self._pid_detail(stream, process)}", "WARN")
                self._kill_process_tree(lv, process, stream)
                process.waitForFinished(3000)
        if lv in self.streams:
            append_app_log(f"NDGR manager child force_stop後始末予約: {lv}", "DEBUG")
            QTimer.singleShot(0, lambda target_lv=lv: self._on_thread_finished(target_lv))

    def stop_all(self, timeout_ms: int = 3000) -> None:
        for lv in list(self.streams.keys()):
            self.force_stop(lv, timeout_ms)

    def _kill_if_running(self, lv: str, process: QProcess) -> None:
        if process.state() != QProcess.ProcessState.NotRunning:
            stream = self.streams.get(lv)
            append_app_log(
                f"NDGR child terminate後も実行中のためkill: {lv} / {self._pid_detail(stream, process)}",
                "WARN",
            )
            self._kill_process_tree(lv, process, stream)

    def _kill_process_tree(self, lv: str, process: QProcess, stream: dict[str, Any] | None) -> None:
        pids = []
        qprocess_pid = int(stream.get("qprocess_pid") or 0) if stream else self._process_pid(process)
        child_pid = int(stream.get("child_pid") or 0) if stream else 0
        for pid in (qprocess_pid, child_pid):
            if pid > 0 and pid not in pids:
                pids.append(pid)
        for pid in pids:
            try:
                result = subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=5,
                )
                level = "DEBUG" if result.returncode == 0 else "WARN"
                tail = ((result.stdout or "") + "\n" + (result.stderr or "")).strip().replace("\r", " ").replace("\n", " ")
                append_app_log(
                    f"NDGR taskkill実行: {lv} / pid={pid} / returncode={result.returncode} / {tail[:500]}",
                    level,
                )
            except Exception as exc:
                append_app_log(f"NDGR taskkill失敗: {lv} / pid={pid} / {type(exc).__name__}: {exc}", "WARN")
        process.kill()

    @pyqtSlot(QProcess.ProcessError)
    def _on_process_error(self, error: QProcess.ProcessError) -> None:
        lv = self._sender_process_lv()
        process = self.sender()
        detail = process.errorString() if isinstance(process, QProcess) else str(error)
        append_app_log(f"NDGR child process error: {lv or '?'} / {error.name} / {detail}", "ERROR")
        if lv:
            self.failed.emit(lv, f"NDGR child process error: {error.name}: {detail}")

    @pyqtSlot(int, QProcess.ExitStatus)
    def _on_process_finished(self, exit_code: int, exit_status: QProcess.ExitStatus) -> None:
        lv = self._sender_process_lv()
        process = self.sender()
        stream = self.streams.get(lv)
        stderr_tail = ""
        stdout_tail = ""
        if stream:
            stderr_tail = str(stream.get("stderr_buffer") or "")[-1200:]
            stdout_tail = str(stream.get("stdout_buffer") or "")[-1200:]
        append_app_log(
            f"NDGR child finished signal: lv={lv or '?'} / exit_code={exit_code} / "
            f"exit_status={exit_status.name} / stop_requested={bool(stream and stream.get('stop_requested'))} / "
            f"child_finished={bool(stream and stream.get('child_finished'))} / "
            f"child_error={bool(stream and stream.get('child_error'))} / comments={stream.get('comments') if stream else ''} / "
            f"{self._pid_detail(stream, process if isinstance(process, QProcess) else None)}",
            "DEBUG",
        )
        if lv:
            if stream and not stream.get("stop_requested") and exit_status == QProcess.ExitStatus.CrashExit:
                detail = (
                    f"NDGR child crashed: lv={lv} exit_code={exit_code} exit_status={exit_status.name}\n"
                    f"stdout_tail:\n{stdout_tail}\n\nstderr_tail:\n{stderr_tail}"
                )
                append_app_log(f"NDGR child crash検出: {lv} / exit_code={exit_code}", "ERROR")
                self.failed.emit(lv, detail)
            elif (
                stream
                and not stream.get("stop_requested")
                and exit_code not in {0}
                and not stream.get("child_finished")
                and not stream.get("child_error")
            ):
                detail = (
                    f"NDGR child exited with error: lv={lv} exit_code={exit_code} exit_status={exit_status.name}\n"
                    f"stdout_tail:\n{stdout_tail}\n\nstderr_tail:\n{stderr_tail}"
                )
                append_app_log(f"NDGR child 異常終了: {lv} / exit_code={exit_code}", "ERROR")
                self.failed.emit(lv, detail)
            self._on_thread_finished(lv)

    def _on_thread_finished(self, lv: str) -> None:
        if lv not in self.streams:
            append_app_log(f"NDGR manager child.finished重複/streamなし: {lv}", "DEBUG")
            return
        append_app_log(f"NDGR manager child.finished受信: {lv}", "DEBUG")
        stream = self.streams.pop(lv, None)
        process = stream.get("process") if stream else None
        if process is not None:
            self.process_lvs.pop(id(process), None)
            process.deleteLater()
        append_app_log(f"NDGR manager finished emit直前: {lv}", "DEBUG")
        self.finished.emit(lv)


class CommentMonitorTab(QWidget):
    def __init__(self, stream_manager: CommentStreamManager, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.stream_manager = stream_manager
        self.closing_tabs: list[BroadcastCommentTab] = []
        self.broadcast_tabs = QTabWidget()
        self.broadcast_tabs.setTabsClosable(True)
        self.broadcast_tabs.tabCloseRequested.connect(self.close_broadcast_tab)
        self.broadcast_tabs.currentChanged.connect(self.on_current_broadcast_tab_changed)
        self.broadcast_tabs.tabBar().setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.broadcast_tabs.tabBar().customContextMenuRequested.connect(self.open_tab_context_menu)

        self.lv_input = QLineEdit()
        self.lv_input.setPlaceholderText("lv350000000 または watch URL")
        self.open_button = QPushButton("放送タブを開く")
        self.open_button.clicked.connect(self.open_from_input)

        controls = QWidget()
        controls_layout = QHBoxLayout(controls)
        controls_layout.setContentsMargins(0, 0, 0, 0)
        controls_layout.addWidget(self.lv_input)
        controls_layout.addWidget(self.open_button)
        controls_layout.addStretch(1)

        layout = QVBoxLayout(self)
        layout.addWidget(controls)
        layout.addWidget(self.broadcast_tabs, 1)

    def open_from_input(self) -> None:
        lv = extract_nicolive_id(self.lv_input.text())
        if not lv:
            show_status(self, "放送IDが違う: lv350000000、jk1、または watch URL を入力してくれ")
            return
        self.open_broadcast_tab(lv, silent_ended=False)
        self.lv_input.clear()

    def open_broadcast_tab(
        self,
        lv: str,
        *,
        activate: bool = True,
        silent_ended: bool = True,
        context: dict[str, Any] | None = None,
    ) -> bool:
        lv = extract_nicolive_id(lv) or ""
        if not lv:
            show_status(self, "放送IDが違う: lv350000000、jk1、または watch URL を入力してくれ")
            return False
        context = context or {}
        try:
            with tracker.connect() as conn:
                if tracker.is_lv_known_ended(conn, lv):
                    if not silent_ended:
                        self.status.setText(f"終了済み: {lv}")
                    return False
        except Exception:
            append_app_log(traceback.format_exc(), "DEBUG")
        for index in range(self.broadcast_tabs.count()):
            tab = self.broadcast_tabs.widget(index)
            if isinstance(tab, BroadcastCommentTab) and tab.lv == lv:
                tab.set_context(
                    broadcast_title=str(context.get("title") or ""),
                    broadcaster_name=str(context.get("broadcaster_name") or ""),
                    broadcaster_id=str(context.get("broadcaster_id") or ""),
                    origin_text=str(context.get("origin_text") or ""),
                )
                tab.load_archived_comments()
                if activate:
                    self.broadcast_tabs.setCurrentIndex(index)
                    show_status(self, f"コメント監視タブ表示: {lv}")
                return True
        tab = BroadcastCommentTab(
            lv,
            self.stream_manager,
            broadcast_title=str(context.get("title") or ""),
            broadcaster_name=str(context.get("broadcaster_name") or ""),
            broadcaster_id=str(context.get("broadcaster_id") or ""),
            origin_text=str(context.get("origin_text") or ""),
        )
        tab.close_requested.connect(self.close_broadcast_tab_by_lv)
        index = self.broadcast_tabs.addTab(tab, lv)
        if activate:
            self.broadcast_tabs.setCurrentIndex(index)
        show_status(self, f"コメント監視タブ追加: {lv}")
        QTimer.singleShot(0, tab.start_stream)
        return True

    def on_current_broadcast_tab_changed(self, index: int) -> None:
        tab = self.broadcast_tabs.widget(index)
        if isinstance(tab, BroadcastCommentTab):
            tab.load_archived_comments()

    def open_tab_context_menu(self, pos) -> None:
        tab_bar = self.broadcast_tabs.tabBar()
        index = tab_bar.tabAt(pos)
        if index < 0:
            return
        tab = self.broadcast_tabs.widget(index)
        if not isinstance(tab, BroadcastCommentTab):
            return
        lv = tab.lv
        url = f"https://live.nicovideo.jp/watch/{lv}"
        menu = QMenu(self)
        open_action = menu.addAction("放送ページを開く")
        copy_url_action = menu.addAction("URLをコピー")
        copy_lv_action = menu.addAction("lvIDをコピー")
        menu.addSeparator()
        close_action = menu.addAction("タブを閉じる")
        action = menu.exec(tab_bar.mapToGlobal(pos))
        if action == open_action:
            QDesktopServices.openUrl(QUrl(url))
            show_status(self, f"放送ページを開く: {lv}")
        elif action == copy_url_action:
            QApplication.clipboard().setText(url)
            show_status(self, f"URLコピー: {url}")
        elif action == copy_lv_action:
            QApplication.clipboard().setText(lv)
            show_status(self, f"lvIDコピー: {lv}")
        elif action == close_action:
            self.close_broadcast_tab(index)

    def close_broadcast_tab_by_lv(self, lv: str) -> None:
        append_app_log(f"コメント監視 close_broadcast_tab_by_lv開始: {lv}", "DEBUG")
        for index in range(self.broadcast_tabs.count()):
            tab = self.broadcast_tabs.widget(index)
            if isinstance(tab, BroadcastCommentTab) and tab.lv == lv:
                self.close_broadcast_tab(index)
                return
        append_app_log(f"コメント監視 close_broadcast_tab_by_lv対象なし: {lv}", "DEBUG")

    def close_broadcast_tab(self, index: int) -> None:
        tab = self.broadcast_tabs.widget(index)
        append_app_log(
            f"コメント監視 close_broadcast_tab開始: index={index} / "
            f"tab_type={type(tab).__name__ if tab is not None else 'None'}",
            "DEBUG",
        )
        if isinstance(tab, BroadcastCommentTab):
            removed = {"done": False}
            if tab not in self.closing_tabs:
                self.closing_tabs.append(tab)
                append_app_log(f"コメント監視 closing_tabs追加: {tab.lv} / count={len(self.closing_tabs)}", "DEBUG")

            def remove_closed_tab(tab_ref=tab) -> None:
                append_app_log(
                    f"コメント監視 remove_closed_tab開始: {tab_ref.lv} / removed={removed['done']}",
                    "DEBUG",
                )
                if removed["done"]:
                    return
                removed["done"] = True
                tab_ref.detach_stream_signals()
                current_index = self.broadcast_tabs.indexOf(tab_ref)
                append_app_log(
                    f"コメント監視 removeTab直前: {tab_ref.lv} / current_index={current_index}",
                    "DEBUG",
                )
                if current_index >= 0:
                    self.broadcast_tabs.removeTab(current_index)
                append_app_log(f"コメント監視 removeTab完了: {tab_ref.lv}", "DEBUG")
                if tab_ref in self.closing_tabs:
                    self.closing_tabs.remove(tab_ref)
                    append_app_log(f"コメント監視 closing_tabs削除: {tab_ref.lv} / count={len(self.closing_tabs)}", "DEBUG")
                append_app_log(f"コメント監視 deleteLater直前: {tab_ref.lv}", "DEBUG")
                tab_ref.deleteLater()
                append_app_log(f"コメント監視 deleteLater予約完了: {tab_ref.lv}", "DEBUG")

            def force_remove_closed_tab(tab_ref=tab) -> None:
                append_app_log(
                    f"コメント監視 force_remove_closed_tab発火: {tab_ref.lv} / removed={removed['done']} / "
                    f"running={self.stream_manager.is_running(tab_ref.lv)}",
                    "DEBUG",
                )
                if removed["done"]:
                    return
                if self.stream_manager.is_running(tab_ref.lv):
                    append_app_log(f"コメント監視タブ終了待ちタイムアウト: {tab_ref.lv}", "WARN")
                    tab_ref.force_close_stream(2000)
                remove_closed_tab(tab_ref)

            if tab.request_close(remove_closed_tab):
                append_app_log(f"コメント監視 request_close即時削除へ: {tab.lv}", "DEBUG")
                remove_closed_tab()
            else:
                append_app_log(f"コメント監視 request_close待機へ: {tab.lv}", "DEBUG")
                QTimer.singleShot(10000, force_remove_closed_tab)
            return
        if tab is not None:
            append_app_log(f"コメント監視 非Broadcastタブ removeTab直前: index={index}", "DEBUG")
            self.broadcast_tabs.removeTab(index)
            tab.deleteLater()
            append_app_log(f"コメント監視 非Broadcastタブ deleteLater予約完了: index={index}", "DEBUG")

    def close_all_streams(self) -> None:
        for index in range(self.broadcast_tabs.count()):
            tab = self.broadcast_tabs.widget(index)
            if isinstance(tab, BroadcastCommentTab):
                tab.force_close_stream(500)
        for tab in list(self.closing_tabs):
            tab.force_close_stream(500)
        self.stream_manager.stop_all(500)


class SettingsTab(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.session_driver = None

        self.account_name = QLineEdit()
        self.account_name.setPlaceholderText("アカウント名")
        self.session_value = QLineEdit()
        self.session_value.setEchoMode(QLineEdit.EchoMode.Password)
        self.session_value.setPlaceholderText("user_session")
        self.toggle_session_visibility_button = QPushButton("👁️")
        self.toggle_session_visibility_button.setFixedWidth(42)
        self.toggle_session_visibility_button.setToolTip("user_sessionの表示/非表示")
        self.toggle_session_visibility_button.clicked.connect(self.toggle_session_visibility)
        self.recorder_path = QLineEdit()
        self.recorder_path.setPlaceholderText("SlNicoLiveRec.exe")
        self.recorder_browse_button = QPushButton("参照")
        self.recorder_browse_button.clicked.connect(self.browse_recorder_path)
        self.apply_recorder_settings_button = QPushButton("推奨設定を適用")
        self.apply_recorder_settings_button.setToolTip(
            "ログイン情報を保持したまま、命名・MP4変換・自動終了設定を適用します"
        )
        self.apply_recorder_settings_button.clicked.connect(self.apply_recorder_settings)
        self.tracker_fetch_method = NoWheelComboBox()
        self.tracker_fetch_method.addItem("Seleniumで取得", "selenium")
        self.tracker_fetch_method.addItem("公式APIで取得", "api")
        self.selenium_headless = QCheckBox("Seleniumをヘッドレスで動かす")
        self.recording_auto_restart = QCheckBox("録画プロセスが終了したら自動再起動")
        self.postprocess_console_log_enabled = QCheckBox("放送終了後フローを起動元cmdにも表示する")
        self.recording_segment_seconds = NoWheelSpinBox()
        self.recording_segment_seconds.setRange(0, 24 * 60 * 60)
        self.recording_segment_seconds.setSuffix(" 秒")
        self.recording_segment_seconds.setToolTip("0なら明示分割なし。1800で30分ごとに切って再接続")
        self.recording_restart_delay_seconds = NoWheelDoubleSpinBox()
        self.recording_restart_delay_seconds.setRange(0.0, 600.0)
        self.recording_restart_delay_seconds.setDecimals(1)
        self.recording_restart_delay_seconds.setSuffix(" 秒")
        self.recording_max_restarts = NoWheelSpinBox()
        self.recording_max_restarts.setRange(0, 9999)
        self.concat_output_scale = QLineEdit()
        self.concat_output_scale.setPlaceholderText("854:-2")
        self.concat_output_fps = NoWheelSpinBox()
        self.concat_output_fps.setRange(1, 120)
        self.concat_output_fps.setSuffix(" fps")
        self.concat_output_crf = NoWheelSpinBox()
        self.concat_output_crf.setRange(0, 51)
        self.concat_video_encoder = NoWheelComboBox()
        self.concat_video_encoder.addItem("GPU h264_nvenc", "h264_nvenc")
        self.concat_video_encoder.addItem("GPU hevc_nvenc", "hevc_nvenc")
        self.concat_video_encoder.addItem("CPU libx264", "libx264")
        self.concat_nvenc_preset = NoWheelComboBox()
        self.concat_nvenc_preset.setEditable(True)
        for preset in ["p1", "p2", "p3", "p4", "p5", "p6", "p7"]:
            self.concat_nvenc_preset.addItem(preset, preset)
        self.character1_name = QLineEdit()
        self.character1_name.setPlaceholderText("ニニちゃん")
        self.character1_image_url = QLineEdit()
        self.character1_image_url.setPlaceholderText("ニニちゃん画像URL")
        self.character2_name = QLineEdit()
        self.character2_name.setPlaceholderText("ココちゃん")
        self.character2_image_url = QLineEdit()
        self.character2_image_url.setPlaceholderText("ココちゃん画像URL")
        self.summary_prompt = QTextEdit()
        self.summary_prompt.setPlaceholderText("Step05 要約プロンプト")
        self.summary_prompt.setMinimumHeight(90)
        self.summary_chunk_size = NoWheelSpinBox()
        self.summary_chunk_size.setRange(1000, 1000000)
        self.summary_chunk_size.setSingleStep(1000)
        self.summary_chunk_size.setSuffix(" 文字")
        self.summary_chunk_prompt = QTextEdit()
        self.summary_chunk_prompt.setPlaceholderText("Step05 チャンクごとの要約プロンプト")
        self.summary_chunk_prompt.setMinimumHeight(70)
        self.summary_final_prompt = QTextEdit()
        self.summary_final_prompt.setPlaceholderText("Step05 チャンク統合プロンプト")
        self.summary_final_prompt.setMinimumHeight(70)
        self.image_prompt = QTextEdit()
        self.image_prompt.setPlaceholderText("Step07 抽象画像プロンプト")
        self.image_prompt.setMinimumHeight(80)
        self.intro_conversation_prompt = QTextEdit()
        self.intro_conversation_prompt.setPlaceholderText("Step08 開始前会話プロンプト")
        self.intro_conversation_prompt.setMinimumHeight(70)
        self.outro_conversation_prompt = QTextEdit()
        self.outro_conversation_prompt.setPlaceholderText("Step08 終了後会話プロンプト")
        self.outro_conversation_prompt.setMinimumHeight(70)
        self.character1_personality = QLineEdit()
        self.character1_personality.setPlaceholderText("ニニちゃん性格")
        self.character2_personality = QLineEdit()
        self.character2_personality.setPlaceholderText("ココちゃん性格")
        self.conversation_turns = NoWheelSpinBox()
        self.conversation_turns.setRange(1, 20)
        self.enable_summary_text = QCheckBox("Step05で要約を作る")
        self.enable_ai_music = QCheckBox("Step06でAI曲生成を有効にする")
        self.enable_summary_image = QCheckBox("Step07で抽象画像を作る")
        self.enable_ai_conversation = QCheckBox("Step08でニニちゃん/ココちゃん会話を作る")
        self.enable_timeline_thumbnails = QCheckBox("タイムラインに10秒サムネを作る")
        self.timeline_thumbnail_width = NoWheelSpinBox()
        self.timeline_thumbnail_width.setRange(16, 640)
        self.timeline_thumbnail_width.setSuffix(" px")
        self.timeline_thumbnail_width.setToolTip("タイムラインサムネの生成幅。既定は80px")
        self.timeline_thumbnail_height = NoWheelSpinBox()
        self.timeline_thumbnail_height.setRange(16, 480)
        self.timeline_thumbnail_height.setSuffix(" px")
        self.timeline_thumbnail_height.setToolTip("タイムラインサムネの生成高さ。既定は60px")
        self.enable_audio_timeline = QCheckBox("音声をタイムラインに乗せる")
        self.enable_timeline_html = QCheckBox("タイムラインHTMLを作る")
        self.enable_comment_ranking = QCheckBox("コメントランキングを作る")
        self.enable_emotion_scores = QCheckBox("感情スコアを作る")
        self.enable_word_extract = QCheckBox("言葉抽出を作る")
        self.suno_api_key = QLineEdit()
        self.suno_api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.suno_api_key.setPlaceholderText("Suno API Key")
        self.suno_music_model = NoWheelComboBox()
        self.suno_music_model.setEditable(False)
        for model in tracker.DEFAULT_SUNO_MODELS:
            self.suno_music_model.addItem(model, model)
        self.refresh_suno_models_button = QPushButton("公式からモデル更新")
        self.refresh_suno_models_button.clicked.connect(self.refresh_suno_models)
        self.suno_music_style = QLineEdit()
        self.suno_music_style.setPlaceholderText("J-Pop, Upbeat")
        self.suno_music_instrumental = QCheckBox("インスト曲にする")
        self.openai_api_key = QLineEdit()
        self.openai_api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.openai_api_key.setPlaceholderText("OpenAI API Key")
        self.google_api_key = QLineEdit()
        self.google_api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.google_api_key.setPlaceholderText("Google API Key")
        self.imgur_api_key = QLineEdit()
        self.imgur_api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.imgur_api_key.setPlaceholderText("Imgur Client-ID")
        self.huggingface_token = QLineEdit()
        self.huggingface_token.setEchoMode(QLineEdit.EchoMode.Password)
        self.huggingface_token.setPlaceholderText("HuggingFace Token")
        self.image_generation_model = NoWheelComboBox()
        self.image_generation_model.setEditable(True)
        for model in ["gpt-image-2", "gpt-image-1.5", "gpt-image-1", "gpt-image-1-mini", "dall-e-3"]:
            self.image_generation_model.addItem(model, model)
        self.image_generation_quality = NoWheelComboBox()
        self.image_generation_quality.setEditable(False)
        for label, value in [("低", "low"), ("中", "medium"), ("高", "high")]:
            self.image_generation_quality.addItem(label, value)
        self.codex_exec_enabled = QCheckBox("AIテキスト生成をCodex execで実行する")
        self.codex_exec_provider = NoWheelComboBox()
        self.codex_exec_provider.addItem("Codex exec", "codex")
        self.codex_exec_provider.addItem("ClaudeCode", "claude")
        self.codex_exec_provider.addItem("Grok build", "grok")
        self.codex_exec_command = QLineEdit()
        self.codex_exec_command.setPlaceholderText("codex")
        self.codex_exec_model = QLineEdit()
        self.codex_exec_model.setPlaceholderText("例: grok-build / sonnet / gpt-5.5")
        self.codex_exec_effort = QLineEdit()
        self.codex_exec_effort.setPlaceholderText("任意")
        self.codex_exec_cwd = QLineEdit()
        self.codex_exec_cwd.setPlaceholderText(str(Path.cwd()))
        self.codex_exec_timeout_seconds = NoWheelSpinBox()
        self.codex_exec_timeout_seconds.setRange(10, 24 * 60 * 60)
        self.codex_exec_timeout_seconds.setSuffix(" 秒")
        self.enable_archive_auto_upload = QCheckBox("Step 15で自動アップロードする")
        self.archive_upload_target_id = QLineEdit()
        self.archive_upload_target_id.setPlaceholderText("lolipop-main")
        self.archive_upload_remote_dir_template = QLineEdit()
        self.archive_upload_remote_dir_template.setPlaceholderText("niconico/{account_id}")
        self.archive_upload_username = QLineEdit()
        self.archive_upload_username.setPlaceholderText("FTPユーザー名")
        self.archive_upload_password = QLineEdit()
        self.archive_upload_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.archive_upload_password.setPlaceholderText("FTPパスワード")
        self.status = QLabel("未取得")

        self.open_login_button = QPushButton("ログイン用Chromeを開く")
        self.open_login_button.clicked.connect(self.open_login_browser)
        self.read_cookie_button = QPushButton("ログイン完了後に取得")
        self.read_cookie_button.clicked.connect(self.read_session_cookie)
        self.save_button = QPushButton("全体設定を保存")
        self.save_button.clicked.connect(self.save_settings)

        box = QGroupBox("ニコニコ セッション")
        box_layout = QVBoxLayout(box)
        box_layout.addWidget(QLabel("アカウント名"))
        box_layout.addWidget(self.account_name)
        box_layout.addWidget(QLabel("user_session"))
        session_row = QWidget()
        session_row_layout = QHBoxLayout(session_row)
        session_row_layout.setContentsMargins(0, 0, 0, 0)
        session_row_layout.addWidget(self.session_value, 1)
        session_row_layout.addWidget(self.toggle_session_visibility_button)
        box_layout.addWidget(session_row)

        buttons = QWidget()
        buttons_layout = QHBoxLayout(buttons)
        buttons_layout.setContentsMargins(0, 0, 0, 0)
        buttons_layout.addWidget(self.open_login_button)
        buttons_layout.addWidget(self.read_cookie_button)
        buttons_layout.addStretch(1)
        box_layout.addWidget(buttons)
        box_layout.addWidget(self.status)

        recorder_box = QGroupBox("録画アプリ")
        recorder_layout = QVBoxLayout(recorder_box)
        recorder_layout.addWidget(QLabel("SlNicoLiveRec.exe"))
        recorder_path_row = QWidget()
        recorder_path_layout = QHBoxLayout(recorder_path_row)
        recorder_path_layout.setContentsMargins(0, 0, 0, 0)
        recorder_path_layout.addWidget(self.recorder_path, 1)
        recorder_path_layout.addWidget(self.recorder_browse_button)
        recorder_path_layout.addWidget(self.apply_recorder_settings_button)
        recorder_layout.addWidget(recorder_path_row)
        recorder_layout.addWidget(QLabel("トラッカー取得方式"))
        recorder_layout.addWidget(self.tracker_fetch_method)
        recorder_layout.addWidget(self.selenium_headless)
        recorder_layout.addWidget(self.recording_auto_restart)
        recorder_layout.addWidget(self.postprocess_console_log_enabled)
        split_row = QWidget()
        split_layout = QHBoxLayout(split_row)
        split_layout.setContentsMargins(0, 0, 0, 0)
        split_layout.addWidget(QLabel("明示分割間隔"))
        split_layout.addWidget(self.recording_segment_seconds)
        split_layout.addWidget(QLabel("再起動待ち"))
        split_layout.addWidget(self.recording_restart_delay_seconds)
        split_layout.addWidget(QLabel("最大再起動回数"))
        split_layout.addWidget(self.recording_max_restarts)
        split_layout.addStretch(1)
        recorder_layout.addWidget(split_row)
        concat_box = QGroupBox("連結出力")
        concat_layout = QHBoxLayout(concat_box)
        concat_layout.addWidget(QLabel("scale"))
        concat_layout.addWidget(self.concat_output_scale, 1)
        concat_layout.addWidget(QLabel("fps"))
        concat_layout.addWidget(self.concat_output_fps)
        concat_layout.addWidget(QLabel("CRF"))
        concat_layout.addWidget(self.concat_output_crf)
        concat_layout.addWidget(QLabel("encoder"))
        concat_layout.addWidget(self.concat_video_encoder)
        concat_layout.addWidget(QLabel("preset"))
        concat_layout.addWidget(self.concat_nvenc_preset)
        recorder_layout.addWidget(concat_box)
        character_box = QGroupBox("HTMLキャラクター")
        character_layout = QVBoxLayout(character_box)
        character_layout.addWidget(QLabel("ニニちゃん 名前"))
        character_layout.addWidget(self.character1_name)
        character_layout.addWidget(QLabel("ニニちゃん 画像URL"))
        character_layout.addWidget(self.character1_image_url)
        character_layout.addWidget(QLabel("ココちゃん 名前"))
        character_layout.addWidget(self.character2_name)
        character_layout.addWidget(QLabel("ココちゃん 画像URL"))
        character_layout.addWidget(self.character2_image_url)
        character_layout.addWidget(QLabel("ニニちゃん 性格"))
        character_layout.addWidget(self.character1_personality)
        character_layout.addWidget(QLabel("ココちゃん 性格"))
        character_layout.addWidget(self.character2_personality)
        character_layout.addWidget(QLabel("会話ターン数"))
        character_layout.addWidget(self.conversation_turns)

        prompt_box = QGroupBox("AIプロンプト")
        prompt_layout = QVBoxLayout(prompt_box)
        prompt_layout.addWidget(QLabel("Step05 要約プロンプト"))
        prompt_layout.addWidget(self.summary_prompt)
        prompt_layout.addWidget(QLabel("Step05 チャンク上限"))
        prompt_layout.addWidget(self.summary_chunk_size)
        prompt_layout.addWidget(QLabel("Step05 チャンクごとの要約プロンプト"))
        prompt_layout.addWidget(self.summary_chunk_prompt)
        prompt_layout.addWidget(QLabel("Step05 チャンク統合プロンプト"))
        prompt_layout.addWidget(self.summary_final_prompt)
        prompt_layout.addWidget(QLabel("Step07 抽象画像プロンプト"))
        prompt_layout.addWidget(self.image_prompt)
        prompt_layout.addWidget(QLabel("Step08 開始前会話プロンプト"))
        prompt_layout.addWidget(self.intro_conversation_prompt)
        prompt_layout.addWidget(QLabel("Step08 終了後会話プロンプト"))
        prompt_layout.addWidget(self.outro_conversation_prompt)

        pipeline_box = QGroupBox("放送終了後HTML生成ステップ")
        pipeline_layout = QVBoxLayout(pipeline_box)
        pipeline_layout.addWidget(self.enable_summary_text)
        pipeline_layout.addWidget(self.enable_ai_music)
        pipeline_layout.addWidget(self.enable_summary_image)
        pipeline_layout.addWidget(self.enable_ai_conversation)
        pipeline_layout.addWidget(self.enable_timeline_thumbnails)
        thumbnail_size_row = QWidget()
        thumbnail_size_layout = QHBoxLayout(thumbnail_size_row)
        thumbnail_size_layout.setContentsMargins(20, 0, 0, 0)
        thumbnail_size_layout.addWidget(QLabel("サムネサイズ"))
        thumbnail_size_layout.addWidget(self.timeline_thumbnail_width)
        thumbnail_size_layout.addWidget(QLabel("x"))
        thumbnail_size_layout.addWidget(self.timeline_thumbnail_height)
        thumbnail_size_layout.addStretch(1)
        pipeline_layout.addWidget(thumbnail_size_row)
        pipeline_layout.addWidget(self.enable_audio_timeline)
        pipeline_layout.addWidget(self.enable_timeline_html)
        pipeline_layout.addWidget(self.enable_comment_ranking)
        pipeline_layout.addWidget(self.enable_emotion_scores)
        pipeline_layout.addWidget(self.enable_word_extract)

        music_box = QGroupBox("Step06 AI曲生成 / Suno API")
        music_layout = QVBoxLayout(music_box)
        music_layout.addWidget(QLabel("Suno APIキー"))
        music_layout.addWidget(self.suno_api_key)
        music_layout.addWidget(QLabel("モデル"))
        model_row = QWidget()
        model_row_layout = QHBoxLayout(model_row)
        model_row_layout.setContentsMargins(0, 0, 0, 0)
        model_row_layout.addWidget(self.suno_music_model, 1)
        model_row_layout.addWidget(self.refresh_suno_models_button)
        music_layout.addWidget(model_row)
        music_layout.addWidget(QLabel("曲調/style"))
        music_layout.addWidget(self.suno_music_style)
        music_layout.addWidget(self.suno_music_instrumental)

        image_box = QGroupBox("Step07 抽象画像生成")
        image_layout = QVBoxLayout(image_box)
        image_layout.addWidget(QLabel("OpenAI APIキー"))
        image_layout.addWidget(self.openai_api_key)
        image_layout.addWidget(QLabel("画像生成モデル"))
        image_layout.addWidget(self.image_generation_model)
        image_layout.addWidget(QLabel("品質"))
        image_layout.addWidget(self.image_generation_quality)
        image_layout.addWidget(QLabel("Imgur Client-ID"))
        image_layout.addWidget(self.imgur_api_key)

        whisperx_box = QGroupBox("WhisperX / 話者分離")
        whisperx_layout = QVBoxLayout(whisperx_box)
        whisperx_layout.addWidget(QLabel("HuggingFace Token"))
        whisperx_layout.addWidget(self.huggingface_token)

        codex_box = QGroupBox("AIテキスト生成")
        codex_layout = QVBoxLayout(codex_box)
        codex_layout.addWidget(self.codex_exec_enabled)
        codex_layout.addWidget(QLabel("Google APIキー"))
        codex_layout.addWidget(self.google_api_key)
        codex_layout.addWidget(QLabel("AI CLI"))
        codex_layout.addWidget(self.codex_exec_provider)
        codex_layout.addWidget(QLabel("Codexコマンド"))
        codex_layout.addWidget(self.codex_exec_command)
        codex_layout.addWidget(QLabel("モデル"))
        codex_layout.addWidget(self.codex_exec_model)
        codex_layout.addWidget(QLabel("Effort"))
        codex_layout.addWidget(self.codex_exec_effort)
        codex_layout.addWidget(QLabel("作業ディレクトリ"))
        codex_layout.addWidget(self.codex_exec_cwd)
        codex_layout.addWidget(QLabel("タイムアウト"))
        codex_layout.addWidget(self.codex_exec_timeout_seconds)

        upload_box = QGroupBox("Step 15 アップロードサーバー")
        upload_layout = QVBoxLayout(upload_box)
        upload_layout.addWidget(self.enable_archive_auto_upload)
        upload_layout.addWidget(QLabel("接続先ID"))
        upload_layout.addWidget(self.archive_upload_target_id)
        upload_layout.addWidget(QLabel("リモート保存先"))
        upload_layout.addWidget(self.archive_upload_remote_dir_template)
        upload_layout.addWidget(QLabel("FTPユーザー名"))
        upload_layout.addWidget(self.archive_upload_username)
        upload_layout.addWidget(QLabel("FTPパスワード"))
        upload_layout.addWidget(self.archive_upload_password)

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.addWidget(box)
        content_layout.addWidget(recorder_box)
        content_layout.addWidget(character_box)
        content_layout.addWidget(prompt_box)
        content_layout.addWidget(pipeline_box)
        content_layout.addWidget(music_box)
        content_layout.addWidget(image_box)
        content_layout.addWidget(whisperx_box)
        content_layout.addWidget(codex_box)
        content_layout.addWidget(upload_box)
        content_layout.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(content)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        save_bar = QWidget()
        save_bar_layout = QHBoxLayout(save_bar)
        save_bar_layout.setContentsMargins(8, 8, 8, 6)
        save_bar_layout.addWidget(self.save_button)
        save_bar_layout.addStretch(1)
        layout.addWidget(save_bar)
        layout.addWidget(scroll)
        self.load_config_settings()
        self.load_saved_session()

    def masked_session_label(self, value: str) -> str:
        if not value:
            return "●●●●"
        return "●" * min(max(len(value), 8), 32)

    def load_saved_session(self) -> None:
        try:
            with tracker.connect() as conn:
                row = conn.execute(
                    """
                    SELECT account_name, user_session
                    FROM niconico_sessions
                    ORDER BY updated_at DESC, id DESC
                    LIMIT 1
                    """
                ).fetchone()
            if not row:
                return
            self.account_name.setText(str(row["account_name"] or ""))
            value = str(row["user_session"] or "")
            self.session_value.setText(value)
            self.session_value.setEchoMode(QLineEdit.EchoMode.Password)
            self.toggle_session_visibility_button.setText("👁️")
            self.status.setText("保存済みuser_session読込済み")
        except Exception as exc:
            self.status.setText(f"保存済みuser_session読込失敗: {exc}")

    def load_config_settings(self) -> None:
        try:
            config = tracker.load_config()
            self.recorder_path.setText(config.slnico_live_rec_exe)
            self.set_combo_value(self.tracker_fetch_method, config.tracker_fetch_method)
            self.selenium_headless.setChecked(bool(config.selenium_headless))
            self.recording_auto_restart.setChecked(bool(config.recording_auto_restart))
            self.postprocess_console_log_enabled.setChecked(bool(config.postprocess_console_log_enabled))
            self.recording_segment_seconds.setValue(int(config.recording_segment_seconds or 0))
            self.recording_restart_delay_seconds.setValue(float(config.recording_restart_delay_seconds or 0.0))
            self.recording_max_restarts.setValue(int(config.recording_max_restarts or 0))
            self.concat_output_scale.setText(config.concat_output_scale or "854:-2")
            self.concat_output_fps.setValue(int(config.concat_output_fps or 15))
            self.concat_output_crf.setValue(int(config.concat_output_crf or 28))
            self.set_combo_value(self.concat_video_encoder, config.concat_video_encoder or "h264_nvenc")
            self.set_combo_value(self.concat_nvenc_preset, config.concat_nvenc_preset or "p4")
            self.character1_name.setText(config.character1_name)
            self.character1_image_url.setText(config.character1_image_url)
            self.character2_name.setText(config.character2_name)
            self.character2_image_url.setText(config.character2_image_url)
            self.summary_prompt.setPlainText(config.summary_prompt)
            self.summary_chunk_size.setValue(int(config.summary_chunk_size or 100000))
            self.summary_chunk_prompt.setPlainText(config.summary_chunk_prompt)
            self.summary_final_prompt.setPlainText(config.summary_final_prompt)
            self.image_prompt.setPlainText(config.image_prompt)
            self.intro_conversation_prompt.setPlainText(config.intro_conversation_prompt)
            self.outro_conversation_prompt.setPlainText(config.outro_conversation_prompt)
            self.character1_personality.setText(config.character1_personality)
            self.character2_personality.setText(config.character2_personality)
            self.conversation_turns.setValue(int(config.conversation_turns or 5))
            self.enable_summary_text.setChecked(bool(config.enable_summary_text))
            self.enable_ai_music.setChecked(bool(config.enable_ai_music))
            self.enable_summary_image.setChecked(bool(config.enable_summary_image))
            self.enable_ai_conversation.setChecked(bool(config.enable_ai_conversation))
            self.enable_timeline_thumbnails.setChecked(bool(config.enable_timeline_thumbnails))
            self.timeline_thumbnail_width.setValue(int(config.timeline_thumbnail_width or 80))
            self.timeline_thumbnail_height.setValue(int(config.timeline_thumbnail_height or 60))
            self.enable_audio_timeline.setChecked(bool(config.enable_audio_timeline))
            self.enable_timeline_html.setChecked(bool(config.enable_timeline_html))
            self.enable_comment_ranking.setChecked(bool(config.enable_comment_ranking))
            self.enable_emotion_scores.setChecked(bool(config.enable_emotion_scores))
            self.enable_word_extract.setChecked(bool(config.enable_word_extract))
            self.suno_api_key.setText(config.suno_api_key)
            self.set_combo_value(self.suno_music_model, config.suno_music_model)
            self.suno_music_style.setText(config.suno_music_style)
            self.suno_music_instrumental.setChecked(bool(config.suno_music_instrumental))
            self.openai_api_key.setText(config.openai_api_key)
            self.google_api_key.setText(config.google_api_key)
            self.imgur_api_key.setText(config.imgur_api_key)
            self.huggingface_token.setText(config.huggingface_token)
            self.set_combo_text(self.image_generation_model, config.image_generation_model)
            quality_index = self.image_generation_quality.findData(config.image_generation_quality)
            if quality_index >= 0:
                self.image_generation_quality.setCurrentIndex(quality_index)
            self.codex_exec_enabled.setChecked(bool(config.codex_exec_enabled))
            self.set_combo_value(self.codex_exec_provider, config.codex_exec_provider)
            self.codex_exec_command.setText(config.codex_exec_command)
            self.codex_exec_model.setText(config.codex_exec_model)
            self.codex_exec_effort.setText(config.codex_exec_effort)
            self.codex_exec_cwd.setText(config.codex_exec_cwd)
            self.codex_exec_timeout_seconds.setValue(int(config.codex_exec_timeout_seconds or 3600))
            self.enable_archive_auto_upload.setChecked(bool(config.enable_archive_auto_upload))
            self.archive_upload_target_id.setText(config.archive_upload_target_id)
            self.archive_upload_remote_dir_template.setText(config.archive_upload_remote_dir_template)
            self.archive_upload_username.setText(config.archive_upload_username)
            self.archive_upload_password.setText(config.archive_upload_password)
        except Exception as exc:
            self.status.setText(f"録画設定読込失敗: {exc}")

    def browse_recorder_path(self) -> None:
        current = self.recorder_path.text().strip()
        start_dir = str(Path(current).parent) if current else str(Path.home())
        path, _ = QFileDialog.getOpenFileName(
            self,
            "録画アプリを選択",
            start_dir,
            "Executable (*.exe);;All Files (*)",
        )
        if path:
            self.recorder_path.setText(path)

    def apply_recorder_settings(self) -> None:
        try:
            config_path = tracker.apply_recommended_slnico_settings(
                self.recorder_path.text().strip()
            )
            self.status.setText(f"SlNicoLiveRec推奨設定を適用: {config_path}")
            QMessageBox.information(
                self,
                "SlNicoLiveRec",
                "推奨設定を適用しました。\nログイン情報は変更していません。",
            )
        except Exception as exc:
            self.status.setText(f"SlNicoLiveRec設定適用失敗: {exc}")
            QMessageBox.warning(self, "SlNicoLiveRec", str(exc))

    def toggle_session_visibility(self) -> None:
        if self.session_value.echoMode() == QLineEdit.EchoMode.Password:
            self.session_value.setEchoMode(QLineEdit.EchoMode.Normal)
            self.toggle_session_visibility_button.setText("隠す")
        else:
            self.session_value.setEchoMode(QLineEdit.EchoMode.Password)
            self.toggle_session_visibility_button.setText("👁️")

    def set_combo_value(self, combo: QComboBox, value: str) -> None:
        index = combo.findData(value)
        combo.setCurrentIndex(index if index >= 0 else 0)

    def set_combo_text(self, combo: QComboBox, value: str) -> None:
        value = str(value or "").strip()
        index = combo.findText(value)
        if index >= 0:
            combo.setCurrentIndex(index)
        elif value:
            combo.addItem(value, value)
            combo.setCurrentIndex(combo.count() - 1)

    def refresh_suno_models(self) -> None:
        self.refresh_suno_models_button.setEnabled(False)
        self.refresh_suno_models_button.setText("取得中")
        current = str(self.suno_music_model.currentData() or self.suno_music_model.currentText() or "")
        self._suno_model_fetch_job = SunoModelFetchJob()
        self._suno_model_fetch_job.signals.finished.connect(lambda models: self.apply_suno_models(models, current))
        self._suno_model_fetch_job.signals.failed.connect(self.suno_model_fetch_failed)
        QThreadPool.globalInstance().start(self._suno_model_fetch_job)

    def apply_suno_models(self, models: list[str], preferred: str = "") -> None:
        cleaned = []
        for model in models:
            value = str(model).strip()
            if value and value not in cleaned:
                cleaned.append(value)
        if not cleaned:
            cleaned = list(tracker.DEFAULT_SUNO_MODELS)
        self.suno_music_model.clear()
        for model in cleaned:
            self.suno_music_model.addItem(model, model)
        self.set_combo_value(self.suno_music_model, preferred or cleaned[0])
        self.refresh_suno_models_button.setEnabled(True)
        self.refresh_suno_models_button.setText("公式からモデル更新")
        self.status.setText(f"Sunoモデル取得: {len(cleaned)}件")

    def suno_model_fetch_failed(self, error: str) -> None:
        self.apply_suno_models(list(tracker.DEFAULT_SUNO_MODELS), str(self.suno_music_model.currentData() or ""))
        self.status.setText(f"Sunoモデル取得失敗。既知候補を表示: {error}")

    def open_login_browser(self) -> None:
        try:
            self.close_driver()
            options = Options()
            options.add_experimental_option("detach", True)
            self.session_driver = webdriver.Chrome(options=options)
            self.session_driver.get("https://account.nicovideo.jp/login")
            self.status.setText("Chromeでログインしてから「ログイン完了後に取得」を押す")
        except Exception as exc:
            self.status.setText(f"Chrome起動失敗: {exc}")

    def read_session_cookie(self) -> None:
        try:
            if not self.session_driver:
                self.status.setText("先にログイン用Chromeを開いてくれ")
                return
            self.session_driver.get("https://www.nicovideo.jp/")
            cookies = self.session_driver.get_cookies()
            for cookie in cookies:
                if cookie.get("name") == "user_session":
                    value = str(cookie.get("value") or "")
                    self.session_value.setText(value)
                    self.session_value.setEchoMode(QLineEdit.EchoMode.Password)
                    self.toggle_session_visibility_button.setText("👁️")
                    self.status.setText("user_session取得済み")
                    return
            self.status.setText("user_sessionが見つからない。ログイン状態を確認してくれ")
        except Exception as exc:
            self.status.setText(f"user_session取得失敗: {exc}")

    def save_settings(self) -> None:
        if not self.save_recorder_settings():
            return
        self.save_session()

    def save_recorder_settings(self) -> bool:
        path = self.recorder_path.text().strip()
        if not path:
            self.status.setText("保存できない: 録画アプリのパスが空")
            return False
        if not Path(path).exists():
            self.status.setText(f"保存できない: 録画アプリが見つからない {path}")
            return False
        tracker.save_config_values(
            {
                "slnico_live_rec_exe": path,
                "tracker_fetch_method": str(self.tracker_fetch_method.currentData() or "api"),
                "selenium_headless": self.selenium_headless.isChecked(),
                "recording_auto_restart": self.recording_auto_restart.isChecked(),
                "postprocess_console_log_enabled": self.postprocess_console_log_enabled.isChecked(),
                "recording_segment_seconds": int(self.recording_segment_seconds.value()),
                "recording_restart_delay_seconds": float(self.recording_restart_delay_seconds.value()),
                "recording_max_restarts": int(self.recording_max_restarts.value()),
                "concat_output_scale": self.concat_output_scale.text().strip() or "854:-2",
                "concat_output_fps": int(self.concat_output_fps.value()),
                "concat_output_crf": int(self.concat_output_crf.value()),
                "concat_video_encoder": str(self.concat_video_encoder.currentData() or self.concat_video_encoder.currentText() or "h264_nvenc"),
                "concat_nvenc_preset": str(self.concat_nvenc_preset.currentText() or "p4").strip() or "p4",
                "character1_name": self.character1_name.text().strip() or tracker.DEFAULT_CHARACTER1_NAME,
                "character1_image_url": self.character1_image_url.text().strip(),
                "character2_name": self.character2_name.text().strip() or tracker.DEFAULT_CHARACTER2_NAME,
                "character2_image_url": self.character2_image_url.text().strip(),
                "summary_prompt": self.summary_prompt.toPlainText().strip(),
                "summary_chunk_size": int(self.summary_chunk_size.value()),
                "summary_chunk_prompt": self.summary_chunk_prompt.toPlainText().strip(),
                "summary_final_prompt": self.summary_final_prompt.toPlainText().strip(),
                "image_prompt": self.image_prompt.toPlainText().strip(),
                "intro_conversation_prompt": self.intro_conversation_prompt.toPlainText().strip(),
                "outro_conversation_prompt": self.outro_conversation_prompt.toPlainText().strip(),
                "character1_personality": self.character1_personality.text().strip(),
                "character2_personality": self.character2_personality.text().strip(),
                "conversation_turns": int(self.conversation_turns.value()),
                "enable_summary_text": self.enable_summary_text.isChecked(),
                "enable_ai_music": self.enable_ai_music.isChecked(),
                "enable_summary_image": self.enable_summary_image.isChecked(),
                "enable_ai_conversation": self.enable_ai_conversation.isChecked(),
                "enable_timeline_thumbnails": self.enable_timeline_thumbnails.isChecked(),
                "timeline_thumbnail_width": int(self.timeline_thumbnail_width.value()),
                "timeline_thumbnail_height": int(self.timeline_thumbnail_height.value()),
                "enable_audio_timeline": self.enable_audio_timeline.isChecked(),
                "enable_timeline_html": self.enable_timeline_html.isChecked(),
                "enable_comment_ranking": self.enable_comment_ranking.isChecked(),
                "enable_emotion_scores": self.enable_emotion_scores.isChecked(),
                "enable_word_extract": self.enable_word_extract.isChecked(),
                "suno_api_key": self.suno_api_key.text().strip(),
                "suno_music_model": str(self.suno_music_model.currentData() or self.suno_music_model.currentText() or "V4"),
                "suno_music_style": self.suno_music_style.text().strip() or "J-Pop, Upbeat",
                "suno_music_instrumental": self.suno_music_instrumental.isChecked(),
                "openai_api_key": self.openai_api_key.text().strip(),
                "google_api_key": self.google_api_key.text().strip(),
                "imgur_api_key": self.imgur_api_key.text().strip(),
                "huggingface_token": self.huggingface_token.text().strip(),
                "image_generation_model": str(self.image_generation_model.currentText() or "gpt-image-2").strip(),
                "image_generation_quality": str(self.image_generation_quality.currentData() or "medium"),
                "codex_exec_enabled": self.codex_exec_enabled.isChecked(),
                "codex_exec_provider": str(self.codex_exec_provider.currentData()),
                "codex_exec_command": self.codex_exec_command.text().strip() or "codex",
                "codex_exec_model": self.codex_exec_model.text().strip(),
                "codex_exec_effort": self.codex_exec_effort.text().strip(),
                "codex_exec_cwd": self.codex_exec_cwd.text().strip() or str(Path.cwd()),
                "codex_exec_timeout_seconds": int(self.codex_exec_timeout_seconds.value()),
                "enable_archive_auto_upload": self.enable_archive_auto_upload.isChecked(),
                "archive_upload_target_id": self.archive_upload_target_id.text().strip() or "lolipop-main",
                "archive_upload_remote_dir_template": self.archive_upload_remote_dir_template.text().strip() or "niconico/{account_id}",
                "archive_upload_username": self.archive_upload_username.text(),
                "archive_upload_password": self.archive_upload_password.text(),
            }
        )
        window = self.window()
        tracker_tab = getattr(window, "tracker_tab", None)
        if tracker_tab is not None:
            tracker_tab.config = tracker.load_config()
            tracker_tab.countdown.setRange(0, max(1, tracker_tab.config.poll_seconds))
        return True

    def save_session(self) -> None:
        account_name = self.account_name.text().strip() or "default"
        user_session = self.session_value.text().strip()
        if not user_session:
            self.status.setText("録画設定を保存。user_sessionは空なので保存してない")
            return
        current_time = tracker.now()
        with tracker.connect() as conn:
            conn.execute(
                """
                INSERT INTO niconico_sessions (account_name, user_session, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                (account_name, user_session, current_time, current_time),
            )
            conn.commit()
        self.status.setText(f"保存済み: {account_name}")

    def close_driver(self) -> None:
        if self.session_driver:
            try:
                self.session_driver.quit()
            except Exception:
                pass
            self.session_driver = None


class SimpleDictTableModel(QAbstractTableModel):
    def __init__(self, columns: list[tuple[str, str]]) -> None:
        super().__init__()
        self.columns = columns
        self._rows: list[dict[str, Any]] = []
        self.column_filters: dict[str, str] = {}

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.columns)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid() or role not in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole):
            return None
        value = self._rows[index.row()].get(self.columns[index.column()][0])
        return "" if value is None else str(value)

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal:
            key, label = self.columns[section]
            return f"{label} *" if self.column_filters.get(key) else label
        return section + 1

    def update_rows(self, rows: list[dict[str, Any]]) -> None:
        self.beginResetModel()
        self._rows = rows
        self.endResetModel()

    def set_column_filters(self, filters: dict[str, str]) -> None:
        self.column_filters = filters
        if self.columns:
            self.headerDataChanged.emit(Qt.Orientation.Horizontal, 0, len(self.columns) - 1)


class LinkedBroadcasterFilterModel(QAbstractTableModel):
    columns = [
        ("visible", "表示"),
        ("broadcaster_name", "配信者"),
        ("broadcaster_id", "ID"),
        ("speech_count", "発言"),
        ("broadcast_count", "配信"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._rows: list[dict[str, Any]] = []

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.columns)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None
        key = self.columns[index.column()][0]
        row = self._rows[index.row()]
        if key == "visible" and role == Qt.ItemDataRole.CheckStateRole:
            return Qt.CheckState.Checked if row.get("visible", True) else Qt.CheckState.Unchecked
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole):
            if key == "visible":
                return ""
            value = row.get(key)
            return "" if value is None else str(value)
        return None

    def setData(self, index: QModelIndex, value: Any, role: int = Qt.ItemDataRole.EditRole) -> bool:
        if not index.isValid() or self.columns[index.column()][0] != "visible":
            return False
        if role != Qt.ItemDataRole.CheckStateRole:
            return False
        checked_value = value.value if isinstance(value, Qt.CheckState) else value
        self._rows[index.row()]["visible"] = checked_value == Qt.CheckState.Checked.value
        self.dataChanged.emit(index, index, [Qt.ItemDataRole.CheckStateRole])
        return True

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if self.columns[index.column()][0] == "visible":
            flags |= Qt.ItemFlag.ItemIsUserCheckable
        return flags

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal:
            if section < 0 or section >= len(self.columns):
                return None
            return self.columns[section][1]
        return section + 1

    def update_rows(self, rows: list[dict[str, Any]]) -> None:
        self.beginResetModel()
        self._rows = [
            {
                "visible": bool(row.get("visible", True)),
                "broadcaster_id": str(row.get("broadcaster_id") or ""),
                "broadcaster_name": str(row.get("broadcaster_name") or ""),
                "speech_count": int(row.get("speech_count") or 0),
                "broadcast_count": int(row.get("broadcast_count") or 0),
            }
            for row in rows
        ]
        self.endResetModel()

    def checked_broadcaster_ids(self) -> list[str]:
        return [
            str(row.get("broadcaster_id") or "")
            for row in self._rows
            if row.get("visible", True) and str(row.get("broadcaster_id") or "")
        ]

    def broadcaster_id_at(self, row: int) -> str:
        if row < 0 or row >= len(self._rows):
            return ""
        return str(self._rows[row].get("broadcaster_id") or "")

    def set_all_checked(self, checked: bool) -> None:
        if not self._rows:
            return
        for row in self._rows:
            row["visible"] = checked
        top_left = self.index(0, 0)
        bottom_right = self.index(len(self._rows) - 1, 0)
        self.dataChanged.emit(top_left, bottom_right, [Qt.ItemDataRole.CheckStateRole])


class DateRangeCheckableModel(QAbstractTableModel):
    def date_bounds(self) -> tuple[QDate | None, QDate | None]:
        dates: list[QDate] = []
        for row in getattr(self, "_rows", []):
            row_date = qdate_from_unix_seconds(row.get("time_value"))
            if row_date:
                dates.append(row_date)
        if not dates:
            return None, None
        return min(dates), max(dates)

    def set_checked_by_date_range(self, start_date: QDate, end_date: QDate) -> None:
        rows = getattr(self, "_rows", [])
        if not rows:
            return
        if start_date > end_date:
            start_date, end_date = end_date, start_date
        for row in rows:
            row_date = qdate_from_unix_seconds(row.get("time_value"))
            row["visible"] = bool(row_date and start_date <= row_date <= end_date)
        top_left = self.index(0, 0)
        bottom_right = self.index(len(rows) - 1, 0)
        self.dataChanged.emit(top_left, bottom_right, [Qt.ItemDataRole.CheckStateRole])


class BroadcastTwoLineDelegate(QStyledItemDelegate):
    def __init__(self, table: QTableView) -> None:
        super().__init__(table)
        self.table = table

    def paint(self, painter: QPainter, option, index: QModelIndex) -> None:
        is_detail = bool(getattr(index.model(), "is_detail_row", lambda _row: False)(index.row()))
        if not is_detail:
            super().paint(painter, option, index)
            return
        if index.column() != 0:
            return
        painter.save()
        painter.setClipRect(self.table.viewport().rect())
        rect = option.rect
        rect.setWidth(max(0, self.table.viewport().width() - rect.x()))
        painter.fillRect(rect, QColor(28, 28, 30))
        painter.setPen(QColor(65, 65, 68))
        painter.drawLine(rect.topLeft(), rect.topRight())
        parts = [part.strip() for part in str(index.data(Qt.ItemDataRole.DisplayRole) or "").split("   ") if part.strip()]
        metrics = painter.fontMetrics()
        x = rect.left() + 7
        y = rect.top() + 4
        for part in parts:
            enabled = part.startswith("☑")
            label = part[1:].strip()
            width = metrics.horizontalAdvance(label) + 28
            badge = rect.adjusted(0, 0, 0, 0)
            badge.setLeft(x)
            badge.setTop(y)
            badge.setWidth(width)
            badge.setHeight(23)
            painter.setPen(QColor(71, 176, 115) if enabled else QColor(93, 93, 98))
            painter.setBrush(QColor(34, 78, 53) if enabled else QColor(42, 42, 45))
            painter.drawRoundedRect(badge, 5, 5)
            painter.setPen(QColor(225, 255, 235) if enabled else QColor(155, 155, 160))
            painter.drawText(
                badge.adjusted(8, 0, -7, 0),
                int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                ("✓ " if enabled else "− ") + label,
            )
            x += width + 6
        painter.restore()

    def sizeHint(self, option, index: QModelIndex) -> QSize:
        if bool(getattr(index.model(), "is_detail_row", lambda _row: False)(index.row())):
            return QSize(0, 31)
        return super().sizeHint(option, index)


class BroadcastFilterModel(DateRangeCheckableModel):
    columns = [
        ("visible", "表示"),
        ("lv", "LV"),
        ("title", "タイトル"),
        ("detected_at", "検出"),
    ]

    generation_labels = [
        ("10秒サムネ", "サムネ"),
        ("音声タイムライン", "音声"),
        ("タイムラインHTML", "HTML"),
        ("ランキング", "ランク"),
        ("ニニココ会話", "会話"),
        ("要約", "要約"),
        ("曲", "曲"),
        ("抽象画像", "画像"),
        ("感情スコア", "感情"),
        ("言葉抽出", "言葉"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._rows: list[dict[str, Any]] = []
        self.show_details = True
        self.show_tags = False

    def row_stride(self) -> int:
        return 1 + int(self.show_details) + int(self.show_tags)

    def is_detail_row(self, row: int) -> bool:
        return row % self.row_stride() != 0

    def is_tag_row(self, row: int) -> bool:
        return self.show_tags and row % self.row_stride() == 1 + int(self.show_details)

    def source_row(self, row: int) -> int:
        return row // self.row_stride()

    def set_show_details(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if self.show_details == enabled:
            return
        self.beginResetModel()
        self.show_details = enabled
        self.endResetModel()

    def set_show_tags(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if self.show_tags == enabled:
            return
        self.beginResetModel()
        self.show_tags = enabled
        self.endResetModel()

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows) * self.row_stride()

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.columns)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None
        source_row = self.source_row(index.row())
        detail_row = self.is_detail_row(index.row())
        key = self.columns[index.column()][0]
        row = self._rows[source_row]
        if detail_row:
            if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole) and index.column() == 0:
                if self.is_tag_row(index.row()):
                    tags = [tag.strip() for tag in str(row.get("tags") or "").split("/") if tag.strip()]
                    return "   ".join(f"☑ #{tag}" for tag in tags) if tags else "☐ タグなし"
                generated = {
                    item.strip() for item in str(row.get("generated_elements") or "").split("/")
                    if item.strip() and item.strip() != "なし"
                }
                parts = [f"{'☑' if row.get('html_uploaded') == '済' else '☐'} 公開"]
                parts.extend(
                    f"{'☑' if source_label in generated else '☐'} {display_label}"
                    for source_label, display_label in self.generation_labels
                )
                return "   ".join(parts)
            return None
        if key == "visible" and role == Qt.ItemDataRole.CheckStateRole:
            return Qt.CheckState.Checked if row.get("visible", True) else Qt.CheckState.Unchecked
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole):
            if key == "visible":
                return ""
            value = row.get(key)
            return "" if value is None else str(value)
        return None

    def setData(self, index: QModelIndex, value: Any, role: int = Qt.ItemDataRole.EditRole) -> bool:
        if not index.isValid() or self.is_detail_row(index.row()) or self.columns[index.column()][0] != "visible":
            return False
        if role != Qt.ItemDataRole.CheckStateRole:
            return False
        checked_value = value.value if isinstance(value, Qt.CheckState) else value
        self._rows[self.source_row(index.row())]["visible"] = checked_value == Qt.CheckState.Checked.value
        self.dataChanged.emit(index, index, [Qt.ItemDataRole.CheckStateRole])
        return True

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        if self.is_detail_row(index.row()):
            return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if self.columns[index.column()][0] == "visible":
            flags |= Qt.ItemFlag.ItemIsUserCheckable
        return flags

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal:
            return self.columns[section][1]
        return section + 1

    def update_rows(self, rows: list[dict[str, Any]]) -> None:
        self.beginResetModel()
        self._rows = [
            {
                "visible": bool(row.get("visible", True)),
                "lv": str(row.get("lv") or ""),
                "title": str(row.get("title") or ""),
                "detected_at": str(row.get("detected_at") or ""),
                "time_value": row.get("time_value"),
                "html_path": str(row.get("html_path") or ""),
                "html_uploaded": str(row.get("html_uploaded") or ""),
                "generated_elements": str(row.get("generated_elements") or ""),
                "tags": str(row.get("tags") or ""),
            }
            for row in rows
        ]
        self.endResetModel()

    def checked_lvs(self) -> list[str]:
        return [
            str(row.get("lv") or "")
            for row in self._rows
            if row.get("visible", True) and str(row.get("lv") or "")
        ]

    def row_at(self, row: int) -> dict[str, Any]:
        source_row = self.source_row(row)
        if source_row < 0 or source_row >= len(self._rows):
            return {}
        return dict(self._rows[source_row])

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal:
            if section < 0 or section >= len(self.columns):
                return None
            return self.columns[section][1]
        return section + 1

    def set_all_checked(self, checked: bool) -> None:
        if not self._rows:
            return
        for row in self._rows:
            row["visible"] = checked
        top_left = self.index(0, 0)
        bottom_right = self.index(self.rowCount() - 1, 0)
        self.dataChanged.emit(top_left, bottom_right, [Qt.ItemDataRole.CheckStateRole])


class InspectionTab(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        self._broadcaster_summary_rows: list[dict[str, Any]] = []
        self._broadcaster_transcript_rows: list[dict[str, Any]] = []
        self._broadcaster_comment_rows: list[dict[str, Any]] = []
        self.special_combo = NoWheelComboBox()
        self.broadcaster_combo = NoWheelComboBox()
        self.special_combo.setMinimumWidth(240)
        self.broadcaster_combo.setMinimumWidth(240)
        self.special_combo.setMaximumWidth(420)
        self.broadcaster_combo.setMaximumWidth(420)
        self.special_combo.setMinimumContentsLength(12)
        self.broadcaster_combo.setMinimumContentsLength(12)
        self.special_combo.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.broadcaster_combo.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.special_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.broadcaster_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.refresh_button = QPushButton("更新")
        self.refresh_button.clicked.connect(self.reload)
        self.special_combo.currentIndexChanged.connect(self.on_special_changed)
        self.broadcaster_combo.currentIndexChanged.connect(self.reload_broadcaster_detail)

        self.special_comments_model = SimpleDictTableModel([
            ("lv", "LV"), ("broadcaster_name", "配信者"), ("broadcaster_id", "配信者ID"),
            ("no", "No"), ("broadcast_seconds", "秒"), ("text", "コメント"), ("posted_at", "投稿時刻"),
        ])
        self.special_hits_model = SimpleDictTableModel([
            ("route", "種類"), ("lv", "LV"), ("broadcaster_name", "配信者"),
            ("broadcaster_id", "配信者ID"), ("first_comment_text", "初回コメント"),
            ("comment_count", "件数"), ("detected_at", "検出時刻"), ("html_uploaded_at", "送信"),
        ])
        self.broadcaster_programs_model = SimpleDictTableModel([
            ("lv", "LV"), ("title", "タイトル"), ("begin_time_text", "開始"),
            ("end_time_text", "終了"), ("watch_url", "URL"), ("html_path", "HTML"),
        ])
        self.broadcaster_comments_model = SimpleDictTableModel([
            ("user_id", "ユーザーID"), ("user_name", "名前"), ("lv", "LV"),
            ("no", "No"), ("broadcast_seconds", "秒"), ("text", "コメント"), ("posted_at", "投稿時刻"),
        ])
        self.special_broadcast_comments_model = SimpleDictTableModel([
            ("no", "No"), ("broadcast_seconds", "秒"), ("posted_at", "投稿時刻"),
            ("user_id", "ユーザーID"), ("user_name", "名前"), ("text", "コメント"),
        ])
        self.broadcaster_summary_model = SimpleDictTableModel([
            ("lv", "LV"), ("title", "タイトル"), ("begin_time_text", "開始"), ("summary", "要約"),
        ])
        self.broadcaster_transcript_model = SimpleDictTableModel([
            ("lv", "LV"), ("time_range", "時間"), ("speaker", "話者"), ("emotion_score", "感情"),
            ("text_length", "文字数"), ("text", "文字起こし"),
        ])
        self.broadcaster_summary_filter_inputs: dict[str, str] = {}
        self.broadcaster_transcript_filter_inputs: dict[str, str] = {}
        self.broadcaster_comments_filter_inputs: dict[str, str] = {}

        self.special_comments_table = self.make_table(self.special_comments_model)
        self.special_hits_table = self.make_table(self.special_hits_model)
        self.broadcaster_programs_table = self.make_table(self.broadcaster_programs_model)
        self.broadcaster_comments_table = self.make_table(self.broadcaster_comments_model)
        self.special_broadcast_comments_table = self.make_table(self.special_broadcast_comments_model)
        self.broadcaster_summary_table = self.make_table(self.broadcaster_summary_model)
        self.broadcaster_summary_table.clicked.connect(
            lambda index: show_table_text_popup(
                self.broadcaster_summary_table,
                index,
                text_key="summary",
                title_keys=("title", "lv", "begin_time_text"),
            )
        )
        self.broadcaster_transcript_table = self.make_table(self.broadcaster_transcript_model)
        attach_header_column_filter_menu(
            self.broadcaster_summary_table,
            self.broadcaster_summary_filter_inputs,
            on_changed=self.apply_broadcaster_content_column_filters,
        )
        attach_header_column_filter_menu(
            self.broadcaster_transcript_table,
            self.broadcaster_transcript_filter_inputs,
            on_changed=self.apply_broadcaster_content_column_filters,
        )
        attach_header_column_filter_menu(
            self.broadcaster_comments_table,
            self.broadcaster_comments_filter_inputs,
            on_changed=self.apply_broadcaster_content_column_filters,
        )
        self.linked_broadcaster_filter_model = LinkedBroadcasterFilterModel()
        self.linked_broadcaster_filter_model.dataChanged.connect(lambda *_args: self.reload_special_detail())
        self.linked_broadcaster_filter_table = self.make_table(self.linked_broadcaster_filter_model)
        self.linked_broadcaster_filter_table.setMaximumWidth(520)
        self.linked_broadcaster_filter_table.setMinimumWidth(420)
        self.linked_broadcaster_filter_table.setColumnWidth(0, 55)
        self.linked_broadcaster_filter_table.setColumnWidth(1, 180)
        self.linked_broadcaster_filter_table.setColumnWidth(2, 110)
        self.linked_broadcaster_filter_table.setColumnWidth(3, 70)
        self.linked_broadcaster_filter_table.setColumnWidth(4, 70)
        self.linked_broadcaster_filter_table.doubleClicked.connect(self.open_broadcaster_broadcast_filter)
        self.linked_check_all_button = QPushButton("全チェック")
        self.linked_check_all_button.clicked.connect(lambda: self.set_linked_broadcasters_checked(True))
        self.linked_uncheck_all_button = QPushButton("全解除")
        self.linked_uncheck_all_button.clicked.connect(lambda: self.set_linked_broadcasters_checked(False))
        self.broadcast_filter_model = BroadcastFilterModel()
        self.broadcast_filter_model.dataChanged.connect(lambda *_args: self.reload_special_detail())
        self.broadcast_filter_table = self.make_table(self.broadcast_filter_model)
        self.broadcast_filter_table.setColumnWidth(0, 55)
        self.broadcast_filter_table.setColumnWidth(1, 110)
        self.broadcast_filter_table.setColumnWidth(2, 220)
        self.broadcast_filter_table.setColumnWidth(3, 130)
        self.broadcast_filter_table.doubleClicked.connect(self.open_special_broadcast_comments)
        self.broadcast_check_all_button = QPushButton("全チェック")
        self.broadcast_check_all_button.clicked.connect(lambda: self.set_broadcasts_checked(True))
        self.broadcast_uncheck_all_button = QPushButton("全解除")
        self.broadcast_uncheck_all_button.clicked.connect(lambda: self.set_broadcasts_checked(False))
        self.broadcast_back_button = QPushButton("配信者一覧に戻る")
        self.broadcast_back_button.clicked.connect(self.show_linked_broadcaster_list)
        self.comment_list_back_button = QPushButton("放送一覧に戻る")
        self.comment_list_back_button.clicked.connect(self.show_special_broadcast_list)
        self.comment_list_title = QLabel("コメント一覧")
        self.broadcast_filter_title = QLabel("放送")
        self.selected_filter_broadcaster_id = ""
        self._special_broadcast_rows: list[dict[str, Any]] = []
        self.special_broadcast_period_enabled = False
        self.special_broadcast_search = QLineEdit()
        self.special_broadcast_search.setPlaceholderText("タイトル / LV / 日時で検索")
        self.special_broadcast_search.textChanged.connect(lambda *_args: self.apply_special_broadcast_filter())
        (
            self.special_broadcast_date_controls,
            self.special_broadcast_from_date,
            self.special_broadcast_to_date,
        ) = make_date_range_controls(
            on_changed=self.on_special_broadcast_date_changed,
            on_all_period=self.clear_special_broadcast_date_range,
        )
        self._broadcaster_broadcast_rows: list[dict[str, Any]] = []
        self.broadcaster_broadcast_period_enabled = False
        self.broadcaster_broadcast_filter_model = BroadcastFilterModel()
        self.broadcaster_broadcast_filter_model.dataChanged.connect(lambda *_args: self.reload_broadcaster_content())
        self.broadcaster_broadcast_filter_table = self.make_table(self.broadcaster_broadcast_filter_model)
        self.broadcaster_broadcast_filter_table.setShowGrid(False)
        self.broadcaster_broadcast_filter_table.setItemDelegate(
            BroadcastTwoLineDelegate(self.broadcaster_broadcast_filter_table)
        )
        self.broadcaster_broadcast_filter_model.modelReset.connect(
            lambda: QTimer.singleShot(
                0, self.broadcaster_broadcast_filter_table.resizeRowsToContents
            )
        )
        setattr(
            self.broadcaster_broadcast_filter_table,
            "_local_processing_rows_sender",
            self.send_broadcaster_rows_to_local_processing,
        )
        self.broadcaster_broadcast_filter_table.clicked.connect(self.show_broadcaster_broadcast_detail)
        self.broadcaster_summary_view = QTextEdit()
        self.broadcaster_summary_view.setReadOnly(True)
        self.broadcaster_transcript_view = QTextEdit()
        self.broadcaster_transcript_view.setReadOnly(True)
        self.broadcaster_comments_view = QTextEdit()
        self.broadcaster_comments_view.setReadOnly(True)
        self.broadcaster_broadcast_filter_table.setColumnWidth(0, 55)
        self.broadcaster_broadcast_filter_table.setColumnWidth(1, 110)
        self.broadcaster_broadcast_filter_table.setColumnWidth(2, 260)
        self.broadcaster_broadcast_filter_table.setColumnWidth(3, 150)
        self.broadcaster_broadcast_check_all_button = QPushButton("全選択")
        self.broadcaster_broadcast_check_all_button.clicked.connect(lambda: self.set_broadcaster_broadcasts_checked(True))
        self.broadcaster_broadcast_uncheck_all_button = QPushButton("全解除")
        self.broadcaster_broadcast_uncheck_all_button.clicked.connect(lambda: self.set_broadcaster_broadcasts_checked(False))
        self.broadcaster_status_visible = QCheckBox("状態を表示")
        self.broadcaster_status_visible.setChecked(True)
        self.broadcaster_status_visible.toggled.connect(
            self.broadcaster_broadcast_filter_model.set_show_details
        )
        self.broadcaster_tags_visible = QCheckBox("タグも表示")
        self.broadcaster_tags_visible.setChecked(False)
        self.broadcaster_tags_visible.toggled.connect(
            self.broadcaster_broadcast_filter_model.set_show_tags
        )
        self.broadcaster_uploaded_only = QCheckBox("HTML公開済みのみ")
        self.broadcaster_uploaded_only.setChecked(True)
        self.broadcaster_uploaded_only.toggled.connect(
            lambda _checked: self.apply_broadcaster_broadcast_filter()
        )
        self.broadcaster_broadcast_search = QLineEdit()
        self.broadcaster_broadcast_search.setPlaceholderText("タイトル / LV / 日時で検索")
        self.broadcaster_broadcast_search.textChanged.connect(lambda *_args: self.apply_broadcaster_broadcast_filter())
        (
            self.broadcaster_broadcast_date_controls,
            self.broadcaster_broadcast_from_date,
            self.broadcaster_broadcast_to_date,
        ) = make_date_range_controls(
            on_changed=self.on_broadcaster_broadcast_date_changed,
            on_all_period=self.clear_broadcaster_broadcast_date_range,
        )
        self.transcript_sort_combo = NoWheelComboBox()
        self.transcript_sort_combo.addItem("放送時間順", "time")
        self.transcript_sort_combo.addItem("感情スコア順", "emotion")
        self.transcript_sort_combo.addItem("文字数順", "length")
        self.transcript_sort_combo.currentIndexChanged.connect(lambda *_args: self.reload_broadcaster_content())

        top = QWidget()
        top_layout = QHBoxLayout(top)
        top_layout.setContentsMargins(0, 0, 0, 0)
        top_layout.setSpacing(8)
        top_layout.addWidget(QLabel("スペシャルユーザー"))
        top_layout.addWidget(self.special_combo)
        top_layout.addWidget(QLabel("配信者"))
        top_layout.addWidget(self.broadcaster_combo)
        top_layout.addWidget(self.refresh_button)
        top_layout.addStretch(1)

        tabs = QTabWidget()
        tabs.setMinimumWidth(0)
        tabs.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        special_page = QWidget()
        special_page.setMinimumWidth(0)
        special_page.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        self.special_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.special_splitter.setChildrenCollapsible(False)
        special_left = QWidget()
        special_left.setMinimumWidth(420)
        special_left.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        special_left_layout = QVBoxLayout(special_left)
        special_left_layout.addWidget(QLabel("発言履歴"))
        special_left_layout.addWidget(self.special_comments_table, 1)
        special_right = QWidget()
        special_right.setMinimumWidth(420)
        special_right.setMaximumWidth(680)
        special_right.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        special_right_layout = QVBoxLayout(special_right)
        self.special_right_stack = QStackedWidget()
        linked_page = QWidget()
        linked_page_layout = QVBoxLayout(linked_page)
        linked_page_layout.addWidget(QLabel("紐づいてる配信者"))
        linked_button_row = QHBoxLayout()
        linked_button_row.addWidget(self.linked_check_all_button)
        linked_button_row.addWidget(self.linked_uncheck_all_button)
        linked_button_row.addStretch(1)
        linked_page_layout.addLayout(linked_button_row)
        linked_page_layout.addWidget(self.linked_broadcaster_filter_table, 1)
        broadcast_page = QWidget()
        broadcast_page_layout = QVBoxLayout(broadcast_page)
        broadcast_page_layout.addWidget(self.broadcast_back_button)
        broadcast_page_layout.addWidget(self.broadcast_filter_title)
        broadcast_page_layout.addWidget(self.special_broadcast_search)
        broadcast_page_layout.addWidget(self.special_broadcast_date_controls)
        broadcast_button_row = QHBoxLayout()
        broadcast_button_row.addWidget(self.broadcast_check_all_button)
        broadcast_button_row.addWidget(self.broadcast_uncheck_all_button)
        broadcast_button_row.addStretch(1)
        broadcast_page_layout.addLayout(broadcast_button_row)
        broadcast_page_layout.addWidget(self.broadcast_filter_table, 1)
        comment_list_page = QWidget()
        comment_list_page_layout = QVBoxLayout(comment_list_page)
        comment_list_page_layout.addWidget(self.comment_list_back_button)
        comment_list_page_layout.addWidget(self.comment_list_title)
        comment_list_page_layout.addWidget(self.special_broadcast_comments_table, 1)
        self.special_right_stack.addWidget(linked_page)
        self.special_right_stack.addWidget(broadcast_page)
        self.special_right_stack.addWidget(comment_list_page)
        special_right_layout.addWidget(self.special_right_stack, 1)
        self.special_splitter.addWidget(special_left)
        self.special_splitter.addWidget(special_right)
        self.special_splitter.setStretchFactor(0, 3)
        self.special_splitter.setStretchFactor(1, 2)
        self.special_splitter.setSizes([820, 460])
        special_layout = QVBoxLayout(special_page)
        special_layout.addWidget(self.special_splitter, 1)
        tabs.addTab(special_page, "スペシャル確認")

        broadcaster_page = QWidget()
        broadcaster_page.setMinimumWidth(0)
        broadcaster_page.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        self.broadcaster_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.broadcaster_splitter.setChildrenCollapsible(False)
        broadcaster_left = QWidget()
        broadcaster_left_layout = QVBoxLayout(broadcaster_left)
        broadcaster_left_layout.setContentsMargins(0, 0, 0, 0)
        broadcaster_content_tabs = QTabWidget()
        summary_page = QWidget()
        summary_layout = QVBoxLayout(summary_page)
        summary_layout.setContentsMargins(0, 0, 0, 0)
        summary_layout.addWidget(self.broadcaster_summary_view, 1)
        broadcaster_content_tabs.addTab(summary_page, "要約")
        transcript_page = QWidget()
        transcript_layout = QVBoxLayout(transcript_page)
        transcript_layout.setContentsMargins(0, 0, 0, 0)
        transcript_sort_row = QHBoxLayout()
        transcript_sort_row.addWidget(QLabel("並び替え"))
        transcript_sort_row.addWidget(self.transcript_sort_combo)
        transcript_sort_row.addStretch(1)
        transcript_layout.addLayout(transcript_sort_row)
        transcript_layout.addWidget(self.broadcaster_transcript_view, 1)
        broadcaster_content_tabs.addTab(transcript_page, "文字起こし")
        comments_page = QWidget()
        comments_layout = QVBoxLayout(comments_page)
        comments_layout.setContentsMargins(0, 0, 0, 0)
        comments_layout.addWidget(self.broadcaster_comments_view, 1)
        broadcaster_content_tabs.addTab(comments_page, "コメント")
        broadcaster_left_layout.addWidget(broadcaster_content_tabs, 1)
        broadcaster_right = QWidget()
        broadcaster_right.setMinimumWidth(420)
        broadcaster_right_layout = QVBoxLayout(broadcaster_right)
        broadcaster_right_layout.setContentsMargins(0, 0, 0, 0)
        broadcaster_right_layout.addWidget(QLabel("放送履歴"))
        broadcaster_right_layout.addWidget(self.broadcaster_broadcast_search)
        broadcaster_right_layout.addWidget(self.broadcaster_broadcast_date_controls)
        broadcaster_broadcast_button_row = QHBoxLayout()
        broadcaster_broadcast_button_row.addWidget(self.broadcaster_broadcast_check_all_button)
        broadcaster_broadcast_button_row.addWidget(self.broadcaster_broadcast_uncheck_all_button)
        broadcaster_broadcast_button_row.addWidget(self.broadcaster_status_visible)
        broadcaster_broadcast_button_row.addWidget(self.broadcaster_tags_visible)
        broadcaster_broadcast_button_row.addWidget(self.broadcaster_uploaded_only)
        broadcaster_broadcast_button_row.addStretch(1)
        broadcaster_right_layout.addLayout(broadcaster_broadcast_button_row)
        broadcaster_right_layout.addWidget(self.broadcaster_broadcast_filter_table, 1)
        self.broadcaster_splitter.addWidget(broadcaster_left)
        self.broadcaster_splitter.addWidget(broadcaster_right)
        self.broadcaster_splitter.setSizes([900, 460])
        broadcaster_layout = QVBoxLayout(broadcaster_page)
        broadcaster_layout.addWidget(self.broadcaster_splitter, 1)
        tabs.addTab(broadcaster_page, "配信者確認")

        layout = QVBoxLayout(self)
        layout.addWidget(top)
        layout.addWidget(tabs, 1)
        self.reload()

    def make_table(self, model: SimpleDictTableModel) -> QTableView:
        table = QTableView()
        table.setModel(model)
        stabilize_table_scroll(table)
        table.setMinimumWidth(0)
        table.setMaximumWidth(16777215)
        table.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        table.setSizeAdjustPolicy(QAbstractScrollArea.SizeAdjustPolicy.AdjustIgnored)
        table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QTableView.SelectionMode.ExtendedSelection)
        table.setAlternatingRowColors(True)
        table.verticalHeader().setVisible(False)
        configure_table_header(table)
        return table

    def send_broadcaster_rows_to_local_processing(
        self,
        rows: list[dict[str, Any]],
    ) -> None:
        lvs = broadcaster_rows_to_lvs(rows)
        paths, missing_lvs = broadcaster_rows_to_local_video_paths(rows)
        if not paths:
            missing_text = ", ".join(missing_lvs or lvs)
            show_status(
                self,
                f"選択した放送のローカル動画が見つかりません: {missing_text}",
                "WARN",
            )
            return
        try:
            result = send_local_files_to_processing_gui(paths)
        except Exception as exc:
            show_status(
                self,
                f"ローカル処理GUI送信失敗: {type(exc).__name__}: {exc}",
                "ERROR",
            )
            return
        action = "起動して送信" if result == "started" else "送信"
        message = f"ローカル処理へ{action}: {len(lvs) - len(missing_lvs)}放送 / {len(paths)}動画"
        if missing_lvs:
            message += f" / 動画なし={','.join(missing_lvs)}"
        show_status(self, message, "WARN" if missing_lvs else "INFO")

    def reload(self) -> None:
        current_special = self.special_combo.currentData()
        current_broadcaster = self.broadcaster_combo.currentData()
        self.special_combo.blockSignals(True)
        self.broadcaster_combo.blockSignals(True)
        self.special_combo.clear()
        self.broadcaster_combo.clear()
        with tracker.connect() as conn:
            specials = conn.execute("SELECT user_id, label FROM special_users ORDER BY label, user_id").fetchall()
            broadcasters = conn.execute(
                """
                SELECT broadcaster_id, broadcaster_name
                FROM monitored_broadcasters
                WHERE COALESCE(broadcaster_id, '') <> ''
                ORDER BY broadcaster_name, broadcaster_id
                """
            ).fetchall()
        for row in specials:
            self.special_combo.addItem(f"{row['label'] or row['user_id']} / {row['user_id']}", row["user_id"])
        for row in broadcasters:
            self.broadcaster_combo.addItem(
                f"{row['broadcaster_name'] or row['broadcaster_id']} / {row['broadcaster_id']}",
                row["broadcaster_id"],
            )
        self.restore_combo(self.special_combo, current_special)
        self.restore_combo(self.broadcaster_combo, current_broadcaster)
        self.special_combo.blockSignals(False)
        self.broadcaster_combo.blockSignals(False)
        self.reload_linked_broadcaster_filter()
        self.reload_special_detail()
        self.reload_broadcaster_detail()

    def restore_combo(self, combo: QComboBox, value: object) -> None:
        if value is None:
            return
        index = combo.findData(value)
        if index >= 0:
            combo.setCurrentIndex(index)

    def on_special_changed(self) -> None:
        self.show_linked_broadcaster_list()
        self.reload_linked_broadcaster_filter()
        self.reload_special_detail()

    def reload_linked_broadcaster_filter(self) -> None:
        user_id = str(self.special_combo.currentData() or "")
        if not user_id:
            self.linked_broadcaster_filter_model.update_rows([])
            return
        with tracker.connect() as conn:
            rows = conn.execute(
                """
                SELECT s.broadcaster_id,
                       s.broadcaster_name,
                       s.enabled,
                       (
                           SELECT COUNT(*)
                           FROM archive_comments c
                           LEFT JOIN broadcast_archive_meta m ON m.lv = c.lv
                           LEFT JOIN broadcasts b ON b.lv = c.lv
                           WHERE c.user_id = s.user_id
                             AND COALESCE(m.broadcaster_id, b.broadcaster_id, '') = s.broadcaster_id
                       ) AS speech_count,
                       (
                           SELECT COUNT(DISTINCT lv)
                           FROM (
                               SELECT lv
                               FROM special_user_broadcast_hits h
                               WHERE h.user_id = s.user_id
                                 AND h.broadcaster_id = s.broadcaster_id
                               UNION
                               SELECT lv
                               FROM broadcaster_monitor_special_user_hits h
                               WHERE h.user_id = s.user_id
                                 AND h.broadcaster_id = s.broadcaster_id
                           )
                       ) AS broadcast_count
                FROM special_user_broadcasters s
                WHERE s.user_id = ?
                  AND COALESCE(broadcaster_id, '') <> ''
                ORDER BY broadcaster_name, broadcaster_id
                """,
                (user_id,),
            ).fetchall()
        self.linked_broadcaster_filter_model.update_rows(
            [
                {
                    "visible": True,
                    "broadcaster_id": row["broadcaster_id"],
                    "broadcaster_name": row["broadcaster_name"] or row["broadcaster_id"],
                    "speech_count": row["speech_count"],
                    "broadcast_count": row["broadcast_count"],
                }
                for row in rows
            ]
        )

    def selected_special_broadcaster_ids(self) -> list[str] | None:
        if self.special_right_stack.currentIndex() in (1, 2) and self.selected_filter_broadcaster_id:
            return [self.selected_filter_broadcaster_id]
        if self.linked_broadcaster_filter_model.rowCount() <= 0:
            return None
        return self.linked_broadcaster_filter_model.checked_broadcaster_ids()

    def set_linked_broadcasters_checked(self, checked: bool) -> None:
        self.linked_broadcaster_filter_model.set_all_checked(checked)

    def open_broadcaster_broadcast_filter(self, index: QModelIndex) -> None:
        broadcaster_id = self.linked_broadcaster_filter_model.broadcaster_id_at(index.row())
        if not broadcaster_id:
            return
        self.selected_filter_broadcaster_id = broadcaster_id
        self.reload_broadcast_filter_for_broadcaster(broadcaster_id)
        self.special_right_stack.setCurrentIndex(1)
        self.ensure_special_splitter_readable()
        self.reload_special_detail()

    def show_linked_broadcaster_list(self) -> None:
        self.selected_filter_broadcaster_id = ""
        self._special_broadcast_rows = []
        self.broadcast_filter_model.update_rows([])
        self.special_broadcast_comments_model.update_rows([])
        self.special_right_stack.setCurrentIndex(0)
        self.ensure_special_splitter_readable()
        self.reload_special_detail()

    def show_special_broadcast_list(self) -> None:
        if self.selected_filter_broadcaster_id:
            self.special_right_stack.setCurrentIndex(1)
        else:
            self.special_right_stack.setCurrentIndex(0)
        self.ensure_special_splitter_readable()
        self.reload_special_detail()

    def open_special_broadcast_comments(self, index: QModelIndex) -> None:
        row = self.broadcast_filter_model.row_at(index.row())
        lv = str(row.get("lv") or "").strip()
        if not lv:
            return
        title = str(row.get("title") or "").strip()
        self.reload_special_broadcast_comments(lv, title)
        self.special_right_stack.setCurrentIndex(2)
        self.ensure_special_splitter_readable()

    def reload_special_broadcast_comments(self, lv: str, title: str = "") -> None:
        with tracker.connect() as conn:
            rows = conn.execute(
                """
                SELECT no,
                       ROUND(broadcast_seconds, 1) AS broadcast_seconds,
                       posted_at,
                       user_id,
                       user_name,
                       text
                FROM archive_comments
                WHERE lv = ?
                ORDER BY COALESCE(no, id), id
                """,
                (lv,),
            ).fetchall()
        self.comment_list_title.setText(f"{lv} / {title or 'タイトル未取得'} / コメント {len(rows)}件")
        self.special_broadcast_comments_model.update_rows([dict(row) for row in rows])

    def ensure_special_splitter_readable(self) -> None:
        sizes = self.special_splitter.sizes()
        total = sum(sizes)
        if total <= 0:
            self.special_splitter.setSizes([820, 460])
            return

        min_left = 420
        min_right = 360
        max_right = min(680, max(min_right, total - min_left))
        right = min(max(sizes[1] if len(sizes) > 1 else 460, min_right), max_right)
        left = total - right
        if left < min_left:
            left = min_left
            right = max(min_right, total - left)
        self.special_splitter.setSizes([left, right])

    def reload_broadcast_filter_for_broadcaster(self, broadcaster_id: str) -> None:
        user_id = str(self.special_combo.currentData() or "")
        if not user_id or not broadcaster_id:
            self._special_broadcast_rows = []
            self.broadcast_filter_model.update_rows([])
            return
        with tracker.connect() as conn:
            rows = conn.execute(
                """
                SELECT lv,
                       MAX(title) AS title,
                       MAX(detected_at) AS detected_at,
                       MAX(time_value) AS time_value
                FROM (
                    SELECT h.lv,
                           COALESCE(m.title, b.title, '') AS title,
                           h.first_seen_at AS detected_at,
                           COALESCE(m.begin_time, m.open_time, m.end_time, CAST(strftime('%s', h.first_seen_at) AS INTEGER), CAST(strftime('%s', b.first_seen_at) AS INTEGER)) AS time_value
                    FROM special_user_broadcast_hits h
                    LEFT JOIN broadcast_archive_meta m ON m.lv = h.lv
                    LEFT JOIN broadcasts b ON b.lv = h.lv
                    WHERE h.user_id = ?
                      AND h.broadcaster_id = ?
                    UNION ALL
                    SELECT h.lv,
                           COALESCE(m.title, b.title, '') AS title,
                           h.detected_at AS detected_at,
                           COALESCE(m.begin_time, m.open_time, m.end_time, CAST(strftime('%s', h.detected_at) AS INTEGER), CAST(strftime('%s', b.first_seen_at) AS INTEGER)) AS time_value
                    FROM broadcaster_monitor_special_user_hits h
                    LEFT JOIN broadcast_archive_meta m ON m.lv = h.lv
                    LEFT JOIN broadcasts b ON b.lv = h.lv
                    WHERE h.user_id = ?
                      AND h.broadcaster_id = ?
                    UNION ALL
                    SELECT c.lv,
                           COALESCE(m.title, b.title, '') AS title,
                           c.posted_at AS detected_at,
                           COALESCE(m.begin_time, m.open_time, m.end_time, CAST(strftime('%s', c.posted_at) AS INTEGER), CAST(strftime('%s', b.first_seen_at) AS INTEGER)) AS time_value
                    FROM archive_comments c
                    LEFT JOIN broadcast_archive_meta m ON m.lv = c.lv
                    LEFT JOIN broadcasts b ON b.lv = c.lv
                    WHERE c.user_id = ?
                      AND COALESCE(m.broadcaster_id, b.broadcaster_id, '') = ?
                )
                WHERE COALESCE(lv, '') <> ''
                GROUP BY lv
                ORDER BY detected_at DESC, lv DESC
                LIMIT 500
                """,
                (user_id, broadcaster_id, user_id, broadcaster_id, user_id, broadcaster_id),
            ).fetchall()
        broadcaster_name = ""
        for row in self.linked_broadcaster_filter_model._rows:
            if str(row.get("broadcaster_id") or "") == broadcaster_id:
                broadcaster_name = str(row.get("broadcaster_name") or "")
                break
        self.broadcast_filter_title.setText(f"放送: {broadcaster_name or broadcaster_id} / {broadcaster_id}")
        self._special_broadcast_rows = [
            {
                "visible": True,
                "lv": row["lv"],
                "title": row["title"],
                "detected_at": row["detected_at"],
                "time_value": row["time_value"],
            }
            for row in rows
        ]
        self.special_broadcast_period_enabled = False
        self.reset_special_broadcast_date_range()
        self.apply_special_broadcast_filter()

    def selected_special_lvs(self) -> list[str] | None:
        if self.special_right_stack.currentIndex() != 1:
            return None
        if self.broadcast_filter_model.rowCount() <= 0:
            return []
        return self.broadcast_filter_model.checked_lvs()

    def set_broadcasts_checked(self, checked: bool) -> None:
        self.broadcast_filter_model.set_all_checked(checked)

    def set_broadcaster_broadcasts_checked(self, checked: bool) -> None:
        self.broadcaster_broadcast_filter_model.set_all_checked(checked)

    def apply_special_broadcast_filter(self) -> None:
        query = self.special_broadcast_search.text().strip().lower()
        start_date = self.special_broadcast_from_date.date()
        end_date = self.special_broadcast_to_date.date()
        if start_date > end_date:
            start_date, end_date = end_date, start_date
        rows: list[dict[str, Any]] = []
        for row in self._special_broadcast_rows:
            if query and not (
                query in str(row.get("lv") or "").lower()
                or query in str(row.get("title") or "").lower()
                or query in str(row.get("detected_at") or "").lower()
            ):
                continue
            if self.special_broadcast_period_enabled:
                row_date = qdate_from_unix_seconds(row.get("time_value"))
                if not row_date or not (start_date <= row_date <= end_date):
                    continue
            rows.append(dict(row, visible=True))
        self.broadcast_filter_model.update_rows(rows)
        self.reload_special_detail()

    def reset_special_broadcast_date_range(self) -> None:
        temp_model = BroadcastFilterModel()
        temp_model.update_rows([dict(row, visible=True) for row in self._special_broadcast_rows])
        start_date, end_date = temp_model.date_bounds()
        set_date_range_controls(
            self.special_broadcast_from_date,
            self.special_broadcast_to_date,
            start_date,
            end_date,
        )

    def on_special_broadcast_date_changed(self) -> None:
        self.special_broadcast_period_enabled = True
        self.apply_special_broadcast_filter()

    def clear_special_broadcast_date_range(self) -> None:
        self.special_broadcast_period_enabled = False
        self.reset_special_broadcast_date_range()
        self.apply_special_broadcast_filter()

    def apply_broadcaster_broadcast_filter(self) -> None:
        query = self.broadcaster_broadcast_search.text().strip().lower()
        start_date = self.broadcaster_broadcast_from_date.date()
        end_date = self.broadcaster_broadcast_to_date.date()
        if start_date > end_date:
            start_date, end_date = end_date, start_date
        rows: list[dict[str, Any]] = []
        for row in self._broadcaster_broadcast_rows:
            if self.broadcaster_uploaded_only.isChecked() and row.get("html_uploaded") != "済":
                continue
            if query and not (
                query in str(row.get("lv") or "").lower()
                or query in str(row.get("title") or "").lower()
                or query in str(row.get("detected_at") or "").lower()
            ):
                continue
            if self.broadcaster_broadcast_period_enabled:
                row_date = qdate_from_unix_seconds(row.get("time_value"))
                if not row_date or not (start_date <= row_date <= end_date):
                    continue
            rows.append(dict(row, visible=True))
        self.broadcaster_broadcast_filter_model.update_rows(rows)
        self.reload_broadcaster_content()

    def reset_broadcaster_broadcast_date_range(self) -> None:
        temp_model = BroadcastFilterModel()
        temp_model.update_rows([dict(row, visible=True) for row in self._broadcaster_broadcast_rows])
        start_date, end_date = temp_model.date_bounds()
        set_date_range_controls(
            self.broadcaster_broadcast_from_date,
            self.broadcaster_broadcast_to_date,
            start_date,
            end_date,
        )

    def on_broadcaster_broadcast_date_changed(self) -> None:
        self.broadcaster_broadcast_period_enabled = True
        self.apply_broadcaster_broadcast_filter()

    def clear_broadcaster_broadcast_date_range(self) -> None:
        self.broadcaster_broadcast_period_enabled = False
        self.reset_broadcaster_broadcast_date_range()
        self.apply_broadcaster_broadcast_filter()
        if self.broadcaster_broadcast_filter_model.rowCount() > 0:
            index = self.broadcaster_broadcast_filter_model.index(0, 1)
            self.broadcaster_broadcast_filter_table.setCurrentIndex(index)
            self.show_broadcaster_broadcast_detail(index)

    def show_broadcaster_broadcast_detail(self, index: QModelIndex) -> None:
        row = self.broadcaster_broadcast_filter_model.row_at(index.row())
        lv = str(row.get("lv") or "").strip()
        if not lv:
            return
        with tracker.connect() as conn:
            data_row = conn.execute(
                "SELECT payload_json FROM archive_broadcast_data WHERE lv = ?",
                (lv,),
            ).fetchone()
            transcript_rows = conn.execute(
                """
                SELECT start_seconds, speaker, text
                FROM archive_transcript_segments
                WHERE lv = ?
                ORDER BY start_seconds, segment_index
                """,
                (lv,),
            ).fetchall()
            comment_rows = conn.execute(
                """
                SELECT broadcast_seconds, user_name, user_id, text
                FROM archive_comments
                WHERE lv = ?
                ORDER BY broadcast_seconds, id
                """,
                (lv,),
            ).fetchall()
        payload = {}
        if data_row:
            try:
                payload = json.loads(data_row["payload_json"] or "{}")
            except Exception:
                payload = {}
        title = str(row.get("title") or "")
        summary = str(payload.get("summary_text") or payload.get("previous_summary") or "要約なし")
        self.broadcaster_summary_view.setPlainText(f"{lv}\n{title}\n\n{summary}")

        def clock(value: Any) -> str:
            seconds = max(0, int(float(value or 0)))
            return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"

        transcript_text = "\n".join(
            f"[{clock(item['start_seconds'])}] "
            f"{(str(item['speaker'] or '').strip() + ': ') if str(item['speaker'] or '').strip() else ''}"
            f"{str(item['text'] or '').strip()}"
            for item in transcript_rows
        )
        self.broadcaster_transcript_view.setPlainText(transcript_text or "文字起こしなし")
        comments_text = "\n".join(
            f"[{clock(item['broadcast_seconds'])}] "
            f"{str(item['user_name'] or item['user_id'] or '匿名')}: {str(item['text'] or '').strip()}"
            for item in comment_rows
        )
        self.broadcaster_comments_view.setPlainText(comments_text or "コメントなし")

    def selected_broadcaster_lvs(self) -> list[str]:
        if self.broadcaster_broadcast_filter_model.rowCount() <= 0:
            return []
        return self.broadcaster_broadcast_filter_model.checked_lvs()

    def reload_special_detail(self) -> None:
        user_id = str(self.special_combo.currentData() or "")
        if not user_id:
            self.special_comments_model.update_rows([])
            self.special_hits_model.update_rows([])
            return
        selected_broadcaster_ids = self.selected_special_broadcaster_ids()
        if selected_broadcaster_ids == []:
            self.special_comments_model.update_rows([])
            self.special_hits_model.update_rows([])
            return
        selected_lvs = self.selected_special_lvs()
        if selected_lvs == []:
            self.special_comments_model.update_rows([])
            self.special_hits_model.update_rows([])
            return
        selected_broadcaster_ids = [str(value) for value in (selected_broadcaster_ids or []) if str(value).strip()]
        selected_lvs = [str(value) for value in (selected_lvs or []) if str(value).strip()]
        comment_where = ["c.user_id = ?"]
        comment_params: list[str] = [user_id]
        if selected_broadcaster_ids:
            placeholders = ",".join("?" for _ in selected_broadcaster_ids)
            comment_where.append(f"COALESCE(m.broadcaster_id, b.broadcaster_id, '') IN ({placeholders})")
            comment_params.extend(selected_broadcaster_ids)
        if selected_lvs:
            placeholders = ",".join("?" for _ in selected_lvs)
            comment_where.append(f"c.lv IN ({placeholders})")
            comment_params.extend(selected_lvs)
        comment_filter_sql = " AND ".join(comment_where)

        hits_where: list[str] = []
        hits_params: list[str] = [user_id, user_id]
        if selected_broadcaster_ids:
            placeholders = ",".join("?" for _ in selected_broadcaster_ids)
            hits_where.append(f"broadcaster_id IN ({placeholders})")
            hits_params.extend(selected_broadcaster_ids)
        if selected_lvs:
            placeholders = ",".join("?" for _ in selected_lvs)
            hits_where.append(f"lv IN ({placeholders})")
            hits_params.extend(selected_lvs)
        hits_filter_sql = f"WHERE {' AND '.join(hits_where)}" if hits_where else ""
        with tracker.connect() as conn:
            comments = conn.execute(
                f"""
                SELECT c.lv, COALESCE(m.broadcaster_name, b.broadcaster_name, '') AS broadcaster_name,
                       COALESCE(m.broadcaster_id, b.broadcaster_id, '') AS broadcaster_id,
                       c.no, ROUND(c.broadcast_seconds, 1) AS broadcast_seconds, c.text, c.posted_at
                FROM archive_comments c
                LEFT JOIN broadcast_archive_meta m ON m.lv = c.lv
                LEFT JOIN broadcasts b ON b.lv = c.lv
                WHERE {comment_filter_sql}
                ORDER BY c.posted_at DESC, c.id DESC
                LIMIT 500
                """,
                tuple(comment_params),
            ).fetchall()
            hits = conn.execute(
                f"""
                SELECT *
                FROM (
                    SELECT 'コメント監視' AS route, lv, broadcaster_id, broadcaster_name,
                           first_comment_text, comment_count, first_seen_at AS detected_at, html_uploaded_at
                    FROM special_user_broadcast_hits
                    WHERE user_id = ?
                    UNION ALL
                    SELECT '配信者監視' AS route, lv, broadcaster_id, broadcaster_name,
                           first_comment_text, comment_count, detected_at, html_uploaded_at
                    FROM broadcaster_monitor_special_user_hits
                    WHERE user_id = ?
                )
                {hits_filter_sql}
                ORDER BY detected_at DESC
                LIMIT 500
                """,
                tuple(hits_params),
            ).fetchall()
        self.special_comments_model.update_rows([dict(row) for row in comments])
        self.special_hits_model.update_rows([dict(row) for row in hits])

    def reload_broadcaster_detail(self) -> None:
        self.reload_broadcaster_broadcast_filter()
        self.reload_broadcaster_content()

    def reload_broadcaster_broadcast_filter(self) -> None:
        broadcaster_id = str(self.broadcaster_combo.currentData() or "")
        if not broadcaster_id:
            self.broadcaster_programs_model.update_rows([])
            self._broadcaster_broadcast_rows = []
            self.broadcaster_broadcast_filter_model.update_rows([])
            self._broadcaster_summary_rows = []
            self._broadcaster_transcript_rows = []
            self._broadcaster_comment_rows = []
            self.apply_broadcaster_content_column_filters()
            return
        with tracker.connect() as conn:
            programs = conn.execute(
                """
                SELECT m.lv,
                       COALESCE(m.title, json_extract(d.payload_json, '$.live_title'), '') AS title,
                       CASE WHEN COALESCE(m.begin_time, json_extract(d.payload_json, '$.begin_time')) IS NOT NULL
                            THEN datetime(COALESCE(m.begin_time, json_extract(d.payload_json, '$.begin_time')), 'unixepoch', 'localtime')
                            ELSE '' END AS begin_time_text,
                       COALESCE(m.begin_time, json_extract(d.payload_json, '$.begin_time'), m.open_time, json_extract(d.payload_json, '$.open_time'), m.end_time, json_extract(d.payload_json, '$.end_time')) AS time_value,
                       CASE WHEN COALESCE(m.end_time, json_extract(d.payload_json, '$.end_time')) IS NOT NULL
                            THEN datetime(COALESCE(m.end_time, json_extract(d.payload_json, '$.end_time')), 'unixepoch', 'localtime')
                            ELSE '' END AS end_time_text,
                       m.watch_url,
                       COALESCE(m.html_path, json_extract(d.payload_json, '$.html_file_path'), '') AS html_path,
                       COALESCE(m.archive_upload_completed, 0) AS archive_upload_completed,
                       d.payload_json
                FROM broadcast_archive_meta m
                LEFT JOIN archive_broadcast_data d ON d.lv = m.lv
                LEFT JOIN recording_jobs j ON j.lv = m.lv
                LEFT JOIN (
                    SELECT lv, MAX(broadcaster_id) AS broadcaster_id
                    FROM recording_segments
                    WHERE COALESCE(broadcaster_id, '') <> ''
                    GROUP BY lv
                ) s ON s.lv = m.lv
                WHERE COALESCE(
                    NULLIF(m.broadcaster_id, ''),
                    NULLIF(json_extract(d.payload_json, '$.owner_id'), ''),
                    NULLIF(j.broadcaster_id, ''),
                    NULLIF(s.broadcaster_id, ''),
                    ''
                ) = ?
                ORDER BY begin_time_text DESC
                LIMIT 500
                """,
                (broadcaster_id,),
            ).fetchall()
            broadcaster_settings = conn.execute(
                "SELECT html_base_url FROM monitored_broadcasters WHERE broadcaster_id = ?",
                (broadcaster_id,),
            ).fetchone()
        program_dicts = [dict(row) for row in programs]
        html_base_url = (
            str(broadcaster_settings["html_base_url"] or "").strip().rstrip("/")
            if broadcaster_settings
            else ""
        )
        if not html_base_url:
            html_base_url = f"https://warehouse.bitter.jp/niconico/{quote(broadcaster_id)}"
        for row in program_dicts:
            lv = str(row.get("lv") or "").strip()
            html_name = Path(str(row.get("html_path") or "")).name
            if lv and html_name:
                row["generated_url"] = f"{html_base_url}/{quote(lv)}/{quote(html_name)}"
        self.broadcaster_programs_model.update_rows(program_dicts)
        self._broadcaster_broadcast_rows = []
        for row in program_dicts:
            payload = {}
            try:
                payload = json.loads(row.get("payload_json") or "{}")
            except Exception:
                payload = {}
            elements: list[str] = []
            broadcast_dir_text = str(payload.get("broadcast_directory_path") or "").strip()
            generated_name = str(payload.get("html_file_path") or "").strip()
            html_path = None
            if broadcast_dir_text and generated_name:
                candidate = Path(broadcast_dir_text) / generated_name
                if candidate.is_file():
                    html_path = candidate
            if html_path is None:
                html_path_text = str(row.get("html_path") or "").strip()
                candidate = Path(html_path_text) if html_path_text else None
                if candidate is not None and candidate.is_file() and candidate.name != f"{row.get('lv')}.html":
                    html_path = candidate
            html_dir = (html_path.parent if html_path and html_path.suffix else html_path)
            lv = str(row.get("lv") or "")
            html_text = ""
            if html_path is not None and html_path.is_file():
                try:
                    with html_path.open("r", encoding="utf-8", errors="ignore") as html_file:
                        html_text = html_file.read(1_048_576)
                except OSError:
                    html_text = ""
            if 'id="timeline1"' in html_text and 'id="timeline2"' in html_text:
                elements.append("タイムラインHTML")
            screenshot_dir = html_dir / "screenshot" / lv if html_dir is not None else None
            if screenshot_dir is not None and screenshot_dir.is_dir() and next(screenshot_dir.glob("*.jpg"), None):
                elements.append("10秒サムネ")
            if html_dir is not None and html_dir.is_dir() and (html_dir / f"{lv}_audio.mp3").is_file():
                elements.append("音声タイムライン")
            if "<h2>🏆 コメントランキング（" in html_text:
                elements.append("ランキング")
            if "<h2>開始前会話</h2>" in html_text or "<h2>終了後会話</h2>" in html_text:
                elements.append("ニニココ会話")
            if "<h2>要約</h2>" in html_text:
                elements.append("要約")
            if "<h4>楽曲 " in html_text:
                elements.append("曲")
            if 'class="summary-image"' in html_text and '<img src=' in html_text:
                elements.append("抽象画像")
            if (
                '<span class="positive-score">' in html_text
                or '<span class=\\"positive-score\\">' in html_text
            ):
                elements.append("感情スコア")
            if "<h2>単語使用頻度ランキング</h2>" in html_text:
                elements.append("言葉抽出")
            page_tags: list[str] = []
            page_tags_match = re.search(
                r'<script[^>]+id=["\']archive-page-tags["\'][^>]*>(.*?)</script>',
                html_text, re.DOTALL | re.IGNORECASE,
            )
            if page_tags_match:
                try:
                    page_tags = [
                        str(tag).strip() for tag in json.loads(page_tags_match.group(1))
                        if str(tag).strip()
                    ]
                except Exception:
                    page_tags = []
            self._broadcaster_broadcast_rows.append(
                {
                "visible": True,
                "lv": row["lv"],
                "title": row["title"],
                "detected_at": row["begin_time_text"] or row["end_time_text"],
                "time_value": row["time_value"],
                "html_path": row["html_path"],
                "html_uploaded": "済" if int(row.get("archive_upload_completed") or 0) else "未",
                "generated_elements": " / ".join(elements) if elements else "なし",
                "tags": " / ".join(page_tags),
                }
            )
        self.broadcaster_broadcast_period_enabled = False
        self.reset_broadcaster_broadcast_date_range()
        self.apply_broadcaster_broadcast_filter()

    def reload_broadcaster_content(self) -> None:
        broadcaster_id = str(self.broadcaster_combo.currentData() or "")
        selected_lvs = self.selected_broadcaster_lvs()
        if not broadcaster_id or selected_lvs == []:
            self._broadcaster_summary_rows = []
            self._broadcaster_transcript_rows = []
            self._broadcaster_comment_rows = []
            self.apply_broadcaster_content_column_filters()
            return
        placeholders = ",".join("?" for _ in selected_lvs)
        with tracker.connect() as conn:
            summary_rows = conn.execute(
                f"""
                SELECT m.lv,
                       COALESCE(m.title, json_extract(d.payload_json, '$.live_title'), '') AS title,
                       CASE WHEN COALESCE(m.begin_time, json_extract(d.payload_json, '$.begin_time')) IS NOT NULL
                            THEN datetime(COALESCE(m.begin_time, json_extract(d.payload_json, '$.begin_time')), 'unixepoch', 'localtime')
                            ELSE '' END AS begin_time_text,
                       d.payload_json
                FROM broadcast_archive_meta m
                LEFT JOIN archive_broadcast_data d ON d.lv = m.lv
                WHERE m.lv IN ({placeholders})
                ORDER BY begin_time_text DESC, m.lv DESC
                """,
                (*selected_lvs,),
            ).fetchall()
            transcript_rows = conn.execute(
                f"""
                SELECT t.lv, t.segment_index, t.start_seconds, t.end_seconds, t.text, t.speaker, t.raw_json,
                       CASE WHEN m.begin_time IS NOT NULL THEN datetime(m.begin_time, 'unixepoch', 'localtime') ELSE '' END AS begin_time_text
                FROM archive_transcript_segments t
                JOIN broadcast_archive_meta m ON m.lv = t.lv
                WHERE t.lv IN ({placeholders})
                ORDER BY m.begin_time ASC, t.start_seconds ASC, t.segment_index ASC
                LIMIT 3000
                """,
                (*selected_lvs,),
            ).fetchall()
            comments = conn.execute(
                f"""
                SELECT c.user_id, c.user_name, c.lv, c.no,
                       ROUND(c.broadcast_seconds, 1) AS broadcast_seconds,
                       c.text, c.posted_at
                FROM archive_comments c
                JOIN broadcast_archive_meta m ON m.lv = c.lv
                WHERE c.lv IN ({placeholders})
                ORDER BY c.posted_at DESC, c.id DESC
                LIMIT 3000
                """,
                (*selected_lvs,),
            ).fetchall()
        summaries = []
        for row in summary_rows:
            payload = {}
            try:
                payload = json.loads(row["payload_json"] or "{}")
            except Exception:
                payload = {}
            summaries.append(
                {
                    "lv": row["lv"],
                    "title": row["title"],
                    "begin_time_text": row["begin_time_text"],
                    "summary": str(payload.get("summary_text") or payload.get("previous_summary") or ""),
                }
            )
        transcripts = [self.transcript_display_row(row) for row in transcript_rows]
        sort_key = str(self.transcript_sort_combo.currentData() or "time")
        if sort_key == "emotion":
            transcripts.sort(key=lambda row: float(row.get("emotion_score_value") or 0.0), reverse=True)
        elif sort_key == "length":
            transcripts.sort(key=lambda row: int(row.get("text_length") or 0), reverse=True)
        else:
            transcripts.sort(key=lambda row: (str(row.get("begin_time_text") or ""), float(row.get("start_seconds") or 0.0)))
        self._broadcaster_summary_rows = summaries
        self._broadcaster_transcript_rows = transcripts
        self._broadcaster_comment_rows = [dict(row) for row in comments]
        self.apply_broadcaster_content_column_filters()

    def apply_broadcaster_content_column_filters(self) -> None:
        self.broadcaster_summary_model.set_column_filters(self.broadcaster_summary_filter_inputs)
        self.broadcaster_transcript_model.set_column_filters(self.broadcaster_transcript_filter_inputs)
        self.broadcaster_comments_model.set_column_filters(self.broadcaster_comments_filter_inputs)
        self.broadcaster_summary_model.update_rows(
            filter_rows_by_column_queries(self._broadcaster_summary_rows, self.broadcaster_summary_filter_inputs)
        )
        self.broadcaster_transcript_model.update_rows(
            filter_rows_by_column_queries(self._broadcaster_transcript_rows, self.broadcaster_transcript_filter_inputs)
        )
        self.broadcaster_comments_model.update_rows(
            filter_rows_by_column_queries(self._broadcaster_comment_rows, self.broadcaster_comments_filter_inputs)
        )

    def transcript_display_row(self, row: sqlite3.Row) -> dict[str, Any]:
        raw = {}
        try:
            raw = json.loads(row["raw_json"] or "{}")
        except Exception:
            raw = {}
        positive = float(raw.get("positive_score") or 0.0)
        center = float(raw.get("center_score") or 0.0)
        negative = float(raw.get("negative_score") or 0.0)
        emotion_score = max(positive, center, negative)
        start = float(row["start_seconds"] or 0.0)
        end = float(row["end_seconds"] or 0.0)
        text = str(row["text"] or "")
        return {
            "lv": row["lv"],
            "begin_time_text": row["begin_time_text"],
            "start_seconds": start,
            "time_range": f"{self.format_seconds(start)} - {self.format_seconds(end)}",
            "speaker": row["speaker"] or "",
            "emotion_score": f"{emotion_score:.3f} / pos {positive:.3f} cen {center:.3f} neg {negative:.3f}",
            "emotion_score_value": emotion_score,
            "text_length": len(text),
            "text": text,
        }

    def format_seconds(self, seconds: float) -> str:
        total = max(0, int(seconds))
        hour = total // 3600
        minute = (total % 3600) // 60
        second = total % 60
        if hour:
            return f"{hour:02d}:{minute:02d}:{second:02d}"
        return f"{minute:02d}:{second:02d}"

    def table_column_widths(self, table: QTableView) -> list[int]:
        return table_column_widths(table)

    def table_header_state(self, table: QTableView) -> str:
        return table_header_state(table)

    def apply_table_column_widths(self, table: QTableView, widths: object) -> None:
        apply_table_column_widths(table, widths)

    def apply_table_header_state(self, table: QTableView, state: object) -> None:
        apply_table_header_state(table, state)

    def ui_state(self) -> dict[str, Any]:
        return {
            "special_splitter": self.special_splitter.sizes(),
            "broadcaster_splitter": self.broadcaster_splitter.sizes(),
            "tables": {
                "special_comments": self.table_column_widths(self.special_comments_table),
                "special_hits": self.table_column_widths(self.special_hits_table),
                "linked_broadcasters": self.table_column_widths(self.linked_broadcaster_filter_table),
                "broadcast_filter": self.table_column_widths(self.broadcast_filter_table),
                "special_broadcast_comments": self.table_column_widths(self.special_broadcast_comments_table),
                "broadcaster_programs": self.table_column_widths(self.broadcaster_programs_table),
                "broadcaster_broadcast_filter": self.table_column_widths(self.broadcaster_broadcast_filter_table),
                "broadcaster_summary": self.table_column_widths(self.broadcaster_summary_table),
                "broadcaster_transcript": self.table_column_widths(self.broadcaster_transcript_table),
                "broadcaster_comments": self.table_column_widths(self.broadcaster_comments_table),
            },
            "headers": {
                "special_comments": self.table_header_state(self.special_comments_table),
                "special_hits": self.table_header_state(self.special_hits_table),
                "linked_broadcasters": self.table_header_state(self.linked_broadcaster_filter_table),
                "broadcast_filter": self.table_header_state(self.broadcast_filter_table),
                "special_broadcast_comments": self.table_header_state(self.special_broadcast_comments_table),
                "broadcaster_programs": self.table_header_state(self.broadcaster_programs_table),
                "broadcaster_broadcast_filter": self.table_header_state(self.broadcaster_broadcast_filter_table),
                "broadcaster_summary": self.table_header_state(self.broadcaster_summary_table),
                "broadcaster_transcript": self.table_header_state(self.broadcaster_transcript_table),
                "broadcaster_comments": self.table_header_state(self.broadcaster_comments_table),
            },
        }

    def restore_ui_state(self, state: object) -> None:
        if not isinstance(state, dict):
            return
        splitter_sizes = state.get("special_splitter")
        if isinstance(splitter_sizes, list) and len(splitter_sizes) == 2:
            try:
                sizes = [int(splitter_sizes[0]), int(splitter_sizes[1])]
            except (TypeError, ValueError):
                sizes = []
            if len(sizes) == 2 and all(size > 0 for size in sizes):
                self.special_splitter.setSizes(sizes)
                self.ensure_special_splitter_readable()
        broadcaster_splitter_sizes = state.get("broadcaster_splitter")
        if isinstance(broadcaster_splitter_sizes, list) and len(broadcaster_splitter_sizes) == 2:
            try:
                sizes = [int(broadcaster_splitter_sizes[0]), int(broadcaster_splitter_sizes[1])]
            except (TypeError, ValueError):
                sizes = []
            if len(sizes) == 2 and all(size > 0 for size in sizes):
                self.broadcaster_splitter.setSizes(sizes)
        tables = state.get("tables")
        if isinstance(tables, dict):
            self.apply_table_column_widths(self.special_comments_table, tables.get("special_comments"))
            self.apply_table_column_widths(self.special_hits_table, tables.get("special_hits"))
            self.apply_table_column_widths(self.linked_broadcaster_filter_table, tables.get("linked_broadcasters"))
            self.apply_table_column_widths(self.broadcast_filter_table, tables.get("broadcast_filter"))
            self.apply_table_column_widths(self.special_broadcast_comments_table, tables.get("special_broadcast_comments"))
            self.apply_table_column_widths(self.broadcaster_programs_table, tables.get("broadcaster_programs"))
            self.apply_table_column_widths(self.broadcaster_broadcast_filter_table, tables.get("broadcaster_broadcast_filter"))
            self.apply_table_column_widths(self.broadcaster_summary_table, tables.get("broadcaster_summary"))
            self.apply_table_column_widths(self.broadcaster_transcript_table, tables.get("broadcaster_transcript"))
            self.apply_table_column_widths(self.broadcaster_comments_table, tables.get("broadcaster_comments"))
        headers = state.get("headers")
        if isinstance(headers, dict):
            self.apply_table_header_state(self.special_comments_table, headers.get("special_comments"))
            self.apply_table_header_state(self.special_hits_table, headers.get("special_hits"))
            self.apply_table_header_state(self.linked_broadcaster_filter_table, headers.get("linked_broadcasters"))
            self.apply_table_header_state(self.broadcast_filter_table, headers.get("broadcast_filter"))
            self.apply_table_header_state(self.special_broadcast_comments_table, headers.get("special_broadcast_comments"))
            self.apply_table_header_state(self.broadcaster_programs_table, headers.get("broadcaster_programs"))
            self.apply_table_header_state(self.broadcaster_broadcast_filter_table, headers.get("broadcaster_broadcast_filter"))
            self.apply_table_header_state(self.broadcaster_summary_table, headers.get("broadcaster_summary"))
            self.apply_table_header_state(self.broadcaster_transcript_table, headers.get("broadcaster_transcript"))
            self.apply_table_header_state(self.broadcaster_comments_table, headers.get("broadcaster_comments"))


def save_special_user(*, user_id: str, label: str = "", note: str = "") -> None:
    current_time = tracker.now()
    with tracker.connect() as conn:
        conn.execute(
            """
            INSERT INTO special_users (user_id, label, note, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                label = excluded.label,
                note = excluded.note,
                updated_at = excluded.updated_at
            """,
            (user_id, label, note, current_time, current_time),
        )
        conn.commit()


NICOVIDEO_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
    )
}


def fetch_nicovideo_user_profile(user_id: str) -> dict[str, str]:
    url = f"https://www.nicovideo.jp/user/{user_id}"
    response = requests.get(url, headers=NICOVIDEO_BROWSER_HEADERS, timeout=10)
    response.raise_for_status()
    response.encoding = "utf-8"
    soup = BeautifulSoup(response.text, "html.parser")
    meta = soup.find("meta", {"property": "profile:username"})
    name = ""
    if meta and meta.get("content"):
        name = str(meta["content"]).strip()
    if not name:
        title = soup.find("title")
        if title and title.text.strip():
            name = title.text.split("-")[0].strip()
    icon_meta = soup.find("meta", {"property": "og:image"})
    icon_url = str(icon_meta.get("content") or "").strip() if icon_meta else ""
    if not name:
        raise RuntimeError("名前を取得できなかった")
    return {"name": name, "icon_url": icon_url}


def fetch_nicovideo_user_name(user_id: str) -> str:
    return fetch_nicovideo_user_profile(user_id)["name"]


def fetch_niconico_channel_info(value: str) -> dict[str, str]:
    text = value.strip()
    slug = extract_channel_slug(text)
    if slug:
        url = f"https://ch.nicovideo.jp/{slug}"
    elif re.fullmatch(r"ch\d+", text):
        url = f"https://ch.nicovideo.jp/{text}"
    else:
        url = text
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
        ),
        "Accept-Language": "ja-JP,ja;q=0.9",
    }
    response = requests.get(url, headers=headers, timeout=10)
    response.raise_for_status()
    response.encoding = "utf-8"
    html = response.text
    channel_id_match = re.search(r"ch\d+", html)
    channel_id = channel_id_match.group(0) if channel_id_match else extract_user_id(text)
    if not channel_id:
        raise RuntimeError(f"チャンネルIDが見つからない: {value}")
    soup = BeautifulSoup(html, "html.parser")
    meta = soup.select_one('meta[property="og:title"], meta[name="twitter:title"]')
    title = meta.get("content") if meta and meta.get("content") else (soup.title.text if soup.title else channel_id)
    name = str(title or channel_id).split(" - ")[0].split("|")[0].strip()
    return {"id": channel_id, "name": name}


class ChildProcessRegistry(QObject):
    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.processes: list[QProcess] = []

    def add(self, process: QProcess) -> None:
        process.setParent(self)
        self.processes.append(process)
        process.finished.connect(lambda _code=0, _status=None, target=process: self.remove(target))

    def remove(self, process: QProcess) -> None:
        if process in self.processes:
            self.processes.remove(process)

    def terminate_all(self, timeout_ms: int = 5000) -> None:
        for process in list(self.processes):
            if process.state() == QProcess.ProcessState.NotRunning:
                self.remove(process)
                continue
            process.terminate()
        for process in list(self.processes):
            if process.state() != QProcess.ProcessState.NotRunning and not process.waitForFinished(timeout_ms):
                process.kill()
                process.waitForFinished(timeout_ms)


TIMESHIFT_VIDEO_SUFFIXES = {".mp4", ".mkv", ".webm", ".flv", ".ts"}


class TimeshiftDropList(QListWidget):
    files_dropped = pyqtSignal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DropOnly)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setAlternatingRowColors(True)

    @staticmethod
    def event_video_paths(event) -> list[Path]:
        mime = event.mimeData()
        if not mime or not mime.hasUrls():
            return []
        return [
            Path(url.toLocalFile())
            for url in mime.urls()
            if url.isLocalFile() and Path(url.toLocalFile()).suffix.lower() in TIMESHIFT_VIDEO_SUFFIXES
        ]

    def dragEnterEvent(self, event) -> None:
        if self.event_video_paths(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:
        if self.event_video_paths(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:
        paths = self.event_video_paths(event)
        if not paths:
            event.ignore()
            return
        event.acceptProposedAction()
        self.files_dropped.emit(paths)


class TimeshiftFinalizeSignals(QObject):
    progress = pyqtSignal(str)
    detail = pyqtSignal(str)
    finished = pyqtSignal(object)


FINALIZE_STAGE_DISPLAY_NAMES = {
    "collect_segments": "録画区間収集",
    "make_gaps": "欠損区間確認",
    "concat_video": "動画連結",
    "extract_wav": "音声抽出",
    "transcribe": "文字起こし",
    "encode_mp3": "MP3生成",
    "archive_steps": "HTMLアーカイブ生成",
}

FINALIZE_LEGACY_STEP_DISPLAY_NAMES = {
    "step01_data_collector": "放送データ収集",
    "step02_audio_transcriber": "音声文字起こし",
    "step03_emotion_scorer": "感情スコア生成",
    "step04_word_analyzer": "単語分析",
    "step05_summarizer": "要約生成",
    "step06_music_generator": "音楽生成",
    "step07_image_generator": "抽象画像生成",
    "step08_conversation_generator": "AI会話生成",
    "step09_screenshot_generator": "スクリーンショット生成",
    "step10_comment_processor": "コメント処理",
    "step11_special_user_html_generator": "特殊ユーザーHTML生成",
    "step12_html_generator": "HTML生成",
    "step13_index_generator": "配信者インデックス生成",
    "step14_modern_list_generator": "一覧生成",
    "step15_lolipop_uploader": "アーカイブ公開",
}


def finalize_stage_start_display(stage: str, message: str) -> str:
    if message == "stage=running":
        return FINALIZE_STAGE_DISPLAY_NAMES.get(stage, stage)
    prefix = "legacy step開始:"
    if message.startswith(prefix):
        step_name = message[len(prefix) :].strip()
        display_name = FINALIZE_LEGACY_STEP_DISPLAY_NAMES.get(step_name, step_name)
        return f"{display_name}（{step_name}）"
    return ""


class TimeshiftFinalizeJob(QRunnable):
    def __init__(
        self,
        groups: dict[str, list[Path]],
        *,
        legacy_steps: list[str],
        run_preprocessing: bool,
    ) -> None:
        super().__init__()
        ordered_lvs = tracker.sort_broadcast_lvs_oldest_first(list(groups))
        self.groups = {lv: list(groups[lv]) for lv in ordered_lvs}
        self.legacy_steps = list(legacy_steps)
        self.run_preprocessing = bool(run_preprocessing)
        self.signals = TimeshiftFinalizeSignals()

    @pyqtSlot()
    def run(self) -> None:
        require_timeshift_process()
        successes: list[dict[str, Any]] = []
        failures: list[dict[str, str]] = []
        for lv, paths in self.groups.items():
            self.signals.progress.emit(f"{lv}: タイムシフト処理開始（{len(paths)}ファイル / parsec=0）")
            try:
                config = tracker.load_config()
                self.signals.progress.emit(f"{lv}: コメントDB確認開始")
                comment_result = tracker.download_and_store_archive_comments(
                    lv,
                    config,
                )
                if comment_result.get("reused"):
                    expected_count = comment_result.get("expected_count")
                    if expected_count == 0 and not comment_result.get("stored_count"):
                        self.signals.progress.emit(
                            f"{lv}: コメントなし確認（番組メタ総数=0 / API取得なし）"
                        )
                    else:
                        self.signals.progress.emit(
                            f"{lv}: コメントDB再利用 "
                            f"保存済み={comment_result.get('stored_count', 0)} "
                            f"番組総数={expected_count if expected_count is not None else '不明'}"
                        )
                else:
                    self.signals.progress.emit(
                        f"{lv}: コメントAPI取得・保存完了 "
                        f"取得={comment_result['fetched_count']} "
                        f"新規={comment_result['inserted_count']} "
                        f"重複={comment_result['duplicate_count']}"
                    )
                self.signals.progress.emit(
                    f"{lv}: 実行Step={','.join(self.legacy_steps) or 'なし'}"
                )

                def emit_stage_start(stage: str, message: str) -> None:
                    display = finalize_stage_start_display(stage, message)
                    if display:
                        self.signals.progress.emit(f"{lv}: 工程開始: {display}")

                broadcaster_id, _step_defaults = tracker.broadcaster_archive_step_defaults(lv)
                if self.run_preprocessing:
                    downstream_steps = [
                        step for step in self.legacy_steps
                        if step not in {"step01_data_collector", "step02_audio_transcriber"}
                    ]
                    result = tracker.run_finalize_pipeline_for_lv(
                        lv,
                        broadcaster_id=broadcaster_id,
                        input_dir=paths[0].parent,
                        transcribe="step02_audio_transcriber" in self.legacy_steps,
                        timeline_mode="timeshift",
                        segment_paths=paths,
                        legacy_steps=downstream_steps,
                        progress_callback=lambda message, current_lv=lv: self.signals.detail.emit(
                            f"{current_lv}: {message}"
                        ),
                    )
                else:
                    legacy_result = tracker.run_legacy_archiver_steps(
                        lv,
                        account_id=broadcaster_id or None,
                        steps=self.legacy_steps,
                        force_overwrite_existing_html=True,
                        input_video_paths=paths,
                        progress_callback=lambda message, current_lv=lv: self.signals.detail.emit(
                            f"{current_lv}: {message}"
                        ),
                    )
                    result = {"lv": lv, "legacy_archiver": legacy_result}
                html_file = (
                    result.get("legacy_archiver", {})
                    .get("steps", {})
                    .get("step12_html_generator", {})
                    .get("result", {})
                    .get("html_file", "")
                )
                archive_result: dict[str, Any] = {}
                if self.run_preprocessing:
                    archive_result = tracker.archive_processed_video_files(
                        lv,
                        paths,
                        target_dir=result.get("target_dir"),
                        html_file=html_file,
                    )
                    self.signals.progress.emit(
                        f"{lv}: 元動画をアーカイブへ移動 "
                        f"{len(archive_result.get('moved') or [])}件 → "
                        f"{archive_result.get('archive_dir')}"
                    )
                summary = {
                    "lv": lv,
                    "target_dir": str(result.get("target_dir") or ""),
                    "mp3_path": str(result.get("mp3_path") or ""),
                    "html_file": str(html_file or ""),
                    "video_archive": archive_result,
                    "comments": comment_result,
                    "total_duration_seconds": float(
                        result.get("recording_segment_timeline", {}).get("total_duration_seconds") or 0.0
                    ),
                }
                successes.append(summary)
                self.signals.progress.emit(
                    f"{lv}: 完了 total={summary['total_duration_seconds']:.3f}s HTML={summary['html_file']}"
                )
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                failures.append({"lv": lv, "error": error})
                self.signals.progress.emit(f"{lv}: 失敗 {error}")
        self.signals.finished.emit({"successes": successes, "failures": failures})


class TimeshiftLocalFilesTab(QWidget):
    archive_step_names = list(FINALIZE_LEGACY_STEP_DISPLAY_NAMES)
    file_dialog_state_path = tracker.DATA_DIR / "timeshift_local_files_ui.json"

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.paths: list[Path] = []
        self.active_job: TimeshiftFinalizeJob | None = None
        self.last_video_directory = self.load_last_video_directory()

        explanation = QLabel(
            "ローカルに保存済みの動画を複数D&Dしてください。lvごとに保存済みコメントを確認し、必要な場合だけコメントAPIを取得してから、parsec=0で処理します。動画は連結せずMP3だけ連結します。\n"
            "古い放送から処理し、成功後の元動画は生成HTMLと同じフォルダの archive サブフォルダへ移動します。\n"
            "ファイル名に lv番号が必要です。生成済みHTMLがあるlvは自動的に除外します。"
        )
        explanation.setWordWrap(True)
        self.drop_list = TimeshiftDropList()
        self.drop_list.setMinimumHeight(230)
        self.drop_list.files_dropped.connect(self.add_paths)
        self.drop_list.setToolTip("複数の動画ファイルをここへドロップ")

        self.step_checks: dict[str, QCheckBox] = {}
        self.step_box = QGroupBox("実行するStep（チェックなしはスキップ）")
        self.step_box.setVisible(True)
        self.step_box.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        step_layout = QVBoxLayout(self.step_box)
        for offset in (0, 8):
            row = QHBoxLayout()
            for step_name in self.archive_step_names[offset : offset + 8]:
                check = QCheckBox(step_name.replace("step", ""))
                check.setToolTip(FINALIZE_LEGACY_STEP_DISPLAY_NAMES[step_name])
                check.setChecked(True)
                self.step_checks[step_name] = check
                row.addWidget(check)
            row.addStretch(1)
            step_layout.addLayout(row)

        add_button = QPushButton("動画を追加")
        add_button.clicked.connect(self.choose_files)
        remove_button = QPushButton("選択を削除")
        remove_button.clicked.connect(self.remove_selected)
        clear_button = QPushButton("クリア")
        clear_button.clicked.connect(self.clear_paths)
        self.start_button = QPushButton("ローカル動画からHTML作成")
        self.start_button.clicked.connect(self.start_finalize)

        controls = QWidget()
        controls_layout = QHBoxLayout(controls)
        controls_layout.setContentsMargins(0, 0, 0, 0)
        controls_layout.addWidget(add_button)
        controls_layout.addWidget(remove_button)
        controls_layout.addWidget(clear_button)
        controls_layout.addStretch(1)
        controls_layout.addWidget(self.start_button)

        self.status_view = QTextEdit()
        self.status_view.setReadOnly(True)
        self.status_view.setMinimumHeight(150)
        self.status_view.setPlaceholderText("処理状況")

        layout = QVBoxLayout(self)
        layout.addWidget(self.step_box)
        layout.addWidget(explanation)
        layout.addWidget(self.drop_list, 1)
        layout.addWidget(controls)
        layout.addWidget(self.status_view, 1)

    def append_status(self, message: str) -> None:
        self.status_view.append(message)
        append_app_log(f"タイムシフト: {message}", "INFO")

    def append_detail_status(self, message: str) -> None:
        self.status_view.append(message)

    @staticmethod
    def lv_from_path(path: Path) -> str:
        match = re.search(r"lv\d+", path.name, flags=re.IGNORECASE)
        return match.group(0).lower() if match else ""

    def choose_files(self) -> None:
        paths, _filter = QFileDialog.getOpenFileNames(
            self,
            "タイムシフト動画を選択",
            str(self.last_video_directory) if self.last_video_directory else "",
            "動画 (*.mp4 *.mkv *.webm *.flv *.ts);;すべて (*)",
        )
        if paths:
            self.last_video_directory = Path(paths[0]).parent
            self.save_last_video_directory()
        self.add_paths([Path(path) for path in paths])

    def load_last_video_directory(self) -> Path | None:
        try:
            payload = json.loads(self.file_dialog_state_path.read_text(encoding="utf-8-sig"))
            directory = Path(str(payload.get("last_video_directory") or "").strip())
            return directory if directory.is_dir() else None
        except Exception:
            return None

    def save_last_video_directory(self) -> None:
        if self.last_video_directory is None:
            return
        try:
            self.file_dialog_state_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.file_dialog_state_path.with_suffix(".json.tmp")
            temporary.write_text(
                json.dumps(
                    {"last_video_directory": str(self.last_video_directory)},
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            temporary.replace(self.file_dialog_state_path)
        except Exception:
            append_app_log(traceback.format_exc(), "DEBUG")

    @pyqtSlot(object)
    def add_paths(self, paths) -> None:
        existing = {str(path.resolve()).casefold() for path in self.paths if path.exists()}
        added = False
        for raw_path in paths or []:
            path = Path(raw_path)
            if not path.is_file() or path.suffix.lower() not in TIMESHIFT_VIDEO_SUFFIXES:
                self.append_status(f"除外（動画ではない）: {path}")
                continue
            lv = self.lv_from_path(path)
            if not lv:
                self.append_status(f"除外（lv番号なし）: {path.name}")
                continue
            resolved_key = str(path.resolve()).casefold()
            if resolved_key in existing:
                continue
            existing.add(resolved_key)
            self.paths.append(path)
            added = True
            self.append_status(f"追加: {lv} / {path.name}")
        if added:
            self.sort_paths_oldest_first()
            self.refresh_list()
            ordered_lvs = tracker.sort_broadcast_lvs_oldest_first(
                [self.lv_from_path(path) for path in self.paths]
            )
            self.append_status(f"処理順（古い→新しい）: {' → '.join(ordered_lvs)}")
            self.apply_broadcaster_step_defaults(ordered_lvs[0])

    def apply_broadcaster_step_defaults(self, lv: str) -> None:
        broadcaster_id, defaults = tracker.broadcaster_archive_step_defaults(lv)
        for step_name, check in self.step_checks.items():
            check.setChecked(bool(defaults.get(step_name, True)))
        self.append_status(
            f"{lv}: 配信者{broadcaster_id or '不明'}の生成設定をStepチェックに反映"
        )

    def sort_paths_oldest_first(self) -> None:
        ordered_lvs = tracker.sort_broadcast_lvs_oldest_first(
            [self.lv_from_path(path) for path in self.paths]
        )
        positions = {lv: index for index, lv in enumerate(ordered_lvs)}
        self.paths.sort(
            key=lambda path: positions.get(self.lv_from_path(path), len(positions))
        )

    def remove_selected(self) -> None:
        rows = sorted((self.drop_list.row(item) for item in self.drop_list.selectedItems()), reverse=True)
        for row in rows:
            self.drop_list.takeItem(row)
            if 0 <= row < len(self.paths):
                self.paths.pop(row)

    def clear_paths(self) -> None:
        if self.active_job is not None:
            return
        self.paths.clear()
        self.drop_list.clear()

    def start_finalize(self) -> None:
        if self.active_job is not None:
            return
        self.sort_paths_oldest_first()
        groups: dict[str, list[Path]] = {}
        retained: list[Path] = []
        for path in self.paths:
            lv = self.lv_from_path(path)
            groups.setdefault(lv, []).append(path)
            retained.append(path)
        self.paths = retained
        self.refresh_list()
        if not groups:
            QMessageBox.information(self, "タイムシフト", "処理対象の動画がありません。")
            return
        self.append_status(f"処理開始順（古い→新しい）: {' → '.join(groups)}")
        self.start_button.setEnabled(False)
        selected_steps = [
            step_name
            for step_name in self.archive_step_names
            if self.step_checks[step_name].isChecked()
        ]
        if not selected_steps:
            QMessageBox.information(self, "タイムシフト", "実行するStepが選択されていません。")
            return
        self.append_status(
            f"開始={selected_steps[0]} / スキップ="
            + ",".join(step for step in self.archive_step_names if step not in selected_steps)
        )
        run_preprocessing = any(
            step in selected_steps
            for step in {"step01_data_collector", "step02_audio_transcriber"}
        )
        self.active_job = TimeshiftFinalizeJob(
            groups,
            legacy_steps=selected_steps,
            run_preprocessing=run_preprocessing,
        )
        self.active_job.signals.progress.connect(self.append_status)
        self.active_job.signals.detail.connect(self.append_detail_status)
        self.active_job.signals.finished.connect(self.finalize_finished)
        QThreadPool.globalInstance().start(self.active_job)

    def refresh_list(self) -> None:
        self.drop_list.clear()
        for path in self.paths:
            lv = self.lv_from_path(path)
            item = QListWidgetItem(f"{lv}  |  {path}")
            item.setData(Qt.ItemDataRole.UserRole, str(path))
            self.drop_list.addItem(item)

    @pyqtSlot(object)
    def finalize_finished(self, payload: dict[str, Any]) -> None:
        successful_lvs = {str(row.get("lv") or "") for row in payload.get("successes") or []}
        self.paths = [path for path in self.paths if self.lv_from_path(path) not in successful_lvs]
        self.refresh_list()
        failures = payload.get("failures") or []
        self.append_status(
            f"一括処理終了: 成功{len(successful_lvs)}件 / 失敗{len(failures)}件"
        )
        self.active_job = None
        self.start_button.setEnabled(True)


class BroadcastTagEditJob(QRunnable):
    def __init__(self, broadcaster_id: str, lv: str, upload: bool) -> None:
        super().__init__()
        self.broadcaster_id = broadcaster_id
        self.lv = lv
        self.upload = upload
        self.signals = TimeshiftAcquireSignals()

    @pyqtSlot()
    def run(self) -> None:
        try:
            steps = ["step12_html_generator", "step13_index_generator", "step14_modern_list_generator"]
            if self.upload:
                steps.append("step15_lolipop_uploader")
            self.signals.progress.emit(f"{self.lv}: タグ再生成開始")
            tracker.run_legacy_archiver_steps(
                self.lv,
                account_id=self.broadcaster_id,
                steps=steps,
                force_overwrite_existing_html=True,
                upload_html_only=True,
                progress_callback=self.signals.progress.emit,
            )
            self.signals.finished.emit({"ok": True})
        except Exception as exc:
            self.signals.finished.emit({"ok": False, "error": f"{type(exc).__name__}: {exc}"})


class IntervalTranscriptionJob(QRunnable):
    def __init__(self, broadcaster_id: str, lv: str, start: float, end: float, db_ids: list[int], row: int) -> None:
        super().__init__()
        self.broadcaster_id = broadcaster_id
        self.lv = lv
        self.start = float(start)
        self.end = float(end)
        self.db_ids = list(db_ids)
        self.row = row
        self.signals = TimeshiftAcquireSignals()

    @pyqtSlot()
    def run(self) -> None:
        temporary_audio: Path | None = None
        try:
            target_dir = tracker.broadcast_target_dir(
                self.lv, tracker.load_config(), broadcaster_id=self.broadcaster_id
            )
            source_audio = target_dir / f"{self.lv}_audio.mp3"
            if not source_audio.is_file():
                raise FileNotFoundError(f"音声ファイルがありません: {source_audio}")
            duration = max(0.1, self.end - self.start)
            self.signals.progress.emit("区間音声を切り出しています")
            work_dir = target_dir / "_interval_transcription"
            work_dir.mkdir(parents=True, exist_ok=True)
            temporary_audio = work_dir / f"{self.lv}_{int(self.start * 1000)}_{int(self.end * 1000)}.wav"
            subprocess.run(
                [
                    "ffmpeg", "-y", "-ss", f"{self.start:.3f}", "-i", str(source_audio),
                    "-t", f"{duration:.3f}", "-vn", "-ac", "1", "-ar", "16000", str(temporary_audio),
                ],
                check=True,
                capture_output=True,
                timeout=max(60, int(duration * 4)),
            )
            settings = tracker.resolve_monitored_broadcaster_transcription_settings(
                self.lv, broadcaster_id=self.broadcaster_id
            )
            self.signals.progress.emit(
                f"Faster-Whisper認識中 model={settings['faster_whisper_model']}（初回はモデル読込に時間がかかります）"
            )
            tracker.transcribe_audio_with_faster_whisper(
                self.lv,
                temporary_audio,
                model_size=str(settings["faster_whisper_model"]),
                target_dir=target_dir,
                timeline_offset_seconds=self.start,
                timeline_end_seconds=self.end,
                replace_scope="source",
                mark_postprocess_done=False,
                initial_prompt=str(settings["initial_prompt"]),
                hotwords=str(settings["hotwords"]),
                progress_callback=self.signals.progress.emit,
            )
            self.signals.progress.emit("認識結果をDBとJSONへ反映しています")
            with tracker.connect() as conn:
                generated = conn.execute(
                    "SELECT id, text FROM archive_transcript_segments "
                    "WHERE lv = ? AND source_audio_path = ? ORDER BY start_seconds, id",
                    (self.lv, str(temporary_audio)),
                ).fetchall()
                text_value = " ".join(str(item["text"] or "").strip() for item in generated).strip()
                conn.execute(
                    "DELETE FROM archive_transcript_segments WHERE lv = ? AND source_audio_path = ?",
                    (self.lv, str(temporary_audio)),
                )
                if self.db_ids:
                    primary_id = self.db_ids[0]
                    old = conn.execute(
                        "SELECT raw_json FROM archive_transcript_segments WHERE id = ? AND lv = ?",
                        (primary_id, self.lv),
                    ).fetchone()
                    raw = json.loads(str(old["raw_json"] or "{}")) if old else {}
                    raw["text"] = text_value
                    conn.execute(
                        "UPDATE archive_transcript_segments SET text = ?, raw_json = ? WHERE id = ? AND lv = ?",
                        (text_value, json.dumps(raw, ensure_ascii=False), primary_id, self.lv),
                    )
                    if len(self.db_ids) > 1:
                        placeholders = ",".join("?" for _ in self.db_ids[1:])
                        conn.execute(
                            f"DELETE FROM archive_transcript_segments WHERE lv = ? AND id IN ({placeholders})",
                            (self.lv, *self.db_ids[1:]),
                        )
                    saved_id = primary_id
                else:
                    tracker.save_transcript_segments(
                        conn,
                        self.lv,
                        [{"start_seconds": self.start, "end_seconds": self.end, "text": text_value}],
                        source_audio_path=str(source_audio),
                        model=f"faster-whisper:{settings['faster_whisper_model']}",
                    )
                    saved_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
                tracker.export_legacy_transcript_file_from_db(conn, self.lv, target_dir=target_dir)
                conn.commit()
            self.signals.finished.emit(
                {"ok": True, "row": self.row, "db_id": saved_id, "text": text_value}
            )
        except Exception as exc:
            self.signals.finished.emit({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
        finally:
            if temporary_audio is not None:
                temporary_audio.unlink(missing_ok=True)


class DisappearedUrlScanJob(QRunnable):
    def __init__(self, broadcaster_id: str) -> None:
        super().__init__()
        self.broadcaster_id = broadcaster_id
        self.signals = TimeshiftAcquireSignals()

    @pyqtSlot()
    def run(self) -> None:
        try:
            path = DisappearedBroadcastUrlTab.path_for(self.broadcaster_id)
            account_dir = path.parent
            index_path = account_dir / "index.html"
            lvs: list[str] = []
            if index_path.is_file():
                document = index_path.read_text(encoding="utf-8-sig", errors="replace")
                match = re.search(
                    r'<script\b[^>]*\bid=["\']archive-data["\'][^>]*>(.*?)</script>',
                    document,
                    re.IGNORECASE | re.DOTALL,
                )
                if match:
                    records = json.loads(match.group(1))
                    if isinstance(records, list):
                        lvs.extend(
                            str(row.get("lv") or "").strip().lower()
                            for row in records if isinstance(row, dict)
                        )
            if not lvs:
                lvs.extend(item.name.lower() for item in account_dir.glob("lv*") if item.is_dir())
            lvs = sorted({lv for lv in lvs if re.fullmatch(r"lv\d+", lv)})
            if not lvs:
                raise RuntimeError("生成済み放送が見つかりません")
            self.signals.progress.emit(f"生成済み放送{len(lvs)}件の実URL確認を開始")
            session = requests.Session()
            session.headers.update({"User-Agent": "Mozilla/5.0 niconico-watch-app"})
            deleted: list[str] = []
            errors: list[str] = []
            for index, lv in enumerate(lvs, start=1):
                try:
                    response = session.get(
                        f"https://live.nicovideo.jp/watch/{lv}", timeout=20, allow_redirects=True
                    )
                    if response.status_code == 404:
                        deleted.append(lv)
                        self.signals.progress.emit(f"消滅検出: {lv} ({index}/{len(lvs)})")
                    elif response.status_code != 200:
                        errors.append(f"{lv}: HTTP {response.status_code}")
                except Exception as exc:
                    errors.append(f"{lv}: {type(exc).__name__}: {exc}")
            payload: dict[str, Any] = {}
            if path.is_file():
                loaded = json.loads(path.read_text(encoding="utf-8-sig"))
                payload = loaded if isinstance(loaded, dict) else {}
            registered = payload.get("_history_deleted", [])
            registered = [str(value).strip() for value in registered] if isinstance(registered, list) else []
            payload["_history_deleted"] = sorted(set([*registered, *deleted]))
            urls = payload.get("_history_deleted_urls", {})
            urls = dict(urls) if isinstance(urls, dict) else {}
            for lv in deleted:
                urls[lv] = f"https://live.nicovideo.jp/watch/{lv}"
            payload["_history_deleted_urls"] = urls
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(path)
            if deleted:
                anchor = deleted[-1]
                tracker.run_legacy_archiver_steps(
                    anchor,
                    account_id=self.broadcaster_id,
                    steps=[
                        "step13_index_generator",
                        "step14_modern_list_generator",
                        "step15_lolipop_uploader",
                    ],
                    upload_html_only=True,
                    progress_callback=self.signals.progress.emit,
                )
            self.signals.finished.emit(
                {"ok": True, "deleted": deleted, "checked": len(lvs), "errors": errors}
            )
        except Exception as exc:
            self.signals.finished.emit({"ok": False, "error": f"{type(exc).__name__}: {exc}"})


class DisappearedBroadcastUrlTab(QWidget):
    """ニコニコの放送履歴から消えた放送を一覧表示用に手動登録する。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.active_job: BroadcastTagEditJob | None = None
        self.scan_job: DisappearedUrlScanJob | None = None
        self.broadcaster_edit = QLineEdit("39532023")
        self.broadcaster_edit.setPlaceholderText("配信者ID")
        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText(
            "https://warehouse.bitter.jp/niconico/39532023/lv350997452/lv350997452_あ.html"
        )
        self.register_button = QPushButton("消滅URLとして登録")
        self.unregister_button = QPushButton("登録を解除")
        self.scan_button = QPushButton("生成済み全放送を確認して自動登録")
        self.register_button.clicked.connect(lambda: self.set_registered(True))
        self.unregister_button.clicked.connect(lambda: self.set_registered(False))
        self.scan_button.clicked.connect(self.scan_all)
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["LV", "消滅した放送URL"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.status_view = QTextEdit()
        self.status_view.setReadOnly(True)
        self.status_view.setMaximumHeight(110)

        layout = QVBoxLayout(self)
        explanation = QLabel(
            "ニコニコの放送履歴から削除された放送URLを登録します。登録した放送はindex.htmlで心拍状に赤く点滅します。"
        )
        explanation.setWordWrap(True)
        layout.addWidget(explanation)
        broadcaster_row = QHBoxLayout()
        broadcaster_row.addWidget(QLabel("配信者ID"))
        broadcaster_row.addWidget(self.broadcaster_edit, 1)
        broadcaster_row.addWidget(self.scan_button)
        layout.addLayout(broadcaster_row)
        row = QHBoxLayout()
        row.addWidget(QLabel("放送URL / lv"))
        row.addWidget(self.url_edit, 1)
        row.addWidget(self.register_button)
        row.addWidget(self.unregister_button)
        layout.addLayout(row)
        layout.addWidget(self.table, 1)
        layout.addWidget(self.status_view)
        self.url_edit.returnPressed.connect(lambda: self.set_registered(True))
        self.reload_table(self.broadcaster_edit.text().strip())

    def identity(self) -> tuple[str, str, str]:
        source = self.url_edit.text().strip()
        match = re.search(r"lv\d+", source, re.IGNORECASE)
        lv = match.group(0).lower() if match else ""
        account_match = re.search(r"/niconico/(\d+)/", source, re.IGNORECASE)
        broadcaster_id = account_match.group(1) if account_match else ""
        if lv and not broadcaster_id:
            with tracker.connect() as conn:
                row = conn.execute(
                    """
                    SELECT broadcaster_id FROM broadcast_archive_meta WHERE lv = ?
                    UNION ALL SELECT broadcaster_id FROM recording_jobs WHERE lv = ?
                    LIMIT 1
                    """,
                    (lv, lv),
                ).fetchone()
            broadcaster_id = str(row["broadcaster_id"] or "").strip() if row else ""
        if not broadcaster_id or not lv:
            raise ValueError("URLから配信者IDとlv番号を特定できません")
        url = source if source.startswith(("http://", "https://")) else f"https://live.nicovideo.jp/watch/{lv}"
        return broadcaster_id, lv, url

    @staticmethod
    def path_for(broadcaster_id: str) -> Path:
        root = tracker.niconico_platform_target_root(tracker.load_config()) / broadcaster_id
        for name in ("broadcast", "bloadcast"):
            candidate = root / name
            if candidate.is_dir():
                return candidate / "index_person_tags.json"
        return root / "broadcast" / "index_person_tags.json"

    def set_registered(self, enabled: bool) -> None:
        try:
            broadcaster_id, lv, url = self.identity()
            path = self.path_for(broadcaster_id)
            payload: dict[str, Any] = {}
            if path.is_file():
                loaded = json.loads(path.read_text(encoding="utf-8-sig"))
                payload = loaded if isinstance(loaded, dict) else {}
            values = payload.get("_history_deleted", [])
            values = [str(value).strip() for value in values] if isinstance(values, list) else []
            if enabled and lv not in values:
                values.append(lv)
            if not enabled:
                values = [value for value in values if value != lv]
            if values:
                payload["_history_deleted"] = sorted(set(values))
            else:
                payload.pop("_history_deleted", None)
            urls = payload.get("_history_deleted_urls", {})
            urls = dict(urls) if isinstance(urls, dict) else {}
            if enabled:
                urls[lv] = url
            else:
                urls.pop(lv, None)
            if urls:
                payload["_history_deleted_urls"] = urls
            else:
                payload.pop("_history_deleted_urls", None)
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(path)
            self.reload_table(broadcaster_id)
            self.status_view.append(f"{lv}: {'登録' if enabled else '解除'}しました。一覧を再生成します")
            self.register_button.setEnabled(False)
            self.unregister_button.setEnabled(False)
            self.active_job = BroadcastTagEditJob(broadcaster_id, lv, True)
            self.active_job.signals.progress.connect(self.status_view.append)
            self.active_job.signals.finished.connect(self.finished)
            QThreadPool.globalInstance().start(self.active_job)
        except Exception as exc:
            QMessageBox.critical(self, "消滅URL登録", str(exc))

    def scan_all(self) -> None:
        broadcaster_id = self.broadcaster_edit.text().strip()
        if not broadcaster_id.isdigit() or self.scan_job is not None:
            return
        self.scan_button.setEnabled(False)
        self.scan_job = DisappearedUrlScanJob(broadcaster_id)
        self.scan_job.signals.progress.connect(self.status_view.append)
        self.scan_job.signals.finished.connect(self.scan_finished)
        QThreadPool.globalInstance().start(self.scan_job)

    @pyqtSlot(object)
    def scan_finished(self, result: dict[str, Any]) -> None:
        broadcaster_id = self.broadcaster_edit.text().strip()
        if result.get("ok"):
            deleted = result.get("deleted") or []
            errors = result.get("errors") or []
            self.reload_table(broadcaster_id)
            self.status_view.append(
                f"一括確認完了: 確認{result.get('checked')}件 / 消滅{len(deleted)}件 / 確認失敗{len(errors)}件"
            )
        else:
            self.status_view.append(f"一括確認失敗: {result.get('error')}")
        self.scan_job = None
        self.scan_button.setEnabled(True)

    def reload_table(self, broadcaster_id: str) -> None:
        path = self.path_for(broadcaster_id)
        payload = json.loads(path.read_text(encoding="utf-8-sig")) if path.is_file() else {}
        values = payload.get("_history_deleted", []) if isinstance(payload, dict) else []
        urls = payload.get("_history_deleted_urls", {}) if isinstance(payload, dict) else {}
        urls = urls if isinstance(urls, dict) else {}
        self.table.setRowCount(0)
        for lv in values if isinstance(values, list) else []:
            row = self.table.rowCount()
            self.table.insertRow(row)
            self.table.setItem(row, 0, QTableWidgetItem(str(lv)))
            self.table.setItem(row, 1, QTableWidgetItem(str(urls.get(str(lv), ""))))

    @pyqtSlot(object)
    def finished(self, result: dict[str, Any]) -> None:
        self.status_view.append("再生成・アップロード完了" if result.get("ok") else f"失敗: {result.get('error')}")
        self.active_job = None
        self.register_button.setEnabled(True)
        self.unregister_button.setEnabled(True)


class TimeshiftTagEditorTab(QWidget):
    """放送単位の人物タグ追加・誤検出除外を編集する。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.active_job: BroadcastTagEditJob | None = None
        self.interval_job: IntervalTranscriptionJob | None = None
        self.interval_play_end_ms = 0
        self.interval_audio_output = QAudioOutput(self)
        self.interval_player = QMediaPlayer(self)
        self.interval_player.setAudioOutput(self.interval_audio_output)
        self.interval_player.positionChanged.connect(self.on_interval_audio_position)
        self.loaded_tags: list[str] = []
        self.broadcaster_edit = QLineEdit()
        self.broadcaster_edit.setPlaceholderText("配信者ID（例: 39532023）")
        self.lv_edit = QLineEdit()
        self.lv_edit.setPlaceholderText("lv番号または放送URL")
        self.load_url_button = QPushButton("URLから呼び出す")
        self.load_url_button.clicked.connect(self.load_values)
        self.tags_table = QTableWidget(0, 1)
        self.tags_table.setHorizontalHeaderLabels(["このHTMLのタグ（チェックなし＝除外）"])
        self.tags_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.tags_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.tags_table.setMaximumHeight(170)
        self.transcript_table = QTableWidget(0, 4)
        self.transcript_table.setHorizontalHeaderLabels(
            ["時間", "文字起こし（文字を直接修正）", "再生操作", "区間再処理"]
        )
        self.transcript_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Interactive
        )
        self.transcript_table.horizontalHeader().setStretchLastSection(False)
        self.transcript_table.horizontalHeader().setMinimumSectionSize(80)
        self.transcript_table.setColumnWidth(0, 220)
        self.transcript_table.setColumnWidth(1, 900)
        self.transcript_table.setColumnWidth(2, 150)
        self.transcript_table.setColumnWidth(3, 190)
        self.transcript_table.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOn
        )
        self.transcript_table.setHorizontalScrollMode(
            QAbstractItemView.ScrollMode.ScrollPerPixel
        )
        self.transcript_table.setTextElideMode(Qt.TextElideMode.ElideNone)
        self.transcript_table.setWordWrap(True)
        self.transcript_table.verticalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        self.transcript_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        load_button = QPushButton("現在の修正を読込")
        load_button.clicked.connect(self.load_values)
        self.upload_check = QCheckBox("保存後に再生成・アップロード")
        self.upload_check.setChecked(True)
        self.save_button = QPushButton("保存して反映")
        self.save_button.clicked.connect(self.save_and_apply)
        self.status_view = QTextEdit()
        self.status_view.setReadOnly(True)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("放送ごとにタグを追加、または誤検出タグを除外します。文字起こし本文は変更しません。"))
        broadcaster_row = QHBoxLayout()
        broadcaster_row.addWidget(QLabel("配信者ID"))
        broadcaster_row.addWidget(self.broadcaster_edit, 1)
        layout.addLayout(broadcaster_row)
        url_row = QHBoxLayout()
        url_row.addWidget(QLabel("放送URL / lv"))
        url_row.addWidget(self.lv_edit, 1)
        url_row.addWidget(self.load_url_button)
        layout.addLayout(url_row)
        layout.addWidget(self.tags_table)
        layout.addWidget(self.transcript_table, 1)
        buttons = QHBoxLayout()
        buttons.addWidget(load_button)
        buttons.addWidget(self.upload_check)
        buttons.addStretch(1)
        buttons.addWidget(self.save_button)
        layout.addLayout(buttons)
        self.status_view.setMaximumHeight(110)
        layout.addWidget(self.status_view)

    @staticmethod
    def format_transcript_time(seconds: float) -> str:
        total = max(0.0, float(seconds))
        hours = int(total // 3600)
        minutes = int((total % 3600) // 60)
        remaining_seconds = total % 60
        return f"{hours:02d}:{minutes:02d}:{remaining_seconds:05.2f}"

    @staticmethod
    def _names(text: str) -> list[str]:
        return list(dict.fromkeys(x.strip() for x in re.split(r"[,、\n]", text) if x.strip()))

    def _identity(self) -> tuple[str, str]:
        source = self.lv_edit.text().strip()
        broadcaster_id = self.broadcaster_edit.text().strip()
        account_match = re.search(r"/niconico/(\d+)/", source, re.IGNORECASE)
        if account_match:
            broadcaster_id = account_match.group(1)
            self.broadcaster_edit.setText(broadcaster_id)
        match = re.search(r"lv\d+", source, re.IGNORECASE)
        lv = match.group(0).lower() if match else ""
        if lv and not broadcaster_id:
            with tracker.connect() as conn:
                row = conn.execute(
                    """
                    SELECT broadcaster_id FROM broadcast_archive_meta WHERE lv = ?
                    UNION ALL
                    SELECT broadcaster_id FROM recording_jobs WHERE lv = ?
                    LIMIT 1
                    """,
                    (lv, lv),
                ).fetchone()
            if row:
                broadcaster_id = str(row["broadcaster_id"] or "").strip()
                self.broadcaster_edit.setText(broadcaster_id)
        if not broadcaster_id or not lv:
            raise ValueError("URLから配信者IDを特定できません。配信者IDも入力してください")
        return broadcaster_id, lv

    def _path(self, broadcaster_id: str) -> Path:
        root = tracker.niconico_platform_target_root(tracker.load_config()) / broadcaster_id
        for name in ("broadcast", "bloadcast"):
            candidate = root / name
            if candidate.is_dir():
                return candidate / "index_person_tags.json"
        return root / "broadcast" / "index_person_tags.json"

    def _payload(self, path: Path) -> dict[str, Any]:
        if not path.is_file():
            return {}
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        return value if isinstance(value, dict) else {}

    def load_values(self) -> None:
        try:
            broadcaster_id, lv = self._identity()
            payload = self._payload(self._path(broadcaster_id))
            account_dir = self._path(broadcaster_id).parent
            html_files = sorted((account_dir / lv).glob(f"{lv}_*.html"))
            if not html_files:
                html_files = sorted((account_dir / lv).glob("*.html"))
            tags: list[str] = []
            if html_files:
                document = html_files[0].read_text(encoding="utf-8-sig", errors="replace")
                tag_match = re.search(
                    r'<script[^>]+id=["\']archive-page-tags["\'][^>]*>(.*?)</script>',
                    document, re.DOTALL | re.IGNORECASE,
                )
                if tag_match:
                    value = json.loads(tag_match.group(1))
                    tags = [str(tag).strip() for tag in value if str(tag).strip()]
            excludes = payload.get("_exclude", {})
            excluded_tags = (
                [str(tag).strip() for tag in excludes.get(lv, []) if str(tag).strip()]
                if isinstance(excludes, dict) else []
            )
            self.loaded_tags = list(dict.fromkeys([*tags, *excluded_tags]))
            self.tags_table.setRowCount(0)
            for tag in self.loaded_tags:
                row = self.tags_table.rowCount()
                self.tags_table.insertRow(row)
                item = QTableWidgetItem(tag)
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(
                    Qt.CheckState.Unchecked if tag in excluded_tags else Qt.CheckState.Checked
                )
                self.tags_table.setItem(row, 0, item)
            self.transcript_table.setRowCount(0)
            with tracker.connect() as conn:
                rows = conn.execute(
                    "SELECT id, start_seconds, end_seconds, text FROM archive_transcript_segments "
                    "WHERE lv = ? ORDER BY start_seconds, id", (lv,),
                ).fetchall()
                duration_row = conn.execute(
                    "SELECT MAX(timeline_start_seconds + duration_seconds) AS total "
                    "FROM recording_segments WHERE lv = ?",
                    (lv,),
                ).fetchone()
            total_seconds = float(duration_row["total"] or 0) if duration_row else 0.0
            if rows:
                total_seconds = max(
                    total_seconds, max(float(segment["end_seconds"] or 0) for segment in rows)
                )
            grouped: dict[int, list[Any]] = {}
            for segment in rows:
                block_start = int(math.floor(float(segment["start_seconds"] or 0) / 10.0) * 10)
                grouped.setdefault(block_start, []).append(segment)
            empty_ranges = 0
            for start in range(0, int(math.ceil(total_seconds / 10.0) * 10), 10):
                end = start + 10.0
                segments = grouped.get(start, [])
                row = self.transcript_table.rowCount()
                self.transcript_table.insertRow(row)
                time_item = QTableWidgetItem(
                    f"{self.format_transcript_time(start)} - {self.format_transcript_time(end)}"
                )
                time_item.setFlags(time_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                db_ids = [int(segment["id"]) for segment in segments]
                time_item.setData(Qt.ItemDataRole.UserRole, db_ids)
                self.transcript_table.setItem(row, 0, time_item)
                self.transcript_table.setItem(
                    row,
                    1,
                    QTableWidgetItem("\n".join(str(segment["text"] or "") for segment in segments)),
                )
                self.install_interval_controls(
                    row, float(start), end, db_ids
                )
                if not segments:
                    empty_ranges += 1
            self.status_view.append(
                f"{lv}: タグ{len(self.loaded_tags)}件 / 文字起こし{len(rows)}件 / "
                f"空区間{empty_ranges}件を読み込みました"
            )
        except Exception as exc:
            QMessageBox.critical(self, "タグ修正", str(exc))

    def install_interval_controls(
        self, row: int, start: float, end: float, db_ids: list[int]
    ) -> None:
        playback = QWidget()
        playback_layout = QHBoxLayout(playback)
        playback_layout.setContentsMargins(0, 0, 0, 0)
        play_button = QPushButton("再生")
        stop_button = QPushButton("停止")
        play_button.clicked.connect(
            lambda _checked=False, start=start, end=end: self.play_interval(start, end)
        )
        stop_button.clicked.connect(lambda _checked=False: self.stop_interval_audio())
        playback_layout.addWidget(play_button)
        playback_layout.addWidget(stop_button)
        self.transcript_table.setCellWidget(row, 2, playback)
        button = QPushButton("この区間を文字起こし")
        button.clicked.connect(
            lambda _checked=False, row=row, start=start, end=end, db_ids=db_ids:
            self.transcribe_interval(row, start, end, db_ids)
        )
        self.transcript_table.setCellWidget(row, 3, button)

    def play_interval(self, start: float, end: float) -> None:
        try:
            broadcaster_id, lv = self._identity()
            audio_path = tracker.broadcast_target_dir(
                lv, tracker.load_config(), broadcaster_id=broadcaster_id
            ) / f"{lv}_audio.mp3"
            if not audio_path.is_file():
                raise FileNotFoundError(f"音声ファイルがありません: {audio_path}")
            source = QUrl.fromLocalFile(str(audio_path.resolve()))
            if self.interval_player.source() != source:
                self.interval_player.setSource(source)
            self.interval_play_end_ms = max(0, int(float(end) * 1000))
            start_ms = max(0, int(float(start) * 1000))
            QTimer.singleShot(100, lambda: self._start_interval_audio(start_ms))
        except Exception as exc:
            QMessageBox.critical(self, "区間再生", str(exc))

    def _start_interval_audio(self, start_ms: int) -> None:
        self.interval_player.setPosition(start_ms)
        self.interval_player.play()

    def stop_interval_audio(self) -> None:
        self.interval_play_end_ms = 0
        self.interval_player.stop()

    def on_interval_audio_position(self, position_ms: int) -> None:
        if self.interval_play_end_ms and position_ms >= self.interval_play_end_ms:
            self.stop_interval_audio()

    def transcribe_interval(
        self, row: int, start: float, end: float, db_ids: list[int]
    ) -> None:
        if self.interval_job is not None:
            self.status_view.append("別の区間を文字起こし中です")
            return
        try:
            broadcaster_id, lv = self._identity()
            self.status_view.append(
                f"{lv}: {self.format_transcript_time(start)} - "
                f"{self.format_transcript_time(end)} を文字起こし開始"
            )
            self.interval_job = IntervalTranscriptionJob(
                broadcaster_id, lv, start, end, db_ids, row
            )
            self.interval_job.signals.progress.connect(self.status_view.append)
            self.interval_job.signals.finished.connect(self.on_interval_transcribed)
            QThreadPool.globalInstance().start(self.interval_job)
        except Exception as exc:
            QMessageBox.critical(self, "区間文字起こし", str(exc))

    @pyqtSlot(object)
    def on_interval_transcribed(self, result: dict[str, Any]) -> None:
        self.interval_job = None
        if not result.get("ok"):
            self.status_view.append(f"区間文字起こし失敗: {result.get('error')}")
            return
        row = int(result["row"])
        if row < self.transcript_table.rowCount():
            text_item = self.transcript_table.item(row, 1)
            if text_item is None:
                text_item = QTableWidgetItem("")
                self.transcript_table.setItem(row, 1, text_item)
            text_item.setText(str(result.get("text") or ""))
            time_item = self.transcript_table.item(row, 0)
            if time_item is not None:
                time_item.setData(Qt.ItemDataRole.UserRole, [int(result["db_id"])])
        self.status_view.append("区間文字起こし完了。文字起こし欄へ反映しました")

    def save_and_apply(self) -> None:
        try:
            broadcaster_id, lv = self._identity()
            path = self._path(broadcaster_id)
            payload = self._payload(path)
            exclusions = [
                self.tags_table.item(row, 0).text().strip()
                for row in range(self.tags_table.rowCount())
                if self.tags_table.item(row, 0)
                and self.tags_table.item(row, 0).text().strip()
                and self.tags_table.item(row, 0).checkState() != Qt.CheckState.Checked
            ]
            exclude_map = payload.get("_exclude")
            if not isinstance(exclude_map, dict):
                exclude_map = {}
            if exclusions:
                exclude_map[lv] = exclusions
            else:
                exclude_map.pop(lv, None)
            if exclude_map:
                payload["_exclude"] = exclude_map
            else:
                payload.pop("_exclude", None)
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(path)
            changed_segments: list[tuple[int, str]] = []
            with tracker.connect() as conn:
                for row in range(self.transcript_table.rowCount()):
                    time_item = self.transcript_table.item(row, 0)
                    text_item = self.transcript_table.item(row, 1)
                    if not time_item or not text_item:
                        continue
                    db_id_values = time_item.data(Qt.ItemDataRole.UserRole) or []
                    if not isinstance(db_id_values, list):
                        db_id_values = [db_id_values]
                    db_ids = [int(value) for value in db_id_values if value is not None]
                    if not db_ids:
                        continue
                    new_text = text_item.text().strip()
                    placeholders = ",".join("?" for _ in db_ids)
                    old_rows = conn.execute(
                        f"SELECT id, text, raw_json FROM archive_transcript_segments "
                        f"WHERE lv = ? AND id IN ({placeholders}) ORDER BY start_seconds, id",
                        (lv, *db_ids),
                    ).fetchall()
                    old_text = "\n".join(str(item["text"] or "") for item in old_rows)
                    if old_rows and new_text != old_text:
                        try:
                            raw = json.loads(str(old_rows[0]["raw_json"] or "{}"))
                        except Exception:
                            raw = {}
                        raw["text"] = new_text
                        primary_id = int(old_rows[0]["id"])
                        conn.execute(
                            "UPDATE archive_transcript_segments SET text = ?, raw_json = ? WHERE id = ? AND lv = ?",
                            (new_text, json.dumps(raw, ensure_ascii=False), primary_id, lv),
                        )
                        if len(old_rows) > 1:
                            extra_ids = [int(item["id"]) for item in old_rows[1:]]
                            extra_placeholders = ",".join("?" for _ in extra_ids)
                            conn.execute(
                                f"DELETE FROM archive_transcript_segments WHERE lv = ? "
                                f"AND id IN ({extra_placeholders})",
                                (lv, *extra_ids),
                            )
                        changed_segments.append((row, new_text))
                conn.commit()
            if changed_segments:
                with tracker.connect() as conn:
                    tracker.export_legacy_transcript_file_from_db(
                        conn, lv, target_dir=path.parent / lv
                    )
            self.status_view.append(
                f"{lv}: 保存完了 / 削除タグ={exclusions or 'なし'} / 文字修正={len(changed_segments)}件"
            )
            self.save_button.setEnabled(False)
            self.active_job = BroadcastTagEditJob(broadcaster_id, lv, self.upload_check.isChecked())
            self.active_job.signals.progress.connect(self.status_view.append)
            self.active_job.signals.finished.connect(self._finished)
            QThreadPool.globalInstance().start(self.active_job)
        except Exception as exc:
            QMessageBox.critical(self, "タグ修正", str(exc))

    @pyqtSlot(object)
    def _finished(self, result: dict[str, Any]) -> None:
        if result.get("ok"):
            self.status_view.append("タグ反映完了")
        else:
            self.status_view.append(f"反映失敗: {result.get('error')}")
        self.active_job = None
        self.save_button.setEnabled(True)


class TimeshiftAcquireSignals(QObject):
    progress = pyqtSignal(str)
    detail = pyqtSignal(str)
    finished = pyqtSignal(object)


class TimeshiftAcquireJob(QRunnable):
    def __init__(
        self,
        input_urls: list[str],
        *,
        create_html: bool = True,
        legacy_steps: list[str] | None = None,
    ) -> None:
        super().__init__()
        self.input_urls = list(input_urls)
        self.create_html = bool(create_html)
        self.legacy_steps = list(legacy_steps or FINALIZE_LEGACY_STEP_DISPLAY_NAMES)
        self.signals = TimeshiftAcquireSignals()

    @pyqtSlot()
    def run(self) -> None:
        require_timeshift_process()
        successes: list[dict[str, Any]] = []
        failures: list[dict[str, str]] = []
        skipped: list[dict[str, str]] = []
        resolved: list[dict[str, Any]] = []
        seen_lvs: set[str] = set()
        for input_url in self.input_urls:
            self.signals.progress.emit(f"URL解析: {input_url}")
            try:
                items = tracker.resolve_timeshift_input_urls([input_url])
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                failures.append({"input": input_url, "error": error})
                self.signals.progress.emit(f"URL解析失敗: {input_url} / {error}")
                continue
            for item in items:
                lv = str(item.get("lv") or "").strip().lower()
                if not lv or lv in seen_lvs:
                    continue
                seen_lvs.add(lv)
                resolved.append(item)
        resolved = tracker.sort_timeshift_download_items_oldest_first(resolved)
        self.signals.progress.emit(f"取得対象を確定: {len(resolved)}件")
        for index, item in enumerate(resolved, start=1):
            deadline_value = item.get("timeshift_end_time")
            try:
                deadline_text = datetime.fromtimestamp(int(deadline_value)).strftime("%Y-%m-%d %H:%M:%S")
            except (TypeError, ValueError, OSError):
                deadline_text = "不明"
            watch_url = str(item.get("watch_url") or "").strip()
            lv = str(item.get("lv") or "").strip()
            self.signals.progress.emit(
                f"[取得可能URL {index}/{len(resolved)}] {watch_url} / {lv} / 視聴期限={deadline_text}"
            )

        config = tracker.load_config()
        for index, item in enumerate(resolved, start=1):
            lv = str(item.get("lv") or "").strip().lower()
            broadcaster_id = str(item.get("broadcaster_id") or "").strip()
            broadcaster_name = str(item.get("broadcaster_name") or "").strip()
            prefix = f"[{index}/{len(resolved)}] {lv}"
            generated = tracker.existing_generated_archive_html(lv)
            if self.create_html and generated is not None:
                skipped.append({"lv": lv, "reason": "html_generated", "path": str(generated)})
                self.signals.progress.emit(f"{prefix}: 除外（HTML生成済み） {generated}")
                continue
            if not broadcaster_id:
                error = "配信者IDがありません"
                failures.append({"lv": lv, "error": error})
                self.signals.progress.emit(f"{prefix}: 失敗 {error}")
                continue

            worker_thread_id = threading.get_ident()

            def forward_tracker_log(level: str, message: str) -> None:
                if threading.get_ident() != worker_thread_id:
                    return
                lv_prefix = f"{lv} "
                if not message.startswith(lv_prefix):
                    return
                detail = message[len(lv_prefix) :]
                self.signals.detail.emit(f"{prefix}: [{level}] {detail}")

            tracker.add_log_sink(forward_tracker_log)
            try:
                self.signals.progress.emit(
                    f"{prefix}: 動画取得開始 / 配信者={broadcaster_id} {broadcaster_name}"
                )
                video_result = tracker.download_timeshift_video_with_recorder(
                    item,
                    config,
                    progress_callback=self.signals.progress.emit,
                )
                video_paths = [
                    Path(path)
                    for path in video_result.get("video_paths") or []
                    if Path(path).is_file()
                ]
                if not video_paths:
                    raise RuntimeError("取得済み動画が見つかりません")
                tracker.mark_timeshift_video_download_completed(lv)
                self.signals.progress.emit(f"{prefix}: 動画取得完了をDB記録")

                self.signals.progress.emit(f"{prefix}: コメントDB確認開始")
                comment_result = tracker.download_and_store_archive_comments(
                    lv,
                    config,
                    broadcast_meta=item,
                )
                if comment_result.get("reused"):
                    expected_count = comment_result.get("expected_count")
                    if expected_count == 0 and not comment_result.get("stored_count"):
                        self.signals.progress.emit(
                            f"{prefix}: コメントなし確認（番組メタ総数=0 / API取得なし）"
                        )
                    else:
                        self.signals.progress.emit(
                            f"{prefix}: コメントDB再利用 "
                            f"保存済み={comment_result.get('stored_count', 0)} "
                            f"番組総数={expected_count if expected_count is not None else '不明'}"
                        )
                else:
                    self.signals.progress.emit(
                        f"{prefix}: コメントAPI取得・保存完了 "
                        f"取得={comment_result['fetched_count']} "
                        f"新規={comment_result['inserted_count']} "
                        f"重複={comment_result.get('duplicate_count', 0)}"
                    )

                tracker.mark_timeshift_comments_download_completed(lv)
                tracker.mark_timeshift_download_completed(lv)
                self.signals.progress.emit(f"{prefix}: コメント取得完了をDB記録")

                html_file = ""
                if self.create_html:
                    self.signals.progress.emit(f"{prefix}: HTML生成開始（parsec=0）")
                    legacy_result = tracker.run_legacy_archiver_steps(
                        lv,
                        account_id=broadcaster_id,
                        steps=self.legacy_steps,
                        force_overwrite_existing_html=True,
                        input_video_paths=video_paths,
                    )
                    html_file = (
                        legacy_result.get("steps", {})
                        .get("step12_html_generator", {})
                        .get("result", {})
                        .get("html_file", "")
                    )
                summary = {
                    "lv": lv,
                    "broadcaster_id": broadcaster_id,
                    "broadcaster_name": broadcaster_name,
                    "video_paths": [str(path) for path in video_paths],
                    "video_reused": bool(video_result.get("reused")),
                    "comments": comment_result,
                    "html_file": str(html_file or ""),
                }
                successes.append(summary)
                if self.create_html:
                    self.signals.progress.emit(f"{prefix}: 完了 HTML={summary['html_file']}")
                else:
                    self.signals.progress.emit(f"{prefix}: 完了（動画・コメントのみ）")
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                failures.append({"lv": lv, "error": error})
                self.signals.progress.emit(f"{prefix}: 失敗 {error}")
                append_app_log(traceback.format_exc(), "DEBUG")
            finally:
                tracker.remove_log_sink(forward_tracker_log)
        self.signals.finished.emit(
            {
                "resolved_count": len(resolved),
                "successes": successes,
                "failures": failures,
                "skipped": skipped,
            }
        )


class TimeshiftTab(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.active_job: TimeshiftAcquireJob | None = None

        explanation = QLabel(
            "放送URLまたは配信者の番組一覧URLを、1行に1件入力してください。\n"
            "放送URLはその1件を取得し、配信一覧URLはAPIで現在タイムシフト視聴可能な番組を全件取得します。"
        )
        explanation.setWordWrap(True)

        self.url_input = QTextEdit()
        self.url_input.setMinimumHeight(170)
        self.url_input.setPlaceholderText(
            "https://live.nicovideo.jp/watch/lv350967061\n"
            "https://www.nicovideo.jp/user/39532023/live_programs"
        )

        self.archive_step_names = list(FINALIZE_LEGACY_STEP_DISPLAY_NAMES)
        self.step_checks: dict[str, QCheckBox] = {}
        self.step_box = QGroupBox("実行するStep（チェックなしはスキップ）")
        self.step_box.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        step_layout = QVBoxLayout(self.step_box)
        for offset in (0, 8):
            row = QHBoxLayout()
            for step_name in self.archive_step_names[offset : offset + 8]:
                check = QCheckBox(step_name.replace("step", ""))
                check.setToolTip(FINALIZE_LEGACY_STEP_DISPLAY_NAMES[step_name])
                check.setChecked(step_name not in {"step06_music_generator", "step15_lolipop_uploader"})
                self.step_checks[step_name] = check
                row.addWidget(check)
            row.addStretch(1)
            step_layout.addLayout(row)

        clear_button = QPushButton("URLをクリア")
        clear_button.clicked.connect(self.url_input.clear)
        self.start_button = QPushButton("動画・コメントを取得してHTML作成")
        self.start_button.clicked.connect(lambda: self.start_acquire(create_html=True))
        self.acquire_only_button = QPushButton("動画・コメントのみ取得")
        self.acquire_only_button.clicked.connect(lambda: self.start_acquire(create_html=False))

        controls = QWidget()
        controls_layout = QHBoxLayout(controls)
        controls_layout.setContentsMargins(0, 0, 0, 0)
        controls_layout.addWidget(clear_button)
        controls_layout.addStretch(1)
        controls_layout.addWidget(self.acquire_only_button)
        controls_layout.addWidget(self.start_button)

        self.status_view = QTextEdit()
        self.status_view.setReadOnly(True)
        self.status_view.setMinimumHeight(190)
        self.status_view.setPlaceholderText("URL解析・動画取得・コメント保存・HTML生成の処理状況")

        layout = QVBoxLayout(self)
        layout.addWidget(self.step_box)
        layout.addWidget(explanation)
        layout.addWidget(QLabel("取得URL"))
        layout.addWidget(self.url_input)
        layout.addWidget(controls)
        layout.addWidget(self.status_view, 1)

    def add_input_urls(self, urls: list[str]) -> list[str]:
        existing = normalize_handoff_urls(self.url_input.toPlainText().splitlines())
        existing_set = set(existing)
        added = [url for url in normalize_handoff_urls(urls) if url not in existing_set]
        if added:
            self.url_input.setPlainText("\n".join([*existing, *added]))
            first_lv = next((extract_nicolive_id(url) for url in added if extract_nicolive_id(url)), "")
            if first_lv:
                broadcaster_id, defaults = tracker.broadcaster_archive_step_defaults(first_lv)
                for step_name, check in self.step_checks.items():
                    check.setChecked(bool(defaults.get(step_name, check.isChecked())))
                self.append_status(f"{first_lv}: 配信者{broadcaster_id or '不明'}のStep設定を反映")
        return added

    def append_status(self, message: str) -> None:
        self.status_view.append(message)
        append_app_log(f"タイムシフト: {message}", "INFO")

    def append_detail_status(self, message: str) -> None:
        # tracker側ですでにファイル/DBへ保存済みなので、ここではGUI表示だけを行う。
        self.status_view.append(message)

    def start_acquire(self, *, create_html: bool = True) -> None:
        if self.active_job is not None:
            return
        input_urls = [
            line.strip()
            for line in self.url_input.toPlainText().splitlines()
            if line.strip()
        ]
        if not input_urls:
            QMessageBox.information(self, "タイムシフト", "放送URLまたは配信一覧URLを入力してください。")
            return
        self.start_button.setEnabled(False)
        self.acquire_only_button.setEnabled(False)
        self.url_input.setEnabled(False)
        selected_steps = [
            step_name
            for step_name in self.archive_step_names
            if self.step_checks[step_name].isChecked()
        ]
        if create_html and not selected_steps:
            QMessageBox.information(self, "タイムシフト", "実行するStepが選択されていません。")
            self.start_button.setEnabled(True)
            self.acquire_only_button.setEnabled(True)
            self.url_input.setEnabled(True)
            return
        self.active_job = TimeshiftAcquireJob(
            input_urls,
            create_html=create_html,
            legacy_steps=selected_steps,
        )
        self.active_job.signals.progress.connect(self.append_status)
        self.active_job.signals.detail.connect(self.append_detail_status)
        self.active_job.signals.finished.connect(self.acquire_finished)
        QThreadPool.globalInstance().start(self.active_job)

    @pyqtSlot(object)
    def acquire_finished(self, payload: dict[str, Any]) -> None:
        successes = payload.get("successes") or []
        failures = payload.get("failures") or []
        skipped = payload.get("skipped") or []
        self.append_status(
            f"一括処理終了: 対象{int(payload.get('resolved_count') or 0)}件 / "
            f"成功{len(successes)}件 / 除外{len(skipped)}件 / 失敗{len(failures)}件"
        )
        self.active_job = None
        self.url_input.setEnabled(True)
        self.start_button.setEnabled(True)
        self.acquire_only_button.setEnabled(True)


class LogTab(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.entries: list[tuple[str, str]] = []
        self.level_filter = NoWheelComboBox()
        self.level_filter.addItems(["TRACE", "DEBUG", "INFO", "WARN", "ERROR"])
        self.level_filter.setCurrentText("INFO")
        self.level_filter.currentTextChanged.connect(self.render_logs)
        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        self.log_view.setPlaceholderText("ログはここに出る。選択してコピーできる。")
        LOG_SINKS.append(self)

        copy_button = QPushButton("選択をコピー")
        copy_button.clicked.connect(self.log_view.copy)
        clear_button = QPushButton("クリア")
        clear_button.clicked.connect(self.clear_logs)

        controls = QWidget()
        controls_layout = QHBoxLayout(controls)
        controls_layout.setContentsMargins(0, 0, 0, 0)
        controls_layout.addWidget(QLabel("表示レベル"))
        controls_layout.addWidget(self.level_filter)
        controls_layout.addWidget(copy_button)
        controls_layout.addWidget(clear_button)
        controls_layout.addStretch(1)

        layout = QVBoxLayout(self)
        layout.addWidget(controls)
        layout.addWidget(self.log_view, 1)

    def append_log(self, level: str, text: str) -> None:
        self.entries.append((level, text))
        if LOG_LEVELS[level] < LOG_LEVELS[self.level_filter.currentText()]:
            return
        scrollbar = self.log_view.verticalScrollBar()
        at_bottom = scrollbar.value() >= scrollbar.maximum() - 4
        self.log_view.append(text)
        if at_bottom:
            self.log_view.verticalScrollBar().setValue(self.log_view.verticalScrollBar().maximum())

    def render_logs(self) -> None:
        scrollbar = self.log_view.verticalScrollBar()
        at_bottom = scrollbar.value() >= scrollbar.maximum() - 4
        minimum = LOG_LEVELS[self.level_filter.currentText()]
        self.log_view.setPlainText("\n".join(text for level, text in self.entries if LOG_LEVELS[level] >= minimum))
        if at_bottom:
            self.log_view.verticalScrollBar().setValue(self.log_view.verticalScrollBar().maximum())

    def clear_logs(self) -> None:
        self.entries.clear()
        self.log_view.clear()

    def closeEvent(self, event) -> None:
        if self in LOG_SINKS:
            LOG_SINKS.remove(self)
        super().closeEvent(event)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("niconico-watch-app")
        self.resize(1280, 760)
        self.restore_ui_state()
        self.child_processes = ChildProcessRegistry(self)
        self.comment_stream_manager = CommentStreamManager(self)
        self.pending_auto_open_lives: list[dict[str, Any]] = []
        self.auto_open_seen_lvs: set[str] = set()
        self.auto_open_timer = QTimer(self)
        self.auto_open_timer.setInterval(1000)
        self.auto_open_timer.timeout.connect(self.open_next_auto_live)
        self.ui_state_save_timer = QTimer(self)
        self.ui_state_save_timer.setSingleShot(True)
        self.ui_state_save_timer.setInterval(500)
        self.ui_state_save_timer.timeout.connect(self.save_ui_state)
        self.finalize_dispatcher_timer = QTimer(self)
        self.finalize_dispatcher_timer.setInterval(5000)
        self.finalize_dispatcher_timer.timeout.connect(tracker.start_finalize_dispatcher_process)
        self.finalize_dispatcher_timer.start()
        QTimer.singleShot(0, tracker.start_finalize_dispatcher_process)

        tabs = QTabWidget()
        self.tracker_tab = TrackerTab()
        self.comment_monitor_tab = CommentMonitorTab(self.comment_stream_manager)
        tabs.addTab(self.tracker_tab, "トラッカー")
        tabs.addTab(self.comment_monitor_tab, "コメント監視")
        self.broadcaster_monitor_tab = BroadcasterMonitorTab()
        tabs.addTab(self.broadcaster_monitor_tab, "配信者監視")
        self.special_users_tab = SpecialUsersTab()
        tabs.addTab(self.special_users_tab, "スペシャルユーザー")
        self.inspection_tab = InspectionTab()
        tabs.addTab(self.inspection_tab, "確認")
        self.connect_ui_state_autosave()
        self.settings_tab = SettingsTab()
        tabs.addTab(self.settings_tab, "設定")
        self.log_tab = LogTab()
        tabs.addTab(self.log_tab, "ログ")
        self.setCentralWidget(tabs)
        self.restore_ui_state()

        toolbar = QToolBar("main")
        self.addToolBar(toolbar)
        quit_action = QAction("終了", self)
        quit_action.triggered.connect(self.close)
        toolbar.addAction(quit_action)

        status = QStatusBar()
        status.showMessage("PyQt6 cockpit ready")
        self.setStatusBar(status)
        append_app_log("GUI起動完了", "INFO")
        QTimer.singleShot(1000, self.scan_startup_linked_lives)

    def connect_ui_state_autosave(self) -> None:
        self.inspection_tab.special_splitter.splitterMoved.connect(self.schedule_ui_state_save)
        for table in [
            self.tracker_tab.table,
            self.inspection_tab.special_comments_table,
            self.inspection_tab.special_hits_table,
            self.inspection_tab.linked_broadcaster_filter_table,
            self.inspection_tab.broadcast_filter_table,
            self.inspection_tab.broadcaster_programs_table,
            self.inspection_tab.broadcaster_comments_table,
        ]:
            header = table.horizontalHeader()
            header.sectionResized.connect(self.schedule_ui_state_save)
            header.sectionMoved.connect(self.schedule_ui_state_save)

    def schedule_ui_state_save(self, *_args) -> None:
        self.ui_state_save_timer.start()

    def open_comment_tab(self, lv: str) -> None:
        self.comment_monitor_tab.open_broadcast_tab(lv)

    def scan_startup_linked_lives(self) -> None:
        self.statusBar().showMessage("有効スペシャルユーザーの配信者ページを確認中...")
        job = StartupLiveScanJob()
        job.signals.live_found.connect(self.enqueue_auto_live)
        job.signals.progress.connect(self.statusBar().showMessage)
        job.signals.finished.connect(self.on_startup_live_scan_finished)
        job.signals.failed.connect(self.on_startup_live_scan_failed)
        QThreadPool.globalInstance().start(job)

    def enqueue_auto_live(self, row: object) -> None:
        live = dict(row) if isinstance(row, dict) else {}
        lv = str(live.get("lv") or "").strip()
        if not lv or lv in self.auto_open_seen_lvs:
            return
        broadcaster_id = str(live.get("broadcaster_id") or "").strip()
        auto_open_source = str(live.get("auto_open_source") or "").strip()
        if broadcaster_id and auto_open_source != "special_linked":
            try:
                with tracker.connect() as conn:
                    if broadcaster_id in tracker.disabled_monitored_broadcaster_ids(conn):
                        self.statusBar().showMessage(f"配信者監視OFFのためコメント監視を開かない: {lv} / {broadcaster_id}")
                        return
            except Exception:
                append_app_log(traceback.format_exc(), "DEBUG")
        live.update(self.auto_live_context(live))
        self.auto_open_seen_lvs.add(lv)
        self.pending_auto_open_lives.append(live)
        if not self.auto_open_timer.isActive():
            self.open_next_auto_live()
            if self.pending_auto_open_lives:
                self.auto_open_timer.start()

    def auto_live_context(self, live: dict[str, Any]) -> dict[str, str]:
        broadcaster_id = str(live.get("broadcaster_id") or "").strip()
        broadcaster_name = str(live.get("broadcaster_name") or live.get("provider_name") or "").strip()
        title = str(live.get("title") or live.get("program_title") or live.get("text") or "").strip()
        origin_parts: list[str] = []
        if broadcaster_id:
            try:
                with tracker.connect() as conn:
                    special_rows = conn.execute(
                        """
                        SELECT u.user_id, COALESCE(NULLIF(u.label, ''), u.user_id) AS label
                        FROM special_user_broadcasters b
                        JOIN special_users u ON u.user_id = b.user_id
                        WHERE b.enabled = 1
                          AND u.enabled = 1
                          AND b.broadcaster_id = ?
                        ORDER BY u.label, u.user_id
                        """,
                        (broadcaster_id,),
                    ).fetchall()
                    monitored = conn.execute(
                        """
                        SELECT broadcaster_name
                        FROM monitored_broadcasters
                        WHERE broadcaster_id = ? AND enabled = 1
                        LIMIT 1
                        """,
                        (broadcaster_id,),
                    ).fetchone()
                if special_rows:
                    names = ", ".join(f"{row['label']}({row['user_id']})" for row in special_rows)
                    origin_parts.append(f"応援アカウント紐づき: {names}")
                if monitored:
                    origin_parts.append("監視対象")
            except Exception:
                append_app_log(traceback.format_exc(), "DEBUG")
        if not broadcaster_name and broadcaster_id:
            broadcaster_name = broadcaster_id
        return {
            "title": title,
            "broadcaster_id": broadcaster_id,
            "broadcaster_name": broadcaster_name,
            "origin_text": " / ".join(origin_parts),
        }

    def open_next_auto_live(self) -> None:
        if not self.pending_auto_open_lives:
            self.auto_open_timer.stop()
            return
        live = self.pending_auto_open_lives.pop(0)
        lv = str(live.get("lv") or "").strip()
        if not lv:
            return
        broadcaster_id = str(live.get("broadcaster_id") or "").strip()
        opened = self.comment_monitor_tab.open_broadcast_tab(
            lv,
            activate=False,
            silent_ended=True,
            context={
                "title": live.get("title") or live.get("text") or "",
                "broadcaster_name": live.get("broadcaster_name") or "",
                "broadcaster_id": broadcaster_id,
                "origin_text": live.get("origin_text") or "",
            },
        )
        if opened:
            self.statusBar().showMessage(f"自動コメント監視追加: {lv} / {broadcaster_id}")

    def on_startup_live_scan_finished(self, count: int) -> None:
        if self.pending_auto_open_lives:
            self.auto_open_timer.start()
        self.statusBar().showMessage(f"起動時配信者ページ確認完了: 放送中 {count}件")

    def on_startup_live_scan_failed(self, detail: str) -> None:
        last_line = next((line for line in reversed(detail.splitlines()) if line.strip()), "確認失敗")
        self.statusBar().showMessage(f"起動時配信者ページ確認失敗: {last_line}")

    def open_linked_broadcast_tabs(self, broadcasts: list[dict[str, Any]]) -> None:
        with tracker.connect() as conn:
            linked_broadcaster_ids = tracker.list_enabled_linked_broadcaster_ids(conn)
            monitored_broadcaster_ids = set(tracker.enabled_monitored_broadcaster_map(conn).keys())
            disabled_broadcaster_ids = tracker.disabled_monitored_broadcaster_ids(conn)
        target_broadcaster_ids = linked_broadcaster_ids | monitored_broadcaster_ids
        if not target_broadcaster_ids:
            return
        queued = 0
        for row in broadcasts:
            lv = str(row.get("lv") or "").strip()
            broadcaster_id = str(row.get("broadcaster_id") or "").strip()
            if not lv or not broadcaster_id or broadcaster_id not in target_broadcaster_ids:
                continue
            live = dict(row)
            if broadcaster_id in linked_broadcaster_ids:
                live["auto_open_source"] = "special_linked"
            elif broadcaster_id in monitored_broadcaster_ids:
                if broadcaster_id in disabled_broadcaster_ids:
                    continue
                live["auto_open_source"] = "monitored_broadcaster"
            else:
                continue
            self.enqueue_auto_live(live)
            queued += 1
        if queued:
            self.statusBar().showMessage(f"監視対象配信者の放送をコメント監視へ予約: {queued}件")

    def reload_special_users(self) -> None:
        self.special_users_tab.reload()
        self.broadcaster_monitor_tab.reload()
        self.inspection_tab.reload()

    def reload_broadcaster_monitors(self) -> None:
        self.broadcaster_monitor_tab.reload()
        self.inspection_tab.reload()

    def closeEvent(self, event) -> None:
        self.save_ui_state()
        try:
            self.comment_monitor_tab.close_all_streams()
        except Exception:
            append_app_log(traceback.format_exc(), "DEBUG")
        self.terminate_launcher_cmd()
        event.accept()

    def terminate_launcher_cmd(self) -> None:
        try:
            launcher_pid = str(os.environ.get("NICONICO_WATCH_APP_CMD_PID") or "").strip()
            if launcher_pid.isdigit():
                subprocess.run(["taskkill", "/PID", launcher_pid, "/T", "/F"], capture_output=True, timeout=3)
                return
            script = rf"""
$pidValue = {os.getpid()}
while ($pidValue) {{
  $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$pidValue" -ErrorAction SilentlyContinue
  if (-not $proc) {{ break }}
  $parentId = [int]$proc.ParentProcessId
  if (-not $parentId) {{ break }}
  $parent = Get-CimInstance Win32_Process -Filter "ProcessId=$parentId" -ErrorAction SilentlyContinue
  if (-not $parent) {{ break }}
  if ($parent.Name -eq 'cmd.exe' -and $parent.CommandLine -like '*niconico-watch-app*start_gui.cmd*') {{
    Write-Output $parent.ProcessId
    break
  }}
  $pidValue = $parentId
}}
"""
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command", script],
                capture_output=True,
                text=True,
                timeout=3,
            )
            launcher_pid = next((line.strip() for line in result.stdout.splitlines() if line.strip().isdigit()), "")
            if not launcher_pid:
                return
            subprocess.Popen(
                ["taskkill", "/PID", launcher_pid, "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass

    def restore_ui_state(self) -> None:
        try:
            if not UI_STATE_PATH.exists():
                return
            data = json.loads(UI_STATE_PATH.read_text(encoding="utf-8"))
            window = data.get("main_window", {})
            width = int(window.get("width") or 0)
            height = int(window.get("height") or 0)
            x = window.get("x")
            y = window.get("y")
            if width >= 800 and height >= 500:
                self.resize(width, height)
            if x is not None and y is not None:
                self.move(int(x), int(y))
            inspection_state = data.get("inspection_tab")
            inspection_tab = getattr(self, "inspection_tab", None)
            if inspection_tab is not None:
                inspection_tab.restore_ui_state(inspection_state)
            tracker_state = data.get("tracker_tab")
            tracker_tab = getattr(self, "tracker_tab", None)
            if tracker_tab is not None:
                tracker_tab.restore_ui_state(tracker_state)
        except Exception:
            append_app_log(traceback.format_exc(), "DEBUG")

    def save_ui_state(self) -> None:
        try:
            UI_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            geometry = self.normalGeometry()
            if geometry.width() <= 0 or geometry.height() <= 0:
                geometry = self.geometry()
            data = {
                "main_window": {
                    "x": geometry.x(),
                    "y": geometry.y(),
                    "width": geometry.width(),
                    "height": geometry.height(),
                },
                "tracker_tab": self.tracker_tab.ui_state(),
                "inspection_tab": self.inspection_tab.ui_state(),
            }
            UI_STATE_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            append_app_log(traceback.format_exc(), "DEBUG")


def main() -> int:
    os.environ[APP_ROLE_ENV] = "monitor"
    try:
        killed = tracker.cleanup_selenium_processes()
        if killed:
            print(f"[startup] cleaned selenium processes: {killed}")
    except Exception:
        print(traceback.format_exc())
    app = QApplication(sys.argv)
    app.setApplicationName("niconico-watch-app")
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
