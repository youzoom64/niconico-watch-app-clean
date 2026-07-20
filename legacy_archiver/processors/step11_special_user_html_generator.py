# processors/step11_06_special_user_html_generator.py
import json
import os
from datetime import datetime
import shutil
import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from codex_exec_runner import CodexExecConfig, run_codex_exec
from archive_db import load_broadcast_data as load_broadcast_data_from_db
from archive_db import load_comments_payload
from utils import find_account_directory
try:
    import google.generativeai as genai
    GEMINI_AVAILABLE = True
except ImportError:
    GEMINI_AVAILABLE = False


def process(pipeline_data):
    """Step11: comments.jsonを使用したスペシャルユーザー処理"""
    try:
        lv_value = pipeline_data['lv_value']
        config = pipeline_data['config']
        
        print(f"Step11 開始: {lv_value}")
        
        # 1. アカウントディレクトリ検索
        account_dir = find_account_directory(pipeline_data['platform_directory'], pipeline_data['account_id'])
        broadcast_dir = os.path.join(account_dir, lv_value)
        
        # 2. 統合JSONファイル読み込み
        broadcast_data = load_broadcast_data(broadcast_dir, lv_value)
        
        # 3. DB由来コメントからスペシャルユーザーを検索
        special_users = get_special_users_from_config(config)
        comments_data = load_comments_payload(lv_value)
        found_special_users = find_special_users_in_comments_data(comments_data, special_users)
        
        # 4. スペシャルユーザーが見つかった場合、ページを生成
        if found_special_users:
            for user_data in found_special_users:
                create_special_user_pages(user_data, broadcast_data, broadcast_dir, lv_value, config)
                
        print(f"Step11 完了: {lv_value} - 検出スペシャルユーザー数: {len(found_special_users)}")
        return {"special_users_found": len(found_special_users), "users": [u['user_id'] for u in found_special_users]}
        
    except Exception as e:
        print(f"Step11 エラー: {str(e)}")
        raise

def load_broadcast_data(broadcast_dir, lv_value):
    """放送データをDBから読み込み"""
    data = load_broadcast_data_from_db(lv_value)
    if data:
        return data
    raise Exception(f"放送データDBが見つかりません: {lv_value}")

def get_special_users_from_config(config):
    """設定からスペシャルユーザーリストを取得（詳細設定対応）"""
    # 新しい詳細設定から取得
    special_users_config = config.get("special_users_config", {})
    detailed_users = special_users_config.get("users", {})
    
    # 詳細設定があるユーザーIDを取得
    user_ids_from_detailed = list(detailed_users.keys())
    
    # 従来のsimple listも取得（後方互換性）
    user_ids_from_simple = config.get("special_users", [])
    
    # 両方をマージ（重複排除）
    all_user_ids = list(set(user_ids_from_detailed + user_ids_from_simple))
    
    print(f"詳細設定ユーザー: {user_ids_from_detailed}")
    print(f"シンプル設定ユーザー: {user_ids_from_simple}")
    print(f"統合ユーザーリスト: {all_user_ids}")
    
    return all_user_ids

def get_user_detail_config(config, user_id):
    """個別ユーザーの詳細設定を取得"""
    special_users_config = config.get("special_users_config", {})
    detailed_users = special_users_config.get("users", {})
    
    if user_id in detailed_users:
        return detailed_users[user_id]
    
    # デフォルト設定を返す
    return {
        "user_id": user_id,
        "display_name": f"ユーザー{user_id}",
        "analysis_enabled": special_users_config.get("default_analysis_enabled", True),
        "analysis_ai_model": special_users_config.get("default_analysis_ai_model", "openai-gpt4o"),
        "analysis_prompt": special_users_config.get("default_analysis_prompt", ""),
        "template": special_users_config.get("default_template", "user_detail.html"),
        "description": "",
        "tags": []
    }

