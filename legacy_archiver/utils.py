import requests
from bs4 import BeautifulSoup
import json
import os
import pickle
from datetime import datetime, timedelta

def fetch_nico_user_name(user_id: str):
    """ニコニコ動画からユーザー名を取得"""
    url = f"https://www.nicovideo.jp/user/{user_id}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
    }
    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.encoding = "utf-8"
        soup = BeautifulSoup(response.text, "lxml")

        # 1. metaタグから取得
        meta_tag = soup.find("meta", {"property": "profile:username"})
        if meta_tag and meta_tag.get("content"):
            return meta_tag["content"]

        # 2. JSON-LDから取得
        json_ld = soup.find("script", type="application/ld+json")
        if json_ld:
            try:
                data = json.loads(json_ld.string)
                if isinstance(data, dict) and "name" in data:
                    return data["name"]
            except json.JSONDecodeError:
                pass

        # 3. クラス名から取得
        nickname_element = soup.find(class_="UserDetailsHeader-nickname")
        if nickname_element:
            return nickname_element.get_text(strip=True)

        return None

    except requests.RequestException as e:
        print(f"HTTPエラー: {e}")
        return None

def get_user_nickname_with_cache(user_id: str, cache_days=7):
    """キャッシュ機能付きでユーザーニックネームを取得"""
    cache_file = f"cache/user_nickname_{user_id}.pkl"
    
    # キャッシュディレクトリを作成
    os.makedirs("cache", exist_ok=True)
    
    # キャッシュが存在し、有効期限内かチェック
    if os.path.exists(cache_file):
        try:
            with open(cache_file, 'rb') as f:
                cached_data = pickle.load(f)
                cached_time = cached_data.get('timestamp')
                cached_nickname = cached_data.get('nickname')
                
                if cached_time and cached_nickname:
                    # 有効期限をチェック
                    if datetime.now() - cached_time < timedelta(days=cache_days):
                        print(f"キャッシュからニックネーム取得: {user_id} -> {cached_nickname}")
                        return cached_nickname
        except (pickle.PickleError, KeyError):
            # キャッシュファイルが破損している場合は削除
            os.remove(cache_file)
    
    # キャッシュが無効または存在しない場合、新しく取得
    print(f"APIからニックネーム取得: {user_id}")
    nickname = fetch_nico_user_name(user_id)
    
    if nickname:
        # 成功した場合はキャッシュに保存
        cache_data = {
            'nickname': nickname,
            'timestamp': datetime.now()
        }
        try:
            with open(cache_file, 'wb') as f:
                pickle.dump(cache_data, f)
            print(f"ニックネームをキャッシュに保存: {user_id} -> {nickname}")
        except Exception as e:
            print(f"キャッシュ保存エラー: {e}")
    
    return nickname

def sanitize_path_component(name: str) -> str:
    """パス用のサニタイズ"""
    invalid = '<>:"/\\|?*\t\r\n'
    table = str.maketrans({ch: "_" for ch in invalid})
    out = name.translate(table).strip().rstrip(".")
    return out or "unknown"

def find_account_directory(platform_directory, account_id, display_name=None):
    """
    アカウントディレクトリを特定する
    形式: {platform_directory}/{account_id}_{display_name}/
    """
    import os
    
    if not platform_directory:
        platform_directory = os.path.abspath(os.path.join(".", "rec"))
    
    if display_name:
        # display_nameがある場合
        safe_name = sanitize_path_component(display_name)
        account_dir = os.path.join(platform_directory, f"{account_id}_{safe_name}")
    else:
        # display_nameがない場合は既存ディレクトリを探す
        if os.path.exists(platform_directory):
            for item in os.listdir(platform_directory):
                item_path = os.path.join(platform_directory, item)
                if os.path.isdir(item_path) and item.startswith(f"{account_id}_"):
                    account_dir = item_path
                    break
            else:
                # 見つからない場合はaccount_idのみ
                account_dir = os.path.join(platform_directory, account_id)
        else:
            account_dir = os.path.join(platform_directory, account_id)

    # niconico-watch-appではアカウント配下の broadcast を放送成果物置き場にする。
    # 旧archiverの各stepは account_dir/lv_value を見に行くため、ここで互換変換する。
    broadcast_dir = os.path.join(account_dir, "broadcast")
    os.makedirs(broadcast_dir, exist_ok=True)
    account_dir = broadcast_dir
    
    os.makedirs(account_dir, exist_ok=True)
    return account_dir

def find_ncv_directory(ncv_directory, account_id, display_name=None):
    """
    NCVディレクトリを特定する
    """
    import os
    
    if display_name:
        safe_name = sanitize_path_component(display_name)
        target_dir = os.path.join(ncv_directory, f"{account_id}_{safe_name}")
        if os.path.exists(target_dir):
            return target_dir
    
    # フォールバック: account_idで始まるディレクトリを探す
    if os.path.exists(ncv_directory):
        for item in os.listdir(ncv_directory):
            item_path = os.path.join(ncv_directory, item)
            if os.path.isdir(item_path) and item.startswith(f"{account_id}_"):
                return item_path
    
    # デフォルト
    return ncv_directory
