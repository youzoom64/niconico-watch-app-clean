from __future__ import annotations

from pathlib import Path

from legacy_archiver.processors import step13_index_generator as step13
from legacy_archiver.processors.html_preservation import read_page_tags_file
import json


def _broadcast(*, broadcaster: str = "yosino", html_file: str = "lv1/lv1.html") -> dict:
    return {
        "lv_value": "lv1",
        "title": "テスト配信",
        "broadcaster": broadcaster,
        "start_time": 1,
        "watch_count": 10,
        "comment_count": 2,
        "elapsed_time": "00:30:00",
        "summary_text": "要約",
        "html_file": html_file,
        "image_url": "https://i.imgur.com/example.png",
        "music_urls": [],
        "transcript_segments": ["本文"],
        "tag_search_text": "",
        "tags": [],
    }


def test_broadcaster_becomes_tag_when_configured_tags_are_empty() -> None:
    broadcasts = [_broadcast()]

    effective_tags = step13.apply_broadcaster_fallback_tags(broadcasts, {})

    assert effective_tags == ["yosino"]
    assert broadcasts[0]["tags"] == ["yosino"]


def test_tag_page_lists_broadcast_urls_relative_to_tags_directory(tmp_path: Path) -> None:
    broadcasts = [_broadcast()]
    step13.apply_broadcaster_fallback_tags(broadcasts, [])

    generated = step13.generate_tag_pages(
        str(tmp_path),
        broadcasts,
        ["yosino"],
        {"tags": ["yosino"]},
    )

    tag_page = tmp_path / "tags" / "tag_yosino.html"
    assert generated == [str(tag_page)]
    assert tag_page.is_file()
    html = tag_page.read_text(encoding="utf-8")
    assert '#yosino の配信一覧' in html
    assert '"url": "../lv1/lv1.html"' in html
    assert '--bg:#111015' in html
    assert '.post{grid-template-columns:64px 1fr;gap:10px;padding:7px 0}' in html
    assert 'href="../index.html"' in html
    assert 'href="${tagPagePrefix}tag_${encodeURIComponent(t)}.html"' in html


def test_manual_person_tags_are_loaded_and_generate_tag_pages(tmp_path: Path) -> None:
    (tmp_path / step13.MANUAL_TAGS_FILENAME).write_text(
        json.dumps({"lv1": ["ハムちゃん", "ハムちゃん", ""]}, ensure_ascii=False),
        encoding="utf-8",
    )
    broadcasts = [_broadcast()]

    manual_tags = step13.load_manual_tags(str(tmp_path))
    step13.apply_manual_tags(broadcasts, manual_tags)
    effective_tags = step13.apply_broadcaster_fallback_tags(broadcasts, [])
    generated = step13.generate_tag_pages(
        str(tmp_path), broadcasts, effective_tags, {"tags": effective_tags}
    )

    assert broadcasts[0]["tags"] == ["ハムちゃん", "yosino"]
    assert effective_tags == ["ハムちゃん", "yosino"]
    assert str(tmp_path / "tags" / "tag_ハムちゃん.html") in generated


def test_manual_exclude_overrides_automatic_tag_detection(tmp_path: Path) -> None:
    (tmp_path / step13.MANUAL_TAGS_FILENAME).write_text(
        json.dumps({"_exclude": {"lv1": ["ガルル"]}}, ensure_ascii=False),
        encoding="utf-8",
    )
    broadcasts = [_broadcast()]
    broadcasts[0]["tags"] = ["ガルル", "yosino"]

    excludes = step13.load_manual_tag_excludes(str(tmp_path))
    step13.apply_manual_tag_excludes(broadcasts, excludes)

    assert broadcasts[0]["tags"] == ["yosino"]


def test_known_person_name_in_full_transcript_becomes_tag() -> None:
    broadcasts = [
        {
            **_broadcast(html_file="lv2/lv2.html"),
            "lv_value": "lv2",
            "transcript_segments": ["冒頭10件には名前なし"],
            "tag_search_text": "ずっと後半でハムちゃんについて話した",
        }
    ]
    manual_tags = {"lv1": ["ハムちゃん"]}

    candidates = step13.collect_manual_tag_names(manual_tags)
    step13.process_tags(broadcasts, candidates)

    assert candidates == ["ハムちゃん"]
    assert broadcasts[0]["tags"] == ["ハムちゃん"]


def test_short_person_name_does_not_match_inside_general_word() -> None:
    broadcasts = [
        {
            **_broadcast(),
            "tag_search_text": "ゆっくり体温を下げていくみたいな感じ",
        }
    ]
    step13.process_tags(broadcasts, ["くみ"])
    assert broadcasts[0]["tags"] == []

    broadcasts[0]["tag_search_text"] = "もうくみはうるさいし"
    step13.process_tags(broadcasts, ["くみ"])
    assert broadcasts[0]["tags"] == ["くみ"]


def test_existing_page_tags_survive_without_manual_json_and_sync_to_pc_mobile(tmp_path: Path) -> None:
    detail_dir = tmp_path / "lv1"
    detail_dir.mkdir()
    pc = detail_dir / "lv1.html"
    mobile = detail_dir / "lv1_mobile.html"
    pc.write_text('<html><body><div id="pc">PC</div></body></html>', encoding="utf-8")
    mobile.write_text('<html><body><div id="mobile">MOBILE</div></body></html>', encoding="utf-8")
    first, _ = step13.update_page_tags_file(pc, ["既存人物"])
    assert first is True

    broadcasts = [_broadcast(html_file="lv1/lv1.html")]
    broadcasts[0]["tags"] = step13.read_broadcast_page_tags(tmp_path, "lv1/lv1.html")
    step13.process_tags(broadcasts, ["新規人物"])
    broadcasts[0]["tag_search_text"] = "新規人物について話した"
    step13.process_tags(broadcasts, ["新規人物"])
    changed = step13.sync_broadcast_html_tags(tmp_path, broadcasts)

    assert broadcasts[0]["tags"] == ["既存人物", "新規人物"]
    assert set(changed) == {"lv1/lv1.html", "lv1/lv1_mobile.html"}
    assert read_page_tags_file(pc) == ["既存人物", "新規人物"]
    assert read_page_tags_file(mobile) == ["既存人物", "新規人物"]
    assert '<div id="pc">PC</div>' in pc.read_text(encoding="utf-8")


def test_existing_index_shell_is_not_rewritten(tmp_path: Path) -> None:
    broadcasts = [_broadcast()]
    step13.apply_broadcaster_fallback_tags(broadcasts, [])
    step13.generate_index_page(str(tmp_path), broadcasts, {"tags": ["yosino"]})
    index_path = tmp_path / "index.html"
    first = index_path.read_text(encoding="utf-8")
    customized = first.replace("</body>", '<aside id="manual-edit">保持</aside></body>')
    index_path.write_text(customized, encoding="utf-8")

    broadcasts[0]["title"] = "更新タイトル"
    changed = []
    step13.generate_index_page(
        str(tmp_path), broadcasts, {"tags": ["yosino"]}, change_log=changed
    )
    updated = index_path.read_text(encoding="utf-8")

    assert '<aside id="manual-edit">保持</aside>' in updated
    assert "更新タイトル" in updated
    assert changed == [str(index_path)]
