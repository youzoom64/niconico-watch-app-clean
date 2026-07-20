import os
import json
import html
import re
from datetime import datetime
from pathlib import Path, PurePosixPath
import sys
from urllib.request import Request, urlopen
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from utils import find_account_directory
from archive_db import list_broadcast_data, load_transcript_payload

try:
    from .html_preservation import read_page_tags_file, update_page_tags_file
    from .step12_mobile_html_generator import mobile_html_filename
except ImportError:
    from processors.html_preservation import read_page_tags_file, update_page_tags_file
    from processors.step12_mobile_html_generator import mobile_html_filename

MANUAL_TAGS_FILENAME = 'index_person_tags.json'
PERSON_ALIASES_FILENAME = 'index_person_aliases.json'
MANUAL_EXCLUDES_KEY = '_exclude'
HISTORY_DELETED_KEY = '_history_deleted'
HISTORY_DELETED_URLS_KEY = '_history_deleted_urls'

def process(pipeline_data):
    """Step13: 一覧ページ生成（index.html + タグページ）"""
    try:
        account_id = pipeline_data['account_id']
        config = pipeline_data['config']
        
        print(f"Step13 一覧ページ生成開始: {account_id}")
        
        # 1. アカウントディレクトリ検索
        account_dir = find_account_directory(pipeline_data['platform_directory'], account_id)
        
        # 2. 全配信データを収集
        broadcast_list = collect_broadcast_data(account_dir, account_id)
        broadcast_list = merge_existing_index_records(
            account_dir, account_id, broadcast_list, config
        )
        
        # 3. タグ処理
        manual_tags = load_manual_tags(account_dir)
        manual_excludes = load_manual_tag_excludes(account_dir)
        history_deleted_lvs = load_history_deleted_lvs(account_dir)
        person_aliases = load_person_aliases(account_dir)
        person_aliases.update({
            str(alias).strip(): str(canonical).strip()
            for alias, canonical in (config.get('tag_aliases') or {}).items()
            if str(alias).strip() and str(canonical).strip()
        })
        manual_tags = canonicalize_manual_tags(manual_tags, person_aliases)
        tags_config = canonicalize_tag_names(config.get('tags', []), person_aliases)
        person_tag_names = collect_manual_tag_names(manual_tags)
        auto_tag_candidates = normalize_tag_names(tags_config + person_tag_names)
        processed_broadcasts = process_tags(broadcast_list, auto_tag_candidates, person_aliases)
        for broadcast in processed_broadcasts:
            broadcast['history_deleted'] = broadcast.get('lv_value') in history_deleted_lvs
        apply_manual_tags(processed_broadcasts, manual_tags)
        apply_manual_tag_excludes(processed_broadcasts, manual_excludes)
        effective_tags = apply_broadcaster_fallback_tags(processed_broadcasts, tags_config)
        effective_config = dict(config)
        effective_config['tags'] = effective_tags
        
        # 4. 各放送ページには管理ブロックだけを追記する。既存HTML全体は再生成しない。
        updated_html_paths = sync_broadcast_html_tags(account_dir, processed_broadcasts, person_aliases)

        # 5. メイン一覧ページ生成（既存時はJSON管理領域だけを更新）
        change_log = []
        generate_index_page(
            account_dir,
            processed_broadcasts,
            effective_config,
            change_log=change_log,
        )
        
        # 6. タグページ生成（既存時はJSON管理領域だけを更新）
        generated_tag_pages = generate_tag_pages(
            account_dir,
            processed_broadcasts,
            effective_tags,
            effective_config,
            change_log=change_log,
        )
        updated_html_paths.extend(
            relative_account_path(account_dir, path) for path in change_log
        )
        updated_html_paths = sorted(set(updated_html_paths))
        
        print(f"Step13 完了: {account_id} - 一覧ページ生成完了")
        return {
            "index_generated": True,
            "broadcast_count": len(processed_broadcasts),
            "tag_pages": len(generated_tag_pages),
            "tags": effective_tags,
            "updated_html_paths": updated_html_paths,
        }
        
    except Exception as e:
        print(f"Step13 エラー: {str(e)}")
        import traceback
        traceback.print_exc()
        raise

def collect_broadcast_data(account_dir, broadcaster_id):
    """DBから指定放送者の配信データを収集"""
    broadcast_list = []
    
    try:
        for data in list_broadcast_data(broadcaster_id):
            lv_value = str(data.get('lv_value') or '').strip()
            if not lv_value:
                continue
            broadcast_dir = str(data.get('broadcast_directory_path') or os.path.join(account_dir, lv_value))
            html_file = find_html_file(broadcast_dir, lv_value, account_dir, data)
            if not html_file:
                continue
            broadcast_info = {
                'lv_value': lv_value,
                'title': data.get('live_title', 'タイトル不明'),
                'broadcaster': data.get('broadcaster') or data.get('owner_name') or '不明',
                'start_time': data.get('start_time') or data.get('begin_time') or data.get('open_time') or 0,
                'watch_count': data.get('watch_count', 0),
                'comment_count': data.get('comment_count', 0),
                'elapsed_time': data.get('elapsed_time', ''),
                'summary_text': data.get('summary_text', ''),
                'html_file': html_file,
                'image_url': data.get('image_generation', {}).get('imgur_url', ''),
                'music_urls': get_music_urls_multiple(data),
                'transcript_segments': get_transcript_segments(broadcast_dir, lv_value),
                'tag_search_text': get_transcript_text(broadcast_dir, lv_value),
                'tags': read_broadcast_page_tags(account_dir, html_file)
            }
            broadcast_list.append(broadcast_info)
        
        # 開始時間順でソート（新しい順）
        broadcast_list.sort(key=lambda x: x['start_time'], reverse=True)
        print(f"配信データ収集完了(DB broadcaster={broadcaster_id}): {len(broadcast_list)}件")
        
    except Exception as e:
        print(f"配信データ収集エラー: {str(e)}")
    
    return broadcast_list

