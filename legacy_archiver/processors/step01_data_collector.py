import os
import re
import json
import time
import html as html_lib
import xml.etree.ElementTree as ET
import requests
from datetime import datetime
import subprocess
import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from archive_db import save_broadcast_data
from utils import find_account_directory


def process(pipeline_data):
    """Step01: 基本情報抽出とJSON作成"""
    try:
        lv_value = pipeline_data['lv_value']
        account_id = pipeline_data['account_id']
        platform_directory = pipeline_data['platform_directory']
        
        print(f"Step01 開始: {lv_value}")
        
        # 1. ディレクトリ構造作成
        account_dir = find_account_directory(platform_directory, account_id)
        broadcast_dir = os.path.join(account_dir, lv_value)
        os.makedirs(broadcast_dir, exist_ok=True)

        # 2. 元URLのHTML取得・保存とbeginTime抽出
        html_content, begin_time = fetch_and_save_html(lv_value, broadcast_dir)
        
        # 3. 放送ページ/APIから旧NCV XML相当のメタ情報を取得
        ncv_xml_path = ""
        ncv_data = fetch_nicolive_program_metadata(lv_value, html_content)

        # 4. 動画ファイル名からserver_time取得  
        platform_xml_path, server_time = get_server_time_from_filename(platform_directory, account_id, lv_value)
                
        # 5. 動画時間情報取得
        video_duration = get_video_duration(pipeline_data)
        
        # 6. 前回放送の要約文取得
        previous_summary = get_previous_broadcast_summary(platform_directory, account_id, lv_value)
        
        # 7. 統合JSON作成（beginTimeを追加）
        broadcast_data = create_broadcast_json(
            lv_value, ncv_data, server_time, begin_time, video_duration, 
            previous_summary, broadcast_dir, ncv_xml_path, platform_xml_path, account_dir
        )
        
        print(f"Step01 完了: {lv_value}")
        return broadcast_data
        
    except Exception as e:
        print(f"Step01 エラー: {str(e)}")
        raise

def get_server_time_from_filename(platform_directory, account_id, lv_value):
    """動画ファイル名からserver_time取得"""
    try:
        for video_path in find_video_files(platform_directory, account_id, lv_value):
            pattern = r'^(\d+)_lv\d+_'
            match = re.search(pattern, os.path.basename(video_path))
            if match:
                server_time = match.group(1)
                print(f"動画ファイル名からserver_time取得: {server_time}")
                return "", server_time
        
        print_video_missing_error(platform_directory, account_id, lv_value)
        return "", ""
        
    except Exception as e:
        print(f"ファイル名からserver_time取得エラー: {str(e)}")
        return "", ""


def find_video_files(platform_directory, account_id, lv_value):
    """放送成果物ディレクトリ配下の動画ファイルを検索"""
    search_dirs = video_search_dirs(platform_directory, account_id, lv_value)
    extensions = ('.mp4', '.ts', '.mkv', '.webm', '.flv')
    found = []
    seen = set()
    for search_dir in search_dirs:
        if not os.path.isdir(search_dir):
            continue
        for file in os.listdir(search_dir):
            path = os.path.join(search_dir, file)
            if not os.path.isfile(path):
                continue
            if not file.lower().endswith(extensions):
                continue
            if lv_value not in file:
                continue
            normalized = os.path.abspath(path)
            if normalized not in seen:
                found.append(path)
                seen.add(normalized)
    found.sort(key=lambda path: os.path.getmtime(path))
    return found


def video_search_dirs(platform_directory, account_id, lv_value):
    account_dir = find_account_directory(platform_directory, account_id)
    broadcast_dir = os.path.join(account_dir, lv_value)
    return [
        broadcast_dir,
        os.path.join(broadcast_dir, "archive"),
    ]


def print_video_missing_error(platform_directory, account_id, lv_value):
    print(f"[ERROR] 対象の動画ファイルが見つかりません: {lv_value}")
    print(f"[ERROR] account_id: {account_id}")
    print(f"[ERROR] platform_directory: {platform_directory}")
    for search_dir in video_search_dirs(platform_directory, account_id, lv_value):
        print(f"[ERROR] 動画探索ディレクトリ: {search_dir} / exists={os.path.isdir(search_dir)}")


