from __future__ import annotations

import base64
import io
import os
import sys
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import requests
from openai import OpenAI
from PIL import Image, UnidentifiedImageError


LEGACY_ARCHIVER_DIR = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
for import_path in (LEGACY_ARCHIVER_DIR, REPOSITORY_ROOT / "app"):
    if str(import_path) not in sys.path:
        sys.path.append(str(import_path))

from archive_db import load_broadcast_data as load_broadcast_data_from_db, update_broadcast_data
def process(pipeline_data):
    """Step07: 設定されたOpenAI画像モデルをImage APIで直接呼び出す。"""
    try:
        lv_value = pipeline_data["lv_value"]
        config = pipeline_data["config"]

        print(f"Step07 開始: {lv_value}")

        if not config["ai_features"].get("enable_summary_image", False):
            print("AI画像生成機能が無効です。処理をスキップします。")
            return {"image_generated": False, "reason": "feature_disabled"}

        broadcast_data = load_broadcast_data(lv_value)
        summary_text = str(broadcast_data.get("summary_text", "") or "")
        if not summary_text.strip():
            print("要約テキストが見つかりません。画像生成をスキップします。")
            return {"image_generated": False, "reason": "no_summary"}

        openai_api_key = config["api_settings"].get("openai_api_key", "")
        if not openai_api_key:
            raise RuntimeError("OpenAI API Keyが設定されていないため、要約画像を生成できません")

        imgur_api_key = config["api_settings"].get("imgur_api_key", "")
        if not imgur_api_key:
            raise RuntimeError("Imgur API Keyが設定されていないため、要約画像をHTML用URLとして保存できません")

        image_result = generate_image_from_summary(
            lv_value=lv_value,
            title=broadcast_data.get("live_title", "タイトル不明"),
            summary=summary_text,
            openai_api_key=openai_api_key,
            imgur_api_key=imgur_api_key,
            config=config,
            broadcast_data=broadcast_data,
        )

        save_broadcast_data(lv_value, {"image_generation": image_result})
        print(
            f"Step07 完了: {lv_value} - OpenAI Image API要約画像生成成功 "
            f"model={image_result['model']} url={image_result['imgur_url']}"
        )
        return {
            "image_generated": True,
            "image_url": image_result["imgur_url"],
            "local_path": image_result["local_path"],
        }

    except Exception as e:
        print(f"Step07 エラー: {str(e)}")
        raise


def load_broadcast_data(lv_value):
    broadcast_data = load_broadcast_data_from_db(lv_value)
    if broadcast_data:
        return broadcast_data
    raise RuntimeError(f"放送データDBが見つかりません: {lv_value}")


def save_broadcast_data(lv_value, updates):
    update_broadcast_data(lv_value, updates)


def generate_image_from_summary(
    *,
    lv_value,
    title,
    summary,
    openai_api_key,
    imgur_api_key,
    config,
    broadcast_data,
):
    """OpenAI Image APIで画像を生成し、検証済みPNGを保存してImgurへアップロードする。"""
    print(f"[INFO] OpenAI Image API要約画像生成開始: lv={lv_value} title={title}")
    print(f"[DEBUG] 要約文字数={len(summary)} 要約先頭={summary[:100]}...")

    image_settings = config.get("image_settings", {})
    model = str(image_settings.get("model") or "gpt-image-2")
    size = str(image_settings.get("size") or "1024x1024")
    quality = str(image_settings.get("quality") or "auto")
    broadcast_dir = resolve_broadcast_directory(broadcast_data)
    final_path = broadcast_dir / f"{lv_value}_summary.png"
    image_prompt = create_image_prompt(title, summary, config)
    print(f"[INFO] OpenAI Image API呼び出し: model={model} size={size} quality={quality}")
    image_data = generate_openai_image(
        image_prompt,
        openai_api_key,
        model=model,
        size=size,
        quality=quality,
    )
    image_data, width, height = validate_generated_image_data(image_data)
    save_local_image(image_data, final_path)
    print(
        f"[INFO] OpenAI画像検証・保存完了: model={model} size={width}x{height} "
        f"bytes={len(image_data)} path={final_path}"
    )

    print("[INFO] OpenAI要約画像のImgurアップロード開始")
    imgur_url = upload_to_imgur(image_data, imgur_api_key, title)
    if not imgur_url:
        raise RuntimeError(f"Imgurへの要約画像アップロードに失敗しました。ローカル画像は保持します: {final_path}")

    return {
        # 後段との互換性のため、既存キーは残す。
        "dalle_url": f"openai:{model}:b64_json",
        "imgur_url": imgur_url,
        "dalle_prompt": image_prompt,
        "image_prompt": image_prompt,
        "generated_at": datetime.now().isoformat(),
        "title": title,
        "model": model,
        "size": size,
        "quality": quality,
        "output_format": "png",
        "prompt_engine": "openai_image_api",
        "generator": "images.generate",
        "local_path": str(final_path.resolve()),
        "width": width,
        "height": height,
    }


