import os
import json
import html
import math
import re
from datetime import datetime
import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from archive_db import load_broadcast_data as load_broadcast_data_from_db
from archive_db import (
    load_comments_payload,
    load_ranking_payload,
    load_transcript_payload,
    update_broadcast_data,
)
from utils import find_account_directory
from datetime import datetime, timezone, timedelta

try:
    from .step13_index_generator import (
        collect_manual_tag_names,
        load_manual_tags,
        load_manual_tag_excludes,
        tag_occurs_in_text,
    )
except ImportError:
    from processors.step13_index_generator import (
        collect_manual_tag_names,
        load_manual_tags,
        load_manual_tag_excludes,
        tag_occurs_in_text,
    )

DEFAULT_CHARACTER1_FULLBODY_URL = "https://raw.githubusercontent.com/youzoom64/niconico-character-icons/main/assets/characters/nini_fullbody.png"
DEFAULT_CHARACTER2_FULLBODY_URL = "https://raw.githubusercontent.com/youzoom64/niconico-character-icons/main/assets/characters/koko_fullbody.png"
COMMENT_RANKING_DISPLAY_LIMIT = 10
VIRTUAL_TIMELINE_WINDOW_SIZE = 36
VIRTUAL_TIMELINE_BUFFER_BEFORE = 12
DEFAULT_TIMELINE_BLOCK_HEIGHT = 180
TIMELINE_BLOCK_VERTICAL_GAP = 10


def process(pipeline_data):
    """Step12: 完全版HTML生成（全機能統合）"""
    try:
        lv_value = pipeline_data['lv_value']
        config = pipeline_data['config']
        
        print(f"Step12 完全版開始: {lv_value}")
        
        # 1. アカウントディレクトリ検索
        account_dir = find_account_directory(pipeline_data['platform_directory'], pipeline_data['account_id'])
        broadcast_dir = os.path.join(account_dir, lv_value)
        registered_person_names = collect_manual_tag_names(load_manual_tags(account_dir))
        excluded_names = set(load_manual_tag_excludes(account_dir).get(lv_value, []))
        registered_person_names = [
            name for name in registered_person_names if name not in excluded_names
        ]
        
        # 2. 全データをDBから読み込み
        broadcast_data = load_broadcast_data_from_db(lv_value)
        if not broadcast_data:
            raise Exception(f"放送データDBが見つかりません: {lv_value}")
        registered_broadcast_dir = str(broadcast_data.get('broadcast_directory_path') or '').strip()
        if registered_broadcast_dir:
            broadcast_dir = registered_broadcast_dir
        transcript_data = load_transcript_payload(lv_value)
        comments_data = load_comments_payload(lv_value)
        ranking_data = load_ranking_payload(lv_value, comments_data)
        recording_segment_timeline = pipeline_data.get('recording_segment_timeline') or {}
        
        # 3. 各種データ準備
        timeline_data = create_timeline_blocks(transcript_data, comments_data, lv_value, broadcast_data)
        timeline_data['recording_segment_timeline'] = recording_segment_timeline
        timeline_data['comments_fetch_failed'] = bool(pipeline_data.get('comments_fetch_failed'))
        transcript_blocks = timeline_data['transcript_blocks']
        comment_blocks = timeline_data['comment_blocks']
        word_ranking = prepare_word_ranking(broadcast_data)
        comment_ranking = prepare_comment_ranking(ranking_data, account_dir, lv_value, comments_data)
        ai_chats = prepare_ai_chats(broadcast_data, config)
        
        # 4. 既存HTMLを正本として保持する。明示的な強制再生成時だけ全体を書き換える。
        live_title = broadcast_data.get('live_title', 'タイトル不明')
        expected_filename = build_html_filename(lv_value, live_title)
        force_overwrite = config.get('force_overwrite_existing_html') is True
        existing_html = find_existing_pc_html(
            broadcast_dir,
            lv_value,
            broadcast_data,
            expected_filename,
        )
        pc_html_preserved = bool(existing_html and not force_overwrite)
        if pc_html_preserved:
            html_file = existing_html
            print(f"既存PC版HTMLを保持: {html_file}")
        else:
            html_content = generate_complete_html(
                timeline_data, broadcast_data, word_ranking,
                comment_ranking, ai_chats, config, lv_value, broadcast_dir,
                registered_person_names,
            )
            html_file = save_html_file(broadcast_dir, lv_value, live_title, html_content)
        
        # 5. 放送データDBにPC版HTMLパスを追加
        broadcast_data['html_file_path'] = os.path.basename(html_file)  # ファイル名のみ
        update_broadcast_data(lv_value, {"html_file_path": broadcast_data["html_file_path"]})
        
        print(f"Step12 完全版完了: {lv_value} - PC版: {html_file}")
        return {
            "html_generated": True,
            "html_file": html_file,
            "pc_html_preserved": pc_html_preserved,
            "mobile_generated": False,
        }
        
    except Exception as e:
        print(f"Step12 エラー: {str(e)}")
        import traceback
        traceback.print_exc()
        raise

def load_json_file(directory, filename):
    """JSONファイルを読み込み"""
    file_path = os.path.join(directory, filename)
    if os.path.exists(file_path):
        with open(file_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}

def create_timeline_blocks(transcript_data, comments_data, lv_value, broadcast_data):
    """Build every ten-second block on the canonical broadcast-wide clock."""
    try:
        # The finalized segment/MP3 timeline is authoritative.  elapsed_time is
        # kept only as a legacy fallback because it may describe scheduled wall
        # time or an older, incorrectly concatenated artifact.
        elapsed_time = broadcast_data.get('elapsed_time', '')
        try:
            video_duration = float(broadcast_data.get('video_duration', 0) or 0)
        except (TypeError, ValueError):
            video_duration = 0.0
        max_seconds = int(math.ceil(video_duration)) if video_duration > 0 else 0
        if max_seconds <= 0:
            max_seconds = parse_elapsed_time_to_seconds(elapsed_time)
        
        print(
            f"DEBUGLOG: canonical video_duration: {video_duration}, "
            f"elapsed_time fallback: {elapsed_time}, 最大秒数: {max_seconds}"
        )
        
        if max_seconds <= 0:
            print("DEBUGLOG: elapsed_time/video_durationが0以下のため、デフォルトで3600秒（1時間）を設定")
            max_seconds = 3600
        
        # 1. 最初に0秒からelapsed_timeまで全タイムブロックを10秒刻みで生成
        all_time_blocks = []
        # elapsed_time は終端なので、60秒放送なら 0-10 ... 50-60 までを作る。
        # 60-70 の存在しないブロックを作ると 60.jpg を参照して画像切れになる。
        for seconds in range(0, max_seconds, 10):
            all_time_blocks.append(seconds)
        
        print(f"DEBUGLOG: 生成する全タイムブロック数: {len(all_time_blocks)} (0秒〜{max_seconds}秒)")
        
        # 2. 文字起こし用ブロック辞書を空で初期化
        transcript_blocks = {}
        for block_time in all_time_blocks:
            transcript_blocks[block_time] = {
                'start_seconds': block_time,
                'end_seconds': block_time + 10,
                'time_range': format_time_range(block_time, block_time + 10),
                'transcript': '',  # 空で初期化
                'speaker': '',
                'transcripts': [],
                'center_score': 0.0,
                'positive_score': 0.0,
                'negative_score': 0.0,
                'screenshot_path': f"./screenshot/{lv_value}/{block_time}.jpg"
            }
        
        # 3. コメント用ブロック辞書を空で初期化
        comment_blocks = {}
        for block_time in all_time_blocks:
            comment_blocks[block_time] = {
                'start_seconds': block_time,
                'end_seconds': block_time + 10,
                'time_range': format_time_range(block_time, block_time + 10),
                'comments': []  # 空で初期化
            }
        
        print(f"DEBUGLOG: 空のタイムラインブロック初期化完了")
        
        # 4. 文字起こしデータを適切なブロックに配置
        transcripts = transcript_data.get('transcripts', [])
        print(f"DEBUGLOG: 文字起こしデータ: {len(transcripts)}件")
        
        for segment in transcripts:
            start_seconds = float(segment.get('start', segment.get('start_seconds', segment.get('timestamp', 0))) or 0)
            timestamp = int(start_seconds)
            timeline_block = int(math.floor(start_seconds / 10.0) * 10)
            
            # elapsed_time範囲内のデータのみ処理
            if timeline_block in transcript_blocks:
                transcript_item = {
                    'start': float(segment.get('start', segment.get('timestamp', timestamp)) or 0),
                    'end': float(segment.get('end', segment.get('timestamp', timestamp)) or 0),
                    'timestamp': int(timestamp or 0),
                    'segment_index': int(segment.get('segment_index') or 0),
                    'text': html.escape(segment.get('text', '')),
                    'speaker': html.escape(str(segment.get('speaker', '') or '')),
                    'center_score': round(segment.get('center_score', 0), 3),
                    'positive_score': round(segment.get('positive_score', 0), 3),
                    'negative_score': round(segment.get('negative_score', 0), 3),
                }
                transcript_blocks[timeline_block]['transcripts'].append(transcript_item)
                transcript_blocks[timeline_block]['transcript'] = '<br>'.join(
                    item['text'] for item in transcript_blocks[timeline_block]['transcripts'] if item.get('text')
                )
                speakers = [
                    item['speaker'] for item in transcript_blocks[timeline_block]['transcripts'] if item.get('speaker')
                ]
                transcript_blocks[timeline_block]['speaker'] = speakers[0] if len(set(speakers)) == 1 else ' / '.join(dict.fromkeys(speakers))
                count = len(transcript_blocks[timeline_block]['transcripts'])
                if count:
                    transcript_blocks[timeline_block]['center_score'] = round(
                        sum(item['center_score'] for item in transcript_blocks[timeline_block]['transcripts']) / count,
                        3,
                    )
                    transcript_blocks[timeline_block]['positive_score'] = round(
                        sum(item['positive_score'] for item in transcript_blocks[timeline_block]['transcripts']) / count,
                        3,
                    )
                    transcript_blocks[timeline_block]['negative_score'] = round(
                        sum(item['negative_score'] for item in transcript_blocks[timeline_block]['transcripts']) / count,
                        3,
                    )
                transcript_blocks[timeline_block].update({
                    'transcripts': transcript_blocks[timeline_block]['transcripts'],
                })
                print(f"DEBUGLOG: 文字起こし配置 - {timeline_block}秒: {segment.get('text', '')[:50]}")
            else:
                print(f"DEBUGLOG: 範囲外の文字起こしスキップ - {timeline_block}秒")
        
        # 5. コメントデータを適切なブロックに配置
        comments = comments_data.get('comments', [])
        print(f"DEBUGLOG: コメントデータ: {len(comments)}件")
        
        for comment in comments:
            timeline_block = comment.get('timeline_block', 0)
            comment_seconds = float(comment.get('broadcast_seconds', 0) or 0)
            
            # elapsed_time範囲内のデータのみ処理
            if timeline_block in comment_blocks:
                user_url = ""
                if not comment.get('anonymity', False) and comment.get('user_id', ''):
                    user_url = f"https://www.nicovideo.jp/user/{comment.get('user_id', '')}"
                
                comment_data = {
                    'index': comment.get('no', 0),
                    'time': format_seconds_to_time(comment.get('broadcast_seconds', 0)),
                    'seconds': comment_seconds,
                    'user_name': html.escape(comment.get('user_name', '')),
                    'user_url': user_url,
                    'text': html.escape(comment.get('text', '')),
                    'icon_url': get_user_icon_url(comment.get('user_id', ''))
                }
                
                comment_blocks[timeline_block]['comments'].append(comment_data)
                print(f"DEBUGLOG: コメント配置 - {timeline_block}秒: {comment.get('text', '')[:30]}")
            else:
                print(f"DEBUGLOG: 範囲外のコメントスキップ - {timeline_block}秒")
        
        # 6. ソートして配列に変換
        transcript_timeline = []
        comment_timeline = []
        
        for block_time in sorted(all_time_blocks):
            transcript_timeline.append(transcript_blocks[block_time])
            
            block = comment_blocks[block_time]
            # コメントを時間順にソート
            block['comments'].sort(key=lambda x: x.get('seconds', 0))
            comment_timeline.append(block)
        
        print(f"DEBUGLOG: 最終タイムライン生成完了")
        print(f"DEBUGLOG: 文字起こしブロック: {len(transcript_timeline)}ブロック")
        print(f"DEBUGLOG: コメントブロック: {len(comment_timeline)}ブロック")
        
        # データがあるブロック数をカウント
        transcript_with_data = sum(1 for block in transcript_timeline if block['transcript'])
        comment_with_data = sum(1 for block in comment_timeline if block['comments'])
        print(f"DEBUGLOG: データ有り文字起こしブロック: {transcript_with_data}")
        print(f"DEBUGLOG: データ有りコメントブロック: {comment_with_data}")
        
        return {
            'transcript_blocks': transcript_timeline,
            'comment_blocks': comment_timeline
        }
        
    except Exception as e:
        print(f"DEBUGLOG: タイムライン作成エラー: {str(e)}")
        import traceback
        traceback.print_exc()
        return {
            'transcript_blocks': [],
            'comment_blocks': []
        }

