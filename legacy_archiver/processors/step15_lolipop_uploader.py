from __future__ import annotations

import json
import os
import re
import subprocess
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit
from urllib.request import Request, urlopen

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from utils import find_account_directory
from archive_db import db_path, load_broadcast_data


ARCHIVE_DATA_PATTERN = re.compile(
    r"<script\b[^>]*\bid=[\"']archive-data[\"'][^>]*>(.*?)</script>",
    re.IGNORECASE | re.DOTALL,
)
TIMELINE_MARKERS = ('id="timeline2"', "id='timeline2'")
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def process(pipeline_data):
    """Step15: 完成済みアーカイブを設定済みターゲットへ自動送信する。"""
    account_id = str(pipeline_data.get("account_id") or "").strip()
    lv_value = str(pipeline_data.get("lv_value") or "").strip()
    config = pipeline_data.get("config") or {}
    settings = config.get("upload_settings") or {}
    if not settings.get("enable_auto_upload", False):
        return {"uploaded": False, "reason": "feature_disabled"}
    if not account_id:
        raise RuntimeError("Step15: account_id がありません")
    if not re.fullmatch(r"lv\d+", lv_value):
        raise RuntimeError(f"Step15: lv_value が不正です: {lv_value or '(empty)'}")

    account_dir_value = find_account_directory(
        pipeline_data["platform_directory"], account_id
    )
    if not account_dir_value:
        raise RuntimeError(f"Step15: アカウントディレクトリが見つかりません: {account_id}")
    account_dir = Path(account_dir_value).resolve()
    broadcast_data = load_broadcast_data(lv_value) or {}
    registered_broadcast_dir = str(broadcast_data.get("broadcast_directory_path") or "").strip()
    if registered_broadcast_dir:
        account_dir = Path(registered_broadcast_dir).resolve().parent
    publish_paths, skipped_paths = collect_publish_paths(account_dir, lv_value)
    changed_html_paths, rejected_changed_paths = collect_changed_html_paths(
        account_dir,
        pipeline_data.get("results") or {},
    )
    publish_paths = sorted(
        {*publish_paths, *changed_html_paths},
        key=publish_sort_key,
    )
    html_only = bool(settings.get("html_only", False))
    if html_only:
        publish_paths = [
            path for path in publish_paths if PurePosixPath(path).suffix.lower() == ".html"
        ]
    skipped_paths = sorted({*skipped_paths, *rejected_changed_paths})
    detail_prefix = f"{lv_value}/"
    if not any(
        path.startswith(detail_prefix)
        and path.lower().endswith(".html")
        and not path.lower().endswith("_mobile.html")
        for path in publish_paths
    ):
        raise RuntimeError(f"Step15: {lv_value} のアップロード対象の完成HTMLがありません")

    target_id = str(settings.get("target_id") or "lolipop-main").strip()
    remote_template = str(
        settings.get("remote_directory_template") or "niconico/{account_id}"
    )
    try:
        remote_directory = remote_template.format(account_id=account_id)
    except (KeyError, ValueError) as exc:
        raise RuntimeError(f"Step15: remote_directory_template が不正です: {exc}") from exc

    python_exe = Path(
        str(
            settings.get("python_exe")
            or PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
        )
    )
    cli_path = Path(
        str(
            settings.get("cli_path")
            or os.environ.get("NICONICO_UPLOAD_TARGETS_CLI")
            or PROJECT_ROOT / "tools" / "upload_targets_cli.py"
        )
    )
    if not python_exe.is_file():
        raise RuntimeError(f"Step15: Pythonが見つかりません: {python_exe}")
    if not cli_path.is_file():
        raise RuntimeError(f"Step15: upload-targets CLIが見つかりません: {cli_path}")

    ensure_credentials_api(settings)

    command = [
        str(python_exe),
        str(cli_path),
        "upload",
        "--target",
        target_id,
        "--source-root",
        str(account_dir),
        "--remote-dir",
        remote_directory,
    ]
    for relative_path in publish_paths:
        command.extend(("--path", relative_path))
    command.extend(("--force-overwrite", "--verify-after", "--json"))
    if settings.get("http_verify", True):
        command.append("--http-verify")

    print(
        f"Step15 自動アップロード開始: target={target_id} "
        f"remote={remote_directory} lv={lv_value} files={len(publish_paths)} "
        f"paths={publish_paths}"
    )
    completed = subprocess.run(
        command,
        cwd=str(cli_path.parent),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=max(30, int(settings.get("timeout_seconds") or 900)),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        check=False,
    )
    payload = parse_cli_payload(completed.stdout)
    if completed.returncode != 0 or not payload.get("success", False):
        detail = str(payload.get("error") or completed.stderr or "upload failed").strip()
        raise RuntimeError(
            f"Step15: 自動アップロード失敗 exit={completed.returncode}: {detail[:1000]}"
        )

    details = payload.get("details") or {}
    verification = payload.get("verification") or {}
    result = {
        "uploaded": True,
        "lv": lv_value,
        "target_id": target_id,
        "remote_directory": remote_directory,
        "file_count": len(publish_paths),
        "uploaded_count": int(details.get("uploaded") or 0),
        "skipped_count": int(details.get("skipped") or 0),
        "verification_success": bool(verification.get("success", False)),
        "html_only": html_only,
        "paths": publish_paths,
        "incomplete_paths_skipped": skipped_paths,
    }
    mark_archive_upload_completed(
        lv_value,
        target_id=target_id,
        remote_directory=remote_directory,
    )
    print(
        f"Step15 完了: files={result['file_count']} "
        f"uploaded={result['uploaded_count']} skipped={result['skipped_count']}"
    )
    return result