def resolve_broadcast_directory(broadcast_data):
    raw_path = str(broadcast_data.get("broadcast_directory_path") or "").strip()
    if not raw_path:
        recording_path = str(broadcast_data.get("recording_file_path") or "").strip()
        if recording_path:
            raw_path = str(Path(recording_path).parent)
    if not raw_path:
        raise RuntimeError("要約画像の保存先 broadcast_directory_path が放送データにありません")
    broadcast_dir = Path(raw_path).resolve()
    broadcast_dir.mkdir(parents=True, exist_ok=True)
    return broadcast_dir


def create_image_prompt(title, summary, config):
    base_prompt = config.get("ai_prompts", {}).get(
        "image_prompt",
        "次の文章は、ある生放送の要約です。この生放送の抽象的なイメージを生成してください:",
    )
    return f"{base_prompt}\n\n配信タイトル: {title}\n要約: {summary}".strip()


def generate_openai_image(prompt, api_key, *, model, size, quality):
    client = OpenAI(api_key=api_key)
    response = client.images.generate(
        model=model,
        prompt=prompt,
        size=size,
        quality=quality,
        output_format="png",
        n=1,
    )
    if not response.data:
        raise RuntimeError(f"OpenAI Image APIが画像を返しませんでした: model={model}")
    image_b64 = getattr(response.data[0], "b64_json", None)
    if not image_b64:
        raise RuntimeError(f"OpenAI Image APIレスポンスにb64_jsonがありません: model={model}")
    try:
        return base64.b64decode(image_b64, validate=True)
    except (ValueError, TypeError) as error:
        raise RuntimeError(f"OpenAI Image APIのb64_jsonを復号できません: model={model}") from error


def validate_generated_image_data(image_data):
    if not image_data:
        raise RuntimeError("OpenAI Image APIが空の画像データを返しました")
    try:
        with Image.open(io.BytesIO(image_data)) as image:
            image_format = str(image.format or "").upper()
            width, height = image.size
            image.verify()
    except (UnidentifiedImageError, OSError, ValueError) as error:
        raise RuntimeError(f"OpenAI Image APIの生成物は有効な画像ではありません: {error}") from error
    if image_format != "PNG":
        raise RuntimeError(f"OpenAI Image APIの生成物がPNGではありません: format={image_format}")
    if width <= 0 or height <= 0:
        raise RuntimeError(f"OpenAI Image APIの生成画像サイズが不正です: {width}x{height}")
    return image_data, width, height


def save_local_image(image_data, final_path):
    final_path = Path(final_path)
    final_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = final_path.with_name(f".{final_path.stem}_{uuid4().hex}.tmp.png")
    try:
        temporary_path.write_bytes(image_data)
        os.replace(temporary_path, final_path)
    finally:
        temporary_path.unlink(missing_ok=True)


def upload_to_imgur(image_data, api_key, title):
    try:
        headers = {"Authorization": f"Client-ID {api_key}"}
        image_b64 = base64.b64encode(image_data).decode("utf-8")
        data = {
            "image": image_b64,
            "type": "base64",
            "title": title,
            "description": f"AI generated image for broadcast: {title}",
        }
        response = requests.post("https://api.imgur.com/3/image", headers=headers, data=data, timeout=30)
        if response.status_code == 200:
            result = response.json()
            if result["success"]:
                imgur_url = result["data"]["link"]
                print(f"Imgur アップロード成功: {imgur_url}")
                return imgur_url
            print(f"Imgur アップロード失敗: {result}")
            return None
        print(f"Imgur API エラー {response.status_code}: {response.text}")
        return None
    except Exception as e:
        print(f"Imgur アップロードエラー: {str(e)}")
        return None