def find_special_users_in_comments(comments_json_path, special_users):
    """comments.jsonからスペシャルユーザーを検索"""
    if not os.path.exists(comments_json_path):
        print("comments.jsonファイルが見つかりません")
        return []
    
    try:
        with open(comments_json_path, 'r', encoding='utf-8') as f:
            comments_data = json.load(f)
        
        found_users = {}
        
        print(f"検出したコメント数: {len(comments_data.get('comments', []))}")
        
        for comment in comments_data.get('comments', []):
            user_id = comment.get('user_id', '')
            user_name = comment.get('user_name', '')
            
            if user_id in special_users:
                if user_id not in found_users:
                    found_users[user_id] = {
                        'user_id': user_id,
                        'user_name': user_name or f"ユーザー{user_id}",
                        'comments': []
                    }

                # コメント情報を追加（XMLと同じ形式）
                comment_data = {
                    'no': comment.get('no', ''),
                    'date': comment.get('date', ''),
                    'broadcast_seconds': comment.get('broadcast_seconds', 0),  # JSON固有
                    'text': comment.get('text', ''),
                    'premium': comment.get('premium', ''),
                    'name': comment.get('user_name', '')
                }
                found_users[user_id]['comments'].append(comment_data)
                print(f"スペシャルユーザーコメント検出: {user_id} - {comment_data['text'][:50]}")
        
        print(f"スペシャルユーザー検出: {list(found_users.keys())}")
        return list(found_users.values())
        
    except Exception as e:
        print(f"comments.json解析エラー: {str(e)}")
        import traceback
        traceback.print_exc()
        return []

def find_special_users_in_comments_data(comments_data, special_users):
    """DB由来コメントデータからスペシャルユーザーを検索"""
    try:
        found_users = {}
        comments = comments_data.get('comments', []) if isinstance(comments_data, dict) else []
        print(f"検出したコメント数: {len(comments)}")

        for comment in comments:
            user_id = comment.get('user_id', '')
            user_name = comment.get('user_name', '')

            if user_id in special_users:
                if user_id not in found_users:
                    found_users[user_id] = {
                        'user_id': user_id,
                        'user_name': user_name or f"ユーザー{user_id}",
                        'comments': []
                    }

                comment_data = {
                    'no': comment.get('no', ''),
                    'date': comment.get('date', ''),
                    'broadcast_seconds': comment.get('broadcast_seconds', 0),
                    'text': comment.get('text', ''),
                    'premium': comment.get('premium', ''),
                    'name': comment.get('user_name', '')
                }
                found_users[user_id]['comments'].append(comment_data)
                print(f"スペシャルユーザーコメント検出: {user_id} - {comment_data['text'][:50]}")

        print(f"スペシャルユーザー検出: {list(found_users.keys())}")
        return list(found_users.values())
    except Exception as e:
        print(f"コメントDBデータ解析エラー: {str(e)}")
        import traceback
        traceback.print_exc()
        return []

def create_special_user_pages(user_data, broadcast_data, broadcast_dir, lv_value, config=None):
    """スペシャルユーザーの一覧ページと個別ページを生成"""
    try:
        user_id = user_data['user_id']
        user_name = user_data['user_name']
        comments = user_data['comments']
        
        print(f"スペシャルユーザーページ生成中: {user_id} ({user_name})")
        
        # ニックネームを取得して上書き
        real_nickname = get_user_nickname_with_cache(user_id)
        if real_nickname:
            user_data['user_name'] = real_nickname
            user_name = real_nickname
            print(f"実際のニックネーム取得: {user_id} -> {real_nickname}")
        
        # テンプレートディレクトリ
        template_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'templates')
        
        # アカウントディレクトリ直下にユーザーディレクトリ作成
        account_dir = os.path.dirname(broadcast_dir)
        user_output_dir = os.path.join(account_dir, f"special_user_{user_id}")
        os.makedirs(user_output_dir, exist_ok=True)
        
        # CSS/JSファイルをコピー
        copy_static_files(template_dir, user_output_dir)
        
        # 1. 個別ページ生成
        create_user_detail_page(user_data, broadcast_data, template_dir, user_output_dir, lv_value, config)
        
        # 2. 一覧ページ生成または更新
        update_user_list_page(user_data, broadcast_data, template_dir, user_output_dir, lv_value)
        
        print(f"スペシャルユーザーページ生成完了: {user_output_dir}")
        
    except Exception as e:
        print(f"スペシャルユーザーページ生成エラー: {str(e)}")
        raise