def fetch_and_save_html(lv_value, broadcast_dir):
    """元URLのHTML取得・保存とbeginTime抽出"""
    try:
        url = f"https://live.nicovideo.jp/watch/{lv_value}"
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        
        response = requests.get(url, headers=headers, timeout=30)
        response.raise_for_status()
        
        html_content = response.text
        
        # HTMLを保存
        html_path = os.path.join(broadcast_dir, f"{lv_value}.html")
        with open(html_path, 'w', encoding='utf-8') as f:
            f.write(html_content)
        
        # beginTimeを抽出
        begin_time = extract_begin_time(html_content)
        
        print(f"HTML保存完了: {html_path}")
        if begin_time:
            print(f"beginTime抽出: {begin_time}")
            
        return html_content, begin_time
        
    except Exception as e:
        print(f"HTML取得エラー: {str(e)}")
        return None, None

def extract_begin_time(html_content):
    """HTMLからbeginTimeを抽出"""
    try:
        import re
        # beginTime&quot;:数字 のパターンを検索
        pattern = r'beginTime&quot;:(\d+)'
        match = re.search(pattern, html_content)
        
        if match:
            return int(match.group(1))
        else:
            # 別のパターンも試す
            pattern2 = r'"beginTime":(\d+)'
            match2 = re.search(pattern2, html_content)
            if match2:
                return int(match2.group(1))
            
        return None
        
    except Exception as e:
        print(f"beginTime抽出エラー: {str(e)}")
        return None


def fetch_nicolive_program_metadata(lv_value, html_content=None):
    """放送ページとAPIから旧NCV XML相当のメタ情報を作る"""
    page_data = parse_embedded_data_from_html(html_content or "")
    api_data = {}
    provider_id = page_data.get("owner_id") or ""
    provider_type = page_data.get("provider_type") or "user"
    if provider_id:
        api_data = fetch_program_history_item(lv_value, provider_id, provider_type)

    merged = merge_program_metadata(lv_value, page_data, api_data)
    print(f"Step01 APIメタ取得: {merged}")
    return merged


def parse_embedded_data_from_html(html_content):
    """watchページのscript#embedded-dataから放送メタを取り出す"""
    if not html_content:
        return {}
    match = re.search(r'<script[^>]*id=["\']embedded-data["\'][^>]*data-props=["\']([^"\']*)["\']', html_content)
    if not match:
        return {}
    try:
        props = json.loads(html_lib.unescape(match.group(1)))
    except Exception as e:
        print(f"embedded-data解析エラー: {str(e)}")
        return {}
    program = props.get("program") or {}
    supplier = program.get("supplier") or {}
    statistics = program.get("statistics") or {}
    social_group = program.get("socialGroup") or {}
    begin_time = int_or_empty(program.get("beginTime"))
    end_time = int_or_empty(program.get("endTime") or program.get("scheduledEndTime"))
    open_time = int_or_empty(program.get("openTime") or begin_time)
    return {
        "live_num": str(program.get("nicoliveProgramId") or "").removeprefix("lv"),
        "elapsed_time": seconds_to_elapsed_text(diff_seconds(begin_time, end_time)),
        "live_title": str(program.get("title") or ""),
        "broadcaster": str(supplier.get("name") or ""),
        "default_community": str(social_group.get("id") or ""),
        "community_name": "",
        "open_time": str(open_time or ""),
        "start_time": str(begin_time or ""),
        "end_time": str(end_time or ""),
        "watch_count": str(statistics.get("watchCount") or ""),
        "comment_count": str(statistics.get("commentCount") or ""),
        "owner_id": str(supplier.get("programProviderId") or ""),
        "owner_name": str(supplier.get("name") or ""),
        "provider_type": normalize_provider_type(program.get("providerType") or program.get("visualProviderType") or ""),
    }