def mark_archive_upload_completed(
    lv_value: str,
    *,
    target_id: str,
    remote_directory: str,
) -> None:
    """通常アーカイブのアップロード成功をLV単位で記録する。"""
    completed_at = datetime.now().isoformat(timespec="microseconds")
    with sqlite3.connect(db_path()) as conn:
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(broadcast_archive_meta)")
        }
        required = {
            "archive_upload_completed": "INTEGER NOT NULL DEFAULT 0",
            "archive_upload_completed_at": "TEXT",
            "archive_upload_target_id": "TEXT",
            "archive_upload_remote_directory": "TEXT",
        }
        for column, definition in required.items():
            if column not in columns:
                conn.execute(
                    f"ALTER TABLE broadcast_archive_meta ADD COLUMN {column} {definition}"
                )
        conn.execute(
            """
            INSERT INTO broadcast_archive_meta
                (lv, fetched_at, archive_upload_completed,
                 archive_upload_completed_at, archive_upload_target_id,
                 archive_upload_remote_directory)
            VALUES (?, ?, 1, ?, ?, ?)
            ON CONFLICT(lv) DO UPDATE SET
                archive_upload_completed = 1,
                archive_upload_completed_at = excluded.archive_upload_completed_at,
                archive_upload_target_id = excluded.archive_upload_target_id,
                archive_upload_remote_directory = excluded.archive_upload_remote_directory
            """,
            (
                lv_value,
                completed_at,
                completed_at,
                target_id,
                remote_directory,
            ),
        )


def collect_publish_paths(
    account_dir: Path | str,
    lv_value: str,
) -> tuple[list[str], list[str]]:
    """現在処理中のLVと、それによって更新された共有ページだけを列挙する。"""
    root = Path(account_dir).resolve()
    current_lv = str(lv_value or "").strip()
    if not re.fullmatch(r"lv\d+", current_lv):
        raise RuntimeError(f"Step15: lv_value が不正です: {current_lv or '(empty)'}")
    index_path = root / "index.html"
    if not index_path.is_file():
        raise RuntimeError(f"Step15: index.html がありません: {index_path}")

    index_text = index_path.read_text(encoding="utf-8")
    match = ARCHIVE_DATA_PATTERN.search(index_text)
    if not match:
        raise RuntimeError("Step15: index.html に archive-data がありません")
    try:
        records = json.loads(match.group(1).strip())
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Step15: archive-data JSONが不正です: {exc}") from exc
    if not isinstance(records, list):
        raise RuntimeError("Step15: archive-data は配列ではありません")

    # index.html は今回のLVを一覧へ公開するため更新済み。タグページは
    # 今回のLVに実際に付いたタグだけを送る。過去LVの成果物は一切列挙しない。
    paths = {"index.html"}
    skipped: set[str] = set()
    tags_dir = root / "tags"

    for record in records:
        if not isinstance(record, dict):
            continue
        relative_path = normalize_detail_path(record.get("url"))
        record_lv = str(record.get("lv") or "").strip()
        path_lv = PurePosixPath(relative_path).parts[0] if relative_path else ""
        if record_lv != current_lv and path_lv != current_lv:
            continue
        if not relative_path:
            skipped.add(str(record.get("url") or current_lv))
            continue
        candidate = (root / Path(*PurePosixPath(relative_path).parts)).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            skipped.add(relative_path)
            continue
        if not candidate.is_file() or not is_completed_detail_html(candidate):
            skipped.add(relative_path)
            continue
        paths.add(relative_path)

        tags = record.get("tags") or []
        if isinstance(tags, str):
            tags = [tags]
        if isinstance(tags, (list, tuple, set)) and tags_dir.is_dir():
            for tag in tags:
                tag_path = tags_dir / tag_page_filename(tag)
                if tag_path.is_file():
                    paths.add(tag_path.relative_to(root).as_posix())

        detail_dir = candidate.parent
        audio_path = detail_dir / f"{current_lv}_audio.mp3"
        if audio_path.is_file():
            paths.add(audio_path.relative_to(root).as_posix())

        screenshot_dir = detail_dir / "screenshot"
        if screenshot_dir.is_dir():
            paths.add(screenshot_dir.relative_to(root).as_posix())

    return sorted(paths, key=publish_sort_key), sorted(skipped)


