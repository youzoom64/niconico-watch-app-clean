# niconico-watch-app

ニコ生監視アプリを、要素ごとに作って最後に統合するための新プロジェクト。

最初の要素として、recent ページを Selenium で1分ごとに見に行くトラッカーを実装する。
25分経過した放送だけ `ndgr_client download` で過去ログ全件を取得する。

最初の目的は「生IDコメント者、または指定キーワードを含むコメントがある放送だけ保存する」こと。
一致しなかった `.nicojk` は削除する。

## 起動

GUI:

```bat
scripts\start_gui.cmd
```

トラッカーだけ:

```bat
scripts\start_tracker.cmd
```

1回だけ試す:

```bat
scripts\run_once.cmd
```

## 設定

`config.json`

- `target_user_ids`: 追うユーザーID。例: `["12345678"]`
- `target_keywords`: 追うキーワード。例: `["おは", "ウホ"]`
- `poll_seconds`: 既定60秒
- `min_elapsed_minutes`: 既定25分

### user_sessionの取得方法

1. メインGUIの「基本設定」を開く。
2. 「ログイン用Chromeを開く」を押す。
3. 開いたChromeで、録画に使うニコニコアカウントへログインする。
4. GUIへ戻り「ログイン完了後に取得」を押す。
5. `user_session`欄へ値が入ったことを確認して「保存」を押す。
6. SlNicoLiveRecでは「user_sessionでログイン」を選び、同じ値を設定する。

GUIで取得できない場合は、ログイン済みChromeでニコニコを開き、`F12` → `Application` → `Storage` → `Cookies` → `https://www.nicovideo.jp`から`user_session`のValueを確認する。

`user_session`はログイン資格情報です。スクリーンショット、README、GitHub、チャットへ貼り付けないでください。ログアウト後やログインエラー時は取り直してください。

## 保存方針

- recent DOMの取得結果は `data/tracker.db` に保存する。
- 25分以上の放送はコメント取得対象にする。
- コメントに `target_user_ids` または `target_keywords` があれば `storage/hits/{lv}/` に `.nicojk` とJSONを保存する。
- 一致しなければ一時 `.nicojk` は削除し、DBには「チェック済み/未一致」だけ残す。
