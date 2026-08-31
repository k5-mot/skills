# 📄 translate-ja-v2

PDF/Word 文書を Docling Serve と OpenAI 互換 API で解析・日本語翻訳し、JSON、Markdown、Word docx を生成するスキルです。

## 🚀 Quick Start

### 📦 1. 依存関係を用意する

リポジトリルートで Python 依存関係を同期します。

```bash
uv sync
```

docx まで生成する場合は `pandoc` も必要です。Markdown/JSON だけ検証する場合は `--skip-docx` を使えます。

### 🔐 2. `.env` を用意する

リポジトリルートの `.env` に次のキーを設定します。

```dotenv
DOCLING_SERVER_URL=https://docling.example.test
DOCLING_API_KEY=your-docling-api-key
OPENAI_BASE_URL=https://openai-compatible.example.test/v1
OPENAI_API_KEY=your-openai-api-key
OPENAI_MODEL=your-model
```

Docling Serve の表構造、セル対応、コード、数式の認識は常に有効です。表構造の解析モードには `accurate` を使います。

timeout、OCR、OpenAI retry、VLM 添付画像数、ログレベルは `translate.py` 内の固定値を使います。バッチ翻訳はJSONモードと出力上限4,096トークンを指定します。429や一時的な5xxなどは最大6回、5秒から最大60秒までの指数バックオフで再試行します。バッチ翻訳の空応答や部分応答は同じ入力を再送せず、要素境界でバッチを二分して再実行します。

### ▶️ 3. 翻訳パイプラインを実行する

`translate.py` はユーザーが直接実行できます。

```bash
uv run python skills/translate-ja-v2/scripts/translate.py \
  --input ./inputs/sample.pdf \
  --output-dir ./outputs/sample \
  --template ./skills/translate-ja-v2/template.dotx \
  --glossary ./skills/translate-ja-v2/examples/glossary.csv \
  --translation-rules ./skills/translate-ja-v2/examples/translation-rules.md
```

VLM 構造補正や docx 生成を省いて軽く検証する場合は次のようにします。

```bash
uv run python skills/translate-ja-v2/scripts/translate.py \
  --input ./sample.pdf \
  --output-dir ./output-v2/sample \
  --skip-vlm \
  --skip-docx
```

翻訳レビューを省略して API 呼び出しを減らす場合は `--skip-review` を指定します。

CLI オプションを確認する場合は `--help` を使います。

```bash
uv run python skills/translate-ja-v2/scripts/translate.py --help
```

Docling Serve への PDF/Word 変換は常に `/v1/convert/file/async` を使います。変換完了待ちでは polling ごとに `poll_count` と status をログへ出力します。

## 🔄 `translate.py` の処理フロー

`scripts/translate.py` は、入力文書を次の順序で処理します。各ステージの実行結果は `manifest.json` に追記されます。

```text
CLI 引数解析
  -> .env 読み込み・ログ初期化
  -> 出力パス構築・manifest 開始記録
  -> ParseStage（Docling 非同期変換）
  -> NormalizeStage（座標による第1段階補正）
  -> StructureStage（VLMによる第2段階補正）
  -> TranslateStage
  -> ReviewStage（翻訳レビュー）
  -> RenderStage（Markdown）
  -> DocxStage（Word docx）
  -> 完了ログ・終了コード返却
```

各フェーズは `scripts/translate.py` 内の具体クラスとして実装します。共通基底クラス、factory、汎用 `StageRunner` は置かず、`run_pipeline()` が各クラスの `run()` を上記の順に呼び出します。これにより、処理順とフェーズ間の受け渡しを一箇所で確認できます。

| クラス | 入力 | 出力 |
| --- | --- | --- |
| `ParseStage` | 入力ファイル | Docling JSON |
| `NormalizeStage` | Docling JSON | 座標補正済み JSON |
| `StructureStage` | 座標補正済み JSON | VLM 構造補正済み JSON |
| `TranslateStage` | 構造補正済み JSON | 翻訳 metadata 付き JSON |
| `ReviewStage` | 翻訳 metadata 付き JSON | レビュー済み JSON |
| `RenderStage` | レビュー済み JSON | Markdown パス |
| `DocxStage` | Markdown パス | docx パス |

