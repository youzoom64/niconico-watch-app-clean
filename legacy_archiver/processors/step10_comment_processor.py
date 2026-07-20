import os
import json
import xml.etree.ElementTree as ET
import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from archive_db import load_broadcast_data as load_broadcast_data_from_db
from archive_db import load_comments_payload, load_ranking_payload
from utils import find_account_directory

def process(pipeline_data):
    """Step10: コメントデータ処理"""
    try:
        lv_value = pipeline_data['lv_value']
        
        print(f"Step10 開始: {lv_value}")
        
        # 1. アカウントディレクトリ検索
        account_dir = find_account_directory(pipeline_data['platform_directory'], pipeline_data['account_id'])
        broadcast_dir = os.path.join(account_dir, lv_value)
        
        # 2. 放送データをDBから読み込む。現行フローではコメントもDB側で用意する。
        broadcast_data = load_broadcast_data(broadcast_dir, lv_value)
        start_time = int(broadcast_data.get('start_time', 0) or 0)

        # 3. 現行フローではDBを正とする。
        comments_payload = load_comments_payload(lv_value)
        comments_data = comments_payload.get("comments", [])
        if not comments_data:
            print("DBコメントなし: 空コメントで続行")
        
        # 4. コメントランキングを生成
        ranking_payload = load_ranking_payload(lv_value, comments_payload)
        if not ranking_payload.get("ranking") and comments_data:
            ranking_payload = {
                "lv_value": lv_value,
                "total_users": 0,
                "ranking": generate_comment_ranking(comments_data),
                "source": comments_payload.get("source", "db"),
            }
            ranking_payload["total_users"] = len(ranking_payload["ranking"])

        pipeline_data["comments_data"] = comments_payload
        pipeline_data["comment_ranking_data"] = ranking_payload
        
        print(f"Step10 完了: {lv_value} - コメント数: {len(comments_data)}, ランキング: {len(ranking_payload.get('ranking', []))}")
        return {
            "comments_count": len(comments_data),
            "ranking_count": len(ranking_payload.get("ranking", [])),
            "comments_source": comments_payload.get("source", "db"),
            "ranking_source": ranking_payload.get("source", "db"),
        }
        
    except Exception as e:
        print(f"Step10 エラー: {str(e)}")
        raise

def load_broadcast_data(broadcast_dir, lv_value):
    """放送データをDBから読み込み"""
    data = load_broadcast_data_from_db(lv_value)
    if data:
        return data
    raise Exception(f"放送データDBが見つかりません: {lv_value}")

def parse_comments_from_xml(xml_path, start_time):
    """NCVのXMLからコメントデータを解析"""
    try:
        tree = ET.parse(xml_path)
        root = tree.getroot()
        
        comments = []
        
        # chat要素を検索（名前空間対応）
        chat_elements = root.findall('.//chat')
        if not chat_elements:
            # 名前空間がある場合
            namespaces = {'ncv': 'http://posite-c.jp/niconamacommentviewer/commentlog/'}
            chat_elements = root.findall('.//ncv:chat', namespaces)
        
        print(f"XMLから{len(chat_elements)}個のコメントを検出")
        
        for chat in chat_elements:
            try:
                comment_date = int(chat.get('date', 0))
                if comment_date == 0:
                    continue
                
                # 配信開始からの秒数を計算
                broadcast_seconds = comment_date - start_time
                
                # 負の値（配信開始前）はスキップ
                if broadcast_seconds < 0:
                    continue
                
                # タイムブロック計算（10秒刻み）
                timeline_block = (broadcast_seconds // 10) * 10
                
                # コメントデータを構築
                comment_data = {
                    "no": int(chat.get('no', 0)),
                    "user_id": chat.get('user_id', ''),
                    "user_name": chat.get('name', ''),
                    "text": chat.text or '',
                    "date": comment_date,
                    "broadcast_seconds": broadcast_seconds,
                    "timeline_block": timeline_block,
                    "premium": int(chat.get('premium', 0)),
                    "anonymity": 'anonymity' in chat.attrib
                }
                
                comments.append(comment_data)
                
            except (ValueError, TypeError) as e:
                print(f"コメント解析エラー: {str(e)}")
                continue
        
        # 時系列順にソート
        comments.sort(key=lambda x: x['broadcast_seconds'])
        
        print(f"有効なコメント: {len(comments)}個")
        return comments
        
    except Exception as e:
        print(f"XML解析エラー: {str(e)}")
        raise

def generate_comment_ranking(comments_data):
    """コメントランキングを生成"""
    try:
        user_stats = {}
        
        # ユーザー別にコメントを集計
        for comment in comments_data:
            user_id = comment['user_id']
            
            if user_id not in user_stats:
                user_stats[user_id] = {
                    "user_id": user_id,
                    "user_name": comment['user_name'],
                    "comment_count": 0,
                    "first_comment": "",
                    "first_comment_time": 0,
                    "last_comment": "",
                    "last_comment_time": 0,
                    "premium": comment['premium'],
                    "anonymity": comment['anonymity']
                }
            
            user_stat = user_stats[user_id]
            user_stat["comment_count"] += 1
            
            # 初回コメント
            if user_stat["comment_count"] == 1:
                user_stat["first_comment"] = comment['text']
                user_stat["first_comment_time"] = comment['broadcast_seconds']
            
            # 最新コメント（常に更新）
            user_stat["last_comment"] = comment['text']
            user_stat["last_comment_time"] = comment['broadcast_seconds']
        
        # コメント数順にソート
        ranking = sorted(user_stats.values(), key=lambda x: x['comment_count'], reverse=True)
        
        # ランク付け
        for i, user in enumerate(ranking, 1):
            user["rank"] = i
        
        print(f"コメントランキング: {len(ranking)}ユーザー")
        return ranking
        
    except Exception as e:
        print(f"ランキング生成エラー: {str(e)}")
        raise

