from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import ctypes
import ftplib
import hmac
import importlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from ctypes import wintypes
from datetime import datetime, timedelta
from html import unescape
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote, urlparse

import requests

try:
    from .codex_exec_runner import CodexExecConfig
    from .console_progress import ConsoleProgress, hms_seconds, parse_ffmpeg_time_seconds
    from .niconico_ids import extract_nicolive_id, extract_user_id
except ImportError:
    from codex_exec_runner import CodexExecConfig
    from console_progress import ConsoleProgress, hms_seconds, parse_ffmpeg_time_seconds
    from niconico_ids import extract_nicolive_id, extract_user_id
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait


ROOT = Path(__file__).resolve().parents[1]
NICONICO_ROOT = ROOT.parent
DATA_DIR = ROOT / "data"
TMP_DIR = ROOT / "tmp"
HIT_DIR = ROOT / "storage" / "hits"
DB_PATH = DATA_DIR / "tracker.db"
CONFIG_PATH = ROOT / "config.json"
SELENIUM_PROFILE_DIR = TMP_DIR / "selenium_profiles"
TRACKER_BROADCAST_TTL_SECONDS = 3600
DEFAULT_SLNICO_EXE = NICONICO_ROOT / "SlNicoLiveRec1062" / "SlNicoLiveRec.exe"
DEFAULT_SLNICO_CONFIG = NICONICO_ROOT / "SlNicoLiveRec1062" / "SlNicoLiveRec_config.json"
DEFAULT_SLNICO_RECORDING_ROOT = NICONICO_ROOT / "SlNicoLiveRec1062" / "rec_file"
DEFAULT_TARGET_ROOT = NICONICO_ROOT / "target"
DEFAULT_RECORDING_ACCOUNT_ID = "51610839"
DEFAULT_CHARACTER1_NAME = "ニニちゃん"
DEFAULT_CHARACTER1_IMAGE_URL = "https://raw.githubusercontent.com/youzoom64/niconico-character-icons/main/assets/characters/nini.png"
DEFAULT_CHARACTER1_FULLBODY_IMAGE_URL = "https://raw.githubusercontent.com/youzoom64/niconico-character-icons/main/assets/characters/nini_fullbody.png"
DEFAULT_CHARACTER2_NAME = "ココちゃん"
DEFAULT_AI_REACTION_PROMPT = "次のニコニコ生放送コメントへの短い反応を作ってください。"
DEFAULT_AI_REACTION_SKIP_PROMPT = "返事しない方がよい場合は、replyにSKIPを返してください。"
DEFAULT_CHARACTER2_IMAGE_URL = "https://raw.githubusercontent.com/youzoom64/niconico-character-icons/main/assets/characters/koko.png"
DEFAULT_CHARACTER2_FULLBODY_IMAGE_URL = "https://raw.githubusercontent.com/youzoom64/niconico-character-icons/main/assets/characters/koko_fullbody.png"
SUNO_MODELS_DOC_URL = "https://docs.sunoapi.org/suno-api/generate-music"
DEFAULT_SUNO_MODELS = ["V5_5", "V5", "V4_5PLUS", "V4_5ALL", "V4_5", "V4"]
_TRACKER_DRIVER: webdriver.Chrome | None = None
_TRACKER_DRIVER_HEADLESS: bool | None = None
_TRACKER_DRIVER_LOCK = threading.RLock()
LOG_LEVELS = {"TRACE": 5, "DEBUG": 10, "INFO": 20, "WARN": 30, "ERROR": 40}
_LOG_SINKS: list[Any] = []
_POSTPROCESS_LOG_ONCE_KEYS: set[str] = set()
_SEGMENT_TRANSCRIPTION_LOCKS: dict[str, threading.Lock] = {}
_SEGMENT_TRANSCRIPTION_LOCKS_GUARD = threading.Lock()
_TIMESHIFT_RECORDER_LOCK = threading.Lock()
_FINALIZE_DISPATCHER_PROCESS: subprocess.Popen | None = None
_FINALIZE_DISPATCHER_PROCESS_LOCK = threading.Lock()
COMMENT_OFFSET_STATE_PATTERN = re.compile(
    r'(<script\s+id="nico-comment-offset-state"\s+type="application/json">).*?(</script>)',
    flags=re.DOTALL,
)


def _windows_no_console_creationflags(creationflags: int = 0) -> int:
    """Force child console programs to stay invisible in GUI app processes."""
    flags = int(creationflags or 0)
    if os.name != "nt":
        return flags
    create_no_window = int(getattr(subprocess, "CREATE_NO_WINDOW", 0) or 0)
    create_new_console = int(getattr(subprocess, "CREATE_NEW_CONSOLE", 0) or 0)
    detached_process = int(getattr(subprocess, "DETACHED_PROCESS", 0) or 0)
    flags &= ~create_new_console
    if create_no_window and not (flags & detached_process):
        flags |= create_no_window
    return flags


def install_no_console_subprocess_policy() -> None:
    """Apply one process-wide policy so every library child uses no console."""
    if os.name != "nt" or str(os.environ.get("NICONICO_WATCH_APP_ROLE") or "").strip().lower() not in {
        "monitor",
        "timeshift",
    }:
        return
    current_popen = subprocess.Popen
    if bool(getattr(current_popen, "_niconico_no_console_policy", False)):
        return

    class NoConsolePopen(current_popen):
        _niconico_no_console_policy = True

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            positional = list(args)
            # Popen accepts creationflags as positional argument 14. Handle it
            # as well as the normal keyword form so the policy has no bypass.
            if len(positional) > 13:
                positional[13] = _windows_no_console_creationflags(positional[13])
            else:
                kwargs["creationflags"] = _windows_no_console_creationflags(kwargs.get("creationflags", 0))
            super().__init__(*positional, **kwargs)

    subprocess.Popen = NoConsolePopen


install_no_console_subprocess_policy()


def is_supported_broadcast_history_provider_id(value: str | int | None) -> bool:
    text = str(value or "").strip()
    return text.isdigit() or bool(re.fullmatch(r"ch\d+", text, flags=re.IGNORECASE))


SCHEMA = """
CREATE TABLE IF NOT EXISTS broadcasts (
    lv TEXT PRIMARY KEY,
    title TEXT,
    broadcaster_id TEXT,
    broadcaster_name TEXT,
    watch_url TEXT,
    elapsed_minutes REAL,
    watch_count INTEGER,
    comment_count INTEGER,
    status TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    raw_json TEXT
);

CREATE TABLE IF NOT EXISTS expired_broadcasts (
    lv TEXT PRIMARY KEY,
    first_seen_at TEXT NOT NULL,
    expired_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS checks (
    lv TEXT PRIMARY KEY,
    checked_at TEXT NOT NULL,
    result TEXT NOT NULL,
    fetched_count INTEGER NOT NULL DEFAULT 0,
    matched_count INTEGER NOT NULL DEFAULT 0,
    saved_dir TEXT,
    deleted_temp INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    raw_json TEXT
);

CREATE TABLE IF NOT EXISTS matches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lv TEXT NOT NULL,
    comment_no INTEGER,
    user_id TEXT,
    text TEXT,
    match_type TEXT NOT NULL,
    matched_value TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS special_users (
    user_id TEXT PRIMARY KEY,
    label TEXT,
    note TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    analysis_model TEXT,
    analysis_api_key TEXT,
    analysis_engine TEXT NOT NULL DEFAULT 'openai',
    analysis_use_codex INTEGER NOT NULL DEFAULT 0,
    analysis_effort TEXT NOT NULL DEFAULT 'medium',
    analysis_session_id TEXT,
    reaction_model TEXT,
    reaction_api_key TEXT,
    reaction_engine TEXT NOT NULL DEFAULT 'openai',
    reaction_use_codex INTEGER NOT NULL DEFAULT 0,
    reaction_effort TEXT NOT NULL DEFAULT 'medium',
    reaction_session_id TEXT,
    reaction_skip_prompt TEXT,
    reaction_max_chars INTEGER NOT NULL DEFAULT 100,
    reaction_split_delay REAL NOT NULL DEFAULT 1.0,
    reaction_delay_seconds REAL NOT NULL DEFAULT 0.0,
    max_reactions INTEGER NOT NULL DEFAULT 1,
    basic_reaction_enabled INTEGER NOT NULL DEFAULT 0,
    basic_reaction_type TEXT NOT NULL DEFAULT 'fixed',
    basic_reaction_messages TEXT,
    basic_reaction_prompt TEXT,
    default_action_type TEXT NOT NULL DEFAULT 'none',
    default_action_payload TEXT,
    post_server_url TEXT,
    post_server_api_key TEXT,
    html_upload_enabled INTEGER NOT NULL DEFAULT 0,
    html_base_url TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS special_user_broadcasters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    broadcaster_id TEXT NOT NULL,
    broadcaster_name TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    basic_reaction_enabled INTEGER NOT NULL DEFAULT 0,
    basic_reaction_type TEXT NOT NULL DEFAULT 'fixed',
    basic_reaction_messages TEXT,
    basic_reaction_prompt TEXT,
    reaction_use_codex INTEGER NOT NULL DEFAULT 0,
    max_reactions INTEGER NOT NULL DEFAULT 1,
    reaction_delay_seconds REAL NOT NULL DEFAULT 0.0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(user_id, broadcaster_id),
    FOREIGN KEY(user_id) REFERENCES special_users(user_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS special_user_triggers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    keyword TEXT NOT NULL,
    action_type TEXT NOT NULL DEFAULT 'none',
    action_payload TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(user_id) REFERENCES special_users(user_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS broadcaster_triggers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    broadcaster_id TEXT NOT NULL,
    trigger_name TEXT NOT NULL DEFAULT '',
    keyword TEXT NOT NULL,
    action_type TEXT NOT NULL DEFAULT 'fixed',
    action_payload TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(user_id) REFERENCES special_users(user_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS special_user_broadcast_hits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lv TEXT NOT NULL,
    user_id TEXT NOT NULL,
    broadcaster_id TEXT NOT NULL,
    broadcaster_name TEXT,
    first_comment_no INTEGER,
    first_comment_text TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    comment_count INTEGER NOT NULL DEFAULT 1,
    html_upload_requested INTEGER NOT NULL DEFAULT 0,
    html_uploaded_at TEXT,
    UNIQUE(lv, user_id, broadcaster_id)
);

CREATE TABLE IF NOT EXISTS broadcaster_monitor_special_user_hits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lv TEXT NOT NULL,
    user_id TEXT NOT NULL,
    broadcaster_id TEXT NOT NULL,
    broadcaster_name TEXT,
    first_comment_no INTEGER,
    first_comment_text TEXT,
    first_comment_seconds REAL,
    detected_at TEXT NOT NULL,
    comment_count INTEGER NOT NULL DEFAULT 1,
    html_upload_requested INTEGER NOT NULL DEFAULT 0,
    html_uploaded_at TEXT,
    UNIQUE(lv, user_id, broadcaster_id)
);

CREATE TABLE IF NOT EXISTS html_upload_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lv TEXT NOT NULL,
    user_id TEXT,
    broadcaster_id TEXT,
    source_path TEXT NOT NULL,
    destination TEXT,
    status TEXT NOT NULL,
    error TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS niconico_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_name TEXT NOT NULL,
    user_session TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS monitored_broadcasters (
    broadcaster_id TEXT PRIMARY KEY,
    broadcaster_name TEXT,
    source_lv TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    html_generation_enabled INTEGER NOT NULL DEFAULT 1,
    custom_settings_enabled INTEGER NOT NULL DEFAULT 0,
    thumbnail_10sec_enabled INTEGER NOT NULL DEFAULT 1,
    audio_timeline_enabled INTEGER NOT NULL DEFAULT 1,
    ranking_enabled INTEGER NOT NULL DEFAULT 1,
    ai_conversation_enabled INTEGER NOT NULL DEFAULT 1,
    ai_conversation_engine TEXT NOT NULL DEFAULT 'codex_exec',
    summary_enabled INTEGER NOT NULL DEFAULT 1,
    summary_engine TEXT NOT NULL DEFAULT 'codex_exec',
    special_user_summary_engine TEXT NOT NULL DEFAULT 'codex_exec',
    music_enabled INTEGER NOT NULL DEFAULT 0,
    abstract_image_enabled INTEGER NOT NULL DEFAULT 1,
    emotion_score_enabled INTEGER NOT NULL DEFAULT 1,
    word_extract_enabled INTEGER NOT NULL DEFAULT 1,
    timeline_enabled INTEGER NOT NULL DEFAULT 1,
    ai_analysis_model TEXT,
    ai_analysis_api_key TEXT,
    ai_analysis_use_codex INTEGER NOT NULL DEFAULT 0,
    ai_reaction_model TEXT,
    ai_reaction_api_key TEXT,
    ai_reaction_use_codex INTEGER NOT NULL DEFAULT 0,
    summary_use_codex INTEGER NOT NULL DEFAULT 0,
    ai_conversation_use_codex INTEGER NOT NULL DEFAULT 0,
    character1_name TEXT,
    character1_image_url TEXT,
    character1_image_flip INTEGER NOT NULL DEFAULT 0,
    character2_name TEXT,
    character2_image_url TEXT,
    character2_image_flip INTEGER NOT NULL DEFAULT 0,
    post_server_url TEXT,
    post_server_api_key TEXT,
    faster_whisper_model TEXT,
    whisperx_model TEXT,
    whisperx_enabled INTEGER NOT NULL DEFAULT 0,
    transcription_initial_prompt TEXT NOT NULL DEFAULT '',
    transcription_hotwords_enabled INTEGER NOT NULL DEFAULT 1,
    speaker_diarization_enabled INTEGER NOT NULL DEFAULT 0,
    diarization_min_speakers INTEGER NOT NULL DEFAULT 1,
    diarization_max_speakers INTEGER NOT NULL DEFAULT 4,
    html_upload_enabled INTEGER NOT NULL DEFAULT 0,
    html_base_url TEXT,
    archive_tags TEXT NOT NULL DEFAULT '',
    summary_prompt TEXT,
    image_prompt TEXT,
    music_prompt TEXT,
    intro_conversation_prompt TEXT,
    outro_conversation_prompt TEXT,
    recording_output_dir TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recording_jobs (
    lv TEXT PRIMARY KEY,
    broadcaster_id TEXT,
    broadcaster_name TEXT,
    watch_url TEXT,
    recorder TEXT NOT NULL,
    pid INTEGER,
    status TEXT NOT NULL,
    target_dir TEXT,
    restart_count INTEGER NOT NULL DEFAULT 0,
    last_exit_at TEXT,
    last_process_check_at TEXT,
    process_check_count INTEGER NOT NULL DEFAULT 0,
    started_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    error TEXT
);

CREATE TABLE IF NOT EXISTS recording_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lv TEXT NOT NULL,
    broadcaster_id TEXT,
    broadcaster_name TEXT,
    watch_url TEXT,
    recorder TEXT NOT NULL,
    pid INTEGER,
    event_type TEXT NOT NULL,
    event_at TEXT NOT NULL,
    started_at TEXT,
    ended_at TEXT,
    duration_us INTEGER,
    exit_code INTEGER,
    target_dir TEXT,
    payload_json TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS archive_comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lv TEXT NOT NULL,
    no INTEGER,
    comment_id TEXT,
    user_id TEXT,
    raw_user_id TEXT,
    hashed_user_id TEXT,
    user_name TEXT,
    text TEXT NOT NULL,
    date INTEGER,
    posted_at TEXT,
    received_at TEXT,
    vpos INTEGER,
    broadcast_seconds REAL,
    timeline_block INTEGER,
    premium INTEGER NOT NULL DEFAULT 0,
    anonymity INTEGER NOT NULL DEFAULT 0,
    mail TEXT,
    source TEXT NOT NULL DEFAULT 'ndgr',
    raw_json TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(lv, no, user_id, text)
);

CREATE TABLE IF NOT EXISTS archive_comment_ranking (
    lv TEXT NOT NULL,
    user_id TEXT NOT NULL,
    user_name TEXT,
    comment_count INTEGER NOT NULL DEFAULT 0,
    first_comment TEXT,
    first_comment_time REAL,
    last_comment TEXT,
    last_comment_time REAL,
    premium INTEGER NOT NULL DEFAULT 0,
    anonymity INTEGER NOT NULL DEFAULT 0,
    rank INTEGER,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(lv, user_id)
);

CREATE TABLE IF NOT EXISTS broadcast_archive_meta (
    lv TEXT PRIMARY KEY,
    watch_url TEXT,
    title TEXT,
    broadcaster_id TEXT,
    broadcaster_name TEXT,
    begin_time INTEGER,
    open_time INTEGER,
    start_time INTEGER,
    end_time INTEGER,
    server_time INTEGER,
    time_diff_seconds INTEGER,
    fetched_at TEXT NOT NULL,
    html_path TEXT,
    comments_fetch_completed INTEGER NOT NULL DEFAULT 0,
    comments_fetch_error TEXT,
    timeshift_video_download_completed INTEGER NOT NULL DEFAULT 0,
    timeshift_video_download_completed_at TEXT,
    timeshift_comments_download_completed INTEGER NOT NULL DEFAULT 0,
    timeshift_comments_download_completed_at TEXT,
    timeshift_download_completed INTEGER NOT NULL DEFAULT 0,
    timeshift_download_completed_at TEXT,
    archive_upload_completed INTEGER NOT NULL DEFAULT 0,
    archive_upload_completed_at TEXT,
    archive_upload_target_id TEXT,
    archive_upload_remote_directory TEXT,
    raw_json TEXT
);

CREATE TABLE IF NOT EXISTS archive_broadcast_data (
    lv TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recording_segments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lv TEXT NOT NULL,
    broadcaster_id TEXT,
    source_path TEXT NOT NULL,
    target_path TEXT,
    file_type TEXT,
    size_bytes INTEGER,
    mtime REAL,
    segment_index INTEGER,
    status TEXT NOT NULL DEFAULT 'found',
    started_at TEXT,
    ended_at TEXT,
    duration_seconds REAL,
    timeline_start_seconds REAL,
    audio_wav_path TEXT,
    audio_mp3_path TEXT,
    transcript_status TEXT NOT NULL DEFAULT 'pending',
    transcript_started_at TEXT,
    transcript_finished_at TEXT,
    transcript_error TEXT,
    transcript_model TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(lv, source_path)
);

CREATE TABLE IF NOT EXISTS recording_gaps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lv TEXT NOT NULL,
    gap_start TEXT NOT NULL,
    gap_end TEXT NOT NULL,
    duration_us INTEGER NOT NULL,
    fill_type TEXT NOT NULL DEFAULT 'black_silent_video',
    status TEXT NOT NULL DEFAULT 'pending',
    generated_video_path TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(lv, gap_start, gap_end)
);

CREATE TABLE IF NOT EXISTS archive_comment_time_adjustments (
    lv TEXT PRIMARY KEY,
    offset_seconds INTEGER NOT NULL DEFAULT 0,
    confirmed INTEGER NOT NULL DEFAULT 0,
    confirm_token TEXT NOT NULL,
    confirmed_at TEXT,
    html_paths_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS postprocess_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lv TEXT NOT NULL,
    stage TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    started_at TEXT,
    finished_at TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(lv, stage)
);

CREATE TABLE IF NOT EXISTS postprocess_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lv TEXT,
    stage TEXT,
    level TEXT NOT NULL,
    message TEXT NOT NULL,
    payload_json TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS finalize_queue (
    lv TEXT PRIMARY KEY,
    broadcaster_id TEXT,
    target_dir TEXT,
    source_kind TEXT NOT NULL DEFAULT 'live',
    timeline_mode TEXT NOT NULL DEFAULT 'live',
    input_dir TEXT,
    segment_paths_json TEXT,
    transcribe INTEGER NOT NULL DEFAULT 1,
    whisper_model TEXT NOT NULL DEFAULT 'large-v3',
    status TEXT NOT NULL DEFAULT 'preparing',
    worker_pid INTEGER,
    attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    result_json TEXT,
    created_at TEXT NOT NULL,
    queued_at TEXT,
    started_at TEXT,
    finished_at TEXT,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_finalize_queue_status_created
ON finalize_queue(status, created_at);

CREATE TABLE IF NOT EXISTS finalize_dispatcher_state (
    singleton_id INTEGER PRIMARY KEY CHECK(singleton_id = 1),
    pid INTEGER,
    started_at TEXT,
    heartbeat_at TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS archive_transcript_segments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lv TEXT NOT NULL,
    segment_index INTEGER NOT NULL,
    start_seconds REAL NOT NULL,
    end_seconds REAL NOT NULL,
    text TEXT NOT NULL,
    confidence REAL,
    speaker TEXT,
    source_audio_path TEXT,
    model TEXT,
    raw_json TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(lv, segment_index, start_seconds, end_seconds, text)
);
"""


@dataclass
class Config:
    recent_url: str
    tracker_fetch_method: str
    poll_seconds: int
    min_elapsed_minutes: float
    target_user_ids: list[str]
    target_keywords: list[str]
    selenium_headless: bool
    max_recent_items: int
    download_timeout_seconds: int
    slnico_live_rec_exe: str
    target_root: str
    recording_account_id: str
    recording_auto_restart: bool
    recording_restart_delay_seconds: float
    recording_max_restarts: int
    recording_segment_seconds: int
    concat_output_scale: str
    concat_output_fps: int
    concat_output_crf: int
    concat_video_encoder: str
    concat_nvenc_preset: str
    filezilla_config_dir: str
    ndgr_python_exe: str
    character1_name: str
    character1_image_url: str
    character1_fullbody_image_url: str
    character2_name: str
    character2_image_url: str
    character2_fullbody_image_url: str
    summary_prompt: str
    summary_chunk_size: int
    summary_chunk_prompt: str
    summary_final_prompt: str
    image_prompt: str
    intro_conversation_prompt: str
    outro_conversation_prompt: str
    character1_personality: str
    character2_personality: str
    conversation_turns: int
    enable_summary_text: bool
    enable_summary_image: bool
    enable_ai_conversation: bool
    enable_ai_music: bool
    enable_timeline_thumbnails: bool
    timeline_thumbnail_width: int
    timeline_thumbnail_height: int
    enable_audio_timeline: bool
    enable_timeline_html: bool
    enable_comment_ranking: bool
    enable_emotion_scores: bool
    enable_word_extract: bool
    suno_api_key: str
    suno_music_model: str
    suno_music_style: str
    suno_music_instrumental: bool
    openai_api_key: str
    google_api_key: str
    imgur_api_key: str
    huggingface_token: str
    image_generation_model: str
    image_generation_quality: str
    codex_exec_enabled: bool
    codex_exec_provider: str
    codex_exec_command: str
    codex_exec_cwd: str
    codex_exec_timeout_seconds: int
    codex_exec_model: str
    codex_exec_effort: str
    codex_exec_extra_args: list[str]
    enable_archive_auto_upload: bool
    archive_upload_target_id: str
    archive_upload_username: str
    archive_upload_password: str
    archive_upload_remote_dir_template: str
    archive_upload_python_exe: str
    archive_upload_cli_path: str
    archive_upload_http_verify: bool
    archive_upload_timeout_seconds: int
    archive_upload_auto_start_credentials_api: bool
    postprocess_console_log_enabled: bool


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def now_micro() -> str:
    return datetime.now().isoformat(timespec="microseconds")


def add_log_sink(sink: Any) -> None:
    if sink not in _LOG_SINKS:
        _LOG_SINKS.append(sink)


def remove_log_sink(sink: Any) -> None:
    if sink in _LOG_SINKS:
        _LOG_SINKS.remove(sink)


def postprocess_log(
    lv: str | None,
    stage: str | None,
    level: str,
    message: str,
    payload: dict[str, Any] | None = None,
    *,
    once_key: str | None = None,
) -> None:
    level = level if level in LOG_LEVELS else "INFO"
    if once_key:
        key = f"{lv or ''}:{stage or ''}:{once_key}"
        if key in _POSTPROCESS_LOG_ONCE_KEYS:
            return
        _POSTPROCESS_LOG_ONCE_KEYS.add(key)
    prefix = f"{lv or '-'}"
    if stage:
        prefix += f" {stage}"
    text = f"{prefix}: {message}"
    if is_postprocess_console_log_enabled():
        print(f"[{datetime.now().strftime('%H:%M:%S')}] [{level}] {text}", flush=True)
    for sink in list(_LOG_SINKS):
        try:
            sink(level, text)
        except Exception:
            pass
    if lv:
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(DB_PATH) as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS postprocess_logs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        lv TEXT,
                        stage TEXT,
                        level TEXT NOT NULL,
                        message TEXT NOT NULL,
                        payload_json TEXT,
                        created_at TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    """
                    INSERT INTO postprocess_logs
                        (lv, stage, level, message, payload_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        lv,
                        stage or "",
                        level,
                        message,
                        json.dumps(payload or {}, ensure_ascii=False, default=str),
                        now_micro(),
                    ),
                )
                conn.commit()
        except Exception:
            pass


def is_postprocess_console_log_enabled() -> bool:
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8")) if CONFIG_PATH.exists() else {}
        return bool(raw.get("postprocess_console_log_enabled", True))
    except Exception:
        return True


def parse_ffmpeg_progress_tail(text: str) -> dict[str, str]:
    progress: dict[str, str] = {}
    if not text:
        return progress
    matches = list(re.finditer(r"(frame|fps|size|time|bitrate|speed)\s*=\s*([^\s]+)", text))
    for match in matches:
        progress[match.group(1)] = match.group(2)
    return progress


def format_progress_message(
    base: str,
    elapsed_seconds: float,
    progress: dict[str, str],
    *,
    progress_total_seconds: float | None = None,
) -> str:
    if progress:
        done_seconds = parse_ffmpeg_time_seconds(progress.get("time"))
        return (
            f"{base} 実行中 elapsed={int(elapsed_seconds)}秒 / "
            f"time={hms_seconds(done_seconds)} / total={hms_seconds(progress_total_seconds)} / "
            f"frame={progress.get('frame', '-')} / size={progress.get('size', '-')} / speed={progress.get('speed', '-')}"
        )
    return f"{base} 実行中 elapsed={int(elapsed_seconds)}秒"


def compact_progress_message(value: Any) -> str:
    text = str(value or "").strip()
    mapping = {
        "モデル読み込み": "load",
        "文字起こし": "transcribe",
        "文字起こし中(WhisperX内部処理)": "transcribe",
        "アラインメント": "align",
        "話者分離": "diarize",
        "保存": "save",
        "完了": "done",
        "model_load": "load",
        "transcribe": "transcribe",
        "align": "align",
        "diarize": "diarize",
        "save": "save",
        "done": "done",
    }
    return mapping.get(text, re.sub(r"[^A-Za-z0-9_.=-]+", "_", text)[:24])


def run_subprocess_with_stage_log(
    cmd: list[str],
    *,
    lv: str | None,
    stage: str,
    label: str,
    timeout: int,
    heartbeat_seconds: float = 30.0,
    env_overrides: dict[str, str] | None = None,
    progress_total_seconds: float | None = None,
    progress_json_path: Path | str | None = None,
) -> subprocess.CompletedProcess[str]:
    postprocess_log(lv, stage, "DEBUG", f"{label} 開始", {"cmd": cmd})
    started = time.monotonic()
    log_dir = TMP_DIR / "subprocess_logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    safe_label = re.sub(r"[^A-Za-z0-9_.-]+", "_", label)[:80] or "subprocess"
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    stdout_path = log_dir / f"{lv or 'no_lv'}_{stage}_{safe_label}_{stamp}.out.log"
    stderr_path = log_dir / f"{lv or 'no_lv'}_{stage}_{safe_label}_{stamp}.err.log"
    stdout_handle = stdout_path.open("w", encoding="utf-8", errors="replace")
    stderr_handle = stderr_path.open("w", encoding="utf-8", errors="replace")
    env = os.environ.copy()
    if env_overrides:
        env.update({key: str(value) for key, value in env_overrides.items() if str(value)})
    process = subprocess.Popen(cmd, stdout=stdout_handle, stderr=stderr_handle, text=True, env=env)
    try:
        inline_progress = progress_total_seconds is not None
        next_heartbeat = started + (1.0 if inline_progress else max(5.0, heartbeat_seconds))
        console_progress = ConsoleProgress(label, total_seconds=progress_total_seconds) if inline_progress else None
        while True:
            code = process.poll()
            now_time = time.monotonic()
            if code is not None:
                break
            if now_time - started > timeout:
                process.kill()
                process.wait(timeout=5)
                raise subprocess.TimeoutExpired(cmd, timeout)
            if now_time >= next_heartbeat:
                stderr_tail = ""
                try:
                    stderr_handle.flush()
                    stderr_tail = stderr_path.read_text(encoding="utf-8", errors="replace")[-4000:]
                except Exception:
                    stderr_tail = ""
                progress = parse_ffmpeg_progress_tail(stderr_tail)
                elapsed_seconds = round(now_time - started, 1)
                progress_payload: dict[str, Any] = {}
                if inline_progress and progress_json_path:
                    try:
                        progress_payload = json.loads(Path(progress_json_path).read_text(encoding="utf-8"))
                    except Exception:
                        progress_payload = {}
                if inline_progress and progress_payload:
                    done_seconds = progress_payload.get("done_seconds")
                    try:
                        done_value = float(done_seconds) if done_seconds is not None else None
                    except Exception:
                        done_value = None
                    if done_value is None and progress_total_seconds and progress_payload.get("percent") is not None:
                        try:
                            done_value = (float(progress_payload["percent"]) / 100.0) * float(progress_total_seconds)
                        except Exception:
                            done_value = None
                    extra_parts = [compact_progress_message(progress_payload.get("message") or progress_payload.get("stage") or "")]
                    if progress_payload.get("segments") is not None:
                        extra_parts.append(f"segments={progress_payload.get('segments')}")
                    console_progress.update(
                        done_value,
                        extra=" ".join(part for part in extra_parts if part),
                        force=True,
                    )
                    next_heartbeat = now_time + 1.0
                elif inline_progress and progress:
                    done_seconds = parse_ffmpeg_time_seconds(progress.get("time"))
                    console_progress.update(
                        done_seconds,
                        speed=progress.get("speed", ""),
                        size=progress.get("size", ""),
                        frame=progress.get("frame", ""),
                        force=True,
                    )
                    next_heartbeat = now_time + 1.0
                elif inline_progress and console_progress:
                    console_progress.update(None, extra="", force=True)
                    next_heartbeat = now_time + 1.0
                else:
                    postprocess_log(
                        lv,
                        stage,
                        "DEBUG",
                        format_progress_message(label, elapsed_seconds, progress, progress_total_seconds=progress_total_seconds),
                        {
                            "elapsed_seconds": elapsed_seconds,
                            "progress": progress,
                            "progress_total_seconds": progress_total_seconds,
                        },
                    )
                    next_heartbeat = now_time + max(5.0, heartbeat_seconds)
            time.sleep(1.0)
    except Exception:
        if process.poll() is None:
            process.kill()
        raise
    finally:
        if progress_total_seconds is not None and "console_progress" in locals() and console_progress:
            console_progress.finish()
        stdout_handle.close()
        stderr_handle.close()
    stdout_tail = stdout_path.read_text(encoding="utf-8", errors="replace")[-4000:] if stdout_path.exists() else ""
    stderr_tail = stderr_path.read_text(encoding="utf-8", errors="replace")[-4000:] if stderr_path.exists() else ""
    completed = subprocess.CompletedProcess(cmd, process.returncode, stdout_tail, stderr_tail)
    if completed.returncode != 0:
        postprocess_log(
            lv,
            stage,
            "ERROR",
            f"{label} 失敗 returncode={completed.returncode}",
            {"stdout_log": str(stdout_path), "stderr_log": str(stderr_path), "stderr_tail": (stderr_tail or stdout_tail)[-4000:]},
        )
        raise subprocess.CalledProcessError(completed.returncode, cmd, output=stdout_tail, stderr=stderr_tail)
    postprocess_log(
        lv,
        stage,
        "DEBUG",
        f"{label} 完了 elapsed={int(time.monotonic() - started)}秒",
        {"elapsed_seconds": round(time.monotonic() - started, 1), "stdout_log": str(stdout_path), "stderr_log": str(stderr_path)},
    )
    return completed


def iso_to_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value)


def ns_to_datetime(ns: int) -> datetime:
    seconds, nanos = divmod(int(ns), 1_000_000_000)
    return datetime.fromtimestamp(seconds).replace(microsecond=nanos // 1000)


def niconico_account_target_root(config: Config | None = None, account_id: str | None = None) -> Path:
    config = config or load_config()
    account_id = str(account_id or config.recording_account_id).strip()
    if not account_id:
        account_id = DEFAULT_RECORDING_ACCOUNT_ID
    return Path(config.target_root) / "platform" / "niconico" / account_id


def niconico_platform_target_root(config: Config | None = None) -> Path:
    config = config or load_config()
    return Path(config.target_root) / "platform" / "niconico"


def broadcast_target_base_dir(config: Config | None = None, broadcaster_id: str | None = None) -> Path:
    return niconico_account_target_root(config, account_id=broadcaster_id) / "broadcast"


def broadcaster_target_dir(broadcaster_id: str, config: Config | None = None) -> Path:
    broadcaster_id = str(broadcaster_id).strip()
    if not broadcaster_id:
        raise ValueError("broadcaster_id is required")
    return broadcast_target_base_dir(config, broadcaster_id=broadcaster_id)


def broadcast_target_dir(lv: str, config: Config | None = None, broadcaster_id: str | None = None) -> Path:
    lv = str(lv).strip()
    if not lv:
        raise ValueError("lv is required")
    return broadcast_target_base_dir(config, broadcaster_id=broadcaster_id) / lv


def ensure_broadcast_target_dirs(
    config: Config | None = None,
    lv: str | None = None,
    broadcaster_id: str | None = None,
) -> Path:
    if lv:
        path = broadcast_target_dir(lv, config, broadcaster_id=broadcaster_id)
    else:
        path = broadcast_target_base_dir(config, broadcaster_id=broadcaster_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def list_broadcaster_triggers(user_id: str, broadcaster_id: str) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id, user_id, broadcaster_id, trigger_name, keyword,
                   action_type, action_payload, enabled, created_at, updated_at
            FROM broadcaster_triggers
            WHERE user_id = ? AND broadcaster_id = ?
            ORDER BY enabled DESC, trigger_name, keyword
            """,
            (user_id, broadcaster_id),
        ).fetchall()
    return [dict(row) for row in rows]


def replace_broadcaster_triggers(
    user_id: str,
    broadcaster_id: str,
    rows: list[dict[str, Any]],
    *,
    old_broadcaster_id: str | None = None,
) -> None:
    current_time = now()
    with connect() as conn:
        if old_broadcaster_id and old_broadcaster_id != broadcaster_id:
            conn.execute(
                "DELETE FROM broadcaster_triggers WHERE user_id = ? AND broadcaster_id = ?",
                (user_id, old_broadcaster_id),
            )
        conn.execute(
            "DELETE FROM broadcaster_triggers WHERE user_id = ? AND broadcaster_id = ?",
            (user_id, broadcaster_id),
        )
        for row in rows:
            keyword = str(row.get("keyword") or "").strip()
            if not keyword:
                continue
            conn.execute(
                """
                INSERT INTO broadcaster_triggers
                    (user_id, broadcaster_id, trigger_name, keyword,
                     action_type, action_payload, enabled, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    broadcaster_id,
                    str(row.get("trigger_name") or "新しいトリガー"),
                    keyword,
                    str(row.get("action_type") or "fixed"),
                    str(row.get("action_payload") or ""),
                    int(bool(row.get("enabled"))),
                    current_time,
                    current_time,
                ),
            )
        conn.commit()


def save_monitored_broadcaster(
    *,
    broadcaster_id: str,
    broadcaster_name: str = "",
    source_lv: str = "",
    enabled: bool = True,
    settings: dict[str, Any] | None = None,
) -> Path:
    if not broadcaster_id.strip():
        raise ValueError("broadcaster_id is required")
    broadcaster_id = broadcaster_id.strip()
    config = load_config()
    source_lv = source_lv.strip()
    target_dir = ensure_broadcast_target_dirs(
        config,
        source_lv if source_lv else None,
        broadcaster_id=broadcaster_id,
    )
    current_time = now()
    values = default_broadcaster_monitor_settings()
    with connect() as conn:
        existing = conn.execute(
            "SELECT * FROM monitored_broadcasters WHERE broadcaster_id = ?",
            (broadcaster_id,),
        ).fetchone()
        if existing and settings is None:
            for key in values:
                if key in existing.keys():
                    values[key] = int(existing[key])
        elif settings:
            values.update({key: int(bool(value)) for key, value in settings.items() if key in values})
        values["enabled"] = int(enabled if not existing else values.get("enabled", int(enabled)))
        conn.execute(
            """
            INSERT INTO monitored_broadcasters
                (broadcaster_id, broadcaster_name, source_lv, enabled, html_generation_enabled,
                 custom_settings_enabled, thumbnail_10sec_enabled, audio_timeline_enabled, ranking_enabled,
                 ai_conversation_enabled, summary_enabled, music_enabled,
                 abstract_image_enabled, emotion_score_enabled, word_extract_enabled,
                 timeline_enabled, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(broadcaster_id) DO UPDATE SET
                broadcaster_name = excluded.broadcaster_name,
                source_lv = excluded.source_lv,
                enabled = excluded.enabled,
                html_generation_enabled = excluded.html_generation_enabled,
                custom_settings_enabled = excluded.custom_settings_enabled,
                thumbnail_10sec_enabled = excluded.thumbnail_10sec_enabled,
                audio_timeline_enabled = excluded.audio_timeline_enabled,
                ranking_enabled = excluded.ranking_enabled,
                ai_conversation_enabled = excluded.ai_conversation_enabled,
                summary_enabled = excluded.summary_enabled,
                music_enabled = excluded.music_enabled,
                abstract_image_enabled = excluded.abstract_image_enabled,
                emotion_score_enabled = excluded.emotion_score_enabled,
                word_extract_enabled = excluded.word_extract_enabled,
                timeline_enabled = excluded.timeline_enabled,
                updated_at = excluded.updated_at
            """,
            (
                broadcaster_id,
                broadcaster_name.strip(),
                source_lv,
                values["enabled"],
                values["html_generation_enabled"],
                values["custom_settings_enabled"],
                values["thumbnail_10sec_enabled"],
                values["audio_timeline_enabled"],
                values["ranking_enabled"],
                values["ai_conversation_enabled"],
                values["summary_enabled"],
                values["music_enabled"],
                values["abstract_image_enabled"],
                values["emotion_score_enabled"],
                values["word_extract_enabled"],
                values["timeline_enabled"],
                current_time,
                current_time,
            ),
        )
        conn.commit()
    return target_dir


def default_broadcaster_monitor_settings() -> dict[str, int]:
    return {
        "enabled": 1,
        "html_generation_enabled": 1,
        "custom_settings_enabled": 0,
        "thumbnail_10sec_enabled": 1,
        "audio_timeline_enabled": 1,
        "ranking_enabled": 1,
        "ai_conversation_enabled": 1,
        "summary_enabled": 1,
        "music_enabled": 0,
        "abstract_image_enabled": 1,
        "emotion_score_enabled": 1,
        "word_extract_enabled": 1,
        "timeline_enabled": 1,
    }


def monitored_broadcaster_html_generation_enabled(broadcaster_id: str) -> bool:
    broadcaster_id = str(broadcaster_id or "").strip()
    if not broadcaster_id:
        return True
    with connect() as conn:
        row = conn.execute(
            """
            SELECT html_generation_enabled
            FROM monitored_broadcasters
            WHERE broadcaster_id = ?
            """,
            (broadcaster_id,),
        ).fetchone()
    return True if row is None else bool(row["html_generation_enabled"])


def broadcaster_archive_step_defaults(lv: str) -> tuple[str, dict[str, bool]]:
    """Resolve the broadcaster and its configured Step03-15 switches for a broadcast."""
    defaults = {
        "step01_data_collector": True,
        "step02_audio_transcriber": True,
        "step03_emotion_scorer": True,
        "step04_word_analyzer": True,
        "step05_summarizer": True,
        "step06_music_generator": False,
        "step07_image_generator": True,
        "step08_conversation_generator": True,
        "step09_screenshot_generator": True,
        "step10_comment_processor": True,
        "step11_special_user_html_generator": True,
        "step12_html_generator": True,
        "step13_index_generator": True,
        "step14_modern_list_generator": True,
        "step15_lolipop_uploader": False,
    }
    with connect() as conn:
        meta = conn.execute(
            "SELECT broadcaster_id FROM broadcast_archive_meta WHERE lv = ?",
            (str(lv or "").strip().lower(),),
        ).fetchone()
        broadcaster_id = str(meta["broadcaster_id"] or "").strip() if meta else ""
        row = conn.execute(
            "SELECT * FROM monitored_broadcasters WHERE broadcaster_id = ?",
            (broadcaster_id,),
        ).fetchone() if broadcaster_id else None
    if row is None:
        return broadcaster_id, defaults
    values = dict(row)
    mapping = {
        "step03_emotion_scorer": "emotion_score_enabled",
        "step04_word_analyzer": "word_extract_enabled",
        "step05_summarizer": "summary_enabled",
        "step06_music_generator": "music_enabled",
        "step07_image_generator": "abstract_image_enabled",
        "step08_conversation_generator": "ai_conversation_enabled",
        "step09_screenshot_generator": "thumbnail_10sec_enabled",
        "step10_comment_processor": "ranking_enabled",
        "step12_html_generator": "timeline_enabled",
        "step15_lolipop_uploader": "html_upload_enabled",
    }
    for step_name, column in mapping.items():
        if column in values and values[column] is not None:
            defaults[step_name] = bool(values[column])
    return broadcaster_id, defaults


def existing_recording_video_paths_by_lv(lvs: list[str]) -> dict[str, list[Path]]:
    normalized: list[str] = []
    seen_lvs: set[str] = set()
    for value in lvs:
        lv = str(value or "").strip().lower()
        if not re.fullmatch(r"lv\d+", lv) or lv in seen_lvs:
            continue
        seen_lvs.add(lv)
        normalized.append(lv)
    result = {lv: [] for lv in normalized}
    if not normalized:
        return result

    video_suffixes = {".mp4", ".mkv", ".webm", ".flv", ".ts"}
    candidates: dict[str, list[Path]] = {lv: [] for lv in normalized}
    for lv in normalized:
        if DEFAULT_SLNICO_RECORDING_ROOT.is_dir():
            candidates[lv].extend(
                path
                for path in DEFAULT_SLNICO_RECORDING_ROOT.rglob(f"{lv}*")
                if path.is_file() and path.suffix.lower() in video_suffixes
            )

    placeholders = ", ".join("?" for _ in normalized)
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT lv, target_path, source_path
            FROM recording_segments
            WHERE lv IN ({placeholders})
            ORDER BY lv, segment_index, id
            """,
            normalized,
        ).fetchall()
        target_rows = conn.execute(
            f"""
            SELECT lv, target_dir
            FROM recording_jobs
            WHERE lv IN ({placeholders})
            """,
            normalized,
        ).fetchall()

    for row in rows:
        lv = str(row["lv"] or "").strip().lower()
        for key in ("target_path", "source_path"):
            path = Path(str(row[key] or "").strip())
            if path.is_file() and path.suffix.lower() in video_suffixes:
                candidates.setdefault(lv, []).append(path)

    for row in target_rows:
        lv = str(row["lv"] or "").strip().lower()
        target_dir = Path(str(row["target_dir"] or "").strip())
        if not target_dir.is_dir():
            continue
        candidates.setdefault(lv, []).extend(
            path
            for path in target_dir.rglob(f"{lv}*")
            if path.is_file() and path.suffix.lower() in video_suffixes
        )

    suffix_priority = {".mp4": 5, ".mkv": 4, ".webm": 3, ".flv": 2, ".ts": 1}
    for lv in normalized:
        by_recording: dict[str, Path] = {}
        for path in candidates.get(lv, []):
            recording_key = path.stem.casefold()
            current = by_recording.get(recording_key)
            if current is None or suffix_priority.get(path.suffix.lower(), 0) > suffix_priority.get(
                current.suffix.lower(), 0
            ):
                by_recording[recording_key] = path
        result[lv] = sorted(
            by_recording.values(),
            key=lambda path: (path.stem.casefold(), path.suffix.casefold()),
        )
        if result[lv]:
            postprocess_log(
                lv,
                "local_video_resolve",
                "INFO",
                f"同一LVの録画区間を全件使用: files={len(result[lv])}",
                {"paths": [str(path) for path in result[lv]]},
            )
        else:
            postprocess_log(
                lv,
                "local_video_resolve",
                "WARN",
                "SlNicoLiveRec保存先にもDB登録パスにも動画なし",
            )
    return result


def archive_processed_video_files(
    lv: str,
    paths: list[Path | str],
    *,
    target_dir: Path | str | None = None,
    html_file: Path | str | None = None,
) -> dict[str, Any]:
    """Move successfully processed source videos below the LV HTML directory."""
    lv = str(lv or "").strip().lower()
    if not re.fullmatch(r"lv\d+", lv):
        raise ValueError(f"invalid lv: {lv}")

    html_path = Path(str(html_file or "").strip()) if str(html_file or "").strip() else None
    destination_parent = html_path.parent if html_path is not None else None
    if destination_parent is None and str(target_dir or "").strip():
        destination_parent = Path(str(target_dir))
    if destination_parent is None:
        with connect() as conn:
            row = conn.execute(
                """
                SELECT m.html_path, j.target_dir
                FROM broadcast_archive_meta AS m
                LEFT JOIN recording_jobs AS j ON j.lv = m.lv
                WHERE m.lv = ?
                """,
                (lv,),
            ).fetchone()
        if row and str(row["html_path"] or "").strip():
            destination_parent = Path(str(row["html_path"])).parent
        elif row and str(row["target_dir"] or "").strip():
            destination_parent = Path(str(row["target_dir"]))
    if destination_parent is None:
        raise RuntimeError(f"{lv}: HTML生成フォルダを特定できません")

    archive_dir = destination_parent / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    archive_dir_resolved = archive_dir.resolve()
    moved: list[tuple[Path, Path]] = []
    reused: list[Path] = []

    try:
        for raw_path in paths:
            source = Path(raw_path)
            if not source.is_file():
                raise FileNotFoundError(f"処理済み動画が見つかりません: {source}")
            source_resolved = source.resolve()
            if source_resolved.parent == archive_dir_resolved:
                reused.append(source_resolved)
                continue

            destination = archive_dir / source.name
            if destination.exists():
                counter = 2
                while True:
                    candidate = archive_dir / f"{source.stem}_{counter}{source.suffix}"
                    if not candidate.exists():
                        destination = candidate
                        break
                    counter += 1
            shutil.move(str(source_resolved), str(destination))
            moved.append((source_resolved, destination.resolve()))

        with connect() as conn:
            for source, destination in moved:
                conn.execute(
                    """
                    UPDATE recording_segments
                    SET source_path = CASE WHEN source_path = ? THEN ? ELSE source_path END,
                        target_path = CASE WHEN target_path = ? THEN ? ELSE target_path END
                    WHERE lv = ? AND (source_path = ? OR target_path = ?)
                    """,
                    (
                        str(source),
                        str(destination),
                        str(source),
                        str(destination),
                        lv,
                        str(source),
                        str(source),
                    ),
                )
            conn.commit()
    except Exception:
        for source, destination in reversed(moved):
            if destination.exists() and not source.exists():
                source.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(destination), str(source))
        raise

    result = {
        "lv": lv,
        "archive_dir": str(archive_dir_resolved),
        "moved": [
            {"source_path": str(source), "archive_path": str(destination)}
            for source, destination in moved
        ],
        "reused": [str(path) for path in reused],
    }
    postprocess_log(
        lv,
        "archive_video",
        "INFO",
        f"処理済み動画をHTMLフォルダ配下へ移動: files={len(moved)}",
        result,
    )
    return result


def list_monitored_broadcasters() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM monitored_broadcasters
            ORDER BY updated_at DESC, broadcaster_id
            """
        ).fetchall()
    return [dict(row) for row in rows]


def list_enabled_monitored_broadcasters() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM monitored_broadcasters
            WHERE enabled = 1
            ORDER BY updated_at DESC, broadcaster_id
            """
        ).fetchall()
    return [dict(row) for row in rows]


def enabled_monitored_broadcaster_map(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT *
        FROM monitored_broadcasters
        WHERE enabled = 1
        """
    ).fetchall()
    return {str(row["broadcaster_id"]): dict(row) for row in rows if row["broadcaster_id"]}


def disabled_monitored_broadcaster_ids(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        """
        SELECT broadcaster_id
        FROM monitored_broadcasters
        WHERE enabled = 0
        """
    ).fetchall()
    return {str(row["broadcaster_id"]).strip() for row in rows if str(row["broadcaster_id"] or "").strip()}


def is_process_running(pid: int | None) -> bool:
    if not pid:
        return False
    pid_value = int(pid)
    if os.name == "nt":
        kernel32 = ctypes.windll.kernel32
        process_query_limited_information = 0x1000
        still_active = 259
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid_value)
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid_value, 0)
        return True
    except (OSError, SystemError):
        return False


def record_recording_event(
    conn: sqlite3.Connection,
    *,
    lv: str,
    broadcaster_id: str,
    broadcaster_name: str,
    watch_url: str,
    recorder: str,
    pid: int | None,
    event_type: str,
    event_at: str,
    started_at: str | None,
    ended_at: str | None,
    duration_us: int | None,
    exit_code: int | None,
    target_dir: str,
    payload: dict[str, Any] | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO recording_events
            (lv, broadcaster_id, broadcaster_name, watch_url, recorder, pid,
             event_type, event_at, started_at, ended_at, duration_us, exit_code,
             target_dir, payload_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            lv,
            broadcaster_id,
            broadcaster_name,
            watch_url,
            recorder,
            pid,
            event_type,
            event_at,
            started_at,
            ended_at,
            duration_us,
            exit_code,
            target_dir,
            json.dumps(payload or {}, ensure_ascii=False, default=str),
            now_micro(),
        ),
    )


def watch_recording_process(
    *,
    process: subprocess.Popen,
    lv: str,
    broadcaster_id: str,
    broadcaster_name: str,
    watch_url: str,
    recorder: str,
    started_at: str,
    target_dir: str,
) -> None:
    process_check_count = 0
    recording_confirmed = False
    while True:
        exit_code = process.poll()
        if exit_code is not None:
            break
        process_check_count += 1
        checked_at = now_micro()
        with connect() as conn:
            conn.execute(
                """
                UPDATE recording_jobs
                SET status = ?,
                    last_process_check_at = ?,
                    process_check_count = process_check_count + 1,
                    updated_at = ?,
                    error = NULL
                WHERE lv = ? AND pid = ?
                """,
                ("recording", checked_at, checked_at, lv, process.pid),
            )
            if not recording_confirmed:
                record_recording_event(
                    conn,
                    lv=lv,
                    broadcaster_id=broadcaster_id,
                    broadcaster_name=broadcaster_name,
                    watch_url=watch_url,
                    recorder=recorder,
                    pid=process.pid,
                    event_type="recording_confirmed",
                    event_at=checked_at,
                    started_at=started_at,
                    ended_at=None,
                    duration_us=None,
                    exit_code=None,
                    target_dir=target_dir,
                    payload={"process_check_count": process_check_count},
                )
                recording_confirmed = True
            conn.commit()
        time.sleep(5.0)
    ended_at = now_micro()
    try:
        duration_us = int((iso_to_datetime(ended_at) - iso_to_datetime(started_at)).total_seconds() * 1_000_000)
    except Exception:
        duration_us = None
    should_restart_after_exit = True
    with connect() as conn:
        current = conn.execute(
            "SELECT status FROM recording_jobs WHERE lv = ? AND pid = ?",
            (lv, process.pid),
        ).fetchone()
        if current and str(current["status"] or "") == "stopped":
            should_restart_after_exit = False
        conn.execute(
            """
            UPDATE recording_jobs
            SET status = CASE WHEN status = 'stopped' THEN status ELSE ? END,
                last_exit_at = ?,
                updated_at = ?,
                error = NULL
            WHERE lv = ? AND pid = ?
            """,
            ("exited", ended_at, ended_at, lv, process.pid),
        )
        record_recording_event(
            conn,
            lv=lv,
            broadcaster_id=broadcaster_id,
            broadcaster_name=broadcaster_name,
            watch_url=watch_url,
            recorder=recorder,
            pid=process.pid,
            event_type="ended",
            event_at=ended_at,
            started_at=started_at,
            ended_at=ended_at,
            duration_us=duration_us,
            exit_code=exit_code,
            target_dir=target_dir,
            payload={"exit_code": exit_code},
        )
        conn.commit()
    start_segment_mp4_conversion_after_exit(
        lv=lv,
        broadcaster_id=broadcaster_id,
        broadcaster_name=broadcaster_name,
        watch_url=watch_url,
        recorder=recorder,
        pid=process.pid,
        started_at=started_at,
        ended_at=ended_at,
        exit_code=exit_code,
        target_dir=target_dir,
    )
    if not should_restart_after_exit:
        return
    maybe_restart_recording_after_exit(
        lv=lv,
        broadcaster_id=broadcaster_id,
        broadcaster_name=broadcaster_name,
        watch_url=watch_url,
        recorder=recorder,
        previous_pid=process.pid,
        exit_code=exit_code,
        ended_at=ended_at,
        target_dir=target_dir,
    )


def maybe_restart_recording_after_exit(
    *,
    lv: str,
    broadcaster_id: str,
    broadcaster_name: str,
    watch_url: str,
    recorder: str,
    previous_pid: int,
    exit_code: int | None,
    ended_at: str,
    target_dir: str,
) -> dict[str, Any]:
    config = load_config()
    if not config.recording_auto_restart:
        return {"restarted": False, "reason": "auto_restart_disabled"}
    if is_supported_broadcast_history_provider_id(broadcaster_id):
        api_check = check_live_still_on_air_by_broadcaster_api(lv, broadcaster_id)
        with connect() as conn:
            record_recording_event(
                conn,
                lv=lv,
                broadcaster_id=broadcaster_id,
                broadcaster_name=broadcaster_name,
                watch_url=watch_url,
                recorder=recorder,
                pid=previous_pid,
                event_type="restart_liveness_checked_api",
                event_at=now_micro(),
                started_at=None,
                ended_at=ended_at,
                duration_us=None,
                exit_code=exit_code,
                target_dir=target_dir,
                payload={**api_check, "source_event": "recording_process_exit"},
            )
            conn.commit()
        if api_check.get("checked") and not api_check.get("on_air"):
            finalize = finalize_recording_if_broadcast_ended(
                lv=lv,
                broadcaster_id=broadcaster_id,
                broadcaster_name=broadcaster_name,
                watch_url=watch_url,
                recorder=recorder,
                previous_pid=previous_pid,
                exit_code=exit_code,
                ended_at=ended_at,
                target_dir=target_dir,
                source_event="restart_precheck_api_end",
            )
            with connect() as conn:
                record_recording_event(
                    conn,
                    lv=lv,
                    broadcaster_id=broadcaster_id,
                    broadcaster_name=broadcaster_name,
                    watch_url=watch_url,
                    recorder=recorder,
                    pid=previous_pid,
                    event_type="restart_skipped",
                    event_at=now_micro(),
                    started_at=None,
                    ended_at=ended_at,
                    duration_us=None,
                    exit_code=exit_code,
                    target_dir=target_dir,
                    payload={"reason": "broadcast_ended", "finalize": finalize},
                )
                conn.commit()
            return {"restarted": False, "reason": "broadcast_ended", "finalize": finalize}
    start_broadcast_endtime_probe_after_exit(
        lv=lv,
        broadcaster_id=broadcaster_id,
        broadcaster_name=broadcaster_name,
        watch_url=watch_url,
        recorder=recorder,
        previous_pid=previous_pid,
        exit_code=exit_code,
        ended_at=ended_at,
        target_dir=target_dir,
    )
    with connect() as conn:
        row = conn.execute(
            "SELECT restart_count FROM recording_jobs WHERE lv = ?",
            (lv,),
        ).fetchone()
        restart_count = int((row["restart_count"] if row else 0) or 0)
        if restart_count >= config.recording_max_restarts:
            record_recording_event(
                conn,
                lv=lv,
                broadcaster_id=broadcaster_id,
                broadcaster_name=broadcaster_name,
                watch_url=watch_url,
                recorder=recorder,
                pid=previous_pid,
                event_type="restart_skipped",
                event_at=now_micro(),
                started_at=None,
                ended_at=ended_at,
                duration_us=None,
                exit_code=exit_code,
                target_dir=target_dir,
                payload={"reason": "max_restarts", "restart_count": restart_count},
            )
            conn.commit()
            return {"restarted": False, "reason": "max_restarts", "restart_count": restart_count}
        scheduled_at = now_micro()
        record_recording_event(
            conn,
            lv=lv,
            broadcaster_id=broadcaster_id,
            broadcaster_name=broadcaster_name,
            watch_url=watch_url,
            recorder=recorder,
            pid=previous_pid,
            event_type="restart_scheduled",
            event_at=scheduled_at,
            started_at=None,
            ended_at=ended_at,
            duration_us=None,
            exit_code=exit_code,
            target_dir=target_dir,
            payload={
                "next_restart_count": restart_count + 1,
                "delay_seconds": config.recording_restart_delay_seconds,
            },
        )
        conn.commit()

    delay = max(0.0, float(config.recording_restart_delay_seconds or 0.0))
    if delay:
        time.sleep(delay)

    with connect() as conn:
        current = conn.execute(
            "SELECT status FROM recording_jobs WHERE lv = ?",
            (lv,),
        ).fetchone()
        current_status = str(current["status"] or "") if current else ""
        if current_status in {"finalize_queued", "finalize_skipped"}:
            return {
                "restarted": False,
                "reason": (
                    "finalize_already_queued"
                    if current_status == "finalize_queued"
                    else "finalize_skipped"
                ),
            }
        conn.execute(
            """
            UPDATE recording_jobs
            SET restart_count = restart_count + 1, updated_at = ?
            WHERE lv = ?
            """,
            (now_micro(), lv),
        )
        conn.commit()
        result = start_recording_for_broadcast(
            conn,
            {
                "lv": lv,
                "broadcaster_id": broadcaster_id,
                "broadcaster_name": broadcaster_name,
                "watch_url": watch_url,
            },
            config,
            recorder=recorder,
            force_restart=True,
        )
        register_recording_gaps_from_events(conn, lv)
        record_recording_event(
            conn,
            lv=lv,
            broadcaster_id=broadcaster_id,
            broadcaster_name=broadcaster_name,
            watch_url=watch_url,
            recorder=recorder,
            pid=int(result.get("pid") or 0),
            event_type="gap_registered_after_restart",
            event_at=now_micro(),
            started_at=None,
            ended_at=ended_at,
            duration_us=None,
            exit_code=exit_code,
            target_dir=target_dir,
            payload={},
        )
        conn.commit()
        return result


def start_broadcast_endtime_probe_after_exit(
    *,
    lv: str,
    broadcaster_id: str,
    broadcaster_name: str,
    watch_url: str,
    recorder: str,
    previous_pid: int,
    exit_code: int | None,
    ended_at: str,
    target_dir: str,
) -> None:
    thread = threading.Thread(
        target=probe_broadcast_endtime_after_recording_exit,
        kwargs={
            "lv": lv,
            "broadcaster_id": broadcaster_id,
            "broadcaster_name": broadcaster_name,
            "watch_url": watch_url,
            "recorder": recorder,
            "previous_pid": previous_pid,
            "exit_code": exit_code,
            "ended_at": ended_at,
            "target_dir": target_dir,
        },
        name=f"broadcast-endtime-probe-{lv}-{previous_pid}",
        daemon=True,
    )
    thread.start()


def probe_broadcast_endtime_after_recording_exit(
    *,
    lv: str,
    broadcaster_id: str,
    broadcaster_name: str,
    watch_url: str,
    recorder: str,
    previous_pid: int,
    exit_code: int | None,
    ended_at: str,
    target_dir: str,
) -> None:
    try:
        finalize_recording_if_broadcast_ended(
            lv=lv,
            broadcaster_id=broadcaster_id,
            broadcaster_name=broadcaster_name,
            watch_url=watch_url,
            recorder=recorder,
            previous_pid=previous_pid,
            exit_code=exit_code,
            ended_at=ended_at,
            target_dir=target_dir,
            source_event="recording_process_exit",
        )
    except Exception as exc:
        with connect() as conn:
            record_recording_event(
                conn,
                lv=lv,
                broadcaster_id=broadcaster_id,
                broadcaster_name=broadcaster_name,
                watch_url=watch_url,
                recorder=recorder,
                pid=previous_pid,
                event_type="broadcast_meta_check_failed_async",
                event_at=now_micro(),
                started_at=None,
                ended_at=ended_at,
                duration_us=None,
                exit_code=exit_code,
                target_dir=target_dir,
                payload={"error": f"{type(exc).__name__}: {exc}"},
            )
            conn.commit()


FINALIZE_QUEUE_BLOCKING_STATES = {"preparing", "queued", "running", "done"}
FINALIZE_PREPARING_STALE_SECONDS = 5 * 60
FINALIZE_DISPATCHER_STALE_SECONDS = 30
FINALIZE_WORKER_LAUNCH_GRACE_SECONDS = 2 * 60


def reserve_finalize_queue_item(
    conn: sqlite3.Connection,
    *,
    lv: str,
    broadcaster_id: str,
    target_dir: str,
    source_kind: str = "live",
    timeline_mode: str = "live",
    input_dir: Path | str | None = None,
    segment_paths: list[Path | str] | None = None,
    transcribe: bool = True,
    whisper_model: str = "large-v3",
) -> bool:
    """Atomically reserve one durable finalization slot for an LV."""
    current_time = now_micro()
    input_dir_text = str(input_dir or "")
    segment_paths_text = json.dumps([str(path) for path in segment_paths or []], ensure_ascii=False)
    transcribe_value = int(bool(transcribe))
    whisper_model_text = str(whisper_model or "large-v3")
    owner_pid = int(os.getpid())
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO finalize_queue
            (lv, broadcaster_id, target_dir, source_kind, timeline_mode,
             input_dir, segment_paths_json, transcribe, whisper_model,
             status, worker_pid, attempts, error, result_json,
             created_at, queued_at, started_at, finished_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'preparing', ?, 0, NULL, NULL,
                ?, NULL, NULL, NULL, ?)
        """,
        (
            lv,
            broadcaster_id,
            target_dir,
            str(source_kind or "live"),
            str(timeline_mode or "live"),
            input_dir_text,
            segment_paths_text,
            transcribe_value,
            whisper_model_text,
            owner_pid,
            current_time,
            current_time,
        ),
    )
    if int(cursor.rowcount or 0) == 1:
        return True
    retry_cursor = conn.execute(
        """
        UPDATE finalize_queue
        SET broadcaster_id = ?, target_dir = ?, source_kind = ?, timeline_mode = ?,
            input_dir = ?, segment_paths_json = ?, transcribe = ?, whisper_model = ?,
            status = 'preparing', worker_pid = ?, attempts = 0,
            error = NULL, result_json = NULL,
            created_at = ?, queued_at = NULL, started_at = NULL, finished_at = NULL,
            updated_at = ?
        WHERE lv = ? AND status = 'failed'
        """,
        (
            broadcaster_id,
            target_dir,
            str(source_kind or "live"),
            str(timeline_mode or "live"),
            input_dir_text,
            segment_paths_text,
            transcribe_value,
            whisper_model_text,
            owner_pid,
            current_time,
            current_time,
            lv,
        ),
    )
    return int(retry_cursor.rowcount or 0) == 1


def mark_finalize_queue_ready(conn: sqlite3.Connection, lv: str) -> None:
    current_time = now_micro()
    conn.execute(
        """
        UPDATE finalize_queue
        SET status = 'queued', queued_at = ?, worker_pid = NULL,
            error = NULL, result_json = NULL, updated_at = ?
        WHERE lv = ? AND status = 'preparing'
        """,
        (current_time, current_time, lv),
    )
    conn.execute(
        """
        UPDATE recording_jobs
        SET status = 'finalize_queued', error = NULL, updated_at = ?
        WHERE lv = ?
        """,
        (current_time, lv),
    )


def finalize_queue_has_work() -> bool:
    with connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM finalize_queue WHERE status IN ('preparing', 'queued', 'running') LIMIT 1"
        ).fetchone()
    return row is not None


def _timestamp_age_seconds(value: str | None) -> float:
    try:
        parsed = datetime.fromisoformat(str(value or ""))
        reference = datetime.now(parsed.tzinfo) if parsed.tzinfo else datetime.now()
        return max(0.0, (reference - parsed).total_seconds())
    except (TypeError, ValueError):
        return float("inf")


def reconcile_oldest_preparing_finalize_item() -> str:
    """Recover a preparation reservation whose owning GUI is no longer healthy."""
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT lv, status, worker_pid, updated_at
            FROM finalize_queue
            WHERE status IN ('preparing', 'queued')
            ORDER BY created_at, lv
            LIMIT 1
            """
        ).fetchone()
        if row is None or str(row["status"] or "") != "preparing":
            conn.commit()
            return "none"
        lv = str(row["lv"])
        owner_pid = int(row["worker_pid"] or 0)
        age_seconds = _timestamp_age_seconds(str(row["updated_at"] or ""))
        if owner_pid > 0 and is_process_running(owner_pid) and age_seconds < FINALIZE_PREPARING_STALE_SECONDS:
            conn.commit()
            return "waiting"
        current_time = now_micro()
        conn.execute(
            """
            UPDATE finalize_queue
            SET status = 'queued', worker_pid = NULL,
                queued_at = COALESCE(queued_at, created_at, ?),
                error = CASE
                    WHEN error IS NULL OR error = '' THEN 'recovered interrupted queue preparation'
                    ELSE error
                END,
                updated_at = ?
            WHERE lv = ? AND status = 'preparing'
            """,
            (current_time, current_time, lv),
        )
        conn.execute(
            """
            UPDATE recording_jobs
            SET status = 'finalize_queued', error = NULL, updated_at = ?
            WHERE lv = ?
            """,
            (current_time, lv),
        )
        conn.commit()
    postprocess_log(
        lv,
        "dispatcher",
        "WARN",
        "中断された終了キュー準備を復旧",
        {"owner_pid": owner_pid, "age_seconds": age_seconds},
    )
    return "recovered"


def claim_next_finalize_queue_item() -> dict[str, Any] | None:
    """Claim the oldest item, but never while another finalizer is running."""
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        running = conn.execute(
            "SELECT 1 FROM finalize_queue WHERE status = 'running' LIMIT 1"
        ).fetchone()
        if running is not None:
            conn.commit()
            return None
        row = conn.execute(
            """
            SELECT lv, broadcaster_id, target_dir, source_kind, timeline_mode, status,
                   input_dir, segment_paths_json, transcribe, whisper_model,
                   attempts, created_at, queued_at
            FROM finalize_queue
            WHERE status IN ('preparing', 'queued')
            ORDER BY created_at, lv
            LIMIT 1
            """
        ).fetchone()
        if row is None:
            conn.commit()
            return None
        if str(row["status"] or "") == "preparing":
            conn.commit()
            return None
        started_at = now_micro()
        cursor = conn.execute(
            """
            UPDATE finalize_queue
            SET status = 'running', worker_pid = NULL,
                attempts = attempts + 1, started_at = ?, finished_at = NULL,
                error = NULL, result_json = NULL, updated_at = ?
            WHERE lv = ? AND status = 'queued'
            """,
            (started_at, started_at, str(row["lv"])),
        )
        if int(cursor.rowcount or 0) != 1:
            conn.rollback()
            return None
        conn.execute(
            "UPDATE recording_jobs SET status = 'finalizing', updated_at = ? WHERE lv = ?",
            (started_at, str(row["lv"])),
        )
        conn.commit()
        result = dict(row)
        result["started_at"] = started_at
        result["attempts"] = int(row["attempts"] or 0) + 1
        return result


def set_finalize_queue_worker_pid(
    lv: str,
    worker_pid: int,
    *,
    attempts: int | None = None,
    only_if_empty: bool = False,
) -> None:
    current_time = now_micro()
    with connect() as conn:
        empty_clause = " AND worker_pid IS NULL" if only_if_empty else ""
        if attempts is None:
            conn.execute(
                f"""
                UPDATE finalize_queue
                SET worker_pid = ?, updated_at = ?
                WHERE lv = ? AND status = 'running'{empty_clause}
                """,
                (int(worker_pid), current_time, lv),
            )
        else:
            conn.execute(
                f"""
                UPDATE finalize_queue
                SET worker_pid = ?, updated_at = ?
                WHERE lv = ? AND status = 'running' AND attempts = ?{empty_clause}
                """,
                (int(worker_pid), current_time, lv, int(attempts)),
            )
        conn.commit()


def finish_finalize_queue_item(
    lv: str,
    *,
    success: bool,
    error: str = "",
    result: dict[str, Any] | None = None,
) -> None:
    current_time = now_micro()
    queue_status = "done" if success else "failed"
    recording_status = "finalized" if success else "finalize_failed"
    with connect() as conn:
        conn.execute(
            """
            UPDATE finalize_queue
            SET status = ?, worker_pid = NULL, finished_at = ?, error = ?,
                result_json = ?, updated_at = ?
            WHERE lv = ?
            """,
            (
                queue_status,
                current_time,
                error or None,
                json.dumps(result, ensure_ascii=False, default=str) if result is not None else None,
                current_time,
                lv,
            ),
        )
        conn.execute(
            "UPDATE recording_jobs SET status = ?, error = ?, updated_at = ? WHERE lv = ?",
            (recording_status, error or None, current_time, lv),
        )
        conn.commit()


def update_finalize_dispatcher_state(pid: int, *, started: bool = False) -> None:
    current_time = now_micro()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO finalize_dispatcher_state
                (singleton_id, pid, started_at, heartbeat_at, updated_at)
            VALUES (1, ?, ?, ?, ?)
            ON CONFLICT(singleton_id) DO UPDATE SET
                pid = excluded.pid,
                started_at = CASE
                    WHEN ? THEN excluded.started_at
                    ELSE COALESCE(finalize_dispatcher_state.started_at, excluded.started_at)
                END,
                heartbeat_at = excluded.heartbeat_at,
                updated_at = excluded.updated_at
            """,
            (int(pid), current_time, current_time, current_time, int(bool(started))),
        )
        conn.commit()


def clear_finalize_dispatcher_state(pid: int) -> None:
    current_time = now_micro()
    with connect() as conn:
        conn.execute(
            """
            UPDATE finalize_dispatcher_state
            SET pid = NULL, heartbeat_at = NULL, updated_at = ?
            WHERE singleton_id = 1 AND pid = ?
            """,
            (current_time, int(pid)),
        )
        conn.commit()


def finalize_dispatcher_is_active() -> bool:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT pid, heartbeat_at
            FROM finalize_dispatcher_state
            WHERE singleton_id = 1
            """
        ).fetchone()
    if row is None:
        return False
    pid = int(row["pid"] or 0)
    heartbeat_age = _timestamp_age_seconds(str(row["heartbeat_at"] or ""))
    return (
        pid > 0
        and heartbeat_age < FINALIZE_DISPATCHER_STALE_SECONDS
        and is_process_running(pid)
    )


def wait_for_finalize_queue_item(
    lv: str,
    *,
    timeout_seconds: float = 24 * 60 * 60,
    stage_start_callback: Callable[[str, str], None] | None = None,
    log_after_id: int = 0,
) -> dict[str, Any]:
    deadline = time.monotonic() + max(1.0, float(timeout_seconds))
    next_dispatcher_check = 0.0
    last_log_id = max(0, int(log_after_id or 0))
    while True:
        stage_events: list[tuple[str, str]] = []
        with connect() as conn:
            if stage_start_callback is not None:
                log_rows = conn.execute(
                    """
                    SELECT id, stage, message
                    FROM postprocess_logs
                    WHERE lv = ? AND id > ?
                      AND (
                          message = 'stage=running'
                          OR message LIKE 'legacy step開始:%'
                      )
                    ORDER BY id
                    """,
                    (lv, last_log_id),
                ).fetchall()
                for log_row in log_rows:
                    last_log_id = max(last_log_id, int(log_row["id"] or 0))
                    stage_events.append(
                        (
                            str(log_row["stage"] or ""),
                            str(log_row["message"] or ""),
                        )
                    )
            row = conn.execute(
                "SELECT status, error, result_json FROM finalize_queue WHERE lv = ?",
                (lv,),
            ).fetchone()
        for stage, message in stage_events:
            stage_start_callback(stage, message)
        if row is None:
            raise RuntimeError(f"終了処理キュー項目が見つかりません: {lv}")
        status = str(row["status"] or "")
        if status == "done":
            raw_result = str(row["result_json"] or "").strip()
            return json.loads(raw_result) if raw_result else {"lv": lv, "queued": True}
        if status == "failed":
            raise RuntimeError(str(row["error"] or f"終了処理失敗: {lv}"))
        monotonic_now = time.monotonic()
        if status in {"preparing", "queued", "running"} and monotonic_now >= next_dispatcher_check:
            start_finalize_dispatcher_process()
            next_dispatcher_check = monotonic_now + 5.0
        if monotonic_now >= deadline:
            raise TimeoutError(f"終了処理キュー待機がタイムアウトしました: {lv} status={status}")
        time.sleep(0.5)


def enqueue_finalize_pipeline_and_wait(
    lv: str,
    *,
    broadcaster_id: str = "",
    input_dir: Path | str | None = None,
    timeline_mode: str = "live",
    segment_paths: list[Path | str] | None = None,
    transcribe: bool = True,
    whisper_model: str = "large-v3",
    target_dir: Path | str | None = None,
    source_kind: str = "manual",
    timeout_seconds: float = 24 * 60 * 60,
    stage_start_callback: Callable[[str, str], None] | None = None,
) -> dict[str, Any]:
    """Put manual/timeshift finalization onto the same durable FIFO and wait."""
    with connect() as conn:
        log_after_id = int(
            conn.execute(
                "SELECT COALESCE(MAX(id), 0) FROM postprocess_logs WHERE lv = ?",
                (lv,),
            ).fetchone()[0]
        )
        reserved = reserve_finalize_queue_item(
            conn,
            lv=lv,
            broadcaster_id=broadcaster_id,
            target_dir=str(target_dir or ""),
            source_kind=source_kind,
            timeline_mode=timeline_mode,
            input_dir=input_dir,
            segment_paths=segment_paths,
            transcribe=transcribe,
            whisper_model=whisper_model,
        )
        if reserved:
            mark_finalize_queue_ready(conn, lv)
        conn.commit()
    start_finalize_dispatcher_process()
    return wait_for_finalize_queue_item(
        lv,
        timeout_seconds=timeout_seconds,
        stage_start_callback=stage_start_callback,
        log_after_id=log_after_id,
    )


def requeue_interrupted_finalize_item(lv: str, *, error: str) -> None:
    current_time = now_micro()
    with connect() as conn:
        row = conn.execute(
            "SELECT attempts FROM finalize_queue WHERE lv = ? AND status = 'running'",
            (lv,),
        ).fetchone()
        if row is None:
            return
        attempts = int(row["attempts"] or 0)
        if attempts >= 3:
            conn.execute(
                """
                UPDATE finalize_queue
                SET status = 'failed', worker_pid = NULL, finished_at = ?, error = ?, updated_at = ?
                WHERE lv = ? AND status = 'running'
                """,
                (current_time, error, current_time, lv),
            )
            conn.execute(
                "UPDATE recording_jobs SET status = 'finalize_failed', error = ?, updated_at = ? WHERE lv = ?",
                (error, current_time, lv),
            )
        else:
            conn.execute(
                """
                UPDATE finalize_queue
                SET status = 'queued', worker_pid = NULL, queued_at = ?,
                    started_at = NULL, error = ?, result_json = NULL, updated_at = ?
                WHERE lv = ? AND status = 'running'
                """,
                (current_time, error, current_time, lv),
            )
            conn.execute(
                "UPDATE recording_jobs SET status = 'finalize_queued', error = NULL, updated_at = ? WHERE lv = ?",
                (current_time, lv),
            )
        conn.commit()


def start_finalize_dispatcher_process() -> bool:
    """Start the small persistent-queue dispatcher when work is waiting."""
    global _FINALIZE_DISPATCHER_PROCESS
    if not finalize_queue_has_work():
        return False
    if finalize_dispatcher_is_active():
        return True
    with _FINALIZE_DISPATCHER_PROCESS_LOCK:
        if _FINALIZE_DISPATCHER_PROCESS is not None and _FINALIZE_DISPATCHER_PROCESS.poll() is None:
            return True
        if finalize_dispatcher_is_active():
            return True
        script = ROOT / "tools" / "run_finalize_dispatcher.py"
        if not script.exists():
            postprocess_log("", "dispatcher", "ERROR", f"終了処理ディスパッチャーが見つからない: {script}")
            return False
        log_dir = TMP_DIR / "finalize_dispatcher_logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"dispatcher_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
        log_handle = log_path.open("a", encoding="utf-8", buffering=1)
        try:
            _FINALIZE_DISPATCHER_PROCESS = subprocess.Popen(
                [str(Path(sys.executable)), str(script)],
                cwd=str(ROOT),
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                text=True,
                creationflags=_windows_no_console_creationflags(),
            )
        finally:
            log_handle.close()
        postprocess_log(
            "",
            "dispatcher",
            "INFO",
            "終了処理の直列キューディスパッチャーを起動",
            {"pid": _FINALIZE_DISPATCHER_PROCESS.pid, "log_path": str(log_path)},
        )
        return True


def start_finalize_pipeline_after_recording_end(
    *,
    lv: str,
    broadcaster_id: str,
    target_dir: str,
) -> None:
    if not start_finalize_dispatcher_process():
        postprocess_log(lv, "dispatcher", "ERROR", "終了処理キューディスパッチャーを起動できない")


def start_visible_finalize_pipeline_process(
    *,
    lv: str,
    broadcaster_id: str,
    target_dir: str,
    input_dir: Path | str | None = None,
    timeline_mode: str = "live",
    segment_paths: list[Path | str] | None = None,
    transcribe: bool = True,
    whisper_model: str = "large-v3",
    prepare_live_inputs: bool = False,
    queue_attempt: int | None = None,
    result_json_path: Path | str | None = None,
) -> subprocess.Popen | None:
    """Run the heavy finalize flow in a separate process without a console."""
    try:
        script = ROOT / "tools" / "run_finalize_pipeline.py"
        python_exe = Path(sys.executable)
        if not script.exists():
            postprocess_log(lv, "launcher", "WARN", f"後処理ランナーが見つからない: {script}")
            return None
        args = [
            str(python_exe),
            str(script),
            "--lv",
            lv,
            "--broadcaster-id",
            broadcaster_id or "",
            "--target-dir",
            target_dir or "",
            "--timeline-mode",
            str(timeline_mode or "live"),
            "--whisper-model",
            str(whisper_model or "large-v3"),
        ]
        if input_dir:
            args.extend(["--input-dir", str(input_dir)])
        for segment_path in segment_paths or []:
            args.extend(["--segment-path", str(segment_path)])
        if not transcribe:
            args.append("--no-transcribe")
        if prepare_live_inputs:
            args.append("--prepare-live-inputs")
        if queue_attempt is not None:
            args.extend(["--queue-attempt", str(int(queue_attempt))])
        if result_json_path:
            args.extend(["--result-json", str(result_json_path)])
        log_dir = TMP_DIR / "finalize_process_logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"{lv}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
        log_handle = log_path.open("a", encoding="utf-8", buffering=1)
        try:
            process = subprocess.Popen(
                args,
                cwd=str(ROOT),
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                text=True,
                creationflags=_windows_no_console_creationflags(),
            )
        finally:
            log_handle.close()
        postprocess_log(
            lv,
            "launcher",
            "INFO",
            "後処理ワーカープロセスを非表示起動",
            {"pid": process.pid, "cmd": args, "log_path": str(log_path)},
        )
        return process
    except Exception as exc:
        postprocess_log(lv, "launcher", "ERROR", f"後処理ワーカー起動失敗: {type(exc).__name__}: {exc}")
        return None


def run_finalize_pipeline_after_recording_end(
    *,
    lv: str,
    broadcaster_id: str,
    target_dir: str,
) -> None:
    try:
        enqueue_finalize_pipeline_and_wait(
            lv,
            broadcaster_id=broadcaster_id,
            target_dir=target_dir,
            source_kind="live",
        )
    except Exception as exc:
        postprocess_log(
            lv,
            "dispatcher",
            "ERROR",
            f"終了処理キュー待機失敗: {type(exc).__name__}: {exc}",
        )


def terminate_recording_processes_for_lv(lv: str, *, exclude_pids: set[int] | None = None) -> list[int]:
    exclude_pids = exclude_pids or set()
    killed: list[int] = []
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT pid
            FROM recording_jobs
            WHERE lv = ?
              AND pid IS NOT NULL
              AND status IN ('launched', 'recording')
            """,
            (lv,),
        ).fetchall()
    for row in rows:
        try:
            pid = int(row["pid"] or 0)
        except (TypeError, ValueError):
            continue
        if not pid or pid in exclude_pids or not is_process_running(pid):
            continue
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            killed.append(pid)
        except Exception:
            continue
    if killed:
        killed_at = now_micro()
        with connect() as conn:
            for pid in killed:
                record_recording_event(
                    conn,
                    lv=lv,
                    broadcaster_id="",
                    broadcaster_name="",
                    watch_url="",
                    recorder="SlNicoLiveRec",
                    pid=pid,
                    event_type="process_terminated_by_parent",
                    event_at=killed_at,
                    started_at=None,
                    ended_at=killed_at,
                    duration_us=None,
                    exit_code=None,
                    target_dir="",
                    payload={"reason": "broadcast_ended"},
                )
            conn.commit()
    return killed


def stop_recording_for_broadcast(lv: str, *, reason: str = "manual_stop") -> dict[str, Any]:
    lv = str(lv or "").strip()
    if not lv:
        return {"stopped": False, "reason": "missing_lv", "killed_pids": []}
    stopped_at = now_micro()
    killed: list[int] = []
    rows: list[sqlite3.Row]
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM recording_jobs
            WHERE lv = ?
              AND pid IS NOT NULL
              AND status IN ('launched', 'recording')
            """,
            (lv,),
        ).fetchall()
    for row in rows:
        pid = int(row["pid"] or 0)
        if pid <= 0:
            continue
        if is_process_running(pid):
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=10,
                )
                killed.append(pid)
            except Exception:
                pass
    first_row: sqlite3.Row | None = rows[0] if rows else None
    with connect() as conn:
        for row in rows:
            pid = int(row["pid"] or 0)
            conn.execute(
                """
                UPDATE recording_jobs
                SET status = ?,
                    last_exit_at = ?,
                    last_process_check_at = ?,
                    updated_at = ?,
                    error = NULL
                WHERE lv = ? AND pid = ?
                """,
                    ("stopped", stopped_at, stopped_at, stopped_at, lv, pid),
            )
            record_recording_event(
                conn,
                lv=lv,
                broadcaster_id=str(row["broadcaster_id"] or ""),
                broadcaster_name=str(row["broadcaster_name"] or ""),
                watch_url=str(row["watch_url"] or ""),
                recorder=str(row["recorder"] or "SlNicoLiveRec"),
                pid=pid,
                event_type="manual_stop",
                event_at=stopped_at,
                started_at=str(row["started_at"] or ""),
                ended_at=stopped_at,
                duration_us=None,
                exit_code=None,
                target_dir=str(row["target_dir"] or ""),
                payload={"reason": reason, "killed": pid in killed},
            )
        conn.commit()
    finalize_result: dict[str, Any] | None = None
    if first_row is not None:
        finalize_result = finalize_recording_if_broadcast_ended(
            lv=lv,
            broadcaster_id=str(first_row["broadcaster_id"] or ""),
            broadcaster_name=str(first_row["broadcaster_name"] or ""),
            watch_url=str(first_row["watch_url"] or f"https://live.nicovideo.jp/watch/{lv}"),
            recorder=str(first_row["recorder"] or "SlNicoLiveRec"),
            previous_pid=int(first_row["pid"] or 0),
            exit_code=None,
            ended_at=stopped_at,
            target_dir=str(first_row["target_dir"] or ""),
            source_event="manual_stop_api_check",
        )
    return {
        "stopped": bool(rows),
        "reason": reason,
        "killed_pids": killed,
        "finalize": finalize_result,
    }


def finalize_recording_if_broadcast_ended(
    *,
    lv: str,
    broadcaster_id: str,
    broadcaster_name: str,
    watch_url: str,
    recorder: str,
    previous_pid: int | None,
    exit_code: int | None,
    ended_at: str,
    target_dir: str,
    source_event: str,
) -> dict[str, Any]:
    with connect() as conn:
        current_job = conn.execute(
            "SELECT status FROM recording_jobs WHERE lv = ?",
            (lv,),
        ).fetchone()
    if current_job and str(current_job["status"] or "") == "finalize_skipped":
        return {
            "finalized": False,
            "finalize_queued": False,
            "reason": "html_generation_disabled",
        }

    api_check: dict[str, Any] | None = None
    ended = False
    if is_supported_broadcast_history_provider_id(broadcaster_id):
        api_check = check_live_still_on_air_by_broadcaster_api(lv, broadcaster_id)
    if api_check and api_check.get("checked"):
        with connect() as conn:
            record_recording_event(
                conn,
                lv=lv,
                broadcaster_id=broadcaster_id,
                broadcaster_name=broadcaster_name,
                watch_url=watch_url,
                recorder=recorder,
                pid=previous_pid,
                event_type="broadcast_liveness_checked_api",
                event_at=now_micro(),
                started_at=None,
                ended_at=ended_at,
                duration_us=None,
                exit_code=exit_code,
                target_dir=target_dir,
                payload={**api_check, "source_event": source_event},
            )
            conn.commit()
        if api_check.get("on_air"):
            return {"finalized": False, "reason": "still_on_air", "api_check": api_check}
        ended = True
        with connect() as conn:
            meta = dict(api_check.get("meta") or {})
            if not meta:
                meta = user_history_program_to_broadcast_archive_meta(
                    None,
                    lv,
                    broadcaster_id=broadcaster_id,
                    broadcaster_name=broadcaster_name,
                )
            if broadcaster_id and not meta.get("broadcaster_id"):
                meta["broadcaster_id"] = broadcaster_id
            if broadcaster_name and not meta.get("broadcaster_name"):
                meta["broadcaster_name"] = broadcaster_name
            save_broadcast_archive_meta(conn, meta)
            record_recording_event(
                conn,
                lv=lv,
                broadcaster_id=broadcaster_id,
                broadcaster_name=broadcaster_name,
                watch_url=watch_url,
                recorder=recorder,
                pid=previous_pid,
                event_type="broadcast_meta_saved_after_api_end",
                event_at=now_micro(),
                started_at=None,
                ended_at=ended_at,
                duration_us=None,
                exit_code=exit_code,
                target_dir=target_dir,
                payload={
                    "begin_time": meta.get("begin_time"),
                    "end_time": meta.get("end_time"),
                    "ended": True,
                    "source_event": source_event,
                    "meta_source": api_check.get("source"),
                    "api_reason": api_check.get("reason"),
                    "api_status": api_check.get("status"),
                },
            )
            conn.commit()
    else:
        if str(broadcaster_id or "").strip().isdigit():
            return {"finalized": False, "reason": "api_unchecked", "api_check": api_check}
        with connect() as conn:
            meta = fetch_and_save_broadcast_archive_meta(conn, lv, broadcaster_id=broadcaster_id or None)
            ended = is_broadcast_ended_by_meta(meta)
            record_recording_event(
                conn,
                lv=lv,
                broadcaster_id=broadcaster_id,
                broadcaster_name=broadcaster_name,
                watch_url=watch_url,
                recorder=recorder,
                pid=previous_pid,
                event_type="broadcast_meta_checked_async",
                event_at=now_micro(),
                started_at=None,
                ended_at=ended_at,
                duration_us=None,
                exit_code=exit_code,
                target_dir=target_dir,
                payload={
                    "begin_time": meta.get("begin_time"),
                    "end_time": meta.get("end_time"),
                    "ended": ended,
                    "api_check": api_check,
                    "source_event": source_event,
                },
            )
            conn.commit()
    if not ended:
        return {"finalized": False, "reason": "not_ended", "api_check": api_check}
    if not monitored_broadcaster_html_generation_enabled(broadcaster_id):
        try:
            killed = terminate_recording_processes_for_lv(
                lv,
                exclude_pids={int(previous_pid or 0)},
            )
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            killed = []
            postprocess_log(
                lv,
                "dispatcher",
                "WARN",
                f"残存録画プロセス確認に失敗: {error}",
            )
        skipped_at = now_micro()
        with connect() as conn:
            conn.execute(
                """
                UPDATE recording_jobs
                SET status = 'finalize_skipped', pid = NULL, error = NULL, updated_at = ?
                WHERE lv = ?
                """,
                (skipped_at, lv),
            )
            record_recording_event(
                conn,
                lv=lv,
                broadcaster_id=broadcaster_id,
                broadcaster_name=broadcaster_name,
                watch_url=watch_url,
                recorder=recorder,
                pid=previous_pid,
                event_type="broadcast_ended_finalize_skipped",
                event_at=skipped_at,
                started_at=None,
                ended_at=ended_at,
                duration_us=None,
                exit_code=exit_code,
                target_dir=target_dir,
                payload={
                    "html_generation_enabled": False,
                    "killed_pids": killed,
                    "source_event": source_event,
                },
            )
            conn.commit()
        postprocess_log(
            lv,
            "dispatcher",
            "INFO",
            "HTML生成OFFのため放送終了後処理をスキップ",
            {
                "killed_pids": killed,
                "target_dir": target_dir,
                "source_event": source_event,
            },
        )
        return {
            "finalized": False,
            "finalize_queued": False,
            "reason": "html_generation_disabled",
            "killed_pids": killed,
        }
    with connect() as conn:
        reserved = reserve_finalize_queue_item(
            conn,
            lv=lv,
            broadcaster_id=broadcaster_id,
            target_dir=target_dir,
        )
        if not reserved:
            existing_queue = conn.execute(
                "SELECT status FROM finalize_queue WHERE lv = ?",
                (lv,),
            ).fetchone()
        else:
            existing_queue = None
        conn.commit()
    if not reserved:
        queue_status = str(existing_queue["status"] or "") if existing_queue else "unknown"
        postprocess_log(
            lv,
            "dispatcher",
            "INFO",
            f"終了処理は既にキュー登録済みのため重複起動を抑止 status={queue_status}",
        )
        return {
            "finalized": False,
            "reason": "finalize_already_queued",
            "queue_status": queue_status,
        }
    try:
        killed = terminate_recording_processes_for_lv(lv, exclude_pids={int(previous_pid or 0)})
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        killed = []
        postprocess_log(
            lv,
            "dispatcher",
            "WARN",
            f"残存録画プロセス確認に失敗したが終了処理はキューで続行: {error}",
        )
    with connect() as conn:
        mark_finalize_queue_ready(conn, lv)
        conn.commit()
    postprocess_log(
        lv,
        "dispatcher",
        "INFO",
        "放送終了処理を共通FIFOキューへ登録",
        {
            "killed_pids": killed,
            "target_dir": target_dir,
            "source_event": source_event,
        },
    )
    start_finalize_pipeline_after_recording_end(
        lv=lv,
        broadcaster_id=broadcaster_id,
        target_dir=target_dir,
    )
    with connect() as conn:
        record_recording_event(
            conn,
            lv=lv,
            broadcaster_id=broadcaster_id,
            broadcaster_name=broadcaster_name,
            watch_url=watch_url,
            recorder=recorder,
            pid=previous_pid,
            event_type="broadcast_ended_finalize_queued",
            event_at=now_micro(),
            started_at=None,
            ended_at=ended_at,
            duration_us=None,
            exit_code=exit_code,
            target_dir=target_dir,
            payload={
                "killed_pids": killed,
                "source_event": source_event,
            },
        )
        conn.commit()
    return {"finalized": True, "killed_pids": killed, "finalize_queued": True}


def active_recording_job_map(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    reconcile_recording_jobs_with_processes(conn)
    rows = conn.execute(
        """
        SELECT lv, pid, status, recorder, started_at, target_dir,
               last_process_check_at, process_check_count
        FROM recording_jobs
        WHERE status IN ('launched', 'recording')
        """
    ).fetchall()
    active: dict[str, dict[str, Any]] = {}
    for row in rows:
        pid = int(row["pid"] or 0)
        if pid <= 0 or not is_process_running(pid):
            continue
        active[str(row["lv"])] = dict(row)
    return active


def reconcile_recording_jobs_with_processes(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT *
        FROM recording_jobs
        WHERE status IN ('launched', 'recording')
        """
    ).fetchall()
    fixed: list[dict[str, Any]] = []
    checked_at = now_micro()
    for row in rows:
        pid = int(row["pid"] or 0)
        if pid > 0 and is_process_running(pid):
            continue
        lv = str(row["lv"] or "")
        conn.execute(
            """
            UPDATE recording_jobs
            SET status = ?,
                last_exit_at = ?,
                last_process_check_at = ?,
                updated_at = ?,
                error = ?
            WHERE lv = ?
            """,
            ("exited", checked_at, checked_at, checked_at, f"process_not_running: {pid}", lv),
        )
        record_recording_event(
            conn,
            lv=lv,
            broadcaster_id=str(row["broadcaster_id"] or ""),
            broadcaster_name=str(row["broadcaster_name"] or ""),
            watch_url=str(row["watch_url"] or ""),
            recorder=str(row["recorder"] or "SlNicoLiveRec"),
            pid=pid,
            event_type="process_missing",
            event_at=checked_at,
            started_at=str(row["started_at"] or ""),
            ended_at=checked_at,
            duration_us=None,
            exit_code=None,
            target_dir=str(row["target_dir"] or ""),
            payload={"reason": "recording_job_pid_not_running"},
        )
        fixed.append({**dict(row), "lv": lv, "pid": pid, "status": "exited", "ended_at": checked_at})
    if fixed:
        conn.commit()
    # A GUI restart or a detached recorder can make us miss the normal
    # QProcess-finished callback. PID reconciliation must therefore perform
    # the same end check and queue Step 1 instead of merely writing "exited".
    for row in fixed:
        try:
            finalize = finalize_recording_if_broadcast_ended(
                lv=str(row.get("lv") or ""),
                broadcaster_id=str(row.get("broadcaster_id") or ""),
                broadcaster_name=str(row.get("broadcaster_name") or ""),
                watch_url=str(row.get("watch_url") or ""),
                recorder=str(row.get("recorder") or "SlNicoLiveRec"),
                previous_pid=int(row.get("pid") or 0),
                exit_code=None,
                ended_at=str(row.get("ended_at") or checked_at),
                target_dir=str(row.get("target_dir") or ""),
                source_event="recording_pid_reconciled_missing",
            )
            row["finalize"] = finalize
        except Exception as exc:
            row["finalize"] = {
                "finalized": False,
                "reason": "reconcile_finalize_error",
                "error": f"{type(exc).__name__}: {exc}",
            }
            postprocess_log(
                str(row.get("lv") or ""),
                "dispatcher",
                "ERROR",
                f"PID消失後のStep開始判定に失敗: {type(exc).__name__}: {exc}",
            )
    return fixed


def start_recording_process_with_timing(
    *,
    exe: Path,
    watch_url: str,
    lv: str,
    broadcaster_id: str,
    broadcaster_name: str,
    recorder: str,
    target_dir: Path,
) -> tuple[subprocess.Popen, str]:
    started_at = now_micro()
    process = subprocess.Popen(
        [str(exe), watch_url],
        cwd=str(exe.parent),
        close_fds=True,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )
    return process, started_at


def start_recording_exit_watcher(
    *,
    process: subprocess.Popen,
    lv: str,
    broadcaster_id: str,
    broadcaster_name: str,
    watch_url: str,
    recorder: str,
    started_at: str,
    target_dir: Path | str,
) -> None:
    thread = threading.Thread(
        target=watch_recording_process,
        kwargs={
            "process": process,
            "lv": lv,
            "broadcaster_id": broadcaster_id,
            "broadcaster_name": broadcaster_name,
            "watch_url": watch_url,
            "recorder": recorder,
            "started_at": started_at,
            "target_dir": str(target_dir),
        },
        name=f"recording-watch-{lv}-{process.pid}",
        daemon=True,
    )
    thread.start()


def start_recording_rotation_timer(
    *,
    process: subprocess.Popen,
    lv: str,
    broadcaster_id: str,
    broadcaster_name: str,
    watch_url: str,
    recorder: str,
    started_at: str,
    target_dir: Path | str,
    segment_seconds: int,
) -> None:
    if segment_seconds <= 0:
        return
    thread = threading.Thread(
        target=rotate_recording_process_after_timeout,
        kwargs={
            "process": process,
            "lv": lv,
            "broadcaster_id": broadcaster_id,
            "broadcaster_name": broadcaster_name,
            "watch_url": watch_url,
            "recorder": recorder,
            "started_at": started_at,
            "target_dir": str(target_dir),
            "segment_seconds": segment_seconds,
        },
        name=f"recording-rotate-{lv}-{process.pid}",
        daemon=True,
    )
    thread.start()


def rotate_recording_process_after_timeout(
    *,
    process: subprocess.Popen,
    lv: str,
    broadcaster_id: str,
    broadcaster_name: str,
    watch_url: str,
    recorder: str,
    started_at: str,
    target_dir: str,
    segment_seconds: int,
) -> None:
    deadline = time.monotonic() + max(1, int(segment_seconds))
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        if process.poll() is not None:
            return
        time.sleep(min(5.0, remaining))
    if process.poll() is not None:
        return
    event_at = now_micro()
    should_rotate = False
    with connect() as conn:
        row = conn.execute(
            "SELECT pid, status FROM recording_jobs WHERE lv = ?",
            (lv,),
        ).fetchone()
        should_rotate = bool(
            row
            and int(row["pid"] or 0) == int(process.pid)
            and str(row["status"] or "") in {"launched", "recording"}
        )
        if should_rotate:
            record_recording_event(
                conn,
                lv=lv,
                broadcaster_id=broadcaster_id,
                broadcaster_name=broadcaster_name,
                watch_url=watch_url,
                recorder=recorder,
                pid=process.pid,
                event_type="rotation_requested",
                event_at=event_at,
                started_at=started_at,
                ended_at=None,
                duration_us=None,
                exit_code=None,
                target_dir=target_dir,
                payload={"segment_seconds": segment_seconds},
            )
            conn.commit()
    if not should_rotate:
        return
    try:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
    except Exception as exc:
        with connect() as conn:
            record_recording_event(
                conn,
                lv=lv,
                broadcaster_id=broadcaster_id,
                broadcaster_name=broadcaster_name,
                watch_url=watch_url,
                recorder=recorder,
                pid=process.pid,
                event_type="rotation_terminate_failed",
                event_at=now_micro(),
                started_at=started_at,
                ended_at=None,
                duration_us=None,
                exit_code=None,
                target_dir=target_dir,
                payload={"error": str(exc)},
            )
            conn.commit()


def start_recording_for_broadcast(
    conn: sqlite3.Connection,
    item: dict[str, Any],
    config: Config | None = None,
    *,
    recorder: str = "SlNicoLiveRec",
    force_restart: bool = False,
) -> dict[str, Any]:
    config = config or load_config()
    lv = str(item.get("lv") or "").strip()
    if not lv:
        return {"started": False, "reason": "missing_lv"}
    watch_url = str(item.get("watch_url") or "").strip() or f"https://live.nicovideo.jp/watch/{lv}"
    broadcaster_id = str(item.get("broadcaster_id") or "").strip()
    broadcaster_name = str(item.get("broadcaster_name") or "").strip()
    current_time = now()
    target_dir = ensure_broadcast_target_dirs(config, lv, broadcaster_id=broadcaster_id or None)

    queue_row = conn.execute(
        "SELECT status FROM finalize_queue WHERE lv = ?",
        (lv,),
    ).fetchone()
    if queue_row and str(queue_row["status"] or "") in FINALIZE_QUEUE_BLOCKING_STATES:
        return {
            "started": False,
            "reason": "finalize_in_progress",
            "queue_status": str(queue_row["status"] or ""),
        }

    existing = conn.execute(
        "SELECT lv, pid, status FROM recording_jobs WHERE lv = ?",
        (lv,),
    ).fetchone()
    if existing:
        pid = int(existing["pid"] or 0)
        existing_status = str(existing["status"] or "")
        if existing_status in {
            "finalize_queued",
            "finalizing",
            "finalized",
            "finalize_failed",
            "finalize_skipped",
        }:
            return {"started": False, "reason": "finalize_in_progress", "status": existing_status}
        if existing_status in {"launched", "recording"}:
            if is_process_running(pid):
                return {"started": False, "reason": "already_running", "pid": pid}
            if not force_restart:
                conn.execute(
                    "UPDATE recording_jobs SET status = ?, updated_at = ? WHERE lv = ?",
                    ("exited", current_time, lv),
                )
                conn.commit()
                finalize = finalize_recording_if_broadcast_ended(
                    lv=lv,
                    broadcaster_id=broadcaster_id,
                    broadcaster_name=broadcaster_name,
                    watch_url=watch_url,
                    recorder=recorder,
                    previous_pid=pid,
                    exit_code=None,
                    ended_at=now_micro(),
                    target_dir=str(target_dir),
                    source_event="auto_recording_dead_pid",
                )
                return {
                    "started": False,
                    "reason": "process_exit_finalized" if finalize.get("finalize_queued") else "process_exit_pending",
                    "pid": pid,
                    "finalize": finalize,
                }
        if not force_restart:
            conn.execute(
                "UPDATE recording_jobs SET status = ?, updated_at = ? WHERE lv = ?",
                ("exited", current_time, lv),
            )

    exe = Path(config.slnico_live_rec_exe)
    if not exe.exists():
        conn.execute(
            """
            INSERT INTO recording_jobs
                (lv, broadcaster_id, broadcaster_name, watch_url, recorder, pid, status, target_dir, started_at, updated_at, error)
            VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)
            ON CONFLICT(lv) DO UPDATE SET
                broadcaster_id = excluded.broadcaster_id,
                broadcaster_name = excluded.broadcaster_name,
                watch_url = excluded.watch_url,
                recorder = excluded.recorder,
                pid = NULL,
                status = excluded.status,
                target_dir = excluded.target_dir,
                updated_at = excluded.updated_at,
                error = excluded.error
            """,
            (
                lv,
                broadcaster_id,
                broadcaster_name,
                watch_url,
                recorder,
                "error",
                str(target_dir),
                current_time,
                current_time,
                f"recorder_not_found: {exe}",
            ),
        )
        return {"started": False, "reason": "recorder_not_found", "path": str(exe)}

    try:
        process, started_at = start_recording_process_with_timing(
            exe=exe,
            watch_url=watch_url,
            lv=lv,
            broadcaster_id=broadcaster_id,
            broadcaster_name=broadcaster_name,
            recorder=recorder,
            target_dir=target_dir,
        )
        try:
            meta = fetch_broadcast_page_meta(lv, broadcaster_id=broadcaster_id or None)
            if broadcaster_id and not meta.get("broadcaster_id"):
                meta["broadcaster_id"] = broadcaster_id
            if broadcaster_name and not meta.get("broadcaster_name"):
                meta["broadcaster_name"] = broadcaster_name
            save_broadcast_archive_meta(conn, meta)
        except Exception as meta_exc:
            record_recording_event(
                conn,
                lv=lv,
                broadcaster_id=broadcaster_id,
                broadcaster_name=broadcaster_name,
                watch_url=watch_url,
                recorder=recorder,
                pid=process.pid,
                event_type="meta_fetch_failed",
                event_at=now_micro(),
                started_at=started_at,
                ended_at=None,
                duration_us=None,
                exit_code=None,
                target_dir=str(target_dir),
                payload={"error": str(meta_exc)},
            )
    except Exception as exc:
        conn.execute(
            """
            INSERT INTO recording_jobs
                (lv, broadcaster_id, broadcaster_name, watch_url, recorder, pid, status, target_dir, started_at, updated_at, error)
            VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)
            ON CONFLICT(lv) DO UPDATE SET
                broadcaster_id = excluded.broadcaster_id,
                broadcaster_name = excluded.broadcaster_name,
                watch_url = excluded.watch_url,
                recorder = excluded.recorder,
                pid = NULL,
                status = excluded.status,
                target_dir = excluded.target_dir,
                updated_at = excluded.updated_at,
                error = excluded.error
            """,
            (
                lv,
                broadcaster_id,
                broadcaster_name,
                watch_url,
                recorder,
                "error",
                str(target_dir),
                current_time,
                current_time,
                str(exc),
            ),
        )
        return {"started": False, "reason": "launch_failed", "error": str(exc)}

    conn.execute(
        """
        INSERT INTO recording_jobs
            (lv, broadcaster_id, broadcaster_name, watch_url, recorder, pid, status,
             target_dir, started_at, last_process_check_at, process_check_count, updated_at, error)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
        ON CONFLICT(lv) DO UPDATE SET
            broadcaster_id = excluded.broadcaster_id,
            broadcaster_name = excluded.broadcaster_name,
            watch_url = excluded.watch_url,
            recorder = excluded.recorder,
            pid = excluded.pid,
            status = excluded.status,
            target_dir = excluded.target_dir,
            last_process_check_at = excluded.last_process_check_at,
            process_check_count = excluded.process_check_count,
            updated_at = excluded.updated_at,
            error = NULL
        """,
        (
            lv,
            broadcaster_id,
            broadcaster_name,
            watch_url,
            recorder,
            process.pid,
            "launched",
            str(target_dir),
            started_at,
            started_at,
            0,
            current_time,
        ),
    )
    record_recording_event(
        conn,
        lv=lv,
        broadcaster_id=broadcaster_id,
        broadcaster_name=broadcaster_name,
        watch_url=watch_url,
        recorder=recorder,
        pid=process.pid,
        event_type="started",
        event_at=started_at,
        started_at=started_at,
        ended_at=None,
        duration_us=None,
        exit_code=None,
        target_dir=str(target_dir),
        payload={"exe": str(exe)},
    )
    conn.commit()
    start_recording_exit_watcher(
        process=process,
        lv=lv,
        broadcaster_id=broadcaster_id,
        broadcaster_name=broadcaster_name,
        watch_url=watch_url,
        recorder=recorder,
        started_at=started_at,
        target_dir=target_dir,
    )
    start_recording_rotation_timer(
        process=process,
        lv=lv,
        broadcaster_id=broadcaster_id,
        broadcaster_name=broadcaster_name,
        watch_url=watch_url,
        recorder=recorder,
        started_at=started_at,
        target_dir=target_dir,
        segment_seconds=int(config.recording_segment_seconds or 0),
    )
    return {"started": True, "pid": process.pid, "watch_url": watch_url, "path": str(exe), "target_dir": str(target_dir)}


def start_recordings_for_monitored_broadcasts(
    conn: sqlite3.Connection,
    items: list[dict[str, Any]],
    config: Config | None = None,
) -> list[dict[str, Any]]:
    monitored = enabled_monitored_broadcaster_map(conn)
    if not monitored:
        return []
    results: list[dict[str, Any]] = []
    for item in items:
        broadcaster_id = str(item.get("broadcaster_id") or "").strip()
        if not broadcaster_id or broadcaster_id not in monitored:
            continue
        merged = dict(item)
        if not merged.get("broadcaster_name"):
            merged["broadcaster_name"] = monitored[broadcaster_id].get("broadcaster_name") or ""
        result = start_recording_for_broadcast(conn, merged, config)
        result["lv"] = merged.get("lv")
        result["broadcaster_id"] = broadcaster_id
        result["broadcaster_name"] = merged.get("broadcaster_name")
        results.append(result)
    conn.commit()
    return results


def start_recordings_for_monitored_broadcaster_api(
    conn: sqlite3.Connection,
    config: Config | None = None,
) -> list[dict[str, Any]]:
    monitored = enabled_monitored_broadcaster_map(conn)
    if not monitored:
        return []
    results: list[dict[str, Any]] = []
    for broadcaster_id, broadcaster in monitored.items():
        broadcaster_key = str(broadcaster_id or "").strip()
        if broadcaster_key.isdigit():
            fetch_lives = fetch_on_air_user_live_programs
        elif re.fullmatch(r"ch\d+", broadcaster_key, flags=re.IGNORECASE):
            fetch_lives = lambda value: fetch_on_air_channel_live_programs(value, config)
        else:
            continue
        try:
            lives = fetch_lives(broadcaster_key)
        except Exception as exc:
            results.append(
                {
                    "started": False,
                    "reason": "onair_api_failed",
                    "broadcaster_id": broadcaster_id,
                    "broadcaster_name": broadcaster.get("broadcaster_name") or "",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            continue
        for live in lives:
            item = dict(live)
            item["broadcaster_id"] = str(item.get("broadcaster_id") or broadcaster_key)
            item["broadcaster_name"] = str(item.get("broadcaster_name") or broadcaster.get("broadcaster_name") or "")
            persist_broadcasts(conn, [item])
            result = start_recording_for_broadcast(conn, item, config)
            result["lv"] = item.get("lv")
            result["broadcaster_id"] = item.get("broadcaster_id")
            result["broadcaster_name"] = item.get("broadcaster_name")
            result["watch_url"] = item.get("watch_url")
            result["title"] = item.get("title")
            result["text"] = item.get("text")
            result["watch_count"] = item.get("watch_count")
            result["comment_count"] = item.get("comment_count")
            result["source"] = "monitored_broadcaster_api"
            results.append(result)
    conn.commit()
    return results


def build_recording_silent_gap_plan(
    conn: sqlite3.Connection,
    lv: str,
    *,
    storage_root: Path | str | None = None,
    timeline_mode: str = "live",
    segment_paths: list[Path | str] | None = None,
) -> list[dict[str, Any]]:
    """Return actual missing-media spans between adjacent recording segments."""
    timeline = build_recording_segment_timeline_plan(
        storage_root or slnico_storage_root(),
        lv=lv,
        conn=conn,
        timeline_mode=timeline_mode,
        segment_paths=segment_paths,
    )
    return [
        {
            "lv": lv,
            "gap_start": str(gap.get("gap_start") or gap.get("gap_start_iso") or ""),
            "gap_end": str(gap.get("gap_end") or gap.get("gap_end_iso") or ""),
            "duration_us": int(round(float(gap.get("duration_seconds") or 0.0) * 1_000_000)),
            "duration_seconds": float(gap.get("duration_seconds") or 0.0),
            "fill": str(gap.get("fill_type") or "black_silent_video"),
            "reason": str(gap.get("reason") or "segment_clock_discontinuity"),
            "previous_path": str(gap.get("previous_path") or ""),
            "next_path": str(gap.get("next_path") or ""),
        }
        for gap in timeline.get("gaps") or []
        if float(gap.get("duration_seconds") or 0.0) > 0.0
        and str(gap.get("reason") or "") == "segment_media_clock_gap"
    ]


def parse_iso_unix_seconds(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp())
    except Exception:
        return None


def extract_unix_time_field(html_content: str, field_names: list[str]) -> int | None:
    text = html_content or ""
    unescaped = unescape(text)
    for source in (text, unescaped):
        for field_name in field_names:
            patterns = [
                rf'{re.escape(field_name)}&quot;\s*:\s*(\d+)',
                rf'"{re.escape(field_name)}"\s*:\s*(\d+)',
                rf"'{re.escape(field_name)}'\s*:\s*(\d+)",
            ]
            for pattern in patterns:
                match = re.search(pattern, source)
                if match:
                    try:
                        return int(match.group(1))
                    except ValueError:
                        pass
    return None


def extract_string_field(html_content: str, field_names: list[str]) -> str:
    text = html_content or ""
    unescaped = unescape(text)
    for source in (text, unescaped):
        for field_name in field_names:
            patterns = [
                rf'{re.escape(field_name)}&quot;\s*:\s*&quot;([^&]+)&quot;',
                rf'"{re.escape(field_name)}"\s*:\s*"([^"]*)"',
            ]
            for pattern in patterns:
                match = re.search(pattern, source)
                if match:
                    return unescape(match.group(1)).strip()
    return ""


def extract_supplier_name(html_content: str) -> str:
    text = html_content or ""
    for source in (text, unescape(text)):
        match = re.search(
            r'"supplier"\s*:\s*\{.*?"name"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)"',
            source,
            flags=re.DOTALL,
        )
        if not match:
            continue
        try:
            return str(json.loads(f'"{match.group(1)}"')).strip()
        except (TypeError, ValueError, json.JSONDecodeError):
            return unescape(match.group(1)).strip()
    return ""


def fetch_broadcast_page_meta(lv: str, *, save_html: bool = True, broadcaster_id: str | None = None) -> dict[str, Any]:
    lv = str(lv).strip()
    if not re.fullmatch(r"lv\d+", lv):
        raise ValueError(f"invalid lv: {lv}")
    broadcaster_id_hint = str(broadcaster_id or "").strip()
    watch_url = f"https://live.nicovideo.jp/watch/{lv}"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
        )
    }
    response = requests.get(watch_url, headers=headers, timeout=30)
    response.raise_for_status()
    html_content = response.text

    begin_time = extract_unix_time_field(html_content, ["beginTime", "begin_time"])
    open_time = extract_unix_time_field(html_content, ["openTime", "open_time"])
    start_time = extract_unix_time_field(html_content, ["startTime", "start_time"])
    end_time = extract_unix_time_field(html_content, ["endTime", "end_time"])
    title = extract_string_field(html_content, ["title", "programTitle", "liveTitle"])
    broadcaster_name = extract_string_field(
        html_content,
        ["supplierName", "broadcaster", "ownerName", "nickname"],
    ) or extract_supplier_name(html_content)
    broadcaster_id = extract_string_field(
        html_content,
        ["programProviderId", "supplierId", "ownerId", "userId"],
    ) or broadcaster_id_hint
    try:
        with connect() as conn:
            existing = conn.execute(
                "SELECT title, broadcaster_id, broadcaster_name FROM broadcasts WHERE lv = ?",
                (lv,),
            ).fetchone()
            if existing:
                title = title or str(existing["title"] or "")
                broadcaster_id = broadcaster_id or str(existing["broadcaster_id"] or "")
                broadcaster_name = broadcaster_name or str(existing["broadcaster_name"] or "")
    except Exception:
        pass
    html_path: Path | None = None
    if save_html:
        config = load_config()
        target_dir = broadcast_target_dir(lv, config, broadcaster_id=broadcaster_id or None)
        target_dir.mkdir(parents=True, exist_ok=True)
        html_path = target_dir / f"{lv}.html"
        html_path.write_text(html_content, encoding="utf-8")
    meta = {
        "lv": lv,
        "watch_url": watch_url,
        "title": title,
        "broadcaster_id": broadcaster_id,
        "broadcaster_name": broadcaster_name,
        "begin_time": begin_time,
        "open_time": open_time,
        "start_time": start_time,
        "end_time": end_time,
        "server_time": None,
        "time_diff_seconds": None,
        "fetched_at": now_micro(),
        "html_path": str(html_path) if html_path is not None else "",
    }
    return meta


def save_broadcast_archive_meta(conn: sqlite3.Connection, meta: dict[str, Any]) -> None:
    begin_time = meta.get("begin_time")
    server_time = meta.get("server_time")
    time_diff_seconds = meta.get("time_diff_seconds")
    if time_diff_seconds is None and begin_time is not None and server_time is not None:
        try:
            time_diff_seconds = int(server_time) - int(begin_time)
        except (TypeError, ValueError):
            time_diff_seconds = None
    conn.execute(
        """
        INSERT INTO broadcast_archive_meta
            (lv, watch_url, title, broadcaster_id, broadcaster_name,
             begin_time, open_time, start_time, end_time, server_time,
             time_diff_seconds, fetched_at, html_path, raw_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(lv) DO UPDATE SET
            watch_url = excluded.watch_url,
            title = CASE WHEN excluded.title IS NOT NULL AND excluded.title != '' THEN excluded.title ELSE broadcast_archive_meta.title END,
            broadcaster_id = CASE WHEN excluded.broadcaster_id IS NOT NULL AND excluded.broadcaster_id != '' THEN excluded.broadcaster_id ELSE broadcast_archive_meta.broadcaster_id END,
            broadcaster_name = CASE WHEN excluded.broadcaster_name IS NOT NULL AND excluded.broadcaster_name != '' THEN excluded.broadcaster_name ELSE broadcast_archive_meta.broadcaster_name END,
            begin_time = COALESCE(excluded.begin_time, broadcast_archive_meta.begin_time),
            open_time = COALESCE(excluded.open_time, broadcast_archive_meta.open_time),
            start_time = COALESCE(excluded.start_time, broadcast_archive_meta.start_time),
            end_time = COALESCE(excluded.end_time, broadcast_archive_meta.end_time),
            server_time = COALESCE(excluded.server_time, broadcast_archive_meta.server_time),
            time_diff_seconds = COALESCE(excluded.time_diff_seconds, broadcast_archive_meta.time_diff_seconds),
            fetched_at = excluded.fetched_at,
            html_path = CASE WHEN excluded.html_path IS NOT NULL AND excluded.html_path != '' THEN excluded.html_path ELSE broadcast_archive_meta.html_path END,
            raw_json = excluded.raw_json
        """,
        (
            meta.get("lv"),
            meta.get("watch_url"),
            meta.get("title"),
            meta.get("broadcaster_id"),
            meta.get("broadcaster_name"),
            meta.get("begin_time"),
            meta.get("open_time"),
            meta.get("start_time"),
            meta.get("end_time"),
            meta.get("server_time"),
            time_diff_seconds,
            meta.get("fetched_at") or now_micro(),
            meta.get("html_path"),
            json.dumps(meta, ensure_ascii=False),
        ),
    )


def is_broadcast_ended_by_meta(meta: dict[str, Any]) -> bool:
    end_time = meta.get("end_time")
    if not end_time:
        return False
    try:
        return int(datetime.now().timestamp()) >= int(end_time)
    except (TypeError, ValueError):
        return False


def fetch_and_save_broadcast_archive_meta(
    conn: sqlite3.Connection,
    lv: str,
    *,
    broadcaster_id: str | None = None,
) -> dict[str, Any]:
    meta = fetch_broadcast_page_meta(lv, broadcaster_id=broadcaster_id)
    save_broadcast_archive_meta(conn, meta)
    return meta


def _api_time_seconds(value: Any) -> int | None:
    if isinstance(value, dict):
        value = value.get("seconds")
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def fetch_user_broadcast_history_programs(
    provider_id: str | int,
    *,
    provider_type: str = "user",
    offset: int = 0,
    limit: int = 20,
    retries: int = 3,
    retry_delay_seconds: float = 0.8,
) -> list[dict[str, Any]]:
    url = "https://live.nicovideo.jp/front/api/v2/user-broadcast-history"
    provider_type = str(provider_type or "user").strip().lower()
    params = {
        "providerId": str(provider_id),
        "providerType": provider_type,
        "isIncludeNonPublic": "false",
        "offset": max(0, int(offset or 0)),
        "limit": max(1, int(limit or 20)),
        "withTotalCount": "true",
    }
    headers = {
        "X-Frontend-Id": "9",
        "X-Frontend-Version": "0",
        "User-Agent": "Mozilla/5.0",
        "Accept-Language": "ja-JP,ja;q=0.9",
    }
    last_error: Exception | None = None
    attempts = max(1, int(retries or 1))
    for attempt in range(1, attempts + 1):
        try:
            response = requests.get(url, params=params, headers=headers, timeout=10)
            response.raise_for_status()
            payload = response.json()
            data = payload.get("data")
            if not isinstance(data, dict) or "programsList" not in data:
                raise RuntimeError("user-broadcast-history response missing data.programsList")
            programs = data.get("programsList") or []
            if not isinstance(programs, list):
                raise RuntimeError("user-broadcast-history data.programsList is not a list")
            return [program for program in programs if isinstance(program, dict)]
        except Exception as exc:
            last_error = exc
            if attempt >= attempts:
                raise RuntimeError(
                    f"user-broadcast-history providerType={provider_type} providerId={provider_id} "
                    f"offset={offset} "
                    f"failed after {attempts} attempts: "
                    f"{type(exc).__name__}: {exc}"
                ) from exc
            time.sleep(max(0.0, float(retry_delay_seconds or 0.0)))
    raise RuntimeError(f"user-broadcast-history providerType={provider_type} providerId={provider_id} failed: {last_error}")


def is_timeshift_program_available(
    program: dict[str, Any],
    *,
    at_timestamp: int | float | None = None,
) -> bool:
    program_info = program.get("program")
    schedule = program_info.get("schedule") if isinstance(program_info, dict) else None
    if not isinstance(schedule, dict):
        return False
    if str(schedule.get("status") or "").strip().upper() != "ENDED":
        return False
    setting = program.get("timeshiftSetting")
    if not isinstance(setting, dict):
        return False
    if str(setting.get("status") or "").strip().upper() != "OPENED":
        return False
    end_time = _api_time_seconds(setting.get("endTime"))
    if end_time is None:
        return True
    current = int(datetime.now().timestamp() if at_timestamp is None else at_timestamp)
    return end_time > current


def fetch_timeshift_available_programs_for_broadcaster(
    broadcaster_id: str | int,
    *,
    page_size: int = 100,
    max_pages: int = 50,
    at_timestamp: int | float | None = None,
) -> list[dict[str, Any]]:
    broadcaster_id = str(broadcaster_id or "").strip()
    if re.fullmatch(r"ch\d+", broadcaster_id, flags=re.IGNORECASE):
        provider_type = "channel"
    elif broadcaster_id.isdigit():
        provider_type = "user"
    else:
        raise ValueError(f"unsupported broadcaster id: {broadcaster_id}")
    page_size = max(1, min(100, int(page_size or 100)))
    max_pages = max(1, int(max_pages or 1))
    available: list[dict[str, Any]] = []
    seen: set[str] = set()
    for page_index in range(max_pages):
        offset = page_index * page_size
        programs = fetch_user_broadcast_history_programs(
            broadcaster_id,
            provider_type=provider_type,
            offset=offset,
            limit=page_size,
        )
        if not programs:
            break
        page_has_timeshift_setting = False
        for program in programs:
            if isinstance(program.get("timeshiftSetting"), dict):
                page_has_timeshift_setting = True
            if not is_timeshift_program_available(program, at_timestamp=at_timestamp):
                continue
            lv = str((program.get("id") or {}).get("value") or "").strip().lower()
            if not re.fullmatch(r"lv\d+", lv) or lv in seen:
                continue
            seen.add(lv)
            available.append(program)
        postprocess_log(
            None,
            "timeshift_api",
            "DEBUG",
            (
                f"タイムシフト履歴API: broadcaster={broadcaster_id} "
                f"offset={offset} fetched={len(programs)} available_total={len(available)}"
            ),
            {
                "broadcaster_id": broadcaster_id,
                "provider_type": provider_type,
                "offset": offset,
                "fetched": len(programs),
                "available_total": len(available),
            },
        )
        if len(programs) < page_size:
            break
        # The endpoint is newest-first. Once an entire page is outside the
        # timeshift window, older pages cannot become watchable again.
        if not page_has_timeshift_setting:
            break
    return available


def user_history_program_to_broadcast_archive_meta(
    program: dict[str, Any] | None,
    lv: str,
    *,
    broadcaster_id: str = "",
    broadcaster_name: str = "",
) -> dict[str, Any]:
    lv = str(lv or "").strip()
    program = program or {}
    program_info = program.get("program") if isinstance(program.get("program"), dict) else {}
    schedule = program_info.get("schedule") if isinstance(program_info.get("schedule"), dict) else {}
    provider = program.get("programProvider") if isinstance(program.get("programProvider"), dict) else {}
    provider_id = provider.get("programProviderId") if isinstance(provider.get("programProviderId"), dict) else {}
    social_group = program.get("socialGroup") if isinstance(program.get("socialGroup"), dict) else {}
    provider_kind = str(program_info.get("provider") or social_group.get("type") or "").strip().upper()
    if provider_kind == "CHANNEL":
        api_broadcaster_id = str(social_group.get("socialGroupId") or "").strip() or str(provider_id.get("value") or "").strip()
        api_broadcaster_name = str(social_group.get("name") or "").strip() or str(provider.get("name") or "").strip()
    else:
        api_broadcaster_id = str(provider_id.get("value") or "").strip() or str(social_group.get("socialGroupId") or "").strip()
        api_broadcaster_name = str(provider.get("name") or "").strip() or str(social_group.get("name") or "").strip()
    title = str(program_info.get("title") or "").strip()
    watch_url = f"https://live.nicovideo.jp/watch/{lv}"
    raw_meta = {
        "source": "user-broadcast-history",
        "program": program,
    }
    return {
        "lv": lv,
        "watch_url": watch_url,
        "title": title,
        "broadcaster_id": api_broadcaster_id or str(broadcaster_id or "").strip(),
        "broadcaster_name": api_broadcaster_name or str(broadcaster_name or "").strip(),
        "begin_time": _api_time_seconds(schedule.get("beginTime")),
        "open_time": _api_time_seconds(schedule.get("openTime")),
        "start_time": _api_time_seconds(schedule.get("beginTime")) or _api_time_seconds(schedule.get("openTime")),
        "end_time": _api_time_seconds(schedule.get("endTime")) or _api_time_seconds(schedule.get("scheduledEndTime")),
        "server_time": None,
        "time_diff_seconds": None,
        "fetched_at": now_micro(),
        "html_path": "",
        "raw_json": json.dumps(raw_meta, ensure_ascii=False),
    }


def timeshift_program_to_download_item(
    program: dict[str, Any],
    *,
    broadcaster_id: str = "",
) -> dict[str, Any]:
    lv = str((program.get("id") or {}).get("value") or "").strip().lower()
    if not re.fullmatch(r"lv\d+", lv):
        raise ValueError(f"timeshift program has invalid lv: {lv}")
    meta = user_history_program_to_broadcast_archive_meta(
        program,
        lv,
        broadcaster_id=str(broadcaster_id or "").strip(),
    )
    setting = program.get("timeshiftSetting") if isinstance(program.get("timeshiftSetting"), dict) else {}
    return {
        **meta,
        "source": "user-broadcast-history",
        "timeshift_available": is_timeshift_program_available(program),
        "timeshift_status": str(setting.get("status") or ""),
        "timeshift_end_time": _api_time_seconds(setting.get("endTime")),
    }


def sort_timeshift_download_items_oldest_first(
    items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    def oldest_key(item: dict[str, Any]) -> tuple[Any, ...]:
        broadcast_time: int | None = None
        for field in ("start_time", "begin_time", "open_time", "end_time"):
            value = _api_time_seconds(item.get(field))
            if value is not None and value > 0:
                broadcast_time = value
                break
        lv = str(item.get("lv") or "").strip().lower()
        match = re.fullmatch(r"lv(\d+)", lv)
        lv_number = int(match.group(1)) if match else sys.maxsize
        return (
            broadcast_time is None,
            int(broadcast_time or 0),
            lv_number,
            int(item.get("timeshift_end_time") or 0),
            lv,
        )

    return sorted(items, key=oldest_key)


def sort_broadcast_lvs_oldest_first(lvs: list[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for value in lvs:
        lv = str(value or "").strip().lower()
        if not re.fullmatch(r"lv\d+", lv) or lv in seen:
            continue
        seen.add(lv)
        normalized.append(lv)
    if not normalized:
        return []

    placeholders = ", ".join("?" for _ in normalized)
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT lv, start_time, begin_time, open_time, end_time
            FROM broadcast_archive_meta
            WHERE lv IN ({placeholders})
            """,
            normalized,
        ).fetchall()
    broadcast_times: dict[str, int] = {}
    for row in rows:
        for field in ("start_time", "begin_time", "open_time", "end_time"):
            value = _api_time_seconds(row[field])
            if value is not None and value > 0:
                broadcast_times[str(row["lv"])] = value
                break

    original_positions = {lv: index for index, lv in enumerate(normalized)}

    def oldest_key(lv: str) -> tuple[Any, ...]:
        broadcast_time = broadcast_times.get(lv)
        match = re.fullmatch(r"lv(\d+)", lv)
        lv_number = int(match.group(1)) if match else sys.maxsize
        return (
            broadcast_time is None,
            int(broadcast_time or 0),
            lv_number,
            original_positions[lv],
        )

    return sorted(normalized, key=oldest_key)


def resolve_timeshift_input_urls(input_values: list[str]) -> list[dict[str, Any]]:
    resolved: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_value in input_values:
        value = str(raw_value or "").strip()
        if not value:
            continue
        lv = extract_nicolive_id(value)
        if lv and re.fullmatch(r"lv\d+", lv, flags=re.IGNORECASE):
            lv = lv.lower()
            if lv in seen:
                continue
            page_meta = fetch_broadcast_page_meta(lv, save_html=False)
            broadcaster_id = str(page_meta.get("broadcaster_id") or "").strip()
            if not broadcaster_id:
                raise RuntimeError(f"{lv}: 配信者IDを取得できませんでした")
            provider_type = "channel" if re.fullmatch(r"ch\d+", broadcaster_id, flags=re.IGNORECASE) else "user"
            programs = fetch_user_broadcast_history_programs(
                broadcaster_id,
                provider_type=provider_type,
                offset=0,
                limit=100,
            )
            program = next(
                (
                    row
                    for row in programs
                    if str((row.get("id") or {}).get("value") or "").strip().lower() == lv
                ),
                None,
            )
            if program is not None:
                if not is_timeshift_program_available(program):
                    raise RuntimeError(f"{lv}: タイムシフト視聴可能ではありません")
                item = timeshift_program_to_download_item(program, broadcaster_id=broadcaster_id)
            else:
                item = {
                    **page_meta,
                    "source": "direct-watch-url",
                    "timeshift_available": True,
                    "timeshift_status": "UNKNOWN",
                    "timeshift_end_time": None,
                }
            seen.add(lv)
            resolved.append(item)
            continue

        broadcaster_id = extract_user_id(value)
        if not broadcaster_id:
            raise ValueError(f"未対応のタイムシフトURLです: {value}")
        programs = fetch_timeshift_available_programs_for_broadcaster(broadcaster_id)
        for program in programs:
            item = timeshift_program_to_download_item(program, broadcaster_id=broadcaster_id)
            item_lv = str(item.get("lv") or "").strip().lower()
            if not item_lv or item_lv in seen:
                continue
            seen.add(item_lv)
            resolved.append(item)
    return resolved


def backfill_broadcast_archive_meta(limit: int = 20) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT b.lv
            FROM broadcasts b
            LEFT JOIN broadcast_archive_meta m ON m.lv = b.lv
            WHERE m.lv IS NULL
            ORDER BY b.elapsed_minutes DESC, b.last_seen_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        for row in rows:
            lv = str(row["lv"])
            try:
                meta = fetch_and_save_broadcast_archive_meta(conn, lv)
                results.append({"lv": lv, "ok": True, "begin_time": meta.get("begin_time")})
            except Exception as exc:
                results.append({"lv": lv, "ok": False, "error": str(exc)})
        conn.commit()
    return results


def slnico_storage_root(config: Config | None = None) -> Path:
    config_path = DEFAULT_SLNICO_CONFIG
    if config is None:
        try:
            config = load_config()
        except Exception:
            config = None
    if config is not None:
        configured_exe = Path(str(config.slnico_live_rec_exe or ""))
        configured_path = configured_exe.parent / "SlNicoLiveRec_config.json"
        if configured_path.is_file():
            config_path = configured_path
    default_root = config_path.parent / "rec_file"
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8-sig"))
        location = str(raw.get("StorageLocation") or "rec_file\\")
        path = Path(location)
        if not path.is_absolute():
            path = config_path.parent / path
        return path
    except Exception:
        return default_root


SLNICO_RECOMMENDED_SETTINGS: dict[str, Any] = {
    "PurgeCredentials": False,
    "Login": 2,
    "ConvertFormat": True,
    "DeleteOriginal": True,
    "ToTrash": True,
    "ConvertOptions": "-c:v copy -c:a copy",
    "ChangeFilenameFormat": True,
    "FilenameFormat": "{id}_{year}_{month}{day}_{hour}{minute}{second}_{title}",
    "ChangeFolderFormat": True,
    "FolderFormat": "{supplier_id}_{author}",
    "TitleBarFormat": "{author} - SlNicoLiveRec",
    "ReconnectionAttempt": False,
    "RetryInterval": 10,
    "RetryLimit": 0,
    "WaitUntilBegin": False,
    "WaitUntilBeginSecond": 60,
    "CloseWindowOnExit": True,
    "DebugMode": False,
}


def apply_recommended_slnico_settings(exe_path: str | Path) -> Path:
    """Apply recorder defaults without replacing credentials or unknown settings."""
    exe = Path(str(exe_path or "").strip()).expanduser()
    if not exe.is_file():
        raise FileNotFoundError(f"SlNicoLiveRec.exeが見つかりません: {exe}")
    config_path = exe.parent / "SlNicoLiveRec_config.json"
    if not config_path.is_file():
        raise FileNotFoundError(
            f"設定ファイルが見つかりません。SlNicoLiveRecを一度起動して終了してください: {config_path}"
        )
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"SlNicoLiveRec設定の読込に失敗しました: {config_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise RuntimeError(f"SlNicoLiveRec設定の形式が不正です: {config_path}")
    raw.update(SLNICO_RECOMMENDED_SETTINGS)
    temporary_path = config_path.with_suffix(config_path.suffix + ".tmp")
    try:
        temporary_path.write_text(
            json.dumps(raw, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary_path.replace(config_path)
    except OSError as exc:
        with contextlib.suppress(OSError):
            temporary_path.unlink()
        raise RuntimeError(f"SlNicoLiveRec設定の保存に失敗しました: {config_path}: {exc}") from exc
    return config_path


def find_recording_segment_files(lv: str, *, storage_root: Path | None = None) -> list[Path]:
    storage_root = storage_root or slnico_storage_root()
    if not storage_root.exists():
        return []
    patterns = ("*.ts", "*.mp4", "*.mkv", "*.webm", "*.flv")
    files: list[Path] = []
    for pattern in patterns:
        for path in storage_root.rglob(pattern):
            if lv in path.name:
                files.append(path)
    return sorted(set(files), key=lambda p: (p.stat().st_mtime if p.exists() else 0, str(p)))


def select_preferred_timeshift_video(
    paths: list[Path | str],
    *,
    broadcaster_id: str = "",
) -> Path | None:
    candidates: list[dict[str, Any]] = []
    for raw_path in paths:
        path = Path(raw_path)
        try:
            if not path.is_file() or path.stat().st_size <= 0:
                continue
            duration = float(probe_media_duration_seconds(path))
            if duration <= 0:
                continue
            stat = path.stat()
        except Exception:
            continue
        candidates.append(
            {
                "duration": duration,
                "format_priority": {
                    ".mp4": 4,
                    ".mkv": 3,
                    ".webm": 2,
                    ".ts": 1,
                    ".flv": 0,
                }.get(path.suffix.lower(), 0),
                "size": int(stat.st_size),
                "mtime": float(stat.st_mtime),
                "path": path,
            }
        )
    if not candidates:
        return None
    broadcaster_id = str(broadcaster_id or "").strip().casefold()
    if broadcaster_id:
        linked = [
            row
            for row in candidates
            if row["path"].parent.name.casefold().startswith(f"{broadcaster_id}_")
        ]
        if linked:
            candidates = linked
    longest = max(float(row["duration"]) for row in candidates)
    tolerance = max(2.0, longest * 0.005)
    near_complete = [
        row for row in candidates if float(row["duration"]) >= longest - tolerance
    ]
    near_complete.sort(
        key=lambda row: (
            int(row["format_priority"]),
            float(row["mtime"]),
            int(row["size"]),
        ),
        reverse=True,
    )
    return Path(near_complete[0]["path"])


def running_live_recording_jobs() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT lv, pid, status, recorder, broadcaster_id, broadcaster_name
            FROM recording_jobs
            WHERE status IN ('launched', 'recording')
            ORDER BY started_at, lv
            """
        ).fetchall()
    return [
        dict(row)
        for row in rows
        if is_process_running(int(row["pid"] or 0))
        and str(row["recorder"] or "") != "SlNicoLiveRec-timeshift"
    ]


def wait_for_live_recordings_to_finish(
    *,
    progress_callback: Callable[[str], None] | None = None,
    poll_seconds: float = 5.0,
) -> None:
    last_report = 0.0
    while True:
        active = running_live_recording_jobs()
        if not active:
            return
        current = time.monotonic()
        if current - last_report >= 30.0:
            lvs = ", ".join(str(row.get("lv") or "") for row in active)
            message = f"ライブ録画中のためタイムシフト取得待機: {lvs}"
            postprocess_log(None, "timeshift_video", "INFO", message, {"active": active})
            if progress_callback is not None:
                progress_callback(message)
            last_report = current
        time.sleep(max(1.0, float(poll_seconds or 1.0)))


def download_timeshift_video_with_recorder(
    item: dict[str, Any],
    config: Config | None = None,
    *,
    reuse_existing: bool = True,
    output_wait_seconds: float = 30.0,
    progress_callback: Callable[[str], None] | None = None,
    wait_for_live_recordings: bool = False,
) -> dict[str, Any]:
    with _TIMESHIFT_RECORDER_LOCK:
        if wait_for_live_recordings:
            wait_for_live_recordings_to_finish(progress_callback=progress_callback)
        return _download_timeshift_video_with_recorder_locked(
            item,
            config,
            reuse_existing=reuse_existing,
            output_wait_seconds=output_wait_seconds,
            progress_callback=progress_callback,
        )


def _download_timeshift_video_with_recorder_locked(
    item: dict[str, Any],
    config: Config | None = None,
    *,
    reuse_existing: bool = True,
    output_wait_seconds: float = 30.0,
    progress_callback: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    config = config or load_config()
    lv = str(item.get("lv") or "").strip().lower()
    if not re.fullmatch(r"lv\d+", lv):
        raise ValueError(f"invalid lv: {lv}")
    broadcaster_id = str(item.get("broadcaster_id") or "").strip()
    if not broadcaster_id:
        raise ValueError(f"{lv}: broadcaster_id is required for timeshift download")
    watch_url = str(item.get("watch_url") or "").strip() or f"https://live.nicovideo.jp/watch/{lv}"
    exe = Path(config.slnico_live_rec_exe)
    if not exe.is_file():
        raise FileNotFoundError(f"SlNicoLiveRec not found: {exe}")
    recorder_config_path = exe.parent / "SlNicoLiveRec_config.json"
    if recorder_config_path.is_file():
        try:
            recorder_config = json.loads(recorder_config_path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"SlNicoLiveRec config read failed: {recorder_config_path}: {exc}") from exc
        if not bool(recorder_config.get("CloseWindowOnExit", False)):
            raise RuntimeError(
                "SlNicoLiveRec CloseWindowOnExit must be true for background timeshift download"
            )

    with connect() as conn:
        save_broadcast_archive_meta(conn, {**item, "lv": lv, "watch_url": watch_url})
        conn.commit()
    target_dir = ensure_broadcast_target_dirs(config, lv, broadcaster_id=broadcaster_id)

    storage_root = slnico_storage_root(config)
    before_paths = find_recording_segment_files(lv, storage_root=storage_root)
    existing = select_preferred_timeshift_video(before_paths, broadcaster_id=broadcaster_id)
    if reuse_existing and existing is not None:
        postprocess_log(
            lv,
            "timeshift_video",
            "INFO",
            f"既存タイムシフト動画を再利用: {existing}",
            {"path": str(existing), "broadcaster_id": broadcaster_id},
        )
        return {
            "lv": lv,
            "broadcaster_id": broadcaster_id,
            "watch_url": watch_url,
            "video_paths": [str(existing)],
            "reused": True,
            "pid": None,
            "returncode": 0,
        }

    before = {
        str(path.resolve()).casefold(): (int(path.stat().st_size), int(path.stat().st_mtime_ns))
        for path in before_paths
        if path.exists()
    }
    process: subprocess.Popen | None = None
    started_at = now_micro()
    try:
        process, started_at = start_recording_process_with_timing(
            exe=exe,
            watch_url=watch_url,
            lv=lv,
            broadcaster_id=broadcaster_id,
            broadcaster_name=str(item.get("broadcaster_name") or ""),
            recorder="SlNicoLiveRec-timeshift",
            target_dir=target_dir,
        )
        postprocess_log(
            lv,
            "timeshift_video",
            "INFO",
            f"タイムシフト動画取得開始: pid={process.pid}",
            {
                "pid": process.pid,
                "exe": str(exe),
                "watch_url": watch_url,
                "broadcaster_id": broadcaster_id,
            },
        )
        last_progress = 0.0
        while process.poll() is None:
            elapsed = time.monotonic() - last_progress
            if progress_callback is not None and elapsed >= 10.0:
                progress_callback(f"{lv}: 動画取得中 pid={process.pid}")
                last_progress = time.monotonic()
            time.sleep(1.0)
        returncode = int(process.returncode or 0)
        if returncode != 0:
            raise RuntimeError(f"SlNicoLiveRec timeshift failed: lv={lv} returncode={returncode}")
    except Exception:
        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except Exception:
                pass
        raise

    deadline = time.monotonic() + max(1.0, float(output_wait_seconds or 0.0))
    changed_paths: list[Path] = []
    last_signature: tuple[tuple[str, int, int], ...] | None = None
    stable_count = 0
    while time.monotonic() < deadline:
        current_paths = find_recording_segment_files(lv, storage_root=storage_root)
        changed_paths = []
        signature_rows: list[tuple[str, int, int]] = []
        for path in current_paths:
            try:
                stat = path.stat()
            except FileNotFoundError:
                continue
            key = str(path.resolve()).casefold()
            current_state = (int(stat.st_size), int(stat.st_mtime_ns))
            if before.get(key) != current_state:
                changed_paths.append(path)
                signature_rows.append((key, *current_state))
        signature = tuple(sorted(signature_rows))
        if changed_paths and signature == last_signature:
            stable_count += 1
        else:
            stable_count = 0
        last_signature = signature
        if changed_paths and stable_count >= 1:
            break
        time.sleep(1.0)
    selected = select_preferred_timeshift_video(changed_paths, broadcaster_id=broadcaster_id)
    if selected is None:
        raise RuntimeError(f"{lv}: SlNicoLiveRec output video was not found under {storage_root}")
    postprocess_log(
        lv,
        "timeshift_video",
        "INFO",
        f"タイムシフト動画取得完了: {selected}",
        {
            "path": str(selected),
            "pid": process.pid if process is not None else None,
            "started_at": started_at,
            "broadcaster_id": broadcaster_id,
        },
    )
    return {
        "lv": lv,
        "broadcaster_id": broadcaster_id,
        "watch_url": watch_url,
        "video_paths": [str(selected)],
        "reused": False,
        "pid": process.pid if process is not None else None,
        "returncode": int(process.returncode or 0) if process is not None else 0,
    }


def get_file_creation_time(path: Path | str) -> dict[str, Any]:
    path = Path(path)
    stat = path.stat()
    creation_ns = int(stat.st_ctime_ns)
    creation_dt = ns_to_datetime(creation_ns)
    return {
        "path": str(path),
        "creation_time": creation_dt.isoformat(timespec="microseconds"),
        "creation_time_ns": creation_ns,
        "last_write_time": ns_to_datetime(int(stat.st_mtime_ns)).isoformat(timespec="microseconds"),
        "last_write_time_ns": int(stat.st_mtime_ns),
        "size_bytes": int(stat.st_size),
    }


def recording_segment_creation_rows(lv: str, *, storage_root: Path | None = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in find_recording_segment_files(lv, storage_root=storage_root):
        try:
            info = get_file_creation_time(path)
        except FileNotFoundError:
            continue
        info["lv"] = lv
        rows.append(info)
    rows.sort(key=lambda row: (int(row["creation_time_ns"]), str(row["path"])))
    return rows


def next_recording_file_after(lv: str, ended_at: str) -> dict[str, Any] | None:
    ended_dt = iso_to_datetime(ended_at)
    for row in recording_segment_creation_rows(lv):
        creation_dt = iso_to_datetime(str(row["creation_time"]))
        if creation_dt > ended_dt:
            return row
    return None


def recording_segment_for_process_exit(lv: str, started_at: str, ended_at: str) -> dict[str, Any] | None:
    """Find the segment most likely produced by the recorder process that just exited."""
    try:
        process_started_dt = iso_to_datetime(started_at)
        started_dt = process_started_dt - timedelta(minutes=5)
    except Exception:
        process_started_dt = None
        started_dt = datetime.min
    try:
        ended_dt = iso_to_datetime(ended_at) + timedelta(minutes=5)
    except Exception:
        ended_dt = datetime.max

    candidates: list[dict[str, Any]] = []
    for row in recording_segment_creation_rows(lv):
        path = Path(str(row["path"]))
        parsed = parse_slnico_segment_filename(path)
        if parsed and not (started_dt <= parsed["started_at"] <= ended_dt):
            continue
        try:
            creation_dt = iso_to_datetime(str(row["creation_time"]))
        except Exception:
            creation_dt = None
        try:
            write_dt = iso_to_datetime(str(row["last_write_time"]))
        except Exception:
            write_dt = None
        if creation_dt and creation_dt > ended_dt:
            continue
        if write_dt and write_dt < started_dt:
            continue
        row = dict(row)
        row["parsed_started_at"] = parsed["started_at_iso"] if parsed else ""
        if parsed and process_started_dt is not None:
            filename_delta = abs((parsed["started_at"] - process_started_dt).total_seconds())
            # A restarted recorder can create its next file before this worker
            # gets scheduled.  A file whose name is far from this process'
            # start belongs to another recording process even if its ctime is
            # newer and falls in the broad exit window.
            if filename_delta > 120.0:
                continue
            row["selection_basis"] = "filename_started_at_nearest_process_started_at"
            row["filename_start_delta_seconds"] = filename_delta
        else:
            row["selection_basis"] = "filesystem_time_fallback"
            row["filename_start_delta_seconds"] = None
        if creation_dt is not None and process_started_dt is not None:
            row["creation_start_delta_seconds"] = abs(
                (creation_dt - process_started_dt).total_seconds()
            )
        else:
            row["creation_start_delta_seconds"] = None
        candidates.append(row)
    if not candidates:
        return None
    candidates.sort(
        key=lambda row: (
            0 if row.get("filename_start_delta_seconds") is not None else 1,
            float(row.get("filename_start_delta_seconds"))
            if row.get("filename_start_delta_seconds") is not None
            else (
                float(row.get("creation_start_delta_seconds"))
                if row.get("creation_start_delta_seconds") is not None
                else float("inf")
            ),
            -int(row.get("last_write_time_ns") or 0),
            str(row.get("path") or ""),
        )
    )
    return candidates[0]


def start_segment_mp4_conversion_after_exit(
    *,
    lv: str,
    broadcaster_id: str,
    broadcaster_name: str,
    watch_url: str,
    recorder: str,
    pid: int | None,
    started_at: str,
    ended_at: str,
    exit_code: int | None,
    target_dir: str,
) -> None:
    """Convert the segment produced by an exited recorder without delaying restart."""

    def worker() -> None:
        event_at = now_micro()
        try:
            segment = recording_segment_for_process_exit(lv, started_at, ended_at)
            if not segment:
                with connect() as conn:
                    record_recording_event(
                        conn,
                        lv=lv,
                        broadcaster_id=broadcaster_id,
                        broadcaster_name=broadcaster_name,
                        watch_url=watch_url,
                        recorder=recorder,
                        pid=pid,
                        event_type="segment_mp4_skipped_no_segment",
                        event_at=event_at,
                        started_at=started_at,
                        ended_at=ended_at,
                        duration_us=None,
                        exit_code=exit_code,
                        target_dir=target_dir,
                        payload={"reason": "segment_file_not_found"},
                    )
                    conn.commit()
                return

            source = Path(str(segment["path"]))
            postprocess_log(
                lv,
                "segment_mp4",
                "INFO",
                (
                    f"終了録画区間を選択 pid={pid or 0} file={source.name} "
                    f"basis={segment.get('selection_basis') or 'unknown'} "
                    f"filename_start_delta={segment.get('filename_start_delta_seconds')}"
                ),
                {
                    "pid": pid,
                    "process_started_at": started_at,
                    "process_ended_at": ended_at,
                    "selected_path": str(source),
                    "selection_basis": segment.get("selection_basis"),
                    "filename_start_delta_seconds": segment.get("filename_start_delta_seconds"),
                    "creation_start_delta_seconds": segment.get("creation_start_delta_seconds"),
                },
            )
            mp4_path = ensure_recording_segment_mp4(source, lv=lv)
            segment_result: dict[str, Any] | None = None
            if mp4_path:
                segment_result = {
                    "lv": lv,
                    "segment_path": str(mp4_path),
                    "transcribed": False,
                    "reason": "deferred_until_broadcast_end",
                }
            with connect() as conn:
                record_recording_event(
                    conn,
                    lv=lv,
                    broadcaster_id=broadcaster_id,
                    broadcaster_name=broadcaster_name,
                    watch_url=watch_url,
                    recorder=recorder,
                    pid=pid,
                    event_type="segment_mp4_done" if mp4_path else "segment_mp4_skipped",
                    event_at=now_micro(),
                    started_at=started_at,
                    ended_at=ended_at,
                    duration_us=None,
                    exit_code=exit_code,
                    target_dir=target_dir,
                    payload={
                        "source": str(source),
                        "mp4_path": str(mp4_path) if mp4_path else "",
                        "segment": segment,
                        "segment_pipeline": segment_result,
                    },
                )
                conn.commit()
        except Exception as exc:
            postprocess_log(lv, "segment_mp4", "ERROR", f"録画終了後MP4化失敗: {type(exc).__name__}: {exc}")
            try:
                with connect() as conn:
                    record_recording_event(
                        conn,
                        lv=lv,
                        broadcaster_id=broadcaster_id,
                        broadcaster_name=broadcaster_name,
                        watch_url=watch_url,
                        recorder=recorder,
                        pid=pid,
                        event_type="segment_mp4_failed",
                        event_at=now_micro(),
                        started_at=started_at,
                        ended_at=ended_at,
                        duration_us=None,
                        exit_code=exit_code,
                        target_dir=target_dir,
                        payload={"error": f"{type(exc).__name__}: {exc}"},
                    )
                    conn.commit()
            except Exception:
                pass

    threading.Thread(target=worker, name=f"segment-mp4-{lv}-{pid or 0}", daemon=True).start()


SLNICO_SEGMENT_RE = re.compile(
    r"^(?P<lv>lv\d+)_(?P<year>\d{4})_(?P<monthday>\d{4})_(?P<hms>\d{6})_(?P<title>.+)\.(?P<ext>ts|mp4|mkv|webm|flv)$",
    re.IGNORECASE,
)


def parse_slnico_segment_filename(path: Path | str) -> dict[str, Any] | None:
    path = Path(path)
    match = SLNICO_SEGMENT_RE.match(path.name)
    if not match:
        return None
    year = int(match.group("year"))
    monthday = match.group("monthday")
    hms = match.group("hms")
    started_at = datetime(
        year,
        int(monthday[:2]),
        int(monthday[2:]),
        int(hms[:2]),
        int(hms[2:4]),
        int(hms[4:]),
    )
    return {
        "path": path,
        "lv": match.group("lv"),
        "title": match.group("title"),
        "ext": match.group("ext").lower(),
        "started_at": started_at,
        "started_at_iso": started_at.isoformat(),
    }


def probe_media_duration_seconds(path: Path | str) -> float:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=nokey=1:noprint_wrappers=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"ffprobe failed: {path}")
    return float(result.stdout.strip())


def build_audio_alignment_plan(
    probe_data: dict[str, Any],
    *,
    sample_rate: int = 16000,
) -> dict[str, Any]:
    """Map the first audio stream onto the canonical container timeline."""
    streams = list(probe_data.get("streams") or [])
    video_stream = next((row for row in streams if row.get("codec_type") == "video"), None)
    audio_stream = next((row for row in streams if row.get("codec_type") == "audio"), None)
    if audio_stream is None:
        raise RuntimeError("音声ストリームが見つかりません")

    format_info = dict(probe_data.get("format") or {})

    def optional_float(value: Any) -> float | None:
        try:
            return float(value) if value not in {None, "", "N/A"} else None
        except (TypeError, ValueError):
            return None

    format_start = optional_float(format_info.get("start_time"))
    video_start = optional_float((video_stream or {}).get("start_time"))
    timeline_start = format_start if format_start is not None else video_start if video_start is not None else 0.0
    duration = optional_float(format_info.get("duration"))
    if duration is None or duration <= 0:
        duration = optional_float((video_stream or {}).get("duration"))
    if duration is None or duration <= 0:
        raise RuntimeError("メディア再生時間を取得できません")

    audio_start = optional_float(audio_stream.get("start_time"))
    if audio_start is None:
        audio_start = timeline_start
    relative_audio_start = audio_start - timeline_start
    sample_rate = max(1, int(sample_rate))
    return {
        "format_start_time": timeline_start,
        "format_duration_seconds": duration,
        "video_start_time": video_start,
        "video_duration_seconds": optional_float((video_stream or {}).get("duration")),
        "audio_start_time": audio_start,
        "audio_duration_seconds": optional_float(audio_stream.get("duration")),
        "sample_rate": sample_rate,
        "target_samples": max(1, round(duration * sample_rate)),
        "leading_silence_samples": max(0, round(relative_audio_start * sample_rate)),
        "head_trim_samples": max(0, round(-relative_audio_start * sample_rate)),
    }


def probe_media_audio_timeline(path: Path | str, *, sample_rate: int = 16000) -> dict[str, Any]:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=start_time,duration:stream=index,codec_type,start_time,duration",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"ffprobe failed: {path}")
    return build_audio_alignment_plan(json.loads(result.stdout or "{}"), sample_rate=sample_rate)


def audio_alignment_filter(plan: dict[str, Any]) -> str:
    sample_rate = int(plan["sample_rate"])
    target_samples = int(plan["target_samples"])
    head_trim_samples = int(plan["head_trim_samples"])
    leading_silence_samples = int(plan["leading_silence_samples"])
    filters = [
        f"aresample={sample_rate}",
        "aformat=sample_fmts=s16:channel_layouts=mono",
        f"atrim=start_sample={head_trim_samples}",
        "asetpts=N/SR/TB",
    ]
    if leading_silence_samples > 0:
        filters.append(f"adelay={leading_silence_samples}S:all=1")
    filters.extend(
        [
            f"apad=whole_len={target_samples}",
            f"atrim=end_sample={target_samples}",
            "asetpts=N/SR/TB",
        ]
    )
    return ",".join(filters)


def probe_media_video_size(path: Path | str) -> tuple[int, int]:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"ffprobe failed: {path}")
    data = json.loads(result.stdout or "{}")
    streams = data.get("streams") or []
    if not streams:
        raise RuntimeError(f"映像ストリームが見つかりません: {path}")
    width = int(streams[0].get("width") or 0)
    height = int(streams[0].get("height") or 0)
    if width <= 0 or height <= 0:
        raise RuntimeError(f"映像サイズを取得できません: {path}")
    return width, height


def build_slnico_file_gap_plan(input_dir: Path | str, *, lv: str | None = None) -> dict[str, Any]:
    input_dir = Path(input_dir)
    parsed: list[dict[str, Any]] = []
    for path in input_dir.rglob("*"):
        if not path.is_file():
            continue
        info = parse_slnico_segment_filename(path)
        if not info:
            continue
        if lv and info["lv"] != lv:
            continue
        info["duration_seconds"] = probe_media_duration_seconds(path)
        info["ended_at"] = info["started_at"] + timedelta(seconds=float(info["duration_seconds"]))
        info["ended_at_iso"] = info["ended_at"].isoformat()
        parsed.append(info)
    parsed.sort(key=lambda row: (row["lv"], row["started_at"], str(row["path"])))
    gaps: list[dict[str, Any]] = []
    ordered_parts: list[dict[str, Any]] = []
    for index, segment in enumerate(parsed):
        segment["segment_index"] = index
        ordered_parts.append({"type": "segment", **segment})
        if index >= len(parsed) - 1:
            continue
        next_segment = parsed[index + 1]
        if next_segment["lv"] != segment["lv"]:
            continue
        gap_seconds = (next_segment["started_at"] - segment["ended_at"]).total_seconds()
        if gap_seconds <= 0.05:
            continue
        gap = {
            "type": "gap",
            "lv": segment["lv"],
            "gap_index": len(gaps),
            "gap_start": segment["ended_at"],
            "gap_end": next_segment["started_at"],
            "gap_start_iso": segment["ended_at"].isoformat(),
            "gap_end_iso": next_segment["started_at"].isoformat(),
            "duration_seconds": gap_seconds,
            "duration_us": int(gap_seconds * 1_000_000),
            "previous_path": str(segment["path"]),
            "next_path": str(next_segment["path"]),
        }
        gaps.append(gap)
        ordered_parts.append(gap)
    return {
        "input_dir": str(input_dir),
        "lv": lv or (parsed[0]["lv"] if parsed else ""),
        "segments": parsed,
        "gaps": gaps,
        "parts": ordered_parts,
    }


def build_recording_gap_concat_plan(
    input_dir: Path | str,
    *,
    lv: str,
    conn: sqlite3.Connection | None = None,
    timeline_mode: str = "live",
    segment_paths: list[Path | str] | None = None,
) -> dict[str, Any]:
    """Build the canonical broadcast clock for independent recording segments.

    Live recordings use ``recording_events.started_at`` for every segment and
    ffprobe duration for its end.  Filename timestamps are used only to pair a
    file with its recorder event; filesystem timestamps are audit metadata only.
    Timeshift imports are explicit and start at zero in the supplied file order.
    """
    normalized_mode = str(timeline_mode or "live").strip().lower()
    if normalized_mode not in {"live", "timeshift"}:
        raise ValueError(f"unknown recording timeline mode: {timeline_mode}")
    input_dir = Path(input_dir)
    segments: list[dict[str, Any]] = []
    if segment_paths is None:
        creation_rows = recording_segment_creation_rows(lv, storage_root=input_dir)
    else:
        creation_rows = []
        for supplied_index, supplied_path in enumerate(segment_paths):
            path = Path(supplied_path)
            if not path.is_file():
                continue
            row = get_file_creation_time(path)
            row["lv"] = lv
            row["supplied_index"] = supplied_index
            creation_rows.append(row)
    deduped_rows: dict[str, dict[str, Any]] = {}
    for row in creation_rows:
        path = Path(str(row["path"]))
        key = str(path.with_suffix("")).lower()
        existing = deduped_rows.get(key)
        if existing is None:
            deduped_rows[key] = dict(row)
            continue
        existing_path = Path(str(existing["path"]))
        if path.suffix.lower() == ".mp4" and existing_path.suffix.lower() != ".mp4":
            deduped_rows[key] = dict(row)
    for row in deduped_rows.values():
        path = Path(str(row["path"]))
        parsed_filename = parse_slnico_segment_filename(path)
        if parsed_filename:
            parsed = dict(parsed_filename)
            parsed["filename_started_at"] = parsed.pop("started_at")
            parsed["filename_started_at_iso"] = parsed.pop("started_at_iso")
        else:
            parsed = {
                "path": path,
                "lv": lv,
                "title": path.stem,
                "ext": path.suffix.lower().lstrip("."),
                "filename_started_at": None,
                "filename_started_at_iso": "",
            }
        parsed["creation_time"] = str(row["creation_time"])
        parsed["creation_time_ns"] = int(row["creation_time_ns"])
        parsed["size_bytes"] = int(row["size_bytes"])
        parsed["supplied_index"] = row.get("supplied_index")
        try:
            parsed["duration_seconds"] = probe_media_duration_seconds(path)
            parsed["duration_probe_error"] = ""
        except Exception as exc:
            # The newest recorder file can still be open while a prior segment
            # is being processed.  Finalization validates that every duration
            # became available before producing archive artifacts.
            parsed["duration_seconds"] = 0.0
            parsed["duration_probe_error"] = f"{type(exc).__name__}: {exc}"
        parsed["path"] = path
        segments.append(parsed)
    if segment_paths is not None:
        segments.sort(
            key=lambda row: (
                int(row["supplied_index"]) if row.get("supplied_index") is not None else 2**31,
                str(row["path"]),
            )
        )
    else:
        segments.sort(
            key=lambda row: (
                row.get("filename_started_at") or datetime.max,
                int(row["creation_time_ns"]),
                str(row["path"]),
            )
        )
    if not segments:
        return {
            "input_dir": str(input_dir),
            "lv": lv,
            "segments": [],
            "gaps": [],
            "parts": [],
            "timeline_origin": "",
            "timeline_origin_source": "missing_segments",
            "timeline_mode": normalized_mode,
        }

    connection_context = contextlib.nullcontext(conn) if conn is not None else connect()
    with connection_context as timeline_conn:
        meta_row = timeline_conn.execute(
            """
            SELECT open_time, begin_time, start_time
            FROM broadcast_archive_meta
            WHERE lv = ?
            """,
            (lv,),
        ).fetchone()
        started_event_rows = timeline_conn.execute(
            """
            SELECT id, pid, started_at, event_at, target_dir
            FROM recording_events
            WHERE lv = ? AND event_type = 'started'
            ORDER BY COALESCE(started_at, event_at) ASC, id ASC
            """,
            (lv,),
        ).fetchall()
        stored_gap_rows = timeline_conn.execute(
            """
            SELECT gap_start, gap_end, duration_us
            FROM recording_gaps
            WHERE lv = ?
              AND duration_us > 0
            ORDER BY gap_start ASC, gap_end ASC
            """,
            (lv,),
        ).fetchall()

    meta = dict(meta_row) if meta_row else {}
    broadcast_start = unix_seconds_to_local_datetime(
        meta.get("open_time") or meta.get("begin_time") or meta.get("start_time")
    )
    started_events: list[dict[str, Any]] = []
    for event_row in started_event_rows:
        event = dict(event_row)
        value = str(event.get("started_at") or event.get("event_at") or "").strip()
        if not value:
            continue
        try:
            event["started_at_datetime"] = iso_to_datetime(value)
        except Exception:
            continue
        event["started_at_iso"] = event["started_at_datetime"].isoformat(timespec="microseconds")
        started_events.append(event)

    unmatched_event_ids: set[int] = {int(event["id"]) for event in started_events}
    unmatched_segment_paths: list[str] = []
    if normalized_mode == "live":
        # SlNico's filename clock is close enough to identify its Popen event,
        # but it is intentionally never used to calculate the timeline.
        for segment_index, segment in enumerate(segments):
            available = [event for event in started_events if int(event["id"]) in unmatched_event_ids]
            filename_start = segment.get("filename_started_at")
            matched_event: dict[str, Any] | None = None
            if available and isinstance(filename_start, datetime):
                candidate = min(
                    available,
                    key=lambda event: abs((event["started_at_datetime"] - filename_start).total_seconds()),
                )
                if abs((candidate["started_at_datetime"] - filename_start).total_seconds()) <= 120.0:
                    matched_event = candidate
            elif available:
                matched_event = available[0]
            if matched_event is None:
                unmatched_segment_paths.append(str(segment["path"]))
                continue
            unmatched_event_ids.discard(int(matched_event["id"]))
            segment["recording_event_id"] = int(matched_event["id"])
            segment["recording_pid"] = matched_event.get("pid")
            segment["started_at"] = matched_event["started_at_datetime"]
            segment["started_at_iso"] = matched_event["started_at_iso"]
            segment["start_time_source"] = "recording_events.started_at"

        segments.sort(
            key=lambda row: (
                row.get("started_at") or datetime.max,
                row.get("filename_started_at") or datetime.max,
                str(row["path"]),
            )
        )
        matched_segments = [segment for segment in segments if isinstance(segment.get("started_at"), datetime)]
        recording_start = matched_segments[0]["started_at"] if matched_segments else None
        if recording_start is not None and broadcast_start is not None:
            timeline_origin = broadcast_start
            timeline_origin_source = "broadcast_open_time"
            raw_initial_offset_seconds = max(0.0, (recording_start - broadcast_start).total_seconds())
            # This is the existing parsec definition used by the archive steps.
            initial_offset_seconds = float(max(0, int(round(raw_initial_offset_seconds))))
        elif recording_start is not None:
            timeline_origin = recording_start
            timeline_origin_source = "first_recording_event_without_broadcast_meta"
            raw_initial_offset_seconds = 0.0
            initial_offset_seconds = 0.0
        else:
            timeline_origin = broadcast_start or datetime(1970, 1, 1)
            timeline_origin_source = "missing_recording_started_events"
            raw_initial_offset_seconds = 0.0
            initial_offset_seconds = 0.0
    else:
        recording_start = None
        timeline_origin = datetime(1970, 1, 1)
        timeline_origin_source = "explicit_timeshift_zero"
        raw_initial_offset_seconds = 0.0
        initial_offset_seconds = 0.0
        cursor = 0.0
        for segment in segments:
            segment["started_at"] = timeline_origin + timedelta(seconds=cursor)
            segment["started_at_iso"] = segment["started_at"].isoformat(timespec="microseconds")
            segment["start_time_source"] = "explicit_timeshift_file_order"
            cursor += max(0.0, float(segment.get("duration_seconds") or 0.0))

    for segment in segments:
        started_at = segment.get("started_at")
        if isinstance(started_at, datetime):
            segment["media_end_at"] = started_at + timedelta(
                seconds=max(0.0, float(segment.get("duration_seconds") or 0.0))
            )
            segment["media_end_at_iso"] = segment["media_end_at"].isoformat(timespec="microseconds")
            segment["filesystem_creation_delta_seconds"] = (
                iso_to_datetime(str(segment["creation_time"])) - started_at
            ).total_seconds()
            filename_start = segment.get("filename_started_at")
            segment["filename_vs_event_seconds"] = (
                (filename_start - started_at).total_seconds()
                if isinstance(filename_start, datetime)
                else None
            )
        else:
            segment["media_end_at"] = None
            segment["media_end_at_iso"] = ""
            segment["filesystem_creation_delta_seconds"] = 0.0
            segment["filename_vs_event_seconds"] = None

    gaps: list[dict[str, Any]] = []
    ordered_parts: list[dict[str, Any]] = []
    pre_gap_seconds = initial_offset_seconds
    if pre_gap_seconds > 0.05:
        pre_gap = {
            "type": "gap",
            "lv": lv,
            "gap_index": len(gaps),
            "gap_start": timeline_origin.isoformat(timespec="microseconds"),
            "gap_end": recording_start.isoformat(timespec="microseconds"),
            "gap_start_iso": timeline_origin.isoformat(timespec="microseconds"),
            "gap_end_iso": recording_start.isoformat(timespec="microseconds"),
            "duration_seconds": pre_gap_seconds,
            "duration_us": int(round(pre_gap_seconds * 1_000_000)),
            "fill_type": "black_silent_video",
            "generated_video_path": "",
            "reason": "broadcast_start_to_live_recording_start",
            "next_path": str(segments[0]["path"]),
        }
        gaps.append(pre_gap)
        ordered_parts.append(pre_gap)

    overlaps: list[dict[str, Any]] = []
    for segment_index, segment in enumerate(segments):
        segment["segment_index"] = segment_index
        if not isinstance(segment.get("started_at"), datetime):
            segment["clock_start_seconds"] = None
            segment["clock_end_seconds"] = None
            ordered_parts.append({"type": "segment", **segment})
            continue
        if segment_index > 0:
            previous = segments[segment_index - 1]
            previous_end = previous.get("media_end_at")
            gap_seconds = (
                (segment["started_at"] - previous_end).total_seconds()
                if isinstance(previous_end, datetime)
                else 0.0
            )
            if gap_seconds > 0.05:
                gap = {
                    "type": "gap",
                    "lv": lv,
                    "gap_index": len(gaps),
                    "gap_start": previous["media_end_at"].isoformat(timespec="microseconds"),
                    "gap_end": segment["started_at"].isoformat(timespec="microseconds"),
                    "gap_start_iso": previous["media_end_at"].isoformat(timespec="microseconds"),
                    "gap_end_iso": segment["started_at"].isoformat(timespec="microseconds"),
                    "duration_seconds": gap_seconds,
                    "duration_us": int(round(gap_seconds * 1_000_000)),
                    "fill_type": "black_silent_video",
                    "generated_video_path": "",
                    "reason": "segment_media_clock_gap",
                    "previous_path": str(previous["path"]),
                    "next_path": str(segment["path"]),
                }
                gaps.append(gap)
                ordered_parts.append(gap)
            elif gap_seconds < -0.05:
                overlap = {
                    "previous_path": str(previous["path"]),
                    "next_path": str(segment["path"]),
                    "duration_seconds": abs(gap_seconds),
                    "previous_media_end": previous["media_end_at_iso"],
                    "next_media_start": segment["started_at_iso"],
                }
                overlaps.append(overlap)
                segment["clock_overlap_seconds"] = abs(gap_seconds)
        segment["clock_start_seconds"] = initial_offset_seconds + (
            max(0.0, (segment["started_at"] - recording_start).total_seconds())
            if recording_start is not None
            else sum(
                max(0.0, float(item.get("duration_seconds") or 0.0))
                for item in segments[:segment_index]
            )
        )
        segment["clock_end_seconds"] = segment["clock_start_seconds"] + float(
            segment.get("duration_seconds") or 0.0
        )
        ordered_parts.append({"type": "segment", **segment})

    return {
        "input_dir": str(input_dir),
        "lv": lv,
        "segments": segments,
        "gaps": gaps,
        "unmatched_gaps": [],
        "overlaps": overlaps,
        "parts": ordered_parts,
        "gap_source": (
            "recording_events.started_at_plus_ffprobe_duration"
            if normalized_mode == "live"
            else "explicit_timeshift_file_order"
        ),
        "timeline_origin": timeline_origin.isoformat(timespec="microseconds"),
        "timeline_origin_source": timeline_origin_source,
        "timeline_mode": normalized_mode,
        "broadcast_start": broadcast_start.isoformat(timespec="microseconds") if broadcast_start else "",
        "recording_start": recording_start.isoformat(timespec="microseconds") if recording_start else "",
        "first_media_start": (
            segments[0]["started_at"].isoformat(timespec="microseconds")
            if isinstance(segments[0].get("started_at"), datetime)
            else ""
        ),
        "initial_offset_seconds": initial_offset_seconds,
        "raw_initial_offset_seconds": raw_initial_offset_seconds,
        "first_media_vs_recording_start_seconds": (
            float(segments[0].get("filename_vs_event_seconds") or 0.0) if recording_start else 0.0
        ),
        "recording_started_event_count": len(started_events),
        "unmatched_recording_event_ids": sorted(unmatched_event_ids),
        "unmatched_segment_paths": unmatched_segment_paths,
        "stored_gap_snapshot": [dict(row) for row in stored_gap_rows],
    }


def recording_segment_identity(path: Path | str) -> str:
    return str(Path(path).with_suffix("")).casefold()


def timeline_plan_from_recording_parts(plan: dict[str, Any]) -> dict[str, Any]:
    """Add broadcast-wide offsets without changing the source concat plan."""
    cursor = 0.0
    parts: list[dict[str, Any]] = []
    segments: list[dict[str, Any]] = []
    gaps: list[dict[str, Any]] = []
    for raw_part in plan.get("parts") or []:
        part = dict(raw_part)
        duration = max(0.0, float(part.get("duration_seconds") or 0.0))
        part["timeline_start_seconds"] = cursor
        part["timeline_end_seconds"] = cursor + duration
        part["duration_seconds"] = duration
        if part.get("type") == "segment":
            part["segment_index"] = len(segments)
            part["segment_identity"] = recording_segment_identity(str(part.get("path") or ""))
            if part.get("clock_start_seconds") is not None:
                part["timeline_clock_error_seconds"] = cursor - float(part["clock_start_seconds"])
            segments.append(part)
        else:
            gaps.append(part)
        parts.append(part)
        cursor += duration
    return {
        **plan,
        "parts": parts,
        "segments": segments,
        "gaps": gaps,
        "total_duration_seconds": cursor,
    }


def log_recording_timeline_plan(lv: str, plan: dict[str, Any]) -> None:
    segments = list(plan.get("segments") or [])
    gaps = list(plan.get("gaps") or [])
    stored_gaps = list(plan.get("stored_gap_snapshot") or [])
    initial_offset = float(segments[0].get("timeline_start_seconds") or 0.0) if segments else 0.0
    computed_inter_gap = sum(
        float(gap.get("duration_seconds") or 0.0)
        for gap in gaps
        if str(gap.get("reason") or "") == "segment_media_clock_gap"
    )
    stored_gap = sum(int(row.get("duration_us") or 0) / 1_000_000 for row in stored_gaps)
    correction = computed_inter_gap - stored_gap
    fingerprint = "|".join(
        f"{Path(str(segment.get('path') or '')).name}:{float(segment.get('duration_seconds') or 0.0):.3f}"
        for segment in segments
    )
    postprocess_log(
        lv,
        "timeline",
        "WARN" if stored_gaps and abs(correction) > 0.05 else "INFO",
        (
            "録画時間軸確定 "
            f"mode={plan.get('timeline_mode') or '-'} "
            f"origin={plan.get('timeline_origin') or '-'} "
            f"origin_source={plan.get('timeline_origin_source') or '-'} "
            f"initial_offset={initial_offset:.3f}s segments={len(segments)} "
            f"inter_gaps={computed_inter_gap:.3f}s total={float(plan.get('total_duration_seconds') or 0.0):.3f}s "
            f"stored_gap_correction={correction:+.3f}s"
        ),
        {
            "timeline_origin": plan.get("timeline_origin"),
            "timeline_origin_source": plan.get("timeline_origin_source"),
            "timeline_mode": plan.get("timeline_mode"),
            "broadcast_start": plan.get("broadcast_start"),
            "recording_start": plan.get("recording_start"),
            "first_media_start": plan.get("first_media_start"),
            "first_media_vs_recording_start_seconds": float(
                plan.get("first_media_vs_recording_start_seconds") or 0.0
            ),
            "initial_offset_seconds": initial_offset,
            "raw_initial_offset_seconds": float(plan.get("raw_initial_offset_seconds") or 0.0),
            "computed_inter_gap_seconds": computed_inter_gap,
            "stored_inter_gap_seconds": stored_gap,
            "stored_gap_correction_seconds": correction,
            "total_duration_seconds": float(plan.get("total_duration_seconds") or 0.0),
            "segment_count": len(segments),
            "overlaps": plan.get("overlaps") or [],
        },
        once_key=f"timeline-summary:{fingerprint}:{initial_offset:.3f}:{computed_inter_gap:.3f}",
    )
    for segment in segments:
        creation_delta = float(segment.get("filesystem_creation_delta_seconds") or 0.0)
        clock_error = float(segment.get("timeline_clock_error_seconds") or 0.0)
        duration_probe_error = str(segment.get("duration_probe_error") or "")
        segment_level = "WARN" if duration_probe_error or abs(clock_error) > 0.05 else "DEBUG"
        postprocess_log(
            lv,
            "timeline",
            segment_level,
            (
                f"区間{int(segment.get('segment_index') or 0)} "
                f"global={float(segment.get('timeline_start_seconds') or 0.0):.3f}s "
                f"local=0.000s duration={float(segment.get('duration_seconds') or 0.0):.3f}s "
                f"media_start={segment.get('started_at_iso') or '-'} "
                f"source={segment.get('start_time_source') or '-'} "
                f"filesystem_creation_delta={creation_delta:+.3f}s "
                f"clock_error={clock_error:+.6f}s "
                f"probe={'failed' if duration_probe_error else 'ok'} "
                f"file={Path(str(segment.get('path') or '')).name}"
            ),
            {
                "path": str(segment.get("path") or ""),
                "media_start": segment.get("started_at_iso"),
                "media_end": segment.get("media_end_at_iso"),
                "duration_seconds": float(segment.get("duration_seconds") or 0.0),
                "timeline_start_seconds": float(segment.get("timeline_start_seconds") or 0.0),
                "timeline_end_seconds": float(segment.get("timeline_end_seconds") or 0.0),
                "start_time_source": segment.get("start_time_source"),
                "recording_event_id": segment.get("recording_event_id"),
                "recording_pid": segment.get("recording_pid"),
                "filename_started_at": segment.get("filename_started_at_iso"),
                "filename_vs_event_seconds": segment.get("filename_vs_event_seconds"),
                "filesystem_creation_time": segment.get("creation_time"),
                "filesystem_creation_delta_seconds": creation_delta,
                "filesystem_creation_used_for_timeline": False,
                "timeline_clock_error_seconds": clock_error,
                "duration_probe_error": duration_probe_error,
            },
            once_key=(
                f"timeline-segment:{recording_segment_identity(str(segment.get('path') or ''))}:"
                f"{float(segment.get('duration_seconds') or 0.0):.3f}:"
                f"{float(segment.get('timeline_start_seconds') or 0.0):.3f}"
            ),
        )
    for gap in gaps:
        reason = str(gap.get("reason") or "")
        if reason != "segment_media_clock_gap":
            continue
        postprocess_log(
            lv,
            "timeline",
            "INFO",
            (
                f"無録音区間 global={float(gap.get('timeline_start_seconds') or 0.0):.3f}s "
                f"duration={float(gap.get('duration_seconds') or 0.0):.6f}s "
                "formula=next_started_at-(previous_started_at+previous_media_duration) "
                f"previous={Path(str(gap.get('previous_path') or '')).name} "
                f"next={Path(str(gap.get('next_path') or '')).name}"
            ),
            {
                "gap_start": gap.get("gap_start_iso") or gap.get("gap_start"),
                "gap_end": gap.get("gap_end_iso") or gap.get("gap_end"),
                "duration_seconds": float(gap.get("duration_seconds") or 0.0),
                "timeline_start_seconds": float(gap.get("timeline_start_seconds") or 0.0),
                "timeline_end_seconds": float(gap.get("timeline_end_seconds") or 0.0),
                "previous_path": str(gap.get("previous_path") or ""),
                "next_path": str(gap.get("next_path") or ""),
            },
            once_key=(
                f"timeline-gap:{gap.get('gap_start_iso') or gap.get('gap_start')}:"
                f"{gap.get('gap_end_iso') or gap.get('gap_end')}"
            ),
        )
    if plan.get("unmatched_recording_event_ids") or plan.get("unmatched_segment_paths"):
        postprocess_log(
            lv,
            "timeline",
            "WARN",
            (
                "録画イベントと動画区間の対応差 "
                f"unmatched_events={len(plan.get('unmatched_recording_event_ids') or [])} "
                f"unmatched_segments={len(plan.get('unmatched_segment_paths') or [])}"
            ),
            {
                "unmatched_recording_event_ids": plan.get("unmatched_recording_event_ids") or [],
                "unmatched_segment_paths": plan.get("unmatched_segment_paths") or [],
            },
            once_key=(
                f"timeline-unmatched:{plan.get('unmatched_recording_event_ids')}:"
                f"{plan.get('unmatched_segment_paths')}"
            ),
        )
    for overlap in plan.get("overlaps") or []:
        postprocess_log(
            lv,
            "timeline",
            "WARN",
            f"録画区間の時刻重複を検出 duration={float(overlap.get('duration_seconds') or 0.0):.3f}s",
            overlap,
            once_key=f"timeline-overlap:{overlap.get('previous_path')}:{overlap.get('next_path')}",
        )


def build_recording_segment_timeline_plan(
    input_dir: Path | str,
    *,
    lv: str,
    conn: sqlite3.Connection | None = None,
    timeline_mode: str = "live",
    segment_paths: list[Path | str] | None = None,
) -> dict[str, Any]:
    plan = timeline_plan_from_recording_parts(
        build_recording_gap_concat_plan(
            input_dir,
            lv=lv,
            conn=conn,
            timeline_mode=timeline_mode,
            segment_paths=segment_paths,
        )
    )
    if conn is None:
        log_recording_timeline_plan(lv, plan)
    return plan


def validate_recording_timeline_plan(
    plan: dict[str, Any],
    *,
    require_complete: bool = False,
) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    segments = list(plan.get("segments") or [])
    if not segments:
        errors.append("recording segments are empty")
    if str(plan.get("timeline_mode") or "live") == "live":
        unmatched_segments = list(plan.get("unmatched_segment_paths") or [])
        if unmatched_segments:
            errors.append(
                "recording segment has no matching recording_events.started_at: "
                + ", ".join(Path(path).name for path in unmatched_segments)
            )
        unmatched_events = list(plan.get("unmatched_recording_event_ids") or [])
        if unmatched_events:
            warnings.append(
                "recording started events without media: "
                + ", ".join(str(event_id) for event_id in unmatched_events)
            )
    for segment in segments:
        label = Path(str(segment.get("path") or "")).name
        duration = float(segment.get("duration_seconds") or 0.0)
        probe_error = str(segment.get("duration_probe_error") or "")
        if duration <= 0.0:
            message = f"segment duration is unavailable: {label}: {probe_error or 'duration<=0'}"
            (errors if require_complete else warnings).append(message)
        if (
            str(plan.get("timeline_mode") or "live") == "live"
            and str(segment.get("start_time_source") or "") != "recording_events.started_at"
        ):
            errors.append(f"segment start is not recording_events.started_at: {label}")
        clock_error = abs(float(segment.get("timeline_clock_error_seconds") or 0.0))
        if clock_error > 0.05:
            errors.append(f"segment clock is discontinuous: {label}: {clock_error:.6f}s")
    for overlap in plan.get("overlaps") or []:
        errors.append(
            "segment media overlap: "
            f"{Path(str(overlap.get('previous_path') or '')).name} -> "
            f"{Path(str(overlap.get('next_path') or '')).name}: "
            f"{float(overlap.get('duration_seconds') or 0.0):.6f}s"
        )
    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "segment_count": len(segments),
        "gap_count": len(plan.get("gaps") or []),
        "total_duration_seconds": float(plan.get("total_duration_seconds") or 0.0),
    }


def select_recording_segment_for_timeline_second(
    plan: dict[str, Any],
    timeline_second: float,
) -> dict[str, Any] | None:
    value = max(0.0, float(timeline_second))
    segments = list(plan.get("segments") or [])
    for index, segment in enumerate(segments):
        start = float(segment.get("timeline_start_seconds") or 0.0)
        end = float(segment.get("timeline_end_seconds") or start)
        if start <= value < end or (index == len(segments) - 1 and abs(value - end) < 0.001):
            selected = dict(segment)
            duration = max(0.0, end - start)
            max_local = max(0.0, duration - 0.001) if duration else 0.0
            selected["local_seconds"] = max(0.0, min(value - start, max_local))
            return selected
    return None


def validate_archive_timeline_alignment(
    conn: sqlite3.Connection,
    lv: str,
    plan: dict[str, Any],
) -> dict[str, Any]:
    """Verify finalized transcript/comment rows against the canonical media clock."""
    media_segments = {
        int(segment.get("segment_index") or index): {
            "start": float(segment.get("timeline_start_seconds") or 0.0),
            "end": float(
                segment.get("timeline_end_seconds")
                or (
                    float(segment.get("timeline_start_seconds") or 0.0)
                    + float(segment.get("duration_seconds") or 0.0)
                )
            ),
        }
        for index, segment in enumerate(plan.get("segments") or [])
    }
    transcript_rows = conn.execute(
        """
        SELECT id, segment_index, start_seconds, end_seconds, raw_json
        FROM archive_transcript_segments
        WHERE lv = ?
        ORDER BY segment_index, start_seconds, id
        """,
        (lv,),
    ).fetchall()
    errors: list[str] = []
    invalid_ids: list[int] = []
    local_clock_mismatches = 0
    for row in transcript_rows:
        db_id = int(row["id"])
        media_index = int(row["segment_index"] or 0) // 1_000_000
        media = media_segments.get(media_index)
        start = float(row["start_seconds"] or 0.0)
        end = float(row["end_seconds"] or start)
        if media is None:
            invalid_ids.append(db_id)
            errors.append(f"transcript id={db_id} has unknown media segment {media_index}")
            continue
        if start < media["start"] - 0.05 or end > media["end"] + 0.05 or end < start:
            invalid_ids.append(db_id)
            errors.append(
                f"transcript id={db_id} [{start:.6f},{end:.6f}] is outside "
                f"segment {media_index} [{media['start']:.6f},{media['end']:.6f}]"
            )
        try:
            raw = json.loads(str(row["raw_json"] or "{}"))
        except Exception:
            raw = {}
        if isinstance(raw, dict) and raw.get("local_start_seconds") is not None:
            try:
                local_start = float(raw["local_start_seconds"])
                saved_offset = float(raw.get("timeline_offset_seconds", media["start"]))
                if abs((local_start + saved_offset) - start) > 0.01:
                    local_clock_mismatches += 1
                    invalid_ids.append(db_id)
                    errors.append(
                        f"transcript id={db_id} local+offset mismatch "
                        f"expected={local_start + saved_offset:.6f} actual={start:.6f}"
                    )
            except (TypeError, ValueError):
                local_clock_mismatches += 1
                invalid_ids.append(db_id)
                errors.append(f"transcript id={db_id} has invalid local timeline metadata")

    total_duration = float(plan.get("total_duration_seconds") or 0.0)
    comment_stats = conn.execute(
        """
        SELECT COUNT(*) AS row_count,
               MIN(broadcast_seconds) AS min_second,
               MAX(broadcast_seconds) AS max_second,
               SUM(CASE WHEN broadcast_seconds < 0 OR broadcast_seconds > ? THEN 1 ELSE 0 END) AS outside_count
        FROM archive_comments
        WHERE lv = ? AND broadcast_seconds IS NOT NULL
        """,
        (total_duration + 1.0, lv),
    ).fetchone()
    comment_count = int(comment_stats["row_count"] or 0) if comment_stats else 0
    comments_outside = int(comment_stats["outside_count"] or 0) if comment_stats else 0
    return {
        "valid": not errors,
        "errors": errors[:50],
        "transcript_count": len(transcript_rows),
        "invalid_transcript_count": len(set(invalid_ids)),
        "local_clock_mismatch_count": local_clock_mismatches,
        "comment_count": comment_count,
        "comment_min_seconds": (
            float(comment_stats["min_second"])
            if comment_stats and comment_stats["min_second"] is not None
            else None
        ),
        "comment_max_seconds": (
            float(comment_stats["max_second"])
            if comment_stats and comment_stats["max_second"] is not None
            else None
        ),
        "comments_outside_timeline": comments_outside,
        "total_duration_seconds": total_duration,
    }


def recording_segment_timeline_entry(plan: dict[str, Any], path: Path | str) -> dict[str, Any] | None:
    identity = recording_segment_identity(path)
    for segment in plan.get("segments") or []:
        if str(segment.get("segment_identity") or "") == identity:
            return dict(segment)
    return None


def segment_transcription_lock(lv: str) -> threading.Lock:
    key = str(lv)
    with _SEGMENT_TRANSCRIPTION_LOCKS_GUARD:
        lock = _SEGMENT_TRANSCRIPTION_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _SEGMENT_TRANSCRIPTION_LOCKS[key] = lock
        return lock


def concat_video_encoder_args(config: Config | None = None) -> list[str]:
    config = config or load_config()
    encoder = str(config.concat_video_encoder or "h264_nvenc").strip()
    crf = int(config.concat_output_crf or 28)
    if encoder == "h264_nvenc":
        preset = str(config.concat_nvenc_preset or "p4").strip() or "p4"
        return [
            "-c:v",
            "h264_nvenc",
            "-preset",
            preset,
            "-cq:v",
            str(crf),
            "-pix_fmt",
            "yuv420p",
        ]
    if encoder == "hevc_nvenc":
        preset = str(config.concat_nvenc_preset or "p4").strip() or "p4"
        return [
            "-c:v",
            "hevc_nvenc",
            "-preset",
            preset,
            "-cq:v",
            str(crf),
            "-pix_fmt",
            "yuv420p",
        ]
    return [
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        str(crf),
        "-pix_fmt",
        "yuv420p",
    ]


def concat_video_filter_suffix(config: Config | None = None) -> str:
    config = config or load_config()
    filters: list[str] = []
    scale = str(config.concat_output_scale or "").strip()
    fps = int(config.concat_output_fps or 0)
    if scale:
        filters.append(f"scale={scale}")
    if fps > 0:
        filters.append(f"fps={fps}")
    return ",".join(filters)


def create_black_silent_gap_video(
    path: Path | str,
    duration_seconds: float,
    *,
    lv: str | None = None,
    reference_video_size: tuple[int, int] | None = None,
) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    config = load_config()
    scale = str(config.concat_output_scale or "854:-2").strip()
    fps = int(config.concat_output_fps or 15)
    if reference_video_size:
        source_size = f"{int(reference_video_size[0])}x{int(reference_video_size[1])}"
    else:
        source_size = scale if re.fullmatch(r"\d+x\d+", scale) else "1280x720"
    run_subprocess_with_stage_log(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c=black:s={source_size}:r={fps}",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=channel_layout=stereo:sample_rate=48000",
            "-t",
            f"{max(0.05, float(duration_seconds)):.6f}",
            *concat_video_encoder_args(config),
            "-c:a",
            "aac",
            "-shortest",
            str(path),
        ],
        lv=lv,
        stage="concat_video",
        label=f"黒無音gap生成 {path.name} size={source_size} duration={duration_seconds:.3f}s",
        timeout=max(30, int(duration_seconds) + 30),
    )
    return path


def convert_recording_ts_to_mp4(
    ts_path: Path | str,
    *,
    output_path: Path | str | None = None,
    lv: str | None = None,
    force: bool = False,
) -> Path | None:
    """Convert one completed SlNicoLiveRec TS segment to a same-name MP4.

    The normal path is a fast remux. If the source cannot be remuxed as-is,
    retry with the configured video encoder so the caller still gets an MP4.
    """
    source = Path(ts_path)
    if source.suffix.lower() != ".ts":
        raise ValueError(f"TSファイルではありません: {source}")

    destination = Path(output_path) if output_path is not None else source.with_suffix(".mp4")
    destination.parent.mkdir(parents=True, exist_ok=True)

    if destination.exists() and not force:
        postprocess_log(
            lv,
            "segment_mp4",
            "DEBUG",
            f"セグメントMP4作成スキップ 既存 mp4={destination.name}",
            {"source": str(source), "output": str(destination)},
        )
        return destination
    if not source.exists():
        if destination.exists():
            postprocess_log(
                lv,
                "segment_mp4",
                "DEBUG",
                f"TSをMP4化スキップ TSなしMP4あり mp4={destination.name}",
                {"source": str(source), "output": str(destination)},
            )
            return destination
        postprocess_log(
            lv,
            "segment_mp4",
            "DEBUG",
            f"TSをMP4化スキップ ファイルなし source={source}",
            {"source": str(source)},
        )
        return None

    tmp_destination = destination.with_name(
        f"{destination.stem}.tmp-{os.getpid()}-{threading.get_ident()}{destination.suffix}"
    )
    if tmp_destination.exists():
        try:
            tmp_destination.unlink()
        except Exception:
            pass

    try:
        duration = probe_media_duration_seconds(source)
    except Exception:
        if destination.exists():
            return destination
        if not source.exists():
            postprocess_log(
                lv,
                "segment_mp4",
                "DEBUG",
                f"TSをMP4化スキップ probe前にTS消失 source={source}",
                {"source": str(source), "output": str(destination)},
            )
            return None
        raise

    source_label = compact_recording_segment_label(source)
    output_label = compact_recording_segment_label(destination)
    remux_cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(source),
        "-map",
        "0",
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        str(tmp_destination),
    ]
    try:
        run_subprocess_with_stage_log(
            remux_cmd,
            lv=lv,
            stage="segment_mp4",
            label=f"TS->MP4 remux {source_label}",
            timeout=max(120, int(duration / 10) + 120),
            progress_total_seconds=duration,
        )
        if destination.exists() and not force:
            try:
                tmp_destination.unlink()
            except Exception:
                pass
            postprocess_log(
                lv,
            "segment_mp4",
            "DEBUG",
            f"TS->MP4 remux後に既存MP4を採用 {output_label}",
            {"source": str(source), "output": str(destination), "discarded_temp": str(tmp_destination)},
        )
            return destination
        tmp_destination.replace(destination)
        return destination
    except Exception as exc:
        if destination.exists() and not force:
            try:
                if tmp_destination.exists():
                    tmp_destination.unlink()
            except Exception:
                pass
            postprocess_log(
                lv,
                "segment_mp4",
                "DEBUG",
                f"TS remux失敗後に既存MP4を採用 {output_label}",
                {"source": str(source), "output": str(destination), "error": f"{type(exc).__name__}: {exc}"},
            )
            return destination
        if not source.exists():
            try:
                if tmp_destination.exists():
                    tmp_destination.unlink()
            except Exception:
                pass
            postprocess_log(
                lv,
                "segment_mp4",
                "DEBUG",
                f"TS remux失敗後にTS消失 source={source}",
                {"source": str(source), "output": str(destination), "error": f"{type(exc).__name__}: {exc}"},
            )
            return None
        postprocess_log(
            lv,
            "segment_mp4",
            "INFO",
            f"TS remux失敗、再エンコードへ切替: {source_label}",
            {"source": str(source), "output": str(destination), "error": f"{type(exc).__name__}: {exc}"},
        )
        try:
            if tmp_destination.exists():
                tmp_destination.unlink()
        except Exception:
            pass

    config = load_config()
    video_filter = concat_video_filter_suffix(config)
    tmp_destination = destination.with_name(
        f"{destination.stem}.tmp-encode-{os.getpid()}-{threading.get_ident()}{destination.suffix}"
    )
    if tmp_destination.exists():
        try:
            tmp_destination.unlink()
        except Exception:
            pass
    reencode_cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(source),
    ]
    if video_filter:
        reencode_cmd.extend(["-vf", video_filter])
    reencode_cmd.extend(
        [
            *concat_video_encoder_args(config),
            "-c:a",
            "aac",
            "-ar",
            "48000",
            "-movflags",
            "+faststart",
            str(tmp_destination),
        ]
    )
    try:
        run_subprocess_with_stage_log(
            reencode_cmd,
            lv=lv,
            stage="segment_mp4",
            label=f"TS->MP4 encode {source_label}",
            timeout=max(600, int(duration * 4) + 300),
            progress_total_seconds=duration,
        )
    except Exception:
        if destination.exists() and not force:
            try:
                if tmp_destination.exists():
                    tmp_destination.unlink()
            except Exception:
                pass
            return destination
        if not source.exists():
            try:
                if tmp_destination.exists():
                    tmp_destination.unlink()
            except Exception:
                pass
            return None
        raise
    if destination.exists() and not force:
        try:
            tmp_destination.unlink()
        except Exception:
            pass
        return destination
    tmp_destination.replace(destination)
    return destination


def ensure_recording_segment_mp4(
    segment_path: Path | str,
    *,
    lv: str | None = None,
    force: bool = False,
) -> Path | None:
    """Return an MP4 for a completed recording segment if one can be found or made.

    SlNicoLiveRec may leave either an MP4 or a TS depending on how it ended.
    If an MP4 already exists, use it. If only TS exists, remux/encode it.
    Missing files are ignored because recorder-side conversion may have moved
    or deleted the original before this helper runs.
    """
    source = Path(segment_path)
    suffix = source.suffix.lower()
    if suffix == ".mp4":
        if source.exists():
            postprocess_log(
                lv,
                "segment_mp4",
                "DEBUG",
                f"セグメントMP4確認 既存 mp4={source.name}",
                {"source": str(source), "output": str(source)},
            )
            return source
        postprocess_log(
            lv,
            "segment_mp4",
            "DEBUG",
            f"セグメントMP4確認スキップ ファイルなし source={source}",
            {"source": str(source)},
        )
        return None

    if suffix == ".ts":
        same_name_mp4 = source.with_suffix(".mp4")
        if same_name_mp4.exists() and not force:
            postprocess_log(
                lv,
                "segment_mp4",
                "DEBUG",
                f"セグメントMP4確認 変換済み mp4={same_name_mp4.name}",
                {"source": str(source), "output": str(same_name_mp4)},
            )
            return same_name_mp4
        return convert_recording_ts_to_mp4(source, output_path=same_name_mp4, lv=lv, force=force)

    postprocess_log(
        lv,
        "segment_mp4",
        "DEBUG",
        f"セグメントMP4確認スキップ 対象外拡張子 source={source}",
        {"source": str(source)},
    )
    return None


def update_recording_segment_transcript_state(
    conn: sqlite3.Connection,
    *,
    lv: str,
    broadcaster_id: str,
    segment_path: Path,
    segment_index: int,
    started_at: str,
    ended_at: str,
    duration_seconds: float,
    timeline_start_seconds: float,
    status: str,
    wav_path: Path | None = None,
    mp3_path: Path | None = None,
    model: str = "",
    error: str = "",
) -> None:
    current_time = now_micro()
    stat = segment_path.stat()
    conn.execute(
        """
        INSERT INTO recording_segments
            (lv, broadcaster_id, source_path, target_path, file_type, size_bytes,
             mtime, segment_index, status, started_at, ended_at, duration_seconds,
             timeline_start_seconds, audio_wav_path, audio_mp3_path,
             transcript_status, transcript_started_at, transcript_finished_at,
             transcript_error, transcript_model, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(lv, source_path) DO UPDATE SET
            broadcaster_id = excluded.broadcaster_id,
            file_type = excluded.file_type,
            size_bytes = excluded.size_bytes,
            mtime = excluded.mtime,
            segment_index = excluded.segment_index,
            status = excluded.status,
            started_at = excluded.started_at,
            ended_at = excluded.ended_at,
            duration_seconds = excluded.duration_seconds,
            timeline_start_seconds = excluded.timeline_start_seconds,
            audio_wav_path = CASE WHEN excluded.audio_wav_path <> '' THEN excluded.audio_wav_path ELSE recording_segments.audio_wav_path END,
            audio_mp3_path = CASE WHEN excluded.audio_mp3_path <> '' THEN excluded.audio_mp3_path ELSE recording_segments.audio_mp3_path END,
            transcript_status = excluded.transcript_status,
            transcript_started_at = CASE WHEN excluded.transcript_status = 'running' THEN excluded.transcript_started_at ELSE recording_segments.transcript_started_at END,
            transcript_finished_at = CASE WHEN excluded.transcript_status IN ('done', 'failed', 'skipped') THEN excluded.transcript_finished_at ELSE recording_segments.transcript_finished_at END,
            transcript_error = excluded.transcript_error,
            transcript_model = CASE WHEN excluded.transcript_model <> '' THEN excluded.transcript_model ELSE recording_segments.transcript_model END,
            updated_at = excluded.updated_at
        """,
        (
            lv,
            broadcaster_id,
            str(segment_path),
            str(segment_path),
            segment_path.suffix.lower().lstrip("."),
            int(stat.st_size),
            float(stat.st_mtime),
            int(segment_index),
            "processed" if status == "done" else "audio_ready" if status == "skipped" else "processing" if status == "running" else "failed",
            started_at,
            ended_at,
            float(duration_seconds),
            float(timeline_start_seconds),
            str(wav_path or ""),
            str(mp3_path or ""),
            status,
            current_time if status == "running" else None,
            current_time if status in {"done", "failed", "skipped"} else None,
            error,
            model,
            current_time,
            current_time,
        ),
    )


def process_completed_recording_segment(
    *,
    lv: str,
    broadcaster_id: str,
    segment_path: Path | str,
    started_at: str = "",
    ended_at: str = "",
    target_dir: Path | str | None = None,
    force: bool = False,
    whisper_model: str = "large-v3",
    input_dir: Path | str | None = None,
    transcribe_enabled: bool = True,
    timeline_mode: str = "live",
    segment_paths: list[Path | str] | None = None,
    progress_callback: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Extract and transcribe one closed segment without touching other segments."""
    segment_path = Path(segment_path)
    mp4_path = ensure_recording_segment_mp4(segment_path, lv=lv)
    if not mp4_path:
        raise FileNotFoundError(f"録画セグメントMP4を用意できません: {segment_path}")
    config = load_config()
    final_target_dir = Path(target_dir) if target_dir else broadcast_target_dir(
        lv,
        config,
        broadcaster_id=broadcaster_id or None,
    )
    asset_dir = final_target_dir / "recording_segments"
    asset_dir.mkdir(parents=True, exist_ok=True)
    wav_path = asset_dir / f"{mp4_path.stem}.wav"
    mp3_path = asset_dir / f"{mp4_path.stem}.mp3"

    with segment_transcription_lock(lv):
        timeline_plan = build_recording_segment_timeline_plan(
            input_dir or slnico_storage_root(),
            lv=lv,
            timeline_mode=timeline_mode,
            segment_paths=segment_paths,
        )
        timeline_entry = recording_segment_timeline_entry(timeline_plan, mp4_path)
        segment_index = int((timeline_entry or {}).get("segment_index") or 0)
        timeline_start = float((timeline_entry or {}).get("timeline_start_seconds") or 0.0)
        duration = float((timeline_entry or {}).get("duration_seconds") or probe_media_duration_seconds(mp4_path))
        timeline_end = float(
            (timeline_entry or {}).get("timeline_end_seconds")
            or (timeline_start + duration)
        )
        start_value = str(
            (timeline_entry or {}).get("started_at_iso")
            or started_at
            or (timeline_entry or {}).get("creation_time")
            or ""
        )
        end_value = str(ended_at or "")
        if not end_value and start_value:
            try:
                end_value = (iso_to_datetime(start_value) + timedelta(seconds=duration)).isoformat(timespec="microseconds")
            except Exception:
                end_value = ""

        with connect() as conn:
            existing = conn.execute(
                "SELECT transcript_status, audio_wav_path, audio_mp3_path FROM recording_segments WHERE lv = ? AND source_path = ?",
                (lv, str(mp4_path)),
            ).fetchone()
            if (
                existing
                and str(existing["transcript_status"] or "") == "done"
                and transcribe_enabled
                and not force
                and Path(str(existing["audio_wav_path"] or wav_path)).exists()
                and Path(str(existing["audio_mp3_path"] or mp3_path)).exists()
            ):
                return {
                    "lv": lv,
                    "segment_path": str(mp4_path),
                    "segment_index": segment_index,
                    "timeline_start_seconds": timeline_start,
                    "transcribed": False,
                    "reason": "already_done",
                }
            if (
                existing
                and not transcribe_enabled
                and not force
                and Path(str(existing["audio_wav_path"] or wav_path)).exists()
                and Path(str(existing["audio_mp3_path"] or mp3_path)).exists()
            ):
                return {
                    "lv": lv,
                    "segment_path": str(mp4_path),
                    "segment_index": segment_index,
                    "timeline_start_seconds": timeline_start,
                    "transcribed": False,
                    "reason": "audio_already_done",
                    "wav_path": str(existing["audio_wav_path"] or wav_path),
                    "mp3_path": str(existing["audio_mp3_path"] or mp3_path),
                }
            update_recording_segment_transcript_state(
                conn,
                lv=lv,
                broadcaster_id=broadcaster_id,
                segment_path=mp4_path,
                segment_index=segment_index,
                started_at=start_value,
                ended_at=end_value,
                duration_seconds=duration,
                timeline_start_seconds=timeline_start,
                status="running",
                wav_path=wav_path,
                mp3_path=mp3_path,
            )
            conn.commit()

        try:
            if progress_callback:
                progress_callback(f"音声抽出開始: {mp4_path.name}")
            audio = extract_audio_from_video(mp4_path, wav_path, mp3_path=mp3_path, lv=lv)
            if progress_callback:
                progress_callback(f"音声抽出完了: {mp4_path.name}")
            if not transcribe_enabled:
                with connect() as conn:
                    update_recording_segment_transcript_state(
                        conn,
                        lv=lv,
                        broadcaster_id=broadcaster_id,
                        segment_path=mp4_path,
                        segment_index=segment_index,
                        started_at=start_value,
                        ended_at=end_value,
                        duration_seconds=duration,
                        timeline_start_seconds=timeline_start,
                        status="skipped",
                        wav_path=Path(str(audio["wav_path"])),
                        mp3_path=Path(str(audio["mp3_path"])),
                    )
                    conn.commit()
                return {
                    "lv": lv,
                    "segment_path": str(mp4_path),
                    "segment_index": segment_index,
                    "timeline_start_seconds": timeline_start,
                    "duration_seconds": duration,
                    "wav_path": str(wav_path),
                    "mp3_path": str(mp3_path),
                    "transcribed": False,
                    "reason": "disabled",
                }
            settings = resolve_monitored_broadcaster_transcription_settings(
                lv,
                broadcaster_id=broadcaster_id,
                fallback_model=whisper_model,
            )
            model = str(settings["faster_whisper_model"])
            if progress_callback:
                progress_callback(
                    f"文字起こし開始 engine={settings['engine']} model={model} device=cuda compute=float16"
                )
            common_kwargs = {
                "model_size": model,
                "initial_prompt": str(settings["initial_prompt"]),
                "hotwords": str(settings["hotwords"]),
                "target_dir": final_target_dir,
                "timeline_offset_seconds": timeline_start,
                "timeline_end_seconds": timeline_end,
                "segment_index_base": segment_index * 1_000_000,
                "replace_scope": "source",
                "mark_postprocess_done": False,
            }
            if settings["engine"] == "whisperx":
                transcription = transcribe_audio_with_whisperx(
                    lv,
                    wav_path,
                    diarize=bool(settings["speaker_diarization_enabled"]),
                    min_speakers=int(settings["diarization_min_speakers"]),
                    max_speakers=int(settings["diarization_max_speakers"]),
                    **common_kwargs,
                )
            else:
                transcription = transcribe_audio_with_faster_whisper(
                    lv, wav_path, progress_callback=progress_callback, **common_kwargs
                )
            model_label = f"{settings['engine']}:{model}"
            with connect() as conn:
                update_recording_segment_transcript_state(
                    conn,
                    lv=lv,
                    broadcaster_id=broadcaster_id,
                    segment_path=mp4_path,
                    segment_index=segment_index,
                    started_at=start_value,
                    ended_at=end_value,
                    duration_seconds=duration,
                    timeline_start_seconds=timeline_start,
                    status="done",
                    wav_path=Path(str(audio["wav_path"])),
                    mp3_path=Path(str(audio["mp3_path"])),
                    model=model_label,
                )
                conn.commit()
            return {
                "lv": lv,
                "segment_path": str(mp4_path),
                "segment_index": segment_index,
                "timeline_start_seconds": timeline_start,
                "duration_seconds": duration,
                "wav_path": str(wav_path),
                "mp3_path": str(mp3_path),
                "transcribed": True,
                "transcription": transcription,
            }
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            with connect() as conn:
                update_recording_segment_transcript_state(
                    conn,
                    lv=lv,
                    broadcaster_id=broadcaster_id,
                    segment_path=mp4_path,
                    segment_index=segment_index,
                    started_at=start_value,
                    ended_at=end_value,
                    duration_seconds=duration,
                    timeline_start_seconds=timeline_start,
                    status="failed",
                    wav_path=wav_path if wav_path.exists() else None,
                    mp3_path=mp3_path if mp3_path.exists() else None,
                    error=error,
                )
                conn.commit()
            raise


def ensure_recording_segment_transcriptions(
    lv: str,
    *,
    broadcaster_id: str,
    target_dir: Path | str,
    transcribe: bool,
    whisper_model: str,
    input_dir: Path | str | None = None,
    timeline_mode: str = "live",
    segment_paths: list[Path | str] | None = None,
    progress_callback: Callable[[str], None] | None = None,
) -> list[dict[str, Any]]:
    plan = build_recording_segment_timeline_plan(
        input_dir or slnico_storage_root(),
        lv=lv,
        timeline_mode=timeline_mode,
        segment_paths=segment_paths,
    )
    results: list[dict[str, Any]] = []
    segments = list(plan.get("segments") or [])
    for position, segment in enumerate(segments, 1):
        path = Path(str(segment.get("path") or ""))
        if progress_callback:
            progress_callback(f"録画区間処理 {position}/{len(segments)}: {path.name}")
        mp4_path = ensure_recording_segment_mp4(path, lv=lv)
        if not mp4_path:
            results.append({"segment_path": str(path), "transcribed": False, "reason": "mp4_missing"})
            continue
        try:
            results.append(
                process_completed_recording_segment(
                    lv=lv,
                    broadcaster_id=broadcaster_id,
                    segment_path=mp4_path,
                    started_at=str(segment.get("started_at_iso") or segment.get("creation_time") or ""),
                    target_dir=target_dir,
                    whisper_model=whisper_model,
                    input_dir=input_dir,
                    transcribe_enabled=transcribe,
                    timeline_mode=timeline_mode,
                    segment_paths=segment_paths,
                    progress_callback=progress_callback,
                )
            )
        except Exception as exc:
            results.append(
                {
                    "segment_path": str(mp4_path),
                    "transcribed": False,
                    "reason": "failed",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
    return results


def ffmpeg_concat_file_line(path: Path | str) -> str:
    escaped = str(path).replace("'", "'\\''")
    return f"file '{escaped}'"


def compact_recording_segment_label(path: Path | str) -> str:
    path = Path(path)
    parsed = parse_slnico_segment_filename(path)
    if parsed:
        dt = parsed["started_at"]
        return f"{parsed['lv']} {dt:%m/%d %H:%M:%S}.{parsed['ext']}"
    name = path.name
    if len(name) <= 48:
        return name
    return name[:44] + "..." + path.suffix


def log_concat_input_summary(lv: str | None, plan: dict[str, Any]) -> None:
    segments = list(plan.get("segments") or [])
    gaps = list(plan.get("gaps") or [])
    parts = list(plan.get("parts") or [])
    total_seconds = sum(float(part.get("duration_seconds") or 0.0) for part in parts)
    lines = [
        f"動画数: {len(segments)}本 / gap: {len(gaps)}本 / 入力: {len(parts)} / 合計: {hms_seconds(total_seconds)}",
    ]
    index = 1
    for part in parts:
        duration = hms_seconds(float(part.get("duration_seconds") or 0.0))
        if part.get("type") == "gap":
            lines.append(f"{index:02d} gap {duration}")
        else:
            path = Path(str(part.get("path") or ""))
            start = str(part.get("started_at_iso") or part.get("creation_time") or "")
            lines.append(f"{index:02d} video {duration} {compact_recording_segment_label(path)} {start}".rstrip())
        index += 1
    postprocess_log(lv, "concat_video", "INFO", "録画連結入力を認識\n" + "\n".join(lines))


def concat_slnico_segments_with_gaps(input_dir: Path | str, output_path: Path | str, *, lv: str | None = None) -> dict[str, Any]:
    plan = build_recording_gap_concat_plan(input_dir, lv=lv) if lv else build_slnico_file_gap_plan(input_dir, lv=lv)
    output_path = Path(output_path)
    if not plan["segments"]:
        raise FileNotFoundError(f"録画セグメントが見つかりません: input_dir={input_dir} lv={lv or ''}")
    if plan.get("unmatched_gaps"):
        raise RuntimeError(f"録画gapをセグメント間に配置できません: lv={lv} gaps={len(plan['unmatched_gaps'])}")
    total_duration_seconds = sum(float(part.get("duration_seconds") or 0.0) for part in plan.get("parts", []))
    log_concat_input_summary(lv, plan)
    work_dir = output_path.parent / "_concat_work"
    work_dir.mkdir(parents=True, exist_ok=True)
    concat_files: list[Path] = []
    generated_gaps: list[dict[str, Any]] = []
    segment_video_sizes: dict[str, tuple[int, int]] = {}
    last_segment_video_size: tuple[int, int] | None = None

    def get_segment_video_size(segment_path_value: Any) -> tuple[int, int] | None:
        if not segment_path_value:
            return None
        mp4_path = ensure_recording_segment_mp4(Path(str(segment_path_value)), lv=lv)
        if not mp4_path:
            return None
        key = str(mp4_path).lower()
        if key not in segment_video_sizes:
            segment_video_sizes[key] = probe_media_video_size(mp4_path)
        return segment_video_sizes[key]

    for part in plan["parts"]:
        if part["type"] == "segment":
            segment_path = Path(part["path"])
            mp4_path = ensure_recording_segment_mp4(segment_path, lv=lv)
            if not mp4_path:
                raise FileNotFoundError(f"録画セグメントMP4を用意できません: {segment_path}")
            last_segment_video_size = get_segment_video_size(mp4_path)
            if last_segment_video_size:
                part["video_size"] = {
                    "width": last_segment_video_size[0],
                    "height": last_segment_video_size[1],
                }
            part["original_path"] = str(segment_path)
            part["path"] = str(mp4_path)
            part["mp4_path"] = str(mp4_path)
            concat_files.append(mp4_path)
            continue
        gap_path = work_dir / f"{part['lv']}_gap_{part['gap_index']:03d}_{part['duration_us']}.mp4"
        reference_video_size = last_segment_video_size
        for candidate_path in (part.get("previous_path"), part.get("next_path")):
            if reference_video_size:
                break
            reference_video_size = get_segment_video_size(candidate_path)
        create_black_silent_gap_video(
            gap_path,
            float(part["duration_seconds"]),
            lv=lv,
            reference_video_size=reference_video_size,
        )
        if reference_video_size:
            part["gap_video_size"] = {
                "width": reference_video_size[0],
                "height": reference_video_size[1],
            }
        part["gap_video_path"] = str(gap_path)
        concat_files.append(gap_path)
        generated_gaps.append(part)
    list_path = work_dir / f"{plan['lv'] or 'concat'}_concat.txt"
    lines = [ffmpeg_concat_file_line(path) for path in concat_files]
    list_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    input_args: list[str] = []
    filter_parts: list[str] = []
    for index, path in enumerate(concat_files):
        input_args.extend(["-i", str(path)])
        filter_parts.append(f"[{index}:v:0][{index}:a:0]")
    video_filter = concat_video_filter_suffix()
    if video_filter:
        filter_complex = "".join(filter_parts) + f"concat=n={len(concat_files)}:v=1:a=1[cv][a];[cv]{video_filter}[v]"
    else:
        filter_complex = "".join(filter_parts) + f"concat=n={len(concat_files)}:v=1:a=1[v][a]"
    run_subprocess_with_stage_log(
        [
            "ffmpeg",
            "-y",
            *input_args,
            "-filter_complex",
            filter_complex,
            "-map",
            "[v]",
            "-map",
            "[a]",
            *concat_video_encoder_args(),
            "-c:a",
            "aac",
            "-ar",
            "48000",
            "-avoid_negative_ts",
            "make_zero",
            str(output_path),
        ],
        lv=lv,
        stage="concat_video",
        label=f"録画連結 inputs={len(concat_files)} output={output_path.name}",
        timeout=600,
        progress_total_seconds=total_duration_seconds,
    )
    if lv and generated_gaps:
        current_time = now_micro()
        with connect() as conn:
            for gap in generated_gaps:
                conn.execute(
                    """
                    UPDATE recording_gaps
                    SET status = ?, generated_video_path = ?, updated_at = ?
                    WHERE lv = ?
                      AND gap_start = ?
                      AND gap_end = ?
                    """,
                    (
                        "generated",
                        str(gap["gap_video_path"]),
                        current_time,
                        lv,
                        str(gap["gap_start"]),
                        str(gap["gap_end"]),
                    ),
                )
            conn.commit()
    plan["concat_list_path"] = str(list_path)
    plan["output_path"] = str(output_path)
    return plan


def register_recording_segments(
    conn: sqlite3.Connection,
    lv: str,
    broadcaster_id: str = "",
    *,
    storage_root: Path | str | None = None,
    timeline_mode: str = "live",
    segment_paths: list[Path | str] | None = None,
) -> list[dict[str, Any]]:
    config = load_config()
    target_dir = broadcast_target_dir(lv, config, broadcaster_id=broadcaster_id or None)
    target_dir.mkdir(parents=True, exist_ok=True)
    timeline_plan = build_recording_segment_timeline_plan(
        storage_root or slnico_storage_root(),
        lv=lv,
        conn=conn,
        timeline_mode=timeline_mode,
        segment_paths=segment_paths,
    )
    rows: list[dict[str, Any]] = []
    current_time = now_micro()
    for segment in timeline_plan.get("segments") or []:
        source = Path(str(segment.get("path") or ""))
        if not source.exists():
            continue
        stat = source.stat()
        target = target_dir / source.name
        started_at = segment.get("started_at_iso") or segment.get("creation_time") or segment.get("started_at")
        if isinstance(started_at, datetime):
            started_at = started_at.isoformat(timespec="microseconds")
        duration_seconds = max(0.0, float(segment.get("duration_seconds") or 0.0))
        ended_at = ""
        try:
            ended_at = (
                iso_to_datetime(str(started_at)) + timedelta(seconds=duration_seconds)
            ).isoformat(timespec="microseconds")
        except Exception:
            ended_at = ""
        row = {
            "lv": lv,
            "broadcaster_id": broadcaster_id,
            "source_path": str(source),
            "target_path": str(target),
            "file_type": source.suffix.lower().lstrip("."),
            "size_bytes": stat.st_size,
            "mtime": stat.st_mtime,
            "segment_index": int(segment.get("segment_index") or 0),
            "status": "found",
            "started_at": str(started_at or ""),
            "ended_at": ended_at,
            "duration_seconds": duration_seconds,
            "timeline_start_seconds": float(segment.get("timeline_start_seconds") or 0.0),
        }
        conn.execute(
            """
            INSERT INTO recording_segments
                (lv, broadcaster_id, source_path, target_path, file_type, size_bytes,
                 mtime, segment_index, status, started_at, ended_at, duration_seconds,
                 timeline_start_seconds, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(lv, source_path) DO UPDATE SET
                broadcaster_id = excluded.broadcaster_id,
                target_path = excluded.target_path,
                file_type = excluded.file_type,
                size_bytes = excluded.size_bytes,
                mtime = excluded.mtime,
                segment_index = excluded.segment_index,
                status = CASE
                    WHEN recording_segments.transcript_status = 'done' THEN recording_segments.status
                    ELSE excluded.status
                END,
                started_at = excluded.started_at,
                ended_at = excluded.ended_at,
                duration_seconds = excluded.duration_seconds,
                timeline_start_seconds = excluded.timeline_start_seconds,
                updated_at = excluded.updated_at
            """,
            (
                row["lv"],
                row["broadcaster_id"],
                row["source_path"],
                row["target_path"],
                row["file_type"],
                row["size_bytes"],
                row["mtime"],
                row["segment_index"],
                row["status"],
                row["started_at"],
                row["ended_at"],
                row["duration_seconds"],
                row["timeline_start_seconds"],
                current_time,
                current_time,
            ),
        )
        rows.append(row)
    return rows


def register_recording_gaps_from_events(
    conn: sqlite3.Connection,
    lv: str,
    *,
    storage_root: Path | str | None = None,
    timeline_mode: str = "live",
    segment_paths: list[Path | str] | None = None,
) -> list[dict[str, Any]]:
    """Persist canonical media-clock gaps; recorder events remain audit metadata."""
    gaps = build_recording_silent_gap_plan(
        conn,
        lv,
        storage_root=storage_root,
        timeline_mode=timeline_mode,
        segment_paths=segment_paths,
    )
    current_time = now_micro()
    conn.execute(
        """
        DELETE FROM recording_gaps
        WHERE lv = ?
        """,
        (lv,),
    )
    for gap in gaps:
        conn.execute(
            """
            INSERT INTO recording_gaps
                (lv, gap_start, gap_end, duration_us, fill_type, status,
                 generated_video_path, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(lv, gap_start, gap_end) DO UPDATE SET
                duration_us = excluded.duration_us,
                fill_type = excluded.fill_type,
                updated_at = excluded.updated_at
            """,
            (
                lv,
                gap["gap_start"],
                gap["gap_end"],
                int(gap["duration_us"]),
                str(gap.get("fill") or "black_silent_video"),
                "pending",
                "",
                current_time,
                current_time,
            ),
        )
    return gaps


def enqueue_postprocess_job(conn: sqlite3.Connection, lv: str, stage: str) -> None:
    current_time = now_micro()
    conn.execute(
        """
        INSERT INTO postprocess_jobs
            (lv, stage, status, created_at, updated_at)
        VALUES (?, ?, 'queued', ?, ?)
        ON CONFLICT(lv, stage) DO UPDATE SET
            status = CASE
                WHEN postprocess_jobs.status IN ('done', 'running') THEN postprocess_jobs.status
                ELSE 'queued'
            END,
            updated_at = excluded.updated_at
        """,
        (lv, stage, current_time, current_time),
    )


def enqueue_finalize_pipeline(conn: sqlite3.Connection, lv: str) -> None:
    for stage in (
        "collect_segments",
        "make_gaps",
        "concat_video",
        "extract_wav",
        "transcribe",
        "encode_mp3",
        "archive_steps",
    ):
        enqueue_postprocess_job(conn, lv, stage)


def prepare_recording_finalize_inputs(lv: str, broadcaster_id: str = "") -> dict[str, Any]:
    config = load_config()
    target_dir = broadcast_target_dir(lv, config, broadcaster_id=broadcaster_id or None)
    with connect() as conn:
        segments = register_recording_segments(conn, lv, broadcaster_id)
        gaps = register_recording_gaps_from_events(conn, lv)
        legacy = export_legacy_archive_files_from_ndgr(conn, lv, target_dir=target_dir)
        transcript = export_legacy_transcript_file_from_db(conn, lv, target_dir=target_dir)
        enqueue_finalize_pipeline(conn, lv)
        conn.execute(
            """
            UPDATE recording_jobs
            SET status = CASE WHEN status = 'finalizing' THEN status ELSE 'finalize_queued' END,
                updated_at = ?
            WHERE lv = ?
            """,
            (now_micro(), lv),
        )
        conn.commit()
    return {
        "lv": lv,
        "segments": len(segments),
        "gaps": len(gaps),
        "legacy_files": legacy,
        "legacy_transcript": transcript,
        "finalize_queued": True,
    }


def update_postprocess_job(
    conn: sqlite3.Connection,
    lv: str,
    stage: str,
    status: str,
    *,
    error: str = "",
) -> None:
    current_time = now_micro()
    conn.execute(
        """
        INSERT INTO postprocess_jobs
            (lv, stage, status, started_at, finished_at, error, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(lv, stage) DO UPDATE SET
            status = excluded.status,
            started_at = CASE WHEN excluded.status = 'running' THEN excluded.started_at ELSE postprocess_jobs.started_at END,
            finished_at = CASE WHEN excluded.status IN ('done', 'failed') THEN excluded.finished_at ELSE postprocess_jobs.finished_at END,
            error = excluded.error,
            updated_at = excluded.updated_at
        """,
        (
            lv,
            stage,
            status,
            current_time if status == "running" else None,
            current_time if status in {"done", "failed"} else None,
            error,
            current_time,
            current_time,
        ),
    )
    conn.execute(
        """
        INSERT INTO postprocess_logs
            (lv, stage, level, message, payload_json, created_at)
        VALUES (?, ?, 'DEBUG', ?, ?, ?)
        """,
        (
            lv,
            stage,
            f"stage={status}",
            json.dumps({"status": status, "error": error} if error else {"status": status}, ensure_ascii=False),
            current_time,
        ),
    )


def staged_media_output_path(output_path: Path | str) -> Path:
    output = Path(output_path)
    return output.with_name(
        f".{output.stem}.{os.getpid()}.{threading.get_ident()}.part{output.suffix}"
    )


def extract_audio_from_video(
    video_path: Path | str,
    audio_path: Path | str,
    *,
    mp3_path: Path | str | None = None,
    lv: str | None = None,
) -> dict[str, Any]:
    video_path = Path(video_path)
    audio_path = Path(audio_path)
    audio_path.parent.mkdir(parents=True, exist_ok=True)
    alignment = probe_media_audio_timeline(video_path)
    video_duration = float(alignment["format_duration_seconds"])
    wav_staged = staged_media_output_path(audio_path)
    mp3_output = Path(mp3_path) if mp3_path else None
    mp3_staged = staged_media_output_path(mp3_output) if mp3_output else None
    wav_staged.unlink(missing_ok=True)
    if mp3_staged is not None:
        mp3_output.parent.mkdir(parents=True, exist_ok=True)
        mp3_staged.unlink(missing_ok=True)
    postprocess_log(
        lv,
        "extract_wav",
        "DEBUG",
        (
            f"音声時間軸補正 format={video_duration:.6f}s "
            f"audio_start={float(alignment['audio_start_time']):.6f}s "
            f"leading_samples={int(alignment['leading_silence_samples'])} "
            f"head_trim_samples={int(alignment['head_trim_samples'])} "
            f"target_samples={int(alignment['target_samples'])}"
        ),
        alignment,
    )
    try:
        run_subprocess_with_stage_log(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(video_path),
                "-map",
                "0:a:0",
                "-vn",
                "-af",
                audio_alignment_filter(alignment),
                "-c:a",
                "pcm_s16le",
                str(wav_staged),
            ],
            lv=lv,
            stage="extract_wav",
            label=f"音声抽出 wav={audio_path.name}",
            timeout=600,
            progress_total_seconds=video_duration,
        )
        wav_duration = probe_media_duration_seconds(wav_staged)
        if abs(wav_duration - video_duration) > 0.01:
            raise RuntimeError(
                f"extracted WAV duration drift is too large: {wav_duration - video_duration:+.6f}s "
                f"(expected={video_duration:.6f}s actual={wav_duration:.6f}s)"
            )
        if mp3_output is not None and mp3_staged is not None:
            run_subprocess_with_stage_log(
                [
                    "ffmpeg",
                    "-y",
                    "-i",
                    str(wav_staged),
                    "-c:a",
                    "libmp3lame",
                    "-b:a",
                    "192k",
                    str(mp3_staged),
                ],
                lv=lv,
                stage="encode_mp3",
                label=f"MP3作成 mp3={mp3_output.name}",
                timeout=600,
                progress_total_seconds=video_duration,
            )
            mp3_duration = probe_media_duration_seconds(mp3_staged)
            if abs(mp3_duration - video_duration) > 0.25:
                raise RuntimeError(
                    f"segment MP3 duration drift is too large: {mp3_duration - video_duration:+.6f}s "
                    f"(expected={video_duration:.6f}s actual={mp3_duration:.6f}s)"
                )
        os.replace(wav_staged, audio_path)
        if mp3_output is not None and mp3_staged is not None:
            os.replace(mp3_staged, mp3_output)
    except Exception:
        wav_staged.unlink(missing_ok=True)
        if mp3_staged is not None:
            mp3_staged.unlink(missing_ok=True)
        raise

    result = {
        "wav_path": str(audio_path),
        "audio_alignment": alignment,
    }
    if mp3_output is not None:
        result["mp3_path"] = str(mp3_output)
    return result


def create_silent_gap_mp3(
    path: Path | str,
    duration_seconds: float,
    *,
    lv: str | None = None,
) -> Path:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    duration = max(0.05, float(duration_seconds))
    run_subprocess_with_stage_log(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=channel_layout=mono:sample_rate=16000",
            "-t",
            f"{duration:.6f}",
            "-c:a",
            "libmp3lame",
            "-b:a",
            "192k",
            str(output),
        ],
        lv=lv,
        stage="concat_audio",
        label=f"無音MP3生成 {output.name} duration={duration:.3f}s",
        timeout=max(30, int(duration) + 30),
        progress_total_seconds=duration,
    )
    return output


def concat_recording_segment_audio(
    lv: str,
    plan: dict[str, Any],
    output_path: Path | str,
) -> dict[str, Any]:
    """Create only the final MP3; original segment videos remain independent."""
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    work_dir = output.parent / "_audio_concat_work"
    work_dir.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT source_path, audio_mp3_path
            FROM recording_segments
            WHERE lv = ?
              AND transcript_status IN ('done', 'skipped')
              AND COALESCE(audio_mp3_path, '') <> ''
            """,
            (lv,),
        ).fetchall()
    audio_by_segment = {
        recording_segment_identity(str(row["source_path"] or "")): Path(str(row["audio_mp3_path"] or ""))
        for row in rows
        if str(row["audio_mp3_path"] or "").strip()
    }
    inputs: list[Path] = []
    input_durations: list[float] = []
    generated_gaps: list[Path] = []
    for part_index, part in enumerate(plan.get("parts") or []):
        duration = max(0.0, float(part.get("duration_seconds") or 0.0))
        if part.get("type") == "gap":
            if duration <= 0:
                continue
            gap_path = work_dir / f"{lv}_gap_{part_index:03d}_{int(duration * 1_000_000)}.mp3"
            create_silent_gap_mp3(gap_path, duration, lv=lv)
            inputs.append(gap_path)
            input_durations.append(duration)
            generated_gaps.append(gap_path)
            continue
        identity = recording_segment_identity(str(part.get("path") or ""))
        audio_path = audio_by_segment.get(identity)
        if audio_path is None or not audio_path.exists():
            raise FileNotFoundError(f"録画区間MP3が見つかりません: {part.get('path')}")
        actual_segment_duration = probe_media_duration_seconds(audio_path)
        segment_drift = actual_segment_duration - duration
        if duration > 0 and abs(segment_drift) > 0.25:
            raise RuntimeError(
                f"録画区間MP3の長さが不正です: {audio_path.name} "
                f"drift={segment_drift:+.6f}s "
                f"(expected={duration:.6f}s actual={actual_segment_duration:.6f}s)"
            )
        inputs.append(audio_path)
        input_durations.append(duration)
    if not inputs:
        raise FileNotFoundError(f"連結する録画区間MP3がありません: {lv}")
    expected_duration = float(plan.get("total_duration_seconds") or sum(input_durations))
    expected_samples = max(1, round(expected_duration * 16000))
    filter_parts: list[str] = []
    concat_inputs: list[str] = []
    for index, duration in enumerate(input_durations):
        part_samples = max(1, round(duration * 16000))
        label = f"a{index}"
        filter_parts.append(
            f"[{index}:a:0]aresample=16000,"
            "aformat=sample_fmts=s16:channel_layouts=mono,"
            f"apad=whole_len={part_samples},atrim=end_sample={part_samples},"
            f"asetpts=N/SR/TB[{label}]"
        )
        concat_inputs.append(f"[{label}]")
    filter_parts.append(
        f"{''.join(concat_inputs)}concat=n={len(inputs)}:v=0:a=1[joined]"
    )
    filter_parts.append(
        f"[joined]apad=whole_len={expected_samples},"
        f"atrim=end_sample={expected_samples},asetpts=N/SR/TB[a]"
    )
    staged_output = staged_media_output_path(output)
    staged_output.unlink(missing_ok=True)
    actual_duration: float | None = None
    duration_drift: float | None = None
    try:
        run_subprocess_with_stage_log(
            [
                "ffmpeg",
                "-y",
                *[arg for path in inputs for arg in ("-i", str(path))],
                "-filter_complex",
                ";".join(filter_parts),
                "-map",
                "[a]",
                "-c:a",
                "libmp3lame",
                "-b:a",
                "192k",
                str(staged_output),
            ],
            lv=lv,
            stage="concat_audio",
            label=f"録画区間MP3連結 inputs={len(inputs)} output={output.name}",
            timeout=600,
            progress_total_seconds=expected_duration,
        )
        if not staged_output.exists() or staged_output.stat().st_size <= 0:
            raise RuntimeError(f"final MP3 was not created: {staged_output}")
        actual_duration = probe_media_duration_seconds(staged_output)
        duration_drift = actual_duration - expected_duration
        drift_level = "INFO" if abs(duration_drift) <= 0.25 else "ERROR"
        postprocess_log(
            lv,
            "concat_audio",
            drift_level,
            (
                f"MP3時間軸検証 expected={expected_duration:.6f}s "
                f"actual={actual_duration:.6f}s drift={duration_drift:+.6f}s"
            ),
            {
                "expected_duration_seconds": expected_duration,
                "actual_duration_seconds": actual_duration,
                "duration_drift_seconds": duration_drift,
                "input_count": len(inputs),
            },
        )
        if abs(duration_drift) > 0.25:
            raise RuntimeError(
                f"final MP3 duration drift is too large: {duration_drift:+.6f}s "
                f"(expected={expected_duration:.6f}s actual={actual_duration:.6f}s)"
            )
        os.replace(staged_output, output)
    except Exception:
        staged_output.unlink(missing_ok=True)
        raise
    return {
        "mp3_path": str(output),
        "input_mp3_paths": [str(path) for path in inputs],
        "generated_gap_mp3_paths": [str(path) for path in generated_gaps],
        "total_duration_seconds": expected_duration,
        "actual_duration_seconds": actual_duration,
        "duration_drift_seconds": duration_drift,
    }


def mark_stage_done(lv: str, stage: str) -> None:
    with connect() as conn:
        update_postprocess_job(conn, lv, stage, "done")
        conn.commit()


def mark_stage_failed(lv: str, stage: str, error: str) -> None:
    with connect() as conn:
        update_postprocess_job(conn, lv, stage, "failed", error=error)
        conn.commit()


def build_legacy_archiver_config(config: Config | None = None) -> dict[str, Any]:
    """Return a minimal config shape accepted by copied legacy_archiver steps."""
    config = config or load_config()
    default_engine = "codex_exec" if config.codex_exec_enabled else "openai"
    legacy_config = {
        "display_name": str(config.recording_account_id or DEFAULT_RECORDING_ACCOUNT_ID),
        "api_settings": {
            "openai_api_key": config.openai_api_key or os.environ.get("OPENAI_API_KEY", ""),
            "google_api_key": config.google_api_key or os.environ.get("GOOGLE_API_KEY", ""),
            "imgur_api_key": config.imgur_api_key or os.environ.get("IMGUR_API_KEY", ""),
            "huggingface_token": config.huggingface_token or os.environ.get("HF_TOKEN", "") or os.environ.get("HUGGINGFACE_TOKEN", ""),
            "suno_api_key": config.suno_api_key or os.environ.get("SUNO_API_KEY", ""),
            "ai_model": os.environ.get("NICONICO_AI_MODEL", "openai-gpt4o"),
            "summary_ai_model": os.environ.get("NICONICO_SUMMARY_AI_MODEL", "openai-gpt4o"),
            "conversation_ai_model": os.environ.get("NICONICO_CONVERSATION_AI_MODEL", "openai-gpt4o"),
        },
        "ai_features": {
            "enable_summary_text": bool(config.enable_summary_text),
            "enable_ai_music": bool(config.enable_ai_music),
            "enable_summary_image": bool(config.enable_summary_image),
            "enable_ai_conversation": bool(config.enable_ai_conversation),
        },
        "display_features": {
            "enable_emotion_scores": bool(config.enable_emotion_scores),
            "enable_word_ranking": bool(config.enable_word_extract),
            "enable_thumbnails": bool(config.enable_timeline_thumbnails),
            "thumbnail_width": max(1, int(config.timeline_thumbnail_width or 80)),
            "thumbnail_height": max(1, int(config.timeline_thumbnail_height or 60)),
            "enable_audio_timeline": bool(config.enable_audio_timeline),
            "enable_timeline_html": bool(config.enable_timeline_html),
            "enable_comment_ranking": bool(config.enable_comment_ranking),
        },
        "audio_settings": {
            "use_gpu": True,
            "whisper_model": "large-v3",
            "cpu_threads": 8,
            "beam_size": 5,
        },
        "music_settings": {
            "model": str(config.suno_music_model or "V4"),
            "style": str(config.suno_music_style or "J-Pop, Upbeat"),
            "instrumental": bool(config.suno_music_instrumental),
        },
        "image_settings": {
            "model": str(config.image_generation_model or "gpt-image-2"),
            "size": "1024x1024",
            "quality": str(config.image_generation_quality or "medium"),
        },
        "upload_settings": {
            "enable_auto_upload": bool(config.enable_archive_auto_upload),
            "target_id": str(config.archive_upload_target_id or "lolipop-main"),
            "username": str(config.archive_upload_username or ""),
            "password": str(config.archive_upload_password or ""),
            "remote_directory_template": str(
                config.archive_upload_remote_dir_template or "niconico/{account_id}"
            ),
            "python_exe": str(config.archive_upload_python_exe),
            "cli_path": str(config.archive_upload_cli_path),
            "http_verify": bool(config.archive_upload_http_verify),
            "timeout_seconds": max(30, int(config.archive_upload_timeout_seconds or 900)),
            "auto_start_credentials_api": bool(
                config.archive_upload_auto_start_credentials_api
            ),
        },
        "ai_prompts": {
            "summary_prompt": config.summary_prompt
            or "次の生放送の文字起こしを、重要な話題・流れ・印象的な発言が分かるように日本語で要約してください。",
            "summary_chunk_size": int(config.summary_chunk_size or 100000),
            "summary_chunk_prompt": config.summary_chunk_prompt
            or "以下は配信の一部です。この部分を要約してください：",
            "summary_final_prompt": config.summary_final_prompt
            or "以下は配信の各部分の要約です。これらを統合して、配信全体の包括的な要約を作成してください：",
            "image_prompt": config.image_prompt
            or "次の文章は、ある生放送の要約です。この生放送の抽象的なイメージを生成してください:",
            "intro_conversation_prompt": config.intro_conversation_prompt
            or "配信開始前の会話として、以下の内容について話し合います:",
            "outro_conversation_prompt": config.outro_conversation_prompt
            or "配信終了後の振り返りとして、以下の内容について話し合います:",
            "character1_name": config.character1_name or DEFAULT_CHARACTER1_NAME,
            "character1_image_url": config.character1_image_url or DEFAULT_CHARACTER1_IMAGE_URL,
            "character1_fullbody_image_url": config.character1_fullbody_image_url or DEFAULT_CHARACTER1_FULLBODY_IMAGE_URL,
            "character1_image_flip": False,
            "character1_personality": config.character1_personality or "ボケ役で標準語を話す明るい女の子",
            "character2_name": config.character2_name or DEFAULT_CHARACTER2_NAME,
            "character2_image_url": config.character2_image_url or DEFAULT_CHARACTER2_IMAGE_URL,
            "character2_fullbody_image_url": config.character2_fullbody_image_url or DEFAULT_CHARACTER2_FULLBODY_IMAGE_URL,
            "character2_image_flip": False,
            "character2_personality": config.character2_personality or "ツッコミ役で関西弁を話すしっかり者の女の子",
            "conversation_turns": int(config.conversation_turns or 5),
        },
        "codex_exec": {
            "enabled": bool(config.codex_exec_enabled),
            "provider": str(config.codex_exec_provider or "codex"),
            "command": str(config.codex_exec_command or "codex"),
            "cwd": str(config.codex_exec_cwd or ROOT),
            "timeout_seconds": int(config.codex_exec_timeout_seconds or 3600),
            "model": str(config.codex_exec_model or ""),
            "effort": str(config.codex_exec_effort or ""),
            "extra_args": list(config.codex_exec_extra_args or []),
        },
        "ai_task_engines": {
            "summary": default_engine,
            "conversation": default_engine,
            "special_user_summary": default_engine,
        },
        "tags": {},
        "special_users": [user["user_id"] for user in list_special_users() if user.get("enabled", 1)],
        "special_users_config": {
            "default_analysis_enabled": False,
            "default_analysis_ai_model": "openai-gpt4o",
            "default_analysis_prompt": "",
            "default_template": "user_detail.html",
            "users": {},
        },
    }
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT user_id, label, note, analysis_model, analysis_api_key,
                   analysis_engine, analysis_effort, analysis_session_id
            FROM special_users
            WHERE enabled = 1
            ORDER BY user_id
            """
        ).fetchall()
    default_analysis_prompt = (
        "このユーザーのコメントを時系列に要約し、主な話題、感情傾向、"
        "配信者や他の視聴者との関わり方を、根拠のない断定を避けて分析してください。"
    )
    legacy_config["special_users_config"]["users"] = {
        str(row["user_id"]): {
            "user_id": str(row["user_id"]),
            "display_name": str(row["label"] or "").strip() or f"ユーザー{row['user_id']}",
            "analysis_enabled": True,
            "analysis_engine": str(row["analysis_engine"] or default_engine),
            "analysis_ai_model": str(row["analysis_model"] or "").strip(),
            "analysis_api_key": str(row["analysis_api_key"] or ""),
            "analysis_effort": str(row["analysis_effort"] or "medium"),
            "analysis_session_id": str(row["analysis_session_id"] or "").strip(),
            "analysis_prompt": default_analysis_prompt,
            "template": "user_detail.html",
            "description": str(row["note"] or ""),
            "tags": [],
        }
        for row in rows
        if str(row["user_id"] or "").strip()
    }
    return legacy_config


def get_monitored_broadcaster_ai_task_engines(lv: str, config: Config | None = None) -> dict[str, str]:
    config = config or load_config()
    default_engine = "codex_exec" if config.codex_exec_enabled else "openai"
    defaults = {
        "summary": default_engine,
        "conversation": default_engine,
        "special_user_summary": default_engine,
    }
    with connect() as conn:
        row = conn.execute(
            """
            SELECT m.custom_settings_enabled, m.summary_engine, m.ai_conversation_engine, m.special_user_summary_engine
            FROM broadcasts b
            JOIN monitored_broadcasters m ON m.broadcaster_id = b.broadcaster_id
            WHERE b.lv = ?
            LIMIT 1
            """,
            (lv,),
        ).fetchone()
        if not row:
            row = conn.execute(
                """
                SELECT m.custom_settings_enabled, m.summary_engine, m.ai_conversation_engine, m.special_user_summary_engine
                FROM recording_jobs r
                JOIN monitored_broadcasters m ON m.broadcaster_id = r.broadcaster_id
                WHERE r.lv = ?
                LIMIT 1
                """,
                (lv,),
            ).fetchone()
    if not row or not int(row["custom_settings_enabled"] or 0):
        return defaults
    return {
        "summary": str(row["summary_engine"] or defaults["summary"]),
        "conversation": str(row["ai_conversation_engine"] or defaults["conversation"]),
        "special_user_summary": str(row["special_user_summary_engine"] or defaults["special_user_summary"]),
    }


def apply_monitored_broadcaster_feature_overrides(lv: str, legacy_config: dict[str, Any]) -> dict[str, Any]:
    """Apply settings belonging to the broadcaster that owns ``lv``.

    Archive tag candidates are always broadcaster-scoped.  Feature switches still
    honour ``custom_settings_enabled`` so adding tags cannot accidentally enable
    unrelated per-broadcaster overrides.
    """
    with connect() as conn:
        row = conn.execute(
            """
            SELECT m.*
            FROM broadcasts b
            JOIN monitored_broadcasters m ON m.broadcaster_id = b.broadcaster_id
            WHERE b.lv = ?
            LIMIT 1
            """,
            (lv,),
        ).fetchone()
        if not row:
            row = conn.execute(
                """
                SELECT m.*
                FROM recording_jobs r
                JOIN monitored_broadcasters m ON m.broadcaster_id = r.broadcaster_id
                WHERE r.lv = ?
                LIMIT 1
                """,
                (lv,),
            ).fetchone()
    if not row:
        return legacy_config

    tag_entries = parse_archive_tag_entries(row["archive_tags"])
    legacy_config["tags"] = tag_entries["tags"]
    legacy_config["tag_aliases"] = tag_entries["aliases"]
    # Saved broadcaster prompts are basic per-account context and must apply
    # even when the optional custom feature/engine overrides are disabled.
    ai_prompts = legacy_config.setdefault("ai_prompts", {})
    for key in (
        "summary_prompt",
        "image_prompt",
        "music_prompt",
        "intro_conversation_prompt",
        "outro_conversation_prompt",
        "character1_name",
        "character1_image_url",
        "character1_fullbody_image_url",
        "character1_personality",
        "character2_name",
        "character2_image_url",
        "character2_fullbody_image_url",
        "character2_personality",
    ):
        text = str(row[key] or "").strip()
        if text:
            ai_prompts[key] = text
    legacy_config.setdefault("upload_settings", {})["enable_auto_upload"] = bool(
        row["html_upload_enabled"]
    )
    if not int(row["custom_settings_enabled"] or 0):
        return legacy_config

    legacy_config.setdefault("ai_features", {}).update(
        {
            "enable_summary_text": bool(row["summary_enabled"]),
            "enable_ai_music": bool(row["music_enabled"]),
            "enable_summary_image": bool(row["abstract_image_enabled"]),
            "enable_ai_conversation": bool(row["ai_conversation_enabled"]),
        }
    )
    legacy_config.setdefault("display_features", {}).update(
        {
            "enable_emotion_scores": bool(row["emotion_score_enabled"]),
            "enable_word_ranking": bool(row["word_extract_enabled"]),
            "enable_thumbnails": bool(row["thumbnail_10sec_enabled"]),
            "enable_audio_timeline": bool(row["audio_timeline_enabled"]),
            "enable_timeline_html": bool(row["timeline_enabled"]),
            "enable_comment_ranking": bool(row["ranking_enabled"]),
        }
    )
    legacy_config["ai_task_engines"] = {
        "summary": str(row["summary_engine"] or legacy_config.get("ai_task_engines", {}).get("summary", "codex_exec")),
        "conversation": str(row["ai_conversation_engine"] or legacy_config.get("ai_task_engines", {}).get("conversation", "codex_exec")),
        "special_user_summary": str(
            row["special_user_summary_engine"]
            or legacy_config.get("ai_task_engines", {}).get("special_user_summary", "codex_exec")
        ),
    }
    return legacy_config


def normalize_archive_tags(value: Any) -> list[str]:
    """Normalize a broadcaster's one-tag-per-line setting without cross-account defaults."""
    return parse_archive_tag_entries(value)["tags"]


def parse_archive_tag_entries(value: Any) -> dict[str, Any]:
    """Parse `recognition text => canonical tag` entries."""
    if isinstance(value, dict):
        values = [f"{key} => {target}" for key, target in value.items()]
    elif isinstance(value, (list, tuple, set)):
        values = value
    else:
        values = str(value or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    tags: list[str] = []
    hotwords: list[str] = []
    aliases: dict[str, str] = {}
    for item in values:
        text = str(item or "").strip()
        if not text:
            continue
        if "=>" in text:
            recognition, canonical = (part.strip() for part in text.split("=>", 1))
        else:
            recognition = canonical = text
        if not recognition or not canonical:
            continue
        if recognition not in hotwords:
            hotwords.append(recognition)
        if canonical not in tags:
            tags.append(canonical)
        if recognition != canonical:
            aliases[recognition] = canonical
    return {"tags": tags, "hotwords": hotwords, "aliases": aliases}


def resolve_monitored_broadcaster_transcription_settings(
    lv: str,
    *,
    broadcaster_id: str = "",
    fallback_model: str = "large-v3",
) -> dict[str, Any]:
    row = None
    with connect() as conn:
        if broadcaster_id:
            row = conn.execute(
                """
                SELECT *
                FROM monitored_broadcasters
                WHERE broadcaster_id = ?
                LIMIT 1
                """,
                (str(broadcaster_id),),
            ).fetchone()
        if row is None:
            row = conn.execute(
                """
                SELECT m.*
                FROM recording_jobs r
                JOIN monitored_broadcasters m ON m.broadcaster_id = r.broadcaster_id
                WHERE r.lv = ?
                LIMIT 1
                """,
                (str(lv),),
            ).fetchone()
        if row is None:
            row = conn.execute(
                """
                SELECT m.*
                FROM broadcasts b
                JOIN monitored_broadcasters m ON m.broadcaster_id = b.broadcaster_id
                WHERE b.lv = ?
                LIMIT 1
                """,
                (str(lv),),
            ).fetchone()
    default_prompt = "これはニコニコ生放送の録画音声です"
    initial_prompt = (
        str(row["transcription_initial_prompt"] or "").strip()
        if row is not None and "transcription_initial_prompt" in row.keys()
        else ""
    ) or default_prompt
    # This switch only controls speech-recognition hints. Tag aliases and
    # index_person_aliases.json remain active independently.
    hotwords_enabled = (
        bool(row["transcription_hotwords_enabled"])
        if row is not None and "transcription_hotwords_enabled" in row.keys()
        else True
    )
    hotwords = (
        " ".join(parse_archive_tag_entries(row["archive_tags"])["hotwords"])
        if row is not None and hotwords_enabled
        else ""
    )
    if row is None or not int(row["custom_settings_enabled"] or 0):
        return {
            "source": "default",
            "custom_settings_enabled": False,
            "engine": "faster-whisper",
            "faster_whisper_model": fallback_model,
            "fw_model": fallback_model,
            "whisperx_model": fallback_model,
            "whisperx_enabled": False,
            "speaker_diarization_enabled": False,
            "diarization_min_speakers": 1,
            "diarization_max_speakers": 4,
            "initial_prompt": initial_prompt,
            "hotwords_enabled": hotwords_enabled,
            "hotwords": hotwords,
        }
    whisper_model = str(row["faster_whisper_model"] or fallback_model or "large-v3")
    whisperx_model_value = row["whisperx_model"] if "whisperx_model" in row.keys() else ""
    whisperx_model = str(whisperx_model_value or whisper_model)
    whisperx_enabled = bool(row["whisperx_enabled"])
    return {
        "source": "monitored_broadcaster",
        "custom_settings_enabled": True,
        "broadcaster_id": str(row["broadcaster_id"] or broadcaster_id or ""),
        "engine": "whisperx" if whisperx_enabled else "faster-whisper",
        "faster_whisper_model": whisperx_model if whisperx_enabled else whisper_model,
        "fw_model": whisper_model,
        "whisperx_model": whisperx_model,
        "whisperx_enabled": whisperx_enabled,
        "speaker_diarization_enabled": bool(row["speaker_diarization_enabled"]),
        "diarization_min_speakers": int(row["diarization_min_speakers"] or 1),
        "diarization_max_speakers": int(row["diarization_max_speakers"] or 4),
        "initial_prompt": initial_prompt,
        "hotwords_enabled": hotwords_enabled,
        "hotwords": hotwords,
    }


def build_legacy_pipeline_data(
    lv: str,
    *,
    account_id: str | None = None,
    config: Config | None = None,
    recording_segment_timeline: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the old pipeline_data dict while keeping the new app directory layout."""
    config = config or load_config()
    account_id = str(account_id or "").strip()
    if not account_id:
        with connect() as conn:
            for table_name in ("broadcast_archive_meta", "recording_jobs", "broadcasts"):
                row = conn.execute(
                    f"SELECT broadcaster_id FROM {table_name} WHERE lv = ? LIMIT 1",
                    (str(lv).strip(),),
                ).fetchone()
                if row and str(row["broadcaster_id"] or "").strip():
                    account_id = str(row["broadcaster_id"]).strip()
                    break
    if not account_id:
        account_id = str(config.recording_account_id or DEFAULT_RECORDING_ACCOUNT_ID).strip()
    platform_directory = niconico_platform_target_root(config)
    legacy_config = build_legacy_archiver_config(config)
    legacy_config["ai_task_engines"] = get_monitored_broadcaster_ai_task_engines(str(lv).strip(), config)
    legacy_config = apply_monitored_broadcaster_feature_overrides(str(lv).strip(), legacy_config)
    if recording_segment_timeline is None:
        try:
            recording_segment_timeline = build_recording_segment_timeline_plan(
                slnico_storage_root(),
                lv=str(lv).strip(),
            )
        except Exception as exc:
            postprocess_log(
                str(lv).strip(),
                "archive_steps",
                "WARN",
                f"録画区間タイムライン構築失敗: {type(exc).__name__}: {exc}",
            )
            recording_segment_timeline = {
                "segments": [],
                "gaps": [],
                "parts": [],
                "total_duration_seconds": 0.0,
            }
    with connect() as conn:
        comment_state = conn.execute(
            "SELECT comments_fetch_error FROM broadcast_archive_meta WHERE lv = ?",
            (str(lv).strip(),),
        ).fetchone()
    comments_fetch_error = (
        str(comment_state["comments_fetch_error"] or "").strip()
        if comment_state is not None and "comments_fetch_error" in comment_state.keys()
        else ""
    )
    return {
        "platform": "niconico",
        "account_id": account_id,
        "platform_directory": str(platform_directory),
        "ncv_directory": "",
        "lv_value": str(lv).strip(),
        "user_name": account_id,
        "config": legacy_config,
        "start_time": datetime.now(),
        "recording_segment_timeline": recording_segment_timeline,
        "comments_fetch_failed": bool(comments_fetch_error),
        "comments_fetch_error": comments_fetch_error,
        "results": {},
    }


def run_legacy_archiver_steps(
    lv: str,
    *,
    account_id: str | None = None,
    steps: list[str] | None = None,
    config: Config | None = None,
    recording_segment_timeline: dict[str, Any] | None = None,
    force_overwrite_existing_html: bool = False,
    upload_html_only: bool = False,
    input_video_paths: list[Path | str] | None = None,
    progress_callback: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Run copied legacy_archiver processors directly with converted inputs."""
    config = config or load_config()
    pipeline_data = build_legacy_pipeline_data(
        lv,
        account_id=account_id,
        config=config,
        recording_segment_timeline=recording_segment_timeline,
    )
    pipeline_data["input_video_paths"] = [
        str(Path(path)) for path in (input_video_paths or []) if Path(path).is_file()
    ]
    if force_overwrite_existing_html:
        pipeline_data["config"]["force_overwrite_existing_html"] = True
    if upload_html_only:
        pipeline_data["config"].setdefault("upload_settings", {})["html_only"] = True
    ai_features = pipeline_data["config"].get("ai_features", {})
    display_features = pipeline_data["config"].get("display_features", {})
    default_steps = []
    if display_features.get("enable_emotion_scores", True):
        default_steps.append("step03_emotion_scorer")
    if display_features.get("enable_word_ranking", True):
        default_steps.append("step04_word_analyzer")
    if ai_features.get("enable_summary_text", False):
        default_steps.append("step05_summarizer")
    if ai_features.get("enable_ai_music", False):
        default_steps.append("step06_music_generator")
    if ai_features.get("enable_summary_image", True):
        default_steps.append("step07_image_generator")
    if ai_features.get("enable_ai_conversation", False):
        default_steps.append("step08_conversation_generator")
    if display_features.get("enable_thumbnails", True):
        default_steps.append("step09_screenshot_generator")
    default_steps.extend([
        "step10_comment_processor",
        "step11_special_user_html_generator",
        "step12_html_generator",
        "step13_index_generator",
        "step14_modern_list_generator",
    ])
    if (pipeline_data["config"].get("upload_settings") or {}).get(
        "enable_auto_upload", False
    ):
        default_steps.append("step15_lolipop_uploader")
    step_names = steps or default_steps
    legacy_dir = ROOT / "legacy_archiver"
    legacy_path = str(legacy_dir)
    inserted = False
    if legacy_path not in sys.path:
        sys.path.insert(0, legacy_path)
        inserted = True
    previous_cwd = Path.cwd()
    results: dict[str, Any] = {"lv": lv, "steps": {}, "pipeline_data": pipeline_data}
    try:
        os.chdir(legacy_dir)
        for step_name in step_names:
            step_started = time.monotonic()
            postprocess_log(lv, "archive_steps", "INFO", f"legacy step開始: {step_name}")
            module = importlib.import_module(f"processors.{step_name}")
            if not hasattr(module, "process"):
                results["steps"][step_name] = {"status": "skipped", "reason": "missing_process"}
                postprocess_log(lv, "archive_steps", "WARN", f"legacy stepスキップ: {step_name} missing_process")
                continue
            results["steps"][step_name] = {"status": "running"}
            log_dir = TMP_DIR / "legacy_step_logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            log_path = log_dir / f"{lv}_{step_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
            log_path.write_text(
                f"[{datetime.now().isoformat(timespec='seconds')}] {lv} {step_name} start\n",
                encoding="utf-8",
            )
            if step_name in {"step11_special_user_html_generator", "step12_html_generator"}:
                start_visible_legacy_step_log_window(lv, step_name, log_path)
            try:
                with log_path.open("a", encoding="utf-8", buffering=1) as log_file:
                    class GuiLogTee:
                        def write(self, text: str) -> int:
                            log_file.write(text)
                            message = text.strip()
                            if message and progress_callback:
                                progress_callback(message)
                            return len(text)

                        def flush(self) -> None:
                            log_file.flush()

                    tee = GuiLogTee()
                    with contextlib.redirect_stdout(tee), contextlib.redirect_stderr(tee):
                        output = module.process(pipeline_data)
                        print(f"[{datetime.now().isoformat(timespec='seconds')}] {lv} {step_name} done")
            except Exception as exc:
                elapsed = time.monotonic() - step_started
                postprocess_log(
                    lv,
                    "archive_steps",
                    "ERROR",
                    f"legacy step失敗: {step_name} elapsed={elapsed:.1f}s {type(exc).__name__}: {exc}",
                    {"step_log": str(log_path)},
                )
                raise
            pipeline_data["results"][step_name] = output
            results["steps"][step_name] = {"status": "done", "result": output}
            elapsed = time.monotonic() - step_started
            postprocess_log(
                lv,
                "archive_steps",
                "INFO",
                f"legacy step完了: {step_name} elapsed={elapsed:.1f}s",
                {"step_log": str(log_path)},
            )
    finally:
        os.chdir(previous_cwd)
        if inserted:
            try:
                sys.path.remove(legacy_path)
            except ValueError:
                pass
    return results


def start_visible_legacy_step_log_window(lv: str, step_name: str, log_path: Path) -> None:
    """Keep HTML-step details in the log file without opening console windows."""
    postprocess_log(
        lv,
        step_name,
        "DEBUG",
        "HTMLステップ専用cmd表示は無効",
        {"log_path": str(log_path)},
    )


def run_finalize_pipeline_for_lv(
    lv: str,
    *,
    broadcaster_id: str = "",
    input_dir: Path | str | None = None,
    transcribe: bool = True,
    whisper_model: str = "large-v3",
    timeline_mode: str = "live",
    segment_paths: list[Path | str] | None = None,
    legacy_steps: list[str] | None = None,
    progress_callback: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    config = load_config()
    broadcaster_id = str(broadcaster_id or "").strip()
    if not broadcaster_id:
        try:
            with connect() as conn:
                meta_row = conn.execute(
                    "SELECT broadcaster_id FROM broadcast_archive_meta WHERE lv = ?",
                    (lv,),
                ).fetchone()
                if meta_row and str(meta_row["broadcaster_id"] or "").strip():
                    broadcaster_id = str(meta_row["broadcaster_id"]).strip()
                else:
                    meta = fetch_and_save_broadcast_archive_meta(conn, lv)
                    broadcaster_id = str(meta.get("broadcaster_id") or "").strip()
                    conn.commit()
        except Exception as exc:
            postprocess_log(
                lv,
                "metadata",
                "WARN",
                f"配信者IDの事前解決失敗: {type(exc).__name__}: {exc}",
            )
    target_dir = broadcast_target_dir(lv, config, broadcaster_id=broadcaster_id or None)
    target_dir.mkdir(parents=True, exist_ok=True)
    source_dir = Path(input_dir) if input_dir else slnico_storage_root()
    result: dict[str, Any] = {
        "lv": lv,
        "source_dir": str(source_dir),
        "target_dir": str(target_dir),
    }

    if progress_callback:
        progress_callback("録画区間・時間軸解析開始")
    timeline_plan = build_recording_segment_timeline_plan(
        source_dir,
        lv=lv,
        timeline_mode=timeline_mode,
        segment_paths=segment_paths,
    )
    timeline_validation = validate_recording_timeline_plan(timeline_plan, require_complete=True)
    if progress_callback:
        progress_callback(
            f"録画区間・時間軸解析完了 segments={len(timeline_plan.get('segments') or [])} "
            f"total={float(timeline_plan.get('total_duration_seconds') or 0.0):.1f}秒"
        )
    result["timeline_validation"] = timeline_validation
    if not timeline_validation["valid"]:
        error = "; ".join(timeline_validation["errors"])
        postprocess_log(
            lv,
            "timeline",
            "ERROR",
            f"録画時間軸検証失敗: {error}",
            timeline_validation,
        )
        mark_stage_failed(lv, "collect_segments", f"timeline_invalid: {error}")
        raise RuntimeError(f"recording timeline is invalid: {error}")
    postprocess_log(
        lv,
        "timeline",
        "INFO",
        (
            f"録画時間軸検証完了 segments={timeline_validation['segment_count']} "
            f"gaps={timeline_validation['gap_count']} "
            f"total={timeline_validation['total_duration_seconds']:.3f}s"
        ),
        timeline_validation,
    )

    # Rebase before register_recording_segments overwrites the prior offsets.
    # Older sentiment analysis output omitted local_* fields, so the stored
    # previous segment offset is required to recover local Whisper timestamps.
    with connect() as conn:
        transcript_rebase = rebase_recording_segment_transcripts(conn, lv, timeline_plan)
        conn.commit()

    try:
        with connect() as conn:
            update_postprocess_job(conn, lv, "collect_segments", "running")
            segments = register_recording_segments(
                conn,
                lv,
                broadcaster_id,
                storage_root=source_dir,
                timeline_mode=timeline_mode,
                segment_paths=segment_paths,
            )
            update_postprocess_job(conn, lv, "collect_segments", "done")
            update_postprocess_job(conn, lv, "make_gaps", "running")
            event_gaps = register_recording_gaps_from_events(
                conn,
                lv,
                storage_root=source_dir,
                timeline_mode=timeline_mode,
                segment_paths=segment_paths,
            )
            update_postprocess_job(conn, lv, "make_gaps", "done")
            conn.commit()
        result["registered_segments"] = len(segments)
        result["event_gaps"] = len(event_gaps)
    except Exception as exc:
        mark_stage_failed(lv, "collect_segments", f"{type(exc).__name__}: {exc}")
        raise

    result["timeline_origin"] = str(timeline_plan.get("timeline_origin") or "")
    result["timeline_origin_source"] = str(timeline_plan.get("timeline_origin_source") or "")
    result["video_concat_skipped"] = True
    result["joined_video_path"] = ""
    result["file_segments"] = len(timeline_plan.get("segments") or [])
    result["file_gaps"] = len(timeline_plan.get("gaps") or [])
    result["recording_segment_timeline"] = {
        "total_duration_seconds": float(timeline_plan.get("total_duration_seconds") or 0.0),
        "segments": [
            {
                "path": str(segment.get("path") or ""),
                "segment_index": int(segment.get("segment_index") or 0),
                "timeline_start_seconds": float(segment.get("timeline_start_seconds") or 0.0),
                "timeline_end_seconds": float(segment.get("timeline_end_seconds") or 0.0),
                "duration_seconds": float(segment.get("duration_seconds") or 0.0),
            }
            for segment in timeline_plan.get("segments") or []
        ],
        "gaps": [
            {
                "timeline_start_seconds": float(gap.get("timeline_start_seconds") or 0.0),
                "timeline_end_seconds": float(gap.get("timeline_end_seconds") or 0.0),
                "duration_seconds": float(gap.get("duration_seconds") or 0.0),
                "reason": str(gap.get("reason") or gap.get("fill_type") or "gap"),
            }
            for gap in timeline_plan.get("gaps") or []
        ],
    }
    with connect() as conn:
        update_postprocess_job(conn, lv, "concat_video", "skipped")
        update_postprocess_job(conn, lv, "extract_wav", "skipped")
        update_postprocess_job(conn, lv, "transcribe", "running" if transcribe else "done")
        if transcribe:
            result["removed_legacy_whole_transcripts"] = remove_non_segment_transcripts(
                conn,
                lv,
                target_dir / "recording_segments",
            )
        conn.commit()
    result["segment_transcript_rebase"] = transcript_rebase
    for rebase_row in transcript_rebase:
        correction = rebase_row.get("correction_seconds")
        correction_text = f"{float(correction):+.3f}s" if correction is not None else "mixed"
        postprocess_log(
            lv,
            "timeline",
            "INFO",
            (
                f"区間文字起こし時刻を再補正 segment={int(rebase_row.get('segment_index') or 0)} "
                f"rows={int(rebase_row.get('rows') or 0)} correction={correction_text} "
                f"new_offset={float(rebase_row.get('new_timeline_offset') or 0.0):.3f}s"
            ),
            rebase_row,
        )

    segment_results = ensure_recording_segment_transcriptions(
        lv,
        broadcaster_id=broadcaster_id,
        target_dir=target_dir,
        transcribe=transcribe,
        whisper_model=whisper_model,
        input_dir=source_dir,
        timeline_mode=timeline_mode,
        segment_paths=segment_paths,
        progress_callback=progress_callback,
    )
    result["segment_transcriptions"] = segment_results
    failed_transcriptions = [row for row in segment_results if str(row.get("reason") or "") in {"failed", "mp4_missing"}]
    with connect() as conn:
        update_postprocess_job(
            conn,
            lv,
            "transcribe",
            "failed" if failed_transcriptions else "done",
            error="; ".join(str(row.get("error") or row.get("reason") or "") for row in failed_transcriptions),
        )
        result["legacy_transcript"] = export_legacy_transcript_file_from_db(conn, lv, target_dir=target_dir)
        timeline_alignment = validate_archive_timeline_alignment(conn, lv, timeline_plan)
        conn.commit()
    result["timeline_alignment"] = timeline_alignment
    postprocess_log(
        lv,
        "timeline",
        "INFO" if timeline_alignment["valid"] else "ERROR",
        (
            f"文字起こし・コメント時間軸検証 valid={timeline_alignment['valid']} "
            f"transcripts={timeline_alignment['transcript_count']} "
            f"invalid={timeline_alignment['invalid_transcript_count']} "
            f"comments={timeline_alignment['comment_count']} "
            f"comment_outside={timeline_alignment['comments_outside_timeline']}"
        ),
        timeline_alignment,
    )
    if not timeline_alignment["valid"]:
        error = "; ".join(timeline_alignment["errors"][:5])
        mark_stage_failed(lv, "transcribe", f"timeline_alignment_invalid: {error}")
        raise RuntimeError(f"final transcript timeline is invalid: {error}")

    try:
        with connect() as conn:
            update_postprocess_job(conn, lv, "encode_mp3", "running")
            conn.commit()
        mp3_path = target_dir / f"{lv}_audio.mp3"
        if progress_callback:
            progress_callback("録画区間音声の連結開始")
        audio = concat_recording_segment_audio(lv, timeline_plan, mp3_path)
        if progress_callback:
            progress_callback(f"録画区間音声の連結完了: {mp3_path}")
        with connect() as conn:
            update_postprocess_job(conn, lv, "encode_mp3", "done")
            conn.commit()
        result.update(audio)
    except Exception as exc:
        mark_stage_failed(lv, "encode_mp3", f"{type(exc).__name__}: {exc}")
        raise

    with connect() as conn:
        update_postprocess_job(conn, lv, "archive_steps", "running")
        result["legacy_files"] = export_legacy_archive_files_from_ndgr(
            conn,
            lv,
            target_dir=target_dir,
            video_duration=float(timeline_plan.get("total_duration_seconds") or 0.0),
            time_diff_seconds=int(float(timeline_plan.get("initial_offset_seconds") or 0.0)),
        )
        result["legacy_transcript"] = export_legacy_transcript_file_from_db(conn, lv, target_dir=target_dir)
        result["broadcaster_monitor_special_hits"] = record_broadcaster_monitor_special_user_hits_from_archive(conn, lv)
        conn.commit()
    try:
        result["legacy_archiver"] = run_legacy_archiver_steps(
            lv,
            account_id=str(broadcaster_id or config.recording_account_id),
            config=config,
            recording_segment_timeline=timeline_plan,
            steps=legacy_steps,
        )
        html_file = (
            result.get("legacy_archiver", {})
            .get("steps", {})
            .get("step12_html_generator", {})
            .get("result", {})
            .get("html_file")
        )
        if html_file:
            html_file = str(Path(html_file).resolve())
            with connect() as conn:
                conn.execute(
                    "UPDATE broadcast_archive_meta SET html_path = ?, fetched_at = ? WHERE lv = ?",
                    (html_file, now_micro(), lv),
                )
                conn.commit()
            result["broadcaster_monitor_html_uploads"] = upload_broadcaster_monitor_html_for_special_user_hits(lv, html_file)
        with connect() as conn:
            update_postprocess_job(conn, lv, "archive_steps", "done")
            conn.commit()
    except Exception as exc:
        mark_stage_failed(lv, "archive_steps", f"{type(exc).__name__}: {exc}")
        result["legacy_archiver_error"] = f"{type(exc).__name__}: {exc}"
        raise
    else:
        with connect() as conn:
            update_postprocess_job(conn, lv, "archive_steps", "done")
            conn.commit()
    return result


def save_transcript_segments(
    conn: sqlite3.Connection,
    lv: str,
    segments: list[dict[str, Any]],
    *,
    source_audio_path: str = "",
    model: str = "",
) -> int:
    current_time = now_micro()
    saved = 0
    for index, segment in enumerate(segments):
        text = str(segment.get("text") or "").strip()
        if not text:
            continue
        start_seconds = float(segment.get("start_seconds") or segment.get("start") or 0.0)
        end_seconds = float(segment.get("end_seconds") or segment.get("end") or start_seconds)
        conn.execute(
            """
            INSERT INTO archive_transcript_segments
                (lv, segment_index, start_seconds, end_seconds, text, confidence,
                 speaker, source_audio_path, model, raw_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(lv, segment_index, start_seconds, end_seconds, text) DO UPDATE SET
                confidence = excluded.confidence,
                speaker = excluded.speaker,
                source_audio_path = excluded.source_audio_path,
                model = excluded.model,
                raw_json = excluded.raw_json
            """,
            (
                lv,
                int(segment.get("segment_index") if segment.get("segment_index") is not None else index),
                start_seconds,
                end_seconds,
                text,
                segment.get("confidence"),
                str(segment.get("speaker") or ""),
                source_audio_path,
                model,
                json.dumps(segment, ensure_ascii=False, default=str),
                current_time,
            ),
        )
        saved += 1
    return saved


def clamp_transcription_interval_to_timeline_end(
    local_start_seconds: float,
    local_end_seconds: float,
    *,
    timeline_offset_seconds: float,
    timeline_end_seconds: float | None,
) -> tuple[float, float, float, float]:
    offset = max(0.0, float(timeline_offset_seconds or 0.0))
    local_start = max(0.0, float(local_start_seconds or 0.0))
    local_end = max(local_start, float(local_end_seconds or local_start))
    start_seconds = offset + local_start
    end_seconds = max(start_seconds, offset + local_end)
    if timeline_end_seconds is not None:
        timeline_end = max(offset, float(timeline_end_seconds))
        start_seconds = min(start_seconds, timeline_end)
        end_seconds = min(max(start_seconds, end_seconds), timeline_end)
        local_start = max(0.0, start_seconds - offset)
        local_end = max(local_start, end_seconds - offset)
    return local_start, local_end, start_seconds, end_seconds


def normalize_transcription_rows_for_timeline(
    segments: list[dict[str, Any]],
    *,
    timeline_offset_seconds: float = 0.0,
    timeline_end_seconds: float | None = None,
    segment_index_base: int = 0,
) -> list[dict[str, Any]]:
    offset = max(0.0, float(timeline_offset_seconds or 0.0))
    rows: list[dict[str, Any]] = []
    for local_index, source in enumerate(segments):
        row = dict(source)
        local_start = float(row.get("start_seconds") or row.get("start") or 0.0)
        local_end = float(row.get("end_seconds") or row.get("end") or local_start)
        local_start, local_end, start_seconds, end_seconds = (
            clamp_transcription_interval_to_timeline_end(
                local_start,
                local_end,
                timeline_offset_seconds=offset,
                timeline_end_seconds=timeline_end_seconds,
            )
        )
        row["local_start_seconds"] = local_start
        row["local_end_seconds"] = local_end
        row["timeline_offset_seconds"] = offset
        row["start_seconds"] = start_seconds
        row["end_seconds"] = end_seconds
        row["segment_index"] = int(segment_index_base) + local_index
        rows.append(row)
    return rows


def persist_transcription_rows(
    conn: sqlite3.Connection,
    lv: str,
    rows: list[dict[str, Any]],
    *,
    source_audio_path: str,
    model: str,
    replace_scope: str = "broadcast",
) -> int:
    if replace_scope == "broadcast":
        conn.execute("DELETE FROM archive_transcript_segments WHERE lv = ?", (lv,))
    elif replace_scope == "source":
        conn.execute(
            "DELETE FROM archive_transcript_segments WHERE lv = ? AND source_audio_path = ?",
            (lv, source_audio_path),
        )
    else:
        raise ValueError(f"unknown transcript replace scope: {replace_scope}")
    return save_transcript_segments(
        conn,
        lv,
        rows,
        source_audio_path=source_audio_path,
        model=model,
    )


def rebase_recording_segment_transcripts(
    conn: sqlite3.Connection,
    lv: str,
    plan: dict[str, Any],
) -> list[dict[str, Any]]:
    """Rebase saved segment-local transcripts without running Whisper again."""
    plan_by_identity = {
        str(segment.get("segment_identity") or recording_segment_identity(str(segment.get("path") or ""))): segment
        for segment in plan.get("segments") or []
    }
    segment_rows = conn.execute(
        """
        SELECT source_path, audio_wav_path, transcript_model, timeline_start_seconds
        FROM recording_segments
        WHERE lv = ?
          AND COALESCE(audio_wav_path, '') <> ''
        ORDER BY segment_index ASC, id ASC
        """,
        (lv,),
    ).fetchall()
    results: list[dict[str, Any]] = []
    for segment_row in segment_rows:
        source_path = str(segment_row["source_path"] or "")
        timeline_entry = plan_by_identity.get(recording_segment_identity(source_path))
        if timeline_entry is None:
            continue
        audio_path = str(segment_row["audio_wav_path"] or "").strip()
        if not audio_path:
            continue
        saved_rows = conn.execute(
            """
            SELECT *
            FROM archive_transcript_segments
            WHERE lv = ? AND source_audio_path = ?
            ORDER BY segment_index ASC, start_seconds ASC, id ASC
            """,
            (lv, audio_path),
        ).fetchall()
        if not saved_rows:
            continue

        new_offset = float(timeline_entry.get("timeline_start_seconds") or 0.0)
        timeline_end = float(
            timeline_entry.get("timeline_end_seconds")
            or (
                new_offset
                + float(timeline_entry.get("duration_seconds") or 0.0)
            )
        )
        fallback_old_offset = float(segment_row["timeline_start_seconds"] or 0.0)
        segment_index_base = int(timeline_entry.get("segment_index") or 0) * 1_000_000
        rebased_rows: list[dict[str, Any]] = []
        old_offsets: list[float] = []
        for local_index, saved_row in enumerate(saved_rows):
            try:
                raw = json.loads(str(saved_row["raw_json"] or "{}"))
            except Exception:
                raw = {}
            has_saved_offset = "timeline_offset_seconds" in raw
            try:
                old_offset = (
                    float(raw.get("timeline_offset_seconds") or 0.0)
                    if has_saved_offset
                    else fallback_old_offset
                )
            except (TypeError, ValueError):
                old_offset = fallback_old_offset
            old_offsets.append(old_offset)
            try:
                local_start = float(raw["local_start_seconds"])
            except (KeyError, TypeError, ValueError):
                local_start = float(saved_row["start_seconds"] or 0.0) - old_offset
            try:
                local_end = float(raw["local_end_seconds"])
            except (KeyError, TypeError, ValueError):
                local_end = float(saved_row["end_seconds"] or 0.0) - old_offset
            local_start, local_end, start_seconds, end_seconds = (
                clamp_transcription_interval_to_timeline_end(
                    local_start,
                    local_end,
                    timeline_offset_seconds=new_offset,
                    timeline_end_seconds=timeline_end,
                )
            )
            raw.update(
                {
                    "text": str(saved_row["text"] or ""),
                    "confidence": saved_row["confidence"],
                    "speaker": str(saved_row["speaker"] or ""),
                    "local_start_seconds": local_start,
                    "local_end_seconds": local_end,
                    "timeline_offset_seconds": new_offset,
                    "start_seconds": start_seconds,
                    "end_seconds": end_seconds,
                    "segment_index": segment_index_base + local_index,
                }
            )
            rebased_rows.append(raw)

        model = str(saved_rows[0]["model"] or segment_row["transcript_model"] or "")
        persist_transcription_rows(
            conn,
            lv,
            rebased_rows,
            source_audio_path=audio_path,
            model=model,
            replace_scope="source",
        )
        distinct_old_offsets = sorted({round(value, 6) for value in old_offsets})
        results.append(
            {
                "source_path": source_path,
                "source_audio_path": audio_path,
                "segment_index": int(timeline_entry.get("segment_index") or 0),
                "old_timeline_offsets": distinct_old_offsets,
                "new_timeline_offset": new_offset,
                "correction_seconds": (
                    new_offset - distinct_old_offsets[0] if len(distinct_old_offsets) == 1 else None
                ),
                "rows": len(rebased_rows),
            }
        )
    return results


def remove_non_segment_transcripts(
    conn: sqlite3.Connection,
    lv: str,
    segment_asset_dir: Path | str,
) -> int:
    asset_dir = Path(segment_asset_dir).resolve()
    rows = conn.execute(
        "SELECT id, source_audio_path FROM archive_transcript_segments WHERE lv = ?",
        (lv,),
    ).fetchall()
    stale_ids: list[int] = []
    for row in rows:
        source = str(row["source_audio_path"] or "").strip()
        try:
            is_segment_audio = source and Path(source).resolve().parent == asset_dir
        except Exception:
            is_segment_audio = False
        if not is_segment_audio:
            stale_ids.append(int(row["id"]))
    if stale_ids:
        conn.executemany(
            "DELETE FROM archive_transcript_segments WHERE id = ?",
            [(row_id,) for row_id in stale_ids],
        )
    return len(stale_ids)


def transcript_row_to_legacy_transcript(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    get = row.get if isinstance(row, dict) else row.__getitem__

    def value(name: str, default: Any = "") -> Any:
        try:
            result = get(name)
        except Exception:
            return default
        return default if result is None else result

    timestamp = int(float(value("start_seconds", 0.0) or 0.0))
    return {
        "timestamp": timestamp,
        "timeline_block": (timestamp // 10) * 10,
        "text": str(value("text", "")),
        "positive_score": 0.0,
        "center_score": 0.0,
        "negative_score": 0.0,
    }


def save_legacy_transcript_json(
    lv: str,
    transcripts: list[dict[str, Any]],
    *,
    target_dir: Path | None = None,
    status: str | None = None,
) -> Path:
    config = load_config()
    target_dir = target_dir or broadcast_target_dir(lv, config)
    target_dir.mkdir(parents=True, exist_ok=True)
    if not transcripts:
        status_value = status or "no_audio_or_failed"
        payload = {
            "lv_value": lv,
            "total_segments": 1,
            "creation_time": now_micro(),
            "status": status_value,
            "transcripts": [
                {
                    "timestamp": 0,
                    "timeline_block": 0,
                    "text": "[音声なし/処理失敗]",
                    "positive_score": 0.0,
                    "center_score": 0.0,
                    "negative_score": 0.0,
                }
            ],
            "source": "ndgr",
        }
    else:
        payload = {
            "lv_value": lv,
            "total_segments": len(transcripts),
            "creation_time": now_micro(),
            "status": status or "completed",
            "transcripts": transcripts,
            "source": "ndgr",
        }
    path = target_dir / f"{lv}_transcript.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def export_legacy_transcript_file_from_db(
    conn: sqlite3.Connection,
    lv: str,
    *,
    target_dir: Path | None = None,
) -> dict[str, Any]:
    rows = conn.execute(
        """
        SELECT *
        FROM archive_transcript_segments
        WHERE lv = ?
        ORDER BY start_seconds ASC, segment_index ASC, id ASC
        """,
        (lv,),
    ).fetchall()
    transcripts = [transcript_row_to_legacy_transcript(row) for row in rows if str(row["text"] or "").strip()]
    path = save_legacy_transcript_json(
        lv,
        transcripts,
        target_dir=target_dir,
        status="completed" if transcripts else "no_audio_or_failed",
    )
    return {"lv": lv, "transcript_path": str(path), "segments": len(transcripts)}


_TIMESHIFT_FASTER_WHISPER_MODEL_CACHE: dict[tuple[str, str, str], Any] = {}
_TIMESHIFT_FASTER_WHISPER_MODEL_CACHE_LOCK = threading.Lock()
_FASTER_WHISPER_PROGRESS_LOG_INTERVAL_SECONDS = 5.0


def transcribe_audio_with_faster_whisper(
    lv: str,
    audio_path: Path | str,
    *,
    model_size: str = "large-v3",
    device: str = "cuda",
    compute_type: str = "float16",
    target_dir: Path | str | None = None,
    timeline_offset_seconds: float = 0.0,
    timeline_end_seconds: float | None = None,
    segment_index_base: int = 0,
    replace_scope: str = "broadcast",
    mark_postprocess_done: bool = True,
    progress_callback: Callable[[str], None] | None = None,
    initial_prompt: str = "これはニコニコ生放送の録画音声です",
    hotwords: str = "",
) -> dict[str, Any]:
    audio = Path(audio_path)
    if not audio.exists():
        raise FileNotFoundError(str(audio))
    audio_duration: float | None = None
    try:
        audio_duration = probe_media_duration_seconds(audio)
    except Exception as exc:
        postprocess_log(lv, "transcribe", "WARN", f"音声長取得失敗: {type(exc).__name__}: {exc}")
    cuda_payload: dict[str, Any] = {}
    try:
        import torch

        cuda_payload = {
            "torch_cuda_available": bool(torch.cuda.is_available()),
            "torch_cuda_device_count": int(torch.cuda.device_count()) if torch.cuda.is_available() else 0,
            "torch_cuda_device_name": torch.cuda.get_device_name(0) if torch.cuda.is_available() else "",
        }
    except Exception as exc:
        cuda_payload = {"torch_check_error": f"{type(exc).__name__}: {exc}"}
    postprocess_log(
        lv,
        "transcribe",
        "DEBUG",
        f"FasterWhisper開始 model={model_size} device={device} compute_type={compute_type} audio_seconds={audio_duration if audio_duration is not None else '-'}",
        {
            "engine": "faster-whisper",
            "model": model_size,
            "device": device,
            "compute_type": compute_type,
            "audio_path": str(audio),
            "audio_seconds": audio_duration,
            "python": sys.executable,
            **cuda_payload,
        },
    )
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        postprocess_log(lv, "transcribe", "ERROR", "faster_whisper import失敗", {"python": sys.executable})
        raise RuntimeError("faster_whisper is not installed in this Python environment") from exc

    cache_enabled = str(os.environ.get("NICONICO_WATCH_APP_ROLE") or "").strip().lower() == "timeshift"
    cache_key = (str(model_size), str(device), str(compute_type))
    try:
        if cache_enabled:
            with _TIMESHIFT_FASTER_WHISPER_MODEL_CACHE_LOCK:
                model = _TIMESHIFT_FASTER_WHISPER_MODEL_CACHE.get(cache_key)
                if model is None:
                    postprocess_log(
                        lv,
                        "transcribe",
                        "INFO",
                        (
                            "FasterWhisperモデル新規ロード開始 "
                            f"model={model_size} device={device} compute_type={compute_type}"
                        ),
                        {
                            "model": model_size,
                            "device": device,
                            "compute_type": compute_type,
                            "cache_scope": "timeshift_process",
                        },
                    )
                    model = WhisperModel(model_size, device=device, compute_type=compute_type)
                    _TIMESHIFT_FASTER_WHISPER_MODEL_CACHE[cache_key] = model
                    postprocess_log(
                        lv,
                        "transcribe",
                        "INFO",
                        (
                            "FasterWhisperモデル新規ロード完了 "
                            f"model={model_size} device={device} compute_type={compute_type}"
                        ),
                        {
                            "model": model_size,
                            "device": device,
                            "compute_type": compute_type,
                            "cache_scope": "timeshift_process",
                            "cache_entries": len(_TIMESHIFT_FASTER_WHISPER_MODEL_CACHE),
                        },
                    )
                else:
                    postprocess_log(
                        lv,
                        "transcribe",
                        "INFO",
                        (
                            "FasterWhisperモデルキャッシュ再利用 "
                            f"model={model_size} device={device} compute_type={compute_type}"
                        ),
                        {
                            "model": model_size,
                            "device": device,
                            "compute_type": compute_type,
                            "cache_scope": "timeshift_process",
                            "cache_entries": len(_TIMESHIFT_FASTER_WHISPER_MODEL_CACHE),
                        },
                    )
        else:
            model = WhisperModel(model_size, device=device, compute_type=compute_type)
            postprocess_log(lv, "transcribe", "DEBUG", f"FasterWhisperモデルロード完了 model={model_size} device={device}")
        raw_segments, info = model.transcribe(
            str(audio),
            vad_filter=True,
            initial_prompt=initial_prompt or "これはニコニコ生放送の録画音声です",
            hotwords=hotwords or None,
        )
    except Exception as exc:
        postprocess_log(
            lv,
            "transcribe",
            "ERROR",
            f"FasterWhisper初期化/開始失敗: {type(exc).__name__}: {exc}",
            {"model": model_size, "device": device, "compute_type": compute_type},
        )
        raise
    rows: list[dict[str, Any]] = []
    started_monotonic = time.monotonic()
    last_console_log_monotonic = started_monotonic
    last_postprocess_log_monotonic: float | None = None
    last_postprocess_log_segments = 0
    console_progress = ConsoleProgress("FasterWhisper", total_seconds=audio_duration)
    for index, segment in enumerate(raw_segments):
        segment_text = str(segment.text or "").strip()
        rows.append(
            {
                "segment_index": index,
                "start_seconds": float(segment.start),
                "end_seconds": float(segment.end),
                "text": segment_text,
                "confidence": None,
                "language": getattr(info, "language", None),
                "duration": getattr(info, "duration", None),
            }
        )
        if progress_callback:
            progress_callback(
                f"[{float(segment.start):.2f}-{float(segment.end):.2f}] {segment_text}"
            )
        current_monotonic = time.monotonic()
        elapsed_since_console_log = current_monotonic - last_console_log_monotonic
        if elapsed_since_console_log >= 1.0:
            last_console_log_monotonic = current_monotonic
            console_progress.update(float(segment.end), extra=f"segments={index + 1}", force=True)
        if (
            last_postprocess_log_monotonic is None
            or current_monotonic - last_postprocess_log_monotonic >= _FASTER_WHISPER_PROGRESS_LOG_INTERVAL_SECONDS
        ):
            last_postprocess_log_monotonic = current_monotonic
            last_postprocess_log_segments = index + 1
            postprocess_log(
                lv,
                "transcribe",
                "INFO",
                (
                    f"FasterWhisper進捗 segments={index + 1} "
                    f"processed={hms_seconds(float(segment.end))} total={hms_seconds(audio_duration)}"
                ),
                {
                    "segments": index + 1,
                    "processed_seconds": float(segment.end),
                    "total_seconds": audio_duration,
                    "elapsed_wall_seconds": round(current_monotonic - started_monotonic, 2),
                },
            )
    console_progress.finish()
    if rows and last_postprocess_log_segments != len(rows):
        postprocess_log(
            lv,
            "transcribe",
            "INFO",
            f"FasterWhisper進捗 segments={len(rows)} processed={hms_seconds(float(rows[-1]['end_seconds']))} total={hms_seconds(audio_duration)}",
            {
                "segments": len(rows),
                "processed_seconds": float(rows[-1]["end_seconds"]),
                "total_seconds": audio_duration,
                "elapsed_wall_seconds": round(time.monotonic() - started_monotonic, 2),
            },
        )
    rows = normalize_transcription_rows_for_timeline(
        rows,
        timeline_offset_seconds=timeline_offset_seconds,
        timeline_end_seconds=timeline_end_seconds,
        segment_index_base=segment_index_base,
    )
    with connect() as conn:
        saved = persist_transcription_rows(
            conn,
            lv,
            rows,
            source_audio_path=str(audio),
            model=f"faster-whisper:{model_size}",
            replace_scope=replace_scope,
        )
        legacy = export_legacy_transcript_file_from_db(
            conn,
            lv,
            target_dir=Path(target_dir) if target_dir is not None else None,
        )
        if mark_postprocess_done:
            conn.execute(
                """
                UPDATE postprocess_jobs
                SET status = ?, finished_at = ?, updated_at = ?, error = NULL
                WHERE lv = ? AND stage = 'transcribe'
                """,
                ("done", now_micro(), now_micro(), lv),
            )
        conn.commit()
    postprocess_log(
        lv,
        "transcribe",
        "INFO",
        f"FasterWhisper完了 segments={len(rows)} language={getattr(info, 'language', None) or '-'}",
        {
            "segments": len(rows),
            "language": getattr(info, "language", None),
            "duration": getattr(info, "duration", None),
            "model": model_size,
            "device": device,
            "compute_type": compute_type,
        },
    )
    return {
        "lv": lv,
        "audio_path": str(audio),
        "model": model_size,
        "segments": len(rows),
        "saved": saved,
        "legacy_transcript": legacy,
        "language": getattr(info, "language", None),
    }


def transcribe_audio_with_whisperx(
    lv: str,
    audio_path: Path | str,
    *,
    model_size: str = "medium",
    device: str = "cuda",
    compute_type: str = "float16",
    diarize: bool = False,
    min_speakers: int = 1,
    max_speakers: int = 4,
    batch_size: int = 16,
    target_dir: Path | str | None = None,
    timeline_offset_seconds: float = 0.0,
    timeline_end_seconds: float | None = None,
    segment_index_base: int = 0,
    replace_scope: str = "broadcast",
    mark_postprocess_done: bool = True,
    initial_prompt: str = "これはニコニコ生放送の録画音声です",
    hotwords: str = "",
) -> dict[str, Any]:
    audio = Path(audio_path)
    if not audio.exists():
        raise FileNotFoundError(str(audio))
    audio_duration: float | None = None
    try:
        audio_duration = probe_media_duration_seconds(audio)
    except Exception as exc:
        postprocess_log(lv, "transcribe", "WARN", f"音声長取得失敗: {type(exc).__name__}: {exc}")
    postprocess_log(
        lv,
        "transcribe",
        "DEBUG",
        f"WhisperX開始 model={model_size} device={device} compute_type={compute_type} diarize={int(diarize)}",
        {
            "engine": "whisperx",
            "model": model_size,
            "device": device,
            "compute_type": compute_type,
            "audio_path": str(audio),
            "audio_seconds": audio_duration,
            "diarize": diarize,
            "min_speakers": min_speakers,
            "max_speakers": max_speakers,
            "python": sys.executable,
        },
    )
    try:
        import whisperx
    except ImportError:
        whisperx = None

    config = load_config()
    huggingface_token = config.huggingface_token or os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_TOKEN") or ""
    if diarize and not huggingface_token:
        raise RuntimeError("WhisperX話者分離には config.json の huggingface_token または HF_TOKEN/HUGGINGFACE_TOKEN が必要です")

    try:
        if whisperx is None:
            helper_python = ROOT / "lab" / "whisperx_diarize_gui" / ".venv" / "Scripts" / "python.exe"
            helper_script = ROOT / "tools" / "whisperx_transcribe_cli.py"
            if not helper_python.exists():
                raise RuntimeError(f"WhisperX helper Python not found: {helper_python}")
            out_path = TMP_DIR / "whisperx_runs" / lv / f"{Path(audio).stem}_whisperx.json"
            progress_path = TMP_DIR / "whisperx_runs" / lv / f"{Path(audio).stem}_whisperx_progress.json"
            cmd = [
                str(helper_python),
                str(helper_script),
                "--audio",
                str(audio),
                "--output",
                str(out_path),
                "--model",
                model_size,
                "--device",
                device,
                "--compute-type",
                compute_type,
                "--batch-size",
                str(batch_size),
                "--min-speakers",
                str(int(min_speakers or 1)),
                "--max-speakers",
                str(int(max_speakers or 4)),
                "--progress-json",
                str(progress_path),
                "--initial-prompt",
                initial_prompt or "これはニコニコ生放送の録画音声です",
                "--hotwords",
                hotwords or "",
            ]
            if diarize:
                cmd.append("--diarize")
            postprocess_log(
                lv,
                "transcribe",
                "DEBUG",
                f"WhisperX外部Python呼び出し python={helper_python}",
                {"python": str(helper_python), "script": str(helper_script), "output": str(out_path)},
            )
            proc = run_subprocess_with_stage_log(
                cmd,
                lv=lv,
                stage="transcribe",
                label="WhisperX",
                timeout=60 * 60 * 6,
                heartbeat_seconds=60,
                env_overrides={
                    "HF_TOKEN": huggingface_token,
                    "HUGGINGFACE_TOKEN": huggingface_token,
                },
                progress_total_seconds=audio_duration,
                progress_json_path=progress_path,
            )
            result = json.loads(out_path.read_text(encoding="utf-8"))
        else:
            model = whisperx.load_model(
                model_size,
                device,
                compute_type=compute_type,
                language="ja",
                asr_options={
                    "initial_prompt": initial_prompt or "これはニコニコ生放送の録画音声です",
                    "hotwords": hotwords or None,
                },
            )
            postprocess_log(lv, "transcribe", "DEBUG", f"WhisperXモデルロード完了 model={model_size} device={device}")
            result = model.transcribe(str(audio), batch_size=batch_size)
            language = result.get("language") or "ja"
            postprocess_log(lv, "transcribe", "DEBUG", f"WhisperXアライン開始 language={language}")
            align_model, metadata = whisperx.load_align_model(language_code=language, device=device)
            result = whisperx.align(
                result["segments"],
                align_model,
                metadata,
                str(audio),
                device,
                return_char_alignments=False,
            )
            if diarize:
                postprocess_log(
                    lv,
                    "transcribe",
                    "DEBUG",
                    f"WhisperX話者分離開始 min={min_speakers} max={max_speakers}",
                    {"min_speakers": min_speakers, "max_speakers": max_speakers},
                )
                diarize_model = whisperx.DiarizationPipeline(use_auth_token=huggingface_token, device=device)
                diarize_segments = diarize_model(
                    str(audio),
                    min_speakers=int(min_speakers or 1),
                    max_speakers=int(max_speakers or 4),
                )
                result = whisperx.assign_word_speakers(diarize_segments, result)
    except Exception as exc:
        postprocess_log(
            lv,
            "transcribe",
            "ERROR",
            f"WhisperX失敗: {type(exc).__name__}: {exc}",
            {"model": model_size, "device": device, "compute_type": compute_type, "diarize": diarize},
        )
        raise

    rows: list[dict[str, Any]] = []
    last_log_monotonic = time.monotonic()
    started_monotonic = last_log_monotonic
    console_progress = ConsoleProgress("WhisperX", total_seconds=audio_duration)
    for index, segment in enumerate(result.get("segments", [])):
        text = str(segment.get("text") or "").strip()
        if not text:
            continue
        rows.append(
            {
                "segment_index": len(rows),
                "start_seconds": float(segment.get("start") or 0.0),
                "end_seconds": float(segment.get("end") or 0.0),
                "text": text,
                "confidence": None,
                "speaker": str(segment.get("speaker") or ""),
                "language": result.get("language") or "ja",
                "duration": audio_duration,
            }
        )
        elapsed_since_log = time.monotonic() - last_log_monotonic
        if elapsed_since_log >= 1.0:
            last_log_monotonic = time.monotonic()
            current_end = float(segment.get("end") or 0.0)
            console_progress.update(current_end, extra=f"segments={len(rows)}", force=True)
    console_progress.finish()
    if rows:
        postprocess_log(
            lv,
            "transcribe",
            "DEBUG",
            f"WhisperX進捗 segments={len(rows)} processed={hms_seconds(float(rows[-1]['end_seconds']))} total={hms_seconds(audio_duration)}",
            {
                "segments": len(rows),
                "processed_seconds": float(rows[-1]["end_seconds"]),
                "total_seconds": audio_duration,
                "elapsed_wall_seconds": round(time.monotonic() - started_monotonic, 2),
            },
        )
    rows = normalize_transcription_rows_for_timeline(
        rows,
        timeline_offset_seconds=timeline_offset_seconds,
        timeline_end_seconds=timeline_end_seconds,
        segment_index_base=segment_index_base,
    )
    with connect() as conn:
        saved = persist_transcription_rows(
            conn,
            lv,
            rows,
            source_audio_path=str(audio),
            model=f"whisperx:{model_size}",
            replace_scope=replace_scope,
        )
        legacy = export_legacy_transcript_file_from_db(
            conn,
            lv,
            target_dir=Path(target_dir) if target_dir is not None else None,
        )
        if mark_postprocess_done:
            conn.execute(
                """
                UPDATE postprocess_jobs
                SET status = ?, finished_at = ?, updated_at = ?, error = NULL
                WHERE lv = ? AND stage = 'transcribe'
                """,
                ("done", now_micro(), now_micro(), lv),
            )
        conn.commit()
    postprocess_log(
        lv,
        "transcribe",
        "INFO",
        f"WhisperX完了 segments={len(rows)} diarize={int(diarize)}",
        {"segments": len(rows), "diarize": diarize, "model": model_size, "device": device},
    )
    return {
        "lv": lv,
        "audio_path": str(audio),
        "engine": "whisperx",
        "model": model_size,
        "segments": len(rows),
        "saved": saved,
        "legacy_transcript": legacy,
        "diarize": diarize,
    }


def normalize_ndgr_comment_for_archive(
    lv: str,
    comment: dict[str, Any],
    *,
    start_time: int | None = None,
) -> dict[str, Any]:
    posted_at = str(comment.get("posted_at") or "") or None
    date_value = parse_iso_unix_seconds(posted_at)
    vpos = comment.get("vpos")
    try:
        vpos_int = int(vpos) if vpos is not None else None
    except (TypeError, ValueError):
        vpos_int = None

    broadcast_seconds: float | None = None
    if date_value is not None and start_time:
        broadcast_seconds = float(date_value - int(start_time))
    elif vpos_int is not None:
        broadcast_seconds = max(0.0, vpos_int / 100.0)

    timeline_block = int(broadcast_seconds // 10 * 10) if broadcast_seconds is not None else None
    raw_user_id = comment.get("raw_user_id")
    hashed_user_id = comment.get("hashed_user_id")
    user_id = str(comment.get("user_id") or raw_user_id or hashed_user_id or "anonymous")
    return {
        "lv": lv,
        "no": comment.get("no"),
        "comment_id": comment.get("comment_id"),
        "user_id": user_id,
        "raw_user_id": "" if raw_user_id is None else str(raw_user_id),
        "hashed_user_id": "" if hashed_user_id is None else str(hashed_user_id),
        "user_name": str(comment.get("user_name") or ""),
        "text": str(comment.get("text") or ""),
        "date": date_value,
        "posted_at": posted_at,
        "received_at": str(comment.get("received_at") or ""),
        "vpos": vpos_int,
        "broadcast_seconds": broadcast_seconds,
        "timeline_block": timeline_block,
        "premium": int(bool(comment.get("is_premium"))),
        "anonymity": int(bool(comment.get("is_anonymous"))),
        "mail": str(comment.get("mail") or ""),
        "source": str(comment.get("source") or "ndgr"),
        "raw_json": json.dumps(comment, ensure_ascii=False),
    }


def save_archive_comment_from_ndgr(
    conn: sqlite3.Connection,
    lv: str,
    comment: dict[str, Any],
    *,
    start_time: int | None = None,
) -> dict[str, Any]:
    if start_time is None:
        meta = conn.execute(
            """
            SELECT start_time, open_time, begin_time
            FROM broadcast_archive_meta
            WHERE lv = ?
            """,
            (lv,),
        ).fetchone()
        if meta:
            for key in ("start_time", "open_time", "begin_time"):
                value = meta[key]
                if value:
                    try:
                        start_time = int(value)
                        break
                    except (TypeError, ValueError):
                        pass
    row = normalize_ndgr_comment_for_archive(lv, comment, start_time=start_time)
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO archive_comments
            (lv, no, comment_id, user_id, raw_user_id, hashed_user_id, user_name, text,
             date, posted_at, received_at, vpos, broadcast_seconds, timeline_block,
             premium, anonymity, mail, source, raw_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            row["lv"],
            row["no"],
            row["comment_id"],
            row["user_id"],
            row["raw_user_id"],
            row["hashed_user_id"],
            row["user_name"],
            row["text"],
            row["date"],
            row["posted_at"],
            row["received_at"],
            row["vpos"],
            row["broadcast_seconds"],
            row["timeline_block"],
            row["premium"],
            row["anonymity"],
            row["mail"],
            row["source"],
            row["raw_json"],
            now_micro(),
        ),
    )
    inserted = int(cursor.rowcount or 0) > 0
    if inserted:
        update_archive_comment_ranking(conn, lv, row)
    row["inserted"] = inserted
    return row


def record_special_user_broadcast_hit_from_comment(
    conn: sqlite3.Connection,
    lv: str,
    comment_row: dict[str, Any],
) -> dict[str, Any]:
    user_id = str(comment_row.get("user_id") or "").strip()
    if not user_id:
        return {"recorded": False, "reason": "missing_user_id"}
    special = conn.execute(
        """
        SELECT user_id, enabled
        FROM special_users
        WHERE user_id = ?
        """,
        (user_id,),
    ).fetchone()
    if not special or not int(special["enabled"] or 0):
        return {"recorded": False, "reason": "not_enabled_special_user"}

    meta = conn.execute(
        """
        SELECT broadcaster_id, broadcaster_name
        FROM broadcast_archive_meta
        WHERE lv = ?
        """,
        (lv,),
    ).fetchone()
    if meta is None:
        try:
            fetch_and_save_broadcast_archive_meta(conn, lv)
            meta = conn.execute(
                """
                SELECT broadcaster_id, broadcaster_name
                FROM broadcast_archive_meta
                WHERE lv = ?
                """,
                (lv,),
            ).fetchone()
        except Exception:
            meta = None
    broadcaster_id = str(meta["broadcaster_id"] or "").strip() if meta else ""
    broadcaster_name = str(meta["broadcaster_name"] or "").strip() if meta else ""
    if not broadcaster_id:
        return {"recorded": False, "reason": "missing_broadcaster_id"}

    link = conn.execute(
        """
        SELECT 1
        FROM special_user_broadcasters
        WHERE user_id = ?
          AND broadcaster_id = ?
          AND enabled = 1
        LIMIT 1
        """,
        (user_id, broadcaster_id),
    ).fetchone()
    if not link:
        return {"recorded": False, "reason": "not_linked_broadcaster", "broadcaster_id": broadcaster_id}

    current_time = now_micro()
    conn.execute(
        """
        INSERT INTO special_user_broadcast_hits
            (lv, user_id, broadcaster_id, broadcaster_name, first_comment_no,
             first_comment_text, first_seen_at, last_seen_at, comment_count,
             html_upload_requested)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 1)
        ON CONFLICT(lv, user_id, broadcaster_id) DO UPDATE SET
            broadcaster_name = excluded.broadcaster_name,
            last_seen_at = excluded.last_seen_at,
            comment_count = special_user_broadcast_hits.comment_count + 1,
            html_upload_requested = 1
        """,
        (
            lv,
            user_id,
            broadcaster_id,
            broadcaster_name,
            int(comment_row.get("no") or 0) if str(comment_row.get("no") or "").isdigit() else None,
            str(comment_row.get("text") or ""),
            current_time,
            current_time,
        ),
    )
    return {"recorded": True, "user_id": user_id, "broadcaster_id": broadcaster_id}


def special_user_upload_targets_for_lv(conn: sqlite3.Connection, lv: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT h.lv, h.user_id, h.broadcaster_id, h.broadcaster_name,
               u.post_server_url, u.post_server_api_key, u.html_base_url,
               u.html_upload_enabled
        FROM special_user_broadcast_hits h
        JOIN special_users u ON u.user_id = h.user_id
        WHERE h.lv = ?
          AND h.html_upload_requested = 1
          AND u.enabled = 1
          AND u.html_upload_enabled = 1
          AND COALESCE(u.post_server_url, '') <> ''
        ORDER BY h.first_seen_at
        """,
        (lv,),
    ).fetchall()
    return [dict(row) for row in rows]


def filezilla_config_dir(config: Config | None = None) -> Path:
    config = config or load_config()
    if str(config.filezilla_config_dir or "").strip():
        return Path(config.filezilla_config_dir)
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "FileZilla"
    return Path.home() / "AppData" / "Roaming" / "FileZilla"


def decode_filezilla_password(element: ET.Element | None) -> str:
    if element is None or not element.text:
        return ""
    value = element.text.strip()
    if element.get("encoding") == "base64":
        try:
            return base64.b64decode(value).decode("utf-8")
        except Exception:
            return ""
    return value


def parse_filezilla_server(server: ET.Element, *, source: str) -> dict[str, Any]:
    def text(tag: str) -> str:
        element = server.find(tag)
        return (element.text or "").strip() if element is not None and element.text else ""

    return {
        "source": source,
        "name": text("Name"),
        "host": text("Host"),
        "port": int(text("Port") or 0),
        "protocol": int(text("Protocol") or 0),
        "type": int(text("Type") or 0),
        "user": text("User"),
        "password": decode_filezilla_password(server.find("Pass")),
        "remote_dir": text("RemoteDir"),
    }


def list_filezilla_servers(config: Config | None = None) -> list[dict[str, Any]]:
    config_dir = filezilla_config_dir(config)
    servers: list[dict[str, Any]] = []
    for filename in ("sitemanager.xml", "recentservers.xml"):
        path = config_dir / filename
        if not path.exists():
            continue
        try:
            root = ET.fromstring(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        for server in root.findall(".//Server"):
            item = parse_filezilla_server(server, source=str(path))
            if item.get("host"):
                servers.append(item)
    return servers


def resolve_filezilla_server(identifier: str, config: Config | None = None) -> dict[str, Any]:
    identifier = identifier.strip()
    parsed = urlparse(identifier if "://" in identifier else f"filezilla://{identifier}")
    host = parsed.hostname or parsed.netloc or parsed.path.split("/", 1)[0]
    user = parsed.username or ""
    port = parsed.port or 0
    for server in list_filezilla_servers(config):
        if server["host"] != host:
            continue
        if user and server["user"] != user:
            continue
        if port and int(server["port"] or 0) != port:
            continue
        return server
    raise FileNotFoundError(f"FileZilla server setting not found: {identifier}")


def remote_parts(path: str) -> list[str]:
    return [unquote(part) for part in path.replace("\\", "/").split("/") if part]


def ftp_makedirs(ftp: ftplib.FTP, path: str) -> None:
    for part in remote_parts(path):
        try:
            ftp.mkd(part)
        except ftplib.error_perm:
            pass
        ftp.cwd(part)


def upload_file_via_filezilla(destination: str, source_path: Path, config: Config | None = None) -> str:
    parsed = urlparse(destination if "://" in destination else f"filezilla://{destination}")
    server = resolve_filezilla_server(destination, config)
    remote_path = parsed.path or ""
    if server.get("remote_dir") and not remote_path:
        remote_path = str(server["remote_dir"])
    remote_path = remote_path.rstrip("/")
    if not remote_path:
        remote_path = "/"
    remote_dir = f"{remote_path}/{source_path.stem}".replace("//", "/")
    remote_name = source_path.name
    if int(server.get("protocol") or 0) == 1:
        try:
            import paramiko  # type: ignore
        except ImportError as exc:
            raise RuntimeError("SFTP upload requires paramiko") from exc
        transport = paramiko.Transport((server["host"], int(server["port"] or 22)))
        try:
            transport.connect(username=server["user"], password=server["password"])
            sftp = paramiko.SFTPClient.from_transport(transport)
            current = ""
            for part in remote_parts(remote_dir):
                current += "/" + part
                try:
                    sftp.mkdir(current)
                except OSError:
                    pass
            remote_file = f"{remote_dir.rstrip('/')}/{remote_name}"
            sftp.put(str(source_path), remote_file)
            sftp.close()
        finally:
            transport.close()
        return f"sftp://{server['host']}:{int(server['port'] or 22)}{remote_dir}/{remote_name}"

    ftp = ftplib.FTP()
    try:
        ftp.connect(server["host"], int(server["port"] or 21), timeout=60)
        ftp.login(server["user"], server["password"])
        ftp.cwd("/")
        ftp_makedirs(ftp, remote_dir)
        with source_path.open("rb") as fh:
            ftp.storbinary(f"STOR {remote_name}", fh)
    finally:
        try:
            ftp.quit()
        except Exception:
            ftp.close()
    return f"ftp://{server['host']}:{int(server['port'] or 21)}{remote_dir}/{remote_name}"


def upload_html_for_special_user_hits(lv: str, html_path: Path | str) -> list[dict[str, Any]]:
    html_path = Path(html_path)
    if not html_path.exists():
        raise FileNotFoundError(str(html_path))
    results: list[dict[str, Any]] = []
    config = load_config()
    with connect() as conn:
        targets = special_user_upload_targets_for_lv(conn, lv)
        for target in targets:
            destination = str(target.get("post_server_url") or "").strip()
            user_id = str(target.get("user_id") or "")
            broadcaster_id = str(target.get("broadcaster_id") or "")
            try:
                if destination.lower().startswith(("http://", "https://")):
                    headers = {}
                    api_key = str(target.get("post_server_api_key") or "")
                    if api_key:
                        headers["X-API-Key"] = api_key
                    with html_path.open("rb") as fh:
                        response = requests.post(
                            destination,
                            data={"lv": lv, "user_id": user_id, "broadcaster_id": broadcaster_id},
                            files={"file": (html_path.name, fh, "text/html; charset=utf-8")},
                            headers=headers,
                            timeout=60,
                        )
                    response.raise_for_status()
                    status = "uploaded"
                    error = ""
                elif destination.lower().startswith("filezilla:"):
                    destination = upload_file_via_filezilla(destination, html_path, config)
                    status = "uploaded"
                    error = ""
                else:
                    dest_dir = Path(destination).expanduser() / user_id / lv
                    dest_dir.mkdir(parents=True, exist_ok=True)
                    dest_file = dest_dir / html_path.name
                    shutil.copy2(html_path, dest_file)
                    destination = str(dest_file)
                    status = "copied"
                    error = ""
                conn.execute(
                    """
                    UPDATE special_user_broadcast_hits
                    SET html_uploaded_at = ?
                    WHERE lv = ? AND user_id = ? AND broadcaster_id = ?
                    """,
                    (now_micro(), lv, user_id, broadcaster_id),
                )
            except Exception as exc:
                status = "failed"
                error = f"{type(exc).__name__}: {exc}"
            conn.execute(
                """
                INSERT INTO html_upload_events
                    (lv, user_id, broadcaster_id, source_path, destination, status, error, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (lv, user_id, broadcaster_id, str(html_path), destination, status, error, now_micro()),
            )
            results.append(
                {
                    "user_id": user_id,
                    "broadcaster_id": broadcaster_id,
                    "destination": destination,
                    "status": status,
                    "error": error,
                }
            )
        conn.commit()
    return results


def record_broadcaster_monitor_special_user_hits_from_archive(conn: sqlite3.Connection, lv: str) -> dict[str, Any]:
    meta = conn.execute(
        """
        SELECT broadcaster_id, broadcaster_name
        FROM broadcast_archive_meta
        WHERE lv = ?
        """,
        (lv,),
    ).fetchone()
    if meta is None:
        try:
            fetch_and_save_broadcast_archive_meta(conn, lv)
            meta = conn.execute(
                """
                SELECT broadcaster_id, broadcaster_name
                FROM broadcast_archive_meta
                WHERE lv = ?
                """,
                (lv,),
            ).fetchone()
        except Exception:
            meta = None
    broadcaster_id = str(meta["broadcaster_id"] or "").strip() if meta else ""
    broadcaster_name = str(meta["broadcaster_name"] or "").strip() if meta else ""
    if not broadcaster_id:
        return {"recorded": 0, "reason": "missing_broadcaster_id"}

    rows = conn.execute(
        """
        SELECT c.user_id, c.no, c.text, c.broadcast_seconds, COUNT(*) AS comment_count
        FROM archive_comments c
        JOIN special_users u
          ON u.user_id = c.user_id
         AND u.enabled = 1
        JOIN special_user_broadcasters b
          ON b.user_id = c.user_id
         AND b.broadcaster_id = ?
         AND b.enabled = 1
        WHERE c.lv = ?
        GROUP BY c.user_id
        ORDER BY MIN(c.broadcast_seconds), MIN(c.no)
        """,
        (broadcaster_id, lv),
    ).fetchall()
    current_time = now_micro()
    recorded = 0
    for row in rows:
        conn.execute(
            """
            INSERT INTO broadcaster_monitor_special_user_hits
                (lv, user_id, broadcaster_id, broadcaster_name, first_comment_no,
                 first_comment_text, first_comment_seconds, detected_at, comment_count,
                 html_upload_requested)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(lv, user_id, broadcaster_id) DO UPDATE SET
                broadcaster_name = excluded.broadcaster_name,
                first_comment_no = COALESCE(broadcaster_monitor_special_user_hits.first_comment_no, excluded.first_comment_no),
                first_comment_text = COALESCE(broadcaster_monitor_special_user_hits.first_comment_text, excluded.first_comment_text),
                first_comment_seconds = COALESCE(broadcaster_monitor_special_user_hits.first_comment_seconds, excluded.first_comment_seconds),
                detected_at = excluded.detected_at,
                comment_count = excluded.comment_count,
                html_upload_requested = 1
            """,
            (
                lv,
                str(row["user_id"] or ""),
                broadcaster_id,
                broadcaster_name,
                int(row["no"] or 0) if row["no"] is not None else None,
                str(row["text"] or ""),
                float(row["broadcast_seconds"] or 0.0),
                current_time,
                int(row["comment_count"] or 0),
            ),
        )
        recorded += 1
    return {"recorded": recorded, "broadcaster_id": broadcaster_id}


def broadcaster_monitor_upload_targets_for_lv(conn: sqlite3.Connection, lv: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT h.lv, h.user_id, h.broadcaster_id, h.broadcaster_name,
               u.post_server_url, u.post_server_api_key, u.html_base_url,
               u.html_upload_enabled
        FROM broadcaster_monitor_special_user_hits h
        JOIN special_users u ON u.user_id = h.user_id
        WHERE h.lv = ?
          AND h.html_upload_requested = 1
          AND u.enabled = 1
          AND u.html_upload_enabled = 1
          AND COALESCE(u.post_server_url, '') <> ''
        ORDER BY h.detected_at
        """,
        (lv,),
    ).fetchall()
    return [dict(row) for row in rows]


def generated_archive_html_paths_for_lv(conn: sqlite3.Connection, lv: str) -> list[Path]:
    """Return existing desktop/mobile archive HTML files in the LV target only."""
    directories: set[Path] = set()
    meta = conn.execute(
        "SELECT broadcaster_id, html_path FROM broadcast_archive_meta WHERE lv = ?",
        (lv,),
    ).fetchone()
    if meta:
        html_path = str(meta["html_path"] or "").strip()
        if html_path:
            directories.add(Path(html_path).parent)
        try:
            directories.add(
                broadcast_target_dir(
                    lv,
                    load_config(),
                    broadcaster_id=str(meta["broadcaster_id"] or "") or None,
                )
            )
        except Exception:
            pass
    job = conn.execute(
        "SELECT target_dir FROM recording_jobs WHERE lv = ?",
        (lv,),
    ).fetchone()
    if job and str(job["target_dir"] or "").strip():
        directories.add(Path(str(job["target_dir"])))

    paths: set[Path] = set()
    for directory in directories:
        if not directory.is_dir():
            continue
        for path in directory.glob(f"{lv}*.html"):
            if path.is_file():
                paths.add(path.resolve())
    return sorted(paths, key=lambda path: path.name.casefold())


def existing_generated_archive_html(lv: str) -> Path | None:
    """Find a completed PC archive for GUI import de-duplication."""
    lv = str(lv or "").strip()
    if not re.fullmatch(r"lv\d+", lv):
        return None
    with connect() as conn:
        paths = generated_archive_html_paths_for_lv(conn, lv)
    pc_paths = [
        path
        for path in paths
        if path.name.lower().startswith(f"{lv.lower()}_")
        and not path.stem.lower().endswith("_mobile")
    ]
    if pc_paths:
        return max(pc_paths, key=lambda path: path.stat().st_mtime)
    try:
        target_root = niconico_platform_target_root(load_config())
        candidates = [
            path
            for path in target_root.rglob(f"{lv}_*.html")
            if path.is_file() and not path.stem.lower().endswith("_mobile")
        ]
        return max(candidates, key=lambda path: path.stat().st_mtime) if candidates else None
    except Exception:
        return None


def rewrite_comment_offset_state_in_html(path: Path | str, state: dict[str, Any]) -> bool:
    html_path = Path(path)
    source = html_path.read_text(encoding="utf-8")
    state_json = json.dumps(state, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    replaced, count = COMMENT_OFFSET_STATE_PATTERN.subn(
        lambda match: f"{match.group(1)}{state_json}{match.group(2)}",
        source,
        count=1,
    )
    if count != 1:
        return False
    temp_path = html_path.with_name(f".{html_path.name}.comment-offset.tmp")
    temp_path.write_text(replaced, encoding="utf-8")
    temp_path.replace(html_path)
    return True


def confirm_archive_comment_offset(
    lv: str,
    offset_seconds: int,
    confirm_token: str,
) -> dict[str, Any]:
    lv = str(lv or "").strip()
    if not re.fullmatch(r"lv\d+", lv):
        raise ValueError("invalid lv")
    try:
        offset = int(offset_seconds)
    except (TypeError, ValueError) as exc:
        raise ValueError("offset_seconds must be an integer") from exc
    if not -86_400 <= offset <= 86_400:
        raise ValueError("offset_seconds is out of range")
    token = str(confirm_token or "").strip()
    if not token:
        raise ValueError("confirm_token is required")

    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM archive_comment_time_adjustments WHERE lv = ?",
            (lv,),
        ).fetchone()
        if row is None or not hmac.compare_digest(str(row["confirm_token"] or ""), token):
            raise ValueError("invalid confirm_token")
        already_confirmed = bool(row["confirmed"])
        previous_offset = int(row["offset_seconds"] or 0)
        if already_confirmed and previous_offset != offset:
            raise ValueError("comment offset is already confirmed")
        candidates = generated_archive_html_paths_for_lv(conn, lv)

    marker_paths = []
    for path in candidates:
        try:
            if COMMENT_OFFSET_STATE_PATTERN.search(path.read_text(encoding="utf-8")):
                marker_paths.append(path)
        except (OSError, UnicodeError):
            continue
    if not marker_paths:
        raise ValueError("generated archive HTML with confirmation marker was not found")

    confirmed_at = str(row["confirmed_at"] or "") if already_confirmed else now_micro()
    state = {
        "lv": lv,
        "offsetSeconds": offset,
        "confirmed": True,
        "confirmToken": token,
        "confirmedAt": confirmed_at,
    }
    rewritten = []
    for path in marker_paths:
        if not rewrite_comment_offset_state_in_html(path, state):
            raise RuntimeError(f"comment offset marker rewrite failed: {path}")
        rewritten.append(str(path))

    with connect() as conn:
        conn.execute(
            """
            UPDATE archive_comment_time_adjustments
            SET offset_seconds = ?, confirmed = 1, confirmed_at = ?,
                html_paths_json = ?, updated_at = ?
            WHERE lv = ? AND confirm_token = ?
            """,
            (
                offset,
                confirmed_at,
                json.dumps(rewritten, ensure_ascii=False),
                now_micro(),
                lv,
                token,
            ),
        )
        conn.commit()
    postprocess_log(
        lv,
        "comment_offset",
        "INFO",
        f"コメント時刻補正を確定 offset={offset:+d}s html={len(rewritten)}",
        {"offset_seconds": offset, "html_paths": rewritten, "confirmed_at": confirmed_at},
    )
    return {
        "lv": lv,
        "offset_seconds": offset,
        "confirmed": True,
        "confirmed_at": confirmed_at,
        "html_paths": rewritten,
        "already_confirmed": already_confirmed,
    }


def upload_broadcaster_monitor_html_for_special_user_hits(lv: str, html_path: Path | str) -> list[dict[str, Any]]:
    html_path = Path(html_path)
    if not html_path.exists():
        raise FileNotFoundError(str(html_path))
    results: list[dict[str, Any]] = []
    config = load_config()
    with connect() as conn:
        targets = broadcaster_monitor_upload_targets_for_lv(conn, lv)
        for target in targets:
            destination = str(target.get("post_server_url") or "").strip()
            user_id = str(target.get("user_id") or "")
            broadcaster_id = str(target.get("broadcaster_id") or "")
            try:
                if destination.lower().startswith(("http://", "https://")):
                    headers = {}
                    api_key = str(target.get("post_server_api_key") or "")
                    if api_key:
                        headers["X-API-Key"] = api_key
                    with html_path.open("rb") as fh:
                        response = requests.post(
                            destination,
                            data={
                                "route": "broadcaster_monitor",
                                "lv": lv,
                                "user_id": user_id,
                                "broadcaster_id": broadcaster_id,
                            },
                            files={"file": (html_path.name, fh, "text/html; charset=utf-8")},
                            headers=headers,
                            timeout=60,
                        )
                    response.raise_for_status()
                    status = "uploaded"
                    error = ""
                elif destination.lower().startswith("filezilla:"):
                    destination = upload_file_via_filezilla(destination, html_path, config)
                    status = "uploaded"
                    error = ""
                else:
                    dest_dir = Path(destination).expanduser() / "broadcaster_monitor" / user_id / lv
                    dest_dir.mkdir(parents=True, exist_ok=True)
                    dest_file = dest_dir / html_path.name
                    shutil.copy2(html_path, dest_file)
                    destination = str(dest_file)
                    status = "copied"
                    error = ""
                conn.execute(
                    """
                    UPDATE broadcaster_monitor_special_user_hits
                    SET html_uploaded_at = ?
                    WHERE lv = ? AND user_id = ? AND broadcaster_id = ?
                    """,
                    (now_micro(), lv, user_id, broadcaster_id),
                )
            except Exception as exc:
                status = "failed"
                error = f"{type(exc).__name__}: {exc}"
            conn.execute(
                """
                INSERT INTO html_upload_events
                    (lv, user_id, broadcaster_id, source_path, destination, status, error, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (lv, user_id, broadcaster_id, str(html_path), destination, status, error, now_micro()),
            )
            results.append(
                {
                    "user_id": user_id,
                    "broadcaster_id": broadcaster_id,
                    "destination": destination,
                    "status": status,
                    "error": error,
                    "route": "broadcaster_monitor",
                }
            )
        conn.commit()
    return results


def recording_start_time_for_lv(conn: sqlite3.Connection, lv: str) -> datetime | None:
    row = conn.execute(
        """
        SELECT started_at, event_at
        FROM recording_events
        WHERE lv = ?
          AND event_type = 'started'
        ORDER BY event_at ASC, id ASC
        LIMIT 1
        """,
        (lv,),
    ).fetchone()
    value = ""
    if row:
        value = str(row["started_at"] or row["event_at"] or "").strip()
    if not value:
        row = conn.execute(
            """
            SELECT started_at
            FROM recording_jobs
            WHERE lv = ?
            ORDER BY updated_at ASC
            LIMIT 1
            """,
            (lv,),
        ).fetchone()
        if row:
            value = str(row["started_at"] or "").strip()
    if not value:
        return None
    try:
        return iso_to_datetime(value)
    except Exception:
        return None


def unix_seconds_to_local_datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    if seconds > 10_000_000_000:
        seconds /= 1000.0
    return datetime.fromtimestamp(seconds).replace(tzinfo=None)


def calculate_recording_time_diff_seconds(conn: sqlite3.Connection, lv: str, meta: dict[str, Any]) -> int:
    recording_start = recording_start_time_for_lv(conn, lv)
    open_time = unix_seconds_to_local_datetime(meta.get("open_time") or meta.get("begin_time") or meta.get("start_time"))
    if recording_start is None or open_time is None:
        existing = meta.get("time_diff_seconds")
        try:
            return int(existing) if existing is not None else 0
        except (TypeError, ValueError):
            return 0
    return max(0, int(round((recording_start - open_time).total_seconds())))


def archive_meta_to_legacy_data(
    meta: dict[str, Any],
    *,
    target_dir: Path | None = None,
    time_diff_seconds: int | None = None,
    video_duration: float | None = None,
) -> dict[str, Any]:
    lv = str(meta.get("lv") or "")
    config = load_config()
    target_dir = target_dir or broadcast_target_dir(lv, config, broadcaster_id=str(meta.get("broadcaster_id") or "") or None)
    account_dir = target_dir.parent
    start_time = meta.get("start_time") or meta.get("begin_time") or meta.get("open_time") or ""
    server_time = meta.get("server_time") or start_time or ""
    open_time = meta.get("open_time") or start_time or ""
    if time_diff_seconds is None:
        try:
            time_diff_seconds = int(meta.get("time_diff_seconds") or 0)
        except (TypeError, ValueError):
            time_diff_seconds = 0
    try:
        video_duration_value = float(video_duration) if video_duration is not None else 0.0
    except (TypeError, ValueError):
        video_duration_value = 0.0
    return {
        "lv_value": lv,
        "timestamp": now_micro(),
        "server_time": "" if server_time is None else str(server_time),
        "begin_time": meta.get("begin_time"),
        "video_duration": video_duration_value,
        "time_diff_seconds": time_diff_seconds,
        "account_directory_path": str(account_dir),
        "broadcast_directory_path": str(target_dir),
        "ncv_xml_path": "",
        "platform_xml_path": "",
        "live_num": lv.removeprefix("lv"),
        "elapsed_time": "",
        "live_title": str(meta.get("title") or ""),
        "broadcaster": str(meta.get("broadcaster_name") or ""),
        "default_community": "",
        "community_name": "",
        "open_time": "" if open_time is None else str(open_time),
        "start_time": "" if start_time is None else str(start_time),
        "end_time": "" if meta.get("end_time") is None else str(meta.get("end_time")),
        "watch_count": "",
        "comment_count": "",
        "owner_id": str(meta.get("broadcaster_id") or ""),
        "owner_name": str(meta.get("broadcaster_name") or ""),
        "previous_summary": "",
        "summary_text": "",
        "intro_chat": [],
        "outro_chat": [],
        "sentiment_stats": {
            "avg_center": 0.0,
            "avg_positive": 0.0,
            "avg_negative": 0.0,
            "max_center": 0.0,
            "max_positive": 0.0,
            "max_negative": 0.0,
            "max_center_time": 0,
            "max_positive_time": 0,
            "max_negative_time": 0,
            "total_segments": 0,
        },
        "word_ranking": [],
        "html_file_path": "",
        "source": "ndgr",
    }


def archive_comment_row_to_legacy_comment(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    get = row.get if isinstance(row, dict) else row.__getitem__

    def value(name: str, default: Any = "") -> Any:
        try:
            result = get(name)
        except Exception:
            return default
        return default if result is None else result

    return {
        "no": int(value("no", 0) or 0),
        "user_id": str(value("user_id", "")),
        "user_name": str(value("user_name", "")),
        "text": str(value("text", "")),
        "date": int(value("date", 0) or 0),
        "broadcast_seconds": float(value("broadcast_seconds", 0.0) or 0.0),
        "timeline_block": int(value("timeline_block", 0) or 0),
        "premium": int(value("premium", 0) or 0),
        "anonymity": bool(value("anonymity", 0)),
    }


def export_legacy_archive_files_from_ndgr(
    conn: sqlite3.Connection,
    lv: str,
    *,
    target_dir: Path | None = None,
    video_duration: float | None = None,
    time_diff_seconds: int | None = None,
) -> dict[str, Any]:
    lv = str(lv).strip()
    config = load_config()
    meta_row = conn.execute(
        "SELECT * FROM broadcast_archive_meta WHERE lv = ?",
        (lv,),
    ).fetchone()
    if meta_row is None:
        meta = fetch_and_save_broadcast_archive_meta(conn, lv)
    else:
        meta = dict(meta_row)
    target_dir = target_dir or broadcast_target_dir(
        lv,
        config,
        broadcaster_id=str(meta.get("broadcaster_id") or "") or None,
    )
    target_dir.mkdir(parents=True, exist_ok=True)

    if time_diff_seconds is None:
        time_diff_seconds = calculate_recording_time_diff_seconds(conn, lv, meta)
    else:
        time_diff_seconds = max(0, int(time_diff_seconds))
    if meta.get("time_diff_seconds") != time_diff_seconds:
        conn.execute(
            "UPDATE broadcast_archive_meta SET time_diff_seconds = ? WHERE lv = ?",
            (time_diff_seconds, lv),
        )
        meta["time_diff_seconds"] = time_diff_seconds

    if video_duration is None:
        joined_mp4 = target_dir / f"{lv}_joined.mp4"
        if joined_mp4.exists():
            try:
                video_duration = probe_media_duration_seconds(joined_mp4)
            except Exception:
                video_duration = None
        if video_duration is None:
            try:
                timeline_plan = build_recording_segment_timeline_plan(slnico_storage_root(), lv=lv)
                video_duration = float(timeline_plan.get("total_duration_seconds") or 0.0)
            except Exception:
                video_duration = None

    data = archive_meta_to_legacy_data(
        meta,
        target_dir=target_dir,
        time_diff_seconds=time_diff_seconds,
        video_duration=video_duration,
    )
    comments_rows = conn.execute(
        """
        SELECT *
        FROM archive_comments
        WHERE lv = ?
        ORDER BY broadcast_seconds ASC, no ASC, id ASC
        """,
        (lv,),
    ).fetchall()
    comments = [archive_comment_row_to_legacy_comment(row) for row in comments_rows]
    ranking_rows = conn.execute(
        """
        SELECT *
        FROM archive_comment_ranking
        WHERE lv = ?
        ORDER BY rank ASC, comment_count DESC, first_comment_time ASC, user_id
        """,
        (lv,),
    ).fetchall()
    ranking = [
        {
            "user_id": str(row["user_id"] or ""),
            "user_name": str(row["user_name"] or ""),
            "comment_count": int(row["comment_count"] or 0),
            "first_comment": str(row["first_comment"] or ""),
            "first_comment_time": float(row["first_comment_time"] or 0.0),
            "last_comment": str(row["last_comment"] or ""),
            "last_comment_time": float(row["last_comment_time"] or 0.0),
            "premium": int(row["premium"] or 0),
            "anonymity": bool(row["anonymity"] or 0),
            "rank": int(row["rank"] or 0),
        }
        for row in ranking_rows
    ]

    data["comment_count"] = str(len(comments))
    data_path = target_dir / f"{lv}_data.json"
    comments_path = target_dir / f"{lv}_comments.json"
    ranking_path = target_dir / f"{lv}_comment_ranking.json"
    data_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    existing_broadcast_data: dict[str, Any] = {}
    row = conn.execute(
        "SELECT payload_json FROM archive_broadcast_data WHERE lv = ?",
        (lv,),
    ).fetchone()
    if row:
        try:
            loaded = json.loads(str(row["payload_json"] or "{}"))
            if isinstance(loaded, dict):
                existing_broadcast_data = loaded
        except Exception:
            existing_broadcast_data = {}
    generated_keys = {
        "summary_text",
        "summary_generated_at",
        "music_generation",
        "image_generation",
        "intro_chat",
        "outro_chat",
        "conversation_generated_at",
        "sentiment_stats",
        "word_ranking",
        "html_file_path",
    }
    db_data = dict(data)
    for key in generated_keys:
        if key in existing_broadcast_data:
            db_data[key] = existing_broadcast_data[key]
    conn.execute(
        """
        INSERT INTO archive_broadcast_data (lv, payload_json, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(lv) DO UPDATE SET
            payload_json = excluded.payload_json,
            updated_at = excluded.updated_at
        """,
        (lv, json.dumps(db_data, ensure_ascii=False), now_micro()),
    )
    comments_path.write_text(
        json.dumps(
            {
                "lv_value": lv,
                "total_comments": len(comments),
                "created_at": now_micro(),
                "comments": comments,
                "source": "ndgr",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    ranking_path.write_text(
        json.dumps(
            {
                "lv_value": lv,
                "total_users": len(ranking),
                "created_at": now_micro(),
                "ranking": ranking,
                "source": "ndgr",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return {
        "lv": lv,
        "data_path": str(data_path),
        "comments_path": str(comments_path),
        "ranking_path": str(ranking_path),
        "comments": len(comments),
        "ranking": len(ranking),
    }


def update_archive_comment_ranking(conn: sqlite3.Connection, lv: str, comment: dict[str, Any]) -> None:
    user_id = str(comment.get("user_id") or "anonymous")
    current = conn.execute(
        "SELECT * FROM archive_comment_ranking WHERE lv = ? AND user_id = ?",
        (lv, user_id),
    ).fetchone()
    comment_time = comment.get("broadcast_seconds")
    if current is None:
        conn.execute(
            """
            INSERT INTO archive_comment_ranking
                (lv, user_id, user_name, comment_count, first_comment, first_comment_time,
                 last_comment, last_comment_time, premium, anonymity, rank, updated_at)
            VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?, NULL, ?)
            """,
            (
                lv,
                user_id,
                comment.get("user_name") or "",
                comment.get("text") or "",
                comment_time,
                comment.get("text") or "",
                comment_time,
                int(comment.get("premium") or 0),
                int(comment.get("anonymity") or 0),
                now_micro(),
            ),
        )
    else:
        conn.execute(
            """
            UPDATE archive_comment_ranking
            SET comment_count = comment_count + 1,
                user_name = CASE WHEN user_name = '' THEN ? ELSE user_name END,
                last_comment = ?,
                last_comment_time = ?,
                premium = MAX(premium, ?),
                anonymity = MAX(anonymity, ?),
                updated_at = ?
            WHERE lv = ? AND user_id = ?
            """,
            (
                comment.get("user_name") or "",
                comment.get("text") or "",
                comment_time,
                int(comment.get("premium") or 0),
                int(comment.get("anonymity") or 0),
                now_micro(),
                lv,
                user_id,
            ),
        )
    rerank_archive_comments(conn, lv)


def rerank_archive_comments(conn: sqlite3.Connection, lv: str) -> None:
    rows = conn.execute(
        """
        SELECT user_id
        FROM archive_comment_ranking
        WHERE lv = ?
        ORDER BY comment_count DESC, first_comment_time ASC, user_id
        """,
        (lv,),
    ).fetchall()
    for index, row in enumerate(rows, 1):
        conn.execute(
            "UPDATE archive_comment_ranking SET rank = ? WHERE lv = ? AND user_id = ?",
            (index, lv, row["user_id"]),
        )


def save_monitored_broadcaster_details(broadcaster_id: str, values: dict[str, Any]) -> None:
    allowed = {
        "broadcaster_name",
        "source_lv",
        "enabled",
        *default_broadcaster_monitor_settings().keys(),
        "summary_engine",
        "ai_conversation_engine",
        "special_user_summary_engine",
        "ai_analysis_model",
        "ai_analysis_api_key",
        "ai_reaction_model",
        "ai_reaction_api_key",
        "character1_name",
        "character1_image_url",
        "character1_fullbody_image_url",
        "character1_image_flip",
        "character1_personality",
        "character2_name",
        "character2_image_url",
        "character2_fullbody_image_url",
        "character2_image_flip",
        "character2_personality",
        "post_server_url",
        "post_server_api_key",
        "faster_whisper_model",
        "whisperx_model",
        "whisperx_enabled",
        "transcription_initial_prompt",
        "transcription_hotwords_enabled",
        "speaker_diarization_enabled",
        "diarization_min_speakers",
        "diarization_max_speakers",
        "html_upload_enabled",
        "html_base_url",
        "archive_tags",
        "summary_prompt",
        "image_prompt",
        "music_prompt",
        "intro_conversation_prompt",
        "outro_conversation_prompt",
        "recording_output_dir",
    }
    broadcaster_id = broadcaster_id.strip()
    updates = {key: value for key, value in values.items() if key in allowed}
    if not broadcaster_id or not updates:
        return
    updates["updated_at"] = now()
    assignments = ", ".join(f"{key} = ?" for key in updates)
    with connect() as conn:
        conn.execute(
            f"UPDATE monitored_broadcasters SET {assignments} WHERE broadcaster_id = ?",
            [*updates.values(), broadcaster_id],
        )
        conn.commit()


def update_monitored_broadcaster_setting(broadcaster_id: str, key: str, enabled: bool) -> None:
    allowed = set(default_broadcaster_monitor_settings())
    if key not in allowed:
        raise ValueError(f"unknown broadcaster monitor setting: {key}")
    with connect() as conn:
        conn.execute(
            f"UPDATE monitored_broadcasters SET {key} = ?, updated_at = ? WHERE broadcaster_id = ?",
            (int(enabled), now(), broadcaster_id),
        )
        conn.commit()


def delete_monitored_broadcaster(broadcaster_id: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM monitored_broadcasters WHERE broadcaster_id = ?", (broadcaster_id,))
        conn.commit()


def list_special_users() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT user_id, label, note, enabled, updated_at
            FROM special_users
            ORDER BY updated_at DESC, user_id
            """
        ).fetchall()
    return [dict(row) for row in rows]


def update_special_user_enabled(user_id: str, enabled: bool) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE special_users SET enabled = ?, updated_at = ? WHERE user_id = ?",
            (int(enabled), now(), user_id),
        )
        conn.commit()


def list_special_user_ids(conn: sqlite3.Connection) -> list[str]:
    return [
        str(row["user_id"])
        for row in conn.execute(
            """
            SELECT user_id
            FROM special_users
            WHERE user_id IS NOT NULL AND user_id != ''
              AND enabled = 1
            ORDER BY user_id
            """
        ).fetchall()
    ]


def special_user_exists(conn: sqlite3.Connection, user_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM special_users WHERE user_id = ? AND enabled = 1 LIMIT 1",
        (user_id,),
    ).fetchone()
    return row is not None


def list_enabled_linked_broadcaster_ids(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        """
        SELECT DISTINCT b.broadcaster_id
        FROM special_user_broadcasters b
        JOIN special_users u ON u.user_id = b.user_id
        WHERE b.enabled = 1
          AND u.enabled = 1
          AND b.broadcaster_id IS NOT NULL
          AND b.broadcaster_id != ''
        """
    ).fetchall()
    return {str(row["broadcaster_id"]).strip() for row in rows if str(row["broadcaster_id"]).strip()}


def enabled_special_user_count_by_broadcaster(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        """
        SELECT b.broadcaster_id, COUNT(DISTINCT b.user_id) AS special_user_count
        FROM special_user_broadcasters b
        JOIN special_users u ON u.user_id = b.user_id
        WHERE b.enabled = 1
          AND u.enabled = 1
          AND b.broadcaster_id IS NOT NULL
          AND b.broadcaster_id != ''
        GROUP BY b.broadcaster_id
        """
    ).fetchall()
    return {
        str(row["broadcaster_id"]).strip(): int(row["special_user_count"] or 0)
        for row in rows
        if str(row["broadcaster_id"]).strip()
    }


def list_enabled_special_user_broadcasters(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT b.user_id, b.broadcaster_id, b.broadcaster_name
        FROM special_user_broadcasters b
        JOIN special_users u ON u.user_id = b.user_id
        WHERE b.enabled = 1
          AND u.enabled = 1
          AND b.broadcaster_id IS NOT NULL
          AND b.broadcaster_id != ''
        ORDER BY b.user_id, b.broadcaster_id
        """
    ).fetchall()
    return [dict(row) for row in rows]


def auto_link_special_user_broadcaster(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    broadcaster_id: str,
    broadcaster_name: str = "",
) -> None:
    if not user_id.strip() or not broadcaster_id.strip():
        return
    current_time = now()
    conn.execute(
        """
        INSERT INTO special_user_broadcasters
            (user_id, broadcaster_id, broadcaster_name, enabled, created_at, updated_at)
        VALUES (?, ?, ?, 1, ?, ?)
        ON CONFLICT(user_id, broadcaster_id) DO UPDATE SET
            broadcaster_name = CASE
                WHEN excluded.broadcaster_name != '' THEN excluded.broadcaster_name
                ELSE special_user_broadcasters.broadcaster_name
            END,
            enabled = 1,
            updated_at = excluded.updated_at
        """,
        (user_id.strip(), broadcaster_id.strip(), broadcaster_name.strip(), current_time, current_time),
    )


def bulk_link_special_user_broadcasters(
    user_id: str,
    broadcasters: list[dict[str, Any]],
) -> dict[str, int]:
    user_id = user_id.strip()
    current_time = now()
    inserted = 0
    updated = 0
    with connect() as conn:
        for row in broadcasters:
            broadcaster_id = str(row.get("broadcaster_id") or row.get("user_id") or "").strip()
            if not user_id or not broadcaster_id:
                continue
            broadcaster_name = str(row.get("broadcaster_name") or row.get("name") or "").strip()
            existed = conn.execute(
                """
                SELECT 1
                FROM special_user_broadcasters
                WHERE user_id = ? AND broadcaster_id = ?
                """,
                (user_id, broadcaster_id),
            ).fetchone()
            conn.execute(
                """
                INSERT INTO special_user_broadcasters
                    (user_id, broadcaster_id, broadcaster_name, enabled, created_at, updated_at)
                VALUES (?, ?, ?, 1, ?, ?)
                ON CONFLICT(user_id, broadcaster_id) DO UPDATE SET
                    broadcaster_name = CASE
                        WHEN excluded.broadcaster_name != '' THEN excluded.broadcaster_name
                        ELSE special_user_broadcasters.broadcaster_name
                    END,
                    enabled = 1,
                    updated_at = excluded.updated_at
                """,
                (user_id, broadcaster_id, broadcaster_name, current_time, current_time),
            )
            if existed:
                updated += 1
            else:
                inserted += 1
        conn.commit()
    return {"inserted": inserted, "updated": updated, "total": len(broadcasters)}


def resolve_reaction_settings(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    broadcaster_id: str,
    comment_text: str = "",
) -> dict[str, Any]:
    user = conn.execute(
        """
        SELECT user_id, label, enabled,
               reaction_model, reaction_api_key, reaction_engine, reaction_use_codex, reaction_effort, reaction_session_id, reaction_skip_prompt, reaction_max_chars,
               reaction_split_delay, reaction_delay_seconds, max_reactions,
               basic_reaction_enabled, basic_reaction_type,
               basic_reaction_messages, basic_reaction_prompt
        FROM special_users
        WHERE user_id = ?
        """,
        (user_id,),
    ).fetchone()
    if not user:
        return {"enabled": False, "source": "none", "reason": "special_user_not_found"}
    if not int(user["enabled"] or 0):
        return {"enabled": False, "source": "none", "reason": "special_user_disabled"}

    user_trigger = resolve_special_user_trigger(
        conn,
        user_id=user_id,
        comment_text=comment_text,
    )
    if user_trigger:
        return {
            "enabled": True,
            "source": "special_user_trigger",
            "user_id": user_id,
            "broadcaster_id": broadcaster_id,
            "trigger_id": user_trigger["id"],
            "matched_keyword": user_trigger["keyword"],
            "reaction_type": user_trigger["action_type"] or "fixed",
            "messages": user_trigger["action_payload"] or "",
            "prompt": user_trigger["action_payload"] or DEFAULT_AI_REACTION_PROMPT,
            "max_reactions": int(user["max_reactions"] or 1),
            "reaction_delay_seconds": float(user["reaction_delay_seconds"] or 0.0),
            "reaction_model": user["reaction_model"] or "",
            "reaction_api_key": user["reaction_api_key"] or "",
            "reaction_engine": user["reaction_engine"] or ("codex_exec" if int(user["reaction_use_codex"] or 0) else "openai"),
            "reaction_effort": user["reaction_effort"] or "medium",
            "reaction_session_id": user["reaction_session_id"] or "",
            "reaction_skip_prompt": user["reaction_skip_prompt"] or DEFAULT_AI_REACTION_SKIP_PROMPT,
            "reaction_max_chars": int(user["reaction_max_chars"] or 100),
            "reaction_split_delay": float(user["reaction_split_delay"] or 1.0),
        }

    trigger = resolve_broadcaster_trigger(
        conn,
        user_id=user_id,
        broadcaster_id=broadcaster_id,
        comment_text=comment_text,
    )
    if trigger:
        return {
            "enabled": True,
            "source": "trigger",
            "user_id": user_id,
            "broadcaster_id": broadcaster_id,
            "trigger_id": trigger["id"],
            "trigger_name": trigger["trigger_name"],
            "matched_keyword": trigger["keyword"],
            "reaction_type": trigger["action_type"] or "fixed",
            "messages": trigger["action_payload"] or "",
            "prompt": trigger["action_payload"] or DEFAULT_AI_REACTION_PROMPT,
            "max_reactions": int(user["max_reactions"] or 1),
            "reaction_delay_seconds": float(user["reaction_delay_seconds"] or 0.0),
            "reaction_model": user["reaction_model"] or "",
            "reaction_api_key": user["reaction_api_key"] or "",
            "reaction_engine": user["reaction_engine"] or ("codex_exec" if int(user["reaction_use_codex"] or 0) else "openai"),
            "reaction_effort": user["reaction_effort"] or "medium",
            "reaction_session_id": user["reaction_session_id"] or "",
            "reaction_skip_prompt": user["reaction_skip_prompt"] or DEFAULT_AI_REACTION_SKIP_PROMPT,
            "reaction_max_chars": int(user["reaction_max_chars"] or 100),
            "reaction_split_delay": float(user["reaction_split_delay"] or 1.0),
        }

    broadcaster = conn.execute(
        """
        SELECT broadcaster_id, broadcaster_name, enabled,
               basic_reaction_enabled, basic_reaction_type,
               basic_reaction_messages, basic_reaction_prompt,
               max_reactions, reaction_delay_seconds
        FROM special_user_broadcasters
        WHERE user_id = ? AND broadcaster_id = ?
        """,
        (user_id, broadcaster_id),
    ).fetchone()

    if broadcaster and int(broadcaster["enabled"] or 0) and int(broadcaster["basic_reaction_enabled"] or 0):
        return {
            "enabled": True,
            "source": "broadcaster",
            "user_id": user_id,
            "broadcaster_id": broadcaster_id,
            "reaction_type": broadcaster["basic_reaction_type"] or "fixed",
            "messages": broadcaster["basic_reaction_messages"] or "",
            "prompt": broadcaster["basic_reaction_prompt"] or DEFAULT_AI_REACTION_PROMPT,
            "max_reactions": int(broadcaster["max_reactions"] or 1),
            "reaction_delay_seconds": float(broadcaster["reaction_delay_seconds"] or 0.0),
            "reaction_model": user["reaction_model"] or "",
            "reaction_api_key": user["reaction_api_key"] or "",
            "reaction_engine": user["reaction_engine"] or ("codex_exec" if int(user["reaction_use_codex"] or 0) else "openai"),
            "reaction_effort": user["reaction_effort"] or "medium",
            "reaction_session_id": user["reaction_session_id"] or "",
            "reaction_skip_prompt": user["reaction_skip_prompt"] or DEFAULT_AI_REACTION_SKIP_PROMPT,
            "reaction_max_chars": int(user["reaction_max_chars"] or 100),
            "reaction_split_delay": float(user["reaction_split_delay"] or 1.0),
        }

    if int(user["basic_reaction_enabled"] or 0):
        return {
            "enabled": True,
            "source": "special_user",
            "user_id": user_id,
            "broadcaster_id": broadcaster_id,
            "reaction_type": user["basic_reaction_type"] or "fixed",
            "messages": user["basic_reaction_messages"] or "",
            "prompt": user["basic_reaction_prompt"] or DEFAULT_AI_REACTION_PROMPT,
            "max_reactions": int(user["max_reactions"] or 1),
            "reaction_delay_seconds": float(user["reaction_delay_seconds"] or 0.0),
            "reaction_model": user["reaction_model"] or "",
            "reaction_api_key": user["reaction_api_key"] or "",
            "reaction_engine": user["reaction_engine"] or ("codex_exec" if int(user["reaction_use_codex"] or 0) else "openai"),
            "reaction_effort": user["reaction_effort"] or "medium",
            "reaction_session_id": user["reaction_session_id"] or "",
            "reaction_skip_prompt": user["reaction_skip_prompt"] or DEFAULT_AI_REACTION_SKIP_PROMPT,
            "reaction_max_chars": int(user["reaction_max_chars"] or 100),
            "reaction_split_delay": float(user["reaction_split_delay"] or 1.0),
        }

    return {"enabled": False, "source": "none", "reason": "no_enabled_basic_reaction"}


def resolve_special_user_trigger(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    comment_text: str,
) -> dict[str, Any] | None:
    text = comment_text or ""
    if not text:
        return None
    rows = conn.execute(
        """
        SELECT id, keyword, action_type, action_payload
        FROM special_user_triggers
        WHERE user_id = ?
          AND enabled = 1
        ORDER BY id
        """,
        (user_id,),
    ).fetchall()
    for row in rows:
        keywords = [
            keyword.strip()
            for keyword in str(row["keyword"] or "").splitlines()
            if keyword.strip()
        ]
        for keyword in keywords:
            if keyword in text:
                result = dict(row)
                result["keyword"] = keyword
                return result
    return None


def resolve_broadcaster_trigger(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    broadcaster_id: str,
    comment_text: str,
) -> dict[str, Any] | None:
    text = comment_text or ""
    if not text:
        return None
    rows = conn.execute(
        """
        SELECT id, trigger_name, keyword, action_type, action_payload
        FROM broadcaster_triggers
        WHERE user_id = ?
          AND broadcaster_id = ?
          AND enabled = 1
        ORDER BY id
        """,
        (user_id, broadcaster_id),
    ).fetchall()
    for row in rows:
        keywords = [
            keyword.strip()
            for keyword in str(row["keyword"] or "").splitlines()
            if keyword.strip()
        ]
        for keyword in keywords:
            if keyword in text:
                result = dict(row)
                result["keyword"] = keyword
                return result
    return None


def load_config() -> Config:
    config_exists = CONFIG_PATH.exists()
    raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8")) if config_exists else {}
    config = Config(
        recent_url=raw.get("recent_url", "https://live.nicovideo.jp/recent?tab=common"),
        tracker_fetch_method=str(raw.get("tracker_fetch_method", "api")),
        poll_seconds=int(raw.get("poll_seconds", 60)),
        min_elapsed_minutes=float(raw.get("min_elapsed_minutes", 25)),
        target_user_ids=list(raw.get("target_user_ids", [])),
        target_keywords=list(raw.get("target_keywords", [])),
        selenium_headless=bool(raw.get("selenium_headless", True)),
        max_recent_items=int(raw.get("max_recent_items", 70)),
        download_timeout_seconds=int(raw.get("download_timeout_seconds", 120)),
        slnico_live_rec_exe=str(raw.get("slnico_live_rec_exe", DEFAULT_SLNICO_EXE)),
        target_root=str(raw.get("target_root", DEFAULT_TARGET_ROOT)),
        recording_account_id=str(raw.get("recording_account_id", DEFAULT_RECORDING_ACCOUNT_ID)),
        recording_auto_restart=bool(raw.get("recording_auto_restart", True)),
        recording_restart_delay_seconds=float(raw.get("recording_restart_delay_seconds", 0.0)),
        recording_max_restarts=int(raw.get("recording_max_restarts", 20)),
        recording_segment_seconds=int(raw.get("recording_segment_seconds", 1800)),
        concat_output_scale=str(raw.get("concat_output_scale", "854:-2")),
        concat_output_fps=int(raw.get("concat_output_fps", 15)),
        concat_output_crf=int(raw.get("concat_output_crf", 28)),
        concat_video_encoder=str(raw.get("concat_video_encoder", "h264_nvenc")),
        concat_nvenc_preset=str(raw.get("concat_nvenc_preset", "p4")),
        filezilla_config_dir=str(raw.get("filezilla_config_dir", "")),
        ndgr_python_exe=str(raw.get("ndgr_python_exe", ROOT / ".venv" / "Scripts" / "python.exe")),
        character1_name=str(raw.get("character1_name", DEFAULT_CHARACTER1_NAME)),
        character1_image_url=str(raw.get("character1_image_url", DEFAULT_CHARACTER1_IMAGE_URL)),
        character1_fullbody_image_url=str(raw.get("character1_fullbody_image_url", DEFAULT_CHARACTER1_FULLBODY_IMAGE_URL)),
        character2_name=str(raw.get("character2_name", DEFAULT_CHARACTER2_NAME)),
        character2_image_url=str(raw.get("character2_image_url", DEFAULT_CHARACTER2_IMAGE_URL)),
        character2_fullbody_image_url=str(raw.get("character2_fullbody_image_url", DEFAULT_CHARACTER2_FULLBODY_IMAGE_URL)),
        summary_prompt=str(
            raw.get(
                "summary_prompt",
                "次の生放送の文字起こしを、重要な話題・流れ・印象的な発言が分かるように日本語で要約してください。",
            )
        ),
        summary_chunk_size=int(raw.get("summary_chunk_size", 100000)),
        summary_chunk_prompt=str(raw.get("summary_chunk_prompt", "以下は配信の一部です。この部分を要約してください：")),
        summary_final_prompt=str(
            raw.get(
                "summary_final_prompt",
                "以下は配信の各部分の要約です。これらを統合して、配信全体の包括的な要約を作成してください：",
            )
        ),
        image_prompt=str(
            raw.get(
                "image_prompt",
                "次の文章は、ある生放送の要約です。この生放送の抽象的なイメージを生成してください:",
            )
        ),
        intro_conversation_prompt=str(raw.get("intro_conversation_prompt", "配信開始前の会話として、以下の内容について話し合います:")),
        outro_conversation_prompt=str(raw.get("outro_conversation_prompt", "配信終了後の振り返りとして、以下の内容について話し合います:")),
        character1_personality=str(raw.get("character1_personality", "ボケ役で標準語を話す明るい女の子")),
        character2_personality=str(raw.get("character2_personality", "ツッコミ役で関西弁を話すしっかり者の女の子")),
        conversation_turns=int(raw.get("conversation_turns", 5)),
        enable_summary_text=bool(raw.get("enable_summary_text", False)),
        enable_summary_image=bool(raw.get("enable_summary_image", True)),
        enable_ai_conversation=bool(raw.get("enable_ai_conversation", False)),
        enable_ai_music=bool(raw.get("enable_ai_music", False)),
        enable_timeline_thumbnails=bool(raw.get("enable_timeline_thumbnails", True)),
        timeline_thumbnail_width=max(1, int(raw.get("timeline_thumbnail_width", 80))),
        timeline_thumbnail_height=max(1, int(raw.get("timeline_thumbnail_height", 60))),
        enable_audio_timeline=bool(raw.get("enable_audio_timeline", True)),
        enable_timeline_html=bool(raw.get("enable_timeline_html", True)),
        enable_comment_ranking=bool(raw.get("enable_comment_ranking", True)),
        enable_emotion_scores=bool(raw.get("enable_emotion_scores", True)),
        enable_word_extract=bool(raw.get("enable_word_extract", True)),
        suno_api_key=str(raw.get("suno_api_key", "")),
        suno_music_model=str(raw.get("suno_music_model", "V4")),
        suno_music_style=str(raw.get("suno_music_style", "J-Pop, Upbeat")),
        suno_music_instrumental=bool(raw.get("suno_music_instrumental", False)),
        openai_api_key=str(raw.get("openai_api_key", "")),
        google_api_key=str(raw.get("google_api_key", "")),
        imgur_api_key=str(raw.get("imgur_api_key", "")),
        huggingface_token=str(raw.get("huggingface_token", "")),
        image_generation_model=str(raw.get("image_generation_model", "gpt-image-2")),
        image_generation_quality=str(raw.get("image_generation_quality", "medium")),
        codex_exec_enabled=bool(raw.get("codex_exec_enabled", True)),
        codex_exec_provider=str(raw.get("codex_exec_provider", "codex")),
        codex_exec_command=str(raw.get("codex_exec_command", "codex")),
        codex_exec_cwd=str(raw.get("codex_exec_cwd", ROOT)),
        codex_exec_timeout_seconds=int(raw.get("codex_exec_timeout_seconds", 3600)),
        codex_exec_model=str(raw.get("codex_exec_model", "")),
        codex_exec_effort=str(raw.get("codex_exec_effort", "")),
        codex_exec_extra_args=list(raw.get("codex_exec_extra_args", [])),
        enable_archive_auto_upload=bool(raw.get("enable_archive_auto_upload", False)),
        archive_upload_target_id=str(raw.get("archive_upload_target_id", "lolipop-main")),
        archive_upload_username=str(raw.get("archive_upload_username", "")),
        archive_upload_password=str(raw.get("archive_upload_password", "")),
        archive_upload_remote_dir_template=str(
            raw.get("archive_upload_remote_dir_template", "niconico/{account_id}")
        ),
        archive_upload_python_exe=str(
            raw.get(
                "archive_upload_python_exe",
                ROOT / ".venv" / "Scripts" / "python.exe",
            )
        ),
        archive_upload_cli_path=str(
            raw.get(
                "archive_upload_cli_path",
                os.environ.get("NICONICO_UPLOAD_TARGETS_CLI", ""),
            )
        ),
        archive_upload_http_verify=bool(raw.get("archive_upload_http_verify", True)),
        archive_upload_timeout_seconds=max(
            30, int(raw.get("archive_upload_timeout_seconds", 900))
        ),
        archive_upload_auto_start_credentials_api=bool(
            raw.get("archive_upload_auto_start_credentials_api", True)
        ),
        postprocess_console_log_enabled=bool(raw.get("postprocess_console_log_enabled", True)),
    )
    if not config_exists:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(
            json.dumps(asdict(config), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return config


def save_config_values(values: dict[str, Any]) -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        load_config()
    raw: dict[str, Any] = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    raw.update(values)
    CONFIG_PATH.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return raw


def fetch_suno_models_from_official_docs(timeout_seconds: int = 10) -> list[str]:
    response = requests.get(
        SUNO_MODELS_DOC_URL,
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=timeout_seconds,
    )
    response.raise_for_status()
    text = response.text
    found = set(re.findall(r"\bV(?:4|5)(?:_5(?:PLUS|ALL)?|_5)?\b", text))
    ordered = [model for model in DEFAULT_SUNO_MODELS if model in found]
    return ordered or list(DEFAULT_SUNO_MODELS)


def codex_exec_config(config: Config | None = None) -> CodexExecConfig:
    config = config or load_config()
    return CodexExecConfig(
        enabled=bool(config.codex_exec_enabled),
        provider=str(config.codex_exec_provider or "codex"),
        command=str(config.codex_exec_command or "codex"),
        cwd=str(config.codex_exec_cwd or ROOT),
        timeout_seconds=int(config.codex_exec_timeout_seconds or 3600),
        model=str(config.codex_exec_model or ""),
        effort=str(config.codex_exec_effort or ""),
        extra_args=tuple(str(arg) for arg in config.codex_exec_extra_args if str(arg).strip()),
    )


def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.executescript(SCHEMA)
    ensure_schema(conn)
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    finalize_queue_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(finalize_queue)").fetchall()
    }
    finalize_queue_required = {
        "source_kind": "TEXT NOT NULL DEFAULT 'live'",
        "timeline_mode": "TEXT NOT NULL DEFAULT 'live'",
        "input_dir": "TEXT",
        "segment_paths_json": "TEXT",
        "transcribe": "INTEGER NOT NULL DEFAULT 1",
        "whisper_model": "TEXT NOT NULL DEFAULT 'large-v3'",
        "result_json": "TEXT",
    }
    for column, definition in finalize_queue_required.items():
        if column not in finalize_queue_columns:
            conn.execute(f"ALTER TABLE finalize_queue ADD COLUMN {column} {definition}")

    broadcast_archive_meta_columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(broadcast_archive_meta)").fetchall()
    }
    if "comments_fetch_completed" not in broadcast_archive_meta_columns:
        conn.execute(
            "ALTER TABLE broadcast_archive_meta "
            "ADD COLUMN comments_fetch_completed INTEGER NOT NULL DEFAULT 0"
        )
    if "comments_fetch_error" not in broadcast_archive_meta_columns:
        conn.execute(
            "ALTER TABLE broadcast_archive_meta ADD COLUMN comments_fetch_error TEXT"
        )
    if "timeshift_download_completed" not in broadcast_archive_meta_columns:
        conn.execute(
            "ALTER TABLE broadcast_archive_meta "
            "ADD COLUMN timeshift_download_completed INTEGER NOT NULL DEFAULT 0"
        )
    if "timeshift_download_completed_at" not in broadcast_archive_meta_columns:
        conn.execute(
            "ALTER TABLE broadcast_archive_meta "
            "ADD COLUMN timeshift_download_completed_at TEXT"
        )
    timeshift_part_columns = {
        "timeshift_video_download_completed": "INTEGER NOT NULL DEFAULT 0",
        "timeshift_video_download_completed_at": "TEXT",
        "timeshift_comments_download_completed": "INTEGER NOT NULL DEFAULT 0",
        "timeshift_comments_download_completed_at": "TEXT",
    }
    for column, definition in timeshift_part_columns.items():
        if column not in broadcast_archive_meta_columns:
            conn.execute(
                f"ALTER TABLE broadcast_archive_meta ADD COLUMN {column} {definition}"
            )
    archive_upload_columns = {
        "archive_upload_completed": "INTEGER NOT NULL DEFAULT 0",
        "archive_upload_completed_at": "TEXT",
        "archive_upload_target_id": "TEXT",
        "archive_upload_remote_directory": "TEXT",
    }
    for column, definition in archive_upload_columns.items():
        if column not in broadcast_archive_meta_columns:
            conn.execute(
                f"ALTER TABLE broadcast_archive_meta ADD COLUMN {column} {definition}"
            )

    columns = {row["name"] for row in conn.execute("PRAGMA table_info(special_users)").fetchall()}
    required = {
        "enabled": "INTEGER NOT NULL DEFAULT 1",
        "analysis_model": "TEXT",
        "analysis_api_key": "TEXT",
        "analysis_engine": "TEXT NOT NULL DEFAULT 'openai'",
        "analysis_use_codex": "INTEGER NOT NULL DEFAULT 0",
        "analysis_effort": "TEXT NOT NULL DEFAULT 'medium'",
        "analysis_session_id": "TEXT",
        "reaction_model": "TEXT",
        "reaction_api_key": "TEXT",
        "reaction_engine": "TEXT NOT NULL DEFAULT 'openai'",
        "reaction_use_codex": "INTEGER NOT NULL DEFAULT 0",
        "reaction_effort": "TEXT NOT NULL DEFAULT 'medium'",
        "reaction_session_id": "TEXT",
        "reaction_skip_prompt": "TEXT",
        "reaction_max_chars": "INTEGER NOT NULL DEFAULT 100",
        "reaction_split_delay": "REAL NOT NULL DEFAULT 1.0",
        "reaction_delay_seconds": "REAL NOT NULL DEFAULT 0.0",
        "max_reactions": "INTEGER NOT NULL DEFAULT 1",
        "basic_reaction_enabled": "INTEGER NOT NULL DEFAULT 0",
        "basic_reaction_type": "TEXT NOT NULL DEFAULT 'fixed'",
        "basic_reaction_messages": "TEXT",
        "basic_reaction_prompt": "TEXT",
        "default_action_type": "TEXT NOT NULL DEFAULT 'none'",
        "default_action_payload": "TEXT",
        "post_server_url": "TEXT",
        "post_server_api_key": "TEXT",
        "html_upload_enabled": "INTEGER NOT NULL DEFAULT 0",
        "html_base_url": "TEXT",
    }
    for name, definition in required.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE special_users ADD COLUMN {name} {definition}")
    broadcaster_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(special_user_broadcasters)").fetchall()
    }
    broadcaster_required = {
        "basic_reaction_enabled": "INTEGER NOT NULL DEFAULT 0",
        "basic_reaction_type": "TEXT NOT NULL DEFAULT 'fixed'",
        "basic_reaction_messages": "TEXT",
        "basic_reaction_prompt": "TEXT",
        "reaction_use_codex": "INTEGER NOT NULL DEFAULT 0",
        "max_reactions": "INTEGER NOT NULL DEFAULT 1",
        "reaction_delay_seconds": "REAL NOT NULL DEFAULT 0.0",
    }
    for name, definition in broadcaster_required.items():
        if name not in broadcaster_columns:
            conn.execute(f"ALTER TABLE special_user_broadcasters ADD COLUMN {name} {definition}")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS broadcaster_triggers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            broadcaster_id TEXT NOT NULL,
            trigger_name TEXT NOT NULL DEFAULT '',
            keyword TEXT NOT NULL,
            action_type TEXT NOT NULL DEFAULT 'fixed',
            action_payload TEXT,
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES special_users(user_id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS monitored_broadcasters (
            broadcaster_id TEXT PRIMARY KEY,
            broadcaster_name TEXT,
            source_lv TEXT,
            enabled INTEGER NOT NULL DEFAULT 1,
            html_generation_enabled INTEGER NOT NULL DEFAULT 1,
            custom_settings_enabled INTEGER NOT NULL DEFAULT 0,
            thumbnail_10sec_enabled INTEGER NOT NULL DEFAULT 1,
            audio_timeline_enabled INTEGER NOT NULL DEFAULT 1,
            ranking_enabled INTEGER NOT NULL DEFAULT 1,
            ai_conversation_enabled INTEGER NOT NULL DEFAULT 1,
            summary_enabled INTEGER NOT NULL DEFAULT 1,
            music_enabled INTEGER NOT NULL DEFAULT 0,
            abstract_image_enabled INTEGER NOT NULL DEFAULT 1,
            emotion_score_enabled INTEGER NOT NULL DEFAULT 1,
            word_extract_enabled INTEGER NOT NULL DEFAULT 1,
            timeline_enabled INTEGER NOT NULL DEFAULT 1,
            archive_tags TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    trigger_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(broadcaster_triggers)").fetchall()
    }
    trigger_required = {
        "trigger_name": "TEXT NOT NULL DEFAULT ''",
        "action_type": "TEXT NOT NULL DEFAULT 'fixed'",
        "action_payload": "TEXT",
        "enabled": "INTEGER NOT NULL DEFAULT 1",
    }
    for name, definition in trigger_required.items():
        if name not in trigger_columns:
            conn.execute(f"ALTER TABLE broadcaster_triggers ADD COLUMN {name} {definition}")
    monitored_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(monitored_broadcasters)").fetchall()
    }
    monitored_required = {
        "html_generation_enabled": "INTEGER NOT NULL DEFAULT 1",
        "custom_settings_enabled": "INTEGER NOT NULL DEFAULT 0",
        "thumbnail_10sec_enabled": "INTEGER NOT NULL DEFAULT 1",
        "audio_timeline_enabled": "INTEGER NOT NULL DEFAULT 1",
        "ranking_enabled": "INTEGER NOT NULL DEFAULT 1",
        "ai_conversation_enabled": "INTEGER NOT NULL DEFAULT 1",
        "ai_conversation_engine": "TEXT NOT NULL DEFAULT 'codex_exec'",
        "summary_enabled": "INTEGER NOT NULL DEFAULT 1",
        "summary_engine": "TEXT NOT NULL DEFAULT 'codex_exec'",
        "special_user_summary_engine": "TEXT NOT NULL DEFAULT 'codex_exec'",
        "music_enabled": "INTEGER NOT NULL DEFAULT 0",
        "abstract_image_enabled": "INTEGER NOT NULL DEFAULT 1",
        "emotion_score_enabled": "INTEGER NOT NULL DEFAULT 1",
        "word_extract_enabled": "INTEGER NOT NULL DEFAULT 1",
        "timeline_enabled": "INTEGER NOT NULL DEFAULT 1",
        "ai_analysis_model": "TEXT",
        "ai_analysis_api_key": "TEXT",
        "ai_analysis_use_codex": "INTEGER NOT NULL DEFAULT 0",
        "ai_reaction_model": "TEXT",
        "ai_reaction_api_key": "TEXT",
        "ai_reaction_use_codex": "INTEGER NOT NULL DEFAULT 0",
        "summary_use_codex": "INTEGER NOT NULL DEFAULT 0",
        "ai_conversation_use_codex": "INTEGER NOT NULL DEFAULT 0",
        "character1_name": "TEXT",
        "character1_image_url": "TEXT",
        "character1_fullbody_image_url": "TEXT",
        "character1_image_flip": "INTEGER NOT NULL DEFAULT 0",
        "character1_personality": "TEXT",
        "character2_name": "TEXT",
        "character2_image_url": "TEXT",
        "character2_fullbody_image_url": "TEXT",
        "character2_image_flip": "INTEGER NOT NULL DEFAULT 0",
        "character2_personality": "TEXT",
        "post_server_url": "TEXT",
        "post_server_api_key": "TEXT",
        "faster_whisper_model": "TEXT",
        "whisperx_model": "TEXT",
        "whisperx_enabled": "INTEGER NOT NULL DEFAULT 0",
        "transcription_initial_prompt": "TEXT NOT NULL DEFAULT ''",
        "transcription_hotwords_enabled": "INTEGER NOT NULL DEFAULT 1",
        "speaker_diarization_enabled": "INTEGER NOT NULL DEFAULT 0",
        "diarization_min_speakers": "INTEGER NOT NULL DEFAULT 1",
        "diarization_max_speakers": "INTEGER NOT NULL DEFAULT 4",
        "html_upload_enabled": "INTEGER NOT NULL DEFAULT 0",
        "html_base_url": "TEXT",
        "archive_tags": "TEXT NOT NULL DEFAULT ''",
        "summary_prompt": "TEXT",
        "image_prompt": "TEXT",
        "music_prompt": "TEXT",
        "intro_conversation_prompt": "TEXT",
        "outro_conversation_prompt": "TEXT",
        "recording_output_dir": "TEXT",
    }
    for name, definition in monitored_required.items():
        if name not in monitored_columns:
            conn.execute(f"ALTER TABLE monitored_broadcasters ADD COLUMN {name} {definition}")
    broadcast_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(broadcasts)").fetchall()
    }
    broadcast_required = {
        "watch_count": "INTEGER",
        "comment_count": "INTEGER",
    }
    for name, definition in broadcast_required.items():
        if name not in broadcast_columns:
            conn.execute(f"ALTER TABLE broadcasts ADD COLUMN {name} {definition}")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS special_user_broadcast_hits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lv TEXT NOT NULL,
            user_id TEXT NOT NULL,
            broadcaster_id TEXT NOT NULL,
            broadcaster_name TEXT,
            first_comment_no INTEGER,
            first_comment_text TEXT,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            comment_count INTEGER NOT NULL DEFAULT 1,
            html_upload_requested INTEGER NOT NULL DEFAULT 0,
            html_uploaded_at TEXT,
            UNIQUE(lv, user_id, broadcaster_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS broadcaster_monitor_special_user_hits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lv TEXT NOT NULL,
            user_id TEXT NOT NULL,
            broadcaster_id TEXT NOT NULL,
            broadcaster_name TEXT,
            first_comment_no INTEGER,
            first_comment_text TEXT,
            first_comment_seconds REAL,
            detected_at TEXT NOT NULL,
            comment_count INTEGER NOT NULL DEFAULT 1,
            html_upload_requested INTEGER NOT NULL DEFAULT 0,
            html_uploaded_at TEXT,
            UNIQUE(lv, user_id, broadcaster_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS html_upload_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lv TEXT NOT NULL,
            user_id TEXT,
            broadcaster_id TEXT,
            source_path TEXT NOT NULL,
            destination TEXT,
            status TEXT NOT NULL,
            error TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    recording_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(recording_jobs)").fetchall()
    }
    recording_required = {
        "target_dir": "TEXT",
        "restart_count": "INTEGER NOT NULL DEFAULT 0",
        "last_exit_at": "TEXT",
        "last_process_check_at": "TEXT",
        "process_check_count": "INTEGER NOT NULL DEFAULT 0",
    }
    for name, definition in recording_required.items():
        if name not in recording_columns:
            conn.execute(f"ALTER TABLE recording_jobs ADD COLUMN {name} {definition}")
    segment_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(recording_segments)").fetchall()
    }
    segment_required = {
        "started_at": "TEXT",
        "ended_at": "TEXT",
        "duration_seconds": "REAL",
        "timeline_start_seconds": "REAL",
        "audio_wav_path": "TEXT",
        "audio_mp3_path": "TEXT",
        "transcript_status": "TEXT NOT NULL DEFAULT 'pending'",
        "transcript_started_at": "TEXT",
        "transcript_finished_at": "TEXT",
        "transcript_error": "TEXT",
        "transcript_model": "TEXT",
    }
    for name, definition in segment_required.items():
        if name not in segment_columns:
            conn.execute(f"ALTER TABLE recording_segments ADD COLUMN {name} {definition}")
    conn.commit()


def chrome_options(headless: bool) -> Options:
    opts = Options()
    SELENIUM_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    profile_dir = SELENIUM_PROFILE_DIR / f"chrome_{os.getpid()}_{threading.get_ident()}"
    profile_dir.mkdir(parents=True, exist_ok=True)
    opts.add_argument(f"--user-data-dir={profile_dir}")
    if headless:
        opts.add_argument("--headless=new")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1400,2200")
    opts.add_argument("--lang=ja-JP")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--process-per-site")
    opts.add_argument("--renderer-process-limit=4")
    opts.add_argument("--disable-site-isolation-trials")
    opts.add_argument("--disable-features=site-per-process,IsolateOrigins,PaintHolding,Prewarm")
    opts.add_argument("--blink-settings=imagesEnabled=false")
    opts.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
    )
    return opts


def get_tracker_driver(config: Config) -> webdriver.Chrome:
    global _TRACKER_DRIVER, _TRACKER_DRIVER_HEADLESS
    with _TRACKER_DRIVER_LOCK:
        if _TRACKER_DRIVER is not None and _TRACKER_DRIVER_HEADLESS == config.selenium_headless:
            try:
                _TRACKER_DRIVER.execute_script("return 1")
                return _TRACKER_DRIVER
            except Exception:
                close_tracker_driver()
        _TRACKER_DRIVER = webdriver.Chrome(options=chrome_options(config.selenium_headless))
        _TRACKER_DRIVER.set_page_load_timeout(35)
        _TRACKER_DRIVER.set_script_timeout(15)
        _TRACKER_DRIVER.implicitly_wait(0)
        _TRACKER_DRIVER_HEADLESS = config.selenium_headless
        return _TRACKER_DRIVER


def close_tracker_driver() -> None:
    global _TRACKER_DRIVER, _TRACKER_DRIVER_HEADLESS
    driver = _TRACKER_DRIVER
    _TRACKER_DRIVER = None
    _TRACKER_DRIVER_HEADLESS = None
    if driver is not None:
        try:
            driver.quit()
        except Exception:
            pass


def cleanup_selenium_processes() -> list[int]:
    close_tracker_driver()
    killed: list[int] = []
    marker = str(SELENIUM_PROFILE_DIR)
    try:
        script = (
            "$all = Get-CimInstance Win32_Process; "
            f"$marker = '{marker.replace(chr(39), chr(39) + chr(39))}'; "
            "$ids = New-Object 'System.Collections.Generic.HashSet[int]'; "
            "$markerProcs = $all | Where-Object { $_.CommandLine -like \"*$marker*\" }; "
            "foreach ($p in $markerProcs) { "
            "  [void]$ids.Add([int]$p.ProcessId); "
            "  $parent = $all | Where-Object { $_.ProcessId -eq $p.ParentProcessId } | Select-Object -First 1; "
            "  if ($parent -and $parent.Name -eq 'chromedriver.exe') { [void]$ids.Add([int]$parent.ProcessId) } "
            "} "
            "$ids"
        )
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            timeout=10,
        )
        # Windows PowerShell/taskkill may emit CP932 even when this Python
        # process runs in UTF-8 mode.  Keep the PID query as bytes because the
        # only useful output is ASCII digits; decoding localized errors in a
        # subprocess reader thread would otherwise raise UnicodeDecodeError.
        for line in (result.stdout or b"").splitlines():
            line = line.strip()
            if not line.isdigit():
                continue
            pid = int(line)
            if pid and pid not in killed:
                try:
                    subprocess.run(
                        ["taskkill", "/PID", str(pid), "/F"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=5,
                    )
                    killed.append(pid)
                except Exception:
                    pass
    except Exception:
        pass
    try:
        shutil.rmtree(SELENIUM_PROFILE_DIR, ignore_errors=True)
    except Exception:
        pass
    return killed


def scrape_recent_dom(config: Config) -> list[dict[str, Any]]:
    with _TRACKER_DRIVER_LOCK:
        driver = get_tracker_driver(config)
        try:
            driver.get(config.recent_url)
            WebDriverWait(driver, 25).until(lambda d: d.execute_script("return document.readyState") == "complete")
            WebDriverWait(driver, 25).until(lambda d: len(d.find_elements(By.CSS_SELECTOR, 'a[href*="/watch/lv"]')) > 0)
            for index in range(2):
                driver.execute_script("window.scrollTo(0, document.body.scrollHeight)")
                time.sleep(1.0)
                clicked_more = driver.execute_script(
                    r"""
const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();
const buttons = [...document.querySelectorAll('button, a[role="button"], [role="button"]')];
const button = buttons.find((node) => clean(node.innerText || node.textContent).includes('もっと見る'));
if (!button) return false;
button.scrollIntoView({block: 'center', inline: 'nearest'});
button.click();
return true;
"""
                )
                print(f"recent もっと見る {index + 1}/2: {'clicked' if clicked_more else 'not_found'}")
                if not clicked_more:
                    break
                time.sleep(1.5)
            last_count = 0
            stable = 0
            for _ in range(12):
                driver.execute_script("window.scrollTo(0, document.body.scrollHeight)")
                time.sleep(1)
                count = len(driver.find_elements(By.CSS_SELECTOR, 'article[id^="lv"], a[href*="/watch/lv"]'))
                stable = stable + 1 if count == last_count else 0
                last_count = count
                if stable >= 2:
                    break
            items = driver.execute_script(
                r"""
const cards = [...document.querySelectorAll('article[id^="lv"], article.program-card')];
const byLv = new Map();
function clean(s){ return (s || '').replace(/\s+/g, ' ').trim(); }
function userIdFromHref(href){ const m = (href || '').match(/\/user\/(\d+)(?:\/|$)/) || (href || '').match(/[?&]user_id=(\d+)/); return m ? m[1] : null; }
function lvFromHref(href){ const m = (href || '').match(/\/watch\/(lv\d+)/); return m ? m[1] : null; }
function elapsedMinutes(text){ const m = clean(text).match(/(\d+(?:\.\d+)?)\s*分経過/); return m ? Number(m[1]) : null; }
function statNumber(card, title){
  const node = card.querySelector(`[title="${title}"] span[data-value]`) || card.querySelector(`[title="${title}"]`);
  if (!node) return null;
  const raw = node.getAttribute && node.getAttribute('data-value') !== null ? node.getAttribute('data-value') : clean(node.innerText || node.textContent);
  const value = Number(String(raw || '').replace(/,/g, '').trim());
  return Number.isFinite(value) ? value : null;
}
for (const card of cards) {
  const watch = card.querySelector('a[href*="/watch/lv"]');
  const lv = (card.id && card.id.match(/^lv\d+$/) ? card.id : null) || lvFromHref(watch && watch.href);
  if (!lv || byLv.has(lv)) continue;
  const titleNode = card.querySelector('[data-role="program-summary"][title], [class*="program-summary"][title]');
  const titleLink = card.querySelector('[class*="program-title"] a');
  const userLinks = [...card.querySelectorAll('a[href*="/user/"]')];
  const userLink = userLinks.find(a => clean(a.innerText) && userIdFromHref(a.href)) || userLinks.find(a => userIdFromHref(a.href));
  const text = clean(card.innerText);
  byLv.set(lv, {
    lv,
    title: (titleNode && titleNode.getAttribute('title')) || clean(titleLink && titleLink.innerText) || null,
    broadcaster_id: userIdFromHref(userLink && userLink.href),
    broadcaster_name: clean(userLink && userLink.innerText) || null,
    watch_url: watch ? watch.href : null,
    elapsed_minutes: elapsedMinutes(text),
    watch_count: statNumber(card, '来場者数'),
    comment_count: statNumber(card, 'コメント数'),
    status: (card.querySelector('[data-status-type]') || {}).getAttribute ? card.querySelector('[data-status-type]').getAttribute('data-status-type') : null,
    text: text.slice(0, 300)
  });
}
return [...byLv.values()];
"""
            )
            return list(items)[: config.max_recent_items]
        except Exception:
            close_tracker_driver()
            raise
        finally:
            close_tracker_driver()


def fetch_recent_programs_api(config: Config) -> list[dict[str, Any]]:
    endpoint = "https://live.nicovideo.jp/front/api/pages/recent/v1/programs"
    headers = {
        "X-Frontend-Id": "9",
        "X-Frontend-Version": "0",
        "User-Agent": "Mozilla/5.0",
        "Accept-Language": "ja-JP,ja;q=0.9",
        "Referer": config.recent_url or "https://live.nicovideo.jp/recent?tab=common",
    }
    max_items = max(1, int(config.max_recent_items or 70))
    page_size = 70
    items: list[dict[str, Any]] = []
    seen_lvs: set[str] = set()
    while len(items) < max_items:
        response = requests.get(
            endpoint,
            params={"tab": "common", "offset": len(items)},
            headers=headers,
            timeout=15,
        )
        response.raise_for_status()
        payload = response.json()
        if int(payload.get("meta", {}).get("statusCode") or 0) != 200:
            raise RuntimeError(f"recent programs API failed: {payload.get('meta')}")
        programs = payload.get("data") or []
        if not isinstance(programs, list):
            raise RuntimeError("recent programs API data is not a list")
        if not programs:
            break
        before_count = len(items)
        for program in programs:
            item = recent_program_api_item_to_tracker_item(program)
            lv = str(item.get("lv") or "").strip()
            if not lv or lv in seen_lvs:
                continue
            seen_lvs.add(lv)
            items.append(item)
            if len(items) >= max_items:
                break
        if len(programs) < page_size or len(items) == before_count:
            break
    return items[:max_items]


def recent_program_api_item_to_tracker_item(program: dict[str, Any]) -> dict[str, Any]:
    lv = str(program.get("id") or "").strip()
    provider = program.get("programProvider") or {}
    statistics = program.get("statistics") or {}
    begin_at = program.get("beginAt")
    elapsed_minutes: float | None = None
    if isinstance(begin_at, (int, float)) and begin_at > 0:
        elapsed_minutes = max(0.0, (time.time() * 1000.0 - float(begin_at)) / 60000.0)
    status = str(program.get("liveCycle") or "").strip() or None
    title = str(program.get("title") or "").strip() or None
    broadcaster_name = str(provider.get("name") or "").strip() or None
    watch_url = str(program.get("watchPageUrl") or "").strip() or (f"https://live.nicovideo.jp/watch/{lv}" if lv else None)
    text_parts = [
        title or "",
        broadcaster_name or "",
        str(provider.get("id") or ""),
        status or "",
    ]
    return {
        "lv": lv,
        "title": title,
        "broadcaster_id": str(provider.get("id") or "").strip() or None,
        "broadcaster_name": broadcaster_name,
        "watch_url": watch_url,
        "elapsed_minutes": elapsed_minutes,
        "watch_count": _int_or_none(statistics.get("watchCount")),
        "comment_count": _int_or_none(statistics.get("commentCount")),
        "status": status,
        "text": " / ".join(part for part in text_parts if part)[:300],
        "thumbnail_url": str(program.get("listingThumbnail") or "").strip() or None,
        "begin_at": begin_at,
        "end_at": program.get("endAt"),
        "provider_type": str(program.get("providerType") or "").strip() or None,
        "source": "recent_api",
    }


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def fetch_recent_programs(config: Config) -> list[dict[str, Any]]:
    method = str(config.tracker_fetch_method or "api").strip().lower()
    if method == "api":
        return fetch_recent_programs_api(config)
    if method == "selenium":
        return scrape_recent_dom(config)
    raise ValueError(f"unknown tracker_fetch_method: {config.tracker_fetch_method}")


def scrape_following_users(user_id: str, config: Config | None = None, *, max_items: int = 1000) -> list[dict[str, str]]:
    user_id = str(user_id).strip()
    if not user_id.isdigit():
        raise ValueError("numeric user_id is required")
    config = config or load_config()
    with _TRACKER_DRIVER_LOCK:
        driver = get_tracker_driver(config)
        try:
            driver.get(f"https://www.nicovideo.jp/user/{user_id}/follow?ref=pc_userpage_top")
            WebDriverWait(driver, 20).until(lambda d: len(d.find_elements(By.CSS_SELECTOR, 'a[href*="/user/"]')) > 0)
            previous_count = -1
            stable_rounds = 0
            for _ in range(60):
                items = _extract_following_users_from_driver(driver, exclude_user_id=user_id)
                if len(items) >= max_items:
                    break
                if len(items) == previous_count:
                    stable_rounds += 1
                else:
                    stable_rounds = 0
                if stable_rounds >= 4:
                    break
                previous_count = len(items)
                driver.execute_script("window.scrollTo(0, document.body.scrollHeight)")
                time.sleep(0.7)
            return _extract_following_users_from_driver(driver, exclude_user_id=user_id)[:max_items]
        except Exception:
            close_tracker_driver()
            raise
        finally:
            try:
                driver.get("about:blank")
            except Exception:
                pass


def broadcast_history_program_to_on_air_row(
    program: dict[str, Any],
    *,
    fallback_broadcaster_id: str = "",
    fallback_broadcaster_name: str = "",
    provider_type: str = "",
) -> dict[str, Any] | None:
    program_info = program.get("program") if isinstance(program.get("program"), dict) else {}
    schedule = program_info.get("schedule") if isinstance(program_info.get("schedule"), dict) else {}
    if schedule.get("status") != "ON_AIR":
        return None
    lv = str(program.get("id", {}).get("value") or "").strip()
    if not lv:
        return None

    provider = program.get("programProvider") if isinstance(program.get("programProvider"), dict) else {}
    provider_id = provider.get("programProviderId") if isinstance(provider.get("programProviderId"), dict) else {}
    social_group = program.get("socialGroup") if isinstance(program.get("socialGroup"), dict) else {}
    statistics = program.get("statistics") if isinstance(program.get("statistics"), dict) else {}
    viewers = statistics.get("viewers") if isinstance(statistics.get("viewers"), dict) else {}
    comments = statistics.get("comments") if isinstance(statistics.get("comments"), dict) else {}
    begin_time = _api_time_seconds(schedule.get("beginTime")) or _api_time_seconds(schedule.get("openTime"))
    elapsed_minutes: float | None = None
    if begin_time:
        elapsed_minutes = max(0.0, (time.time() - float(begin_time)) / 60.0)

    provider_kind = str(program_info.get("provider") or social_group.get("type") or provider_type or "").strip().upper()
    if provider_kind == "CHANNEL":
        broadcaster_id = (
            str(social_group.get("socialGroupId") or "").strip()
            or str(provider_id.get("value") or "").strip()
            or str(fallback_broadcaster_id or "").strip()
        )
        broadcaster_name = (
            str(social_group.get("name") or "").strip()
            or str(provider.get("name") or "").strip()
            or str(fallback_broadcaster_name or "").strip()
        )
    else:
        broadcaster_id = (
            str(provider_id.get("value") or "").strip()
            or str(social_group.get("socialGroupId") or "").strip()
            or str(fallback_broadcaster_id or "").strip()
        )
        broadcaster_name = (
            str(provider.get("name") or "").strip()
            or str(social_group.get("name") or "").strip()
            or str(fallback_broadcaster_name or "").strip()
        )
    title = str(program_info.get("title") or "").strip()
    text_parts = [title, broadcaster_name, broadcaster_id, "ON_AIR"]
    thumbnail = program.get("thumbnail") if isinstance(program.get("thumbnail"), dict) else {}
    thumbnail_large = thumbnail.get("large") if isinstance(thumbnail.get("large"), dict) else {}
    listing = thumbnail.get("listing") if isinstance(thumbnail.get("listing"), dict) else {}
    return {
        "lv": lv,
        "watch_url": f"https://live.nicovideo.jp/watch/{lv}",
        "status": "ON_AIR",
        "title": title,
        "text": " / ".join(part for part in text_parts if part)[:300],
        "broadcaster_id": broadcaster_id,
        "broadcaster_name": broadcaster_name,
        "elapsed_minutes": elapsed_minutes,
        "watch_count": _int_or_none(viewers.get("value")),
        "comment_count": _int_or_none(comments.get("value")),
        "thumbnail_url": (
            str(listing.get("middle") or "").strip()
            or str(thumbnail_large.get("value") or "").strip()
            or None
        ),
        "provider_type": str(provider_type or program_info.get("provider") or "").strip().lower() or None,
        "source": "user_broadcast_history_api",
    }


def fetch_on_air_user_live_programs(
    user_id: str | int,
    *,
    retries: int = 3,
    retry_delay_seconds: float = 0.8,
) -> list[dict[str, str]]:
    programs = fetch_user_broadcast_history_programs(
        user_id,
        provider_type="user",
        limit=20,
        retries=retries,
        retry_delay_seconds=retry_delay_seconds,
    )
    rows: list[dict[str, Any]] = []
    for program in programs:
        row = broadcast_history_program_to_on_air_row(
            program,
            fallback_broadcaster_id=str(user_id),
            provider_type="user",
        )
        if row:
            rows.append(row)
    return rows


def resolve_channel_provider_id(channel_id_or_url: str, *, timeout: float = 10.0) -> str:
    value = str(channel_id_or_url or "").strip()
    if not value:
        return ""
    direct_match = re.search(r"\bch\d+\b", value, flags=re.IGNORECASE)
    if direct_match:
        return direct_match.group(0)
    if value.startswith("http://") or value.startswith("https://"):
        url = value
    else:
        url = f"https://ch.nicovideo.jp/{value.lstrip('/')}"
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept-Language": "ja-JP,ja;q=0.9",
        "Referer": "https://live.nicovideo.jp/",
    }
    response = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
    response.raise_for_status()
    match = re.search(r"\bch\d+\b", response.text, flags=re.IGNORECASE)
    if match:
        return match.group(0)
    return value.lstrip("/").strip()


def fetch_on_air_channel_live_programs(
    channel_id_or_url: str,
    config: Config | None = None,
    *,
    retries: int = 3,
    retry_delay_seconds: float = 0.8,
) -> list[dict[str, Any]]:
    config = config or load_config()
    provider_id = resolve_channel_provider_id(channel_id_or_url)
    if not provider_id:
        return []
    try:
        programs = fetch_user_broadcast_history_programs(
            provider_id,
            provider_type="channel",
            limit=20,
            retries=retries,
            retry_delay_seconds=retry_delay_seconds,
        )
        rows: list[dict[str, Any]] = []
        for program in programs:
            row = broadcast_history_program_to_on_air_row(
                program,
                fallback_broadcaster_id=provider_id,
                provider_type="channel",
            )
            if row:
                rows.append(row)
        return rows
    except Exception:
        if str(config.tracker_fetch_method or "").strip().lower() == "api":
            raise
        raise


def fetch_on_air_channel_live_programs_from_html(
    channel_id_or_url: str,
    config: Config | None = None,
    *,
    retries: int = 3,
    retry_delay_seconds: float = 0.8,
) -> list[dict[str, str]]:
    value = str(channel_id_or_url or "").strip()
    if not value:
        return []
    if value.startswith("http://") or value.startswith("https://"):
        url = value
    elif re.fullmatch(r"ch\d+", value):
        url = f"https://ch.nicovideo.jp/{value}"
    else:
        url = f"https://ch.nicovideo.jp/{value.lstrip('/')}"
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept-Language": "ja-JP,ja;q=0.9",
        "Referer": "https://live.nicovideo.jp/",
    }
    last_error: Exception | None = None
    attempts = max(1, int(retries or 1))
    for attempt in range(1, attempts + 1):
        try:
            response = requests.get(url, headers=headers, timeout=15, allow_redirects=True)
            response.raise_for_status()
            return parse_channel_on_air_live_programs(response.text, page_url=response.url)
        except Exception as exc:
            last_error = exc
            if attempt >= attempts:
                raise RuntimeError(
                    f"channel live page failed after {attempts} attempts: "
                    f"{type(exc).__name__}: {exc}"
                ) from exc
            time.sleep(max(0.0, float(retry_delay_seconds or 0.0)))
    raise RuntimeError(f"channel live page failed: {last_error}")


def parse_channel_on_air_live_programs(html: str, *, page_url: str = "") -> list[dict[str, str]]:
    html = html or ""
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for match in re.finditer(r"<[^>]*\bdata-live_(?:id|status)\b[^>]*>", html, flags=re.IGNORECASE):
        tag = match.group(0)
        status_match = re.search(r"""\bdata-live_status\s*=\s*["']([^"']+)["']""", tag, flags=re.IGNORECASE)
        live_match = re.search(r"""\bdata-live_id\s*=\s*["'](\d+)["']""", tag, flags=re.IGNORECASE)
        if not status_match or not live_match:
            continue
        if status_match.group(1).strip().lower() != "onair":
            continue
        lv = f"lv{live_match.group(1).strip()}"
        if lv in seen:
            continue
        seen.add(lv)
        text = _nearby_html_text(html, match.start(), match.end())
        rows.append(
            {
                "lv": lv,
                "watch_url": f"https://live.nicovideo.jp/watch/{lv}",
                "status": "ON_AIR",
                "title": "",
                "text": text,
                "source": "channel_html",
                "page_url": page_url,
            }
        )
    return rows


def _nearby_html_text(html: str, start: int, end: int) -> str:
    left = max(0, start - 1200)
    right = min(len(html), end + 1200)
    fragment = html[left:right]
    fragment = re.sub(r"<script\b[^>]*>.*?</script>", " ", fragment, flags=re.IGNORECASE | re.DOTALL)
    fragment = re.sub(r"<style\b[^>]*>.*?</style>", " ", fragment, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", fragment)
    text = unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:300]


def check_live_still_on_air_by_broadcaster_api(lv: str, broadcaster_id: str) -> dict[str, Any]:
    lv = str(lv or "").strip()
    broadcaster_id = str(broadcaster_id or "").strip()
    if re.fullmatch(r"ch\d+", broadcaster_id, flags=re.IGNORECASE):
        provider_type = "channel"
    elif broadcaster_id.isdigit():
        provider_type = "user"
    else:
        provider_type = ""
    if not lv or not provider_type:
        return {"checked": False, "source": "user-broadcast-history", "reason": "missing_supported_broadcaster_id"}
    try:
        programs = fetch_user_broadcast_history_programs(broadcaster_id, provider_type=provider_type, limit=20)
    except Exception as exc:
        return {
            "checked": False,
            "source": "user-broadcast-history",
            "reason": "api_failed",
            "lv": lv,
            "broadcaster_id": broadcaster_id,
            "error": f"{type(exc).__name__}: {exc}",
        }
    live_ids: list[str] = []
    target_program: dict[str, Any] | None = None
    for program in programs:
        program_lv = str(program.get("id", {}).get("value") or "").strip()
        schedule = program.get("program", {}).get("schedule", {})
        if schedule.get("status") == "ON_AIR" and program_lv:
            live_ids.append(program_lv)
        if program_lv == lv:
            target_program = program
    if target_program is not None:
        schedule = target_program.get("program", {}).get("schedule", {})
        status = str(schedule.get("status") or "").strip()
        meta = user_history_program_to_broadcast_archive_meta(
            target_program,
            lv,
            broadcaster_id=broadcaster_id,
        )
        if status == "ON_AIR":
            return {
                "checked": True,
                "source": "user-broadcast-history",
                "on_air": True,
                "reason": "same_lv_on_air",
                "lv": lv,
                "broadcaster_id": broadcaster_id,
                "provider_type": provider_type,
                "status": status,
                "on_air_lvs": live_ids,
                "meta": meta,
            }
        return {
            "checked": True,
            "source": "user-broadcast-history",
            "on_air": False,
            "reason": "target_lv_not_on_air",
            "lv": lv,
            "broadcaster_id": broadcaster_id,
            "provider_type": provider_type,
            "status": status,
            "on_air_lvs": live_ids,
            "meta": meta,
        }
    return {
        "checked": True,
        "source": "user-broadcast-history",
        "on_air": False,
        "reason": "target_lv_not_found",
        "lv": lv,
        "broadcaster_id": broadcaster_id,
        "provider_type": provider_type,
        "on_air_lvs": live_ids,
        "meta": user_history_program_to_broadcast_archive_meta(
            None,
            lv,
            broadcaster_id=broadcaster_id,
        ),
    }


def scrape_on_air_live_programs(broadcaster_id: str, config: Config | None = None) -> list[dict[str, str]]:
    broadcaster_id = str(broadcaster_id).strip()
    if not (broadcaster_id.isdigit() or re.fullmatch(r"ch\d+", broadcaster_id)):
        return []
    config = config or load_config()
    if broadcaster_id.startswith("ch"):
        return fetch_on_air_channel_live_programs(broadcaster_id, config)
    if broadcaster_id.isdigit():
        try:
            return fetch_on_air_user_live_programs(broadcaster_id)
        except Exception:
            if str(config.tracker_fetch_method or "").strip().lower() == "api":
                raise
    if str(config.tracker_fetch_method or "").strip().lower() == "api":
        raise RuntimeError(f"API tracker mode cannot check broadcaster with Selenium fallback: {broadcaster_id}")
    with _TRACKER_DRIVER_LOCK:
        driver = get_tracker_driver(config)
        try:
            if broadcaster_id.startswith("ch"):
                url = f"https://ch.nicovideo.jp/{broadcaster_id}"
            else:
                url = f"https://www.nicovideo.jp/user/{broadcaster_id}/live_programs?ref=watch_user_information"
            driver.get(url)
            WebDriverWait(driver, 20).until(lambda d: d.execute_script("return document.readyState") == "complete")
            deadline = time.monotonic() + 20.0
            items: list[dict[str, Any]] = []
            while time.monotonic() < deadline:
                items = driver.execute_script(
                    r"""
const out = [];
const seen = new Set();
const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();
for (const node of document.querySelectorAll('[data-live_status="onair"][data-live_id]')) {
  const liveId = String(node.getAttribute('data-live_id') || '').trim();
  const lv = liveId.startsWith('lv') ? liveId : `lv${liveId}`;
  if (!/^lv\d+$/.test(lv) || seen.has(lv)) continue;
  const link = node.querySelector('a[href*="live.nicovideo.jp/watch/lv"], a[href*="/watch/lv"]');
  seen.add(lv);
  out.push({lv, watch_url: link ? link.href : `https://live.nicovideo.jp/watch/${lv}`, status: 'ON_AIR', text: clean(node.innerText || node.textContent).slice(0, 300)});
}
for (const a of document.querySelectorAll('a[href*="live.nicovideo.jp/watch/lv"], a[href*="/watch/lv"]')) {
  const status = a.getAttribute('data-status-type') ||
    (a.querySelector('[data-status-type]') && a.querySelector('[data-status-type]').getAttribute('data-status-type')) ||
    '';
  if (status !== 'ON_AIR') continue;
  const href = a.href || '';
  const m = href.match(/\/watch\/(lv\d+)/);
  if (!m || seen.has(m[1])) continue;
  seen.add(m[1]);
  const card = a.closest('article, li, section, div') || a;
  const text = clean(card.innerText || card.textContent);
  out.push({lv: m[1], watch_url: href, status, text: text.slice(0, 300)});
}
return out;
"""
                ) or []
                if items:
                    break
                driver.execute_script(
                    r"""
const broadcasterId = arguments[0];
for (const node of document.querySelectorAll('a, button, [role="tab"]')) {
  const text = (node.innerText || node.textContent || '').replace(/\s+/g, ' ').trim();
  const href = node.href || '';
  if ((text === '生放送' || text === '番組一覧') && href.includes(`/user/${broadcasterId}/live_programs`)) {
    try { node.click(); } catch (e) {}
  }
}
""",
                    broadcaster_id,
                )
                time.sleep(1.0)
            return [
                {
                    "lv": str(item.get("lv") or ""),
                    "watch_url": str(item.get("watch_url") or ""),
                    "status": str(item.get("status") or ""),
                    "text": str(item.get("text") or ""),
                }
                for item in items or []
                if item.get("lv")
            ]
        except Exception:
            close_tracker_driver()
            raise
        finally:
            try:
                driver.get("about:blank")
            except Exception:
                pass


def _extract_following_users_from_driver(driver: webdriver.Chrome, *, exclude_user_id: str) -> list[dict[str, str]]:
    items = driver.execute_script(
        """
const out = [];
const clean = (s) => (s || '').replace(/\\s+/g, ' ').trim();
for (const a of document.querySelectorAll('a[href*="/user/"]')) {
  const href = a.href || '';
  const m = href.match(/\\/user\\/(\\d+)(?:[/?#]|$)/);
  if (!m) continue;
  const id = m[1];
  if (!id || id === arguments[0]) continue;
  let parent = a.closest('li, article, section, div');
  let name = clean(a.getAttribute('aria-label')) ||
    clean(a.getAttribute('title')) ||
    clean(a.querySelector('img[alt]') && a.querySelector('img[alt]').getAttribute('alt'));
  if (!name && parent) {
    const candidates = [
      '[class*="name"]',
      '[class*="Name"]',
      '[class*="nickname"]',
      '[class*="Nickname"]',
      'h1',
      'h2',
      'h3'
    ];
    for (const selector of candidates) {
      const node = parent.querySelector(selector);
      if (node) {
        name = clean(node.innerText || node.textContent);
        if (name) break;
      }
    }
  }
  if (!name) {
    const text = clean(a.innerText);
    name = text.split(/\\n|\\r|  |　/).map(clean).filter(Boolean)[0] || '';
  }
  out.push({user_id: id, name, href});
}
return out;
""",
        exclude_user_id,
    )
    by_id: dict[str, dict[str, str]] = {}
    for item in items or []:
        target_id = str(item.get("user_id") or "").strip()
        if not target_id or target_id in by_id:
            continue
        by_id[target_id] = {
            "user_id": target_id,
            "name": str(item.get("name") or "").strip(),
            "href": str(item.get("href") or "").strip(),
        }
    return list(by_id.values())


def persist_broadcasts(conn: sqlite3.Connection, items: list[dict[str, Any]]) -> None:
    seen = now()
    cleanup_expired_broadcasts(conn, ttl_seconds=TRACKER_BROADCAST_TTL_SECONDS, commit=False)
    for item in items:
        lv = str(item.get("lv") or "").strip()
        if not lv:
            continue
        if is_lv_known_ended(conn, lv):
            conn.execute("DELETE FROM broadcasts WHERE lv = ?", (lv,))
            continue
        status = str(item.get("status") or "").strip()
        if status and status != "ON_AIR":
            conn.execute("DELETE FROM broadcasts WHERE lv = ?", (lv,))
            continue
        conn.execute(
            """
            INSERT INTO broadcasts
                (lv, title, broadcaster_id, broadcaster_name, watch_url, elapsed_minutes, watch_count, comment_count, status, first_seen_at, last_seen_at, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(lv) DO UPDATE SET
                title = excluded.title,
                broadcaster_id = excluded.broadcaster_id,
                broadcaster_name = excluded.broadcaster_name,
                watch_url = excluded.watch_url,
                elapsed_minutes = excluded.elapsed_minutes,
                watch_count = excluded.watch_count,
                comment_count = excluded.comment_count,
                status = excluded.status,
                last_seen_at = excluded.last_seen_at,
                raw_json = excluded.raw_json
            """,
            (
                lv,
                item.get("title"),
                item.get("broadcaster_id"),
                item.get("broadcaster_name"),
                item.get("watch_url"),
                item.get("elapsed_minutes"),
                item.get("watch_count"),
                item.get("comment_count"),
                status,
                seen,
                seen,
                json.dumps(item, ensure_ascii=False),
            ),
        )
    conn.commit()


def cleanup_expired_broadcasts(
    conn: sqlite3.Connection,
    *,
    ttl_seconds: int = TRACKER_BROADCAST_TTL_SECONDS,
    commit: bool = True,
) -> int:
    cutoff = (datetime.now() - timedelta(seconds=ttl_seconds)).isoformat(timespec="seconds")
    expired_count = conn.execute(
        """
        SELECT COUNT(*)
        FROM broadcasts
        WHERE first_seen_at <= ?
        """,
        (cutoff,),
    ).fetchone()[0]
    conn.execute("DELETE FROM broadcasts WHERE first_seen_at <= ?", (cutoff,))
    if commit:
        conn.commit()
    return int(expired_count)


def effective_elapsed_minutes(row: sqlite3.Row | dict[str, Any], *, at: datetime | None = None) -> float | None:
    get = row.get if isinstance(row, dict) else row.__getitem__
    try:
        elapsed = get("elapsed_minutes")
    except Exception:
        return None
    if elapsed is None:
        return None
    try:
        value = float(elapsed)
    except (TypeError, ValueError):
        return None
    try:
        status = str(get("status") or "")
        last_seen_at = str(get("last_seen_at") or "")
    except Exception:
        return value
    if status and status != "ON_AIR":
        return value
    if not last_seen_at:
        return value
    try:
        last_seen = datetime.fromisoformat(last_seen_at)
    except ValueError:
        return value
    now_value = at or datetime.now()
    delta_minutes = max(0.0, (now_value - last_seen).total_seconds() / 60.0)
    return value + delta_minutes


def is_lv_known_ended(conn: sqlite3.Connection, lv: str) -> bool:
    row = conn.execute(
        "SELECT end_time FROM broadcast_archive_meta WHERE lv = ?",
        (str(lv).strip(),),
    ).fetchone()
    if not row or row["end_time"] in {None, ""}:
        return False
    try:
        end_time = int(row["end_time"])
    except (TypeError, ValueError):
        return False
    return end_time > 0 and datetime.now().timestamp() >= end_time


def list_active_broadcasts(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    cleanup_expired_broadcasts(conn, ttl_seconds=TRACKER_BROADCAST_TTL_SECONDS, commit=False)
    conn.execute(
        """
        DELETE FROM broadcasts
        WHERE lv IN (
            SELECT b.lv
            FROM broadcasts b
            JOIN broadcast_archive_meta m ON m.lv = b.lv
            WHERE m.end_time IS NOT NULL
              AND m.end_time != ''
              AND m.end_time <= ?
        )
        """,
        (int(datetime.now().timestamp()),),
    )
    rows = conn.execute(
        """
        SELECT lv, title, broadcaster_id, broadcaster_name, watch_url,
               elapsed_minutes, watch_count, comment_count, status, first_seen_at, last_seen_at, raw_json
        FROM broadcasts
        WHERE COALESCE(status, '') IN ('', 'ON_AIR')
        ORDER BY
            elapsed_minutes IS NULL,
            elapsed_minutes DESC,
            last_seen_at DESC,
            first_seen_at DESC
        """
    ).fetchall()
    conn.commit()
    now_value = datetime.now()
    items = [dict(row) for row in rows]
    for item in items:
        effective = effective_elapsed_minutes(item, at=now_value)
        if effective is not None:
            item["observed_elapsed_minutes"] = item.get("elapsed_minutes")
            item["elapsed_minutes"] = effective
    items.sort(
        key=lambda item: (
            item.get("elapsed_minutes") is None,
            -(float(item.get("elapsed_minutes") or 0.0)),
            str(item.get("last_seen_at") or ""),
            str(item.get("first_seen_at") or ""),
        )
    )
    return items


def eligible_lvs(conn: sqlite3.Connection, config: Config) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT b.*
        FROM broadcasts b
        LEFT JOIN checks c ON c.lv = b.lv
        WHERE c.lv IS NULL
        """
    ).fetchall()
    now_value = datetime.now()
    out: list[dict[str, Any]] = []
    skipped_known_ended = 0
    skipped_status = 0
    skipped_elapsed = 0
    for row in rows:
        item = dict(row)
        lv = str(item.get("lv") or "").strip()
        if lv and is_lv_known_ended(conn, lv):
            skipped_known_ended += 1
            postprocess_log(
                lv,
                "ndgr_candidate",
                "TRACE",
                "NDGR候補除外: 既知の終了済み",
                {"first_seen_at": item.get("first_seen_at"), "status": item.get("status")},
            )
            discard_broadcast_without_match(conn, lv, item.get("first_seen_at"))
            continue
        if str(item.get("status") or "").strip() not in {"", "ON_AIR"}:
            skipped_status += 1
            if lv:
                postprocess_log(
                    lv,
                    "ndgr_candidate",
                    "TRACE",
                    "NDGR候補除外: statusがON_AIRではない",
                    {"status": item.get("status"), "first_seen_at": item.get("first_seen_at")},
                )
            if lv:
                discard_broadcast_without_match(conn, lv, item.get("first_seen_at"))
            continue
        effective = effective_elapsed_minutes(item, at=now_value)
        if effective is None or effective < config.min_elapsed_minutes:
            skipped_elapsed += 1
            continue
        item["observed_elapsed_minutes"] = item.get("elapsed_minutes")
        item["elapsed_minutes"] = effective
        postprocess_log(
            lv,
            "ndgr_candidate",
            "TRACE",
            "NDGR候補採用",
            {
                "first_seen_at": item.get("first_seen_at"),
                "last_seen_at": item.get("last_seen_at"),
                "observed_elapsed_minutes": item.get("observed_elapsed_minutes"),
                "effective_elapsed_minutes": effective,
                "min_elapsed_minutes": config.min_elapsed_minutes,
                "status": item.get("status"),
                "broadcaster_id": item.get("broadcaster_id"),
                "broadcaster_name": item.get("broadcaster_name"),
            },
        )
        out.append(item)
    out.sort(key=lambda item: float(item.get("elapsed_minutes") or 0.0), reverse=True)
    postprocess_log(
        None,
        "ndgr_candidate",
        "TRACE",
        (
            "NDGR候補選定サマリ: "
            f"unchecked={len(rows)} targets={len(out)} "
            f"skip_known_ended={skipped_known_ended} "
            f"skip_status={skipped_status} skip_elapsed={skipped_elapsed}"
        ),
        {
            "unchecked": len(rows),
            "targets": len(out),
            "skip_known_ended": skipped_known_ended,
            "skip_status": skipped_status,
            "skip_elapsed": skipped_elapsed,
        },
    )
    return out


class _WindowsDataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_byte)),
    ]


def decrypt_windows_dpapi_text(encrypted_base64: str) -> str:
    if os.name != "nt":
        raise RuntimeError("SlNicoLiveRec credentials can only be decrypted on Windows")
    try:
        encrypted = base64.b64decode(str(encrypted_base64 or ""), validate=True)
    except (ValueError, TypeError) as exc:
        raise RuntimeError("SlNicoLiveRec credential is not valid base64") from exc
    if not encrypted:
        raise RuntimeError("SlNicoLiveRec credential is empty")
    encrypted_buffer = (ctypes.c_byte * len(encrypted)).from_buffer_copy(encrypted)
    input_blob = _WindowsDataBlob(
        len(encrypted),
        ctypes.cast(encrypted_buffer, ctypes.POINTER(ctypes.c_byte)),
    )
    output_blob = _WindowsDataBlob()
    crypt_unprotect = ctypes.windll.crypt32.CryptUnprotectData
    crypt_unprotect.argtypes = [
        ctypes.POINTER(_WindowsDataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_WindowsDataBlob),
    ]
    crypt_unprotect.restype = wintypes.BOOL
    if not crypt_unprotect(
        ctypes.byref(input_blob),
        None,
        None,
        None,
        None,
        0,
        ctypes.byref(output_blob),
    ):
        raise ctypes.WinError()
    try:
        value = ctypes.string_at(output_blob.pbData, output_blob.cbData).decode("utf-8")
    finally:
        local_free = ctypes.windll.kernel32.LocalFree
        local_free.argtypes = [ctypes.c_void_p]
        local_free.restype = ctypes.c_void_p
        local_free(ctypes.cast(output_blob.pbData, ctypes.c_void_p))
    value = value.rstrip("\x00")
    if not value:
        raise RuntimeError("SlNicoLiveRec credential decrypted to an empty value")
    return value


def load_registered_slnico_user_session(config: Config | None = None) -> str:
    config = config or load_config()
    exe = Path(str(config.slnico_live_rec_exe or ""))
    config_path = exe.parent / "SlNicoLiveRec_config.json"
    if not config_path.is_file():
        raise FileNotFoundError(f"SlNicoLiveRec config not found: {config_path}")
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"SlNicoLiveRec config read failed: {config_path}: {exc}") from exc
    encrypted = str(raw.get("UserSession") or "").strip()
    if not encrypted:
        raise RuntimeError("SlNicoLiveRec UserSession is not configured")
    return decrypt_windows_dpapi_text(encrypted)


def download_timeshift_comments(lv: str, config: Config | None = None) -> Path:
    from ndgr_client import NDGRClient

    lv = str(lv or "").strip().lower()
    if not re.fullmatch(r"lv\d+", lv):
        raise ValueError(f"invalid lv: {lv}")
    config = config or load_config()
    out_dir = TMP_DIR / "downloads" / lv
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    output_path = out_dir / f"{lv}.nicojk"
    user_session = load_registered_slnico_user_session(config)

    async def fetch() -> tuple[str, int]:
        async with NDGRClient(lv, verbose=False, console_output=False) as client:
            # NDGRClient's cookie-login return value can be None after a 303
            # redirect even though the cookie is installed and usable.
            await client.login(cookies={"user_session": user_session})
            if not client.is_logged_in:
                raise RuntimeError("Niconico user_session was not installed")
            comments = await client.downloadBackwardComments()
            return NDGRClient.convertToXMLString(comments), len(comments)

    started = time.monotonic()
    postprocess_log(
        lv,
        "timeshift_comments_api",
        "INFO",
        "認証付きタイムシフトコメントAPI取得開始",
        {"output_path": str(output_path)},
    )
    xml_text, comment_count = asyncio.run(fetch())
    output_path.write_text(xml_text, encoding="utf-8")
    postprocess_log(
        lv,
        "timeshift_comments_api",
        "INFO",
        f"認証付きタイムシフトコメントAPI取得完了: comments={comment_count}",
        {
            "output_path": str(output_path),
            "comment_count": comment_count,
            "elapsed_seconds": time.monotonic() - started,
        },
    )
    return output_path


def download_comments(lv: str, config: Config) -> Path:
    out_dir = TMP_DIR / "downloads" / lv
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    python_exe = str(config.ndgr_python_exe or "").strip() or sys.executable
    cmd = [python_exe, "-m", "ndgr_client", "download", lv, "--output-dir", str(out_dir)]
    started = time.monotonic()
    postprocess_log(
        lv,
        "ndgr_download",
        "TRACE",
        "NDGR一括取得プロセス開始",
        {"cmd": cmd, "python_exe": python_exe, "out_dir": str(out_dir), "timeout": config.download_timeout_seconds},
    )
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    postprocess_log(
        lv,
        "ndgr_download",
        "TRACE",
        "NDGR一括取得プロセス起動",
        {"pid": process.pid, "cmd": cmd},
    )
    try:
        stdout, stderr = process.communicate(timeout=config.download_timeout_seconds)
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, stderr = process.communicate()
        elapsed = time.monotonic() - started
        postprocess_log(
            lv,
            "ndgr_download",
            "ERROR",
            f"NDGR一括取得タイムアウト: pid={process.pid} elapsed={elapsed:.1f}s",
            {"pid": process.pid, "elapsed_seconds": elapsed, "timeout": config.download_timeout_seconds},
        )
        raise
    elapsed = time.monotonic() - started
    result = subprocess.CompletedProcess(cmd, process.returncode, stdout, stderr)
    postprocess_log(
        lv,
        "ndgr_download",
        "TRACE",
        f"NDGR一括取得プロセス終了: pid={process.pid} returncode={result.returncode} elapsed={elapsed:.1f}s",
        {
            "pid": process.pid,
            "returncode": result.returncode,
            "elapsed_seconds": elapsed,
            "stdout_tail": (stdout or "")[-2000:],
            "stderr_tail": (stderr or "")[-2000:],
        },
    )
    if result.returncode != 0:
        output = "\n".join(part for part in (result.stdout, result.stderr) if part)
        detail = summarize_ndgr_failure(output)
        raise RuntimeError(
            f"NDGR download failed: lv={lv} returncode={result.returncode}; {detail}"
        )
    path = out_dir / f"{lv}.nicojk"
    if not path.exists():
        raise RuntimeError(f"download output missing: {path}")
    postprocess_log(
        lv,
        "ndgr_download",
        "TRACE",
        "NDGR一括取得出力確認",
        {"path": str(path), "size": path.stat().st_size if path.exists() else None},
    )
    return path


def summarize_ndgr_failure(output: str, *, max_chars: int = 5000) -> str:
    text = (output or "").replace("\r", "")
    lines = [line.strip(" │") for line in text.split("\n")]
    useful = [
        line
        for line in lines
        if line
        and not set(line) <= {"─", "━", "┌", "┐", "└", "┘", "┬", "┴", "│", " "}
    ]
    cause = ""
    for line in reversed(useful):
        if re.search(r"(Error|Exception|ValueError|RuntimeError|Timeout|failed|empty|denied|forbidden|not found)", line, re.I):
            cause = line
            break
    if not cause and useful:
        cause = useful[-1]
    title = next((line for line in useful if line.startswith("Title:")), "")
    period = next((line for line in useful if line.startswith("Period:")), "")
    tail = "\n".join(useful[-40:])
    parts = []
    if cause:
        parts.append(f"cause={cause}")
    if title:
        parts.append(title)
    if period:
        parts.append(period)
    if tail:
        parts.append("tail:\n" + tail)
    detail = "\n".join(parts) or text or "no stderr/stdout"
    if len(detail) > max_chars:
        detail = detail[:1000] + "\n...\n" + detail[-(max_chars - 1005):]
    return detail


def parse_comments(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return []
    root = ET.fromstring(f"<packet>{text}</packet>")
    rows: list[dict[str, Any]] = []
    for chat in root.findall("chat"):
        raw = dict(chat.attrib)
        raw["text"] = chat.text or ""
        rows.append(raw)
    return rows


def _nonnegative_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def expected_archive_comment_count(payload: Any, *, _depth: int = 0) -> int | None:
    """Return the provider-side comment total embedded in saved program data."""
    if _depth > 5:
        return None
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
    if not isinstance(payload, dict):
        return None

    for key in ("comment_count", "total_comments"):
        if key in payload:
            count = _nonnegative_int(payload.get(key))
            if count is not None:
                return count

    statistics = payload.get("statistics")
    if isinstance(statistics, dict):
        count = _nonnegative_int(statistics.get("commentCount"))
        if count is not None:
            return count
        comments = statistics.get("comments")
        if isinstance(comments, dict):
            count = _nonnegative_int(comments.get("value"))
        else:
            count = _nonnegative_int(comments)
        if count is not None:
            return count

    for key in ("raw_json", "payload_json", "program", "payload", "data"):
        if key not in payload:
            continue
        count = expected_archive_comment_count(payload.get(key), _depth=_depth + 1)
        if count is not None:
            return count
    return None


def _saved_archive_comment_inventory(
    conn: sqlite3.Connection,
    lv: str,
    *,
    broadcast_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    stored_count = int(
        conn.execute(
            "SELECT COUNT(*) FROM archive_comments WHERE lv = ?",
            (lv,),
        ).fetchone()[0]
    )
    expected_count: int | None = None
    expected_count_source = ""
    fetch_completed = False

    candidates: list[tuple[str, Any]] = []
    if broadcast_meta:
        candidates.append(("broadcast_meta", broadcast_meta))
    row = conn.execute(
        "SELECT raw_json, comments_fetch_completed "
        "FROM broadcast_archive_meta WHERE lv = ?",
        (lv,),
    ).fetchone()
    if row:
        fetch_completed = bool(row["comments_fetch_completed"])
        if row["raw_json"]:
            candidates.append(("broadcast_archive_meta", row["raw_json"]))
    for source, payload in candidates:
        expected_count = expected_archive_comment_count(payload)
        if expected_count is not None:
            expected_count_source = source
            break

    if fetch_completed and (
        expected_count is None or stored_count >= expected_count
    ):
        reusable = True
        reason = "comments_fetch_completed"
    elif stored_count > 0 and expected_count is None:
        reusable = True
        reason = "archive_comments_present"
    elif expected_count == 0 and stored_count == 0:
        reusable = True
        reason = "provider_comment_count_zero"
    elif expected_count is not None and stored_count >= expected_count:
        reusable = True
        reason = "archive_comments_complete"
    else:
        reusable = False
        reason = "archive_comments_incomplete"

    return {
        "stored_count": stored_count,
        "expected_count": expected_count,
        "expected_count_source": expected_count_source,
        "fetch_completed": fetch_completed,
        "reusable": reusable,
        "reason": reason,
    }


def mark_archive_comments_fetch_completed(conn: sqlite3.Connection, lv: str) -> None:
    conn.execute(
        """
        INSERT INTO broadcast_archive_meta
            (lv, fetched_at, comments_fetch_completed)
        VALUES (?, ?, 1)
        ON CONFLICT(lv) DO UPDATE SET
            comments_fetch_completed = 1
        """,
        (lv, now_micro()),
    )


def set_archive_comments_fetch_error(conn: sqlite3.Connection, lv: str, error: str) -> None:
    conn.execute(
        """
        INSERT INTO broadcast_archive_meta (lv, fetched_at, comments_fetch_error)
        VALUES (?, ?, ?)
        ON CONFLICT(lv) DO UPDATE SET comments_fetch_error = excluded.comments_fetch_error
        """,
        (lv, now_micro(), str(error or "").strip() or None),
    )


def mark_timeshift_download_completed(lv: str) -> None:
    """動画とコメントの双方が取得済みになった時点を記録する。"""
    completed_at = now_micro()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO broadcast_archive_meta
                (lv, fetched_at, timeshift_download_completed, timeshift_download_completed_at)
            VALUES (?, ?, 1, ?)
            ON CONFLICT(lv) DO UPDATE SET
                timeshift_download_completed = 1,
                timeshift_download_completed_at = excluded.timeshift_download_completed_at
            """,
            (lv, completed_at, completed_at),
        )
        conn.commit()


def mark_timeshift_video_download_completed(lv: str) -> None:
    completed_at = now_micro()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO broadcast_archive_meta
                (lv, fetched_at, timeshift_video_download_completed,
                 timeshift_video_download_completed_at)
            VALUES (?, ?, 1, ?)
            ON CONFLICT(lv) DO UPDATE SET
                timeshift_video_download_completed = 1,
                timeshift_video_download_completed_at = excluded.timeshift_video_download_completed_at
            """,
            (lv, completed_at, completed_at),
        )
        conn.commit()


def mark_timeshift_comments_download_completed(lv: str) -> None:
    completed_at = now_micro()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO broadcast_archive_meta
                (lv, fetched_at, timeshift_comments_download_completed,
                 timeshift_comments_download_completed_at)
            VALUES (?, ?, 1, ?)
            ON CONFLICT(lv) DO UPDATE SET
                timeshift_comments_download_completed = 1,
                timeshift_comments_download_completed_at = excluded.timeshift_comments_download_completed_at
            """,
            (lv, completed_at, completed_at),
        )
        conn.commit()


def download_and_store_archive_comments(
    lv: str,
    config: Config | None = None,
    *,
    broadcast_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    lv = str(lv or "").strip().lower()
    if not re.fullmatch(r"lv\d+", lv):
        raise ValueError(f"invalid lv: {lv}")

    with connect() as conn:
        if broadcast_meta:
            meta = dict(broadcast_meta)
            meta["lv"] = lv
            save_broadcast_archive_meta(conn, meta)
            conn.commit()
        inventory = _saved_archive_comment_inventory(
            conn,
            lv,
            broadcast_meta=broadcast_meta,
        )
        if inventory["reusable"] and not inventory["fetch_completed"]:
            mark_archive_comments_fetch_completed(conn, lv)
            conn.commit()
            inventory["fetch_completed"] = True

    if inventory["reusable"]:
        with connect() as conn:
            set_archive_comments_fetch_error(conn, lv, "")
            conn.commit()
        result = {
            "lv": lv,
            "fetched_count": 0,
            "inserted_count": 0,
            "duplicate_count": 0,
            "stored_count": inventory["stored_count"],
            "expected_count": inventory["expected_count"],
            "expected_count_source": inventory["expected_count_source"],
            "comments_fetch_completed": inventory["fetch_completed"],
            "reused": True,
            "reason": inventory["reason"],
            "source": "database",
            "db_path": str(DB_PATH),
            "table": "archive_comments",
            "temp_path": "",
            "temp_deleted": True,
        }
        postprocess_log(
            lv,
            "timeshift_comments",
            "INFO",
            (
                "保存済みコメントを再利用: "
                f"stored={result['stored_count']} expected={result['expected_count']} "
                f"reason={result['reason']}"
            ),
            result,
        )
        return result

    config = config or load_config()
    try:
        temp_path = download_timeshift_comments(lv, config)
        comments = parse_comments(temp_path)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        with connect() as conn:
            set_archive_comments_fetch_error(conn, lv, error)
            conn.commit()
        result = {
            "lv": lv,
            "fetched_count": 0,
            "inserted_count": 0,
            "duplicate_count": 0,
            "stored_count": inventory["stored_count"],
            "expected_count": inventory["expected_count"],
            "expected_count_source": inventory["expected_count_source"],
            "comments_fetch_completed": inventory["fetch_completed"],
            "reused": True,
            "reason": "timeshift_api_failed_fallback",
            "source": "database_fallback",
            "acquisition_error": error,
            "db_path": str(DB_PATH),
            "table": "archive_comments",
            "temp_path": "",
            "temp_deleted": True,
        }
        postprocess_log(
            lv,
            "timeshift_comments",
            "WARN",
            (
                "タイムシフトコメント取得失敗のため保存済みコメントで続行: "
                f"stored={result['stored_count']} expected={result['expected_count']} error={error}"
            ),
            result,
        )
        return result
    inserted_count = 0
    with connect() as conn:
        for comment in comments:
            row = save_archive_comment_from_ndgr(conn, lv, comment)
            if row.get("inserted"):
                inserted_count += 1
        stored_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM archive_comments WHERE lv = ?",
                (lv,),
            ).fetchone()[0]
        )
        mark_archive_comments_fetch_completed(conn, lv)
        set_archive_comments_fetch_error(conn, lv, "")
        conn.commit()
    shutil.rmtree(temp_path.parent, ignore_errors=True)
    result = {
        "lv": lv,
        "fetched_count": len(comments),
        "inserted_count": inserted_count,
        "duplicate_count": len(comments) - inserted_count,
        "stored_count": stored_count,
        "expected_count": inventory["expected_count"],
        "expected_count_source": inventory["expected_count_source"],
        "comments_fetch_completed": True,
        "reused": False,
        "reason": "timeshift_api_fetched",
        "source": "timeshift_api",
        "db_path": str(DB_PATH),
        "table": "archive_comments",
        "temp_path": str(temp_path),
        "temp_deleted": not temp_path.parent.exists(),
    }
    postprocess_log(
        lv,
        "timeshift_comments",
        "INFO",
        (
            f"タイムシフトコメント保存完了: fetched={result['fetched_count']} "
            f"inserted={result['inserted_count']} duplicate={result['duplicate_count']}"
        ),
        result,
    )
    return result


def find_matches(comments: list[dict[str, Any]], config: Config) -> list[dict[str, Any]]:
    target_ids = set(config.target_user_ids)
    keywords = [kw for kw in config.target_keywords if kw]
    matches: list[dict[str, Any]] = []
    for comment in comments:
        user_id = comment.get("user_id") or ""
        text = comment.get("text") or ""
        if user_id in target_ids:
            matches.append({**comment, "match_type": "user_id", "matched_value": user_id})
        for keyword in keywords:
            if keyword in text:
                matches.append({**comment, "match_type": "keyword", "matched_value": keyword})
    return matches


def find_special_user_matches(comments: list[dict[str, Any]], special_user_ids: list[str]) -> list[dict[str, Any]]:
    target_ids = {str(user_id).strip() for user_id in special_user_ids if str(user_id).strip()}
    matches: list[dict[str, Any]] = []
    for comment in comments:
        user_id = str(comment.get("user_id") or "").strip()
        if user_id in target_ids:
            matches.append({**comment, "match_type": "special_user_id", "matched_value": user_id})
    return matches


def record_check(conn: sqlite3.Connection, lv: str, result: str, comments: list[dict[str, Any]], matches: list[dict[str, Any]], saved_dir: Path | None, deleted_temp: bool, error: str | None = None) -> None:
    conn.execute(
        """
        INSERT INTO checks
            (lv, checked_at, result, fetched_count, matched_count, saved_dir, deleted_temp, error, raw_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(lv) DO UPDATE SET
            checked_at = excluded.checked_at,
            result = excluded.result,
            fetched_count = excluded.fetched_count,
            matched_count = excluded.matched_count,
            saved_dir = excluded.saved_dir,
            deleted_temp = excluded.deleted_temp,
            error = excluded.error,
            raw_json = excluded.raw_json
        """,
        (
            lv,
            now(),
            result,
            len(comments),
            len(matches),
            str(saved_dir) if saved_dir else None,
            int(deleted_temp),
            error,
            json.dumps({"matches": matches[-20:]}, ensure_ascii=False),
        ),
    )
    for match in matches:
        conn.execute(
            """
            INSERT INTO matches (lv, comment_no, user_id, text, match_type, matched_value, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                lv,
                int(match["no"]) if str(match.get("no", "")).isdigit() else None,
                match.get("user_id"),
                match.get("text"),
                match["match_type"],
                match["matched_value"],
                now(),
            ),
        )
    conn.commit()


def check_lv_for_special_users(conn: sqlite3.Connection, row: sqlite3.Row, config: Config) -> dict[str, Any]:
    lv = str(row["lv"])
    broadcaster_id = str(row["broadcaster_id"] or "").strip()
    broadcaster_name = str(row["broadcaster_name"] or "").strip()
    started = time.monotonic()
    postprocess_log(
        lv,
        "ndgr_check",
        "TRACE",
        "NDGRチェック開始",
        {
            "broadcaster_id": broadcaster_id,
            "broadcaster_name": broadcaster_name,
            "elapsed_minutes": row["elapsed_minutes"] if "elapsed_minutes" in row.keys() else None,
            "first_seen_at": row["first_seen_at"] if "first_seen_at" in row.keys() else None,
            "status": row["status"] if "status" in row.keys() else None,
        },
    )
    if is_lv_known_ended(conn, lv) or str(row["status"] or "").strip() not in {"", "ON_AIR"}:
        discard_broadcast_without_match(conn, lv, row["first_seen_at"])
        postprocess_log(
            lv,
            "ndgr_check",
            "TRACE",
            f"NDGRチェック終了: ended_deleted elapsed={time.monotonic() - started:.1f}s",
            {"result": "ended_deleted", "elapsed_seconds": time.monotonic() - started},
        )
        return {"lv": lv, "result": "ended_deleted", "matches": 0, "linked": 0}
    if not broadcaster_id:
        record_check(conn, lv, "no_broadcaster_id", [], [], None, deleted_temp=False)
        postprocess_log(
            lv,
            "ndgr_check",
            "TRACE",
            f"NDGRチェック終了: no_broadcaster_id elapsed={time.monotonic() - started:.1f}s",
            {"result": "no_broadcaster_id", "elapsed_seconds": time.monotonic() - started},
        )
        return {"lv": lv, "result": "no_broadcaster_id", "matches": 0, "linked": 0}

    liveness = check_lv_liveness_before_ndgr(conn, lv, broadcaster_id, broadcaster_name)
    if liveness.get("checked") and not liveness.get("on_air"):
        discard_broadcast_without_match(conn, lv, row["first_seen_at"])
        postprocess_log(
            lv,
            "ndgr_check",
            "TRACE",
            f"NDGRチェック終了: ended_deleted_api_precheck elapsed={time.monotonic() - started:.1f}s",
            {"result": "ended_deleted_api_precheck", "liveness": liveness, "elapsed_seconds": time.monotonic() - started},
        )
        return {
            "lv": lv,
            "result": "ended_deleted_api_precheck",
            "matches": 0,
            "linked": 0,
            "reason": liveness.get("reason"),
        }
    if not liveness.get("checked"):
        postprocess_log(
            lv,
            "ndgr_precheck",
            "WARN",
            f"NDGR前API生存確認不可。NDGR一括取得へ進む: broadcaster_id={broadcaster_id} reason={liveness.get('reason')}",
            {"liveness": liveness},
        )

    special_user_ids = list_special_user_ids(conn)
    if not special_user_ids:
        record_check(conn, lv, "no_special_users", [], [], None, deleted_temp=False)
        postprocess_log(
            lv,
            "ndgr_check",
            "TRACE",
            f"NDGRチェック終了: no_special_users elapsed={time.monotonic() - started:.1f}s",
            {"result": "no_special_users", "elapsed_seconds": time.monotonic() - started},
        )
        return {"lv": lv, "result": "no_special_users", "matches": 0, "linked": 0}

    try:
        temp_path = download_comments(lv, config)
        comments = parse_comments(temp_path)
        matches = find_special_user_matches(comments, special_user_ids)
        if matches:
            linked_user_ids = sorted({str(match["matched_value"]) for match in matches})
            for user_id in linked_user_ids:
                auto_link_special_user_broadcaster(
                    conn,
                    user_id=user_id,
                    broadcaster_id=broadcaster_id,
                    broadcaster_name=broadcaster_name,
                )
            save_dir = HIT_DIR / lv
            save_dir.mkdir(parents=True, exist_ok=True)
            saved_log = save_dir / temp_path.name
            shutil.copy2(temp_path, saved_log)
            (save_dir / "matches.json").write_text(json.dumps(matches, ensure_ascii=False, indent=2), encoding="utf-8")
            record_check(conn, lv, "special_user_linked", comments, matches, save_dir, deleted_temp=False)
            postprocess_log(
                lv,
                "ndgr_check",
                "TRACE",
                f"NDGRチェック終了: special_user_linked elapsed={time.monotonic() - started:.1f}s comments={len(comments)} matches={len(matches)}",
                {
                    "result": "special_user_linked",
                    "comments": len(comments),
                    "matches": len(matches),
                    "linked_user_ids": linked_user_ids,
                    "elapsed_seconds": time.monotonic() - started,
                },
            )
            conn.commit()
            return {"lv": lv, "result": "special_user_linked", "matches": len(matches), "linked": len(linked_user_ids)}

        shutil.rmtree(temp_path.parent, ignore_errors=True)
        record_check(conn, lv, "no_special_user_deleted", comments, [], None, deleted_temp=True)
        # A negative special-user scan only marks this scan as completed.
        # The broadcast row is shared with recording/end detection, so deleting
        # it here can orphan a valid recording before finalization is queued.
        postprocess_log(
            lv,
            "ndgr_check",
            "TRACE",
            f"NDGRチェック終了: no_special_user_checked elapsed={time.monotonic() - started:.1f}s comments={len(comments)}",
            {"result": "no_special_user_checked", "comments": len(comments), "elapsed_seconds": time.monotonic() - started},
        )
        return {"lv": lv, "result": "no_special_user_checked", "matches": 0, "linked": 0}
    except Exception as exc:
        error = str(exc)
        if is_ndgr_ended_failure(error):
            discard_broadcast_without_match(conn, lv, row["first_seen_at"])
            postprocess_log(
                lv,
                "ndgr_check",
                "TRACE",
                f"NDGRチェック終了: ended_deleted elapsed={time.monotonic() - started:.1f}s error={type(exc).__name__}",
                {"result": "ended_deleted", "error": error, "elapsed_seconds": time.monotonic() - started},
            )
            return {"lv": lv, "result": "ended_deleted", "matches": 0, "linked": 0}
        record_check(conn, lv, "error", [], [], None, deleted_temp=False, error=str(exc))
        postprocess_log(
            lv,
            "ndgr_check",
            "TRACE",
            f"NDGRチェック終了: error elapsed={time.monotonic() - started:.1f}s error={type(exc).__name__}",
            {"result": "error", "error": error, "elapsed_seconds": time.monotonic() - started},
        )
        return {"lv": lv, "result": "error", "matches": 0, "linked": 0, "error": str(exc)}


def is_ndgr_ended_failure(error: str) -> bool:
    text = str(error or "")
    ended_markers = (
        "[ENDED]",
        "has already ended",
        "webSocketUrl is empty",
        "HTTP Error 404",
    )
    return any(marker in text for marker in ended_markers)


def check_lv_liveness_before_ndgr(
    conn: sqlite3.Connection,
    lv: str,
    broadcaster_id: str,
    broadcaster_name: str = "",
) -> dict[str, Any]:
    lv = str(lv or "").strip()
    broadcaster_id = str(broadcaster_id or "").strip()
    broadcaster_name = str(broadcaster_name or "").strip()
    if not lv:
        return {"checked": False, "reason": "missing_lv"}
    if is_lv_known_ended(conn, lv):
        postprocess_log(lv, "ndgr_precheck", "DEBUG", "NDGR前確認: metaで終了済み", {"broadcaster_id": broadcaster_id})
        return {"checked": True, "on_air": False, "reason": "known_ended_meta"}
    if not is_supported_broadcast_history_provider_id(broadcaster_id):
        return {
            "checked": False,
            "reason": "unsupported_broadcaster_id",
            "broadcaster_id": broadcaster_id,
        }
    postprocess_log(
        lv,
        "ndgr_precheck",
        "DEBUG",
        f"NDGR前API生存確認開始: broadcaster_id={broadcaster_id}",
        {"broadcaster_id": broadcaster_id, "broadcaster_name": broadcaster_name},
    )
    result = check_live_still_on_air_by_broadcaster_api(lv, broadcaster_id)
    postprocess_log(
        lv,
        "ndgr_precheck",
        "DEBUG" if result.get("checked") and result.get("on_air") else "WARN",
        (
            "NDGR前API生存確認結果: "
            f"checked={result.get('checked')} on_air={result.get('on_air')} "
            f"reason={result.get('reason')} status={result.get('status')} "
            f"on_air_lvs={result.get('on_air_lvs')}"
        ),
        {"liveness": result},
    )
    meta = result.get("meta")
    if isinstance(meta, dict) and meta:
        if broadcaster_id and not meta.get("broadcaster_id"):
            meta["broadcaster_id"] = broadcaster_id
        if broadcaster_name and not meta.get("broadcaster_name"):
            meta["broadcaster_name"] = broadcaster_name
        try:
            save_broadcast_archive_meta(conn, meta)
            conn.commit()
        except Exception as exc:
            postprocess_log(
                lv,
                "ndgr_precheck",
                "WARN",
                f"NDGR前APIメタ保存失敗: {type(exc).__name__}: {exc}",
                {"meta": meta},
            )
    return result


def discard_broadcast_without_match(conn: sqlite3.Connection, lv: str, first_seen_at: str | None = None) -> None:
    conn.execute("DELETE FROM matches WHERE lv = ?", (lv,))
    conn.execute("DELETE FROM broadcasts WHERE lv = ?", (lv,))
    conn.commit()


def check_eligible_broadcasts_for_special_users(conn: sqlite3.Connection, config: Config) -> list[dict[str, Any]]:
    targets = eligible_lvs(conn, config)
    results: list[dict[str, Any]] = []
    for row in targets:
        results.append(check_lv_for_special_users(conn, row, config))
    return results


def check_lv(conn: sqlite3.Connection, row: sqlite3.Row, config: Config) -> None:
    lv = row["lv"]
    print(f"[check] {lv} {row['elapsed_minutes']}min {row['broadcaster_id']} / {row['broadcaster_name']} | {row['title']}")
    try:
        temp_path = download_comments(lv, config)
        comments = parse_comments(temp_path)
        matches = find_matches(comments, config)
        if matches:
            save_dir = HIT_DIR / lv
            save_dir.mkdir(parents=True, exist_ok=True)
            saved_log = save_dir / temp_path.name
            shutil.copy2(temp_path, saved_log)
            (save_dir / "matches.json").write_text(json.dumps(matches, ensure_ascii=False, indent=2), encoding="utf-8")
            record_check(conn, lv, "matched_saved", comments, matches, save_dir, deleted_temp=False)
            print(f"[hit] {lv} comments={len(comments)} matches={len(matches)} saved={save_dir}")
        else:
            shutil.rmtree(temp_path.parent, ignore_errors=True)
            record_check(conn, lv, "no_match_deleted", comments, [], None, deleted_temp=True)
            print(f"[no-match] {lv} comments={len(comments)} temp_deleted=1")
    except Exception as exc:
        record_check(conn, lv, "error", [], [], None, deleted_temp=False, error=str(exc))
        print(f"[error] {lv} {exc}")


def run_iteration(config: Config) -> None:
    print("\n" + "=" * 80)
    print(f"[{now()}] fetch recent programs ({config.tracker_fetch_method})")
    items = fetch_recent_programs(config)
    first_source = str(items[0].get("source") or "selenium_dom") if items else "none"
    print(f"[recent-source] method={config.tracker_fetch_method} items={len(items)} source={first_source}")
    with connect() as conn:
        persist_broadcasts(conn, items)
        targets = eligible_lvs(conn, config)
        print(f"[recent] items={len(items)} eligible_unchecked={len(targets)} min_elapsed={config.min_elapsed_minutes}")
        for item in items[:8]:
            print(f"  {item.get('lv')} {item.get('elapsed_minutes')}min {item.get('broadcaster_id')} / {item.get('broadcaster_name')} | {item.get('title')}")
        for row in targets:
            check_lv(conn, row, config)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    config = load_config()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    HIT_DIR.mkdir(parents=True, exist_ok=True)
    if not config.target_user_ids and not config.target_keywords:
        print("[warn] config has no target_user_ids or target_keywords. Downloads will be deleted unless you add targets.")
    if args.once:
        run_iteration(config)
        return
    print("[niconico-watch-app] tracker running")
    while True:
        try:
            run_iteration(config)
        except KeyboardInterrupt:
            print("\n[niconico-watch-app] tracker stopped")
            return
        except Exception as exc:
            print(f"[loop-error] {exc}")
        time.sleep(max(10, config.poll_seconds))


if __name__ == "__main__":
    main()
