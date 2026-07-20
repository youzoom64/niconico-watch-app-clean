import json
import os
import openai
try:
    import google.generativeai as genai
except ImportError:
    genai = None
from datetime import datetime
import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from codex_exec_runner import CodexExecConfig, run_codex_exec
from archive_db import load_transcript_payload, update_broadcast_data
from utils import find_account_directory

def process(pipeline_data):
    """Step05: AI要約生成"""
    try:
        lv_value = pipeline_data['lv_value']
        config = pipeline_data['config']
        
        print(f"Step05 開始: {lv_value}")
        
        # 1. アカウントディレクトリ検索
        account_dir = find_account_directory(pipeline_data['platform_directory'], pipeline_data['account_id'])
        broadcast_dir = os.path.join(account_dir, lv_value)
        
        # 2. transcript.jsonから本文抽出
        transcript_text = extract_transcript_text(broadcast_dir, lv_value)
        if not transcript_text.strip():
            print("文字起こしテキストが空です")
            return {"summary": ""}
        
        # 3. AI要約生成
        summary_engine = get_ai_task_engine(config, "summary")
        ai_model = model_for_engine(summary_engine, config["api_settings"]["summary_ai_model"])
        print(f"[INFO] 要約担当: {engine_label(summary_engine)} / model={ai_model}")
        summary = normalize_summary_text(generate_summary(transcript_text, config, ai_model))
        
        # 4. 統合JSONに要約を追加
        update_broadcast_json(broadcast_dir, lv_value, summary)
        
        # 5. 要約テキストファイル保存
        save_summary_text(broadcast_dir, lv_value, summary)
        
        print(f"Step05 完了: {lv_value} - 要約文字数: {len(summary)}")
        return {"summary": summary, "model_used": ai_model}
        
    except Exception as e:
        print(f"Step05 エラー: {str(e)}")
        raise

def extract_transcript_text(broadcast_dir, lv_value):
    """transcript.jsonから本文のみを抽出"""
    try:
        transcript_data = load_transcript_payload(lv_value)
        if not transcript_data.get("transcripts"):
            transcript_path = os.path.join(broadcast_dir, f"{lv_value}_transcript.json")
            if not os.path.exists(transcript_path):
                raise Exception(f"文字起こしDB/JSONが見つかりません: {lv_value}")
            with open(transcript_path, 'r', encoding='utf-8') as f:
                transcript_data = json.load(f)
        
        transcripts = transcript_data.get('transcripts', [])
        
        # 本文のみを抽出して結合
        text_segments = []
        for segment in transcripts:
            text = segment.get('text', '').strip()
            if text:
                text_segments.append(text)
        
        full_text = '\n'.join(text_segments)
        print(f"抽出したテキスト文字数: {len(full_text)}")
        
        return full_text
        
    except Exception as e:
        print(f"テキスト抽出エラー: {str(e)}")
        raise

def generate_summary(text, config, ai_model):
    """AIを使用して要約生成"""
    try:
        prompts = config.get("ai_prompts", {})
        summary_prompt = prompts["summary_prompt"]
        
        # テキストが長すぎる場合は分割処理
        max_chunk_size = int(prompts.get("summary_chunk_size") or 100000)
        
        if len(text) <= max_chunk_size:
            # 短いテキストはそのまま処理
            return generate_summary_single(text, summary_prompt, config, ai_model)
        else:
            # 長いテキストは分割して処理
            return generate_summary_chunked(text, summary_prompt, config, ai_model, max_chunk_size)
        
    except Exception as e:
        print(f"要約生成エラー: {str(e)}")
        raise


def normalize_summary_text(summary):
    """要約文の句点ごとに改行を入れて保存・表示しやすくする。"""
    text = str(summary or "").strip()
    if not text:
        return ""
    return text.replace("。\n", "。").replace("。", "。\n").rstrip()


