from __future__ import annotations

import ctypes
import faulthandler
import os
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import QThreadPool
from PyQt6.QtWidgets import (
    QApplication,
    QLabel,
    QMainWindow,
    QStatusBar,
    QTabWidget,
)

import tracker
from gui_app import (
    APP_ROLE_ENV,
    TimeshiftTagEditorTab,
    TimeshiftLocalFilesTab,
    TimeshiftTab,
    append_app_log,
)
from timeshift_handoff import (
    TimeshiftHandoffServer,
    normalize_local_files,
    normalize_urls,
    send_local_files,
    send_tag_edit_url,
    send_urls,
)


_SINGLE_INSTANCE_HANDLE: int | None = None
_CRASH_LOG_HANDLE = None


def install_crash_logging() -> None:
    """Persist Python and native crashes from the standalone timeshift process."""
    global _CRASH_LOG_HANDLE
    crash_path = tracker.DATA_DIR / "timeshift_crash.log"
    crash_path.parent.mkdir(parents=True, exist_ok=True)
    _CRASH_LOG_HANDLE = crash_path.open("a", encoding="utf-8", buffering=1)
    faulthandler.enable(_CRASH_LOG_HANDLE, all_threads=True)

    def write_exception(kind: str, exc_type, exc_value, exc_traceback) -> None:
        _CRASH_LOG_HANDLE.write(
            f"\n[{datetime.now().isoformat(timespec='seconds')}] {kind} pid={os.getpid()}\n"
        )
        traceback.print_exception(exc_type, exc_value, exc_traceback, file=_CRASH_LOG_HANDLE)
        _CRASH_LOG_HANDLE.flush()

    sys.excepthook = lambda exc_type, exc_value, exc_traceback: write_exception(
        "UNHANDLED_EXCEPTION", exc_type, exc_value, exc_traceback
    )
    threading.excepthook = lambda args: write_exception(
        f"THREAD_EXCEPTION thread={args.thread.name}",
        args.exc_type,
        args.exc_value,
        args.exc_traceback,
    )


def acquire_single_instance() -> bool:
    """Keep at most one standalone timeshift process in this Windows session."""
    global _SINGLE_INSTANCE_HANDLE
    if os.name != "nt":
        return True
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.GetLastError.restype = ctypes.c_ulong
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel32.CreateMutexW(
        None,
        False,
        "Local\\NiconicoWatchAppTimeshiftStandaloneV1",
    )
    if not handle:
        raise ctypes.WinError()
    if int(kernel32.GetLastError()) == 183:  # ERROR_ALREADY_EXISTS
        kernel32.CloseHandle(handle)
        return False
    _SINGLE_INSTANCE_HANDLE = int(handle)
    return True