def fetch_program_history_item(lv_value, provider_id, provider_type="user"):
    """放送者履歴APIから対象LVのメタを取得する"""
    api_provider_type = normalize_history_provider_type(provider_type)
    params = {
        "providerId": provider_id,
        "providerType": api_provider_type,
        "isIncludeNonPublic": "false",
        "offset": 0,
        "limit": 20,
        "withTotalCount": "true",
    }
    headers = {
        "User-Agent": "Mozilla/5.0",
        "X-Frontend-Id": "9",
        "X-Frontend-Version": "0",
    }
    url = "https://live.nicovideo.jp/front/api/v2/user-broadcast-history"
    try:
        response = requests.get(url, params=params, headers=headers, timeout=20)
        response.raise_for_status()
        payload = response.json()
        programs = payload.get("data", {}).get("programsList", [])
        for item in programs:
            if str(item.get("id", {}).get("value") or "") == lv_value:
                return program_history_item_to_ncv_data(item)
        print(f"user-broadcast-historyに対象LVなし: {lv_value} provider={provider_id}/{api_provider_type}")
    except Exception as e:
        print(f"user-broadcast-history取得エラー: {str(e)}")
    return {}


def program_history_item_to_ncv_data(item):
    program = item.get("program") or {}
    schedule = program.get("schedule") or {}
    provider = item.get("programProvider") or {}
    social_group = item.get("socialGroup") or {}
    statistics = item.get("statistics") or {}
    begin_time = seconds_value(schedule.get("beginTime"))
    end_time = seconds_value(schedule.get("endTime") or schedule.get("scheduledEndTime"))
    open_time = seconds_value(schedule.get("openTime") or schedule.get("beginTime"))
    provider_id = provider.get("programProviderId")
    if isinstance(provider_id, dict):
        provider_id = provider_id.get("value")
    return {
        "live_num": str(item.get("id", {}).get("value") or "").removeprefix("lv"),
        "elapsed_time": seconds_to_elapsed_text(diff_seconds(begin_time, end_time)),
        "live_title": str(program.get("title") or ""),
        "broadcaster": str(provider.get("name") or ""),
        "default_community": str(social_group.get("socialGroupId") or ""),
        "community_name": "",
        "open_time": str(open_time or ""),
        "start_time": str(begin_time or ""),
        "end_time": str(end_time or ""),
        "watch_count": str(value_field(statistics.get("viewers")) or ""),
        "comment_count": str(value_field(statistics.get("comments")) or ""),
        "owner_id": str(provider_id or ""),
        "owner_name": str(provider.get("name") or ""),
        "provider_type": normalize_provider_type(provider.get("type") or program.get("provider") or ""),
    }


def merge_program_metadata(lv_value, page_data, api_data):
    keys = [
        "live_num",
        "elapsed_time",
        "live_title",
        "broadcaster",
        "default_community",
        "community_name",
        "open_time",
        "start_time",
        "end_time",
        "watch_count",
        "comment_count",
        "owner_id",
        "owner_name",
    ]
    merged = {}
    for key in keys:
        merged[key] = str(api_data.get(key) or page_data.get(key) or "")
    if not merged["live_num"]:
        merged["live_num"] = str(lv_value).removeprefix("lv")
    if not merged["elapsed_time"]:
        merged["elapsed_time"] = seconds_to_elapsed_text(diff_seconds(merged.get("start_time"), merged.get("end_time")))
    return merged


def normalize_provider_type(value):
    value = str(value or "").strip().lower()
    if value in {"user", "community", "channel"}:
        return value
    if value == "official":
        return "channel"
    return "user"


def normalize_history_provider_type(value):
    value = normalize_provider_type(value)
    if value == "channel":
        return "channel"
    return "user"


def value_field(value):
    if isinstance(value, dict):
        return value.get("value")
    return value


def seconds_value(value):
    if isinstance(value, dict):
        return int_or_empty(value.get("seconds"))
    return int_or_empty(value)


def int_or_empty(value):
    try:
        if value is None or value == "":
            return ""
        return int(value)
    except (TypeError, ValueError):
        return ""