### 1. 起動と出力先の準備

Typer で CLI 引数を解析し、`--env` で指定された `.env` を `python-dotenv` で読み込みます。入力ファイル名から各 JSON、Markdown、docx の出力パスを組み立て、入力ファイルの SHA-256 を `manifest.json` の `start` イベントに記録します。

### 2. Docling 非同期変換

`<stem>.docling.json` がない場合、または `--force` 指定時に Docling Serve を呼び出します。

1. `/v1/convert/file/async` へ PDF/Word と変換設定を multipart 送信します。
2. 応答の `task_id` を使って `/v1/status/poll/{task_id}` を 10 秒間隔で polling します。
3. polling ごとに `poll_count`、task status、HTTP status をログへ出力します。
4. 完了後に `/v1/result/{task_id}` から zip を取得し、Docling JSON と `artifacts/` 内の画像を atomic write で保存します。

既存の `<stem>.docling.json` を再利用する場合、この変換だけを省略し、以降のステージは毎回実行します。

### 3. Normalize

Docling JSON を複製し、まず各 text の `prov[].page_no` と `prov[].bbox` を使って読み順を補正します。`BOTTOMLEFT` と `TOPLEFT` の座標原点を判別し、ページ順、上から下、同じ高さでは左から右の順に並べます。座標がない要素は元の位置を保ちます。

並べ替え時は texts 配列だけでなく、`self_ref`、`$ref`、body/group の children 参照も更新し、Docling JSON の参照整合性を保ちます。その後、URL を保護しながら本文と表セルの過剰な記号、空白、改行を決定論的に整形します。コード要素には翻訳対象外の metadata を付与し、各変更を patch として記録したうえで `<stem>.normalized.json` を保存します。

### 4. Structure

Normalize で座標補正した Docling 要素をページごとに分け、1ページ画像とそのページの text JSON を OpenAI 互換 API へ渡します。VLM は段組みなど座標だけでは曖昧な箇所を判断し、見出し、レベル、本文順序、分割検出された表・コード・段落の結合を patch だけで返します。

1ページ分の text context が 50,000 文字を超える場合は、同じページ内で隣接する2要素を比較して `merge_texts` の要否を判断します。その後、同じページ内の2要素を総当たりで比較し、順序が明らかに逆の場合だけ `swap_texts` を適用します。許可された `set_label`、`set_level`、`set_text`、`reorder_texts`、`merge_texts`、`swap_texts` だけを適用し、`<stem>.structured.json` を保存します。

`--skip-vlm` はこの第2段階補正だけを省略します。Normalize の座標補正は常に実行されます。

### 5. Translate

OpenAI 互換 API で texts と tables をバッチ翻訳し、原文を保持したまま `translate_ja_v2` metadata を追加します。見出しとその配下の下位見出し・本文を意味ブロックとして扱い、同じレベルまたは上位レベルの見出しで次のブロックを開始します。例えば、見出しレベル2、見出しレベル3、本文は同じブロックです。原文合計1,500文字以内で複数ブロックを1回のリクエストへまとめます。1,500文字を超えるブロックは要素境界で分割し、単独要素が上限を超える場合だけ意味を壊す文字列分割を避けて単独送信します。

表タイトルと翻訳対象セルは、原文合計が上限内なら1表につき1回で翻訳し、超える場合はセル境界で分割します。コードブロックと、コード・URL・パス・識別子と判定した表セルはリクエストへ含めません。バッチ応答は入力IDごとのJSONに限定し、IDの欠落、追加、変更、重複があれば翻訳結果を採用しません。一時的な 429、5xx、timeout、接続エラーは指数バックオフで再試行します。