def collect_changed_html_paths(
    account_dir: Path | str,
    step_results: dict,
) -> tuple[list[str], list[str]]:
    """Step13/14が管理領域を更新した既存HTMLを安全に追加する。"""
    root = Path(account_dir).resolve()
    accepted: set[str] = set()
    rejected: set[str] = set()
    if not isinstance(step_results, dict):
        return [], []
    for result in step_results.values():
        if not isinstance(result, dict):
            continue
        values = result.get("updated_html_paths") or []
        if isinstance(values, str):
            values = [values]
        if not isinstance(values, (list, tuple, set)):
            continue
        for value in values:
            relative = str(value or "").strip().replace("\\", "/")
            pure = PurePosixPath(relative)
            if (
                not relative
                or pure.is_absolute()
                or ".." in pure.parts
                or pure.suffix.lower() != ".html"
                or pure.name.lower().endswith("_mobile.html")
            ):
                rejected.add(relative or "(empty)")
                continue
            candidate = (root / Path(*pure.parts)).resolve()
            try:
                candidate.relative_to(root)
            except ValueError:
                rejected.add(relative)
                continue
            if not candidate.is_file():
                rejected.add(relative)
                continue
            accepted.add(candidate.relative_to(root).as_posix())
    return sorted(accepted, key=publish_sort_key), sorted(rejected)


def tag_page_filename(tag) -> str:
    """Step13と同じ規則で、現在LVに関連するタグページ名を得る。"""
    safe_tag = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", str(tag or "").strip())
    safe_tag = safe_tag.rstrip(". ") or "untagged"
    return f"tag_{safe_tag}.html"


def normalize_detail_path(value) -> str:
    raw = str(value or "").strip().replace("\\", "/")
    if not raw:
        return ""
    parsed = urlsplit(raw)
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
        return ""
    decoded = unquote(parsed.path)
    pure_path = PurePosixPath(decoded)
    if pure_path.is_absolute() or ".." in pure_path.parts:
        return ""
    if len(pure_path.parts) != 2 or not re.fullmatch(r"lv\d+", pure_path.parts[0]):
        return ""
    if pure_path.suffix.lower() != ".html" or pure_path.name.lower().endswith(
        "_mobile.html"
    ):
        return ""
    return pure_path.as_posix()


def is_completed_detail_html(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    return any(marker in text for marker in TIMELINE_MARKERS)


def publish_sort_key(path: str) -> tuple[int, str]:
    if path == "index.html":
        return (0, path)
    if path.startswith("tags/"):
        return (1, path)
    if path.endswith("_audio.mp3"):
        return (3, path)
    if path.endswith("_mobile_data") or "/screenshot" in path:
        return (4, path)
    return (2, path)


def parse_cli_payload(stdout: str) -> dict:
    text = str(stdout or "").strip()
    if not text:
        return {}
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Step15: upload-targetsのJSON応答を読めません: {exc}") from exc
    return payload if isinstance(payload, dict) else {}


def ensure_credentials_api(settings: dict) -> None:
    """認証APIが停止中なら既存サービスを非表示で起動する。"""
    if not settings.get("auto_start_credentials_api", True):
        return
    health_url = str(
        settings.get("credentials_api_health_url")
        or "http://127.0.0.1:8796/health"
    )
    if credentials_api_healthy(health_url):
        return

    python_exe = Path(
        str(
            settings.get("credentials_api_python_exe")
            or PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
        )
    )
    workdir = Path(str(
        settings.get("credentials_api_workdir")
        or os.environ.get("NICONICO_CREDENTIALS_API_WORKDIR")
        or PROJECT_ROOT
    ))
    module = str(
        settings.get("credentials_api_module")
        or "scripts.password_manager.api_main"
    )
    if not python_exe.is_file() or not workdir.is_dir():
        raise RuntimeError("Step15: パスワード管理APIの起動環境がありません")

    subprocess.Popen(
        [str(python_exe), "-m", module],
        cwd=str(workdir),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    deadline = time.monotonic() + max(
        1, int(settings.get("credentials_api_start_timeout_seconds") or 10)
    )
    while time.monotonic() < deadline:
        if credentials_api_healthy(health_url):
            print("Step15: パスワード管理APIを自動起動しました")
            return
        time.sleep(0.25)
    raise RuntimeError("Step15: パスワード管理APIを自動起動できませんでした")


def credentials_api_healthy(url: str) -> bool:
    try:
        request = Request(url, headers={"User-Agent": "niconico-step15/1"})
        with urlopen(request, timeout=1.5) as response:
            return 200 <= int(response.status) < 300
    except Exception:
        return False
