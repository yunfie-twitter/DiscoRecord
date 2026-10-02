# Discord ボイス録音 Bot

Python 3.12 を推奨。`main.py` だけで Bot が動作します。
Pycord の DAVE 受信対応 PR #3159 のコミット
`10a5e8cf13bd8db21f95baf62cf866056d1e1091` に固定しています。
開発版なので実際のサーバーで音声を録音・再生して確認してください。
安定版の Pycord や discord.py への置き換えは、このコードの対応範囲外です。

## 準備と招待

1. [Discord Developer Portal](https://discord.com/developers/applications) で Application を作成し、Bot ページでトークンを取得します。
2. OAuth2 の URL Generator で `bot` と `applications.commands` を選択します。
3. 権限として「チャンネルを見る」「接続」「発言」「メッセージを送信」「ファイルを添付」を指定します。スレッドで使う場合は「スレッドでメッセージを送信」も必要です。
4. 生成した招待 URL を開き、サーバーに追加します。各チャンネルの権限上書きでも許可してください。Administrator 権限は不要です。

Message Content Intent や Server Members Intent は不要です。
サーバー内でコマンドが許可されている利用者は、他の利用者が開始した録音も停止できます。

## インストールと起動（Windows / PowerShell）

Git と Python をインストールし、作業フォルダーで実行します。
discord.py と同じ環境にインストールしないでください。

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
winget install --id Gyan.FFmpeg --exact
```

FFmpeg インストール後はターミナルを開き直し、`ffmpeg -version` が実行できることを確認します。
FFmpeg は Python パッケージではなく、別途インストールする実行ファイルです。

```powershell
$env:DISCORD_TOKEN = "取得した Bot トークン"
.\.venv\Scripts\python.exe main.py
```

トークンをソースや Git に保存しないでください。Linux では FFmpeg と libopus をインストールしてください。
Opus の自動検索に失敗する環境では、`OPUS_LIBRARY` に共有ライブラリの絶対パスを設定できます。

## コマンド

- `/rec start`：実行者が参加している通常のボイスチャンネルを録音します。
- `/rec start channel:対象`：指定した通常のボイスチャンネルに接続して録音します。
- `/rec stop`：停止し、開始時のテキストチャンネルへユーザーごとの `ユーザーID_ユーザー名.mp3` を送信します。

録音中の公開メッセージは `🔴 録音中...` です。終了時は同じメッセージを編集します。
ファイル名にはニックネームではなくユーザー名を使用し、使用できない記号は置換します。
ステージチャンネルと DM は対象外です。停止後は Bot が切断します。

録音機能を使う際は利用者に通知し、同意を得ることを推奨します。
録音データはメモリーに保持するため、長時間録音ではメモリー使用量が増えます。
送信処理後に Bot 内のデータを破棄しますが、Discord に送った添付はメッセージに残ります。
容量上限を超えたファイル、変換・送信に失敗したファイルは再送用に保存しません。
録音中のプロセス終了ではデータが失われます。

## 検証

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

モックで変換完了待ち、ユーザー別送信、容量超過、変換・送信失敗、状態解放を検証します。
FFmpeg が PATH にある場合は実際の MP3 変換とデコードも検証します。
実機では以下を確認してください。

- ２人以上が発話し、それぞれの MP3 を再生できる。
- 途中参加・退出、停止後の再録音、複数サーバーでの並行録音。
- 未参加、権限不足、二重開始、未録音での停止、無音、Bot の強制切断。

この作業環境での確認結果は Python 3.14 の固定版インストール、API 読み込み、モック検証、FFmpeg 変換です。
Python 3.12 および実際の Discord 接続での録音は未検証です。

参考：[Pycord DAVE 受信対応 PR](https://github.com/Pycord-Development/pycord/pull/3159)