def get_user_nickname(user_id):
    """ニコニコ動画のユーザーページからニックネームを取得"""
    try:
        import requests
        from bs4 import BeautifulSoup
        import time
        
        url = f"https://www.nicovideo.jp/user/{user_id}"
        
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # ニックネームを取得
        nickname_element = soup.find(class_="UserDetailsHeader-nickname")
        
        if nickname_element:
            nickname = nickname_element.get_text(strip=True)
            print(f"ユーザー {user_id} のニックネーム: {nickname}")
            time.sleep(1)  # レート制限対策
            return nickname
        else:
            print(f"ユーザー {user_id} のニックネームが見つかりません")
            return None
            
    except Exception as e:
        print(f"ユーザー {user_id} の情報取得エラー: {e}")
        return None

def get_user_nickname_with_cache(user_id, cache_dir="user_cache"):
    """キャッシュ付きでニックネーム取得"""
    import json
    import os
    from datetime import datetime, timedelta
    
    # キャッシュディレクトリ作成
    os.makedirs(cache_dir, exist_ok=True)
    cache_file = os.path.join(cache_dir, f"{user_id}.json")
    
    # キャッシュチェック（7日間有効）
    if os.path.exists(cache_file):
        try:
            with open(cache_file, 'r', encoding='utf-8') as f:
                cache_data = json.load(f)
            
            cache_time = datetime.fromisoformat(cache_data['cached_at'])
            if datetime.now() - cache_time < timedelta(days=7):
                print(f"キャッシュからニックネーム取得: {user_id} -> {cache_data['nickname']}")
                return cache_data['nickname']
        except Exception as e:
            print(f"キャッシュ読み込みエラー: {e}")
    
    # 新規取得
    nickname = get_user_nickname(user_id)
    
    if nickname:
        # キャッシュ保存
        cache_data = {
            'user_id': user_id,
            'nickname': nickname,
            'cached_at': datetime.now().isoformat()
        }
        try:
            with open(cache_file, 'w', encoding='utf-8') as f:
                json.dump(cache_data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"キャッシュ保存エラー: {e}")
    
    return nickname

def create_user_detail_page(user_data, broadcast_data, template_dir, output_dir, lv_value, config=None):
    """個別ユーザーページを生成"""
    user_id = user_data['user_id']
    
    # ユーザーの詳細設定を取得
    if config:
        user_detail_config = get_user_detail_config(config, user_id)
        template_name = user_detail_config.get("template", "user_detail.html")
        print(f"ユーザー {user_id} のテンプレート: {template_name}")
    else:
        template_name = "user_detail.html"
    
    template_path = os.path.join(template_dir, template_name)
    if not os.path.exists(template_path):
        print(f"テンプレートファイルが見つかりません: {template_path}")
        template_path = os.path.join(template_dir, 'user_detail.html')
        if not os.path.exists(template_path):
            return
    
    with open(template_path, 'r', encoding='utf-8') as f:
        template = f.read()
    
    # コメント行を生成（broadcast_secondsを使用）
    comment_rows = generate_comment_rows(user_data['comments'])
    
    # 分析テキストを生成（詳細設定を考慮）
    if config:
        analysis_text = generate_analysis_text_with_config(user_data['comments'], config, user_id)
    else:
        analysis_text = generate_analysis_text(user_data['comments'])
    broadcast_url = f"https://live.nicovideo.jp/watch/lv{broadcast_data.get('live_num', '')}"

    # テンプレート変数を置換
    html_content = template.replace(
    '{{broadcast_title}}', 
    f'<a href="{broadcast_url}" target="_blank">{broadcast_data.get("live_title", "タイトル不明")}</a>')
    html_content = html_content.replace('{{start_time}}', format_start_time(broadcast_data.get('start_time', '')))
    html_content = html_content.replace('{{user_avatar}}', get_user_icon_path(user_data['user_id']))
    html_content = html_content.replace('{{user_name}}', user_data['user_name'])
    html_content = html_content.replace('{{user_profile_url}}', f"https://www.nicovideo.jp/user/{user_data['user_id']}")
    html_content = html_content.replace('{{user_id}}', user_data['user_id'])
    html_content = html_content.replace('{{comment_rows}}', comment_rows)
    html_content = html_content.replace('{{analysis_text}}', analysis_text)
    
    # ファイル保存
    output_path = os.path.join(output_dir, f"{user_data['user_id']}_{lv_value}_detail.html")
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(html_content)
    
    print(f"個別ページ生成: {output_path}")

