import os
import json
from datetime import datetime
import subprocess
import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from archive_db import load_broadcast_data as load_broadcast_data_from_db
from utils import find_account_directory
from legacy_archiver.processors.step01_data_collector import find_video_files
import math

def process(pipeline_data):
    """Step09: スクリーンショット生成"""
    try:
        lv_value = pipeline_data['lv_value']
        config = pipeline_data['config']
        
        print(f"Step09 開始: {lv_value}")
        
        # 1. サムネイル生成機能が有効か確認
        if not config["display_features"].get("enable_thumbnails", True):
            print("サムネイル生成機能が無効です。処理をスキップします。")
            return {"screenshot_generated": False, "reason": "feature_disabled"}
        
        # 2. アカウントディレクトリ検索
        account_dir = find_account_directory(pipeline_data['platform_directory'], pipeline_data['account_id'])
        broadcast_dir = os.path.join(account_dir, lv_value)
        
        # 3. 統合JSONから動画時間とtime_diff_seconds取得
        broadcast_data = load_broadcast_data(broadcast_dir, lv_value)
        video_duration = broadcast_data.get('video_duration', 0.0)
        time_diff_seconds = broadcast_data.get('time_diff_seconds', 0)
        
        # 5. スクリーンショットディレクトリ作成
        screenshot_dir = os.path.join(broadcast_dir, "screenshot", lv_value)
        os.makedirs(screenshot_dir, exist_ok=True)
        
        # 6. スクリーンショット生成
        display_features = config.get("display_features", {})
        thumbnail_width = max(1, int(display_features.get("thumbnail_width", 80) or 80))
        thumbnail_height = max(1, int(display_features.get("thumbnail_height", 60) or 60))
        print(f"サムネイルサイズ: {thumbnail_width}x{thumbnail_height}")
        timeline_plan = pipeline_data.get("recording_segment_timeline") or {}
        if timeline_plan.get("segments"):
            video_duration = float(timeline_plan.get("total_duration_seconds") or video_duration or 0.0)
            screenshot_count = generate_segment_screenshots(
                timeline_plan,
                screenshot_dir,
                video_duration,
                thumbnail_width,
                thumbnail_height,
            )
        else:
            mp4_path = find_mp4_file(pipeline_data['platform_directory'], pipeline_data['account_id'], lv_value)
            if not mp4_path:
                raise Exception(f"MP4ファイルが見つかりません: {lv_value}")
            screenshot_count = generate_screenshots(
                mp4_path,
                screenshot_dir,
                video_duration,
                time_diff_seconds,
                thumbnail_width,
                thumbnail_height,
            )
        
        print(f"Step09 完了: {lv_value} - スクリーンショット生成数: {screenshot_count}")
        return {
            "screenshot_generated": True, 
            "screenshot_count": screenshot_count, 
            "screenshot_dir": screenshot_dir
        }
        
    except Exception as e:
        print(f"Step09 エラー: {str(e)}")
        raise

def find_mp4_file(platform_directory, account_id, lv_value):
    """MP4ファイルを検索"""
    for mp4_path in find_video_files(platform_directory, account_id, lv_value):
        if mp4_path.lower().endswith('.mp4'):
            print(f"MP4ファイル発見: {mp4_path}")
            return mp4_path
    
    return None

def load_broadcast_data(broadcast_dir, lv_value):
    """放送データをDBから読み込み"""
    data = load_broadcast_data_from_db(lv_value)
    if data:
        return data
    raise Exception(f"放送データDBが見つかりません: {lv_value}")

def generate_screenshots(mp4_path, screenshot_dir, video_duration, time_diff_seconds, thumbnail_width=80, thumbnail_height=60):
    """録画ファイルから10秒刻みでスクリーンショット生成"""
    try:
        screenshot_count = 0
        
        for recording_seconds in range(0, int(video_duration) + 1, 10):
            if recording_seconds > video_duration:
                break
                
            # 配信時間を計算
            broadcast_seconds = recording_seconds + time_diff_seconds
            
            # タイムブロック位置を計算（10の倍数に切り上げ）
            timeline_position = math.ceil(broadcast_seconds / 10.0) * 10
            
            output_path = os.path.join(screenshot_dir, f"{recording_seconds}.jpg")
            
            # ffmpegでスクリーンショット生成（設定サイズにリサイズ）
            cmd = [
                'ffmpeg', '-y',
                '-ss', str(recording_seconds),
                '-i', mp4_path,
                '-vframes', '1',
                '-vf', f'scale={int(thumbnail_width)}:{int(thumbnail_height)}',
                '-q:v', '5',        # JPEG品質（1-31、低い数字=高品質）
                '-f', 'image2',     # または '-f', 'mjpeg'
                output_path
            ]
            
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='ignore', timeout=30)
                if result.returncode == 0:
                    screenshot_count += 1
                    print(f"録画{recording_seconds}秒 → 配信{broadcast_seconds}秒 → タイムブロック{timeline_position}秒 → {output_path}")
                else:
                    print(f"スクリーンショット生成失敗 (録画{recording_seconds}秒): {result.stderr}")
                    
            except subprocess.TimeoutExpired:
                print(f"スクリーンショット生成タイムアウト: 録画{recording_seconds}秒")
            except Exception as e:
                print(f"スクリーンショット生成エラー (録画{recording_seconds}秒): {str(e)}")
        
        return screenshot_count
        
    except Exception as e:
        print(f"スクリーンショット生成処理エラー: {str(e)}")
        return 0


