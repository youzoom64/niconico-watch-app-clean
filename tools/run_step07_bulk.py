from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "app"
LEGACY_DIR = ROOT / "legacy_archiver"
for import_path in (APP_DIR, LEGACY_DIR):
    if str(import_path) not in sys.path:
        sys.path.insert(0, str(import_path))

from archive_db import load_broadcast_data
from tracker import run_legacy_archiver_steps


DEFAULT_BROADCAST_ROOT = ROOT.parent / "target" / "platform" / "niconico"


def emit(event: str, **fields: object) -> None:
    payload = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "event": event,
        **fields,
    }
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="要約済み・画像未生成の放送へStep07だけを順次実行します。"
    )
    parser.add_argument("--broadcast-root", type=Path, default=DEFAULT_BROADCAST_ROOT)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=0, help="0なら件数制限なし")
    parser.add_argument(
        "--retry-existing",
        action="store_true",
        help="image_generationがある放送も再生成します。",
    )
    parser.add_argument(
        "--skip-html-refresh",
        action="store_true",
        help="画像生成後のStep12/Step13/Step14再生成を省略します。",
    )
    return parser.parse_args()


def direct_api_image_lvs(broadcast_root: Path) -> list[str]:
    """直接Image APIで生成済みの放送を、詳細HTML再生成対象として返す。"""
    lvs: list[str] = []
    for directory in sorted(broadcast_root.glob("lv*")):
        if not directory.is_dir():
            continue
        broadcast = load_broadcast_data(directory.name)
        image_generation = (broadcast or {}).get("image_generation")
        if (
            isinstance(image_generation, dict)
            and image_generation.get("prompt_engine") == "openai_image_api"
        ):
            lvs.append(directory.name)
    return lvs


def refresh_generated_html(broadcast_root: Path) -> tuple[int, int]:
    """個別HTML、タグページ、最終一覧を現在のDB内容から作り直す。"""
    account_id = broadcast_root.parent.name
    refresh_lvs = direct_api_image_lvs(broadcast_root)
    emit("html_refresh_plan", account_id=account_id, broadcasts=len(refresh_lvs))

    succeeded = 0
    failed = 0
    for index, lv in enumerate(refresh_lvs, start=1):
        emit("html_refresh_start", lv=lv, index=index, total=len(refresh_lvs))
        try:
            run_legacy_archiver_steps(
                lv,
                account_id=account_id,
                steps=["step12_html_generator"],
            )
            succeeded += 1
            emit("html_refresh_success", lv=lv)
        except Exception as error:
            failed += 1
            emit(
                "html_refresh_failure",
                lv=lv,
                error=f"{type(error).__name__}: {error}",
            )

    if refresh_lvs:
        anchor_lv = refresh_lvs[0]
        for step_name in ("step13_index_generator", "step14_modern_list_generator"):
            emit("list_refresh_start", step=step_name, anchor_lv=anchor_lv)
            try:
                result = run_legacy_archiver_steps(
                    anchor_lv,
                    account_id=account_id,
                    steps=[step_name],
                )
                step = result.get("steps", {}).get(step_name, {})
                if step.get("status") != "done":
                    raise RuntimeError(f"unexpected {step_name} result: {step}")
                emit("list_refresh_success", step=step_name, result=step.get("result", {}))
            except Exception as error:
                failed += 1
                emit(
                    "list_refresh_failure",
                    step=step_name,
                    error=f"{type(error).__name__}: {error}",
                )

    return succeeded, failed


def main() -> int:
    args = parse_args()
    broadcast_root = args.broadcast_root.resolve()
    if not broadcast_root.is_dir():
        emit("fatal", reason="broadcast_root_not_found", path=str(broadcast_root))
        return 2

    candidates: list[str] = []
    skipped: dict[str, int] = {"no_db": 0, "no_summary": 0, "existing_image": 0}
    for directory in sorted(broadcast_root.glob("lv*")):
        if not directory.is_dir():
            continue
        lv = directory.name
        broadcast = load_broadcast_data(lv)
        if not broadcast:
            skipped["no_db"] += 1
            emit("skip", lv=lv, reason="no_db")
            continue
        if not str(broadcast.get("summary_text") or "").strip():
            skipped["no_summary"] += 1
            emit("skip", lv=lv, reason="no_summary")
            continue
        image_generation = broadcast.get("image_generation")
        generated_by_direct_api = (
            isinstance(image_generation, dict)
            and image_generation.get("prompt_engine") == "openai_image_api"
        )
        if generated_by_direct_api and not args.retry_existing:
            skipped["existing_image"] += 1
            emit("skip", lv=lv, reason="existing_direct_api_image")
            continue
        candidates.append(lv)

    if args.limit > 0:
        candidates = candidates[: args.limit]
    emit(
        "plan",
        root=str(broadcast_root),
        candidates=len(candidates),
        skipped=skipped,
        refresh_html=not args.skip_html_refresh,
    )
    if args.dry_run:
        for lv in candidates:
            emit("candidate", lv=lv)
        if not args.skip_html_refresh:
            emit("html_refresh_plan", broadcasts=len(direct_api_image_lvs(broadcast_root)))
        return 0

    succeeded = 0
    failed = 0
    for index, lv in enumerate(candidates, start=1):
        emit("start", lv=lv, index=index, total=len(candidates))
        try:
            result = run_legacy_archiver_steps(lv, steps=["step07_image_generator"])
            step = result.get("steps", {}).get("step07_image_generator", {})
            if step.get("status") != "done" or not step.get("result", {}).get("image_generated"):
                raise RuntimeError(f"unexpected Step07 result: {step}")
            succeeded += 1
            emit(
                "success",
                lv=lv,
                image_url=step["result"].get("image_url", ""),
                local_path=step["result"].get("local_path", ""),
            )
        except Exception as error:
            failed += 1
            emit("failure", lv=lv, error=f"{type(error).__name__}: {error}")

    html_refreshed = 0
    if not args.skip_html_refresh:
        html_refreshed, refresh_failed = refresh_generated_html(broadcast_root)
        failed += refresh_failed

    emit(
        "complete",
        candidates=len(candidates),
        succeeded=succeeded,
        html_refreshed=html_refreshed,
        failed=failed,
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