def generate_comment_rows(comments):
    """コメントテーブルの行を生成（broadcast_secondsを使用）"""
    rows = []
    for i, comment in enumerate(comments, 1):
        # broadcast_secondsから直接時間を計算
        broadcast_seconds = comment.get('broadcast_seconds', 0)
        time_str = format_seconds_to_time(broadcast_seconds)
        date_str = format_unix_time(comment.get('date', ''))
        
        row = f'''
        <tr>
            <td>{i}</td>
            <td>{time_str}</td>
            <td>{date_str}</td>
            <td><b style="font-size: 25px;">{escape_html(comment.get('text', ''))}</b></td>
        </tr>'''
        rows.append(row)
    
    return '\n'.join(rows)

def format_seconds_to_time(seconds):
    """秒数を時間表記に変換"""
    try:
        seconds = int(seconds)
        minutes = seconds // 60
        hours = minutes // 60
        return f"{hours:02d}:{minutes%60:02d}:{seconds%60:02d}"
    except:
        return "00:00:00"

def generate_analysis_text_with_config(comments, config, user_id):
    """詳細設定を考慮したAI分析テキストを生成"""
    user_detail_config = get_user_detail_config(config, user_id)
    
    # 基本統計を最初に生成
    basic_stats = generate_basic_stats(comments)
    
    if not user_detail_config.get("analysis_enabled", True):
        return basic_stats + "このユーザーの分析は無効化されています。"
    
    # AI分析が有効な場合
    ai_analysis = None
    if user_detail_config.get("analysis_prompt"):
        engine = get_ai_task_engine(config, "special_user_summary")
        ai_model = model_for_engine(engine, user_detail_config.get("analysis_ai_model", "openai-gpt4o"))
        print(f"[INFO] スペシャルユーザーまとめ担当: {engine_label(engine)} / model={ai_model}")
        
        if engine in {"codex_exec", "claude", "grok"} or ai_model == "openai-gpt4o":
            ai_analysis = generate_ai_analysis(comments, config, user_detail_config)
        elif engine == "gemini" or ai_model == "google-gemini-2.5-flash":
            ai_analysis = generate_gemini_analysis(comments, config, user_detail_config)
    
    # 結果を組み合わせ
    if ai_analysis:
        result = basic_stats + ai_analysis
    else:
        result = basic_stats
    
    if user_detail_config.get("description"):
        result += f"<br><br><strong>メモ:</strong><br>{user_detail_config['description']}"
    
    return result

def generate_basic_stats(comments):
    """基本統計情報を生成"""
    if not comments:
        return "コメントがありません。<br><br>"
    
    total_comments = len(comments)
    total_chars = sum(len(comment.get('text', '')) for comment in comments)
    avg_chars = total_chars / total_comments if total_comments > 0 else 0
    
    return f"""
        - 総コメント数: {total_comments}件<br><br>
        - 平均文字数: {avg_chars:.1f}文字<br><br>
    """

