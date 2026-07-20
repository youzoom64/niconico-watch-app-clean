from __future__ import annotations

import os
import secrets
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any
import json


APP_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = APP_ROOT / "data" / "tracker.db"


def db_path() -> Path:
    raw = os.environ.get("NICONICO_WATCH_APP_DB", "").strip()
    return Path(raw) if raw else DEFAULT_DB_PATH


def connect() -> sqlite3.Connection | None:
    path = db_path()
    if not path.exists():
        print(f"archive DBなし: {path}")
        return None
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    ensure_archive_tables(conn)
    return conn


def ensure_archive_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS archive_broadcast_data (
            lv TEXT PRIMARY KEY,
            payload_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS archive_comment_time_adjustments (
            lv TEXT PRIMARY KEY,
            offset_seconds INTEGER NOT NULL DEFAULT 0,
            confirmed INTEGER NOT NULL DEFAULT 0,
            confirm_token TEXT NOT NULL,
            confirmed_at TEXT,
            html_paths_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )


def load_or_create_comment_time_adjustment(lv_value: str) -> dict[str, Any]:
    """Return the durable preview/confirmed state embedded into generated HTML."""
    conn = connect()
    if conn is None:
        return {
            "lv": lv_value,
            "offset_seconds": 0,
            "confirmed": False,
            "confirm_token": "",
        }
    try:
        row = conn.execute(
            "SELECT * FROM archive_comment_time_adjustments WHERE lv = ?",
            (lv_value,),
        ).fetchone()
        if row is None:
            current_time = datetime.now().isoformat()
            conn.execute(
                """
                INSERT INTO archive_comment_time_adjustments
                    (lv, offset_seconds, confirmed, confirm_token, created_at, updated_at)
                VALUES (?, 0, 0, ?, ?, ?)
                """,
                (lv_value, secrets.token_urlsafe(24), current_time, current_time),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM archive_comment_time_adjustments WHERE lv = ?",
                (lv_value,),
            ).fetchone()
        return {
            "lv": str(row["lv"] or lv_value),
            "offset_seconds": int(row["offset_seconds"] or 0),
            "confirmed": bool(row["confirmed"]),
            "confirm_token": str(row["confirm_token"] or ""),
            "confirmed_at": str(row["confirmed_at"] or ""),
        }
    finally:
        conn.close()


def load_broadcast_data(lv_value: str) -> dict[str, Any]:
    conn = connect()
    if conn is None:
        return {}
    try:
        row = conn.execute(
            "SELECT payload_json FROM archive_broadcast_data WHERE lv = ?",
            (lv_value,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return {}
    try:
        import json

        data = json.loads(str(row["payload_json"] or "{}"))
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        print(f"archive_broadcast_data読込エラー: {lv_value}: {exc}")
        return {}


def list_broadcast_data(owner_id: str | int | None = None) -> list[dict[str, Any]]:
    target_owner_id = str(owner_id or "").strip()
    conn = connect()
    if conn is None:
        return []
    try:
        rows = conn.execute(
            """
            SELECT d.lv, d.payload_json,
                   COALESCE(m.begin_time, m.open_time, m.start_time, m.end_time, 0) AS time_value
            FROM archive_broadcast_data d
            LEFT JOIN broadcast_archive_meta m ON m.lv = d.lv
            ORDER BY time_value DESC, d.lv DESC
            """
        ).fetchall()
    except sqlite3.OperationalError:
        rows = conn.execute(
            """
            SELECT d.lv, d.payload_json, 0 AS time_value
            FROM archive_broadcast_data d
            ORDER BY d.lv DESC
            """
        ).fetchall()
    finally:
        conn.close()

    broadcasts: list[dict[str, Any]] = []
    for row in rows:
        try:
            data = json.loads(str(row["payload_json"] or "{}"))
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        data.setdefault("lv_value", str(row["lv"] or ""))
        if row["time_value"]:
            data.setdefault("start_time", row["time_value"])
        if target_owner_id and str(data.get("owner_id") or "").strip() != target_owner_id:
            continue
        broadcasts.append(data)
    return broadcasts


def load_previous_broadcast_summary(lv_value: str, owner_id: str | int | None = None) -> str:
    current_lv = str(lv_value or "").strip()
    current_data = load_broadcast_data(current_lv)
    target_owner_id = str(owner_id or current_data.get("owner_id") or "").strip()
    if not target_owner_id:
        return ""

    try:
        current_lv_num = int(current_lv.removeprefix("lv"))
    except ValueError:
        current_lv_num = 0

    current_time = 0
    conn = connect()
    if conn is None:
        return ""
    try:
        try:
            row = conn.execute(
                """
                SELECT COALESCE(begin_time, open_time, start_time, end_time, 0) AS time_value
                FROM broadcast_archive_meta
                WHERE lv = ?
                """,
                (current_lv,),
            ).fetchone()
            if row:
                current_time = int(row["time_value"] or 0)
        except sqlite3.OperationalError:
            current_time = 0

        try:
            rows = conn.execute(
                """
                SELECT d.lv, d.payload_json,
                       COALESCE(m.begin_time, m.open_time, m.start_time, m.end_time, 0) AS time_value
                FROM archive_broadcast_data d
                LEFT JOIN broadcast_archive_meta m ON m.lv = d.lv
                WHERE d.lv != ?
                """,
                (current_lv,),
            ).fetchall()
        except sqlite3.OperationalError:
            rows = conn.execute(
                """
                SELECT d.lv, d.payload_json, 0 AS time_value
                FROM archive_broadcast_data d
                WHERE d.lv != ?
                """,
                (current_lv,),
            ).fetchall()
    finally:
        conn.close()

    candidates: list[tuple[int, int, str, str]] = []
    for row in rows:
        try:
            data = json.loads(str(row["payload_json"] or "{}"))
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        if str(data.get("owner_id") or "").strip() != target_owner_id:
            continue
        summary = str(data.get("summary_text") or "").strip()
        if not summary:
            continue
        prev_lv = str(row["lv"] or "").strip()
        try:
            prev_lv_num = int(prev_lv.removeprefix("lv"))
        except ValueError:
            prev_lv_num = 0
        time_value = int(row["time_value"] or 0)
        if current_time and time_value and time_value >= current_time:
            continue
        if not current_time and current_lv_num and prev_lv_num and prev_lv_num >= current_lv_num:
            continue
        candidates.append((time_value, prev_lv_num, prev_lv, summary))

    if not candidates:
        return ""
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    prev_lv = candidates[0][2]
    print(f"前回放送の要約取得(DB): {prev_lv}")
    return candidates[0][3]


def save_broadcast_data(lv_value: str, data: dict[str, Any]) -> None:
    conn = connect()
    if conn is None:
        return
    try:
        import json

        conn.execute(
            """
            INSERT INTO archive_broadcast_data (lv, payload_json, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(lv) DO UPDATE SET
                payload_json = excluded.payload_json,
                updated_at = excluded.updated_at
            """,
            (
                lv_value,
                json.dumps(data, ensure_ascii=False),
                datetime.now().isoformat(),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def update_broadcast_data(lv_value: str, updates: dict[str, Any]) -> dict[str, Any]:
    data = load_broadcast_data(lv_value)
    data.update(updates)
    save_broadcast_data(lv_value, data)
    return data


def load_transcript_payload(lv_value: str) -> dict[str, Any]:
    conn = connect()
    if conn is None:
        return {"transcripts": []}
    try:
        rows = conn.execute(
            """
            SELECT *
            FROM archive_transcript_segments
            WHERE lv = ?
            ORDER BY start_seconds ASC, id ASC
            """,
            (lv_value,),
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    finally:
        conn.close()
    transcripts = []
    for row in rows:
        raw = {}
        try:
            raw = json.loads(str(row["raw_json"] or "{}"))
        except Exception:
            raw = {}
        # Keep the stable DB identity and segment-local timing metadata.  The
        # recording pipeline intentionally reserves one million indices per
        # media segment, so replacing it with enumerate() makes sentiment writes
        # miss every segment after the first and also makes timeline rebasing
        # non-idempotent.
        transcript = dict(raw) if isinstance(raw, dict) else {}
        transcript.update(
            {
                "db_id": int(row["id"]),
                "segment_index": int(row["segment_index"] or 0),
                "timestamp": int(row["start_seconds"] or 0),
                "start": float(row["start_seconds"] or 0.0),
                "end": float(row["end_seconds"] or 0.0),
                "start_seconds": float(row["start_seconds"] or 0.0),
                "end_seconds": float(row["end_seconds"] or 0.0),
                "text": str(row["text"] or ""),
                "speaker": str(row["speaker"] or ""),
                "center_score": float(raw.get("center_score") or 0.0),
                "positive_score": float(raw.get("positive_score") or 0.0),
                "negative_score": float(raw.get("negative_score") or 0.0),
            }
        )
        transcripts.append(transcript)
    return {
        "lv_value": lv_value,
        "total_segments": len(transcripts),
        "transcripts": transcripts,
        "source": "db",
    }


def save_transcript_sentiment_scores(lv_value: str, transcripts: list[dict[str, Any]]) -> None:
    conn = connect()
    if conn is None:
        return
    try:
        for index, segment in enumerate(transcripts):
            text = str(segment.get("text") or "").strip()
            if not text:
                continue
            start_seconds = float(segment.get("start") or segment.get("start_seconds") or segment.get("timestamp") or 0.0)
            end_seconds = float(segment.get("end") or segment.get("end_seconds") or start_seconds)
            db_id = segment.get("db_id")
            if db_id is not None:
                current = conn.execute(
                    "SELECT raw_json FROM archive_transcript_segments WHERE lv = ? AND id = ?",
                    (lv_value, int(db_id)),
                ).fetchone()
            else:
                current = conn.execute(
                    """
                    SELECT raw_json
                    FROM archive_transcript_segments
                    WHERE lv = ? AND segment_index = ? AND start_seconds = ?
                      AND end_seconds = ? AND text = ?
                    """,
                    (
                        lv_value,
                        int(segment.get("segment_index") if segment.get("segment_index") is not None else index),
                        start_seconds,
                        end_seconds,
                        text,
                    ),
                ).fetchone()
            existing_raw = {}
            if current:
                try:
                    existing_raw = json.loads(str(current["raw_json"] or "{}"))
                except Exception:
                    existing_raw = {}
            raw = dict(existing_raw) if isinstance(existing_raw, dict) else {}
            raw.update(segment)
            raw.pop("db_id", None)
            if db_id is not None:
                conn.execute(
                    """
                    UPDATE archive_transcript_segments
                    SET raw_json = ?
                    WHERE lv = ? AND id = ?
                    """,
                    (json.dumps(raw, ensure_ascii=False, default=str), lv_value, int(db_id)),
                )
            else:
                conn.execute(
                    """
                    UPDATE archive_transcript_segments
                    SET raw_json = ?
                    WHERE lv = ?
                      AND segment_index = ?
                      AND start_seconds = ?
                      AND end_seconds = ?
                      AND text = ?
                    """,
                    (
                        json.dumps(raw, ensure_ascii=False, default=str),
                        lv_value,
                        int(segment.get("segment_index") if segment.get("segment_index") is not None else index),
                        start_seconds,
                        end_seconds,
                        text,
                    ),
                )
        conn.commit()
    finally:
        conn.close()


def load_comments_payload(lv_value: str) -> dict[str, Any]:
    conn = connect()
    if conn is None:
        return empty_comments_payload(lv_value)
    try:
        rows = conn.execute(
            """
            SELECT *
            FROM archive_comments
            WHERE lv = ?
            ORDER BY broadcast_seconds ASC, no ASC, id ASC
            """,
            (lv_value,),
        ).fetchall()
    finally:
        conn.close()
    comments = [comment_row_to_legacy(row) for row in rows]
    return {
        "lv_value": lv_value,
        "total_comments": len(comments),
        "created_at": datetime.now().isoformat(),
        "comments": comments,
        "source": "db",
    }


def load_ranking_payload(lv_value: str, comments_payload: dict[str, Any] | None = None) -> dict[str, Any]:
    conn = connect()
    ranking: list[dict[str, Any]] = []
    if conn is not None:
        try:
            rows = conn.execute(
                """
                SELECT *
                FROM archive_comment_ranking
                WHERE lv = ?
                ORDER BY rank ASC, comment_count DESC, first_comment_time ASC, user_id
                """,
                (lv_value,),
            ).fetchall()
        finally:
            conn.close()
        ranking = [ranking_row_to_legacy(row) for row in rows]
    if not ranking:
        comments = (comments_payload or load_comments_payload(lv_value)).get("comments", [])
        ranking = generate_comment_ranking(comments)
    return {
        "lv_value": lv_value,
        "total_users": len(ranking),
        "created_at": datetime.now().isoformat(),
        "ranking": ranking,
        "source": "db",
    }


def empty_comments_payload(lv_value: str) -> dict[str, Any]:
    return {
        "lv_value": lv_value,
        "total_comments": 0,
        "created_at": datetime.now().isoformat(),
        "comments": [],
        "source": "db",
    }


def comment_row_to_legacy(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "no": int(row["no"] or 0),
        "user_id": str(row["user_id"] or ""),
        "user_name": str(row["user_name"] or ""),
        "text": str(row["text"] or ""),
        "date": int(row["date"] or 0),
        "broadcast_seconds": float(row["broadcast_seconds"] or 0.0),
        "timeline_block": int(row["timeline_block"] or 0),
        "premium": int(row["premium"] or 0),
        "anonymity": bool(row["anonymity"] or 0),
    }


def ranking_row_to_legacy(row: sqlite3.Row) -> dict[str, Any]:
    return {
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


def generate_comment_ranking(comments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    user_stats: dict[str, dict[str, Any]] = {}
    for comment in comments:
        user_id = str(comment.get("user_id") or "")
        if user_id not in user_stats:
            user_stats[user_id] = {
                "user_id": user_id,
                "user_name": comment.get("user_name", ""),
                "comment_count": 0,
                "first_comment": "",
                "first_comment_time": 0,
                "last_comment": "",
                "last_comment_time": 0,
                "premium": comment.get("premium", 0),
                "anonymity": comment.get("anonymity", False),
            }
        stat = user_stats[user_id]
        stat["comment_count"] += 1
        if stat["comment_count"] == 1:
            stat["first_comment"] = comment.get("text", "")
            stat["first_comment_time"] = comment.get("broadcast_seconds", 0)
        stat["last_comment"] = comment.get("text", "")
        stat["last_comment_time"] = comment.get("broadcast_seconds", 0)

    ranking = sorted(user_stats.values(), key=lambda x: x["comment_count"], reverse=True)
    for index, user in enumerate(ranking, 1):
        user["rank"] = index
    return ranking