def get_music_urls_multiple(data):
    """音楽URLを複数取得"""
    music_data = data.get('music_generation', {})
    songs = music_data.get('songs', [])
    urls = []
    for song in songs:
        if song.get('primary_url'):
            urls.append(song['primary_url'])
    return urls


def find_html_file(broadcast_dir, lv_value, account_dir=None, data=None):
    """配信ディレクトリからHTMLファイルを検索"""
    preferred = (data or {}).get('html_file_path')
    if preferred:
        preferred_path = preferred if os.path.isabs(str(preferred)) else os.path.join(broadcast_dir, preferred)
        if os.path.exists(preferred_path):
            if account_dir:
                return os.path.relpath(preferred_path, account_dir).replace('\\', '/')
            return os.path.join(lv_value, preferred).replace('\\', '/')

    if not os.path.isdir(broadcast_dir):
        return None
    for file in os.listdir(broadcast_dir):
        if file.startswith(lv_value) and file.endswith('.html') and not file.lower().endswith('_mobile.html'):
            html_path = os.path.join(broadcast_dir, file)
            if account_dir:
                return os.path.relpath(html_path, account_dir).replace('\\', '/')
            return os.path.join(lv_value, file).replace('\\', '/')
    return None

def get_music_url(data):
    """音楽URLを取得"""
    music_data = data.get('music_generation', {})
    songs = music_data.get('songs', [])
    if songs and songs[0].get('primary_url'):
        return songs[0]['primary_url']
    return ''

def get_transcript_text(broadcast_dir, lv_value):
    """文字起こしテキストを取得"""
    transcript_file = os.path.join(broadcast_dir, f"{lv_value}_transcript.json")
    if os.path.exists(transcript_file):
        with open(transcript_file, 'r', encoding='utf-8') as f:
            transcript_data = json.load(f)
        
        transcripts = transcript_data.get('transcripts', [])
        return ' '.join([t.get('text', '') for t in transcripts])
    return ''

def process_tags(broadcast_list, tags_config, person_aliases=None):
    """タイトル・要約・文字起こし全文から候補タグを自動付与する。"""
    for broadcast in broadcast_list:
        # 安全にフィールドを取得
        title = broadcast.get('title', '')
        summary_text = broadcast.get('summary_text', '')
        transcript_text = str(broadcast.get('tag_search_text') or '')
        if not transcript_text:
            transcript_segments = broadcast.get('transcript_segments', [])
            transcript_text = ' '.join(transcript_segments) if transcript_segments else ''
        
        search_text = f"{title} {summary_text} {transcript_text}"
        search_text = search_text.lower()
        
        # 各タグをチェック
        aliases = person_aliases or {}
        broadcast['tags'] = canonicalize_tag_names(broadcast.get('tags', []), aliases)
        search_terms = [(tag, tag) for tag in tags_config]
        search_terms.extend((alias, canonical) for alias, canonical in aliases.items())
        for search_name, canonical_name in search_terms:
            if tag_occurs_in_text(search_name, search_text) and canonical_name not in broadcast['tags']:
                broadcast['tags'].append(canonical_name)
    
    return broadcast_list