def generate_ai_analysis(comments, config, user_detail_config):
    """OpenAI APIを使用してユーザー分析を生成"""
    try:
        # API設定を取得
        api_settings = config.get("api_settings", {})
        ai_model = user_detail_config.get("analysis_ai_model", "openai-gpt4o")  # OpenAIがデフォルト
        
        # コメントデータを整理
        comment_texts = []
        for comment in comments:
            timestamp = format_unix_time(comment.get('date', ''))
            broadcast_seconds = comment.get('broadcast_seconds', 0)
            time_str = format_seconds_to_time(broadcast_seconds)
            text = comment.get('text', '')
            comment_texts.append(f"[{timestamp} - 放送内時間:{time_str}] {text}")
        
        if not comment_texts:
            return "分析対象のコメントがありません。"
        
        # プロンプトを構築
        analysis_prompt = user_detail_config.get("analysis_prompt", "")
        analysis_prompt = analysis_prompt.replace("{name}", user_detail_config.get('display_name', user_detail_config['user_id']))

        # system promptも置換
        system_prompt = "あなたは優秀な精神科医です。次の文章は{name}と言う人物のコメントです。この文章を要約し、感情分析と精神分析をしてください。特に攻撃性と現実逃避に焦点を当てて下さい。要約は箇条書きにし、人物名に注目してください。そして鋭く批判的に要約してください。"
        system_prompt = system_prompt.replace("{name}", user_detail_config.get('display_name', user_detail_config['user_id']))

        user_data_text = "\n".join(comment_texts)
        
        full_prompt = f"""
{analysis_prompt}

ユーザーID: {user_detail_config['user_id']}
表示名: {user_detail_config.get('display_name', 'なし')}
総コメント数: {len(comments)}件

コメント履歴:
{user_data_text}

上記のデータを基に、このユーザーの詳細な分析を日本語で行ってください。
分析結果はHTML形式で出力し、<br>タグで改行してください。
"""

        codex_config = get_codex_exec_config(config)
        if codex_config.enabled:
            prompt = f"{system_prompt}\n\n{full_prompt}"
            print(f"[INFO] スペシャルユーザーまとめAI CLI呼び出し開始: provider={codex_config.provider} model={codex_config.model or '-'} prompt文字数={len(prompt)}")
            result = run_codex_exec(prompt, config=codex_config)
            if not result.ok:
                raise Exception(f"Codex exec failed: rc={result.returncode} stderr={result.stderr.strip()}")
            return (result.text or result.stdout).strip()

        import openai

        # OpenAI APIキーの確認
        openai_api_key = api_settings.get("openai_api_key", "")
        if not openai_api_key:
            print("OpenAI APIキーが設定されていません")
            return None

        # OpenAI APIを呼び出し
        client = openai.OpenAI(api_key=openai_api_key)
        
        response = client.chat.completions.create(
            model="gpt-4o" if ai_model == "openai-gpt4o" else "gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": system_prompt},  # 置換済みを使用
                {"role": "user", "content": full_prompt}
            ],
            max_tokens=1500,
            temperature=0.7
        )
        
        ai_result = response.choices[0].message.content.strip()
        

        return ai_result
        
    except Exception as e:
        print(f"AI分析エラー: {str(e)}")
        return f"AI分析中にエラーが発生しました: {str(e)}"

def get_codex_exec_config(config):
    raw = config.get("codex_exec", {})
    engine = get_ai_task_engine(config, "special_user_summary")
    enabled = bool(raw.get("enabled", False)) and engine in {"codex_exec", "claude", "grok"}
    return CodexExecConfig(
        enabled=enabled,
        provider=provider_for_engine(engine, raw),
        command=str(raw.get("command") or "codex"),
        cwd=str(raw.get("cwd") or os.getcwd()),
        timeout_seconds=int(raw.get("timeout_seconds") or 3600),
        model=model_for_cli_engine(engine, raw),
        effort=str(raw.get("effort") or ""),
        extra_args=tuple(str(arg) for arg in raw.get("extra_args", []) if str(arg).strip()),
    )

def get_ai_task_engine(config, task):
    return str(config.get("ai_task_engines", {}).get(task) or "openai")

def model_for_engine(engine, fallback_model):
    if engine == "codex_exec":
        return "openai-gpt4o"
    if engine == "claude":
        return "openai-gpt4o"
    if engine == "grok":
        return "openai-gpt4o"
    if engine == "gemini":
        return "google-gemini-2.5-flash"
    return "openai-gpt4o" if not fallback_model else fallback_model

def engine_label(engine):
    return {
        "codex_exec": "Codex exec",
        "claude": "ClaudeCode",
        "grok": "Grok build",
        "openai": "OpenAI API",
        "gemini": "Gemini API",
    }.get(engine, engine)

def provider_for_engine(engine, raw):
    if engine == "claude":
        return "claude"
    if engine == "grok":
        return "grok"
    return str(raw.get("provider") or "codex")

def model_for_cli_engine(engine, raw):
    if engine == "claude":
        return str(raw.get("claude_model") or raw.get("model") or "sonnet")
    if engine == "grok":
        return str(raw.get("grok_model") or raw.get("model") or "grok-build")
    return str(raw.get("model") or "")

