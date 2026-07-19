# niconico-watch-app

ニコニコ生放送の監視、録画、コメント取得、文字起こし、要約、画像生成、HTMLアーカイブ作成、公開までをまとめて扱うWindows向けアプリです。通常の監視はメインGUI、終了済み放送やローカル動画の処理はタイムシフトGUIから実行します。

## 主な機能

- 監視配信者の放送開始検知
- `SlNicoLiveRec`を利用した録画と、録画停止時の自動再開
- 同じ`lv`で分割された録画ファイルの時間軸統合
- ニコ生コメントの取得・DB保存
- Faster-WhisperまたはWhisperXによる文字起こし
- WhisperX + pyannoteによる話者分離
- 初期プロンプトとHotwordsによる固有名詞の認識支援
- 要約、感情分析、単語分析、スクリーンショット、要約画像などの段階処理
- PC向け個別HTML、配信一覧、タグ別一覧の生成
- ロリポップなど外部公開先へのHTMLアップロード
- 保存済みローカル動画からの再処理

## 動作環境

- Windows 10/11
- Python 3.10または3.11
- FFmpeg / FFprobe
- ニコ生録画を行う場合は`SlNicoLiveRec`
- GPU文字起こしを行う場合は、対応するNVIDIA GPU・ドライバー・CUDA対応PyTorch
- 話者分離を行う場合はHugging Faceトークン

CPUでも文字起こしできますが、`large-v3`やWhisperXは非常に時間がかかります。

## インストール

```powershell
git clone https://github.com/youzoom64/niconico-watch-app-clean.git
cd niconico-watch-app-clean
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip
pip install -r requirements.txt
```

`scripts\setup_venv.cmd`も利用できますが、同ファイルの`PYTHON_BASE`は作成環境に合わせて変更してください。初期値は作者環境の絶対パスです。

FFmpegがPATHにない場合は、`ffmpeg.exe`と`ffprobe.exe`をPATHへ追加してください。

### CUDAを使用する場合

