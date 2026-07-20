from __future__ import annotations

import json
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

import tracker


@pytest.fixture()
def isolated_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    path = tmp_path / "tracker.db"
    monkeypatch.setattr(tracker, "DB_PATH", path)
    monkeypatch.setattr(tracker, "DATA_DIR", tmp_path)
    return path


def history_program(
    lv: str,
    *,
    broadcaster_id: str = "39532023",
    status: str | None = "OPENED",
    end_time: int = 2_000,
) -> dict:
    row = {
        "id": {"value": lv},
        "program": {
            "title": lv,
            "provider": "COMMUNITY",
            "schedule": {
                "status": "ENDED",
                "openTime": {"seconds": 100},
                "beginTime": {"seconds": 100},
                "endTime": {"seconds": 200},
            },
        },
        "programProvider": {
            "type": "COMMUNITY",
            "name": "yosino",
            "programProviderId": {"value": broadcaster_id},
        },
    }
    if status is not None:
        row["timeshiftSetting"] = {
            "status": status,
            "endTime": {"seconds": end_time},
        }
    return row


def test_timeshift_availability_requires_open_status_and_future_deadline() -> None:
    assert tracker.is_timeshift_program_available(
        history_program("lv1", end_time=200),
        at_timestamp=100,
    )
    assert not tracker.is_timeshift_program_available(
        history_program("lv1", end_time=100),
        at_timestamp=100,
    )
    assert not tracker.is_timeshift_program_available(
        history_program("lv1", status="CLOSED"),
        at_timestamp=100,
    )
    assert not tracker.is_timeshift_program_available(
        history_program("lv1", status=None),
        at_timestamp=100,
    )
    on_air = history_program("lv1", end_time=200)
    on_air["program"]["schedule"]["status"] = "ON_AIR"
    assert not tracker.is_timeshift_program_available(on_air, at_timestamp=100)


def test_timeshift_items_are_sorted_by_oldest_broadcast_regardless_of_input_order() -> None:
    rows = tracker.sort_timeshift_download_items_oldest_first(
        [
            {"lv": "lv-new", "timeshift_end_time": 500, "end_time": 400},
            {"lv": "lv-old-b", "timeshift_end_time": 300, "end_time": 250},
            {"lv": "lv-old-a", "timeshift_end_time": 300, "end_time": 200},
            {"lv": "lv-unknown", "timeshift_end_time": None, "end_time": 100},
        ]
    )
    assert [row["lv"] for row in rows] == [
        "lv-unknown",
        "lv-old-a",
        "lv-old-b",
        "lv-new",
    ]


def test_local_lvs_are_sorted_by_saved_broadcast_start_time(
    isolated_db: Path,
) -> None:
    with tracker.connect() as conn:
        for lv, start_time in (
            ("lv350970204", 400),
            ("lv350969805", 300),
            ("lv350969678", 200),
            ("lv350969551", 100),
        ):
            tracker.save_broadcast_archive_meta(
                conn,
                {"lv": lv, "start_time": start_time},
            )
        conn.commit()

    assert tracker.sort_broadcast_lvs_oldest_first(
        ["lv350970204", "lv350969805", "lv350969678", "lv350969551"]
    ) == [
        "lv350969551",
        "lv350969678",
        "lv350969805",
        "lv350970204",
    ]


