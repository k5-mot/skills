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

timeout、OCR、OpenAI retry、ログレベルは `translate.py` 内の固定値を使います。アプリの既定ログレベルは `DEBUG`、依存ライブラリは `WARNING` 以上です。これによりOpenAI SDKやHTTP clientのrequest内部データは表示しません。ログ本文は英語で、DEBUGはcyan、INFOはgreen、WARNINGはyellow、ERRORはred、CRITICALはmagentaのANSI色付きレベル名を出力します。工程の開始・完了・省略・ResumeはINFO、polling、バッチ処理、要素単位の変更はDEBUGです。OpenAI request のテキスト上限は `--context-chars`、翻訳バッチの原文上限は `--batch-chars` で変更できます。バッチ翻訳はJSONモードと出力上限4,096トークンを指定します。429や一時的な5xxなどは最大6回、5秒から最大60秒までの指数バックオフで再試行します。バッチ翻訳の空応答や部分応答は同じ入力を再送せず、要素境界でバッチを二分して再実行します。

### ▶️ 3. 翻訳パイプラインを実行する

`translate.py` はユーザーが直接実行できます。

```bash
uv run python skills/translate-ja-v2/scripts/translate.py \
  --context-chars 50000 \
  --batch-chars 5000 \
  --input ./inputs/sample.pdf \
  --output-dir ./outputs/sample \
  --template ./skills/translate-ja-v2/examples/template.dotx \
  --glossary ./skills/translate-ja-v2/examples/glossary.csv \
  --translation-rules ./skills/translate-ja-v2/examples/translation-rules.md
```

VLM 構造補正や docx 生成を省いて軽く検証する場合は次のようにします。

```bash
uv run python skills/translate-ja-v2/scripts/translate.py \
  --input ./sample.pdf \
  --output-dir ./outputs/sample \
  --skip-vlm \
  --skip-docx
```

翻訳レビューを省略して API 呼び出しを減らす場合は `--skip-review` を指定します。

OpenAI request のテキスト上限は既定で50,000文字、翻訳バッチの原文上限は既定で1,500文字です。必要に応じて `--context-chars` と `--batch-chars` で変更します。

CLI オプションを確認する場合は `--help` を使います。

```bash
uv run python skills/translate-ja-v2/scripts/translate.py --help
```

Docling Serve への PDF/Word 変換は常に `/v1/convert/file/async` を使います。変換完了待ちでは polling ごとに `poll_count` と status をDEBUGログへ出力します。

## 🔄 `translate.py` の処理フロー

`scripts/translate.py` は、入力文書を次の順序で処理します。`manifest.json` には各工程の状態、入力・設定・出力の SHA-256、要素別の進捗を記録します。