`pip install torch`だけではCPU版が入る場合があります。使用するCUDAに対応したPyTorchを[PyTorch公式手順](https://pytorch.org/get-started/locally/)で入れてから、次を確認してください。

```powershell
python -c "import torch; print(torch.cuda.is_available(), torch.version.cuda)"
```

`True`とCUDAバージョンが表示されればGPUを利用できます。

### Hugging Faceトークン

話者分離モデルを使う場合は、Hugging Faceでトークンを作成し、環境変数へ設定します。

```powershell
setx HF_TOKEN "hf_xxxxxxxxxxxxxxxxxxxx"
```

設定後はGUIを再起動してください。利用するpyannoteモデルによっては、Hugging Face上でモデルの利用条件への同意が必要です。

## 起動方法

### メインGUI

```bat
scripts\start_gui.cmd
```

または：

```powershell
.\.venv\Scripts\python.exe main.py gui
```

監視配信者の追加・編集、録画設定、文字起こし設定、生成Step、保存済み放送の確認などを行います。

### タイムシフトGUI

```bat
scripts\start_timeshift_gui.cmd
```

ローカル動画やタイムシフト対象を追加し、選択したStepだけを実行します。GUIを閉じると、そのGUIが開始したローカル処理も終了します。

### トラッカーのみ

```bat
scripts\start_tracker.cmd
```

1回だけ取得する場合：

```bat
scripts\run_once.cmd
```

### ローカル介入API

```bat
scripts\start_intervention_api.cmd
```

既定の待受先は`127.0.0.1:8794`です。

## 初回設定

通常はメインGUIから設定してください。設定は`config.json`、監視配信者ごとの設定や処理状態は`data\tracker.db`へ保存されます。どちらもGit管理対象外です。

最低限、次を設定します。

1. 保存先`target_root`
2. 監視する配信者ID
3. `SlNicoLiveRec.exe`のパス
4. 文字起こしエンジンとモデル
5. GPU利用時のデバイス・計算タイプ
6. 実行する生成Step
7. 必要なら公開先と認証情報

保存先は次の形式に統一されています。

```text
<target_root>\platform\niconico\<配信者ID>\broadcast\<lv番号>\
```

旧版で使われていた`bloadcast`は綴り間違いであり、現在の正しい保存先は`broadcast`です。

## 文字起こし

### Faster-Whisper

速度を優先する通常の文字起こし向けです。GPUでは`device=cuda`、`compute_type=float16`を推奨します。

### WhisperX

単語時刻合わせと話者分離が必要な場合に使用します。処理順は概ね次の通りです。

1. 音声読み込み
2. Whisper文字起こし
3. 単語時刻合わせ
4. pyannote話者分離
5. 発言セグメントへの話者割り当て

VRAM不足の場合は、まずバッチサイズを下げてください。ビームサイズもVRAMと処理時間を増やします。`large-v3`で不足する場合は、小さいモデルも検討してください。

### 初期プロンプトとHotwords

監視配信者編集画面では「認識支援文字」と「正式タグ」を設定できます。

```text
認識支援文字: 認識させたい表記
正式タグ:     公開時の正式表記
```

- 左列は文字起こし時のHotwordsへ渡されます。
- 右列はHTMLや一覧で使う正式タグです。
- 左右が違う場合は、認識結果を正式タグへ正規化します。

初期プロンプトが空欄の場合は、次が使われます。

```text
これはニコニコ生放送の録画音声です
```

固有名詞が多い場合は、Hotwordsが人物名であることを初期プロンプトへ書くと文脈を補助できます。

## アーカイブ処理Step

| Step | 処理 |
|---|---|
| 01 | 放送メタデータ収集 |
| 02 | 音声抽出・文字起こし |
| 03 | 感情分析 |
| 04 | 単語分析 |
| 05 | 要約生成 |
| 06 | 音楽生成 |
| 07 | 要約画像生成 |
| 08 | AI会話生成 |
| 09 | スクリーンショット生成 |
| 10 | コメント処理 |
| 11 | 特別ユーザーHTML生成 |
| 12 | 個別HTML生成 |
| 13 | タグ・一覧データ生成 |
| 14 | モダン一覧HTML生成 |
| 15 | 公開先へアップロード |

GUIのチェックを外したStepはスキップされます。既に音声・文字起こし・要約がある放送は、Step12以降だけを再実行してHTMLを作り直せます。

## タグと人物ジャンプ

- タグ候補の正本は監視配信者GUIの設定です。
- 放送別の個別タグは`index_person_tags.json`で管理されます。
- 人物タグを押すと、該当人物の発言時刻へ順番にジャンプします。
- 発言中の元の人物名表記は正式タグ名へ置換しません。
- `(現在回数/総数)`だけを追加し、ジャンプ位置として使用します。

PC版HTMLだけを生成・更新対象とし、スマホ専用`*_mobile.html`は新規生成しません。通常のHTML自体がレスポンシブ表示へ対応します。

## 生成物とデータ

```text
data\tracker.db
    放送、録画、コメント、文字起こし、処理状態

<target_root>\platform\niconico\<配信者ID>\broadcast\
    index.html
    index_person_tags.json
    index_person_aliases.json
    tags\tag_<タグ名>.html
    <lv番号>\
        個別HTML
        transcript JSON
        MP3
        スクリーンショット
        要約画像など
```

`data/`、`target/`、DB、ログ、認証設定は`.gitignore`で除外されています。

## ローカル動画の処理

タイムシフトGUIへMP4またはMKVを追加し、必要なStepを選んで処理します。同じ`lv`の録画が複数に分割されている場合は、録画区間の時刻情報を使って一つの放送として扱います。

動画が途中で切れて再録画された場合も、同じ`lv`の区間として登録されていれば時間軸へ統合されます。ファイル名だけで判断せず、DBの録画区間情報を利用します。

## 公開アップロード

Step15は完成したHTMLを設定済みターゲットへ送信します。認証情報はリポジトリへ保存しないでください。作者環境では外部のアップロードCLIと共通環境変数を使用しています。別環境ではGUI設定または`config.json`のアップロード設定を調整してください。

公開前に次を確認してください。

- APIキー、Cookie、Hugging FaceトークンがHTMLや設定へ含まれていない
- 配信者IDと公開先ディレクトリが一致している
- `index.html`と個別HTMLの相対リンクが正しい
- 公開対象に動画・音声を含めるか

## よくある問題

### `Torch not compiled with CUDA enabled`

CPU版PyTorchを使っています。CUDA対応版PyTorchを入れ直してください。

### `CUDA failed with error out of memory`

バッチサイズを下げます。それでも不足する場合はビームサイズ、モデルサイズ、同時に動いているGPUアプリを確認してください。

### Hugging Face関連の認証エラー

`HF_TOKEN`、モデル利用条件への同意、`huggingface_hub`と`pyannote.audio`の互換性を確認してください。

### Step12で動画が見つからない

DBの`broadcast_directory_path`と、実ファイルの保存先が一致しているか確認します。正しい形式は`<配信者ID>\broadcast\<lv番号>`です。

### GUIを閉じても処理が残る

タイムシフトGUIが開始した処理はGUI終了時に停止する設計です。残る場合はタスクマネージャーでPython、FFmpeg、WhisperX関連プロセスを確認してください。監視アプリ本体のプロセスは別管理です。

### 日本語が文字化けする

付属CMDはUTF-8を有効化します。直接起動する場合は次を設定してください。

```powershell
$env:PYTHONUTF8='1'
$env:PYTHONIOENCODING='utf-8'
```

## テスト

```powershell
.\.venv\Scripts\python.exe -m pytest
```

変更箇所に応じて、特定テストだけを実行できます。

```powershell
python -m pytest tests\test_step13_index_generator.py
python -m pytest tests\test_step15_lolipop_uploader.py
python -m pytest tests\test_timeshift_acquisition.py
```

## 注意事項

- ニコニコ生放送の利用規約、著作権、配信者・コメント投稿者のプライバシーを守って利用してください。
- 録画・文字起こし・公開は、必要な権利と許可がある範囲で行ってください。
- AIによる文字起こし、要約、人物タグ、感情分析には誤りが含まれます。
- 公開前に生成HTMLと個人情報を必ず確認してください。

## ライセンス

現時点ではライセンスファイルを同梱していません。第三者へ配布・利用許諾する場合は、依存ライブラリと外部ツールのライセンスを確認し、リポジトリへ適切なライセンスを追加してください。
