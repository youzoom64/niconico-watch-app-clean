import os
import json
import html
from pathlib import Path
from datetime import datetime
import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from utils import find_account_directory
from archive_db import list_broadcast_data, load_transcript_payload

try:
    from .html_preservation import atomic_write_text, safe_json, update_json_script_blocks
except ImportError:
    from processors.html_preservation import atomic_write_text, safe_json, update_json_script_blocks

def process(pipeline_data):
    """Step14: Step13のタグ保持処理を通した同一生成経路を使う。"""
    try:
        from . import step13_index_generator as step13
    except ImportError:
        from processors import step13_index_generator as step13
    result = step13.process(pipeline_data)
    return {
        "modern_list_generated": True,
        "broadcast_count": result.get("broadcast_count", 0),
        **result,
    }

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
                'music_urls': get_music_urls(data),
                'transcript_segments': get_transcript_segments(broadcast_dir, lv_value),
                'tags': []
            }
            broadcast_list.append(broadcast_info)
        
        broadcast_list.sort(key=lambda x: x['start_time'], reverse=True)
        print(f"配信データ収集完了(DB broadcaster={broadcaster_id}): {len(broadcast_list)}件")
        
    except Exception as e:
        print(f"配信データ収集エラー: {str(e)}")
    
    return broadcast_list

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

def get_music_urls(data):
    """音楽URL取得"""
    music_data = data.get('music_generation', {})
    songs = music_data.get('songs', [])
    urls = []
    for song in songs:
        if song.get('primary_url'):
            urls.append(song['primary_url'])
    return urls

def get_transcript_segments(broadcast_dir, lv_value):
    """文字起こしセグメントを取得"""
    transcript_file = os.path.join(broadcast_dir, f"{lv_value}_transcript.json")
    if os.path.exists(transcript_file):
        with open(transcript_file, 'r', encoding='utf-8') as f:
            transcript_data = json.load(f)
        
        transcripts = transcript_data.get('transcripts', [])
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

def process_tags(broadcast_list, tags_config):
    """タグマッチング処理"""
    for broadcast in broadcast_list:
        search_text = f"{broadcast['title']} {broadcast['summary_text']}"
        if broadcast.get('transcript_segments'):
            search_text += " " + " ".join(broadcast['transcript_segments'])
        search_text = search_text.lower()
        
        for tag in tags_config:
            if tag.lower() in search_text:
                broadcast['tags'].append(tag)
    
    return broadcast_list

