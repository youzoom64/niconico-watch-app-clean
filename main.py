from __future__ import annotations

import argparse
import importlib
import os
import sys
from collections.abc import Callable, Sequence
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parent
APP_ROLE_ENV = "NICONICO_WATCH_APP_ROLE"


def ensure_import_path() -> None:
    for path in (APP_ROOT, APP_ROOT / "app"):
        value = str(path)
        if value not in sys.path:
            sys.path.insert(0, value)


def run_gui() -> int:
    os.environ[APP_ROLE_ENV] = "monitor"
    ensure_import_path()
    module = importlib.import_module("app.gui_app")
    return int(module.main())


def run_timeshift_gui(
    input_urls: Sequence[str] = (),
    input_files: Sequence[str] = (),
    tag_url: str = "",
) -> int:
    os.environ[APP_ROLE_ENV] = "timeshift"
    ensure_import_path()
    module = importlib.import_module("app.timeshift_app")
    return int(
        module.main(
            initial_urls=list(input_urls),
            initial_files=list(input_files),
            initial_tag_url=tag_url,
        )
    )


def run_tracker(args: Sequence[str]) -> int:
    ensure_import_path()
    module = importlib.import_module("app.tracker")
    original_argv = sys.argv[:]
    try:
        sys.argv = [str(APP_ROOT / "app" / "tracker.py"), *args]
        result = module.main()
        return int(result or 0)
    finally:
        sys.argv = original_argv


def run_api(args: Sequence[str]) -> int:
    ensure_import_path()
    module = importlib.import_module("app.api.intervention_server")
    return int(module.run(list(args)))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="niconico-watch-app entrypoint")
    subparsers = parser.add_subparsers(dest="command")

    gui_parser = subparsers.add_parser("gui", help="PyQt6 GUIを起動する")
    gui_parser.set_defaults(handler=lambda _args: run_gui())

    timeshift_parser = subparsers.add_parser(
        "timeshift",
        help="タイムシフト専用GUIを別プロセスで起動する",
    )
    timeshift_parser.add_argument(
        "--input-url",
        action="append",
        default=[],
        help="専用GUIのURL入力欄へ追加するURL。複数指定可",
    )
    timeshift_parser.add_argument(
        "--input-file",
        action="append",
        default=[],
        help="ローカル処理へ追加する動画ファイル。複数指定可",
    )
    timeshift_parser.add_argument("--tag-url", default="")
    timeshift_parser.set_defaults(
        handler=lambda args: run_timeshift_gui(args.input_url, args.input_file, args.tag_url)
    )

    tracker_parser = subparsers.add_parser("tracker", help="トラッカーCLIを起動する")
    tracker_parser.add_argument("--once", action="store_true", help="1回だけ取得して終了する")
    tracker_parser.set_defaults(handler=lambda args: run_tracker(["--once"] if args.once else []))

    api_parser = subparsers.add_parser("api", help="ローカル介入APIを起動する")
    api_parser.add_argument("--host", default="127.0.0.1")
    api_parser.add_argument("--port", default="8794")
    api_parser.set_defaults(handler=lambda args: run_api(["--host", args.host, "--port", args.port]))

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler: Callable[[argparse.Namespace], int] | None = getattr(args, "handler", None)
    if handler is None:
        return run_gui()
    return handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