`--glossary` には `english,japanese,desc,genre,note` 列を持つ CSV を指定できます。翻訳対象テキストに `english` が含まれる entry だけを抽出し、翻訳ルールと一緒に LLM へ渡します。`--translation-rules` を省略した場合は既定の翻訳ルールを使います。

- 見出し: `英語 / 日本語`
- 本文: 日本語訳のみ
- 表タイトル: `英語 / 日本語`
- 表セル: 日本語訳。コード、URL、パス、識別子は原文のまま
- コードブロック: 翻訳せず原文を保持

結果は `<stem>.translated.json` に保存します。

### 6. Review

翻訳済み JSON を近接要素とあわせて OpenAI 互換 API へ渡し、誤訳、用語集不一致、前後要素との表記ゆれ、文体の不自然なずれを保守的に修正します。レビューは `translate_ja_v2` metadata の訳文と render 用文字列だけを更新し、原文、Docling 構造、順序、label、表構造は変更しません。

レビュー対象は翻訳済みの本文、見出し、表タイトル、表セルです。コード、URL、パス、識別子など翻訳対象外の要素は含めません。ローカルLLMとの互換性を優先し、structured output や JSON 形式を要求せず、文書順に1要素ずつレビュー後の日本語訳だけを受け取ります。各要素の直前に前後の最新訳文を参照するため、先に補正した表記も後続要素のレビューへ反映されます。応答が空の場合は、その要素だけ元の訳文を使って継続します。訳文を変更した場合は、対象IDと変更文字数をINFOログへ出力します。

結果は `<stem>.reviewed.json` に保存します。`--skip-review` を指定した場合はこの工程を省略し、`<stem>.translated.json` から Markdown を生成します。

### 7. Markdown と Word の生成

レビュー済み JSON の texts、tables、pictures をページ順に並べ、見出し、fenced code block、Markdown 表、画像参照として `<stem>.ja.md` へ書き出します。

`--skip-docx` がなければ pandoc を呼び出し、`--template` の dotx/docx を reference document にして `<stem>.ja.docx` を生成します。pandoc がない環境で docx 生成を指定するとエラーになるため、pandoc を導入するか `--skip-docx` を指定してください。

各 JSON、Markdown、Docling zip 内の artifact は、一時ファイルへ書き込んだ後に flush、`fsync`、`os.replace()` の順で置き換えます。例外発生時は stack trace をログへ出力して終了コード `1`、中断時は `130` を返します。

## 📦 Outputs

出力先には次のファイルが作られます。

```text
output-v2/sample/
├── sample.docling.json
├── sample.normalized.json
├── sample.structured.json
├── sample.translated.json
├── sample.reviewed.json
├── sample.ja.md
├── sample.ja.docx
├── artifacts/
└── manifest.json
```

`--skip-docx` を指定した場合、`sample.ja.docx` は生成されません。
Markdown 内の `artifacts/...` 画像は、docx 変換時に出力ディレクトリ基準で解決されます。

## 🧪 Verification

単体テストは外部 API を fake client で置き換えて実行できます。

```bash
uv run pytest skills/translate-ja-v2/tests/test_translate_pipeline.py
```

静的解析と format 確認は次を実行します。

```bash
uv run ruff check skills/translate-ja-v2
uv run ruff format --check skills/translate-ja-v2
uv run ty check skills/translate-ja-v2
```

## 🧰 Tech Stack

- [Python 3.12+](https://docs.python.org/3/)
- [Typer](https://typer.tiangolo.com/)
- [Pydantic](https://docs.pydantic.dev/)
- [HTTPX](https://www.python-httpx.org/)
- [python-dotenv](https://github.com/theskumar/python-dotenv)
- [OpenAI Python SDK](https://github.com/openai/openai-python)
- [Docling Serve](https://github.com/docling-project/docling-serve)
- [pandoc](https://pandoc.org/)
- [pytest](https://docs.pytest.org/)
- [Ruff](https://docs.astral.sh/ruff/)
- [ty](https://docs.astral.sh/ty/)

## 👤 Author

k5-mot

## 📜 License

MIT