def generate_modern_list_page(account_dir, broadcast_list, tags_config):
    """モダンでインタラクティブな配信一覧ページ生成"""
    try:
        # JavaScriptデータ準備
        js_data = {}
        for broadcast in broadcast_list:
            js_data[broadcast['lv_value']] = {
                'title': broadcast['title'],
                'broadcaster': broadcast['broadcaster'],
                'summary': broadcast['summary_text'],
                'imageUrl': broadcast['image_url'],
                'musicUrls': broadcast['music_urls'],
                'transcriptSegments': broadcast['transcript_segments'],
                'tags': broadcast['tags']
            }
        
        html_content = f"""<!DOCTYPE html>
<html lang="ja">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>配信アーカイブ一覧</title>
    <style>
        * {{
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }}
        
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            color: #333;
        }}
        
        .container {{
            max-width: 1200px;
            margin: 0 auto;
            padding: 2rem;
        }}
        
        .header {{
            text-align: center;
            margin-bottom: 3rem;
            color: white;
        }}
        
        .header h1 {{
            font-size: 3rem;
            font-weight: 300;
            margin-bottom: 0.5rem;
            text-shadow: 0 2px 10px rgba(0,0,0,0.3);
        }}
        
        .header p {{
            font-size: 1.2rem;
            opacity: 0.9;
        }}
        
        .controls {{
            display: flex;
            justify-content: center;
            gap: 1rem;
            margin-bottom: 2rem;
            flex-wrap: wrap;
        }}
        
        .control-btn {{
            background: rgba(255,255,255,0.2);
            color: white;
            border: 2px solid rgba(255,255,255,0.3);
            padding: 0.8rem 1.5rem;
            border-radius: 50px;
            cursor: pointer;
            transition: all 0.3s ease;
            backdrop-filter: blur(10px);
            font-weight: 500;
            text-decoration: none;
            display: inline-block;
        }}
        
        .control-btn:hover, .control-btn.active {{
            background: rgba(255,255,255,0.3);
            border-color: rgba(255,255,255,0.5);
            transform: translateY(-2px);
            box-shadow: 0 5px 15px rgba(0,0,0,0.2);
        }}
        
        .tag-controls {{
            display: flex;
            justify-content: center;
            gap: 0.5rem;
            margin-bottom: 2rem;
            flex-wrap: wrap;
        }}
        
        .tag-btn {{
            background: rgba(255,255,255,0.1);
            color: white;
            border: 1px solid rgba(255,255,255,0.2);
            padding: 0.5rem 1rem;
            border-radius: 25px;
            cursor: pointer;
            transition: all 0.3s ease;
            font-size: 0.9rem;
            text-decoration: none;
        }}
        
        .tag-btn:hover, .tag-btn.active {{
            background: rgba(255,255,255,0.2);
            border-color: rgba(255,255,255,0.4);
        }}
        
        .broadcast-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(400px, 1fr));
            gap: 2rem;
        }}
        
        .broadcast-item {{
            position: relative;
            background: rgba(255,255,255,0.95);
            border-radius: 20px;
            overflow: hidden;
            box-shadow: 0 10px 30px rgba(0,0,0,0.2);
            transition: all 0.4s cubic-bezier(0.175, 0.885, 0.32, 1.275);
            cursor: pointer;
        }}
        
        .broadcast-item:hover {{
            transform: translateY(-10px) scale(1.02);
            box-shadow: 0 20px 50px rgba(0,0,0,0.3);
        }}
        
        .broadcast-item::before {{
            content: '';
            position: absolute;
            top: 0;
            left: 0;
            right: 0;
            height: 4px;
            background: linear-gradient(90deg, #667eea, #764ba2);
        }}
        
        .broadcast-content {{
            padding: 1.5rem;
        }}
        
        .broadcast-title {{
            font-size: 1.3rem;
            font-weight: 600;
            color: #333;
            margin-bottom: 1rem;
            line-height: 1.4;
            display: -webkit-box;
            -webkit-line-clamp: 2;
            -webkit-box-orient: vertical;
            overflow: hidden;
        }}
        
        .broadcast-meta {{
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 0.5rem;
            margin-bottom: 1rem;
            font-size: 0.9rem;
            color: #666;
        }}
        
        .meta-item {{
            display: flex;
            align-items: center;
            gap: 0.5rem;
        }}
        
        .broadcast-tags {{
            display: flex;
            flex-wrap: wrap;
            gap: 0.5rem;
            margin-top: 1rem;
        }}
        
        .broadcast-tag {{
            background: rgba(102, 126, 234, 0.1);
            color: #667eea;
            padding: 0.3rem 0.8rem;
            border-radius: 15px;
            font-size: 0.8rem;
            border: 1px solid rgba(102, 126, 234, 0.2);
        }}
        
        .comment-flow {{
            position: absolute;
            top: 0;
            left: 0;
            right: 0;
            bottom: 0;
            pointer-events: none;
            overflow: hidden;
            opacity: 0;
            transition: opacity 0.3s ease;
        }}
        
        .broadcast-item:hover .comment-flow {{
            opacity: 1;
        }}
        
        .flowing-comment {{
            position: absolute;
            background: rgba(102, 126, 234, 0.9);
            color: white;
            padding: 0.5rem 1rem;
            border-radius: 20px;
            font-size: 0.85rem;
            white-space: nowrap;
            animation: commentSlide 8s linear infinite;
            backdrop-filter: blur(5px);
            box-shadow: 0 2px 10px rgba(0,0,0,0.2);
        }}
        
        @keyframes commentSlide {{
            from {{
                transform: translateX(100%);
                opacity: 1;
            }}
            to {{
                transform: translateX(-100%);
                opacity: 0;
            }}
        }}
        
        .preview-popup {{
            position: fixed;
            background: white;
            border-radius: 15px;
            box-shadow: 0 20px 60px rgba(0,0,0,0.3);
            z-index: 1000;
            pointer-events: none;
            opacity: 0;
            transform: scale(0.8);
            transition: all 0.3s ease;
            max-width: 500px;
            overflow: hidden;
        }}
        
        .preview-popup.show {{
            opacity: 1;
            transform: scale(1);
        }}
        
        .preview-image {{
            width: 100%;
            height: 200px;
            object-fit: cover;
        }}
        
        .preview-content {{
            padding: 1.5rem;
        }}
        
        .preview-title {{
            font-weight: 600;
            margin-bottom: 1rem;
            color: #333;
        }}
        
        .preview-summary {{
            color: #666;
            line-height: 1.5;
            margin-bottom: 1rem;
            font-size: 0.9rem;
        }}
        
        .preview-audio {{
            width: 100%;
            height: 40px;
            border-radius: 20px;
        }}
        
        @media (max-width: 768px) {{
            .container {{
                padding: 1rem;
            }}
            
            .header h1 {{
                font-size: 2rem;
            }}
            
            .broadcast-grid {{
                grid-template-columns: 1fr;
                gap: 1rem;
            }}
            
            .broadcast-meta {{
                grid-template-columns: 1fr;
            }}
        }}
        
        .fade-in {{
            opacity: 0;
            transform: translateY(20px);
            animation: fadeInUp 0.6s ease forwards;
        }}
        
        @keyframes fadeInUp {{
            to {{
                opacity: 1;
                transform: translateY(0);
            }}
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header fade-in">
            <h1>配信アーカイブ</h1>
            <p>全{len(broadcast_list)}件の配信記録</p>
        </div>
        
        <div class="controls fade-in">
            <button class="control-btn active" data-action="all">すべて表示</button>
            <button class="control-btn" data-action="music" id="musicToggle">音楽プレビュー</button>
            <a href="index.html" class="control-btn">詳細一覧へ</a>
        </div>
        
        <div class="tag-controls fade-in">
            <button class="tag-btn active" data-tag="all">すべて</button>
            {generate_tag_buttons(tags_config)}
        </div>
        
        <div class="broadcast-grid">
            {generate_broadcast_cards(broadcast_list)}
        </div>
    </div>

    <div class="preview-popup" id="previewPopup">
        <img class="preview-image" id="previewImage" alt="配信画像">
        <div class="preview-content">
            <div class="preview-title" id="previewTitle"></div>
            <div class="preview-summary" id="previewSummary"></div>
            <audio class="preview-audio" id="previewAudio" controls style="display: none;">
                <source type="audio/mp3">
            </audio>
        </div>
    </div>

    <script>
        const broadcastData = {json.dumps(js_data, ensure_ascii=False, indent=2)};
        let musicEnabled = false;
        let commentIntervals = new Map();
        let previewPopup = null;
        
        document.addEventListener('DOMContentLoaded', function() {{
            // アニメーション遅延適用
            document.querySelectorAll('.broadcast-item').forEach((item, index) => {{
                item.style.animationDelay = `${{index * 0.1}}s`;
                item.classList.add('fade-in');
            }});
            
            // コントロールボタン
            document.getElementById('musicToggle').addEventListener('click', function() {{
                musicEnabled = !musicEnabled;
                this.textContent = musicEnabled ? '音楽プレビュー ON' : '音楽プレビュー';
                this.classList.toggle('active');
            }});
            
            // タグフィルター
            document.querySelectorAll('.tag-btn').forEach(btn => {{
                btn.addEventListener('click', function() {{
                    const selectedTag = this.dataset.tag;
                    filterByTag(selectedTag);
                    
                    document.querySelectorAll('.tag-btn').forEach(b => b.classList.remove('active'));
                    this.classList.add('active');
                }});
            }});
            
            // プレビューポップアップ要素
            previewPopup = document.getElementById('previewPopup');
            
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
                
                item.addEventListener('mousemove', updatePreviewPosition);
                
                item.addEventListener('click', function() {{
                    const link = this.querySelector('a');
                    if (link) link.click();
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
            
            // 画像設定
            const img = document.getElementById('previewImage');
            if (data.imageUrl) {{
                img.src = data.imageUrl;
                img.style.display = 'block';
            }} else {{
                img.style.display = 'none';
            }}
            
            // テキスト設定
            document.getElementById('previewTitle').textContent = data.title;
            document.getElementById('previewSummary').textContent = data.summary || '要約なし';
            
            // 音楽設定
            const audio = document.getElementById('previewAudio');
            if (musicEnabled && data.musicUrls && data.musicUrls.length > 0) {{
                audio.src = data.musicUrls[0];
                audio.style.display = 'block';
                audio.volume = 0.3;
            }} else {{
                audio.style.display = 'none';
            }}
            
            previewPopup.classList.add('show');
            updatePreviewPosition(event);
        }}
        
        function hidePreview() {{
            previewPopup.classList.remove('show');
            const audio = document.getElementById('previewAudio');
            audio.pause();
        }}
        
        function updatePreviewPosition(event) {{
            if (!previewPopup.classList.contains('show')) return;
            
            const x = Math.min(event.clientX + 20, window.innerWidth - previewPopup.offsetWidth - 20);
            const y = Math.max(event.clientY - previewPopup.offsetHeight - 20, 20);
            
            previewPopup.style.left = x + 'px';
            previewPopup.style.top = y + 'px';
        }}
        
        function startCommentFlow(item) {{
            const lvValue = item.dataset.lv;
            const data = broadcastData[lvValue];
            
            if (!data || !data.transcriptSegments || data.transcriptSegments.length === 0) return;
            
            const commentFlow = item.querySelector('.comment-flow');
            let commentIndex = 0;
            
            const interval = setInterval(() => {{
                const comment = document.createElement('div');
                comment.className = 'flowing-comment';
                comment.textContent = data.transcriptSegments[commentIndex % data.transcriptSegments.length];
                comment.style.top = Math.random() * 70 + '%';
                comment.style.animationDelay = '0s';
                
                commentFlow.appendChild(comment);
                
                setTimeout(() => {{
                    if (comment.parentNode) {{
                        comment.parentNode.removeChild(comment);
                    }}
                }}, 8000);
                
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
        
        list_file = os.path.join(account_dir, 'modern_list.html')
        with open(list_file, 'w', encoding='utf-8') as f:
            f.write(html_content)
        
        print(f"モダン一覧ページ生成: {list_file}")
        
    except Exception as e:
        print(f"一覧ページ生成エラー: {str(e)}")
        raise

def generate_tag_buttons(tags_config):
    """タグボタンHTML生成"""
    buttons = []
    for tag in tags_config:
        buttons.append(f'<a href="tags/tag_{html.escape(tag)}.html" class="tag-btn">{html.escape(tag)}</a>')
    return '\n            '.join(buttons)

def generate_broadcast_cards(broadcast_list):
    """配信カードHTML生成"""
    cards = []
    
    for i, broadcast in enumerate(broadcast_list):
        start_time_str = datetime.fromtimestamp(int(broadcast['start_time'])).strftime('%m/%d %H:%M') if broadcast['start_time'] else '不明'
        tags_str = ','.join(broadcast['tags'])
        tags_html = ''.join([f'<span class="broadcast-tag">{html.escape(tag)}</span>' for tag in broadcast['tags']])
        
        card_html = f"""
            <div class="broadcast-item" data-lv="{broadcast['lv_value']}" data-tags="{html.escape(tags_str)}" style="animation-delay: {i * 0.1}s;">
                <div class="broadcast-content">
                    <a href="{broadcast['html_file']}" style="text-decoration: none; color: inherit;">
                        <h3 class="broadcast-title">{html.escape(broadcast['title'])}</h3>
                    </a>
                    
                    <div class="broadcast-meta">
                        <div class="meta-item">
                            <span>👤</span>
                            <span>{html.escape(broadcast['broadcaster'])}</span>
                        </div>
                        <div class="meta-item">
                            <span>🕒</span>
                            <span>{start_time_str}</span>
                        </div>
                        <div class="meta-item">
                            <span>👥</span>
                            <span>{broadcast['watch_count']}人</span>
                        </div>
                        <div class="meta-item">
                            <span>💬</span>
                            <span>{broadcast['comment_count']}コメ</span>
                        </div>
                    </div>
                    
                    <div class="broadcast-tags">
                        {tags_html}
                    </div>
                </div>
                
                <div class="comment-flow"></div>
            </div>"""
        
        cards.append(card_html)
    
    return '\n        '.join(cards)


# Link Archive 風の一覧生成。上の旧カード型実装を同名関数で上書きする。
def generate_modern_list_page(
    account_dir,
    broadcast_list,
    tags_config,
    *,
    output_file=None,
    document_title='リンク一覧 - Archive',
    heading='リンク一覧',
    heading_en='LINK ARCHIVE',
    link_prefix='',
    compact=False,
    back_href='',
    back_label='全配信一覧に戻る',
    change_log=None,
):
    """Link Archive型のダークな配信一覧ページ生成"""
    try:
        def page_relative_url(value):
            url = str(value or '')
            if not link_prefix or not url:
                return url
            lowered = url.lower()
            if (
                '://' in url
                or lowered.startswith(('data:', 'mailto:', 'javascript:'))
                or url.startswith(('/', '#', './', '../'))
            ):
                return url
            return f"{link_prefix}{url}"

        records = []
        tag_counts = {}
        for broadcast in broadcast_list:
            start_ts = int(broadcast.get('start_time') or 0)
            dt = datetime.fromtimestamp(start_ts) if start_ts else None
            tags = list(broadcast.get('tags') or [])
            if not tags:
                broadcaster = str(broadcast.get('broadcaster') or '').strip()
                if broadcaster:
                    tags.append(broadcaster)
            for tag in tags:
                tag_counts[tag] = tag_counts.get(tag, 0) + 1

            image_url = str(broadcast.get('image_url') or '')
            if image_url.startswith('./'):
                image_url = f"{broadcast['lv_value']}/{image_url[2:]}"

            records.append({
                'lv': broadcast['lv_value'],
                'title': broadcast.get('title') or 'タイトル不明',
                'url': page_relative_url(broadcast.get('html_file') or ''),
                'date': dt.strftime('%Y-%m-%d %H:%M:%S') if dt else '',
                'year': dt.strftime('%Y') if dt else 'Unknown',
                'md': dt.strftime('%m/%d') if dt else '--/--',
                'hm': dt.strftime('%H:%M') if dt else '--:--',
                'broadcaster': broadcast.get('broadcaster') or '不明',
                'watch_count': broadcast.get('watch_count') or 0,
                'comment_count': broadcast.get('comment_count') or 0,
                'elapsed_time': broadcast.get('elapsed_time') or '',
                'summary': broadcast.get('summary_text') or '',
                'image_url': page_relative_url(image_url),
                'tags': tags,
                'transcript_segments': broadcast.get('transcript_segments') or [],
                'history_deleted': bool(broadcast.get('history_deleted')),
            })

        records.sort(key=lambda item: item['date'], reverse=True)
        years = []
        for record in records:
            if record['year'] not in years:
                years.append(record['year'])

        data_json = safe_json(records)
        tag_json = safe_json(tag_counts)
        special_links = collect_special_user_links(account_dir)
        for special_link in special_links:
            special_link['url'] = page_relative_url(special_link.get('url'))
        special_json = safe_json(special_links)
        generated_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        compact_css = """