def get_user_icon_url(user_id):
    """ユーザーアイコンURL生成"""
    if not user_id or len(user_id) <= 4:
        return f"https://secure-dcdn.cdn.nimg.jp/nicoaccount/usericon/{user_id}.jpg"
    else:
        path_prefix = user_id[:-4]
        return f"https://secure-dcdn.cdn.nimg.jp/nicoaccount/usericon/{path_prefix}/{user_id}.jpg"

def parse_elapsed_time_to_seconds(elapsed_time_str):
    """elapsed_time文字列を秒数に変換"""
    try:
        # "01:32:11.6330331" -> 秒数に変換
        if not elapsed_time_str:
            return 0
            
        time_parts = elapsed_time_str.split(':')
        if len(time_parts) != 3:
            return 0
            
        hours = int(time_parts[0])
        minutes = int(time_parts[1])
        seconds = float(time_parts[2])
        
        total_seconds = hours * 3600 + minutes * 60 + seconds
        return int(total_seconds)
        
    except (ValueError, IndexError, AttributeError) as e:
        print(f"elapsed_time解析エラー: {elapsed_time_str} - {str(e)}")
        return 0


def prepare_word_ranking(broadcast_data):
    """単語ランキングデータを準備"""
    try:
        word_ranking = []
        for word_item in broadcast_data.get('word_ranking', []):
            word_ranking.append({
                'word': html.escape(word_item.get('word', '')),
                'count': word_item.get('count', 0),
                'font_size': word_item.get('font_size', 16)
            })
        print(f"単語ランキング準備: {len(word_ranking)}語")
        return word_ranking
    except Exception as e:
        print(f"単語ランキング準備エラー: {str(e)}")
        return []

def prepare_comment_ranking(ranking_data, account_dir, lv_value, comments_data=None):
    """コメントランキングデータを準備（表示は上位だけ、DB/JSONは全件維持）"""
    try:
        comment_ranking = []

        all_comments = {}
        comments_payload = comments_data if isinstance(comments_data, dict) else load_comments_payload(lv_value)
        ranking_rows = ranking_data.get('ranking', [])[:COMMENT_RANKING_DISPLAY_LIMIT]
        ranking_user_ids = {rank_data.get('user_id', '') for rank_data in ranking_rows}

        # ユーザーID別にコメントをグループ化
        for comment in comments_payload.get('comments', []):
            user_id = comment.get('user_id', '')
            if user_id not in ranking_user_ids:
                continue
            if user_id not in all_comments:
                all_comments[user_id] = []
            all_comments[user_id].append({
                'index': comment.get('no', 0),
                'text': html.escape(comment.get('text', '')),
                'time': format_seconds_to_time(comment.get('broadcast_seconds', 0)),
                'broadcast_seconds': comment.get('broadcast_seconds', 0)
            })
        
        for rank_data in ranking_rows:
            user_id = rank_data.get('user_id', '')
            user_name = html.escape(rank_data.get('user_name', ''))
            
            # スペシャルユーザーページ確認
            special_user_dir = os.path.join(account_dir, f"special_user_{user_id}")
            detail_file = os.path.join(special_user_dir, f"{user_id}_{lv_value}_detail.html")
            
            if os.path.exists(detail_file):
                user_name_display = f'<a href="../special_user_{user_id}/{user_id}_{lv_value}_detail.html" target="_blank">{user_name}</a>'
            else:
                user_name_display = user_name
            
            user_url = ""
            if not rank_data.get('anonymity', False) and user_id:
                user_url = f"https://www.nicovideo.jp/user/{user_id}"
            
            # そのユーザーの全コメントを取得
            user_comments = all_comments.get(user_id, [])
            # 時間順にソート
            user_comments.sort(key=lambda x: x['broadcast_seconds'])
            
            comment_ranking.append({
                'rank': rank_data.get('rank', 0),
                'user_id': user_id,
                'user_name': user_name_display,
                'user_url': user_url,
                'icon_url': f"https://secure-dcdn.cdn.nimg.jp/nicoaccount/usericon/{user_id[:-4]}/{user_id}.jpg",
                'comment_count': rank_data.get('comment_count', 0),
                'first_comment': html.escape(rank_data.get('first_comment', '')),
                'first_comment_time': format_seconds_to_time(rank_data.get('first_comment_time', 0)),
                'last_comment': html.escape(rank_data.get('last_comment', '')),
                'last_comment_time': format_seconds_to_time(rank_data.get('last_comment_time', 0)),
                'comments': user_comments  # キー名をcommentsに統一
            })
        
        print(f"コメントランキング準備: {len(comment_ranking)}ユーザー (表示上限: {COMMENT_RANKING_DISPLAY_LIMIT})")
        return comment_ranking
    except Exception as e:
        print(f"コメントランキング準備エラー: {str(e)}")
        return []
    
def prepare_ai_chats(broadcast_data, config):
    """AI会話データを準備"""
    try:
        ai_prompts = config.get('ai_prompts', {})
        char1_name = ai_prompts.get('character1_name', 'ニニちゃん')
        char1_image = ai_prompts.get('character1_image_url', '')
        char1_flip = ai_prompts.get('character1_image_flip', False)
        char2_name = ai_prompts.get('character2_name', 'ココちゃん')
        char2_image = ai_prompts.get('character2_image_url', '')
        char2_flip = ai_prompts.get('character2_image_flip', False)
        
        def get_character_info(name):
            if name == char1_name:
                return {'icon': char1_image, 'flip': char1_flip}
            elif name == char2_name:
                return {'icon': char2_image, 'flip': char2_flip}
            return {'icon': '', 'flip': False}
        
        intro_chat = []
        for chat in broadcast_data.get('intro_chat', []):
            char_info = get_character_info(chat.get('name', ''))
            intro_chat.append({
                'name': html.escape(chat.get('name', '')),
                'dialogue': html.escape(chat.get('dialogue', '')),
                'icon': char_info['icon'],
                'flip': char_info['flip']
            })
        
        outro_chat = []
        for chat in broadcast_data.get('outro_chat', []):
            char_info = get_character_info(chat.get('name', ''))
            outro_chat.append({
                'name': html.escape(chat.get('name', '')),
                'dialogue': html.escape(chat.get('dialogue', '')),
                'icon': char_info['icon'],
                'flip': char_info['flip']
            })
        
        print(f"AI会話準備: 開始前{len(intro_chat)}件, 終了後{len(outro_chat)}件")
        return {'intro': intro_chat, 'outro': outro_chat}
    except Exception as e:
        print(f"AI会話準備エラー: {str(e)}")
        return {'intro': [], 'outro': []}

def prepare_ai_fullbody_assets(config):
    """ニニココ会話用の全身背景画像を準備"""
    ai_prompts = config.get('ai_prompts', {})
    char1_url = ai_prompts.get('character1_fullbody_image_url') or DEFAULT_CHARACTER1_FULLBODY_URL
    char2_url = ai_prompts.get('character2_fullbody_image_url') or DEFAULT_CHARACTER2_FULLBODY_URL
    return {
        'char1_url': html.escape(str(char1_url), quote=True),
        'char2_url': html.escape(str(char2_url), quote=True),
    }


def build_speaker_emotion_data(transcript_blocks, recording_segment_timeline=None):
    """Chart.js向けに話者別の感情系列を作る"""
    speakers = []
    for block in transcript_blocks:
        items = block.get('transcripts') or []
        if items:
            for item in items:
                speaker = str(item.get('speaker') or '').strip()
                if speaker and speaker not in speakers:
                    speakers.append(speaker)
        else:
            speaker = str(block.get('speaker') or '').strip()
            if speaker and speaker not in speakers:
                speakers.append(speaker)

    speaker_data = {}
    for speaker in speakers:
        speaker_blocks = []
        for block in transcript_blocks:
            items = [
                item for item in (block.get('transcripts') or [])
                if str(item.get('speaker') or '').strip() == speaker
            ]
            speaker_block = dict(block)
            speaker_block['transcripts'] = items
            speaker_blocks.append(speaker_block)
        speaker_data[speaker] = build_emotion_chart_series(
            speaker_blocks,
            recording_segment_timeline,
        )
    return speaker_data


def build_emotion_chart_series(
    transcript_blocks,
    recording_segment_timeline=None,
):
    """Build a global series that can stop and resume across recording gaps."""
    timeline = recording_segment_timeline or {}
    media_segments = list(timeline.get('segments') or [])

    def recorded_segment_index(second):
        if not media_segments:
            return 0
        value = float(second)
        for index, segment in enumerate(media_segments):
            if (
                float(segment.get('timeline_start_seconds') or 0.0)
                <= value
                < float(segment.get('timeline_end_seconds') or 0.0)
            ):
                return index
        return None

    def item_start(item, fallback):
        for key in ('start', 'start_seconds', 'timestamp'):
            value = item.get(key)
            if value is not None:
                try:
                    return float(value)
                except (TypeError, ValueError):
                    pass
        return float(fallback)

    points = []
    for block in transcript_blocks:
        items = list(block.get('transcripts') or [])
        block_start = float(block.get('start_seconds') or 0.0)
        if items:
            # Use each finalized transcript's global start time directly.
            # No block averaging is allowed: a ten-second display block can
            # straddle a reconnect gap, and averaging would manufacture a
            # sentiment point at a time where no recording exists.
            for item in items:
                second = item_start(item, block_start)
                segment_index = recorded_segment_index(second)
                if segment_index is None:
                    points.append((second, None, None, None))
                else:
                    points.append((
                        second,
                        round(float(item.get('positive_score') or 0.0), 3),
                        round(float(item.get('center_score') or 0.0), 3),
                        round(float(item.get('negative_score') or 0.0), 3),
                    ))
        # Empty display blocks do not manufacture a zero-valued sentiment
        # sample.  The graph is derived only from finalized transcript timing;
        # real recording gaps are represented by the exact null boundaries
        # appended below.

    # Null points at exact boundaries prevent Chart.js from drawing a false
    # line through missing media.  A later transcript point resumes the series.
    for gap in timeline.get('gaps') or []:
        gap_start = float(gap.get('timeline_start_seconds') or 0.0)
        gap_end = float(gap.get('timeline_end_seconds') or gap_start)
        if gap_end <= gap_start:
            continue
        points.append((gap_start, None, None, None))
        points.append((max(gap_start, gap_end - 0.000001), None, None, None))

    points.sort(key=lambda point: point[0])
    return {
        'segments': [round(point[0], 6) for point in points],
        'positive': [point[1] for point in points],
        'center': [point[2] for point in points],
        'negative': [point[3] for point in points],
    }