def generate_gemini_analysis(comments, config, user_detail_config):
    """Google Gemini APIを使用してユーザー分析を生成"""
    try:
        import google.generativeai as genai
        
        # API設定を取得
        api_settings = config.get("api_settings", {})
        google_api_key = api_settings.get("google_api_key", "")
        
        if not google_api_key:
            print("Google APIキーが設定されていません")
            return None
        
        # Gemini APIを設定
        genai.configure(api_key=google_api_key)
        # 設定からモデル名を取得
        ai_model = user_detail_config.get("analysis_ai_model", "google-gemini-2.5-flash")
        model_name = ai_model.replace("google-", "") if ai_model.startswith("google-") else ai_model
        model = genai.GenerativeModel(model_name)
        
        # プロンプトを構築（OpenAIと同様）
        comment_texts = []
        for comment in comments:
            timestamp = format_unix_time(comment.get('date', ''))
            broadcast_seconds = comment.get('broadcast_seconds', 0)
            time_str = format_seconds_to_time(broadcast_seconds)
            text = comment.get('text', '')
            comment_texts.append(f"[{timestamp} - 放送内時間:{time_str}] {text}")
        
        analysis_prompt = user_detail_config.get("analysis_prompt", "")
        user_data_text = "\n".join(comment_texts)
        
        full_prompt = f"""
{analysis_prompt}

ユーザーID: {user_detail_config['user_id']}
表示名: {user_detail_config.get('display_name', 'なし')}
総コメント数: {len(comments)}件

コメント履歴:
{user_data_text}

上記のデータを基に、このユーザーの詳細な分析を日本語で行ってください。
分析結果はHTML形式で出力し、<br>タグで改行してください。
"""

        response = model.generate_content(full_prompt)
        
        metadata = f"""
<div style="background-color: #f0f8ff; padding: 10px; margin: 10px 0; border-left: 4px solid #0066cc;">
<strong>AI分析情報</strong><br>
分析モデル: google-gemini-2.5-flash<br>
分析日時: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}<br>
分析対象: {len(comments)}件のコメント
</div>
"""
        
        return metadata + response.text
        
    except Exception as e:
        print(f"Gemini分析エラー: {str(e)}")
        return f"Gemini分析中にエラーが発生しました: {str(e)}"

def generate_analysis_text(comments):
    """簡単な分析テキストを生成"""
    if not comments:
        return "コメントがありません。"
    
    total_comments = len(comments)
    total_chars = sum(len(comment.get('text', '')) for comment in comments)
    avg_chars = total_chars / total_comments if total_comments > 0 else 0
    
    analysis = f"""
        - 総コメント数: {total_comments}件<br><br>
        - 平均文字数: {avg_chars:.1f}文字<br><br>
    """
    
    return analysis

def update_user_list_page(user_data, broadcast_data, template_dir, output_dir, lv_value):
    """一覧ページを生成または更新（複数放送対応）"""
    template_path = os.path.join(template_dir, 'user_list.html')
    list_file_path = os.path.join(output_dir, f"{user_data['user_id']}_list.html")
    
    # 既存の一覧ページがある場合は読み込み
    existing_items = []
    if os.path.exists(list_file_path):
        existing_items = load_existing_broadcast_items(list_file_path)
    
    # 新しい放送アイテムを追加
    new_item = generate_broadcast_items(user_data, broadcast_data, lv_value)
    existing_items.append(new_item)
    
    # テンプレートを読み込み
    if not os.path.exists(template_path):
        print(f"テンプレートファイルが見つかりません: {template_path}")
        return
    
    with open(template_path, 'r', encoding='utf-8') as f:
        template = f.read()
    
    # 全ての放送アイテムを結合
    all_items = '\n'.join(existing_items)

    # テンプレート変数を置換
    html_content = template.replace('{{broadcaster_name}}', user_data['user_name'])
    html_content = html_content.replace('{{thumbnail_url}}', get_user_icon_path(user_data['user_id']))
    html_content = html_content.replace('{{broadcast_items}}', all_items)
    
    # ファイル保存
    with open(list_file_path, 'w', encoding='utf-8') as f:
        f.write(html_content)
    
    print(f"一覧ページ更新: {list_file_path}")