.wrap{max-width:980px;padding:32px 28px 52px}
header.top{margin-bottom:20px}
.h-row{padding-bottom:8px}
h1{font-size:25px}
.meta-line{margin-top:6px}
.search{margin-top:16px;padding:7px 11px}
.tags-wrap{margin-top:10px}
.tags-label{margin-bottom:5px}
.tag{padding:2px 8px;font-size:11px}
.special-links{margin-top:12px;padding:9px 12px}
.year-section{margin-top:22px}
.year-head{padding-bottom:4px;margin-bottom:2px}
.year-head h2{font-size:18px}
.post{grid-template-columns:64px 88px 1fr;gap:10px;padding:7px 0}
.p-cover{width:88px;height:55px}
.p-thumb,.p-thumb-empty{width:88px;height:55px}
.p-date{font-size:11px;line-height:1.2;padding-top:1px}
.p-date .md{font-size:12px}
.p-date .hm{font-size:10px;margin-top:0}
.p-body{gap:2px}
.p-title{font-size:14px;line-height:1.3}
.p-tags{gap:3px}
.p-tag{font-size:10px;padding:1px 6px;line-height:1.35}
.p-meta{font-size:10.5px;line-height:1.3;gap:8px}
.summary-pop{left:74px;top:calc(100% - 2px)}
.flow-line{inset:0 0 0 180px}
@media(max-width:700px){.wrap{padding:22px 16px 42px}.p-thumb,.p-thumb-empty{width:82px;height:52px}}
""" if compact else ''
        back_link_html = ''
        if back_href:
            back_link_html = (
                f'<a class="back-link" href="{html.escape(back_href)}">'
                f'&larr; {html.escape(back_label)}</a>'
            )

        html_content = f"""<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8" />