```text
CLI 引数解析
  -> .env 読み込み・ログ初期化
  -> 出力パス構築・manifest 開始記録
  -> ParseStage（Docling 非同期変換）
  -> NormalizeStage（座標による第1段階補正）
  -> StructureStage（VLMによる第2段階補正）
  -> CleanStage（連続記号の決定論的校正）
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
| `CleanStage` | VLM 構造補正済み JSON | 句読点校正済み JSON |
| `TranslateStage` | 句読点校正済み JSON | 翻訳 metadata 付き JSON |
| `ReviewStage` | 翻訳 metadata 付き JSON | レビュー済み JSON |
| `RenderStage` | レビュー済み JSON | Markdown パス |
| `DocxStage` | Markdown パス | docx パス |

### 1. 起動と出力先の準備

Typer で CLI 引数を解析し、`--env` で指定された `.env` を `python-dotenv` で読み込みます。入力ファイル名から各 JSON、Markdown、docx の出力パスを組み立て、入力ファイルの SHA-256 を `manifest.json` の `start` イベントに記録します。

### 2. Docling 非同期変換

`<入力stem>.json` がない場合、または `--force` 指定時に Docling Serve を呼び出します。

1. `/v1/convert/file/async` へ PDF/Word と変換設定を multipart 送信します。
2. 応答の `task_id` を使って `/v1/status/poll/{task_id}` を 10 秒間隔で polling します。
3. polling ごとに `poll_count`、task status、HTTP status をDEBUGログへ出力します。
4. 完了後に `/v1/result/{task_id}` から zip を取得します。ZIP内の唯一のJSONを `<入力stem>.json` として atomic write で保存します。`artifacts/` は一時ディレクトリへ展開してからディレクトリ単位で置換するため、以前の変換で作られた不要画像は残りません。

入力ファイル、`<入力stem>.json`、`artifacts/` の各 SHA-256 が manifest の完了記録と一致する場合、この変換を省略します。単にJSONが存在するだけでは再利用しません。

### 3. Normalize

Docling JSON を複製し、まず各 text の `prov[].page_no` と `prov[].bbox` を使って読み順を補正します。`BOTTOMLEFT` と `TOPLEFT` の座標原点を判別し、ページ順、上から下、同じ高さでは左から右の順に並べます。座標がない要素は元の位置を保ちます。

並べ替え時は texts 配列だけでなく、`self_ref`、`$ref`、body/group の children 参照も更新し、Docling JSON の参照整合性を保ちます。Normalizeでは本文、label、表セルを変更せず、座標順の変更だけをpatchとして記録して `document.normalized.json` を保存します。

### 4. Structure

Normalize で座標補正した Docling 要素をページごとに分け、`pages[].image.uri` が指す1ページ画像、そのページのtext JSON、表セルを OpenAI 互換 API へ渡します。URIや画像ファイルがない場合は無関係な画像を推測せず、テキストだけを渡します。VLMは本文と誤認識されたコードのlabelを `code` へ変更し、同じコードブロックに属する前後要素を連結します。表セルでは原文に完全一致するインラインコードspanを検出し、`structure_ja_v2.inline_code_spans` に保存します。

1ページ分の context が `--context-chars` の上限（既定50,000文字）を超える場合は、同じページ内で隣接する2要素を比較してコード判定と `merge_texts` の要否を判断します。その後、同じページ内の2要素を総当たりで比較し、順序が明らかに逆の場合だけ `swap_texts` を適用します。表セルはcontext上限内のまとまりに分割します。許可された `set_label`、`set_level`、`set_text`、`reorder_texts`、`merge_texts`、`swap_texts`、`set_table_cell_inline_code` だけを適用し、`document.structured.json` を保存します。

`--skip-vlm` はこの第2段階補正だけを省略します。Normalize の座標補正は常に実行されます。

### 5. Clean

Structure後の本文と表セルについて、3文字以上連続する `.` を `...`、`・` を `・・・` へ縮め、`document.cleaned.json` に保存します。2文字以下および最初から3文字の連続は変更しません。見出し、コードブロック、Structureが `structure_ja_v2.inline_code_spans` に記録した表セル内コードは変更しません。

Cleanは外部APIを使わない決定論的な工程です。manifestには要素別進捗を持たず、成果物とhashが一致する場合に工程単位でResumeします。

### 6. Translate

OpenAI 互換 API で texts と tables をバッチ翻訳し、原文を保持したまま `translate_ja_v2` metadata を追加します。見出しとその配下の下位見出し・本文を意味ブロックとして扱い、同じレベルまたは上位レベルの見出しで次のブロックを開始します。例えば、見出しレベル2、見出しレベル3、本文は同じブロックです。原文合計を `--batch-chars` の上限（既定1,500文字）以内に収めて、複数ブロックを1回のリクエストへまとめます。上限を超えるブロックは要素境界で分割し、単独要素が上限を超える場合だけ意味を壊す文字列分割を避けて単独送信します。

表タイトルと翻訳対象セルは、原文合計が上限内なら1表につき1回で翻訳し、超える場合はセル境界で分割します。コードブロックと、コード・URL・パス・識別子と判定した表セルはリクエストへ含めません。バッチ応答は入力IDごとのJSONに限定し、IDの欠落、追加、変更、重複があれば翻訳結果を採用しません。一時的な 429、5xx、timeout、接続エラーは指数バックオフで再試行します。

`--glossary` には `english,japanese,desc,genre,note` 列を持つ CSV を指定できます。翻訳対象テキストに `english` が含まれる entry だけを抽出し、翻訳ルールと一緒に LLM へ渡します。`--translation-rules` を省略した場合は既定の翻訳ルールを使います。

- 見出し: `英語 / 日本語`
- 本文: 日本語訳のみ
- 表タイトル: `英語 / 日本語`
- 表セル: 日本語訳。コード、URL、パス、識別子は原文のまま
- コードブロック: 翻訳せず原文を保持

結果は `document.translated.json` に保存します。

### 7. Review

翻訳済み JSON を近接要素とあわせて OpenAI 互換 API へ渡し、誤訳、用語集不一致、前後要素との表記ゆれ、文体の不自然なずれを保守的に修正します。レビューは `translate_ja_v2` metadata の訳文と render 用文字列だけを更新し、原文、Docling 構造、順序、label、表構造は変更しません。

レビュー対象は翻訳済みの本文、見出し、表タイトル、表セルです。コード、URL、パス、識別子など翻訳対象外の要素は含めません。ローカルLLMとの互換性を優先し、structured output や JSON 形式を要求せず、文書順に1要素ずつレビュー後の日本語訳だけを受け取ります。各要素の直前に前後の最新訳文を参照するため、先に補正した表記も後続要素のレビューへ反映されます。応答が空の場合は、その要素だけ元の訳文を使って継続します。訳文を変更した場合は、対象IDと変更文字数をDEBUGログへ出力します。

結果は `document.reviewed.json` に保存します。`--skip-review` を指定した場合はこの工程を省略し、`document.translated.json` から Markdown を生成します。

### 8. Markdown と Word の生成

レビュー済み JSON の texts、tables、pictures をページ順に並べ、見出し、fenced code block、Markdown 表、画像参照として `document.ja.md` へ書き出します。

`--skip-docx` がなければ pandoc を呼び出し、`--template` の dotx/docx を reference document にして `document.ja.docx` を生成します。pandoc がない環境で docx 生成を指定するとエラーになるため、pandoc を導入するか `--skip-docx` を指定してください。

各 JSON、Markdown、Docling zip 内の artifact は、一時ファイルへ書き込んだ後に flush、`fsync`、`os.replace()` の順で置き換えます。例外発生時は stack trace をログへ出力して終了コード `1`、中断時は `130` を返します。

## 📦 Outputs

出力先には次のファイルが作られます。

```text
outputs/sample/
├── sample.json
├── document.normalized.json
├── document.structured.json
├── document.cleaned.json
├── document.translated.json
├── document.reviewed.json
├── document.ja.md
├── document.ja.docx
├── artifacts/
└── manifest.json
```

`--skip-docx` を指定した場合、`document.ja.docx` は生成されません。
Markdown 内の `artifacts/...` 画像は、docx 変換時に出力ディレクトリ基準で解決されます。

## ⏯️ Resume

同じコマンドを再実行すると、`manifest.json` と成果物の SHA-256 を照合し、完了済みの工程を再利用します。入力、工程設定、成果物のいずれかが変わった工程は再実行され、以降の工程も新しい入力から処理されます。`--force` は Parse から強制的にやり直します。

- Structure は VLM がページ単位で処理するため、成功したページ内のtext refと表セルrefを完了として記録し、未完了要素を含む最初のページから再開します。
- Translate は本文、見出し、表タイトル、表セルの各 ID を記録し、未翻訳要素から再開します。
- Review はレビュー対象の各 ID を記録し、未レビュー要素から再開します。
- Parse、Normalize、Clean、Markdown、Docx は工程単位で完了状態を記録し、完了済み成果物を再利用します。

処理中に停止した Structure、Translate、Review の部分成果物は、それぞれの通常の出力 JSON に atomic write されます。別のチェックポイントファイルは作りません。

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