def diff_seconds(start, end):
    try:
        if start == "" or end == "":
            return 0
        return max(0, int(end) - int(start))
    except (TypeError, ValueError):
        return 0


def seconds_to_elapsed_text(seconds):
    try:
        seconds = int(seconds or 0)
    except (TypeError, ValueError):
        seconds = 0
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    rest = seconds % 60
    return f"{hours:02d}:{minutes:02d}:{rest:02d}"

def wait_and_parse_ncv_xml(ncv_directory, lv_value, account_id="", display_name=""):
    """NCVのXMLファイル監視・解析（新しいディレクトリ構造対応）"""
    
    print(f"DEBUGLOG: ncv_directory引数: {ncv_directory}")
    print(f"DEBUGLOG: account_id: {account_id}")
    print(f"DEBUGLOG: display_name: {display_name}")
    
    # 新しいディレクトリ構造を考慮
    if account_id:
        from utils import find_ncv_directory
        actual_ncv_dir = find_ncv_directory(ncv_directory, account_id, display_name)
        print(f"DEBUGLOG: find_ncv_directory結果: {actual_ncv_dir}")
    else:
        actual_ncv_dir = ncv_directory
        print(f"DEBUGLOG: actual_ncv_dir (フォールバック): {actual_ncv_dir}")
    
    print(f"DEBUGLOG: 実際の探索ディレクトリ: {actual_ncv_dir}")
    print(f"DEBUGLOG: ディレクトリ存在確認: {os.path.exists(actual_ncv_dir)}")
    
    if os.path.exists(actual_ncv_dir):
        files = os.listdir(actual_ncv_dir)
        print(f"DEBUGLOG: ディレクトリ内のファイル一覧: {files}")
        xml_files = [f for f in files if f.endswith('.xml')]
        print(f"DEBUGLOG: XMLファイル一覧: {xml_files}")
        lv_xml_files = [f for f in xml_files if lv_value in f]
        print(f"DEBUGLOG: lv値を含むXMLファイル: {lv_xml_files}")
    else:
        print(f"DEBUGLOG: 探索ディレクトリが存在しません: {actual_ncv_dir}")
    
    for i in range(60):
        try:
            xml_file = find_xml_file_containing_lv(actual_ncv_dir, lv_value)
            if xml_file:
                print(f"DEBUGLOG: XMLファイル発見: {xml_file}")
                ncv_data = parse_ncv_xml(xml_file)
                return xml_file, ncv_data
            else:
                print(f"DEBUGLOG: XMLファイル未発見 (試行{i+1}/60)")
        except Exception as e:
            print(f"XML解析エラー(試行{i+1}): {str(e)}")
        time.sleep(1)
    
    raise Exception(f"NCVのXMLファイルが見つかりません: {lv_value}")

def get_server_time_from_xml(platform_directory, lv_value, account_id):
    """監視ディレクトリのXMLからserver_time取得（ファイル名部分一致対応）"""
    try:
        # アカウントディレクトリ内を検索
        account_dir = find_account_directory(platform_directory, account_id)
        xml_file = find_xml_file_containing_lv(account_dir, lv_value)
        
        if xml_file:
            tree = ET.parse(xml_file)
            root = tree.getroot()
            
            # <thread>要素からserver_timeを取得
            thread_elem = root.find('.//thread')
            if thread_elem is not None:
                server_time = thread_elem.get('server_time', '')
                print(f"server_time取得成功: {server_time}")
                return xml_file, server_time
            else:
                print("thread要素が見つかりません")
        else:
            print(f"XMLファイルが見つかりません: {account_dir}")
        
        return "", ""
        
    except Exception as e:
        print(f"server_time取得エラー: {str(e)}")
        return "", ""

def find_xml_file_containing_lv(directory, lv_value):
    """指定ディレクトリでlv_valueを含むXMLファイルを検索"""
    try:
        if not os.path.exists(directory):
            return None
            
        for filename in os.listdir(directory):
            if filename.endswith('.xml') and lv_value in filename:
                xml_path = os.path.join(directory, filename)
                print(f"XMLファイル発見: {xml_path}")
                return xml_path
        
        return None
        
    except Exception as e:
        print(f"XMLファイル検索エラー: {str(e)}")
        return None