class TimeshiftMainWindow(QMainWindow):
    """Standalone UI for timeshift acquisition and archive generation."""

    def __init__(
        self,
        initial_urls: list[str] | None = None,
        initial_files: list[str] | None = None,
        initial_tag_url: str = "",
    ) -> None:
        super().__init__()
        role = str(os.environ.get(APP_ROLE_ENV) or "").strip().lower()
        if role != "timeshift":
            raise RuntimeError("タイムシフト専用プロセスとして起動されていません")

        self.setWindowTitle("Niconico タイムシフト / ローカル処理")
        self.resize(1100, 760)

        self.tabs = QTabWidget()
        self.acquire_tab = TimeshiftTab()
        self.local_files_tab = TimeshiftLocalFilesTab()
        self.tag_editor_tab = TimeshiftTagEditorTab()
        self.tabs.addTab(self.acquire_tab, "URLから取得")
        self.tabs.addTab(self.local_files_tab, "ローカル処理")
        self.tabs.addTab(self.tag_editor_tab, "タグ修正")
        self.setCentralWidget(self.tabs)

        status = QStatusBar()
        status.addPermanentWidget(QLabel(f"共有DB: {tracker.DB_PATH}"))
        self.setStatusBar(status)
        self.statusBar().showMessage("監視アプリとは別プロセスで動作中")
        append_app_log(
            f"タイムシフト専用GUI起動 / pid={os.getpid()} / shared_db={tracker.DB_PATH}",
            "INFO",
        )

        self.handoff_server = TimeshiftHandoffServer(self)
        self.handoff_server.urls_received.connect(self.receive_urls)
        self.handoff_server.local_files_received.connect(self.receive_local_files)
        self.handoff_server.tag_edit_received.connect(self.receive_tag_edit)
        if not self.handoff_server.start():
            raise RuntimeError("タイムシフトGUI受信サーバーを起動できません")
        if initial_urls:
            self.receive_urls(initial_urls)
        if initial_files:
            self.receive_local_files(initial_files)
        if initial_tag_url:
            self.receive_tag_edit(initial_tag_url)

    def receive_tag_edit(self, url: str) -> None:
        self.tag_editor_tab.lv_edit.setText(str(url or ""))
        self.tag_editor_tab.load_values()
        self.tabs.setCurrentWidget(self.tag_editor_tab)
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def receive_urls(self, urls: list[str]) -> None:
        normalized = normalize_urls(urls)
        if not normalized:
            return
        added = self.acquire_tab.add_input_urls(normalized)
        self.tabs.setCurrentWidget(self.acquire_tab)
        if self.isMinimized():
            self.showNormal()
        else:
            self.show()
        self.raise_()
        self.activateWindow()
        self.statusBar().showMessage(
            f"監視アプリからURL受信: 新規{len(added)}件 / 受信{len(normalized)}件"
        )
        append_app_log(
            f"タイムシフトGUI URL受信: 新規{len(added)}件 / 受信{len(normalized)}件",
            "INFO",
        )

    def receive_local_files(self, paths: list[str]) -> None:
        normalized = normalize_local_files(paths)
        if not normalized:
            return
        before = {
            str(path.resolve()).casefold()
            for path in self.local_files_tab.paths
            if path.exists()
        }
        self.local_files_tab.add_paths([Path(path) for path in normalized])
        added = [
            path
            for path in self.local_files_tab.paths
            if str(path.resolve()).casefold() not in before
        ]
        self.tabs.setCurrentWidget(self.local_files_tab)
        if self.isMinimized():
            self.showNormal()
        else:
            self.show()
        self.raise_()
        self.activateWindow()
        self.statusBar().showMessage(
            f"監視アプリからローカル動画受信: 新規{len(added)}件 / 受信{len(normalized)}件"
        )
        append_app_log(
            f"ローカル処理GUI 動画受信: 新規{len(added)}件 / 受信{len(normalized)}件",
            "INFO",
        )

    def closeEvent(self, event) -> None:
        self.handoff_server.stop()
        append_app_log(f"タイムシフト専用GUI終了 / pid={os.getpid()}", "INFO")
        if os.name == "nt":
            script = rf"""
$rootPid = {os.getpid()}
$selfPid = $PID
$pending = @($rootPid)
$killPids = @()
while ($pending.Count -gt 0) {{
    $parentPid = $pending[0]
    if ($pending.Count -eq 1) {{ $pending = @() }} else {{ $pending = $pending[1..($pending.Count - 1)] }}
    $found = @(Get-CimInstance Win32_Process -Filter "ParentProcessId=$parentPid" -ErrorAction SilentlyContinue)
    foreach ($child in $found) {{
        if ([int]$child.ProcessId -ne $selfPid) {{
            $pending += [int]$child.ProcessId
            if ($child.Name -match '^(pythonw?|ffmpeg|ffprobe|cmd|conhost)\.exe$') {{
                $killPids += [int]$child.ProcessId
            }}
        }}
    }}
}}
foreach ($childPid in ($killPids | Sort-Object -Descending -Unique)) {{
    Stop-Process -Id $childPid -Force -ErrorAction SilentlyContinue
}}
"""
            subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True,
                timeout=5,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        event.accept()
        os._exit(0)


def main(
    initial_urls: list[str] | None = None,
    initial_files: list[str] | None = None,
    initial_tag_url: str = "",
) -> int:
    install_crash_logging()
    initial_urls = normalize_urls(initial_urls or [])
    initial_files = normalize_local_files(initial_files or [])
    role = str(os.environ.get(APP_ROLE_ENV) or "").strip().lower()
    if role and role != "timeshift":
        raise RuntimeError(
            f"別ロールのプロセスでは起動できません: {APP_ROLE_ENV}={role}"
        )
    os.environ[APP_ROLE_ENV] = "timeshift"

    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("niconico-watch-app-timeshift")
    app.setApplicationDisplayName("Niconico タイムシフト / ローカル処理")
    acquired = acquire_single_instance()
    if not acquired:
        if initial_urls or initial_files or initial_tag_url:
            for _attempt in range(50):
                urls_sent = not initial_urls or send_urls(initial_urls, timeout_ms=200)
                files_sent = not initial_files or send_local_files(initial_files, timeout_ms=200)
                tag_sent = not initial_tag_url or send_tag_edit_url(initial_tag_url, timeout_ms=200)
                if urls_sent and files_sent and tag_sent:
                    return 0
                time.sleep(0.1)
                if acquire_single_instance():
                    acquired = True
                    break
        if not acquired:
            return 2

    # URL取得とローカルD&Dを同時実行させず、この専用プロセス内でも直列にする。
    QThreadPool.globalInstance().setMaxThreadCount(1)

    window = TimeshiftMainWindow(
        initial_urls=initial_urls,
        initial_files=initial_files,
        initial_tag_url=initial_tag_url,
    )
    window.show()
    return int(app.exec())


if __name__ == "__main__":
    raise SystemExit(main())