def format_segment_time(seconds):
    try:
        seconds = float(seconds)
    except (TypeError, ValueError):
        seconds = 0.0
    minutes = int(seconds // 60)
    rest = seconds - minutes * 60
    return f"{minutes:02d}:{rest:05.2f}"


def _is_valid_registered_person_match(name, text, end_index):
    """Use the same short-name boundary as Step13's person-tag matcher."""
    if str(name).strip().lower() != 'くみ':
        return True
    suffix = str(text)[end_index:].lower()
    return re.match(
        r'^(?:さん|ちゃん)?(?:は|が|を|に|の|と|も|へ|って|、|。|\s|$)',
        suffix,
    ) is not None


def render_registered_person_names(text, registered_person_names=None):
    """Escape transcript text and enlarge names registered by Step13."""
    raw_text = html.unescape(str(text or ''))
    names = []
    for value in registered_person_names or []:
        name = str(value or '').strip()
        if name and name not in names and tag_occurs_in_text(name, raw_text):
            names.append(name)
    if not names:
        return html.escape(raw_text)

    pattern = re.compile(
        '|'.join(re.escape(name) for name in sorted(names, key=len, reverse=True)),
        re.IGNORECASE,
    )
    parts = []
    cursor = 0
    for match in pattern.finditer(raw_text):
        if not _is_valid_registered_person_match(
            match.group(0), raw_text, match.end()
        ):
            continue
        parts.append(html.escape(raw_text[cursor:match.start()]))
        parts.append(
            '<span class="registered-person-name">{}</span>'.format(
                html.escape(match.group(0))
            )
        )
        cursor = match.end()
    parts.append(html.escape(raw_text[cursor:]))
    return ''.join(parts)


def render_transcript_lines(block, registered_person_names=None):
    items = block.get('transcripts') or []
    if not items:
        transcript = str(block.get("transcript") or "").strip()
        if not transcript:
            return ""
        return '<p class="comment transcript-comment">{}</p>'.format(
            render_registered_person_names(transcript, registered_person_names)
        )
    if all(str(item.get('speaker') or '').strip() in {'', 'SPEAKER_?'} for item in items):
        texts = [str(item.get('text') or '').strip() for item in items if str(item.get('text') or '').strip()]
        if not texts:
            return ""
        return '<p class="comment transcript-comment">{}</p>'.format(
            '<br>'.join(
                render_registered_person_names(text, registered_person_names)
                for text in texts
            )
        )
    lines = []
    plain_texts = []
    for item in items:
        speaker = str(item.get('speaker') or 'SPEAKER_?')
        speaker_class = re.sub(r'[^A-Za-z0-9_-]', '_', speaker)
        text = html.unescape(str(item.get('text') or ''))
        plain_texts.append(text)
        lines.append(
            '<div class="transcript-line speaker-{speaker_class}">'
            '<span class="transcript-speaker">{speaker}</span>'
            '<span class="transcript-time">{start}-{end}</span>'
            '<span class="transcript-text">{text}</span>'
            '</div>'.format(
                speaker_class=speaker_class,
                speaker=speaker,
                start=format_segment_time(item.get('start')),
                end=format_segment_time(item.get('end')),
                text=render_registered_person_names(text, registered_person_names),
            )
        )
    hidden_comment = '<p class="comment" style="display:none;">{}</p>'.format(
        '<br>'.join(html.escape(text) for text in plain_texts)
    )
    return hidden_comment + '<div class="transcript-lines">' + ''.join(lines) + '</div>'


def render_transcript_time_block(block, registered_person_names=None):
    """Render one transcript row for the virtual timeline payload."""
    start_seconds = int(block.get('start_seconds', 0) or 0)
    transcript_html = render_transcript_lines(block, registered_person_names)
    return (
        f'<div class="time-block" id="time_block_{start_seconds}" '
        f'style="position: relative; height: {DEFAULT_TIMELINE_BLOCK_HEIGHT}px;">'
        f'<strong>{block.get("time_range") or format_time_range(start_seconds, start_seconds + 10)}</strong>'
        f'{transcript_html}'
        '<div class="score-container">'
        f'<span class="center-score">center:{block.get("center_score", 0)}</span>'
        f'<span class="positive-score">positive:{block.get("positive_score", 0)}</span>'
        f'<span class="negative-score">negative:{block.get("negative_score", 0)}</span>'
        '</div>'
        '<div class="play-button">PLAY▶</div>'
        '<div class="img_container">'
        f'<img loading="lazy" decoding="async" fetchpriority="low" '
        f'src="{block.get("screenshot_path", "")}" '
        f'alt="動画のスクリーンショット {start_seconds}秒">'
        '</div>'
        '<div class="nico-jump"><button>タイムシフトにジャンプ</button></div>'
        '</div>'
    )


def render_comment_time_block(time_second, comment_block=None, comments_fetch_failed=False):
    """Render one comment row for the virtual timeline payload."""
    comments = (comment_block or {}).get('comments') or []
    comment_html = []
    for comment in comments:
        user_name = comment.get('user_name', '')
        user_url = comment.get('user_url', '')
        user_display = (
            f'<a href="{user_url}" target="_blank">{user_name}</a>'
            if user_url else str(user_name)
        )
        comment_html.append(
            f'<p class="comment-item" data-comment-seconds="{comment.get("seconds", 0)}">'
            f'{comment.get("index", "")} | {comment.get("time", "")} - {user_display} : '
            f'<img loading="lazy" decoding="async" src="{comment.get("icon_url", "")}" '
            'style="width: 20px; height: 20px; vertical-align: middle; margin-left: 5px;" '
            "onerror=\"this.onerror=null; this.src='https://secure-dcdn.cdn.nimg.jp/"
            "nicoaccount/usericon/defaults/blank.jpg';\">"
            f'{comment.get("text", "")}<br></p>'
        )
    if not comment_html:
        empty_text = 'コメント取得失敗のため未収録' if comments_fetch_failed else 'コメントなし'
        comment_html.append(
            '<p class="comment-empty" style="color: #999; font-style: italic; '
            f'text-align: center; margin-top: 50px;">{empty_text}</p>'
        )
    return (
        f'<div class="time-block" id="time_block_{time_second}" '
        f'style="height: {DEFAULT_TIMELINE_BLOCK_HEIGHT}px;">'
        f'<strong>{format_time_range(time_second, time_second + 10)}</strong>'
        f'<div class="comment-list">{"".join(comment_html)}</div>'
        '</div>'
    )


def build_virtual_timeline_payload(
    transcript_blocks,
    comment_blocks,
    registered_person_names=None,
    comments_fetch_failed=False,
):
    """Build complete row HTML while keeping it outside the initial live DOM."""
    ordered_transcripts = sorted(
        transcript_blocks,
        key=lambda block: int(block.get('start_seconds', 0) or 0),
    )
    comment_by_second = {
        int(block.get('start_seconds', 0) or 0): block
        for block in comment_blocks
    }
    all_time_seconds = sorted({
        *(
            int(block.get('start_seconds', 0) or 0)
            for block in ordered_transcripts
        ),
        *comment_by_second.keys(),
    })
    return {
        'timeline1': [
            render_transcript_time_block(block, registered_person_names)
            for block in ordered_transcripts
        ],
        'timeline2': [
            render_comment_time_block(
                second,
                comment_by_second.get(second),
                comments_fetch_failed,
            )
            for second in all_time_seconds
        ],
    }


def serialize_json_for_html_script(value):
    """Serialize JSON without allowing payload text to close the script tag."""
    return (
        json.dumps(value, ensure_ascii=False, separators=(',', ':'))
        .replace('</', '<\\/')
        .replace('\u2028', '\\u2028')
        .replace('\u2029', '\\u2029')
    )


def render_virtual_timeline_host(timeline_id):
    return (
        f'<div class="virtual-timeline-host" data-virtual-timeline="{timeline_id}">'
        '<div class="virtual-spacer virtual-spacer-top"></div>'
        '<div class="virtual-window"></div>'
        '<div class="virtual-spacer virtual-spacer-bottom"></div>'
        '</div>'
    )


def build_virtual_timeline_script(lv_value):
    """Return the browser-side bounded-DOM timeline renderer."""
    script = r'''
    <script>
    (function () {
        const dataNode = document.getElementById("nico-virtual-timeline-data");
        if (!dataNode) return;
        let timelineData = {};
        try {
            timelineData = JSON.parse(dataNode.textContent || "{}");
        } catch (error) {
            console.error("仮想タイムラインデータを読み込めません", error);
        }
        dataNode.textContent = "";

        const windowSize = __WINDOW_SIZE__;
        const bufferBefore = __BUFFER_BEFORE__;
        const blockGap = __BLOCK_GAP__;
        const liveValue = __LV_VALUE__;
        let blockHeight = __BLOCK_HEIGHT__;
        let pitch = blockHeight + blockGap;
        let renderedStart = -1;
        let renderedEnd = -1;
        let ticking = false;
        const total = Math.max(
            (timelineData.timeline1 || []).length,
            (timelineData.timeline2 || []).length
        );

        function hosts() {
            return Array.from(document.querySelectorAll("[data-virtual-timeline]"));
        }

        function clampStart(start) {
            if (total <= 0) return 0;
            return Math.max(0, Math.min(Math.max(0, total - windowSize), start));
        }

        function render(start, force) {
            start = clampStart(Number.parseInt(start, 10) || 0);
            const end = Math.min(total, start + windowSize);
            if (!force && start === renderedStart && end === renderedEnd) return;

            hosts().forEach((host) => {
                const id = host.dataset.virtualTimeline;
                const rows = timelineData[id] || [];
                const localEnd = Math.min(rows.length, end);
                host.style.setProperty("--virtual-block-height", blockHeight + "px");
                host.querySelector(".virtual-spacer-top").style.height = (start * pitch) + "px";
                host.querySelector(".virtual-window").innerHTML = rows.slice(start, localEnd).join("");
                host.querySelector(".virtual-spacer-bottom").style.height = (
                    Math.max(0, rows.length - localEnd) * pitch
                ) + "px";
            });

            renderedStart = start;
            renderedEnd = end;
            document.dispatchEvent(new CustomEvent("nico-virtual-window-rendered", {
                detail: { start, end, total, blockHeight }
            }));
        }

        function contentTop() {
            const host = document.querySelector('[data-virtual-timeline="timeline1"]');
            return host ? host.getBoundingClientRect().top + window.scrollY : 0;
        }

        function renderForViewport() {
            ticking = false;
            if (total <= 0) return;
            const firstVisible = Math.floor(
                Math.max(0, window.scrollY - contentTop()) / pitch
            );
            render(firstVisible - bufferBefore, false);
        }

        function scheduleRender() {
            if (ticking) return;
            ticking = true;
            window.requestAnimationFrame(renderForViewport);
        }

        function renderSecond(second, scrollIntoView) {
            if (total <= 0) return null;
            const index = Math.max(
                0,
                Math.min(total - 1, Math.floor(Number(second || 0) / 10))
            );
            const blockSecond = index * 10;
            render(index - bufferBefore, false);
            const selector = '#timeline1 .time-block[id="time_block_' + blockSecond + '"]';
            const target = document.querySelector(selector);
            if (target && scrollIntoView) {
                window.requestAnimationFrame(() => {
                    const current = document.querySelector(selector);
                    if (current) current.scrollIntoView({ behavior: "smooth", block: "center" });
                });
            }
            return target;
        }

        function setBlockHeight(value) {
            const nextHeight = Math.max(100, Number.parseInt(value, 10) || __BLOCK_HEIGHT__);
            if (nextHeight === blockHeight) return;
            blockHeight = nextHeight;
            pitch = blockHeight + blockGap;
            render(renderedStart < 0 ? 0 : renderedStart, true);
            scheduleRender();
        }

        function transcriptText(second) {
            const rows = timelineData.timeline1 || [];
            if (!rows.length) return "";
            const index = Math.max(0, Math.min(rows.length - 1, Math.floor(Number(second || 0) / 10)));
            const template = document.createElement("template");
            template.innerHTML = rows[index] || "";
            const comment = template.content.querySelector(".comment, .transcript-lines");
            return comment ? (comment.textContent || "").trim() : "";
        }

        window.NicoVirtualTimeline = {
            renderSecond,
            renderIndex(index, scrollIntoView) {
                return renderSecond((Number(index) || 0) * 10, scrollIntoView);
            },
            setBlockHeight,
            getBlockHeight() {
                return blockHeight;
            },
            getMaxSecond() {
                return Math.max(0, (total - 1) * 10);
            },
            transcriptText,
            stats() {
                return {
                    totalPerTimeline: total,
                    renderedPerTimeline: Math.max(0, renderedEnd - renderedStart),
                    renderedStart,
                    renderedEnd
                };
            }
        };

        function init() {
            render(0, true);
            window.addEventListener("scroll", scheduleRender, { passive: true });
            window.addEventListener("resize", scheduleRender, { passive: true });
        }
        if (document.readyState === "loading") {
            document.addEventListener("DOMContentLoaded", init, { once: true });
        } else {
            init();
        }

        document.addEventListener("click", function (event) {
            const target = event.target;
            if (!(target instanceof Element)) return;
            const block = target.closest("[data-virtual-timeline] .time-block");
            if (!block) return;
            const seconds = Number.parseInt(block.id.replace("time_block_", ""), 10) || 0;
            if (target.closest(".play-button")) {
                event.preventDefault();
                const audio = document.getElementById("audioPlayer");
                const seekbar = document.getElementById("seekbar");
                if (audio) {
                    audio.currentTime = seconds;
                    if (seekbar) seekbar.value = String(seconds);
                    audio.play();
                }
            } else if (target.closest(".nico-jump button")) {
                event.preventDefault();
                window.open("https://live.nicovideo.jp/watch/" + liveValue + "#" + seconds, "_blank");
            }
        });
    })();
    </script>
'''
    return (
        script
        .replace('__WINDOW_SIZE__', str(VIRTUAL_TIMELINE_WINDOW_SIZE))
        .replace('__BUFFER_BEFORE__', str(VIRTUAL_TIMELINE_BUFFER_BEFORE))
        .replace('__BLOCK_GAP__', str(TIMELINE_BLOCK_VERTICAL_GAP))
        .replace('__BLOCK_HEIGHT__', str(DEFAULT_TIMELINE_BLOCK_HEIGHT))
        .replace('__LV_VALUE__', json.dumps(str(lv_value), ensure_ascii=False))
    )


def generate_complete_html(
    timeline_data,
    broadcast_data,
    word_ranking,
    comment_ranking,
    ai_chats,
    config,
    lv_value,
    broadcast_dir,
    registered_person_names=None,
):
    """完全版HTMLを生成（全機能統合）"""
    try:
        # timeline_dataから文字起こしとコメントブロックを取得
        transcript_blocks = timeline_data['transcript_blocks']
        comment_blocks = timeline_data['comment_blocks']
        
        html_parts = []
        
        sentiment_stats = broadcast_data.get('sentiment_stats', {})
        music_data = broadcast_data.get('music_generation', {})
        image_data = broadcast_data.get('image_generation', {})
        timeline_audio_src = select_timeline_audio_source(broadcast_dir, lv_value)
        display_features = config.get('display_features', {})
        show_emotion_scores = display_features.get('enable_emotion_scores', True)
        show_word_ranking = display_features.get('enable_word_ranking', True)
        show_thumbnails = display_features.get('enable_thumbnails', True)
        show_audio_timeline = display_features.get('enable_audio_timeline', True)
        show_timeline_html = display_features.get('enable_timeline_html', True)
        show_comment_ranking = display_features.get('enable_comment_ranking', True)
        has_ai_conversation = bool(ai_chats.get('intro') or ai_chats.get('outro'))
        fullbody_assets = prepare_ai_fullbody_assets(config)
        try:
            canonical_duration = max(0.0, float(broadcast_data.get('video_duration', 0) or 0.0))
        except (TypeError, ValueError):
            canonical_duration = 0.0
        
        # JavaScript用データ準備
        emotion_chart_series = build_emotion_chart_series(
            transcript_blocks,
            timeline_data.get('recording_segment_timeline') or {},
        )
        segments_js = json.dumps(emotion_chart_series['segments'], ensure_ascii=False)
        positive_data_js = json.dumps(emotion_chart_series['positive'], ensure_ascii=False)
        center_data_js = json.dumps(emotion_chart_series['center'], ensure_ascii=False)
        negative_data_js = json.dumps(emotion_chart_series['negative'], ensure_ascii=False)
        speaker_emotion_data = build_speaker_emotion_data(
            transcript_blocks,
            timeline_data.get('recording_segment_timeline') or {},
        )
        speaker_emotion_json = json.dumps(speaker_emotion_data, ensure_ascii=False)
        
        # HTMLヘッダー
        feature_css = ""
        if not show_timeline_html:
            feature_css += (
                "\n        .container { display: none !important; }"
                "\n        #portrait-left-controls, #portrait-right-scroll { display: none !important; }"
            )
        if not show_audio_timeline:
            feature_css += (
                "\n        #controls-container { display: none !important; }"
                "\n        #portrait-right-scroll, #portrait-bottom-controls { display: none !important; }"
            )
        if not show_thumbnails:
            feature_css += (
                "\n        .img_container { display: none !important; }"
                "\n        #portraitThumbnailControl { display: none !important; }"
            )
        if not show_emotion_scores:
            feature_css += "\n        .score-container, .emotion-chart-card { display: none !important; }"
        display_features = config.get('display_features', {})
        thumbnail_width = max(1, int(display_features.get('thumbnail_width', 80) or 80))
        thumbnail_height = max(1, int(display_features.get('thumbnail_height', 60) or 60))
        ai_fullbody_css = ""
        if has_ai_conversation:
            ai_fullbody_css = """
        .ai-chat-section {
            background: transparent;
        }
        .ai-fullbody-backdrop {
            position: fixed;
            inset: 0;
            pointer-events: none;
            z-index: 0;
            overflow: hidden;
        }
        .ai-fullbody-backdrop img {
            position: fixed;
            --fullbody-zoom-scale: 1;
            top: var(--fullbody-top, 54px);
            height: 820px;
            width: auto;
            object-fit: contain;
            opacity: 0.96;
            filter: drop-shadow(0 22px 32px rgba(30, 20, 20, 0.20));
            transform: scale(var(--fullbody-zoom-scale));
            will-change: transform;
        }
        .ai-fullbody-backdrop .nini-fullbody {
            left: clamp(38px, 8vw, 230px);
            transform-origin: top left;
        }
        .ai-fullbody-backdrop .koko-fullbody {
            right: clamp(38px, 8vw, 230px);
            transform-origin: top right;
        }
        body > *:not(.ai-fullbody-backdrop) {
            position: relative;
            z-index: 1;
        }"""
        html_parts.append(f"""<!DOCTYPE html>
<html lang="ja">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{html.escape(broadcast_data.get('live_title', ''))}</title>
    <link rel="stylesheet" href="css/archive-style.css" />
    <style>
        @import url('https://fonts.googleapis.com/css2?family=Zen+Antique&display=swap');
        body {{ font-family: Arial, sans-serif; margin: 20px; line-height: 1.6; }}
        .header {{ background: transparent; padding: 20px; margin-bottom: 20px; border-radius: 5px; }}
        .broadcast-link {{ display: block; }}
        .broadcast-heading {{ text-align: center; margin-top: 28px; }}
        .broadcast-lv,
        .broadcast-title {{
            font-family: 'Zen Antique', serif;
            -webkit-text-stroke: 2px rgba(255, 255, 255, 0.95);
            paint-order: stroke fill;
            text-shadow: 0 4px 10px rgba(0, 0, 0, 0.48), 0 1px 2px rgba(0, 0, 0, 0.65);
        }}
        .broadcast-lv {{ font-size: clamp(36px, 4vw, 56px); font-weight: 800; line-height: 1.1; }}
        .broadcast-title {{ margin: 10px 0 0; font-size: clamp(56px, 7vw, 96px); font-weight: 900; line-height: 1.05; }}
        .stats {{ display: flex; gap: 20px; margin: 10px 0; flex-wrap: wrap; }}
        .stat-item {{ background: white; padding: 10px; border-radius: 3px; border-left: 3px solid #007cba; flex: 1; min-width: 150px; }}
        .section {{ margin: 30px 0; padding: 20px; background: #fafafa; border-radius: 5px; }}
        .section h2 {{ color: #333; border-bottom: 2px solid #007cba; padding-bottom: 10px; }}
        {feature_css}
        .chat-container {{
            margin: 20px auto; 
            max-width: 1200px;
            padding: 0 20px; 
        }}

        .chat-message {{ 
            display: flex; 
            margin: 15px 0; 
            align-items: flex-start; 
            gap: 10px; 
            max-width: 1000px;
            margin-left: auto; 
            margin-right: auto; 
        }}

        /* スマホ対応 */
        @media (max-width: 768px) {{
            .chat-container {{
                max-width: 100%;
                padding: 0 10px;
            }}
            
            .chat-message {{
                max-width: 100%;
            }}
        }}
        .chat-avatar {{ width: 120px; height: 120px; border-radius: 50%; object-fit: cover; flex: 0 0 120px; }}
        .chat-bubble {{ background: #e3f2fd; padding: 10px 15px; border-radius: 15px; max-width: 70%; }}
        .ranking-list {{ list-style: none; padding: 0; }}
        .ranking-item {{
            background: white;
            margin: 10px 0;
            padding: 15px;
            border-radius: 5px;
            border-left: 4px solid #007cba; /* デフォルト色（青） */
        }}
        /* 1〜3位だけ色変更 */
        .rank-1 {{
            border-left-color: gold;       /* 金メダル風 */
        }}
        .rank-2 {{
            border-left-color: silver;     /* 銀メダル風 */
        }}
        .rank-3 {{
            border-left-color: #cd7f32;    /* ブロンズ */
        }}
        .word-list {{ display: flex; flex-wrap: wrap; gap: 10px; }}
        .word-item {{ background: #007cba; color: white; padding: 5px 10px; border-radius: 15px; }}
        .summary-section {{
            background: white;                    /* 背景色を白に設定 */
            color: #333;                         /* 文字色を濃いグレーに設定 */
            padding: 30px;                       /* 内側の余白を上下左右30px */
            border-radius: 10px;                 /* 角を10px丸める */
            border: 1px solid #ddd;              /* 1px幅の薄いグレーの枠線 */
            box-shadow: 0 2px 4px rgba(0,0,0,0.1); /* 軽い影をつける（右に0px、下に2px、ぼかし4px、10%透明の黒） */
        }}
        .audio-player {{ margin: 20px 0; }}
        .summary-image {{ text-align: center; margin: 20px 0; }}
        .summary-image img {{ width: 100%; max-width: 1000px; height: auto; box-sizing: border-box; border-radius: 10px; box-shadow: 0 4px 8px rgba(0,0,0,0.3); }}
        
        .container {{ display: flex; gap: 20px; margin: 20px 0; }}
        .timeline {{ flex: 1; min-width: 0; background: #fff; border-radius: 8px; padding: 10px; }}
        .timeline h2 {{ text-align: center; margin-bottom: 20px; }}
        .virtual-timeline-host {{
            --virtual-block-height: {DEFAULT_TIMELINE_BLOCK_HEIGHT}px;
            position: relative;
            width: 100%;
            min-width: 0;
            overflow-anchor: none;
        }}
        .virtual-timeline-host * {{
            overflow-anchor: none;
        }}
        .virtual-window {{
            display: flex;
            flex-direction: column;
        }}
        .virtual-window .time-block {{
            flex: 0 0 auto;
            height: var(--virtual-block-height) !important;
            margin: 5px 0 !important;
        }}
        .virtual-spacer {{
            display: block;
            width: 1px;
            height: 0;
            pointer-events: none;
        }}
        .time-block {{ 
            position: relative; 
            height: 180px; 
            border: 1px solid #ddd; 
            margin: 10px 0; 
            padding: 10px; 
            border-radius: 5px; 
            overflow: hidden;
            background: #fff;
            box-sizing: border-box;
        }}
        .time-block strong {{ 
            display: block; 
            font-size: 1.1em; 
            color: #007cba; 
            margin-bottom: 10px; 
        }}
        .comment {{ 
            background: #f0f8ff; 
            padding: 8px; 
            margin: 5px 0; 
            border-radius: 3px; 
            font-size: 0.9em;
            max-height: 80px;
            overflow-y: auto;
        }}
        #timeline1 .transcript-comment {{
            flex: 1 1 auto;
            min-height: 0;
            max-height: none;
            overflow-y: auto;
            position: relative;
            z-index: 2;
            background: transparent;
            margin-top: 0;
            padding-top: 0;
            font-weight: 700;
            text-shadow:
                -1px -1px 0 rgba(255, 255, 255, 0.95),
                 0 -1px 0 rgba(255, 255, 255, 0.95),
                 1px -1px 0 rgba(255, 255, 255, 0.95),
                -1px  0 0 rgba(255, 255, 255, 0.95),
                 1px  0 0 rgba(255, 255, 255, 0.95),
                -1px  1px 0 rgba(255, 255, 255, 0.95),
                 0  1px 0 rgba(255, 255, 255, 0.95),
                 1px  1px 0 rgba(255, 255, 255, 0.95),
                 0 2px 3px rgba(0, 0, 0, 0.55);
        }}
        #timeline1 .registered-person-name {{
            font-size: 1.4em;
            font-weight: 900;
        }}
        #timeline1 .time-block {{
            display: flex;
            flex-direction: column;
        }}
        #timeline1 .time-block > strong {{
            flex: 0 0 auto;
            margin-bottom: 0;
        }}
        .score-container {{ 
            margin: 5px 0; 
            font-size: 0.8em; 
        }}
        #timeline1 .score-container {{
            position: absolute;
            left: 155px;
            bottom: 8px;
            margin: 0;
            font-size: 0.78em;
            white-space: nowrap;
            z-index: 3;
        }}
        .center-score {{ color: #2196F3; font-weight: bold; }}
        .positive-score {{ color: #4CAF50; font-weight: bold; }}
        .negative-score {{ color: #F44336; font-weight: bold; }}
        .play-button {{ 
            position: absolute; 
            top: 5px; 
            right: 5px; 
            background: #007cba; 
            color: white; 
            padding: 5px 10px; 
            border-radius: 3px; 
            cursor: pointer; 
            font-size: 0.8em;
            z-index: 4;
        }}
        .img_container {{
            position: absolute; 
            bottom: 22px;
            right: 32px;
            width: {thumbnail_width}px;
            height: {thumbnail_height}px;
            z-index: 1;
            opacity: 1;
        }}
        .img_container img {{ 
            width: 100%; 
            height: 100%; 
            box-sizing: border-box;
            object-fit: cover; 
            border-radius: 3px; 
            border: 1px solid #ddd;
        }}
        .nico-jump {{ 
            position: absolute; 
            left: 5px; 
            bottom: 5px; 
            z-index: 4;
        }}
        .nico-jump button {{ 
            background: #ff6b35; 
            color: white; 
            border: none; 
            padding: 3px 8px; 
            border-radius: 3px; 
            font-size: 0.7em; 
            cursor: pointer;
        }}
        .comment-list {{ 
            overflow-y: auto; 
            font-size: 0.8em;
        }}
        #timeline2 .time-block {{
            display: flex;
            flex-direction: column;
            box-sizing: border-box;
            overflow: hidden;
        }}
        #timeline2 .time-block strong {{
            flex: 0 0 auto;
        }}
        #timeline2 .time-block .comment-list {{
            flex: 1 1 auto;
            min-height: 0;
            max-height: none;
            overflow-y: auto;
        }}
        .comment-item {{ 
            margin: 3px 0; 
            padding: 3px; 
            border-bottom: 1px dotted #ccc; 
        }}
        .comment-item:last-child {{ border-bottom: none; }}
        .flash-fade-out {{ border: 3px solid #ff6b35 !important; transition: border 1s ease-out; }}
        .transcript-lines {{
            display: flex;
            flex-direction: column;
            gap: 6px;
            margin: 8px 0 10px;
            max-height: 108px;
            overflow-y: auto;
        }}
        #timeline1 .transcript-lines {{
            position: relative;
            z-index: 2;
        }}
        .transcript-line {{
            display: grid;
            grid-template-columns: auto auto 1fr;
            gap: 8px;
            align-items: baseline;
            padding: 7px 9px;
            border-radius: 8px;
            background: rgba(255,255,255,0.78);
            border-left: 4px solid #999;
            box-shadow: 0 1px 4px rgba(0,0,0,0.08);
            line-height: 1.55;
        }}
        .transcript-speaker {{
            font-weight: 700;
            font-size: 12px;
            white-space: nowrap;
            color: #222;
        }}
        .transcript-time {{
            color: #666;
            font-size: 12px;
            white-space: nowrap;
        }}
        .transcript-text {{
            color: #111;
            overflow-wrap: anywhere;
        }}
        .speaker-SPEAKER_00 {{ border-left-color: #2196f3; background: rgba(227,242,253,0.86); }}
        .speaker-SPEAKER_01 {{ border-left-color: #e91e63; background: rgba(252,228,236,0.86); }}
        .speaker-SPEAKER_02 {{ border-left-color: #43a047; background: rgba(232,245,233,0.86); }}
        .speaker-SPEAKER_03 {{ border-left-color: #fb8c00; background: rgba(255,243,224,0.86); }}
        
        #controls-container {{
            position: fixed;
            bottom: 20px;
            left: 20px;
            right: 20px;
            background: white;
            border: 2px solid #007cba;
            border-radius: 10px;
            padding: 10px;
            box-shadow: 0 4px 8px rgba(0,0,0,0.2);
            z-index: 1000;
            display: flex;
            flex-wrap: wrap;
            align-items: center;
            gap: 15px;
        }}
        #controls-container audio {{ flex: 1; margin: 0; }}
        #seekbar {{ flex: 1; margin: 0; }}
        #controls-container label, #controls-container input[type="checkbox"] {{ margin: 0; }}
        .thumbnail-size-control {{
            display: flex;
            align-items: center;
            gap: 6px;
            white-space: nowrap;
        }}
        .thumbnail-size-control input[type="range"] {{
            width: 160px;
        }}
        #thumbnailSizeStatus {{
            min-width: 7em;
            color: #555;
            font-size: 12px;
        }}
        #portrait-left-controls,
        #portrait-right-scroll,
        #portrait-bottom-controls {{
            display: none;
        }}
        @media (orientation: portrait) {{
            #controls-container {{
                display: none !important;
            }}
            .container {{
                display: flex !important;
                flex-direction: row !important;
                flex-wrap: nowrap !important;
                align-items: flex-start !important;
                gap: 8px;
            }}
            body #timeline1,
            body #timeline2 {{
                display: block !important;
                visibility: visible !important;
                flex: 1 1 0 !important;
                width: 50% !important;
                min-width: 0 !important;
            }}
            #portrait-left-controls,
            #portrait-right-scroll {{
                position: fixed;
                z-index: 10020;
                display: flex;
                background: transparent;
                border: 0;
                border-radius: 0;
                box-shadow: none;
                pointer-events: none;
            }}
            #portrait-left-controls {{
                left: 0;
                top: 8vh;
                bottom: 8vh;
                width: 34px;
                flex-direction: column;
                align-items: center;
                justify-content: space-around;
                padding: 0;
            }}
            .portrait-side-control {{
                display: flex;
                flex-direction: column;
                align-items: center;
                gap: 4px;
                font-size: 11px;
                font-weight: 700;
            }}
            .portrait-side-control input[type="range"] {{
                width: 24px;
                height: 27vh;
                writing-mode: vertical-lr;
                direction: rtl;
                -webkit-appearance: slider-vertical;
                pointer-events: auto;
            }}
            .portrait-side-control output {{
                font-size: 10px;
                color: #444;
            }}
            #portrait-right-scroll {{
                right: 0;
                top: 8vh;
                bottom: 8vh;
                width: 30px;
                align-items: center;
                justify-content: center;
                padding: 0;
            }}
            #portraitSeekbar {{
                width: 26px;
                height: 72vh;
                writing-mode: vertical-lr;
                direction: ltr;
                -webkit-appearance: slider-vertical;
                pointer-events: auto;
            }}
            #portrait-bottom-controls {{
                position: fixed;
                left: 50%;
                bottom: 0;
                z-index: 10030;
                display: flex;
                align-items: center;
                gap: 8px;
                padding: 5px 10px;
                transform: translateX(-50%);
                background: rgba(255, 255, 255, 0.9);
                border-radius: 8px 8px 0 0;
                box-shadow: 0 -2px 8px rgba(0, 0, 0, 0.2);
                white-space: nowrap;
            }}
            #portrait-bottom-controls label {{
                display: flex;
                align-items: center;
                gap: 4px;
                font-size: 12px;
                font-weight: 700;
            }}
            #portrait-bottom-controls button {{
                border: 0;
                border-radius: 5px;
                padding: 5px 10px;
                color: #fff;
                font-weight: 700;
                cursor: pointer;
            }}
            #portraitPlayButton {{ background: #0787c1; }}
            #portraitStopButton {{ background: #d94b43; }}
        }}
        
        #gaugeBarContainer {{
            position: fixed;
            top: 20px;
            right: 20px;
            background: white;
            border: 1px solid #ddd;
            border-radius: 5px;
            padding: 10px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
            z-index: 1000;
        }}
        
        .graph-container {{ margin: 20px 0; text-align: left; }}
        .emotion-zoom-controls {{
            display: flex;
            align-items: center;
            gap: 10px;
            margin: 10px 0;
            font-weight: 700;
        }}
        .emotion-zoom-controls input {{ width: min(420px, 55vw); }}
        .emotion-graph-scroll {{
            overflow-x: auto;
            overflow-y: hidden;
            border: 1px solid #d8e7f3;
            border-radius: 8px;
            background: #fff;
            cursor: grab;
            user-select: none;
        }}
        .emotion-graph-scroll.dragging {{ cursor: grabbing; }}
        .emotion-graph-inner {{ width: 100%; min-width: 800px; }}
        .emotion-graph-inner canvas {{
            display: block;
            height: 300px !important;
            max-height: 300px !important;
        }}
        .emotion-tabs {{ display: flex; gap: 8px; flex-wrap: wrap; margin: 8px 0 14px; }}
        .emotion-tab {{
            border: 1px solid #2196f3;
            background: #fff;
            color: #0b73b7;
            border-radius: 999px;
            padding: 6px 12px;
            cursor: pointer;
            font-weight: 700;
        }}
        .emotion-tab.active {{ background: #2196f3; color: #fff; }}
        # HTMLヘッダーのスタイル部分に追加
        .ranking-header {{
            display: flex;
            align-items: center;
            margin-bottom: 5px;
        }}
        .ranking-summary {{
            margin-bottom: 10px;
        }}
        .toggle-comments-btn:hover {{
            background-color: #005a8a;
        }}
        .comment-entry:last-child {{
            border-bottom: none;
        }}
        .flip-horizontal {{
        transform: scaleX(-1);
        }}
        .char1-bubble {{
            background: #e3f2fd; /* 青系 */
            border-left: 3px solid #2196f3;
        }}
        .char2-bubble {{
            background: #fce4ec; /* 薄いピンク */
            border-right: 3px solid #e91e63;
        }}
        .flip-horizontal {{
            transform: scaleX(-1);
        }}
        .section {{
            margin-bottom: 100px;
        }}
        {ai_fullbody_css}
    </style>
</head>
<body>
    {f'''<div class="ai-fullbody-backdrop" aria-hidden="true">
        <img class="nini-fullbody" src="{fullbody_assets['char1_url']}" alt="">
        <img class="koko-fullbody" src="{fullbody_assets['char2_url']}" alt="">
    </div>''' if has_ai_conversation else ''}
    {'''<script>
      (function () {
        const baseDpr = window.devicePixelRatio || 1;
        function lockFullbodyVisualSize() {
          const currentDpr = window.devicePixelRatio || baseDpr;
          const scale = baseDpr / currentDpr;
          document.querySelectorAll('.ai-fullbody-backdrop img').forEach((img) => {
            img.style.setProperty('--fullbody-zoom-scale', String(scale));
          });
        }
        lockFullbodyVisualSize();
        window.addEventListener('resize', lockFullbodyVisualSize, { passive: true });
        if (window.visualViewport) {
          window.visualViewport.addEventListener('resize', lockFullbodyVisualSize, { passive: true });
        }
        setInterval(lockFullbodyVisualSize, 250);
      })();
    </script>''' if has_ai_conversation else ''}
    <script>
      window.NICO_ARCHIVE_CONFIG = {{
          lvValue: "{lv_value}",
          duration: {canonical_duration:.6f},
          segments: [{segments_js}],
          emotionData: {{
              positive: [{positive_data_js}],
              center: [{center_data_js}],
              negative: [{negative_data_js}]
          }},
          screenshotPath: "./screenshot/{lv_value}",
          broadcast: {{
              title: "{html.escape(broadcast_data.get('live_title', ''))}",
              broadcaster: "{html.escape(broadcast_data.get('broadcaster', ''))}",
              community: "{broadcast_data.get('default_community', '')}"
          }}
      }};
    </script>
""")

        # JST (UTC+9) のタイムゾーンを定義
        jst = timezone(timedelta(hours=9))

        # ヘッダー情報のHTML生成部分を修正
        start_time_jst = datetime.fromtimestamp(int(broadcast_data.get('start_time', 0)), tz=jst)
        end_time_jst = datetime.fromtimestamp(int(broadcast_data.get('end_time', 0)), tz=jst)

        html_parts.append(f"""
            <div class="header">
                <div class="stats">
                    <div class="stat-item">
                        <strong>配信者:</strong> {html.escape(broadcast_data.get('broadcaster', ''))}
                    </div>
                    <div class="stat-item">
                        <strong>開始時間:</strong> {start_time_jst.strftime('%Y/%m/%d %H:%M')}
                    </div>
                    <div class="stat-item">
                        <strong>終了時間:</strong> {end_time_jst.strftime('%Y/%m/%d %H:%M')}
                    </div>
                    <div class="stat-item">
                        <strong>来場者数:</strong> {broadcast_data.get('watch_count', '0')}人
                    </div>
                    <div class="stat-item">
                        <strong>コメント数:</strong> {broadcast_data.get('comment_count', '0')}コメ
                    </div>
                </div>
                <a class="broadcast-link" href="https://live.nicovideo.jp/watch/{html.escape(str(lv_value), quote=True)}" target="_blank" rel="noopener noreferrer">
                    <div class="broadcast-heading">
                        <div class="broadcast-lv">{html.escape(str(lv_value))}</div>
                        <h1 class="broadcast-title">{html.escape(broadcast_data.get('live_title', ''))}</h1>
                    </div>
                </a>
            </div>
        """)

        # 要約画像は開始前会話の直前に置く。開始前会話がない放送でも
        # ヘッダー直後の同じ位置を維持する。
        if image_data.get('imgur_url'):
            summary_image_url = html.escape(str(image_data['imgur_url']), quote=True)
            html_parts.append(f"""
        <div class="summary-image">
            <a href="{summary_image_url}" target="_blank">
                <img src="{summary_image_url}" alt="配信の抽象化イメージ">
            </a>
        </div>
""")

        # 開始前AI会話
        if ai_chats['intro']:
            html_parts.append("""
        <div class="section ai-chat-section">
            <h2>開始前会話</h2>
            <div class="chat-container">
        """)
            char1_name = config.get('ai_prompts', {}).get('character1_name', 'ニニちゃん')
            char2_name = config.get('ai_prompts', {}).get('character2_name', 'ココちゃん')
            
            for i, chat in enumerate(ai_chats['intro']):
                side = 'left' if i % 2 == 0 else 'right'
                flip_class = ' flip-horizontal' if chat.get('flip', False) else ''
                
                # キャラクターごとに異なるCSSクラスを適用
                if chat['name'] == char1_name:
                    bubble_class = 'chat-bubble char1-bubble'
                elif chat['name'] == char2_name:
                    bubble_class = 'chat-bubble char2-bubble'
                else:
                    bubble_class = 'chat-bubble'
                    
                html_parts.append(f"""
                <div class="chat-message" style="flex-direction: {'row' if side == 'left' else 'row-reverse'};">
                    <img src="{chat['icon']}" alt="{chat['name']}" class="chat-avatar{flip_class}" onerror="this.style.display='none'">
                    <div class="{bubble_class}">
                        <strong>{chat['name']}:</strong><br>
                        {chat['dialogue']}
                    </div>
                </div>
        """)
            html_parts.append("    </div>\n</div>\n")

        # コメントランキング部分の修正版
        if show_comment_ranking:
            html_parts.append("""
                    <div class="section">
                        <h2>🏆 コメントランキング（10位まで）</h2>
                        <ul class="ranking-list">
                    """)
            if not comment_ranking:
                html_parts.append("""
                        <li class="ranking-item rank-empty">
                            コメントはありません
                        </li>
                    """)
            for user in comment_ranking:
                # ランク別のクラスと見た目設定
                rank_class_map = {
                    1: ("rank-1", 60, "1.4em"),
                    2: ("rank-2", 45, "1.2em"),
                    3: ("rank-3", 36, "1.1em"),
                }
                rank_class, img_size, font_size = rank_class_map.get(user['rank'], ("rank-other", 30, "1em"))

                user_display = (
                    f'<a href="{user["user_url"]}" target="_blank">{user["user_name"]}</a>'
                    if user['user_url'] else user['user_name']
                )

                html_parts.append(f"""
                        <li class="ranking-item {rank_class}">
                            <div class="ranking-header" style="font-size:{font_size};">
                                <strong>{user['rank']}位:</strong>
                                <img src="{user['icon_url']}"
                                    style="width:{img_size}px; height:{img_size}px; border-radius:50%; vertical-align:middle; margin:0 5px;"
                                    onerror="this.onerror=null; this.src='https://secure-dcdn.cdn.nimg.jp/nicoaccount/usericon/defaults/blank.jpg';">
                                {user_display} - {user['comment_count']}コメント
                                <button class="toggle-comments-btn" data-user-id="{user['user_id']}"
                                    style="margin-left:10px; padding:3px 8px; background:#007cba; color:white; border:none; border-radius:3px; cursor:pointer; font-size:0.8em;">
                                    全コメント表示
                                </button>
                            </div>
                            <div class="ranking-summary">
                                <small>初コメント ({user['first_comment_time']}): {user['first_comment']}</small><br>
                                <small>最終コメント ({user['last_comment_time']}): {user['last_comment']}</small>
                            </div>
                            <div class="user-comments" id="comments-{user['user_id']}"
                                style="display:none; margin-top:10px; max-height:300px; overflow-y:auto; background:#f8f9fa; padding:10px; border-radius:5px;">
                        """)

                # ユーザーの全コメントを表示
                for comment in user.get('comments', []):
                    html_parts.append(f"""
                                <div class="comment-entry" style="margin: 5px 0; padding: 5px; border-bottom: 1px dotted #ccc;">
                                    <span style="color: #666; font-size: 0.8em;">[{comment['time']}]</span>
                                    <span style="margin-left: 5px;">{comment['text']}</span>
                                </div>
                            """)

                html_parts.append("""
                            </div>
                        </li>
                        """)

            html_parts.append("""
                        </ul>
                    </div>
                    """)

        # 要約セクション
        summary_html = html.escape(broadcast_data.get('summary_text', '')).replace('\n', '<br>')
        emotion_summary_html = ""
        if show_emotion_scores:
            emotion_summary_html = f"""
        <p><strong>感情分析:</strong> 
           ポジティブ: {round(sentiment_stats.get('avg_positive', 0), 3)} | 
           センター: {round(sentiment_stats.get('avg_center', 0), 3)} | 
           ネガティブ: {round(sentiment_stats.get('avg_negative', 0), 3)}
        </p>
"""
        html_parts.append(f"""
    <div class="summary-section">
        <h2>要約</h2>
        <p><strong>要約:</strong><br>{summary_html}</p>
""")

        # AI音楽（複数曲対応）
        if music_data.get('songs'):
            html_parts.append("""
                <div class="audio-player">
                    <h3>要約を歌詞とした音楽</h3>
        """)
            
            for i, song in enumerate(music_data['songs']):
                if song.get('primary_url'):
                    song_title = f"楽曲 {i+1}"
                    html_parts.append(f"""
                    <div style="margin: 10px 0;">
                        <h4>{song_title}</h4>
                        <audio controls style="width: 100%;">
                            <source src="{song['primary_url']}" type="audio/mp3">
                        </audio>
                    </div>
        """)
            
            html_parts.append("        </div>\n")

        # 感情分析グラフ
        if show_emotion_scores:
            html_parts.append(f"""
        <div class="emotion-chart-card">
            <h3>感情分析グラフ</h3>
            <div class="emotion-tabs" id="emotion-tabs"></div>
            <div class="emotion-zoom-controls">
                <label for="emotion-zoom-range">時間幅</label>
                <input id="emotion-zoom-range" type="range" min="1" max="8" step="0.25" value="1">
                <span id="emotion-zoom-value">1.00x</span>
            </div>
            <div class="graph-container">
                <div class="emotion-graph-scroll" id="emotion-graph-scroll">
                    <div class="emotion-graph-inner" id="emotion-graph-inner"></div>
                </div>
            </div>
            {emotion_summary_html}
        </div>
""")
        html_parts.append("    </div>\n")

        # 単語ランキング
        if show_word_ranking and word_ranking:
            html_parts.append("""
    <div class="section">
        <h2>単語使用頻度ランキング</h2>
        <div class="word-list">
""")
            for word in word_ranking:
                html_parts.append(f"""
            <span class="word-item" style="font-size: {min(word['font_size'], 32)}px;">
                {word['word']}: {word['count']}回
            </span>
""")
            html_parts.append("        </div>\n    </div>\n")

        # 横並びタイムライン
        virtual_timeline_payload = build_virtual_timeline_payload(
            transcript_blocks,
            comment_blocks,
            registered_person_names,
            bool(timeline_data.get('comments_fetch_failed')),
        )
        virtual_timeline_json = serialize_json_for_html_script(
            virtual_timeline_payload
        )
        html_parts.append(f"""
    <div class="container">
        <!-- 放送者タイムライン -->
        <div class="timeline" id="timeline1">
            <h2>放送者文字おこしのタイムライン</h2>
            {render_virtual_timeline_host('timeline1')}
        </div>
        
        <!-- コメントタイムライン -->
        <div class="timeline" id="timeline2">
            <h2>コメントのタイムライン</h2>
            {render_virtual_timeline_host('timeline2')}
        </div>
    </div>
    <script id="nico-virtual-timeline-data" type="application/json">{virtual_timeline_json}</script>
""")

        # 終了後AI会話
        if ai_chats['outro']:
            html_parts.append("""
        <div class="section ai-chat-section">
            <h2>終了後会話</h2>
            <div class="chat-container">
        """)
            for i, chat in enumerate(ai_chats['outro']):
                side = 'left' if i % 2 == 0 else 'right'
                flip_class = ' flip-horizontal' if chat.get('flip', False) else ''
                
                # キャラクターごとに異なるCSSクラスを適用
                if chat['name'] == char1_name:
                    bubble_class = 'chat-bubble char1-bubble'
                elif chat['name'] == char2_name:
                    bubble_class = 'chat-bubble char2-bubble'
                else:
                    bubble_class = 'chat-bubble'
                    
                html_parts.append(f"""
                <div class="chat-message" style="flex-direction: {'row' if side == 'left' else 'row-reverse'};">
                    <img src="{chat['icon']}" alt="{chat['name']}" class="chat-avatar{flip_class}" onerror="this.style.display='none'">
                    <div class="{bubble_class}">
                        <strong>{chat['name']}:</strong><br>
                        {chat['dialogue']}
                    </div>
                </div>
        """)
            html_parts.append("    </div>\n</div>\n")

        # プレイヤーコントロール
        html_parts.append(f"""
    <div id="portrait-left-controls" aria-label="縦長画面用表示調整">
        <label class="portrait-side-control" for="portraitHeightRange">
            高さ
            <input id="portraitHeightRange" type="range" min="100" max="800" step="10" value="{DEFAULT_TIMELINE_BLOCK_HEIGHT}" />
            <output id="portraitHeightValue">{DEFAULT_TIMELINE_BLOCK_HEIGHT}</output>
        </label>
        <label class="portrait-side-control" id="portraitThumbnailControl" for="portraitThumbnailRange">
            サムネ
            <input id="portraitThumbnailRange" type="range" min="50" max="400" step="5" value="{thumbnail_width}" />
            <output id="portraitThumbnailValue">{thumbnail_width}</output>
        </label>
    </div>
    <div id="portrait-right-scroll" aria-label="縦長画面用タイムラインスクロール">
        <input id="portraitSeekbar" type="range" min="0" max="{canonical_duration:.6f}" step="10" value="0" aria-label="タイムラインスクロール" />
    </div>
    <div id="portrait-bottom-controls" aria-label="縦長画面用音声操作">
        <label for="portraitAutoJumpToggle">
            <input checked id="portraitAutoJumpToggle" type="checkbox" />
            Auto-Jump
        </label>
        <button id="portraitPlayButton" type="button">再生▶</button>
        <button id="portraitStopButton" type="button">停止■</button>
    </div>
    <div id="controls-container">
        <label for="autoJumpToggle">Auto-Jump:</label>
        <input checked id="autoJumpToggle" name="autoJumpToggle" type="checkbox" />
        <audio controls id="audioPlayer">
            <source src="{timeline_audio_src}" type="audio/mp3" />
            Your browser does not support the audio element.
        </audio>
        <input id="seekbar" max="{canonical_duration:.6f}" min="0" step="1" type="range" value="0" />
        <div class="thumbnail-size-control" id="thumbnailSizeControl">
            <label for="thumbnailSizeRange">サムネサイズ:</label>
            <input id="thumbnailSizeRange" type="range" min="50" max="400" step="5" value="{thumbnail_width}" />
            <span id="thumbnailSizeStatus">{thumbnail_width} × {thumbnail_height}px</span>
        </div>
        <label for="gaugeBar">高さ:</label>
        <input id="gaugeBar" max="800" min="100" type="range" value="180" style="width: 100px;" />
    </div>
""")


        # メタデータ
        html_parts.append(f"""
            <div class="section">
                <h2>メタデータ</h2>
                <ul>
                    <li>LiveNum: {broadcast_data.get('lv_value', '')}</li>
                    <li>配信時間: {broadcast_data.get('elapsed_time', '')}</li>
                    <li>開始時刻: {start_time_jst.strftime('%Y-%m-%d %H:%M:%S JST')}</li>
                    <li>終了時刻: {end_time_jst.strftime('%Y-%m-%d %H:%M:%S JST')}</li>
                    <li>配信者ID: {broadcast_data.get('owner_id', '')}</li>
                </ul>
            </div>
        """)

        # JavaScript
        html_parts.append(build_virtual_timeline_script(lv_value))
        html_parts.append(f"""
    <script src="https://cdn.jsdelivr.net/npm/chart.js@2.9.4"></script>
    <script>
    document.addEventListener("DOMContentLoaded", function () {{
        const audioPlayer = document.getElementById("audioPlayer");
        const seekbar = document.getElementById("seekbar");
        const autoJumpToggle = document.getElementById("autoJumpToggle");
        let manualBlockHeight = null;
        const thumbnailSizeRange = document.getElementById("thumbnailSizeRange");
        const thumbnailSizeStatus = document.getElementById("thumbnailSizeStatus");
        const gaugeBar = document.getElementById("gaugeBar");
        const portraitSeekbar = document.getElementById("portraitSeekbar");
        const portraitHeightRange = document.getElementById("portraitHeightRange");
        const portraitThumbnailRange = document.getElementById("portraitThumbnailRange");
        const portraitHeightValue = document.getElementById("portraitHeightValue");
        const portraitThumbnailValue = document.getElementById("portraitThumbnailValue");
        const portraitAutoJumpToggle = document.getElementById("portraitAutoJumpToggle");
        const portraitPlayButton = document.getElementById("portraitPlayButton");
        const portraitStopButton = document.getElementById("portraitStopButton");
        const thumbnailBaseWidth = {thumbnail_width};
        const thumbnailBaseHeight = {thumbnail_height};
        let portraitSeekActive = false;

        function setThumbnailSize(value) {{
            const min = Number.parseInt(thumbnailSizeRange?.min || "50", 10);
            const max = Number.parseInt(thumbnailSizeRange?.max || "400", 10);
            const requestedWidth = Number.parseInt(value, 10) || thumbnailBaseWidth;
            const width = Math.max(min, Math.min(max, requestedWidth));
            const height = Math.round(width * thumbnailBaseHeight / thumbnailBaseWidth);

            document.querySelectorAll("#timeline1 .img_container").forEach(thumbnail => {{
                thumbnail.style.width = width + "px";
                thumbnail.style.height = height + "px";
            }});

            if (thumbnailSizeRange) thumbnailSizeRange.value = String(width);
            if (thumbnailSizeStatus) thumbnailSizeStatus.textContent = width + " × " + height + "px";
            if (portraitThumbnailRange) portraitThumbnailRange.value = String(width);
            if (portraitThumbnailValue) portraitThumbnailValue.value = String(width);
        }}

        if (thumbnailSizeRange) {{
            thumbnailSizeRange.addEventListener("input", () => setThumbnailSize(thumbnailSizeRange.value));
            setThumbnailSize(thumbnailSizeRange.value);
        }}
        document.addEventListener("nico-virtual-window-rendered", function (event) {{
            setThumbnailSize(thumbnailSizeRange?.value || thumbnailBaseWidth);
            if (portraitSeekbar && !portraitSeekActive && event.detail) {{
                const visibleSecond = Math.max(0, Number(event.detail.start || 0) * 10);
                portraitSeekbar.value = String(
                    Math.min(Number(portraitSeekbar.max) || visibleSecond, visibleSecond)
                );
            }}
            syncCommentFlow();
        }});

        function syncCommentFlow() {{
            if (!audioPlayer) return;
            const second = audioPlayer.currentTime || 0;
            const blockSecond = Math.floor(second / 10) * 10;
            const block = document.querySelector(
                `#timeline2 .time-block[id="time_block_${{blockSecond}}"]`
            );
            if (!block) return;
            const list = block.querySelector('.comment-list');
            if (!list) return;
            let newest = null;
            list.querySelectorAll('.comment-item[data-comment-seconds]').forEach(item => {{
                const visible = Number(item.dataset.commentSeconds) <= second;
                item.hidden = false;
                if (visible) newest = item;
            }});
            if (newest) {{
                list.scrollTop = newest.offsetTop + newest.offsetHeight - list.clientHeight;
            }}
        }}
        
        // 音声プレイヤー初期化
        if (audioPlayer && seekbar) {{
            audioPlayer.onloadedmetadata = function () {{
                const canonicalDuration = Number(window.NICO_ARCHIVE_CONFIG?.duration || 0);
                seekbar.max = canonicalDuration > 0 ? canonicalDuration : audioPlayer.duration;
                if (portraitSeekbar) portraitSeekbar.max = seekbar.max;
            }};
            
            seekbar.addEventListener("input", function () {{
                audioPlayer.currentTime = this.value;
                syncCommentFlow();
                if (autoJumpToggle.checked) {{
                    scrollToCurrentTimeBlock();
                }}
            }});
            
            audioPlayer.addEventListener("timeupdate", function () {{
                seekbar.value = audioPlayer.currentTime;
                syncCommentFlow();
                if (autoJumpToggle.checked) {{
                    if (portraitSeekbar && !portraitSeekActive) {{
                        portraitSeekbar.value = String(Math.floor(audioPlayer.currentTime));
                    }}
                    scrollToCurrentTimeBlock();
                }}
            }});
        }}

        const virtualMaximum = Number(
            window.NicoVirtualTimeline?.getMaxSecond?.() || 0
        );
        const canonicalMaximum = Number(window.NICO_ARCHIVE_CONFIG?.duration || 0);
        if (portraitSeekbar) {{
            portraitSeekbar.max = String(
                canonicalMaximum > 0 ? canonicalMaximum : virtualMaximum
            );
            portraitSeekbar.addEventListener("pointerdown", () => {{
                portraitSeekActive = true;
            }});
            portraitSeekbar.addEventListener("pointerup", () => {{
                portraitSeekActive = false;
            }});
            portraitSeekbar.addEventListener("input", function () {{
                const second = Number(this.value) || 0;
                if (seekbar) seekbar.value = String(second);
                if (audioPlayer) audioPlayer.currentTime = second;
                window.NicoVirtualTimeline?.renderSecond(second, true);
                syncCommentFlow();
            }});
        }}
        if (portraitThumbnailRange) {{
            portraitThumbnailRange.addEventListener("input", function () {{
                if (thumbnailSizeRange) {{
                    thumbnailSizeRange.value = this.value;
                    thumbnailSizeRange.dispatchEvent(new Event("input", {{ bubbles: true }}));
                }} else {{
                    setThumbnailSize(this.value);
                }}
            }});
        }}
        if (portraitHeightRange && gaugeBar) {{
            portraitHeightRange.addEventListener("input", function () {{
                gaugeBar.value = this.value;
                gaugeBar.dispatchEvent(new Event("input", {{ bubbles: true }}));
            }});
        }}
        if (portraitAutoJumpToggle && autoJumpToggle) {{
            portraitAutoJumpToggle.checked = autoJumpToggle.checked;
            portraitAutoJumpToggle.addEventListener("change", function () {{
                autoJumpToggle.checked = this.checked;
            }});
            autoJumpToggle.addEventListener("change", function () {{
                portraitAutoJumpToggle.checked = this.checked;
            }});
        }}
        if (portraitPlayButton && audioPlayer) {{
            portraitPlayButton.addEventListener("click", function () {{
                audioPlayer.play();
            }});
        }}
        if (portraitStopButton && audioPlayer) {{
            portraitStopButton.addEventListener("click", function () {{
                audioPlayer.pause();
            }});
        }}
        
        // コメント表示/非表示トグル機能
        document.querySelectorAll('.toggle-comments-btn').forEach(button => {{
            button.addEventListener('click', function() {{
                const userId = this.dataset.userId;
                const commentsDiv = document.getElementById('comments-' + userId);
                
                if (commentsDiv.style.display === 'none') {{
                    commentsDiv.style.display = 'block';
                    this.textContent = '全コメント非表示';
                    this.style.backgroundColor = '#dc3545';
                }} else {{
                    commentsDiv.style.display = 'none';
                    this.textContent = '全コメント表示';
                    this.style.backgroundColor = '#007cba';
                }}
            }});
        }});
        
        let lastFlashedBlock = null;
        
        function scrollToCurrentTimeBlock() {{
            if (!audioPlayer) return;
            const currentBlock = Math.floor(audioPlayer.currentTime / 10) * 10;
            const timeBlockId = `time_block_${{currentBlock}}`;
            window.NicoVirtualTimeline?.renderSecond(currentBlock, false);
            const timeBlock1 = document.querySelector(
                `#timeline1 .time-block[id="${{timeBlockId}}"]`
            );
            const timeBlock2 = document.querySelector(`#timeline2 .time-block[id="${{timeBlockId}}"]`);
            
            if (timeBlock1 && lastFlashedBlock !== currentBlock) {{
                timeBlock1.scrollIntoView({{
                    behavior: "smooth",
                    block: "center"
                }});
                
                timeBlock1.classList.add('flash-fade-out');
                if (timeBlock2) {{
                    timeBlock2.classList.add('flash-fade-out');
                }}
                
                setTimeout(() => {{
                    timeBlock1.classList.remove('flash-fade-out');
                    if (timeBlock2) {{
                        timeBlock2.classList.remove('flash-fade-out');
                    }}
                }}, 1000);
                
                lastFlashedBlock = currentBlock;
            }}
        }}

        // ゲージバー機能
        gaugeBar.addEventListener('input', function() {{
            const gaugeValue = this.value;
            const nearestBlockId = getNearestTimeBlockId();
            const nearestBlock = nearestBlockId ? document.querySelector(
                `#timeline1 .time-block[id="${{nearestBlockId}}"]`
            ) : null;
            const offsetTop = nearestBlock ? nearestBlock.getBoundingClientRect().top : 0;
            
            manualBlockHeight = Number.parseInt(gaugeValue, 10) || {DEFAULT_TIMELINE_BLOCK_HEIGHT};
            if (window.NicoVirtualTimeline) {{
                window.NicoVirtualTimeline.setBlockHeight(manualBlockHeight);
            }} else {{
                document.querySelectorAll('.time-block').forEach(block => {{
                    block.style.height = `${{manualBlockHeight}}px`;
                }});
            }}
            if (portraitHeightRange) portraitHeightRange.value = String(manualBlockHeight);
            if (portraitHeightValue) portraitHeightValue.value = String(manualBlockHeight);

            if (nearestBlock) {{
                window.requestAnimationFrame(() => {{
                    const currentNearest = document.querySelector(
                        `#timeline1 .time-block[id="${{nearestBlockId}}"]`
                    );
                    if (currentNearest) {{
                        window.scrollBy(0, currentNearest.getBoundingClientRect().top - offsetTop);
                    }}
                }});
            }}
        }});
        
        function getNearestTimeBlockId() {{
            const timeBlocks = document.querySelectorAll('#timeline1 .time-block');
            let nearestBlockId = null;
            let nearestDistance = Infinity;
            
            timeBlocks.forEach(block => {{
                const rect = block.getBoundingClientRect();
                const distance = Math.abs(rect.top);
                
                if (distance < nearestDistance) {{
                    nearestDistance = distance;
                    nearestBlockId = block.id;
                }}
            }});
            
            return nearestBlockId;
        }}
        
        // 感情分析グラフ
        var segments = {segments_js};
        var positiveData = {positive_data_js};
        var centerData = {center_data_js};
        var negativeData = {negative_data_js};
        var speakerEmotionData = {speaker_emotion_json};
        var emotionSeries = Object.assign({{
            "全体": {{
                segments: segments,
                positive: positiveData,
                center: centerData,
                negative: negativeData
            }}
        }}, speakerEmotionData);
        var currentEmotionSegments = segments;
        
        function createTooltipText(dataIndex) {{
            var timeBlockID = Math.floor(Number(currentEmotionSegments[dataIndex] || 0) / 10) * 10;
            if (window.NicoVirtualTimeline?.transcriptText) {{
                return window.NicoVirtualTimeline.transcriptText(timeBlockID);
            }}
            var commentElement = document.querySelector(
                '#timeline1 .time-block[id="time_block_' + timeBlockID + '"]'
            );
            if (commentElement && commentElement.querySelector('.comment')) {{
                var htmlContent = commentElement.querySelector('.comment').innerHTML;
                return htmlContent.replace(/<[^>]*>/g, '').trim();
            }}
            return '';
        }}
        
        function jumpToTimeBlock(dataIndex) {{
            var timeBlockID = Math.floor(Number(currentEmotionSegments[dataIndex] || 0) / 10) * 10;
            if (window.NicoVirtualTimeline) {{
                window.NicoVirtualTimeline.renderSecond(timeBlockID, true);
                return;
            }}
            var timeBlockElement = document.querySelector(
                '#timeline1 .time-block[id="time_block_' + timeBlockID + '"]'
            );
            if (timeBlockElement) {{
                timeBlockElement.scrollIntoView({{behavior: 'smooth', block: 'center'}});
            }}
        }}
        
        function setEmotionTab(name) {{
            var series = emotionSeries[name] || emotionSeries["全体"];
            currentEmotionSegments = series.segments || segments;
            sentimentChart.data.labels = currentEmotionSegments.map(formatEmotionTime);
            sentimentChart.data.datasets[0].data = series.positive;
            sentimentChart.data.datasets[1].data = series.center;
            sentimentChart.data.datasets[2].data = series.negative;
            sentimentChart.update();
            document.querySelectorAll('.emotion-tab').forEach(function(button) {{
                button.classList.toggle('active', button.dataset.speaker === name);
            }});
        }}

        function setupEmotionTabs() {{
            var tabs = document.getElementById('emotion-tabs');
            if (!tabs) return;
            Object.keys(emotionSeries).forEach(function(name) {{
                var button = document.createElement('button');
                button.type = 'button';
                button.className = 'emotion-tab' + (name === '全体' ? ' active' : '');
                button.dataset.speaker = name;
                button.textContent = name;
                button.addEventListener('click', function() {{
                    setEmotionTab(name);
                }});
                tabs.appendChild(button);
            }});
        }}

        var emotionZoom = 1;
        var graphScroll = document.getElementById('emotion-graph-scroll');
        var graphInner = document.getElementById('emotion-graph-inner');
        var zoomRange = document.getElementById('emotion-zoom-range');
        var zoomValue = document.getElementById('emotion-zoom-value');

        function updateEmotionGraphWidth() {{
            var baseWidth = Math.max(800, graphScroll ? graphScroll.clientWidth : 800);
            var nextWidth = Math.round(baseWidth * emotionZoom);
            if (graphInner) graphInner.style.width = nextWidth + 'px';
            if (ctx) {{
                ctx.width = nextWidth;
                ctx.height = 300;
                ctx.style.width = nextWidth + 'px';
                ctx.style.height = '300px';
            }}
            if (zoomValue) zoomValue.textContent = emotionZoom.toFixed(2) + 'x';
            if (typeof sentimentChart !== 'undefined') {{
                sentimentChart.resize();
                sentimentChart.update(0);
            }}
        }}

        function setupEmotionGraphDrag() {{
            if (!graphScroll) return;
            var dragging = false;
            var startX = 0;
            var startScrollLeft = 0;
            graphScroll.addEventListener('mousedown', function(event) {{
                dragging = true;
                startX = event.pageX;
                startScrollLeft = graphScroll.scrollLeft;
                graphScroll.classList.add('dragging');
                event.preventDefault();
            }});
            window.addEventListener('mousemove', function(event) {{
                if (!dragging) return;
                graphScroll.scrollLeft = startScrollLeft - (event.pageX - startX);
            }});
            window.addEventListener('mouseup', function() {{
                dragging = false;
                graphScroll.classList.remove('dragging');
            }});
        }}

        if (zoomRange) {{
            zoomRange.addEventListener('input', function() {{
                emotionZoom = Number(zoomRange.value) || 1;
                updateEmotionGraphWidth();
            }});
        }}
        setupEmotionGraphDrag();

        function formatEmotionTime(value) {{
            var total = Math.max(0, Number(value) || 0);
            var minutes = Math.floor(total / 60);
            var seconds = (total - minutes * 60).toFixed(total % 1 ? 1 : 0).padStart(2, '0');
            return minutes + ':' + seconds;
        }}

        // Chart.js でグラフ作成
        var ctx = document.createElement('canvas');
        ctx.width = 800;
        ctx.height = 300;
        (graphInner || document.querySelector('.graph-container')).appendChild(ctx);
        
        var sentimentChart = new Chart(ctx.getContext('2d'), {{
            type: 'line',
            data: {{
                labels: segments.map(formatEmotionTime),
                datasets: [
                    {{ 
                        label: 'Positive', 
                        data: positiveData,
                        borderColor: '#4CAF50',
                        backgroundColor: 'rgba(76, 175, 80, 0.1)',
                        fill: false
                    }},
                    {{ 
                        label: 'Center', 
                        data: centerData,
                        borderColor: '#2196F3',
                        backgroundColor: 'rgba(33, 150, 243, 0.1)',
                        fill: false
                    }},
                    {{ 
                        label: 'Negative', 
                        data: negativeData,
                        borderColor: '#F44336',
                        backgroundColor: 'rgba(244, 67, 54, 0.1)',
                        fill: false
                    }}
                ]
            }},
            options: {{
                responsive: true,
                maintainAspectRatio: false,
                tooltips: {{
                    enabled: true,
                    mode: 'index',
                    intersect: false,
                    callbacks: {{
                        beforeBody: function(tooltipItems, data) {{
                            var segmentIndex = tooltipItems[0].index;
                            return createTooltipText(segmentIndex);
                        }},
                        label: function(tooltipItem, data) {{
                            var label = data.datasets[tooltipItem.datasetIndex].label;
                            var value = tooltipItem.yLabel.toFixed(3);
                            return label + ': ' + value;
                        }}
                    }}
                }},
                onClick: function(evt) {{
                    var activePoints = sentimentChart.getElementsAtEvent(evt);
                    if (activePoints.length > 0) {{
                        var dataIndex = activePoints[0]._index;
                        jumpToTimeBlock(dataIndex);
                    }}
                }},
                scales: {{
                    yAxes: [{{
                        ticks: {{
                            beginAtZero: true,
                            max: 1.0
                        }}
                    }}]
                }}
            }}
        }});
        setupEmotionTabs();
        updateEmotionGraphWidth();
    }});
    </script>
</body>
</html>""")
        return ''.join(html_parts)
        
    except Exception as e:
        print(f"完全HTML生成エラー: {str(e)}")
        import traceback
        traceback.print_exc()
        return "<html><body>HTML生成エラー</body></html>"

def format_time_range(start_seconds, end_seconds):
    """時間範囲を表記"""
    start_time = format_seconds_to_time(start_seconds)
    end_time = format_seconds_to_time(end_seconds)
    return f"{start_time} - {end_time}"

def format_seconds_to_time(seconds):
    """秒数を時間表記に変換"""
    try:
        seconds = int(seconds)
        minutes = seconds // 60
        hours = minutes // 60
        return f"{hours:02d}:{minutes%60:02d}:{seconds%60:02d}"
    except:
        return "00:00:00"

def build_html_filename(lv_value, live_title):
    """従来と同じ規則でPC版HTMLのファイル名を作る。"""
    filename = f"{lv_value}_{live_title}.html"
    invalid_chars = '<>:"/\\|?*'
    for char in invalid_chars:
        filename = filename.replace(char, '_')
    filename = filename.strip('. ')
    if len(filename) > 200:
        filename = filename[:200]
    return filename


def find_existing_pc_html(broadcast_dir, lv_value, broadcast_data, expected_filename):
    """DB指定・期待名・既存完成ページの順で、保持すべきPC版を探す。"""
    root = os.path.realpath(broadcast_dir)
    candidates = []

    def add_candidate(value):
        if not value:
            return
        path = str(value)
        if not os.path.isabs(path):
            path = os.path.join(root, path)
        path = os.path.realpath(path)
        try:
            if os.path.commonpath([root, path]) != root:
                return
        except ValueError:
            return
        name = os.path.basename(path)
        if name.lower().endswith('_mobile.html') or name.lower() == f'{lv_value.lower()}.html':
            return
        if os.path.isfile(path) and path not in candidates:
            candidates.append(path)

    add_candidate((broadcast_data or {}).get('html_file_path'))
    add_candidate(expected_filename)
    if os.path.isdir(root):
        for name in sorted(os.listdir(root)):
            if name.startswith(lv_value) and name.lower().endswith('.html'):
                add_candidate(name)

    for path in candidates:
        try:
            document = open(path, 'r', encoding='utf-8').read()
        except (OSError, UnicodeDecodeError):
            continue
        if 'id="timeline2"' in document or "id='timeline2'" in document:
            return path
    return candidates[0] if candidates else None


def save_html_file(broadcast_dir, lv_value, live_title, html_content):
    """HTMLファイルを保存"""
    try:
        filename = build_html_filename(lv_value, live_title)
        
        html_file = os.path.join(broadcast_dir, filename)
        
        with open(html_file, 'w', encoding='utf-8') as f:
            f.write(html_content)
        
        print(f"完全HTML保存完了: {html_file}")
        return html_file
    except Exception as e:
        print(f"HTML保存エラー: {str(e)}")
        raise


def select_timeline_audio_source(broadcast_dir, lv_value):
    """HTMLのタイムラインプレイヤーに使う音声ファイルを選ぶ。"""
    for filename in (
        f"{lv_value}_silent_audio.mp3",
        f"{lv_value}_audio.mp3",
        f"{lv_value}_full_audio.mp3",
    ):
        if os.path.exists(os.path.join(broadcast_dir, filename)):
            return f"./{filename}"
    return f"./{lv_value}_silent_audio.mp3"
