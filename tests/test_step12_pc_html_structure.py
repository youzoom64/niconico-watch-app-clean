from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LEGACY_ROOT = ROOT / "legacy_archiver"
if str(LEGACY_ROOT) not in sys.path:
    sys.path.insert(0, str(LEGACY_ROOT))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "app") not in sys.path:
    sys.path.insert(0, str(ROOT / "app"))

from processors import step12_html_generator as step12


def test_comment_timeline_marks_failed_comment_acquisition():
    failed = step12.render_comment_time_block(0, {}, comments_fetch_failed=True)
    empty = step12.render_comment_time_block(0, {}, comments_fetch_failed=False)

    assert "コメント取得失敗のため未収録" in failed
    assert "コメントなし" in empty


def test_pc_generator_matches_current_published_layout(tmp_path):
    lv_value = "lv350973849"
    title = "祭"
    broadcast = {
        "lv_value": lv_value,
        "live_title": title,
        "broadcaster": "yosino",
        "start_time": 1_700_000_000,
        "end_time": 1_700_000_900,
        "watch_count": "12",
        "comment_count": "34",
        "elapsed_time": "00:15:00",
        "video_duration": 900,
        "owner_id": "39532023",
        "summary_text": "テスト要約",
        "sentiment_stats": {
            "avg_positive": 0.233,
            "avg_center": 0.436,
            "avg_negative": 0.331,
        },
        "image_generation": {"imgur_url": "https://example.com/summary.png"},
    }
    timeline = {
        "transcript_blocks": [],
        "comment_blocks": [],
        "recording_segment_timeline": {},
    }
    ai_chats = {
        "intro": [
            {
                "name": "ニニちゃん",
                "icon": "https://example.com/nini.png",
                "dialogue": "開始前会話",
                "flip": False,
            }
        ],
        "outro": [],
    }
    config = {
        "display_features": {
            "enable_word_ranking": False,
            "enable_comment_ranking": False,
            "enable_timeline_html": False,
            "enable_audio_timeline": False,
            "thumbnail_width": 150,
            "thumbnail_height": 80,
        },
        "ai_prompts": {
            "character1_name": "ニニちゃん",
            "character2_name": "ココちゃん",
        },
    }

    document = step12.generate_complete_html(
        timeline,
        broadcast,
        [],
        [],
        ai_chats,
        config,
        lv_value,
        str(tmp_path),
    )

    assert "family=Zen+Antique" in document
    assert ".header { background: transparent;" in document
    assert ".chat-container {\n            margin: 20px auto; \n            max-width: 1200px;" in document
    assert "max-width: 1000px;" in document
    assert ".summary-image img { width: 100%; max-width: 1000px; height: auto;" in document
    assert "#timeline1 .transcript-comment {\n            flex: 1 1 auto;" in document
    assert "max-height: none;\n            overflow-y: auto;\n            position: relative;\n            z-index: 2;\n            background: transparent;" in document
    assert "#timeline1 .time-block {\n            display: flex;\n            flex-direction: column;" in document
    assert ".transcript-lines {" in document
    assert "#timeline1 .transcript-lines {\n            position: relative;\n            z-index: 2;" in document
    assert "#timeline2 .time-block .comment-list {" in document
    assert "overflow: hidden;" in document
    assert "bottom: 22px;" in document
    assert "right: 32px;" in document
    assert "width: 150px;" in document
    assert "height: 80px;" in document
    assert "box-sizing: border-box;" in document
    assert '<label for="thumbnailSizeRange">サムネサイズ:</label>' in document
    assert 'id="thumbnailSizeRange" type="range" min="50" max="400" step="5" value="150"' in document
    assert '<span id="thumbnailSizeStatus">150 × 80px</span>' in document
    assert 'document.querySelectorAll("#timeline1 .img_container")' in document
    assert "thumbnail.style.width = width + \"px\";" in document
    assert "thumbnail.style.height = height + \"px\";" in document
    assert "コメント秒" not in document
    assert "commentOffset" not in document
    assert "nico-comment-offset-state" not in document
    assert "archive-comment-offset" not in document
    assert "const targetHeight = manualBlockHeight || 180;" in document
    assert "block.scrollHeight" not in document
    assert "measureTimeline1Block" not in document

    header_start = document.index('<div class="header">')
    header_end = document.index("</div>", document.index("</a>", header_start)) + len("</div>")
    header = document[header_start:header_end]
    assert "<strong>配信時間:</strong>" not in header
    assert '<a class="broadcast-link" href="https://live.nicovideo.jp/watch/lv350973849"' in header
    assert '<div class="broadcast-lv">lv350973849</div>' in header
    assert '<h1 class="broadcast-title">祭</h1>' in header
    assert ".broadcast-link { display: block; }" in document
    assert "text-decoration: none" not in document

    image_position = document.index('<div class="summary-image">')
    intro_position = document.index('<div class="section ai-chat-section">')
    summary_position = document.index('<div class="summary-section">')
    assert image_position < intro_position < summary_position
    assert document.count('class="summary-image"') == 1
    assert "要約を元に生成した画像" not in document

    emotion_card = document[document.index('<div class="emotion-chart-card">') :]
    assert emotion_card.index('id="emotion-graph-inner"') < emotion_card.index(
        "<p><strong>感情分析:</strong>"
    )