def generate_summary_single(text, prompt, config, ai_model):
    """単一テキストの要約生成"""
    try:
        full_prompt = f"{prompt}\n\n{text}"
        print(f"[DEBUG] generate_summary_single: モデル={ai_model}, prompt文字数={len(full_prompt)}")
        
        if ai_model == "openai-gpt4o":
            print("[DEBUG] 要約生成ルート: openai-gpt4o互換（担当設定によりAI CLIまたはOpenAI API）")
            return call_openai_api(full_prompt, config)
        elif ai_model == "google-gemini-2.5-flash":
            print("[DEBUG] Google Gemini 2.5 Flash APIを呼び出します")
            return call_google_api(full_prompt, config)
        else:
            raise Exception(f"未対応のAIモデル: {ai_model}")
            
    except Exception as e:
        print(f"単一要約生成エラー: {str(e)}")
        raise


def generate_summary_chunked(text, prompt, config, ai_model, chunk_size):
    """分割テキストの要約生成"""
    try:
        prompts = config.get("ai_prompts", {})
        chunk_instruction = str(prompts.get("summary_chunk_prompt") or "以下は配信の一部です。この部分を要約してください：")
        final_instruction = str(
            prompts.get("summary_final_prompt")
            or "以下は配信の各部分の要約です。これらを統合して、配信全体の包括的な要約を作成してください："
        )
        chunks = split_text_smart(text, chunk_size)
        print(f"[DEBUG] テキストを{len(chunks)}個のチャンクに分割しました (モデル={ai_model})")
        
        chunk_summaries = []
        for i, chunk in enumerate(chunks):
            print(f"[DEBUG] チャンク {i+1}/{len(chunks)} 要約開始: 長さ={len(chunk)}")
            chunk_prompt = f"{prompt}\n\n{chunk_instruction}\n\n{chunk}"
            
            if ai_model == "openai-gpt4o":
                print("[DEBUG] 要約生成ルート: openai-gpt4o互換（担当設定によりAI CLIまたはOpenAI API）")
                summary = call_openai_api(chunk_prompt, config)
            elif ai_model == "google-gemini-2.5-flash":
                print("[DEBUG] Google Gemini 2.5 Flash APIを呼び出します")
                summary = call_google_api(chunk_prompt, config)
            else:
                raise Exception(f"未対応のAIモデル: {ai_model}")
            
            chunk_summaries.append(summary)
        
        print("[DEBUG] チャンク要約を統合して最終要約を生成します")
        combined_summaries = "\n\n".join(chunk_summaries)
        final_prompt = f"{final_instruction}\n\n{combined_summaries}"
        
        if ai_model == "openai-gpt4o":
            print("[DEBUG] 統合要約生成ルート: openai-gpt4o互換（担当設定によりAI CLIまたはOpenAI API）")
            final_summary = call_openai_api(final_prompt, config)
        elif ai_model == "google-gemini-2.5-flash":
            print("[DEBUG] Google Gemini 2.5 Flash APIを呼び出します（統合要約）")
            final_summary = call_google_api(final_prompt, config)
        else:
            raise Exception(f"未対応のAIモデル: {ai_model}")
        
        print("[DEBUG] チャンク要約統合完了")
        return final_summary
        
    except Exception as e:
        print(f"分割要約生成エラー: {str(e)}")
        raise


def split_text_smart(text, chunk_size):
    """テキストを適切に分割（文の境界を考慮）"""
    chunks = []
    current_chunk = ""
    
    # 改行で分割
    lines = text.split('\n')
    
    for line in lines:
        # 現在のチャンクに追加した場合のサイズをチェック
        potential_chunk = current_chunk + '\n' + line if current_chunk else line
        
        if len(potential_chunk) <= chunk_size:
            current_chunk = potential_chunk
        else:
            # チャンクサイズを超える場合
            if current_chunk:
                chunks.append(current_chunk)
                current_chunk = line
            else:
                # 単一行がチャンクサイズを超える場合、強制分割
                while len(line) > chunk_size:
                    chunks.append(line[:chunk_size])
                    line = line[chunk_size:]
                current_chunk = line
    
    # 最後のチャンクを追加
    if current_chunk:
        chunks.append(current_chunk)
    
    return chunks