<title>{html.escape(document_title)}</title>
<meta name="viewport" content="width=device-width,initial-scale=1" />
<link rel="preconnect" href="https://fonts.googleapis.com" />
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
<link href="https://fonts.googleapis.com/css2?family=Reggae+One&family=Noto+Sans+JP:wght@400;500;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet" />
<style>
*,*::before,*::after{{box-sizing:border-box}}
:root{{
  --bg:#111015;--paper:#1b171f;--ink:#ffeaf2;--ink-soft:#f2c8d9;--mute:#b98fa2;
  --line:#3a2b35;--accent:#ff9ec2;--accent-soft:#3a2230;--tag-bg:#2a2028;
  --tag-bg-active:#ffd6e7;--tag-text-active:#111111;
}}
html,body{{margin:0;padding:0;background:var(--bg);color:var(--ink)}}
body{{font-family:"Noto Sans JP",system-ui,sans-serif;font-size:14px;line-height:1.6;-webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility}}
.wrap{{max-width:920px;margin:0 auto;padding:56px 32px 80px}}
@media(max-width:700px){{.wrap{{padding:28px 18px 60px}}}}
header.top{{margin-bottom:36px}}
.h-row{{display:flex;align-items:baseline;gap:14px;border-bottom:1px solid var(--ink);padding-bottom:12px;flex-wrap:wrap}}
h1{{font-family:"Reggae One",system-ui,sans-serif;font-weight:700;font-size:30px;letter-spacing:.04em;margin:0}}
.h-en{{font-family:"JetBrains Mono",monospace;font-size:11px;letter-spacing:.22em;color:var(--mute);text-transform:uppercase;margin-left:auto}}
.meta-line{{margin-top:10px;font-size:12px;color:var(--mute);font-family:"JetBrains Mono",monospace;letter-spacing:.04em;display:flex;gap:18px;flex-wrap:wrap}}
.meta-line b{{color:var(--ink);font-weight:500}}
.search{{margin-top:28px;display:flex;align-items:center;gap:10px;border:1px solid var(--line);background:var(--paper);border-radius:2px;padding:10px 14px;transition:border-color .2s ease,box-shadow .2s ease}}
.search:focus-within{{border-color:var(--ink);box-shadow:0 0 0 3px rgba(255,158,194,.14)}}
.search svg{{flex-shrink:0;color:var(--mute)}}
.search input{{appearance:none;border:0;background:transparent;outline:none;flex:1;min-width:0;font:inherit;color:var(--ink);font-size:14px}}
.search .clear{{appearance:none;border:0;background:transparent;color:var(--mute);cursor:default;padding:2px 6px;border-radius:999px;font-size:12px}}
.search .clear:hover{{color:var(--ink);background:var(--bg)}}
.search-type{{appearance:none;border:1px solid var(--line);background:var(--bg);color:var(--ink);border-radius:3px;padding:5px 9px;font:inherit;font-size:12px;cursor:pointer}}
.tags-wrap{{margin-top:18px}}
.tags-label{{font-size:10px;letter-spacing:.14em;text-transform:uppercase;color:var(--mute);font-family:"JetBrains Mono",monospace;margin-bottom:8px}}
.tags{{display:flex;flex-wrap:wrap;gap:6px}}
.tag{{appearance:none;border:0;font:inherit;font-size:12px;color:var(--ink-soft);background:var(--tag-bg);padding:4px 10px;border-radius:999px;cursor:default;transition:background .15s ease,color .15s ease;display:inline-flex;align-items:center;gap:6px;white-space:nowrap}}
.tag:hover{{background:#3a2a36}}
.tag.active{{background:var(--tag-bg-active);color:var(--tag-text-active)}}
.tag .count{{font-family:"JetBrains Mono",monospace;font-size:10.5px;color:var(--mute);font-variant-numeric:tabular-nums}}
.tag.active .count{{color:rgba(17,17,17,.62)}}
.special-links{{margin-top:22px;padding:14px 16px;border:1px solid var(--line);background:linear-gradient(135deg,rgba(255,158,194,.08),rgba(27,23,31,.72));border-radius:10px;display:none}}
.special-links.show{{display:block}}
.special-head{{display:flex;align-items:baseline;justify-content:space-between;gap:12px;margin-bottom:10px}}
.special-head b{{font-family:"Reggae One",system-ui,sans-serif;font-size:16px;font-weight:500;letter-spacing:.04em}}
.special-head span{{font-family:"JetBrains Mono",monospace;font-size:10px;color:var(--mute);letter-spacing:.12em;text-transform:uppercase}}
.special-list{{display:flex;flex-wrap:wrap;gap:8px}}
.special-link{{display:inline-flex;align-items:center;gap:8px;text-decoration:none;color:var(--ink);background:var(--tag-bg);border:1px solid rgba(255,158,194,.22);border-radius:999px;padding:6px 12px;font-size:12px;transition:background .15s ease,color .15s ease,border-color .15s ease}}
.special-link:hover{{background:var(--ink);color:var(--paper);border-color:var(--ink)}}
.special-link small{{font-family:"JetBrains Mono",monospace;color:var(--mute);font-size:10px}}
.special-link:hover small{{color:rgba(17,17,17,.62)}}
.year-section{{margin-top:40px}}
.year-head{{display:flex;align-items:baseline;gap:14px;padding-bottom:8px;margin-bottom:8px;border-bottom:1px solid var(--line)}}
.year-head h2{{font-family:"Reggae One",system-ui,sans-serif;font-size:22px;font-weight:500;margin:0;letter-spacing:.04em}}
.year-head .y-count{{font-family:"JetBrains Mono",monospace;font-size:11px;color:var(--mute)}}
.post{{position:relative;display:grid;grid-template-columns:78px 120px 1fr;gap:16px;padding:14px 0;border-bottom:1px dashed var(--line);transition:background .15s ease}}
.post:hover{{background:rgba(255,255,255,.035);z-index:5}}
.post.history-deleted{{position:relative;overflow:visible;isolation:isolate;background:rgba(150,45,65,.12);box-shadow:inset 3px 0 0 rgba(255,125,145,.55);animation:history-deleted-heartbeat 2.4s ease-in-out infinite}}
.post.history-deleted::before{{content:none}}
.history-deleted-marquee{{position:absolute!important;inset:0;z-index:0!important;overflow:hidden;pointer-events:none;user-select:none;-webkit-mask-image:linear-gradient(to right,transparent 0,#000 10%,#000 90%,transparent 100%);mask-image:linear-gradient(to right,transparent 0,#000 10%,#000 90%,transparent 100%)}}
.history-deleted-marquee canvas{{display:block;width:100%;height:100%}}
.post.history-deleted>*{{position:relative;z-index:1}}
.post.history-deleted:hover{{background:rgba(150,45,65,.28)}}
@keyframes history-deleted-heartbeat{{0%,28%,62%,100%{{background:transparent;box-shadow:inset 3px 0 0 transparent,0 0 0 transparent}}14%{{background:rgba(190,45,70,.34);box-shadow:inset 4px 0 0 rgba(255,145,165,.9),0 0 20px rgba(255,75,110,.34)}}43%{{background:rgba(175,45,68,.23);box-shadow:inset 3px 0 0 rgba(255,135,155,.72),0 0 11px rgba(255,75,110,.20)}}}}
@media(max-width:600px){{.post{{grid-template-columns:60px 84px 1fr;gap:10px}}}}
.p-date{{font-family:"JetBrains Mono",monospace;font-size:12px;color:var(--mute);line-height:1.4;padding-top:2px}}
.p-date .md{{color:var(--ink);font-weight:500;font-size:13px}}
.p-date .hm{{display:block;color:var(--mute);font-size:11px;margin-top:1px}}
.p-thumb{{width:120px;height:68px;object-fit:cover;border-radius:6px;background:#242229}}
.p-thumb-empty{{width:120px;height:68px;background:#242229;border-radius:6px}}
@media(max-width:600px){{.p-thumb,.p-thumb-empty{{width:84px;height:48px}}}}
.p-cover{{position:relative;width:112px;height:70px;border:1px solid var(--line);border-radius:8px;background:var(--paper);overflow:visible;z-index:6}}
.p-cover img{{display:block;width:100%;height:100%;object-fit:cover;border-radius:7px;transition:transform .18s ease,box-shadow .18s ease;transform-origin:left center}}
.p-cover:hover img{{transform:scale(2.6);box-shadow:0 18px 54px rgba(0,0,0,.68);position:relative;z-index:20}}
.p-cover.empty{{display:grid;place-items:center;padding:0;color:var(--mute);font-size:9px;text-align:center}}
.p-body{{display:flex;flex-direction:column;gap:6px;min-width:0}}
.p-title{{margin:0;font-size:15px;font-weight:500;line-height:1.5;color:var(--ink);position:relative;overflow:hidden}}
.p-title a{{color:inherit;text-decoration:none;border-bottom:1px solid transparent;transition:border-color .15s ease,color .15s ease;display:inline-flex;max-width:100%;white-space:nowrap}}
.p-title::before{{content:"";position:absolute;inset:0 auto 0 0;width:42px;pointer-events:none;opacity:0;z-index:2;background:linear-gradient(90deg,var(--bg),rgba(17,16,21,0));transition:opacity .18s ease}}
.post:hover .p-title::before{{opacity:1}}
.post:hover .p-title a{{animation:title-marquee 7s linear infinite;border-bottom-color:currentColor}}
@keyframes title-marquee{{0%,12%{{transform:translateX(0)}}82%,100%{{transform:translateX(calc(-100% + min(100%,560px)))}}}}
.p-title a:hover{{color:var(--accent);border-bottom-color:currentColor}}
.p-title .arrow{{display:inline-block;font-family:"JetBrains Mono",monospace;font-size:12px;color:var(--mute);margin-left:4px;transition:transform .15s ease,color .15s ease}}
.p-title a:hover .arrow{{color:var(--accent);transform:translateX(2px)}}
.p-tags{{display:flex;flex-wrap:wrap;gap:4px}}
.p-tag{{font-size:10.5px;color:var(--mute);background:var(--tag-bg);padding:2px 7px;border-radius:999px;cursor:default;transition:background .15s ease,color .15s ease;text-decoration:none}}
.p-tag:hover{{background:var(--ink);color:var(--paper)}}
.p-tag.highlight{{background:var(--accent-soft);color:var(--accent)}}
.p-meta{{font-family:"JetBrains Mono",monospace;font-size:11px;color:var(--mute);display:flex;gap:10px;flex-wrap:wrap}}
.summary-pop{{position:absolute;left:94px;top:calc(100% - 4px);width:min(1300px,calc(100vw - 100px));display:grid;grid-template-columns:600px 1fr;gap:14px;padding:14px;border:1px solid var(--line);border-radius:10px;background:rgba(27,23,31,.97);box-shadow:0 18px 50px rgba(0,0,0,.42);color:var(--ink);opacity:0;pointer-events:none;transform:translateY(8px);transition:opacity .16s ease,transform .16s ease}}
.post:has(.p-thumb:hover) .summary-pop,.post:has(.p-title a:hover) .summary-pop{{opacity:1;transform:translateY(0)}}
.summary-image{{width:600px;aspect-ratio:16/10;border-radius:8px;border:1px solid var(--line);background:radial-gradient(circle at 25% 30%,rgba(255,158,194,.34),transparent 34%),linear-gradient(135deg,#271a24,#141219 70%);display:grid;place-items:center;color:var(--mute);font-size:11px;overflow:hidden}}
.summary-image img{{width:100%;height:100%;object-fit:cover;display:block}}
.summary-text{{min-width:0;font-size:17px;line-height:1.8;color:var(--ink-soft)}}
.summary-text b{{display:block;margin-bottom:4px;color:var(--ink);font-size:14px;letter-spacing:.08em}}
.flow-line{{position:absolute;inset:0 0 0 222px;pointer-events:none;overflow:hidden;opacity:0;z-index:2;transition:opacity .15s ease;mask-image:linear-gradient(90deg,transparent 0,rgba(0,0,0,.12) 26%,#000 48%,#000 82%,transparent 100%)}}
.post:hover .flow-line{{opacity:.82}}
.flow-comment{{position:absolute;left:100%;white-space:nowrap;color:rgba(255,234,242,.82);font-size:12px;background:rgba(58,34,48,.78);border:1px solid rgba(255,158,194,.22);border-radius:999px;padding:2px 9px;animation:flow-left 8s linear forwards;box-shadow:0 6px 18px rgba(0,0,0,.22);display:inline-flex;align-items:center;gap:6px}}
.flow-comment img{{width:18px;height:18px;border-radius:50%;object-fit:cover;flex:0 0 18px;background:var(--tag-bg)}}
@keyframes flow-left{{from{{transform:translateX(0);opacity:1}}60%{{opacity:1}}to{{transform:translateX(calc(-100vw - 100%));opacity:0}}}}
mark{{background:var(--accent-soft);color:var(--accent);padding:0 2px;border-radius:2px}}
.empty{{text-align:center;padding:60px 20px;color:var(--mute);font-family:"JetBrains Mono",monospace;font-size:13px}}
.top-btn{{position:fixed;right:24px;bottom:24px;appearance:none;border:1px solid var(--line);background:var(--paper);color:var(--ink);width:44px;height:44px;border-radius:50%;cursor:default;font-size:16px;display:grid;place-items:center;box-shadow:0 4px 18px rgba(0,0,0,.35);opacity:0;pointer-events:none;transition:opacity .25s ease,transform .25s ease;transform:translateY(8px)}}
.top-btn.show{{opacity:1;pointer-events:auto;transform:none}}
.top-btn:hover{{background:var(--ink);color:var(--paper);border-color:var(--ink)}}
.back-link{{display:inline-flex;margin-bottom:12px;color:var(--accent);font-family:"JetBrains Mono",monospace;font-size:11px;text-decoration:none;letter-spacing:.04em}}
.back-link:hover{{color:var(--ink);text-decoration:underline}}
@media(max-width:700px){{.p-cover{{width:82px;height:52px}}.p-cover:hover img{{transform:scale(2)}}.summary-pop{{left:0;width:min(100%,calc(100vw - 36px));grid-template-columns:1fr}}.summary-image{{width:100%}}.flow-line{{display:none}}}}
.summary-pop{{left:auto!important;right:0!important;box-sizing:border-box;max-width:calc(100vw - 32px)!important}}
.summary-text{{overflow-wrap:anywhere;max-height:min(60vh,520px);overflow-y:auto}}
@media(max-width:600px){{.summary-pop{{left:0!important;right:0!important;width:100%!important;max-width:100%!important}}.summary-image{{max-width:100%;height:auto}}}}
{compact_css}
</style>
</head>
<body>
<div class="wrap">
  <header class="top">
    {back_link_html}
    <div class="h-row"><h1>{html.escape(heading)}</h1><span class="h-en">{html.escape(heading_en)}</span></div>
    <div class="meta-line">
      <span>全 <b id="totalCount">0</b> 件</span>
      <span>表示 <b id="shownCount">0</b> 件</span>
      <span id="dateRange">generated {html.escape(generated_at)}</span>
    </div>
    <div class="search">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/></svg>
      <select id="searchType" class="search-type" aria-label="検索種別"><option value="all">全て</option><option value="transcript">文字おこし</option><option value="summary">要約</option><option value="comments">コメント</option></select>
      <input type="search" id="search" placeholder="全てから検索..." autocomplete="off" />
      <button class="clear" id="clearBtn" hidden>x クリア</button>
    </div>
    <div class="tags-wrap">
      <div class="tags-label">TAGS - 複数選択可（選択タグのいずれかを含む放送）</div>
      <div class="tags" id="tagCloud"></div>
    </div>
    <section class="special-links" id="specialLinks">
      <div class="special-head"><b>スペシャルユーザー</b><span>Special user pages</span></div>
      <div class="special-list" id="specialList"></div>
    </section>
  </header>
  <main id="list"></main>
</div>
<button class="top-btn" id="topBtn" aria-label="ページ上部へ">↑</button>
<script id="archive-data" type="application/json">{data_json}</script>
<script id="tag-data" type="application/json">{tag_json}</script>
<script id="special-data" type="application/json">{special_json}</script>
<script>
const records = JSON.parse(document.getElementById('archive-data').textContent);
const tagCounts = JSON.parse(document.getElementById('tag-data').textContent);
const specialLinks = JSON.parse(document.getElementById('special-data').textContent);
const tagPagePrefix = {json.dumps(page_relative_url('tags/'), ensure_ascii=False)};
const state = {{ query: '', searchType: 'all', tags: new Set() }};
const list = document.getElementById('list');
const search = document.getElementById('search');
const searchType = document.getElementById('searchType');
const clearBtn = document.getElementById('clearBtn');
const totalCount = document.getElementById('totalCount');
const shownCount = document.getElementById('shownCount');
const tagCloud = document.getElementById('tagCloud');
const specialLinksBox = document.getElementById('specialLinks');
const specialList = document.getElementById('specialList');
const commentIntervals = new Map();
const commentCache = new Map();
const commentSearchTexts = new Map();
let renderSequence = 0;
totalCount.textContent = records.length;
function esc(s){{return String(s ?? '').replace(/[&<>"']/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]));}}
function normalize(s){{return String(s ?? '').toLowerCase();}}
function renderSpecialLinks(){{
  if(!specialLinks.length) return;
  specialLinksBox.classList.add('show');
  specialList.innerHTML = specialLinks.map(link => `<a class="special-link" href="${{esc(link.url)}}">${{esc(link.label)}} <small>${{esc(link.user_id)}}</small></a>`).join('');
}}
function makeTags(){{
  const tags = [['all', records.length], ...Object.entries(tagCounts).sort((a,b)=>b[1]-a[1])];
  tagCloud.innerHTML = tags.map(([tag,count])=>`<button class="tag ${{(tag==='all'&&!state.tags.size)||state.tags.has(tag)?'active':''}}" data-tag="${{esc(tag)}}">${{esc(tag==='all'?'すべて':tag)}} <span class="count">${{count}}</span></button>`).join('');
  tagCloud.querySelectorAll('.tag').forEach(btn=>btn.addEventListener('click',()=>{{const tag=btn.dataset.tag;if(tag==='all')state.tags.clear();else if(state.tags.has(tag))state.tags.delete(tag);else state.tags.add(tag);render();}}));
}}
function matchRecord(r){{
  const q = normalize(state.query);
  const tagOk = !state.tags.size || r.tags.some(tag=>state.tags.has(tag));
  if(!tagOk) return false;
  if(!q) return true;
  const fields={{transcript:r.transcript_segments||[],summary:[r.summary],comments:[commentSearchTexts.get(r.lv)||''],all:[r.title,r.broadcaster,r.lv,r.summary,...r.tags,...(r.transcript_segments||[]),commentSearchTexts.get(r.lv)||'']}};
  return normalize((fields[state.searchType]||fields.all).join(' ')).includes(q);
}}
function markText(text){{
  if(!state.query) return esc(text);
  const q = state.query.replace(/[.*+?^${{}}()|[\\]\\\\]/g,'\\\\$&');
  return esc(text).replace(new RegExp(q,'ig'),m=>`<mark>${{m}}</mark>`);
}}
async function ensureCommentSearchIndex(){{const missing=records.filter(r=>!commentSearchTexts.has(r.lv));if(!missing.length)return;shownCount.textContent='読込中...';await Promise.all(missing.map(async r=>{{const items=await fetchCommentItems(r);commentSearchTexts.set(r.lv,items.map(item=>item.text).join(' '));}}));}}
async function render(){{
  const sequence=++renderSequence;
  makeTags();
  if(state.query&&(state.searchType==='all'||state.searchType==='comments'))await ensureCommentSearchIndex();
  if(sequence!==renderSequence)return;
  const rows = records.filter(matchRecord);
  shownCount.textContent = rows.length;
  clearBtn.hidden = !state.query;
  if(!rows.length){{list.innerHTML='<div class="empty">該当するリンクがありません</div>';return;}}
  const byYear = new Map();
  rows.forEach(r=>{{if(!byYear.has(r.year))byYear.set(r.year,[]);byYear.get(r.year).push(r);}});
  list.innerHTML = [...byYear.entries()].map(([year,items])=>`
    <section class="year-section">
      <div class="year-head"><h2>${{esc(year)}}</h2><span class="y-count">${{items.length}} links</span></div>
      ${{items.map(renderPost).join('')}}
    </section>`).join('');
  bindPostCommentFlow();
}}
function renderPost(r){{
  const tags = r.tags.map(t=>`<a class="p-tag ${{state.tags.has(t)?'highlight':''}}" href="${{tagPagePrefix}}tag_${{encodeURIComponent(t)}}.html">${{esc(t)}}</a>`).join('');
  const img = r.image_url ? `<img src="${{esc(r.image_url)}}" alt="">` : 'summary image';
  const summary = esc(r.summary || '要約なし').replace(/。/g, '。<br>');
  return `<article class="post${{r.history_deleted?' history-deleted':''}}">
    <div class="p-date"><span class="md">${{esc(r.md)}}</span><span class="hm">${{esc(r.hm)}}</span></div>
    ${{r.image_url ? `<img class="p-thumb" src="${{esc(r.image_url)}}" alt="要約画像" loading="lazy">` : `<div class="p-thumb-empty"></div>`}}
    <div class="p-body">
      <h3 class="p-title"><a href="${{esc(r.url)}}">${{markText(r.title)}}<span class="arrow">-></span></a></h3>
      <div class="p-tags">${{tags}}</div>
      <div class="p-meta"><span>${{esc(r.broadcaster)}}</span><span>${{esc(r.elapsed_time)}}</span><span>${{esc(r.watch_count)}} views</span><span>${{esc(r.comment_count)}} comments</span></div>
      <div class="flow-line" data-lv="${{esc(r.lv)}}"></div>
      <div class="summary-pop"><div class="summary-image">${{img}}</div><div class="summary-text"><b>SUMMARY</b>${{summary}}</div></div>
    </div>
  </article>`;
}}
function bindPostCommentFlow(){{
  commentIntervals.forEach(interval => clearInterval(interval));
  commentIntervals.clear();
  document.querySelectorAll('.post').forEach(post => {{
    post.addEventListener('mouseenter', () => startCommentFlow(post));
    post.addEventListener('mouseleave', () => stopCommentFlow(post));
  }});
}}
async function fetchCommentItems(record){{
  if(commentCache.has(record.lv)) return commentCache.get(record.lv);
  try{{
    const res = await fetch(record.url, {{ cache: 'no-store' }});
    if(!res.ok) throw new Error(`HTTP ${{res.status}}`);
    const htmlText = await res.text();
    const doc = new DOMParser().parseFromString(htmlText, 'text/html');
    let commentNodes = [...doc.querySelectorAll('#timeline2 .comment-item')];
    if(!commentNodes.length) {{
      const payload = doc.getElementById('nico-virtual-timeline-data');
      if(payload) {{
        const virtualData = JSON.parse(payload.textContent || '{{}}');
        const virtualDoc = new DOMParser().parseFromString(
          `<div id="timeline2">${{(virtualData.timeline2 || []).join('')}}</div>`,
          'text/html'
        );
        commentNodes = [...virtualDoc.querySelectorAll('#timeline2 .comment-item')];
      }}
    }}
    const items = commentNodes.map(item => {{
      const icon = item.querySelector('img')?.getAttribute('src') || '';
      const clone = item.cloneNode(true);
      clone.querySelectorAll('a,img').forEach(el => el.remove());
      let text = clone.textContent.replace(/\\s+/g, ' ').trim();
      text = text.replace(/^\\d+\\s*\\|\\s*\\d{{2}}:\\d{{2}}:\\d{{2}}\\s*-\\s*:?\\s*/, '').trim();
      text = text.replace(/^[:：-]+\\s*/, '').trim();
      return {{ text, icon }};
    }}).filter(item => item.text);
    commentCache.set(record.lv, items);
    return items;
  }}catch(err){{
    console.warn('コメントHTML取得失敗', record.url, err);
    commentCache.set(record.lv, []);
    return [];
  }}
}}
async function startCommentFlow(post){{
  const line = post.querySelector('.flow-line');
  const lv = line?.dataset.lv;
  const record = records.find(r => r.lv === lv);
  if(!line || !record || commentIntervals.has(lv)) return;
  const comments = await fetchCommentItems(record);
  if(!comments.length) return;
  let index = 0;
  const spawn = () => {{
    const item = comments[index % comments.length];
    const node = document.createElement('span');
    node.className = 'flow-comment';
    if(item.icon){{
      const img = document.createElement('img');
      img.src = item.icon;
      img.alt = '';
      img.onerror = () => img.remove();
      node.appendChild(img);
    }}
    const text = document.createElement('span');
    text.textContent = item.text;
    node.appendChild(text);
    node.style.top = `${{8 + Math.random() * 42}}px`;
    line.appendChild(node);
    setTimeout(() => node.remove(), 8200);
    index++;
  }};
  spawn();
  commentIntervals.set(lv, setInterval(spawn, 1400));
}}
function stopCommentFlow(post){{
  const line = post.querySelector('.flow-line');
  const lv = line?.dataset.lv;
  const interval = commentIntervals.get(lv);
  if(interval) clearInterval(interval);
  commentIntervals.delete(lv);
  if(line) line.innerHTML = '';
}}
search.addEventListener('input',()=>{{state.query=search.value.trim();render();}});
searchType.addEventListener('change',()=>{{state.searchType=searchType.value;const labels={{all:'全て',transcript:'文字おこし',summary:'要約',comments:'コメント'}};search.placeholder=`${{labels[state.searchType]}}から検索...`;render();}});
clearBtn.addEventListener('click',()=>{{search.value='';state.query='';render();search.focus();}});
const topBtn=document.getElementById('topBtn');
window.addEventListener('scroll',()=>topBtn.classList.toggle('show',window.scrollY>360));
topBtn.addEventListener('click',()=>window.scrollTo({{top:0,behavior:'smooth'}}));
renderSpecialLinks();
render();
const deletedMarqueeCanvases=new Set();
const decorateDeletedPosts=()=>document.querySelectorAll('.post.history-deleted:not(:has(.history-deleted-marquee))').forEach(post=>{{
  const clip=document.createElement('div');
  clip.className='history-deleted-marquee';
  const canvas=document.createElement('canvas');
  clip.appendChild(canvas);
  deletedMarqueeCanvases.add(canvas);
  post.prepend(clip);
}});
decorateDeletedPosts();
new MutationObserver(decorateDeletedPosts).observe(document.body,{{childList:true,subtree:true}});
const drawDeletedMarquees=time=>{{
  deletedMarqueeCanvases.forEach(canvas=>{{
    if(!canvas.isConnected){{deletedMarqueeCanvases.delete(canvas);return}}
    const box=canvas.getBoundingClientRect(),dpr=devicePixelRatio||1,w=box.width,h=box.height;
    if(!w||!h)return;
    if(canvas.width!==Math.round(w*dpr)||canvas.height!==Math.round(h*dpr)){{canvas.width=Math.round(w*dpr);canvas.height=Math.round(h*dpr)}}
    const ctx=canvas.getContext('2d');ctx.setTransform(dpr,0,0,dpr,0,0);ctx.clearRect(0,0,w,h);
    const size=Math.min(112,Math.max(48,innerWidth*.08)),text='消されてしまった放送ページ　';
    ctx.font=`900 ${{size}}px sans-serif`;ctx.textBaseline='middle';ctx.fillStyle='rgba(185,185,190,.30)';ctx.filter='blur(8px)';
    const spacing=.72,unit=ctx.measureText(text).width*spacing,offset=-((time*.055)%unit);
    for(let base=offset-unit;base<w+unit;base+=unit){{let x=base;for(const ch of text){{const cw=ctx.measureText(ch).width,cx=x+cw*spacing/2;if(cx>=0&&cx<=w){{const angle=Math.PI*(cx/w-.5),sx=Math.max(0,Math.cos(angle)),mx=w/2+(w/2)*Math.sin(angle);ctx.save();ctx.translate(mx,h/2);ctx.scale(sx,1);ctx.globalAlpha=.35+.65*sx;ctx.fillText(ch,-cw/2,0);ctx.restore()}}x+=cw*spacing}}}}
  }});
  requestAnimationFrame(drawDeletedMarquees);
}};
requestAnimationFrame(drawDeletedMarquees);
</script>
</body>
</html>"""

        index_file = os.fspath(output_file or os.path.join(account_dir, 'index.html'))
        os.makedirs(os.path.dirname(index_file), exist_ok=True)
        existed = os.path.isfile(index_file)
        previous = Path(index_file).read_text(encoding='utf-8') if existed else None
        changed = previous != html_content
        if changed:
            atomic_write_text(index_file, html_content)
        if changed and change_log is not None:
            change_log.append(index_file)

        action = '管理データ更新' if existed else '新規生成'
        print(f"モダン一覧ページ{action}: {index_file}")
        return index_file

    except Exception as e:
        print(f"一覧ページ生成エラー: {str(e)}")
        raise


def collect_special_user_links(account_dir):
    """生成済みスペシャルユーザー一覧ページへのリンクを収集"""
    links = []
    try:
        for item in sorted(os.listdir(account_dir)):
            if not item.startswith('special_user_'):
                continue
            user_id = item.replace('special_user_', '', 1)
            user_dir = os.path.join(account_dir, item)
            if not os.path.isdir(user_dir):
                continue
            list_file = os.path.join(user_dir, f"{user_id}_list.html")
            if not os.path.exists(list_file):
                continue
            label = read_special_user_label(list_file, user_id)
            links.append({
                "user_id": user_id,
                "label": label,
                "url": f"{item}/{user_id}_list.html",
            })
    except Exception as exc:
        print(f"スペシャルユーザーリンク収集エラー: {exc}")
    return links


def read_special_user_label(list_file, user_id):
    """一覧HTMLから表示名を軽く推定。取れなければID表示。"""
    try:
        text = open(list_file, 'r', encoding='utf-8').read()
        import re
        for pattern in (
            r'<a[^>]+href="https://www\.nicovideo\.jp/user/%s"[^>]*>(.*?)</a>' % re.escape(user_id),
            r'<title>(.*?)</title>',
        ):
            match = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
            if match:
                value = re.sub(r'<[^>]+>', '', match.group(1)).strip()
                value = value.replace('のリンク一覧', '').replace('Chat Data', '').strip()
                if value:
                    return value
    except Exception:
        pass
    return f"スペシャルユーザー {user_id}"