ARCHIVE_DATA_PATTERN = re.compile(
    r'<script\b[^>]*\bid=["\']archive-data["\'][^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)


def merge_existing_index_records(account_dir, account_id, current, config):
    """既存の公開一覧を正本として読み、新規DB分で上書き統合する。"""
    root = Path(account_dir).resolve()
    sources = [root / 'index.html']
    legacy = root.parent / 'bloadcast' / 'index.html'
    if legacy != sources[0]:
        sources.append(legacy)

    records = []
    for path in sources:
        if path.is_file():
            records.extend(read_archive_records(path.read_text(encoding='utf-8')))

    settings = config.get('upload_settings') or {}
    public_template = str(
        settings.get('public_index_url_template')
        or 'https://warehouse.bitter.jp/niconico/{account_id}/index.html'
    )
    try:
        public_url = public_template.format(account_id=account_id)
        request = Request(public_url, headers={'User-Agent': 'niconico-watch-app/1.0'})
        with urlopen(request, timeout=10) as response:
            records.extend(read_archive_records(response.read().decode('utf-8')))
    except Exception as exc:
        print(f'Step13 公開index取得をスキップ: {type(exc).__name__}')

    merged = {}
    for record in records:
        converted = archive_record_to_broadcast(record)
        if converted:
            merged[converted['lv_value']] = converted
    for broadcast in current:
        merged[broadcast['lv_value']] = broadcast
    result = list(merged.values())
    result.sort(key=lambda item: int(item.get('start_time') or 0), reverse=True)
    print(f'Step13 既存一覧統合: existing={len(records)} merged={len(result)}')
    return result


def read_archive_records(document):
    match = ARCHIVE_DATA_PATTERN.search(str(document or ''))
    if not match:
        return []
    try:
        payload = json.loads(match.group(1).strip())
    except json.JSONDecodeError:
        return []
    return [item for item in payload if isinstance(item, dict)] if isinstance(payload, list) else []


def archive_record_to_broadcast(record):
    lv = str(record.get('lv') or '').strip()
    url = str(record.get('url') or '').strip().replace('\\', '/')
    if not re.fullmatch(r'lv\d+', lv) or not url:
        return None
    start_time = 0
    date_text = str(record.get('date') or '').strip()
    if date_text:
        try:
            start_time = int(datetime.strptime(date_text, '%Y-%m-%d %H:%M:%S').timestamp())
        except ValueError:
            pass
    return {
        'lv_value': lv,
        'title': record.get('title') or 'タイトル不明',
        'broadcaster': record.get('broadcaster') or '不明',
        'start_time': start_time,
        'watch_count': record.get('watch_count') or 0,
        'comment_count': record.get('comment_count') or 0,
        'elapsed_time': record.get('elapsed_time') or '',
        'summary_text': record.get('summary') or '',
        'html_file': url,
        'image_url': record.get('image_url') or '',
        'music_urls': record.get('music_urls') or [],
        'transcript_segments': record.get('transcript_segments') or [],
        'tag_search_text': ' '.join(record.get('transcript_segments') or []),
        'tags': normalize_tag_names(record.get('tags') or []),
        'history_deleted': bool(record.get('history_deleted')),
    }


def resolve_account_html_path(account_dir, relative_path):
    """一覧DBの相対パスをアカウント配下だけに制限して解決する。"""
    root = Path(account_dir).resolve()
    value = str(relative_path or '').strip().replace('\\', '/')
    if not value:
        raise ValueError('HTML相対パスが空です')
    pure = PurePosixPath(value)
    if pure.is_absolute() or '..' in pure.parts:
        raise ValueError(f'アカウント外のHTMLパスです: {value}')
    candidate = (root / Path(*pure.parts)).resolve()
    candidate.relative_to(root)
    return candidate


def relative_account_path(account_dir, path):
    root = Path(account_dir).resolve()
    candidate = Path(path).resolve()
    return candidate.relative_to(root).as_posix()


def read_broadcast_page_tags(account_dir, relative_path):
    """PC版HTMLだけを既存タグの正本として読む。"""
    pc_path = resolve_account_html_path(account_dir, relative_path)
    return normalize_tag_names(read_page_tags_file(pc_path))


def sync_broadcast_html_tags(account_dir, broadcast_list, person_aliases=None):
    """PC版HTMLへタグ管理ブロックだけを加え、変更パスを返す。"""
    changed_paths = []
    for broadcast in broadcast_list:
        relative_path = broadcast.get('html_file')
        if not relative_path:
            continue
        pc_path = resolve_account_html_path(account_dir, relative_path)
        if not pc_path.is_file():
            continue
        changed, merged = update_page_tags_file(
            pc_path,
            broadcast.get('tags', []),
            person_aliases=person_aliases,
        )
        broadcast['tags'] = normalize_tag_names(merged)
        if changed:
            changed_paths.append(relative_account_path(account_dir, pc_path))
    return changed_paths


def tag_occurs_in_text(tag, search_text):
    """人物名の出現を判定し、短い名前の一般語への部分一致を避ける。"""
    normalized_tag = str(tag or '').strip().lower()
    normalized_text = str(search_text or '').lower()
    if not normalized_tag:
        return False
    if normalized_tag == 'くみ':
        return re.search(r'くみ(?:さん|ちゃん)?(?:は|が|を|に|の|と|も|へ|って|、|。|\s|$)', normalized_text) is not None
    return normalized_tag in normalized_text


def normalize_tag_names(tags_config):
    """設定値を、重複と空文字のないタグ名リストへ正規化する。"""
    if isinstance(tags_config, dict):
        values = tags_config.keys()
    elif isinstance(tags_config, str):
        values = [tags_config]
    else:
        values = tags_config or []

    tags = []
    for value in values:
        tag = str(value or '').strip()
        if tag and tag not in tags:
            tags.append(tag)
    return tags


def load_manual_tags(account_dir):
    """LVごとに明示したタグをアカウント配下のJSONから読む。"""
    path = os.path.join(account_dir, MANUAL_TAGS_FILENAME)
    if not os.path.isfile(path):
        return {}
    with open(path, 'r', encoding='utf-8-sig') as f:
        payload = json.load(f)
    if not isinstance(payload, dict):
        raise ValueError(f'{MANUAL_TAGS_FILENAME} はオブジェクト形式である必要があります')
    result = {}
    for lv_value, tags in payload.items():
        lv = str(lv_value or '').strip()
        if lv in {MANUAL_EXCLUDES_KEY, HISTORY_DELETED_KEY, HISTORY_DELETED_URLS_KEY}:
            continue
        normalized = normalize_tag_names(tags)
        if lv and normalized:
            result[lv] = normalized
    return result


def load_manual_tag_excludes(account_dir):
    """LVごとに自動検出から除外するタグを読む。"""
    path = os.path.join(account_dir, MANUAL_TAGS_FILENAME)
    if not os.path.isfile(path):
        return {}
    with open(path, 'r', encoding='utf-8-sig') as f:
        payload = json.load(f)
    raw = payload.get(MANUAL_EXCLUDES_KEY, {}) if isinstance(payload, dict) else {}
    if not isinstance(raw, dict):
        return {}
    return {
        str(lv).strip(): normalize_tag_names(tags)
        for lv, tags in raw.items()
        if str(lv).strip() and normalize_tag_names(tags)
    }


def load_history_deleted_lvs(account_dir):
    """ニコニコ側の放送履歴から削除済みと手動指定したLVを読む。"""
    path = os.path.join(account_dir, MANUAL_TAGS_FILENAME)
    if not os.path.isfile(path):
        return set()
    with open(path, 'r', encoding='utf-8-sig') as f:
        payload = json.load(f)
    values = payload.get(HISTORY_DELETED_KEY, []) if isinstance(payload, dict) else []
    return {
        str(value).strip().lower()
        for value in values
        if re.fullmatch(r'lv\d+', str(value).strip(), re.I)
    }


def load_person_aliases(account_dir):
    """人物名の別表記または誤記から正規名への対応を読む。"""
    path = os.path.join(account_dir, PERSON_ALIASES_FILENAME)
    if not os.path.isfile(path):
        return {}
    with open(path, 'r', encoding='utf-8-sig') as f:
        payload = json.load(f)
    if isinstance(payload, dict) and isinstance(payload.get('canonical_names'), dict):
        canonical_names = payload['canonical_names']
        payload = {
            str(alias or '').strip(): str(canonical or '').strip()
            for canonical, alias_list in canonical_names.items()
            for alias in normalize_tag_names(alias_list)
        }
    elif isinstance(payload, dict) and isinstance(payload.get('aliases'), dict):
        payload = payload['aliases']
    if not isinstance(payload, dict):
        raise ValueError(f'{PERSON_ALIASES_FILENAME} はオブジェクト形式である必要があります')
    aliases = {}
    for alias, canonical in payload.items():
        alias_name = str(alias or '').strip()
        canonical_name = str(canonical or '').strip()
        if alias_name and canonical_name and alias_name != canonical_name:
            aliases[alias_name] = canonical_name
    return aliases


def canonicalize_name(name, aliases):
    """連鎖した別名も終端の正規名へ収束させる。"""
    value = str(name or '').strip()
    seen = set()
    while value in aliases and value not in seen:
        seen.add(value)
        value = aliases[value]
    return value


def canonicalize_tag_names(tags, aliases):
    return normalize_tag_names(canonicalize_name(tag, aliases) for tag in normalize_tag_names(tags))


def canonicalize_manual_tags(manual_tags, aliases):
    return {lv: canonicalize_tag_names(tags, aliases) for lv, tags in manual_tags.items()}


def collect_manual_tag_names(manual_tags):
    """LV別明示タグから、全文字起こしの自動検出候補を作る。"""
    names = []
    for tags in manual_tags.values():
        for tag in normalize_tag_names(tags):
            if tag not in names:
                names.append(tag)
    return names


def apply_manual_tags(broadcast_list, manual_tags):
    """明示タグを該当LVへ追加する。存在しないLVは将来分として無視する。"""
    for broadcast in broadcast_list:
        lv_value = str(broadcast.get('lv_value') or '').strip()
        tags = normalize_tag_names(broadcast.get('tags', []))
        for tag in manual_tags.get(lv_value, []):
            if tag not in tags:
                tags.append(tag)
        broadcast['tags'] = tags
    return broadcast_list


def apply_manual_tag_excludes(broadcast_list, manual_excludes):
    """放送単位の誤検出指定を最優先で取り除く。"""
    for broadcast in broadcast_list:
        lv_value = str(broadcast.get('lv_value') or '').strip()
        excluded = set(normalize_tag_names(manual_excludes.get(lv_value, [])))
        if excluded:
            broadcast['tags'] = [
                tag for tag in normalize_tag_names(broadcast.get('tags', []))
                if tag not in excluded
            ]
    return broadcast_list


def apply_broadcaster_fallback_tags(broadcast_list, configured_tags):
    """各配信へ配信者名を付け、人物タグなどと併存する実効タグ一覧を返す。"""
    effective_tags = normalize_tag_names(configured_tags)
    for broadcast in broadcast_list:
        tags = normalize_tag_names(broadcast.get('tags', []))
        broadcaster = str(broadcast.get('broadcaster') or '').strip()
        if broadcaster and broadcaster != '不明' and broadcaster not in tags:
            tags.append(broadcaster)
        broadcast['tags'] = tags
        for tag in tags:
            if tag not in effective_tags:
                effective_tags.append(tag)
    return effective_tags

def generate_index_page(account_dir, broadcast_list, config, *, change_log=None):
    """Step14と共通のモダンスタイルでメイン一覧ページを生成する。"""
    try:
        from . import step14_modern_list_generator as step14
    except ImportError:
        from processors import step14_modern_list_generator as step14

    return step14.generate_modern_list_page(
        account_dir,
        broadcast_list,
        config.get('tags', []),
        change_log=change_log,
    )

def generate_tag_pages(account_dir, broadcast_list, tags_config, config, *, change_log=None):
    """タグページ生成"""
    try:
        from . import step14_modern_list_generator as step14
    except ImportError:
        from processors import step14_modern_list_generator as step14

    tags_dir = os.path.join(account_dir, 'tags')
    os.makedirs(tags_dir, exist_ok=True)
    generated_pages = []
    
    for tag in tags_config:
        # そのタグを含む配信のみフィルタ
        filtered_broadcasts = [b for b in broadcast_list if tag in b['tags']]
        
        if filtered_broadcasts:
            tag_file = os.path.join(tags_dir, tag_page_filename(tag))
            step14.generate_modern_list_page(
                account_dir,
                filtered_broadcasts,
                tags_config,
                output_file=tag_file,
                document_title=f'#{tag} の配信一覧 - Archive',
                heading=f'#{tag} の配信一覧',
                heading_en='TAG ARCHIVE',
                link_prefix='../',
                compact=True,
                back_href='../index.html',
                change_log=change_log,
            )
            generated_pages.append(tag_file)
            
            print(f"タグページ生成: {tag_file} ({len(filtered_broadcasts)}件)")

    return generated_pages


def tag_page_filename(tag):
    """Windows上でも安全なタグページ名を返す。"""
    safe_tag = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', '_', str(tag or '').strip())
    safe_tag = safe_tag.rstrip('. ') or 'untagged'
    return f"tag_{safe_tag}.html"

def create_index_html(broadcast_list, all_tags, tag_page_prefix='tags/'):
    """メイン一覧HTML生成"""
    # JavaScript用データ準備
    js_data = {}
    for broadcast in broadcast_list:
        js_data[broadcast['lv_value']] = {
            'title': broadcast['title'],
            'broadcaster': broadcast['broadcaster'],
            'summary': broadcast['summary_text'],
            'imageUrl': broadcast['image_url'],
            'musicUrls': broadcast['music_urls'],  # 配列として渡す
            'comments': broadcast['transcript_segments']
        }
    
    html_content = f"""<!DOCTYPE html>
<html lang="ja">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>配信アーカイブ一覧</title>
    <style>
        body {{
            font-family: Arial, sans-serif;
            margin: 0;
            padding: 20px;
            background-color: #f5f5f5;
        }}
        .container {{
            max-width: 1200px;
            margin: 0 auto;
            background-color: white;
            padding: 20px;
            border-radius: 10px;
            box-shadow: 0 4px 8px rgba(0,0,0,0.1);
        }}
        .header {{
            text-align: center;
            margin-bottom: 30px;
            padding-bottom: 20px;
            border-bottom: 3px solid #007cba;
        }}
        .controls {{
            text-align: center;
            margin-bottom: 20px;
            padding: 15px;
            background-color: #f8f9fa;
            border-radius: 8px;
        }}
        .music-toggle {{
            display: inline-flex;
            align-items: center;
            gap: 10px;
            padding: 8px 15px;
            background-color: #007cba;
            color: white;
            border: none;
            border-radius: 20px;
            cursor: pointer;
            font-size: 14px;
            margin-right: 15px;
        }}
        .music-toggle.active {{
            background-color: #28a745;
        }}
        .tag-filter {{
            display: inline-block;
        }}
        .tag-button {{
            display: inline-block;
            padding: 5px 15px;
            margin: 5px;
            background-color: #007cba;
            color: white;
            text-decoration: none;
            border-radius: 15px;
            font-size: 0.9em;
            cursor: pointer;
            border: none;
        }}
        .tag-button:hover, .tag-button.active {{
            background-color: #005a8a;
        }}
        .broadcast-item {{
            position: relative;
            border: 1px solid #ddd;
            margin: 15px 0;
            padding: 20px;
            border-radius: 8px;
            background-color: #fafafa;
            transition: all 0.3s ease;
            overflow: hidden;
        }}
        .broadcast-item:hover {{
            border-color: #007cba;
            box-shadow: 0 4px 12px rgba(0,123,186,0.2);
        }}
        .broadcast-item.history-deleted {{
            position: relative;
            isolation: isolate;
            background-color: #fff0f1;
            border-color: #e7b8bd;
            animation: history-deleted-heartbeat 2.4s ease-in-out infinite;
        }}
        .broadcast-item.history-deleted::before {{
            content: "消されてしまった放送ページ\\00a0消されてしまった放送ページ\\00a0";
            position: absolute;
            top: 50%;
            left: 0;
            z-index: 0;
            width: max-content;
            color: rgba(120, 120, 125, .28);
            font-size: clamp(48px, 8vw, 112px);
            font-weight: 900;
            letter-spacing: .08em;
            line-height: 1.05;
            white-space: nowrap;
            filter: blur(8px);
            text-shadow: 0 0 32px rgba(135, 135, 140, .32);
            pointer-events: none;
            user-select: none;
            animation: history-deleted-marquee 18s linear infinite;
            will-change: transform;
        }}
        .broadcast-item.history-deleted > * {{ position: relative; z-index: 1; }}
        @keyframes history-deleted-heartbeat {{
            0%, 100% {{ background-color: #fff4f5; box-shadow: 0 0 0 rgba(210,55,75,0); }}
            14% {{ background-color: #ffcbd0; box-shadow: 0 0 18px rgba(210,55,75,.42); }}
            28% {{ background-color: #fff1f2; box-shadow: 0 0 2px rgba(210,55,75,.08); }}
            43% {{ background-color: #ffe1e4; box-shadow: 0 0 10px rgba(210,55,75,.24); }}
            62% {{ background-color: #fff4f5; box-shadow: 0 0 0 rgba(210,55,75,0); }}
        }}
        @keyframes history-deleted-marquee {{
            from {{ transform: translate(0, -50%); }}
            to {{ transform: translate(-50%, -50%); }}
        }}
        .broadcast-title {{
            font-size: 1.4em;
            font-weight: bold;
            color: #007cba;
            text-decoration: none;
            margin-bottom: 10px;
            display: block;
        }}
        .broadcast-title:hover {{
            color: #005a8a;
        }}
        .broadcast-info {{
            display: flex;
            gap: 30px;
            margin: 10px 0;
            flex-wrap: wrap;
        }}
        .info-item {{
            color: #666;
            font-size: 0.95em;
        }}
        .info-label {{
            font-weight: bold;
            color: #333;
        }}
        .broadcast-tags {{
            margin-top: 10px;
        }}
        .tag {{
            display: inline-block;
            background-color: #e3f2fd;
            color: #1976d2;
            padding: 3px 8px;
            margin: 2px;
            border-radius: 12px;
            font-size: 0.8em;
            border: 1px solid #bbdefb;
        }}
        .preview-popup {{
            position: absolute;
            background: white;
            border: 2px solid #007cba;
            border-radius: 10px;
            padding: 15px;
            box-shadow: 0 8px 16px rgba(0,0,0,0.3);
            z-index: 1000;
            width: 500px;
            pointer-events: none;
        }}
        .preview-image {{
            width: 100%;
            height: 250px;
            object-fit: cover;
            border-radius: 8px;
            margin-bottom: 15px;
        }}
        .preview-title {{
            font-weight: bold;
            color: #333;
            margin-bottom: 10px;
            font-size: 1.2em;
        }}
        .preview-summary {{
            color: #666;
            font-size: 0.9em;
            line-height: 1.5;
            max-height: 80px;
            overflow: hidden;
            margin-bottom: 15px;
        }}
        .preview-audio {{
            width: 100%;
            height: 30px;
        }}
        .comment-flow {{
            position: absolute;
            top: 0;
            left: 0;
            right: 0;
            height: 100%;
            pointer-events: none;
            overflow: hidden;
        }}
        .comment {{
            position: absolute;
            background-color: rgba(173, 216, 230, 0.9);
            padding: 4px 10px;
            border-radius: 15px;
            font-size: 0.85em;
            white-space: nowrap;
            animation: commentFlow 10s linear infinite;
            box-shadow: 0 2px 4px rgba(0,0,0,0.2);
            max-width: 300px;
            overflow: hidden;
            text-overflow: ellipsis;
        }}
        @keyframes commentFlow {{
            from {{
                transform: translateX(100vw);
                opacity: 1;
            }}
            to {{
                transform: translateX(-100%);
                opacity: 0;
            }}
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>配信アーカイブ一覧</h1>
            <p>全{len(broadcast_list)}件の配信記録</p>
        </div>
        
        <div class="controls">
            <button class="music-toggle" id="musicToggle">
                音楽プレビュー: OFF
            </button>
            
            <div class="tag-filter">
                <button class="tag-button active" data-tag="all">すべて</button>
                {generate_tag_buttons(all_tags)}
            </div>
        </div>
        
        <div class="broadcast-list">
            {generate_broadcast_items(broadcast_list)}
        </div>
    </div>

    <script>
        const broadcastData = {json.dumps(js_data, ensure_ascii=False, indent=2)};
        const tagPageUrls = {json.dumps({tag: f"{tag_page_prefix}{tag_page_filename(tag)}" for tag in all_tags}, ensure_ascii=False)};
        
        let previewPopup = null;
        let commentIntervals = new Map();
        let musicEnabled = false;
        
        document.addEventListener('DOMContentLoaded', function() {{
            // 音楽トグル
            document.getElementById('musicToggle').addEventListener('click', function() {{
                musicEnabled = !musicEnabled;
                this.textContent = musicEnabled ? '音楽プレビュー: ON' : '音楽プレビュー: OFF';
                this.className = musicEnabled ? 'music-toggle active' : 'music-toggle';
            }});
            
            // タグフィルター
            document.querySelectorAll('.tag-button').forEach(button => {{
                button.addEventListener('click', function() {{
                    const selectedTag = this.dataset.tag;
                    filterByTag(selectedTag);
                    
                    document.querySelectorAll('.tag-button').forEach(btn => btn.classList.remove('active'));
                    this.classList.add('active');
                }});
            }});
            
            // 配信アイテムイベント
            document.querySelectorAll('.broadcast-item').forEach(item => {{
                item.addEventListener('mouseenter', function(e) {{
                    showPreview(this, e);
                    startCommentFlow(this);
                }});
                
                item.addEventListener('mouseleave', function() {{
                    hidePreview();
                    stopCommentFlow(this);
                }});
                
                item.addEventListener('mousemove', function(e) {{
                    updatePreviewPosition(e);
                }});
            }});
            document.querySelectorAll('.tag').forEach(tag => {{
                tag.addEventListener('click', function(e) {{
                    e.stopPropagation();
                    const tagName = this.dataset.tag;
                    const tagUrl = tagPageUrls[tagName];
                    if (tagUrl) window.location.href = tagUrl;
                }});
            }});
        }});
        
        function filterByTag(tag) {{
            document.querySelectorAll('.broadcast-item').forEach(item => {{
                const itemTags = item.dataset.tags ? item.dataset.tags.split(',') : [];
                if (tag === 'all' || itemTags.includes(tag)) {{
                    item.style.display = 'block';
                }} else {{
                    item.style.display = 'none';
                }}
            }});
        }}
        
        function showPreview(item, event) {{
            const lvValue = item.dataset.lv;
            const data = broadcastData[lvValue];
            
            if (!data) return;
            
            previewPopup = document.createElement('div');
            previewPopup.className = 'preview-popup';
            
            if (data.imageUrl) {{
                const img = document.createElement('img');
                img.className = 'preview-image';
                img.src = data.imageUrl;
                img.onerror = function() {{
                    this.style.display = 'none';
                }};
                previewPopup.appendChild(img);
            }}
            
            const title = document.createElement('div');
            title.className = 'preview-title';
            title.textContent = data.title;
            previewPopup.appendChild(title);
            
            const summary = document.createElement('div');
            summary.className = 'preview-summary';
            summary.textContent = data.summary;
            previewPopup.appendChild(summary);
            
            if (musicEnabled && data.musicUrl) {{
                const audio = document.createElement('audio');
                audio.className = 'preview-audio';
                audio.controls = true;
                audio.volume = 0.3;
                
                const source = document.createElement('source');
                source.src = data.musicUrl;
                source.type = 'audio/mp3';
                audio.appendChild(source);
                
                previewPopup.appendChild(audio);
            }}
            
            document.body.appendChild(previewPopup);
            updatePreviewPosition(event);
        }}
        
        function hidePreview() {{
            if (previewPopup) {{
                const audio = previewPopup.querySelector('audio');
                if (audio) audio.pause();
                
                document.body.removeChild(previewPopup);
                previewPopup = null;
            }}
        }}
        
        function updatePreviewPosition(event) {{
            if (previewPopup) {{
                const mouseX = event.clientX;
                const mouseY = event.clientY;
                const popupWidth = previewPopup.offsetWidth;
                const popupHeight = previewPopup.offsetHeight;
                const windowWidth = window.innerWidth;
                const windowHeight = window.innerHeight;
                
                // X座標: マウスの左側に少し離して配置
                let x = mouseX - popupWidth - 20;  // マウスから20px左に離す
                
                // 左端が窮屈な場合は右側に表示
                if (x < 10) {{
                    x = mouseX + 20;  // マウスの右側に表示
                }}
                
                // 右端制限
                if (x + popupWidth > windowWidth - 10) {{
                    x = windowWidth - popupWidth - 10;
                }}
                
                // Y座標: マウスの中央（ポップアップの中央がマウス位置になるよう）
                let y = mouseY - (popupHeight / 2);
                
                // 上端制限
                if (y < 10) {{
                    y = 10;
                }}
                
                // 下端制限
                if (y + popupHeight > windowHeight - 10) {{
                    y = windowHeight - popupHeight - 10;
                }}
                
                previewPopup.style.left = x + 'px';
                previewPopup.style.top = y + 'px';
            }}
        }}
        
        function startCommentFlow(item) {{
            const lvValue = item.dataset.lv;
            const data = broadcastData[lvValue];
            
            if (!data || !data.comments || data.comments.length === 0) return;
            
            const commentFlow = item.querySelector('.comment-flow');
            let commentIndex = 0;
            
            const interval = setInterval(() => {{
                const comment = document.createElement('div');
                comment.className = 'comment';
                comment.textContent = data.comments[commentIndex % data.comments.length];
                comment.style.top = Math.random() * 80 + 'px';
                
                commentFlow.appendChild(comment);
                
                setTimeout(() => {{
                    if (comment.parentNode) {{
                        comment.parentNode.removeChild(comment);
                    }}
                }}, 10000);
                
                commentIndex++;
            }}, 1500);
            
            commentIntervals.set(lvValue, interval);
        }}
        
        function stopCommentFlow(item) {{
            const lvValue = item.dataset.lv;
            const interval = commentIntervals.get(lvValue);
            
            if (interval) {{
                clearInterval(interval);
                commentIntervals.delete(lvValue);
            }}
            
            const commentFlow = item.querySelector('.comment-flow');
            commentFlow.innerHTML = '';
        }}
    </script>
</body>
</html>"""
    
    return html_content

def generate_tag_buttons(tags):
    """タグボタンHTML生成"""
    buttons = []
    for tag in tags:
        buttons.append(f'<button class="tag-button" data-tag="{html.escape(tag)}">{html.escape(tag)}</button>')
    return '\n                '.join(buttons)

def generate_broadcast_items(broadcast_list):
    """配信アイテムHTML生成"""
    items = []
    for broadcast in broadcast_list:
        tags_str = ','.join(broadcast['tags'])
        
        # タグをクリック可能にしてdata-tag属性を追加
        tags_html = ''
        for tag in broadcast['tags']:
            tags_html += f'<span class="tag" data-tag="{html.escape(tag)}" style="cursor: pointer;">{html.escape(tag)}</span>'
        
        start_time_str = datetime.fromtimestamp(int(broadcast['start_time'])).strftime('%Y/%m/%d %H:%M') if broadcast['start_time'] else '不明'
        
        item_html = f"""
            <div class="broadcast-item{' history-deleted' if broadcast.get('history_deleted') else ''}" data-lv="{broadcast['lv_value']}" data-tags="{html.escape(tags_str)}">
                <a href="{broadcast['html_file']}" class="broadcast-title">{html.escape(broadcast['title'])}</a>
                <div class="broadcast-info">
                    <div class="info-item">
                        <span class="info-label">配信者:</span> {html.escape(broadcast['broadcaster'])}
                    </div>
                    <div class="info-item">
                        <span class="info-label">開始時間:</span> {start_time_str}
                    </div>
                    <div class="info-item">
                        <span class="info-label">来場者数:</span> {broadcast['watch_count']}人
                    </div>
                    <div class="info-item">
                        <span class="info-label">コメント数:</span> {broadcast['comment_count']}コメ
                    </div>
                    <div class="info-item">
                        <span class="info-label">配信時間:</span> {broadcast['elapsed_time']}
                    </div>
                </div>
                <div class="broadcast-tags">
                    {tags_html}
                </div>
                <div class="comment-flow"></div>
            </div>"""
        items.append(item_html)
    
    return '\n        '.join(items)



def create_tag_html(filtered_broadcasts, tag, all_tags):
    """タグページHTML生成"""
    # tags/ 配下から各配信HTMLへ到達できる相対URLに直す。
    page_broadcasts = []
    for broadcast in filtered_broadcasts:
        page_broadcast = dict(broadcast)
        html_file = str(page_broadcast.get('html_file') or '')
        if html_file and not re.match(r'^(?:[a-z][a-z0-9+.-]*:|/|\.\.?/)', html_file, re.I):
            page_broadcast['html_file'] = f"../{html_file}"
        page_broadcasts.append(page_broadcast)

    # メイン一覧と同じ構造だが、タグ間リンクは tags/ 内の兄弟ページを指す。
    html_content = create_index_html(page_broadcasts, all_tags, tag_page_prefix='')
    
    # タイトル部分を置換
    html_content = html_content.replace(
        '<h1>配信アーカイブ一覧</h1>',
        f'<h1>#{html.escape(tag)} の配信一覧</h1>'
    )
    html_content = html_content.replace(
        f'<p>全{len(filtered_broadcasts)}件の配信記録</p>',
        f'<p>タグ「{html.escape(tag)}」: {len(filtered_broadcasts)}件の配信</p>'
    )
    
    # 戻るリンク追加
    html_content = html_content.replace(
        '<div class="controls">',
        '<div style="margin-bottom: 20px;"><a href="../index.html" style="color: #007cba;">← 全配信一覧に戻る</a></div>\n        <div class="controls">'
    )
    
    return html_content

def get_transcript_segments(broadcast_dir, lv_value):
    """文字起こしセグメントを個別に取得"""
    transcript_file = os.path.join(broadcast_dir, f"{lv_value}_transcript.json")
    if os.path.exists(transcript_file):
        with open(transcript_file, 'r', encoding='utf-8') as f:
            transcript_data = json.load(f)
        
        transcripts = transcript_data.get('transcripts', [])
        # 空でないセグメントのみ取得、最大10個
        segments = []
        for t in transcripts:
            text = t.get('text', '').strip()
            if text and len(segments) < 10:
                segments.append(text)
        return segments
    transcript_data = load_transcript_payload(lv_value)
    segments = []
    for t in transcript_data.get('transcripts', []):
        text = t.get('text', '').strip()
        if text and len(segments) < 10:
            segments.append(text)
    return segments