def generate_broadcast_items(user_data, broadcast_data, lv_value):
    """放送アイテムリストを生成（broadcast_secondsを使用）"""
    if not user_data['comments']:
        return "<p>コメントがありません</p>"
    
    first_comment = user_data['comments'][0].get('text', '') if user_data['comments'] else ''
    last_comment = user_data['comments'][-1].get('text', '') if user_data['comments'] else ''
    
    item = f'''
        <div class="link-item">
            <p class="separator">―――――――――――――――――――――――――――――――――――――――――――</p>
            <p class="start-time">開始時間: {format_start_time(broadcast_data.get('start_time', ''))}</p>
            <div class="comment-preview">
                <p>初コメ: {escape_html(first_comment)}</p>
                <p>最終コメ: {escape_html(last_comment)}</p>
            </div>
            
            <button onclick="toggleDiv('chat-data-{lv_value}')" class="toggle-button">
                コメントを表示:非表示
            </button>
            
            <div class="chat-data" id="chat-data-{lv_value}" style="display: none">
                <table border="1">
                    <thead>
                        <tr>
                            <th>コメント番号</th>
                            <th>放送内時間</th>
                            <th>日時</th>
                            <th>コメント内容</th>
                        </tr>
                    </thead>
                    <tbody>
                        {generate_comment_rows(user_data['comments'])}
                    </tbody>
                </table>
            </div>
            
            <div class="broadcast-link">
                <a href="{user_data['user_id']}_{lv_value}_detail.html">{broadcast_data.get('live_title', 'タイトル不明')}: における{user_data['user_name']}のコメント分析</a>
            </div>
        </div>
    '''
    
    return item

def load_existing_broadcast_items(list_file_path):
    """既存の一覧ページから放送アイテムを抽出"""
    try:
        with open(list_file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # 簡単な方法：既存のlink-itemを抽出
        import re
        pattern = r'<div class="link-item">.*?</div>'
        matches = re.findall(pattern, content, re.DOTALL)
        return matches
        
    except Exception as e:
        print(f"既存アイテム読み込みエラー: {str(e)}")
        return []

def copy_static_files(template_dir, output_dir):
    """CSS/JSファイルを出力ディレクトリにコピー"""
    try:
        # cssディレクトリをコピー
        css_src = os.path.join(template_dir, 'css')
        css_dst = os.path.join(output_dir, 'css')
        if os.path.exists(css_src):
            shutil.copytree(css_src, css_dst, dirs_exist_ok=True)
        
        # jsディレクトリをコピー
        js_src = os.path.join(template_dir, 'js')
        js_dst = os.path.join(output_dir, 'js')
        if os.path.exists(js_src):
            shutil.copytree(js_src, js_dst, dirs_exist_ok=True)
        
        # assetsディレクトリをコピー
        assets_src = os.path.join(template_dir, 'assets')
        assets_dst = os.path.join(output_dir, 'assets')
        if os.path.exists(assets_src):
            shutil.copytree(assets_src, assets_dst, dirs_exist_ok=True)
            
    except Exception as e:
        print(f"静的ファイルコピーエラー: {str(e)}")

def get_user_icon_path(user_id):
    """ニコニコ動画のユーザーアイコンパスを生成"""
    if len(user_id) <= 4:
        return f"https://secure-dcdn.cdn.nimg.jp/nicoaccount/usericon/{user_id}.jpg"
    else:
        path_prefix = user_id[:-4]
        return f"https://secure-dcdn.cdn.nimg.jp/nicoaccount/usericon/{path_prefix}/{user_id}.jpg"

def format_unix_time(unix_time_str):
    """UNIX時間を日時表記に変換"""
    try:
        unix_time = int(unix_time_str)
        dt = datetime.fromtimestamp(unix_time)
        return dt.strftime('%Y-%m-%d %H:%M:%S')
    except:
        return unix_time_str

def format_start_time(start_time_str):
    """開始時間をフォーマット"""
    try:
        unix_time = int(start_time_str)
        dt = datetime.fromtimestamp(unix_time)
        return dt.strftime('%Y/%m/%d(%a) %H:%M')
    except:
        return start_time_str

def escape_html(text):
    """HTMLエスケープ"""
    if not text:
        return ""
    return (text.replace('&', '&amp;')
                .replace('<', '&lt;')
                .replace('>', '&gt;')
                .replace('"', '&quot;')
                .replace("'", '&#x27;'))