def parse_ncv_xml(xml_path):
    """NCVのXMLファイル解析"""
    try:
        print(f"DEBUG: XML解析開始: {xml_path}")
        tree = ET.parse(xml_path)
        root = tree.getroot()
        print(f"DEBUG: XMLルート要素: {root.tag}")
        
        # 名前空間を正しく定義
        ns = {'': 'http://posite-c.jp/niconamacommentviewer/commentlog/'}
        
        # LiveInfoとPlayerStatus要素を取得（名前空間付きで検索）
        live_info = root.find('.//LiveInfo', ns)
        print(f"DEBUG: LiveInfo要素: {live_info}")
        
        if live_info is not None:
            print(f"DEBUG: LiveInfo子要素:")
            for child in live_info:
                print(f"  {child.tag}: {child.text}")
        
        player_status = root.find('.//PlayerStatus', ns)
        print(f"DEBUG: PlayerStatus要素: {player_status}")
        
        stream = None
        if player_status is not None:
            stream = player_status.find('.//Stream', ns)
            print(f"DEBUG: Stream要素: {stream}")
            if stream is not None:
                for child in stream:
                    print(f"  Stream/{child.tag}: {child.text}")
        
        # 各データを個別に取得してログ出力（名前空間付きで）
        start_time = get_text_content_with_ns(live_info, './/StartTime', ns)
        print(f"DEBUG: start_time取得結果: '{start_time}'")
        
        live_title = get_text_content_with_ns(live_info, './/LiveTitle', ns)
        print(f"DEBUG: live_title取得結果: '{live_title}'")
        
        data = {
            'live_num': get_text_content_with_ns(root, './/LiveNum', ns),
            'elapsed_time': get_text_content_with_ns(root, './/ElapsedTime', ns),
            'live_title': live_title,
            'broadcaster': get_text_content_with_ns(live_info, './/Broadcaster', ns),
            'default_community': get_text_content_with_ns(live_info, './/DefaultCommunity', ns),
            'community_name': '',
            'open_time': get_text_content_with_ns(live_info, './/OpenTime', ns),
            'start_time': start_time,
            'end_time': get_text_content_with_ns(live_info, './/EndTime', ns),
            'watch_count': get_text_content_with_ns(stream, './/WatchCount', ns) if stream is not None else '',
            'comment_count': get_text_content_with_ns(stream, './/CommentCount', ns) if stream is not None else '',
            'owner_id': get_text_content_with_ns(stream, './/OwnerId', ns) if stream is not None else '',
            'owner_name': get_text_content_with_ns(stream, './/OwnerName', ns) if stream is not None else ''
        }
        
        print(f"DEBUG: 解析結果データ: {data}")
        return data
        
    except Exception as e:
        print(f"NCVのXML解析エラー: {str(e)}")
        import traceback
        print(f"DEBUG: エラートレースバック: {traceback.format_exc()}")
        raise

def get_text_content_with_ns(element, xpath, ns):
    """名前空間対応版のテキスト取得"""
    if element is None:
        return ""
    found = element.find(xpath, ns)
    return found.text if found is not None and found.text else ""

def get_text_content(element, xpath, ns=None):
    """XMLから安全にテキスト取得"""
    if element is None:
        return ""
    if ns:
        found = element.find(xpath, ns)
    else:
        found = element.find(xpath)
    if found is None:
        # 名前空間なしでも試行
        found = element.find(xpath)
    return found.text if found is not None and found.text else ""

def get_text_content(element, xpath):
    """XMLから安全にテキスト取得"""
    if element is None:
        return ""
    found = element.find(xpath)
    return found.text if found is not None and found.text else ""