def call_openai_api(prompt, config):
    """OpenAI GPT-4oを呼び出し"""
    try:
        codex_config = get_codex_exec_config(config)
        if codex_config.enabled:
            print(f"[INFO] 要約AI CLI呼び出し開始: provider={codex_config.provider} model={codex_config.model or '-'} prompt文字数={len(prompt)}")
            result = run_codex_exec(prompt, config=codex_config)
            if not result.ok:
                raise Exception(f"Codex exec failed: rc={result.returncode} stderr={result.stderr.strip()}")
            result_text = (result.text or result.stdout).strip()
            if not result_text:
                print("[WARN] AI CLIから空のレスポンスが返されました")
            else:
                print(f"[DEBUG] AI CLIレスポンス文字数: {len(result_text)}")
            return result_text

        api_key = config["api_settings"]["openai_api_key"]
        if not api_key:
            raise Exception("OpenAI API Keyが設定されていません")
        
        # デバッグログ
        print(f"[DEBUG] OpenAI API呼び出し開始: モデル=gpt-4o, prompt文字数={len(prompt)}")

        client = openai.OpenAI(api_key=api_key)
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=1000,
            temperature=0.7
        )

        # レスポンス情報をログ出力
        if hasattr(response, "usage"):
            print(f"[DEBUG] OpenAI使用トークン: prompt={response.usage.prompt_tokens}, completion={response.usage.completion_tokens}, total={response.usage.total_tokens}")
        else:
            print("[DEBUG] OpenAIトークン使用量情報なし")

        # 応答本文の取り出し
        result_text = response.choices[0].message.content.strip() if response.choices else ""
        if not result_text:
            print("[WARN] OpenAI APIから空のレスポンスが返されました")
        else:
            print(f"[DEBUG] OpenAIレスポンス文字数: {len(result_text)}")
        
        return result_text

    except Exception as e:
        print(f"OpenAI API呼び出しエラー: {str(e)}")
        raise


def get_codex_exec_config(config):
    raw = config.get("codex_exec", {})
    engine = get_ai_task_engine(config, "summary")
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


def call_google_api(prompt, config):
    """Google Gemini 2.5 Flashを呼び出し"""
    try:
        if genai is None:
            raise Exception("google-generativeai がインストールされていません")
        api_key = config["api_settings"]["google_api_key"]
        if not api_key:
            raise Exception("Google API Keyが設定されていません")
        
        # デバッグログ
        print(f"[DEBUG] Google API呼び出し開始: モデル=gemini-2.0-flash-exp, prompt文字数={len(prompt)}")

        genai.configure(api_key=api_key)
        model = genai.GenerativeModel('gemini-2.0-flash-exp')
        
        response = model.generate_content(
            prompt,
            generation_config=genai.types.GenerationConfig(
                max_output_tokens=1000,
                temperature=0.7
            )
        )

        # 追加デバッグログ
        if hasattr(response, "candidates"):
            print(f"[DEBUG] Google APIレスポンス候補数: {len(response.candidates)}")
        if hasattr(response, "usage_metadata"):
            print(f"[DEBUG] Google API token使用量: {response.usage_metadata}")

        # 安全に text を取り出す
        result_text = getattr(response, "text", "").strip()
        if not result_text:
            print("[WARN] Google APIから空のレスポンスが返されました")
        else:
            print(f"[DEBUG] Google APIレスポンス文字数: {len(result_text)}")
        
        return result_text

    except Exception as e:
        print(f"Google API呼び出しエラー: {str(e)}")
        raise


def update_broadcast_json(broadcast_dir, lv_value, summary):
    """統合JSONに要約を追加"""
    try:
        update_broadcast_data(
            lv_value,
            {
                "summary_text": summary,
                "summary_generated_at": datetime.now().isoformat(),
            },
        )
        print(f"放送データDBに要約を追加: {lv_value}")
            
    except Exception as e:
        print(f"統合JSON更新エラー: {str(e)}")

def save_summary_text(broadcast_dir, lv_value, summary):
    """要約テキストファイルを保存"""
    try:
        print("要約テキストファイル保存はDB化によりスキップ")
        
    except Exception as e:
        print(f"要約テキストファイル保存エラー: {str(e)}")