def select_segment_for_broadcast_second(timeline_plan, broadcast_seconds):
    value = max(0.0, float(broadcast_seconds))
    segments = list(timeline_plan.get("segments") or [])
    for index, segment in enumerate(segments):
        start = float(segment.get("timeline_start_seconds") or 0.0)
        end = float(segment.get("timeline_end_seconds") or start)
        if start <= value < end or (index == len(segments) - 1 and abs(value - end) < 0.001):
            duration = max(0.0, end - start)
            max_local = max(0.0, duration - 0.001) if duration else 0.0
            return segment, max(0.0, min(value - start, max_local))
    return None, None


def generate_segment_screenshots(
    timeline_plan,
    screenshot_dir,
    video_duration,
    thumbnail_width=80,
    thumbnail_height=60,
):
    """全体時刻を該当録画区間と区間内時刻へ変換して画像を取る。"""
    os.makedirs(screenshot_dir, exist_ok=True)
    timeline_seconds = list(range(0, int(float(video_duration)) + 1, 10))
    expected_names = {f"{second}.jpg" for second in timeline_seconds}
    # A rerun with a corrected/shorter timeline must never leave thumbnails
    # from the former axis (for example 53:40) in the generated directory.
    for filename in os.listdir(screenshot_dir):
        stem, extension = os.path.splitext(filename)
        if extension.lower() == '.jpg' and stem.isdigit() and filename not in expected_names:
            os.remove(os.path.join(screenshot_dir, filename))
            print(f"旧時間軸スクリーンショット削除: {filename}")
    screenshot_count = 0
    for broadcast_seconds in timeline_seconds:
        output_path = os.path.join(screenshot_dir, f"{broadcast_seconds}.jpg")
        if os.path.exists(output_path):
            os.remove(output_path)
        segment, local_seconds = select_segment_for_broadcast_second(timeline_plan, broadcast_seconds)
        if not segment:
            if generate_gap_placeholder(
                output_path,
                thumbnail_width=thumbnail_width,
                thumbnail_height=thumbnail_height,
            ):
                screenshot_count += 1
                print(f"配信{broadcast_seconds}秒は録画gapのため欠落画像を生成 → {output_path}")
            else:
                print(f"配信{broadcast_seconds}秒は録画gap、欠落画像生成失敗")
            continue
        segment_path = str(segment.get("mp4_path") or segment.get("path") or "")
        if not segment_path or not os.path.exists(segment_path):
            print(f"配信{broadcast_seconds}秒の録画区間が見つかりません: {segment_path}")
            continue
        cmd = [
            'ffmpeg', '-y',
            '-ss', f"{float(local_seconds):.6f}",
            '-i', segment_path,
            '-vframes', '1',
            '-vf', f'scale={int(thumbnail_width)}:{int(thumbnail_height)}',
            '-q:v', '5',
            '-f', 'image2',
            output_path,
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='ignore', timeout=30)
            if result.returncode == 0:
                screenshot_count += 1
                print(f"配信{broadcast_seconds}秒 → 区間{segment.get('segment_index', 0)}の{float(local_seconds):.3f}秒 → {output_path}")
            else:
                print(f"スクリーンショット生成失敗 (配信{broadcast_seconds}秒): {result.stderr}")
        except subprocess.TimeoutExpired:
            print(f"スクリーンショット生成タイムアウト: 配信{broadcast_seconds}秒")
        except Exception as exc:
            print(f"スクリーンショット生成エラー (配信{broadcast_seconds}秒): {exc}")
    return screenshot_count


def generate_gap_placeholder(output_path, *, thumbnail_width=80, thumbnail_height=60):
    """Create a deterministic dark thumbnail for a broadcast-clock media gap."""
    cmd = [
        'ffmpeg', '-y',
        '-f', 'lavfi',
        '-i', f'color=c=0x20242b:s={int(thumbnail_width)}x{int(thumbnail_height)}:d=0.04',
        '-vframes', '1',
        '-q:v', '5',
        '-f', 'image2',
        output_path,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='ignore', timeout=30)
        return result.returncode == 0 and os.path.exists(output_path)
    except Exception as exc:
        print(f"録画gap欠落画像生成エラー: {exc}")
        return False
