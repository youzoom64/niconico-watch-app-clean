from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
for value in (ROOT, ROOT / "app"):
    if str(value) not in sys.path:
        sys.path.insert(0, str(value))

from app import tracker


def seconds_from_block(block: Any) -> float:
    match = re.search(r"time_block_(\d+)", str(block.get("id") or ""))
    return float(match.group(1)) if match else 0.0


def parse_transcripts(timeline: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index, block in enumerate(timeline.select(":scope > .time-block")):
        comment = block.select_one(".comment")
        text = comment.get_text("\n", strip=True) if comment else ""
        if not text:
            continue
        start = seconds_from_block(block)
        row: dict[str, Any] = {
            "segment_index": index,
            "start": start,
            "end": start + 10.0,
            "start_seconds": start,
            "end_seconds": start + 10.0,
            "text": text,
            "speaker": "",
            "source": "legacy_comment_html",
        }
        for key in ("center", "positive", "negative"):
            node = block.select_one(f".{key}-score")
            match = re.search(r"-?\d+(?:\.\d+)?", node.get_text(" ", strip=True) if node else "")
            row[f"{key}_score"] = float(match.group(0)) if match else 0.0
        rows.append(row)
    return rows


def legacy_user_id(paragraph: Any, no: int) -> tuple[str, str]:
    link = paragraph.select_one('a[href*="nicovideo.jp/user/"]')
    if link:
        match = re.search(r"/user/([^/?#]+)", str(link.get("href") or ""))
        if match:
            return match.group(1), link.get_text(" ", strip=True)
    image = paragraph.select_one("img[src]")
    if image:
        stem = Path(str(image.get("src") or "").split("?", 1)[0]).stem
        if stem and stem != "blank":
            return f"legacy-{stem}", ""
    return f"legacy-comment-{no}", ""


def parse_comments(timeline: Any, begin_time: int) -> list[dict[str, Any]]:
    comments: list[dict[str, Any]] = []
    pattern = re.compile(r"^\s*(\d+)\s*\|\s*(\d{2}):(\d{2}):(\d{2})\s*-\s*(.*?)\s*:\s*(.*)$", re.S)
    for block in timeline.select(":scope > .time-block"):
        for paragraph in block.find_all("p", recursive=False):
            text = paragraph.get_text(" ", strip=True)
            match = pattern.match(text)
            if not match:
                continue
            no = int(match.group(1))
            second = int(match.group(2)) * 3600 + int(match.group(3)) * 60 + int(match.group(4))
            displayed_name = match.group(5).strip()
            body = match.group(6).strip()
            user_id, linked_name = legacy_user_id(paragraph, no)
            comments.append({
                "no": no,
                "comment_id": f"legacy-{no}",
                "user_id": user_id,
                "raw_user_id": user_id,
                "user_name": linked_name or displayed_name,
                "text": body,
                "date": begin_time + second,
                "vpos": second * 100,
                "source": "legacy_comment_html",
                "received_at": "",
            })
    return comments


def main() -> int:
    parser = argparse.ArgumentParser(description="旧コメントHTMLを現在のアーカイブDBへ変換")
    parser.add_argument("html", type=Path)
    parser.add_argument("--lv", required=True)
    parser.add_argument("--broadcaster-id", required=True)
    parser.add_argument("--broadcaster-name", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--begin-time", type=int, required=True)
    parser.add_argument("--end-time", type=int, required=True)
    args = parser.parse_args()

    soup = BeautifulSoup(args.html.read_text(encoding="utf-8-sig"), "html.parser")
    timelines = soup.select(".timeline")
    if len(timelines) < 2:
        raise RuntimeError("旧HTML内に2本のタイムラインがありません")
    transcripts = parse_transcripts(timelines[0])
    comments = parse_comments(timelines[1], args.begin_time)
    lv = args.lv.strip()
    config = tracker.load_config()
    account_dir = tracker.niconico_platform_target_root(config) / args.broadcaster_id / "broadcast"
    target_dir = account_dir / lv
    target_dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "lv": lv,
        "watch_url": f"https://live.nicovideo.jp/watch/{lv}",
        "title": args.title,
        "broadcaster_id": args.broadcaster_id,
        "broadcaster_name": args.broadcaster_name,
        "begin_time": args.begin_time,
        "open_time": args.begin_time,
        "start_time": args.begin_time,
        "end_time": args.end_time,
        "fetched_at": tracker.now_micro(),
        "html_path": "",
        "legacy_source_html": str(args.html.resolve()),
    }
    payload = {
        "lv_value": lv,
        "live_num": lv.removeprefix("lv"),
        "live_title": args.title,
        "broadcaster": args.broadcaster_name,
        "owner_id": args.broadcaster_id,
        "owner_name": args.broadcaster_name,
        "begin_time": args.begin_time,
        "open_time": args.begin_time,
        "start_time": args.begin_time,
        "end_time": args.end_time,
        "watch_count": 17,
        "comment_count": len(comments),
        "elapsed_time": str(datetime.utcfromtimestamp(args.end_time - args.begin_time).strftime("%H:%M:%S")),
        "broadcast_directory_path": str(target_dir),
        "legacy_source_html": str(args.html.resolve()),
    }
    with tracker.connect() as conn:
        tracker.save_broadcast_archive_meta(conn, meta)
        conn.execute("DELETE FROM archive_transcript_segments WHERE lv = ?", (lv,))
        tracker.save_transcript_segments(conn, lv, transcripts, model="legacy_html")
        conn.execute("DELETE FROM archive_comments WHERE lv = ? AND source = ?", (lv, "legacy_comment_html"))
        for comment in comments:
            tracker.save_archive_comment_from_ndgr(conn, lv, comment, start_time=args.begin_time)
        conn.execute(
            "INSERT INTO archive_broadcast_data (lv, payload_json, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(lv) DO UPDATE SET payload_json=excluded.payload_json, updated_at=excluded.updated_at",
            (lv, json.dumps(payload, ensure_ascii=False), tracker.now_micro()),
        )
        conn.commit()
    (target_dir / f"{lv}_transcript.json").write_text(
        json.dumps({"lv_value": lv, "total_segments": len(transcripts), "transcripts": transcripts, "source": "legacy_html"}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (target_dir / f"{lv}_comments.json").write_text(
        json.dumps({"lv_value": lv, "total_comments": len(comments), "comments": comments, "source": "legacy_html"}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps({"lv": lv, "broadcaster_id": args.broadcaster_id, "transcripts": len(transcripts), "comments": len(comments), "target_dir": str(target_dir)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
