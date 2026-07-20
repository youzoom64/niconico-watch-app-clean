import sys
import os

# 環境変数で出力バッファリングを無効化
os.environ['PYTHONUNBUFFERED'] = '1'

print(f"DEBUG: Pipeline実行Python: {sys.executable}")
print(f"DEBUG: Python PATH: {sys.path[:3]}")
try:
    import moviepy
    print("DEBUG: moviepy import成功")
    from moviepy.editor import VideoFileClip
    print("DEBUG: moviepy.editor import成功")
except ImportError as e:
    print(f"DEBUG: moviepy import失敗: {e}")

import json
import importlib
from datetime import datetime

def load_user_config(account_id):
    """ユーザー設定を読み込む"""
    config_path = f"config/users/{account_id}.json"
    if os.path.exists(config_path):
        with open(config_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    return None

def should_run_step(config, step_name):
    step_mapping = {
        'step02_audio_transcriber': config['ai_features']['enable_summary_text'],
        'step03_emotion_scorer': config['display_features']['enable_emotion_scores'],
        'step04_word_analyzer': config['display_features']['enable_word_ranking'],
        'step05_summarizer': config['ai_features']['enable_summary_text'],
        'step06_music_generator': config['ai_features']['enable_ai_music'],
        'step07_image_generator': config['ai_features']['enable_summary_image'],
        'step08_conversation_generator': True,
        'step09_screenshot_generator': config['display_features'].get('enable_thumbnails', True),
        'step10_comment_processor': True,
        'step11_special_user_html_generator': True,
        'step12_html_generator': True,
        'step13_index_generator': True
    }
    
    # デバッグ出力を追加
    if step_name == 'step11_06_special_user_html_generator':
        print(f"=== DEBUG: step11_06 判定 ===")
        print(f"step_mapping の値: {step_mapping.get(step_name)}")
        print(f"戻り値: {step_mapping.get(step_name, True)}")
        print("=== DEBUG END ===")
    
    return step_mapping.get(step_name, True)

def run_pipeline(platform, account_id, platform_directory, ncv_directory, lv_value, config_account_id):
    """パイプライン処理を実行"""
    try:
        # ユーザー設定を読み込み
        config = load_user_config(config_account_id)
        if not config:
            raise Exception(f"ユーザー設定が見つかりません: {config_account_id}")
        
        print(f"[{config_account_id}] パイプライン開始: {lv_value}")
        
        # 処理データを準備
        pipeline_data = {
            'platform': platform,
            'account_id': account_id,
            'platform_directory': platform_directory,
            'ncv_directory': ncv_directory,
            'lv_value': lv_value,
            'user_name': config_account_id,
            'config': config,
            'start_time': datetime.now(),
            'results': {}
        }
        
        # 各ステップを順次実行
# 各ステップを順次実行
        steps = [
            'step01_data_collector',
            'step02_audio_transcriber',
            'step03_emotion_scorer',
            'step04_word_analyzer',
            'step05_summarizer',
            'step06_music_generator',
            'step07_image_generator',
            'step08_conversation_generator',
            'step09_screenshot_generator',
            'step10_comment_processor',
            'step11_special_user_html_generator',
            'step12_html_generator',
            'step13_index_generator'
        ]
        
        for step_name in steps:
            if should_run_step(config, step_name):
                print(f"[{config_account_id}] 実行中: {step_name}")
                
                try:
                    # ステップモジュールを動的読み込み
                    module = importlib.import_module(f"processors.{step_name}")
                    
                    # process関数を実行
                    if hasattr(module, 'process'):
                        result = module.process(pipeline_data)
                        pipeline_data['results'][step_name] = result
                        print(f"[{config_account_id}] 完了: {step_name}")
                    else:
                        print(f"[{config_account_id}] スキップ: {step_name} (process関数なし)")
                        
                except ImportError as e:
                    print(f"[{config_account_id}] スキップ: {step_name} (モジュールなし)")
                    print(f"[{config_account_id}] ImportError詳細: {e}")
                except Exception as e:
                    print(f"[{config_account_id}] エラー: {step_name} - {str(e)}")
                    print(f"[{config_account_id}] Exception詳細: {type(e).__name__}: {e}")
                    import traceback
                    traceback.print_exc()
                    # エラーが発生してもパイプラインは継続
                    
            else:
                print(f"[{config_account_id}] スキップ: {step_name} (設定により無効)")
        
        print(f"[{config_account_id}] パイプライン完了: {lv_value}")
        return 0
        
    except Exception as e:
        print(f"[{config_account_id}] パイプライン失敗: {str(e)}", file=sys.stderr)
        return 1

def main():
    if len(sys.argv) != 6:
        return 1
    
    platform = sys.argv[1]
    account_id = sys.argv[2]
    platform_directory = sys.argv[3]
    ncv_directory = sys.argv[4]
    lv_value = sys.argv[5]
    
    return run_pipeline(platform, account_id, platform_directory, ncv_directory, lv_value, account_id)

if __name__ == "__main__":
    sys.exit(main())