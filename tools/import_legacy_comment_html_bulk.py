from __future__ import annotations

import html
import json
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
for value in (ROOT, ROOT / "app"):
    if str(value) not in sys.path:
        sys.path.insert(0, str(value))

from app import tracker

SOURCE_ROOT = Path(r"J:\lab\saveniconicocomment_download\post")
CHECKPOINT = ROOT / "tmp" / "legacy_html_bulk_import.json"


def save(state: dict) -> None:
    CHECKPOINT.parent.mkdir(parents=True, exist_ok=True)
    temporary = CHECKPOINT.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(CHECKPOINT)


def program_meta(lv: str) -> dict:
    response = requests.get(f"https://live.nicovideo.jp/watch/{lv}", timeout=30)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    node = soup.select_one("#embedded-data[data-props]")
    if node is None:
        raise RuntimeError("embedded-dataがありません")
    payload = json.loads(html.unescape(str(node.get("data-props") or "{}")))
    program = payload.get("program") or {}
    supplier = program.get("supplier") or {}
    return {
        "lv": lv,
        "broadcaster_id": str(supplier.get("programProviderId") or "").strip(),
        "broadcaster_name": str(supplier.get("name") or "").strip(),
        "title": str(program.get("title") or lv).strip(),
        "begin_time": int(program.get("beginTime") or program.get("openTime") or 0),
        "end_time": int(program.get("endTime") or program.get("scheduledEndTime") or 0),
    }


def main() -> int:
    files = sorted(SOURCE_ROOT.glob("*.html"))
    state = json.loads(CHECKPOINT.read_text(encoding="utf-8")) if CHECKPOINT.is_file() else {}
    state.setdefault("imported", {})
    state.setdefault("step12_done", [])
    state.setdefault("published", [])
    state.setdefault("failures", {})
    groups: dict[str, list[str]] = defaultdict(list)
    html_by_lv: dict[str, Path] = {}
    for path in files:
        match = re.search(r"lv\d+", path.read_text(encoding="utf-8-sig", errors="ignore"))
        if match:
            html_by_lv[match.group(0)] = path
    total = len(html_by_lv)
    for index, (lv, path) in enumerate(html_by_lv.items(), start=1):
        try:
            meta = state["imported"].get(lv) or program_meta(lv)
            if not meta.get("broadcaster_id") or not meta.get("begin_time") or not meta.get("end_time"):
                raise RuntimeError(f"放送メタ情報不足: {meta}")
            if lv not in state["imported"]:
                command = [
                    sys.executable, str(ROOT / "tools" / "import_legacy_comment_html.py"), str(path),
                    "--lv", lv,
                    "--broadcaster-id", meta["broadcaster_id"],
                    "--broadcaster-name", meta["broadcaster_name"],
                    "--title", meta["title"],
                    "--begin-time", str(meta["begin_time"]),
                    "--end-time", str(meta["end_time"]),
                ]
                subprocess.run(command, cwd=ROOT, check=True, stdout=subprocess.DEVNULL)
                state["imported"][lv] = meta
                save(state)
            groups[meta["broadcaster_id"]].append(lv)
            if lv not in state["step12_done"]:
                tracker.run_legacy_archiver_steps(
                    lv, account_id=meta["broadcaster_id"], steps=["step12_html_generator"],
                    force_overwrite_existing_html=True,
                )
                state["step12_done"].append(lv)
                save(state)
            print(f"[{index}/{total}] import+step12 {lv} broadcaster={meta['broadcaster_id']}", flush=True)
        except Exception as exc:
            state["failures"][lv] = f"{type(exc).__name__}: {exc}"
            save(state)
            print(f"[{index}/{total}] FAILED {lv}: {exc}", flush=True)

    for broadcaster_id, lvs in groups.items():
        pending = [lv for lv in lvs if lv not in state["published"]]
        if not pending:
            continue
        anchor = lvs[-1]
        tracker.run_legacy_archiver_steps(
            anchor, account_id=broadcaster_id,
            steps=["step13_index_generator", "step14_modern_list_generator", "step15_lolipop_uploader"],
            upload_html_only=True,
        )
        if anchor not in state["published"]:
            state["published"].append(anchor)
        save(state)
        for lv in pending:
            if lv == anchor:
                continue
            tracker.run_legacy_archiver_steps(
                lv, account_id=broadcaster_id, steps=["step15_lolipop_uploader"],
                upload_html_only=True,
            )
            state["published"].append(lv)
            save(state)
            print(f"publish {lv} broadcaster={broadcaster_id}", flush=True)
    print(json.dumps({
        "files": total,
        "imported": len(state["imported"]),
        "step12_done": len(state["step12_done"]),
        "published": len(state["published"]),
        "failures": state["failures"],
    }, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