def test_timeshift_history_pages_until_settings_disappear(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []

    def fake_fetch(_provider_id, *, provider_type, offset, limit, **_kwargs):
        assert provider_type == "user"
        assert limit == 2
        calls.append(offset)
        if offset == 0:
            return [
                history_program("lv100", end_time=500),
                history_program("lv099", status="CLOSED"),
            ]
        if offset == 2:
            return [
                history_program("lv098", status=None),
                history_program("lv097", status=None),
            ]
        raise AssertionError(f"unexpected offset: {offset}")

    monkeypatch.setattr(tracker, "fetch_user_broadcast_history_programs", fake_fetch)
    rows = tracker.fetch_timeshift_available_programs_for_broadcaster(
        "39532023",
        page_size=2,
        at_timestamp=100,
    )
    assert [row["id"]["value"] for row in rows] == ["lv100"]
    assert calls == [0, 2]


def test_resolve_timeshift_urls_deduplicates_and_keeps_broadcaster(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    direct = history_program("lv100", end_time=5_000)
    listed = history_program("lv101", end_time=5_000)
    monkeypatch.setattr(
        tracker,
        "fetch_broadcast_page_meta",
        lambda lv, save_html=False: {
            "lv": lv,
            "watch_url": f"https://live.nicovideo.jp/watch/{lv}",
            "title": lv,
            "broadcaster_id": "39532023",
            "broadcaster_name": "yosino",
        },
    )
    monkeypatch.setattr(
        tracker,
        "fetch_user_broadcast_history_programs",
        lambda *_args, **_kwargs: [direct],
    )
    monkeypatch.setattr(
        tracker,
        "fetch_timeshift_available_programs_for_broadcaster",
        lambda *_args, **_kwargs: [direct, listed],
    )
    monkeypatch.setattr(tracker, "is_timeshift_program_available", lambda *_args, **_kwargs: True)

    rows = tracker.resolve_timeshift_input_urls(
        [
            "https://live.nicovideo.jp/watch/lv100",
            "https://www.nicovideo.jp/user/39532023/live_programs",
        ]
    )
    assert [row["lv"] for row in rows] == ["lv100", "lv101"]
    assert all(row["broadcaster_id"] == "39532023" for row in rows)
    assert all(row["broadcaster_name"] == "yosino" for row in rows)


def test_supplier_fields_are_extracted_from_watch_page_json() -> None:
    source = (
        '<script>{"supplier":{"supplierType":"user","name":"yosino",'
        '"programProviderId":"39532023"},"beginTime":100}</script>'
    )
    assert tracker.extract_supplier_name(source) == "yosino"
    assert tracker.extract_string_field(source, ["programProviderId"]) == "39532023"


def test_archive_comment_retry_does_not_double_ranking(isolated_db: Path) -> None:
    comment = {
        "no": "1",
        "user_id": "user-1",
        "text": "同じコメント",
        "date": "110",
        "vpos": "1000",
    }
    with tracker.connect() as conn:
        tracker.save_broadcast_archive_meta(
            conn,
            {
                "lv": "lv100",
                "watch_url": "https://live.nicovideo.jp/watch/lv100",
                "broadcaster_id": "39532023",
                "broadcaster_name": "yosino",
                "start_time": 100,
            },
        )
        first = tracker.save_archive_comment_from_ndgr(conn, "lv100", comment)
        second = tracker.save_archive_comment_from_ndgr(conn, "lv100", comment)
        conn.commit()
        comment_count = conn.execute(
            "SELECT COUNT(*) FROM archive_comments WHERE lv = 'lv100'"
        ).fetchone()[0]
        ranking_count = conn.execute(
            "SELECT comment_count FROM archive_comment_ranking "
            "WHERE lv = 'lv100' AND user_id = 'user-1'"
        ).fetchone()[0]
    assert first["inserted"] is True
    assert second["inserted"] is False
    assert comment_count == 1
    assert ranking_count == 1


def test_local_comment_processing_reuses_complete_archive_comments(
    isolated_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lv = "lv100"
    with tracker.connect() as conn:
        tracker.save_broadcast_archive_meta(
            conn,
            {
                "lv": lv,
                "raw_json": json.dumps(
                    {
                        "source": "user-broadcast-history",
                        "program": {
                            "statistics": {"comments": {"value": 11}},
                        },
                    }
                ),
            },
        )
        for number in range(1, 12):
            tracker.save_archive_comment_from_ndgr(
                conn,
                lv,
                {
                    "no": str(number),
                    "user_id": f"user-{number}",
                    "text": f"comment-{number}",
                    "date": "110",
                    "vpos": str(number * 100),
                },
            )
        conn.commit()

    def fail_download(*_args, **_kwargs):
        raise AssertionError("complete DB comments must not call the timeshift API")

    monkeypatch.setattr(tracker, "download_timeshift_comments", fail_download)
    result = tracker.download_and_store_archive_comments(lv, SimpleNamespace())

    assert result["reused"] is True
    assert result["source"] == "database"
    assert result["stored_count"] == 11
    assert result["expected_count"] == 11
    assert result["reason"] == "archive_comments_complete"
    with tracker.connect() as conn:
        completed = conn.execute(
            "SELECT comments_fetch_completed FROM broadcast_archive_meta WHERE lv = ?",
            (lv,),
        ).fetchone()[0]
    assert completed == 1


def test_local_comment_processing_accepts_saved_provider_zero_without_api(
    isolated_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lv = "lv100"
    with tracker.connect() as conn:
        tracker.save_broadcast_archive_meta(
            conn,
            {
                "lv": lv,
                "raw_json": json.dumps(
                    {
                        "source": "user-broadcast-history",
                        "program": {
                            "statistics": {"comments": {"value": 0}},
                        },
                    }
                ),
            },
        )
        conn.commit()

    def fail_download(*_args, **_kwargs):
        raise AssertionError("provider total 0 must not call the timeshift API")

    monkeypatch.setattr(tracker, "download_timeshift_comments", fail_download)
    result = tracker.download_and_store_archive_comments(lv, SimpleNamespace())

    assert result["reused"] is True
    assert result["source"] == "database"
    assert result["stored_count"] == 0
    assert result["expected_count"] == 0
    assert result["reason"] == "provider_comment_count_zero"
    with tracker.connect() as conn:
        completed = conn.execute(
            "SELECT comments_fetch_completed FROM broadcast_archive_meta WHERE lv = ?",
            (lv,),
        ).fetchone()[0]
    assert completed == 1


def test_local_comment_processing_continues_when_timeshift_api_fails(
    isolated_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lv = "lv100"
    with tracker.connect() as conn:
        tracker.save_broadcast_archive_meta(
            conn,
            {
                "lv": lv,
                "raw_json": json.dumps(
                    {
                        "source": "user-broadcast-history",
                        "program": {"statistics": {"comments": {"value": 1}}},
                    }
                ),
            },
        )
        conn.commit()

    def fail_download(*_args, **_kwargs):
        raise ValueError("Failed to reserve timeshift. (HTTP Error 404)")

    monkeypatch.setattr(tracker, "download_timeshift_comments", fail_download)
    result = tracker.download_and_store_archive_comments(lv, SimpleNamespace())

    assert result["reused"] is True
    assert result["source"] == "database_fallback"
    assert result["reason"] == "timeshift_api_failed_fallback"
    assert result["stored_count"] == 0
    assert result["expected_count"] == 1
    assert "HTTP Error 404" in result["acquisition_error"]
    with tracker.connect() as conn:
        saved_error = conn.execute(
            "SELECT comments_fetch_error FROM broadcast_archive_meta WHERE lv = ?",
            (lv,),
        ).fetchone()[0]
    assert "HTTP Error 404" in saved_error


def test_processed_video_moves_below_generated_html_directory(
    isolated_db: Path,
    tmp_path: Path,
) -> None:
    lv = "lv100"
    source_dir = tmp_path / "rec_file" / "39532023_yosino"
    source_dir.mkdir(parents=True)
    source = source_dir / "lv100_recording.mp4"
    source.write_bytes(b"video")
    html_dir = tmp_path / "target" / "39532023" / "broadcast" / lv
    html_dir.mkdir(parents=True)
    html_file = html_dir / "lv100_title.html"
    html_file.write_text("done", encoding="utf-8")
    stamp = tracker.now_micro()
    with tracker.connect() as conn:
        conn.execute(
            """
            INSERT INTO recording_segments
                (lv, source_path, created_at, updated_at)
            VALUES (?, ?, ?, ?)
            """,
            (lv, str(source.resolve()), stamp, stamp),
        )
        conn.commit()

    result = tracker.archive_processed_video_files(
        lv,
        [source],
        html_file=html_file,
    )

    archived = html_dir / "archive" / source.name
    assert not source.exists()
    assert archived.read_bytes() == b"video"
    assert result["archive_dir"] == str((html_dir / "archive").resolve())
    assert result["moved"][0]["archive_path"] == str(archived.resolve())
    with tracker.connect() as conn:
        row = conn.execute(
            "SELECT source_path FROM recording_segments WHERE lv = ?",
            (lv,),
        ).fetchone()
    assert row["source_path"] == str(archived.resolve())


def test_finalize_wait_reports_only_stage_start_events(
    isolated_db: Path,
) -> None:
    lv = "lv100"
    with tracker.connect() as conn:
        assert tracker.reserve_finalize_queue_item(
            conn,
            lv=lv,
            broadcaster_id="1",
            target_dir="target",
        )
        tracker.mark_finalize_queue_ready(conn, lv)
        for stage, message in (
            ("collect_segments", "stage=running"),
            ("collect_segments", "stage=done"),
            ("timeline", "録画時間軸検証完了"),
            ("archive_steps", "legacy step開始: step12_html_generator"),
            ("archive_steps", "legacy step完了: step12_html_generator"),
        ):
            conn.execute(
                """
                INSERT INTO postprocess_logs
                    (lv, stage, level, message, payload_json, created_at)
                VALUES (?, ?, 'INFO', ?, '{}', ?)
                """,
                (lv, stage, message, tracker.now_micro()),
            )
        conn.execute(
            """
            UPDATE finalize_queue
            SET status = 'done', result_json = ?, updated_at = ?
            WHERE lv = ?
            """,
            (json.dumps({"lv": lv, "done": True}), tracker.now_micro(), lv),
        )
        conn.commit()

    events: list[tuple[str, str]] = []
    result = tracker.wait_for_finalize_queue_item(
        lv,
        stage_start_callback=lambda stage, message: events.append((stage, message)),
    )

    assert result["done"] is True
    assert events == [
        ("collect_segments", "stage=running"),
        ("archive_steps", "legacy step開始: step12_html_generator"),
    ]


def test_timeshift_video_selection_prefers_mp4_when_duration_is_equivalent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    folder = tmp_path / "39532023_yosino"
    folder.mkdir()
    ts_path = folder / "lv100_old.ts"
    mp4_path = folder / "lv100_old.mp4"
    partial_path = folder / "lv100_partial.mp4"
    for path in (ts_path, mp4_path, partial_path):
        path.write_bytes(b"video")
    durations = {ts_path: 1_800.2, mp4_path: 1_800.0, partial_path: 1_200.0}
    monkeypatch.setattr(
        tracker,
        "probe_media_duration_seconds",
        lambda path: durations[Path(path)],
    )
    selected = tracker.select_preferred_timeshift_video(
        [partial_path, ts_path, mp4_path],
        broadcaster_id="39532023",
    )
    assert selected == mp4_path


def test_timeshift_recorder_reuses_existing_broadcaster_video(
    isolated_db: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    recorder_dir = tmp_path / "recorder"
    recorder_dir.mkdir()
    exe = recorder_dir / "SlNicoLiveRec.exe"
    exe.write_bytes(b"exe")
    (recorder_dir / "SlNicoLiveRec_config.json").write_text(
        json.dumps({"CloseWindowOnExit": True}),
        encoding="utf-8",
    )
    video_dir = tmp_path / "rec_file" / "39532023_yosino"
    video_dir.mkdir(parents=True)
    video = video_dir / "lv100_program.mp4"
    video.write_bytes(b"video")
    config = SimpleNamespace(
        slnico_live_rec_exe=str(exe),
        target_root=str(tmp_path / "target"),
        recording_account_id="fallback",
    )
    monkeypatch.setattr(tracker, "slnico_storage_root", lambda _config=None: tmp_path / "rec_file")
    monkeypatch.setattr(tracker, "probe_media_duration_seconds", lambda _path: 1_800.0)

    result = tracker.download_timeshift_video_with_recorder(
        {
            "lv": "lv100",
            "watch_url": "https://live.nicovideo.jp/watch/lv100",
            "broadcaster_id": "39532023",
            "broadcaster_name": "yosino",
        },
        config,
        wait_for_live_recordings=False,
    )
    assert result["reused"] is True
    assert result["video_paths"] == [str(video)]
    assert (tmp_path / "target" / "platform" / "niconico" / "39532023").exists()


def test_timeshift_job_forwards_its_step_logs_to_gui_detail_signal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # The full suite also loads lab/simple_comment_viewer/app. Make this
    # repository's namespace visible while importing the main GUI module.
    loaded_app = sys.modules.get("app")
    if loaded_app is not None and hasattr(loaded_app, "__path__"):
        main_app_dir = str(Path(__file__).resolve().parents[1] / "app")
        monkeypatch.setattr(
            loaded_app,
            "__path__",
            [main_app_dir, *list(loaded_app.__path__)],
        )
    import gui_app

    video = tmp_path / "lv100.mp4"
    video.write_bytes(b"video")
    item = {
        "lv": "lv100",
        "watch_url": "https://live.nicovideo.jp/watch/lv100",
        "broadcaster_id": "39532023",
        "broadcaster_name": "yosino",
        "timeshift_end_time": 2_000,
    }
    active_sinks: list = []
    added_sinks: list = []
    removed_sinks: list = []

    def add_sink(sink) -> None:
        active_sinks.append(sink)
        added_sinks.append(sink)

    def remove_sink(sink) -> None:
        active_sinks.remove(sink)
        removed_sinks.append(sink)

    def emit_log(level: str, message: str) -> None:
        for sink in list(active_sinks):
            sink(level, message)

    def fake_video(*_args, **_kwargs):
        emit_log("INFO", "lv100 timeshift_video: 既存動画を確認")
        emit_log("INFO", "lv999 timeshift_video: 別番組")
        other_thread = threading.Thread(
            target=lambda: emit_log("DEBUG", "lv100 transcribe: 別スレッド")
        )
        other_thread.start()
        other_thread.join()
        return {"video_paths": [str(video)], "reused": True}

    def fake_comments(*_args, **_kwargs):
        emit_log("INFO", "lv100 timeshift_comments_api: コメントAPI取得開始")
        return {"fetched_count": 3, "inserted_count": 2}

    def fake_finalize(*_args, **_kwargs):
        emit_log("INFO", "lv100 timeline: 録画時間軸確定")
        emit_log("DEBUG", "lv100 extract_wav: 音声抽出開始")
        emit_log("DEBUG", "lv100 encode_mp3: MP3作成開始")
        emit_log("DEBUG", "lv100 transcribe: FasterWhisper開始")
        emit_log("INFO", "lv100 archive_steps: legacy step開始: step12_html_generator")
        return {
            "legacy_archiver": {
                "steps": {
                    "step12_html_generator": {
                        "result": {"html_file": str(tmp_path / "index.html")}
                    }
                }
            }
        }

    monkeypatch.setattr(gui_app.tracker, "resolve_timeshift_input_urls", lambda _urls: [item])
    monkeypatch.setattr(gui_app.tracker, "load_config", lambda: SimpleNamespace())
    monkeypatch.setattr(gui_app.tracker, "existing_generated_archive_html", lambda _lv: None)
    monkeypatch.setattr(gui_app.tracker, "download_timeshift_video_with_recorder", fake_video)
    monkeypatch.setattr(gui_app.tracker, "download_and_store_archive_comments", fake_comments)
    monkeypatch.setattr(gui_app.tracker, "enqueue_finalize_pipeline_and_wait", fake_finalize)
    monkeypatch.setattr(gui_app.tracker, "add_log_sink", add_sink)
    monkeypatch.setattr(gui_app.tracker, "remove_log_sink", remove_sink)

    details: list[str] = []
    finished: list[dict] = []
    monkeypatch.setenv(gui_app.APP_ROLE_ENV, "timeshift")
    job = gui_app.TimeshiftAcquireJob([item["watch_url"]])
    job.signals.detail.connect(details.append)
    job.signals.finished.connect(finished.append)
    job.run()

    assert [line.split(": [", 1)[1] for line in details] == [
        "INFO] timeshift_video: 既存動画を確認",
        "INFO] timeshift_comments_api: コメントAPI取得開始",
        "INFO] timeline: 録画時間軸確定",
        "DEBUG] extract_wav: 音声抽出開始",
        "DEBUG] encode_mp3: MP3作成開始",
        "DEBUG] transcribe: FasterWhisper開始",
        "INFO] archive_steps: legacy step開始: step12_html_generator",
    ]
    assert all("lv999" not in line and "別スレッド" not in line for line in details)
    assert added_sinks == removed_sinks
    assert active_sinks == []
    assert len(finished) == 1
    assert finished[0]["failures"] == []


def test_local_files_job_downloads_comments_before_finalize(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    loaded_app = sys.modules.get("app")
    if loaded_app is not None and hasattr(loaded_app, "__path__"):
        main_app_dir = str(Path(__file__).resolve().parents[1] / "app")
        monkeypatch.setattr(
            loaded_app,
            "__path__",
            [main_app_dir, *list(loaded_app.__path__)],
        )
    import gui_app

    video = tmp_path / "lv100_part1.mp4"
    video.write_bytes(b"video")
    config = SimpleNamespace(name="shared-config")
    call_order: list[str] = []

    monkeypatch.setenv(gui_app.APP_ROLE_ENV, "timeshift")
    monkeypatch.setattr(gui_app.tracker, "load_config", lambda: config)

    def fake_comments(lv, received_config, **_kwargs):
        assert lv == "lv100"
        assert received_config is config
        call_order.append("comments")
        return {
            "fetched_count": 12,
            "inserted_count": 10,
            "duplicate_count": 2,
        }

    def fake_finalize(lv, **kwargs):
        assert lv == "lv100"
        assert kwargs["timeline_mode"] == "timeshift"
        assert kwargs["segment_paths"] == [video]
        call_order.append("finalize")
        stage_callback = kwargs["stage_start_callback"]
        stage_callback("collect_segments", "stage=running")
        stage_callback("archive_steps", "legacy step開始: step12_html_generator")
        return {
            "target_dir": str(tmp_path),
            "mp3_path": str(tmp_path / "lv100.mp3"),
            "recording_segment_timeline": {"total_duration_seconds": 1_800.0},
            "legacy_archiver": {
                "steps": {
                    "step12_html_generator": {
                        "result": {"html_file": str(tmp_path / "lv100.html")}
                    }
                }
            },
        }

    def fake_archive(lv, paths, **kwargs):
        assert lv == "lv100"
        assert paths == [video]
        call_order.append("archive")
        return {
            "archive_dir": str(tmp_path / "archive"),
            "moved": [{"archive_path": str(tmp_path / "archive" / video.name)}],
        }

    monkeypatch.setattr(
        gui_app.tracker,
        "download_and_store_archive_comments",
        fake_comments,
    )
    monkeypatch.setattr(gui_app.tracker, "enqueue_finalize_pipeline_and_wait", fake_finalize)
    monkeypatch.setattr(gui_app.tracker, "archive_processed_video_files", fake_archive)

    progress: list[str] = []
    finished: list[dict] = []
    job = gui_app.TimeshiftFinalizeJob({"lv100": [video]})
    job.signals.progress.connect(progress.append)
    job.signals.finished.connect(finished.append)
    job.run()

    assert call_order == ["comments", "finalize", "archive"]
    assert any("コメントDB確認開始" in line for line in progress)
    assert any("コメントAPI取得・保存完了 取得=12 新規=10 重複=2" in line for line in progress)
    assert any("工程開始: 録画区間収集" in line for line in progress)
    assert any("工程開始: HTML生成（step12_html_generator）" in line for line in progress)
    assert finished[0]["failures"] == []
    assert finished[0]["successes"][0]["comments"]["fetched_count"] == 12


def test_local_files_job_runs_oldest_broadcast_first(
    isolated_db: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import gui_app

    old_video = tmp_path / "lv100_old.mp4"
    new_video = tmp_path / "lv200_new.mp4"
    old_video.write_bytes(b"old")
    new_video.write_bytes(b"new")
    with tracker.connect() as conn:
        tracker.save_broadcast_archive_meta(conn, {"lv": "lv100", "start_time": 100})
        tracker.save_broadcast_archive_meta(conn, {"lv": "lv200", "start_time": 200})
        conn.commit()

    order: list[str] = []
    monkeypatch.setenv(gui_app.APP_ROLE_ENV, "timeshift")
    monkeypatch.setattr(gui_app.tracker, "load_config", lambda: SimpleNamespace())

    def fake_comments(lv, *_args, **_kwargs):
        order.append(f"comments:{lv}")
        return {
            "fetched_count": 0,
            "inserted_count": 0,
            "duplicate_count": 0,
            "stored_count": 0,
            "expected_count": 0,
            "reused": True,
        }

    def fake_finalize(lv, **_kwargs):
        order.append(f"finalize:{lv}")
        return {
            "target_dir": str(tmp_path / lv),
            "recording_segment_timeline": {},
            "legacy_archiver": {
                "steps": {
                    "step12_html_generator": {
                        "result": {"html_file": str(tmp_path / lv / f"{lv}.html")}
                    }
                }
            },
        }

    def fake_archive(lv, *_args, **_kwargs):
        order.append(f"archive:{lv}")
        return {"archive_dir": str(tmp_path / lv / "archive"), "moved": [{}]}

    monkeypatch.setattr(gui_app.tracker, "download_and_store_archive_comments", fake_comments)
    monkeypatch.setattr(gui_app.tracker, "enqueue_finalize_pipeline_and_wait", fake_finalize)
    monkeypatch.setattr(gui_app.tracker, "archive_processed_video_files", fake_archive)

    job = gui_app.TimeshiftFinalizeJob(
        {
            "lv200": [new_video],
            "lv100": [old_video],
        }
    )
    job.run()

    assert order == [
        "comments:lv100",
        "finalize:lv100",
        "archive:lv100",
        "comments:lv200",
        "finalize:lv200",
        "archive:lv200",
    ]