def get_video_duration(pipeline_data):
    """動画時間情報取得"""
    try:
        platform_directory = pipeline_data['platform_directory']
        account_id = pipeline_data['account_id']
        lv_value = pipeline_data['lv_value']
        
        mp4_files = find_video_files(platform_directory, account_id, lv_value)
        
        if mp4_files:
            # ffprobeで動画時間取得
            cmd = [
                'ffprobe', '-v', 'quiet', '-show_entries', 
                'format=duration', '-of', 'csv=p=0', mp4_files[0]
            ]
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode == 0:
                return float(result.stdout.strip())
        
        print_video_missing_error(platform_directory, account_id, lv_value)
        return 0.0
        
    except Exception as e:
        print(f"動画時間取得エラー: {str(e)}")
        return 0.0
    

def get_previous_broadcast_summary(platform_directory, account_id, current_lv_value):
    """前回放送の要約文取得"""
    try:
        account_dir = find_account_directory(platform_directory, account_id)
        
        # 現在のlv値から数値部分を抽出
        current_num = int(re.search(r'lv(\d+)', current_lv_value).group(1))
        
        # 一つ前の放送を探す
        for i in range(1, 100):
            prev_lv = f"lv{current_num - i}"
            prev_dir = os.path.join(account_dir, prev_lv)
            
            if os.path.exists(prev_dir):
                # JSONファイルを探す
                for file in os.listdir(prev_dir):
                    if file.endswith('.json'):
                        json_path = os.path.join(prev_dir, file)
                        with open(json_path, 'r', encoding='utf-8') as f:
                            data = json.load(f)
                            summary = data.get('summary_text', '')
                            if summary:
                                print(f"前回放送の要約取得: {prev_lv}")
                                return summary
        
        return ""
        
    except Exception as e:
        print(f"前回放送要約取得エラー: {str(e)}")
        return ""

def create_broadcast_json(lv_value, ncv_data, server_time, begin_time, video_duration, previous_summary, broadcast_dir, ncv_xml_path, platform_xml_path, account_dir_path):
    """統合JSON作成"""
    
    # open_timeとserver_timeの差を計算
    time_diff_seconds = calculate_time_difference(ncv_data.get('open_time', ''), server_time)
   
    broadcast_data = {
        'lv_value': lv_value,
        'timestamp': datetime.now().isoformat(),
        'server_time': server_time,
        'begin_time': begin_time,  # beginTimeを追加
        'video_duration': video_duration,
        'time_diff_seconds': time_diff_seconds,
        
        # ディレクトリパス
        'account_directory_path': account_dir_path,
        'broadcast_directory_path': broadcast_dir,
        
        # XMLファイルパス
        'ncv_xml_path': ncv_xml_path,
        'platform_xml_path': platform_xml_path,
        
        # NCVデータ
        'live_num': ncv_data.get('live_num', ''),
        'elapsed_time': ncv_data.get('elapsed_time', ''),
        'live_title': ncv_data.get('live_title', ''),
        'broadcaster': ncv_data.get('broadcaster', ''),
        'default_community': ncv_data.get('default_community', ''),
        'community_name': '',
        'open_time': ncv_data.get('open_time', ''),
        'start_time': ncv_data.get('start_time', ''),
        'end_time': ncv_data.get('end_time', ''),
        'watch_count': ncv_data.get('watch_count', ''),
        'comment_count': ncv_data.get('comment_count', ''),
        'owner_id': ncv_data.get('owner_id', ''),
        'owner_name': ncv_data.get('owner_name', ''),
        
        # 前回放送情報
        'previous_summary': previous_summary,
        
        # 後で追加される項目（空で初期化）
        'summary_text': '',
        'intro_chat': [],
        'outro_chat': []
    }
    
    save_broadcast_data(lv_value, broadcast_data)
    print(f"放送データDB保存完了: {lv_value}")
    return broadcast_data

def calculate_time_difference(open_time, server_time):
    """open_timeとserver_timeの差を秒で計算"""
    try:
        if not open_time or not server_time:
            return 0
        
        open_time_int = int(open_time)
        server_time_int = int(server_time)
        
        diff_seconds = server_time_int - open_time_int
        return diff_seconds
        
    except (ValueError, TypeError) as e:
        print(f"時間差計算エラー: {str(e)}")
        return 0
