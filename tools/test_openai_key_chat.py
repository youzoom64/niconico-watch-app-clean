from __future__ import annotations

import argparse
import json
from pathlib import Path

from openai import APIStatusError, OpenAI


DEFAULT_ENV_FILE = Path(__file__).resolve().parents[1] / ".env"


def talk_to_openai(api_key: str, *, model: str = "gpt-5.6") -> str:
    """渡されたAPIキーを直接使い、OpenAIのテキストモデルへ1回問い合わせる。"""
    client = OpenAI(api_key=api_key)
    response = client.responses.create(
        model=model,
        input="API接続テストです。『接続成功』とだけ返してください。",
    )
    return str(response.output_text or "").strip()


def read_env_value(path: Path, name: str) -> str:
    value = ""
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, candidate = line.split("=", 1)
        if key.strip() == name:
            value = candidate.strip().strip('"').strip("'")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description="OpenAI APIキーの生接続テスト")
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--model", default="gpt-5.6")
    args = parser.parse_args()

    api_key = read_env_value(args.env_file, "OPENAI_API_KEY")
    if not api_key:
        print(json.dumps({"ok": False, "error": "OPENAI_API_KEY not found"}))
        return 2

    try:
        reply = talk_to_openai(api_key, model=args.model)
    except APIStatusError as error:
        body = error.body if isinstance(error.body, dict) else {}
        detail = body.get("error") if isinstance(body.get("error"), dict) else body
        print(
            json.dumps(
                {
                    "ok": False,
                    "model": args.model,
                    "http_status": error.status_code,
                    "error_type": detail.get("type", type(error).__name__),
                    "error_code": detail.get("code"),
                    "request_id": error.request_id,
                },
                ensure_ascii=False,
            )
        )
        return 1

    print(json.dumps({"ok": True, "model": args.model, "reply": reply}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
