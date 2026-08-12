# translate-ja SPEC v0

この文書は `translate-ja` の v0 仕様である。旧Docling artifacts調査メモ、runnerメモ、Chunk JSONL採用ADRの内容を集約し、Docling artifacts、runner、一括実行、Chunk JSONL 採用理由を含める。

## 想定利用方法

```bash
# 1. PDF -> Docling-Schema-JSON
python preprocess_doc_with_docling.py --input ./docs/source/source.pdf --output ./docs/source/output/source.bronze.json
# 2. Docling-Schema-JSON -> Docling-Schema-JSON
python realign_doc_struct_with_llm.py --input ./docs/source/output/source.bronze.json --output ./docs/source/output/source.silver.json
# 3. Docling-Schema-JSON -> Docling-Schema-JSON
python clean_doc.py --input ./docs/source/output/source.silver.json --output ./docs/source/output/source.gold.json
# 4. Docling-Schema-JSON -> Chunk-JSONL
python chunk_docling_json.py --input ./docs/source/output/source.gold.json --output ./docs/source/output/chunks-en/chunks.source.jsonl
# 5. Chunk-JSONL(EN)->Chunk-JSONL(JA)
python translate_chunks.py --input ./docs/source/output/chunks-en/ --output ./docs/source/output/chunks-ja/
# 6. Chunk-JSONL(JA)->Markdown(Full-JA)
python concat_chunks.py --input ./docs/source/output/chunks-ja/ --output ./docs/source/output/source.ja.md
# 7. Markdown(Full-JA)->Word.docx
python convert_md_to_docx_with_docling.py --input ./docs/source/output/source.ja.md --output ./docs/source/output/source.ja.docx
```

## 工程ファイル一覧

1. PDF->Docling-Schema-JSON：preprocess_doc_with_docling.py
2. Docling-Schema-JSON->Docling-Schema-JSON：realign_doc_struct_with_llm.py
3. Docling-Schema-JSON->Docling-Schema-JSON：clean_doc.py
4. Docling-Schema-JSON->Chunk-JSONL：chunk_docling_json.py
5. Chunk-JSONL(EN)->Chunk-JSONL(JA)：translate_chunks.py
6. Chunk-JSONL(JA)->Markdown(Full-JA)：concat_chunks.py
7. Markdown(Full-JA)->Word.docx：convert_md_to_docx_with_docling.py

## 目的

`translate-ja` は、PDF、Word、PowerPoint、HTML、画像などの入力ドキュメントを Docling schema JSON に正規化し、構造補正、テキスト成形、Chunk JSONL 化、日本語翻訳、Markdown 連結、Word 出力までを一貫して実行するスキルである。

主なゴールは次の通り。

- 原文ドキュメントの見出し、本文、表、コードブロック、画像参照、ページ対応をできるだけ維持する。
- Docling の構造解析結果を LLM/VLM で補正し、ページ画像との突合により見出し階層や読み順のブレを減らす。
- 翻訳前に不要な繰り返し文字や過剰な記号を削減し、LLM 入力コストと翻訳ノイズを下げる。
- Chunk JSONL を翻訳単位の正本として扱い、表とコードブロックは分割しない。
- 最終成果物として日本語 Markdown と、`template.dotx` を適用した日本語 Word ファイルを生成する。

## 前提

すべての処理は Python スクリプトとして実装する。`pandoc` の呼び出しだけは外部バイナリ実行を許可するが、直接シェルスクリプトを書かず、Python の `subprocess.run()` で実行する。

必要な外部サービスと環境変数は `.env.sample` に合わせる。

- `DOCLING_SERVER_URL`: Docling Serve のベース URL。例: `http://docling:5001`
- `DOCLING_API_KEY`: Docling Serve の API キー。HTTP ヘッダー `X-Api-Key` に設定する。
- `OPENAI_BASE_URL`: OpenAI 互換 Chat Completions API のベース URL。例: `http://litellm:4000`
- `OPENAI_API_KEY`: OpenAI 互換 API キー。
- `OPENAI_MODEL`: 構造補正と翻訳に使う既定モデル。
- `OPENAI_TIMEOUT_SECONDS`: OpenAI Python クライアントの timeout 秒数。ローカル LLM を想定し、既定値は `1800` 秒とする。
- `LOG_LEVEL`: logger のログレベル。`DEBUG` のとき、LLM stream の受信内容を逐次ログ出力する。
- `LANGFUSE_PUBLIC_KEY`: Langfuse 連携用の public key。任意。
- `LANGFUSE_SECRET_KEY`: Langfuse 連携用の secret key。任意。logger、manifest、エラーには出さない。
- `LANGFUSE_OTEL_HOST`: Langfuse OTEL または Langfuse 連携先ホスト。任意。
- `LANGFUSE_TRACE_USER_ID`: Langfuse の `langfuse_trace_user_id` に使うユーザー ID。任意。

Docling Serve は v1 API を前提にし、同期処理は `/v1/convert/file`、長時間処理は `/v1/convert/file/async`、ポーリングは `/v1/status/poll/{task_id}`、結果取得は `/v1/result/{task_id}` を使う。実装時は起動中サーバーの `/docs` を正として、オプション名の差分を吸収できるクライアント層を置く。

## 非対象

- レイアウト完全再現を目的とした DTP 変換。
- 原文 Word の全スタイルを完全継承する処理。
- PDF 内画像そのものの翻訳、画像編集、図表の再描画。
- 翻訳品質を人手でレビューする UI。
- LLM/VLM による事実追加や内容要約。翻訳では原文の意味を保持する。

## 成果物

想定利用方法に基づき、入力ドキュメントは `./docs/source/source.pdf` に置き、中間成果物と最終成果物は `./docs/source/output/` 以下に保存する。`output` ディレクトリ以外へ中間成果物を書き出さない。

```text
./docs/source/
  source.pdf
  output/
    source.bronze.json
    source.silver.json
    manifest.realign.json
    source.gold.json
    source.ja.md
    source.ja.docx
    artifacts/
      image_000000_<hash>.png
      page_000001_<hash>.png
    chunks-en/
      chunks.source.jsonl
    chunks-ja/
      chunks.ja.jsonl
      manifest.translate.json
    reports/
      preprocess_report.json
    logs/
      run.jsonl
```

JSONL は 1 行 1 JSON とし、途中再開と差分確認をしやすくする。

## v0集約メモ

### Docling referenced image artifacts

Docling Serve v1 / Docling Jobkit で `image_export_mode: "referenced"` かつ zip 返却を使う場合、エクスポート本文と同じ階層に `artifacts/` ディレクトリが作られ、その中に参照画像が PNG として保存される前提で扱う。

想定される zip 内レイアウト:

```text
converted_docs.zip
├── <source-stem>.json
└── artifacts/
    ├── image_000000_<hash>.png
    ├── image_000001_<hash>.png
    ├── page_000001_<hash>.png
    └── ...
```

Markdown も同時に出す場合でも同じ `artifacts/` を参照する。ただし `translate-ja` v0 では後段の正本を Chunk JSONL とするため、初回変換では Markdown 全文生成を必須にしない。

`preprocess_doc_with_docling.py` は Docling Serve zip 返却を前提にし、zip 内の `*.json` を `source.bronze.json` として保存し、zip 内の `artifacts/` を同じ出力ディレクトリへ展開する。

```text
output/
├── source.bronze.json
└── artifacts/
    ├── image_000000_<hash>.png
    ├── image_000001_<hash>.png
    ├── page_000001_<hash>.png
    └── ...
```

JSON 内の `image.uri` は `artifacts/...png` のような相対パスとして扱い、`source.bronze.json` の親ディレクトリを基準に解決する。`DOCLING_SERVE_ARTIFACTS_PATH` / `--artifacts-path` はモデル重みロード用であり、出力画像ディレクトリ指定ではない。

### Chunk JSONL採用ADR

Translate JA は、補正済み Docling schema JSON から英語全文 Markdown を一度出力せず、Gold JSON から直接 Chunk JSONL を生成する。これにより、補正済み Docling schema JSON を Docling Serve が Markdown 再エクスポートできるかに依存せず、chunking、resume behavior、structure-preserving translation が 1 つの安定した正本を共有できる。

`chunks-en/chunks.source.jsonl` を翻訳前チャンクの正本、`chunks-ja/chunks.ja.jsonl` を翻訳後チャンクの正本として扱う。

## Manifest

LLM/VLM を使う処理は、進捗管理、中断再開、事後解析のために manifest ファイルを必ず作成する。manifest はログではなく、その工程の再開判定に使う状態ファイルである。

対象工程:

- `realign_doc_struct_with_llm.py`: output ファイルと同じ階層に `manifest.realign.json` を保存する。
- `translate_chunks.py`: output ディレクトリ直下に `manifest.translate.json` を保存する。

manifest の共通要件:

- スクリプト開始時に manifest を読み込み、入力ファイル、入力ハッシュ、出力先、処理設定、モデル設定が一致する場合は未完了の処理単位から再開する。
- `Ctrl+C` などの中断時も、完了済み処理単位と処理中だった単位が分かるように、処理単位ごとに manifest を更新する。
- manifest 更新は一時ファイルへ書いてから `rename` する atomic write とする。
- API キー、Authorization ヘッダー、`.env` の秘密値は manifest に保存しない。
- 処理単位の状態は `pending`、`running`、`success`、`failed`、`skipped`、`fallback_source` を基本とする。
- 再実行時、`success`、`skipped`、`fallback_source` の処理単位は既定で再実行しない。必要な場合は `--force` で manifest と出力を作り直す。
- manifest と既存出力が矛盾する場合は安全側に倒し、対象処理単位を再実行するか、明示的なエラーにする。
- `manifest.realign.json` は `correction_report.json` の役割を兼ねる。構造補正の変更箇所、理由、確信度、補正パッチ、適用結果は manifest に保存し、別ファイルの `correction_report.json` は作らない。

manifest の共通フィールド例:

```json
{
  "schema_version": 1,
  "script": "translate_chunks.py",
  "run_id": "uuid-or-stable-run-id",
  "started_at": "2026-07-30T00:00:00Z",
  "updated_at": "2026-07-30T00:10:00Z",
  "input_path": "./docs/source/output/chunks-en/",
  "input_sha256": "sha256...",
  "output_path": "./docs/source/output/chunks-ja/",
  "model": "google/gemma4:31b",
  "settings": {
    "target_lang": "ja",
    "temperature": 0.0,
    "timeout_seconds": 1800,
    "openai_max_retries": 0,
    "app_max_retries": 10,
    "stream": true,
    "langfuse_enabled": true
  },
  "units": [
    {
      "unit_id": "chunk-0001",
      "status": "success",
      "attempts": 1,
      "updated_at": "2026-07-30T00:01:00Z",
      "output_ref": "chunks.ja.jsonl#chunk-0001",
      "changes": []
    }
  ]
}
```

## LLM クライアント設定

`realign_doc_struct_with_llm.py` と `translate_chunks.py` は OpenAI Python クライアントを使う。ローカル LLM は応答開始や生成に時間がかかるため timeout は長めに設定し、OpenAI Python SDK の自動リトライは無効化する。

- timeout は `OPENAI_TIMEOUT_SECONDS` を使い、未設定時は `1800` 秒とする。
- OpenAI Python クライアントの `max_retries` は `0` とする。
- アプリ側の LLM 出力修復、構造検証、API 失敗時の再試行は最大 `10` 回とする。
- Chat Completions は stream 処理を有効化する。
- `LOG_LEVEL=DEBUG` のとき、stream で受信した差分テキストを logger へ逐次出力する。
- DEBUG ログには API キー、Authorization ヘッダー、`.env` の秘密値を出さない。
- INFO ログではチャンク ID、補正単位 ID、試行回数、開始/終了、処理時間だけを出す。
- stream 出力の逐次ログは、長時間処理中に外から「動いている」ことを確認するための運用機能として扱う。
- Langfuse 環境変数が設定されている場合は、OpenAI Chat Completions のリクエストに Langfuse トレース用ヘッダーを付ける。
- Langfuse ヘッダーはトレースの紐付け用であり、`LANGFUSE_SECRET_KEY` などの秘密値を直接入れない。

実装イメージ:

```python
extra_headers = build_langfuse_headers(
    script_name="translate_chunks.py",
    run_id=manifest.run_id,
    unit_id=chunk.chunk_id,
    user_id=os.environ.get("USER", "translate-ja"),
)

client = OpenAI(
    base_url=settings.openai_base_url,
    api_key=settings.openai_api_key,
    timeout=settings.openai_timeout_seconds,
    max_retries=0,
)

stream = client.chat.completions.create(
    model=settings.openai_model,
    messages=messages,
    temperature=0.0,
    stream=True,
    extra_headers=extra_headers,
)
```

## Langfuse ヘッダー

Langfuse 連携は optional とする。`LANGFUSE_PUBLIC_KEY`、`LANGFUSE_SECRET_KEY`、`LANGFUSE_OTEL_HOST` のいずれかが `.env` または環境変数に設定されている場合、`realign_doc_struct_with_llm.py` と `translate_chunks.py` は OpenAI 互換 API リクエストへトレース用ヘッダーを付ける。

ヘッダー値は Open WebUI の `MESSAGE_ID`、`CHAT_ID`、`USER_ID` のような対話 ID ではなく、ドキュメント翻訳バッチの実行 ID と処理単位 ID から生成する。

- `langfuse_trace_id`: manifest の `run_id`。未作成なら `uuid4` で生成し manifest に保存する。
- `langfuse_trace_name`: `translate-ja:<input_stem>`。例: `translate-ja:source`
- `langfuse_session_id`: 入力ドキュメント単位で安定する ID。例: `sha256(input_path)[:16]`
- `langfuse_trace_user_id`: `LANGFUSE_TRACE_USER_ID` があればその値、なければ `USER`、それもなければ `translate-ja`
- `langfuse_tags`: JSON 文字列。既定値は `["translate-ja", "<script_name>"]`
- `langfuse_generation_id`: `<run_id>:<unit_id>:<attempt>`。再試行ごとに一意にする。
- `langfuse_generation_name`: `<script_name>:<unit_id>`。例: `translate_chunks.py:chunk-0001`

ヘッダー例:

```python
{
    "langfuse_trace_id": run_id,
    "langfuse_trace_name": f"translate-ja:{input_stem}",
    "langfuse_session_id": session_id,
    "langfuse_trace_user_id": trace_user_id,
    "langfuse_tags": json.dumps(["translate-ja", script_name], ensure_ascii=False),
    "langfuse_generation_id": f"{run_id}:{unit_id}:{attempt}",
    "langfuse_generation_name": f"{script_name}:{unit_id}",
}
```

`LOG_LEVEL=DEBUG` でも、`LANGFUSE_SECRET_KEY`、`OPENAI_API_KEY`、`Authorization` ヘッダーは出力しない。Langfuse ヘッダーをログに出す場合は、`langfuse_trace_id`、`langfuse_generation_id` などの非秘密トレース ID だけにする。

機密文書を扱う場合、`LOG_LEVEL=DEBUG` の stream 差分ログには原文や翻訳文が含まれうる。README には、機密文書翻訳時は DEBUG ログを無効にすること、ログ保存先のアクセス制御、ログ削除方針を強い注意書きとして記載する。

## ディレクトリ構成

実装時は次の構成を目安にする。

```text
skills/translate-ja/
  SKILL.md
  SPEC.md
  README.md
  run.sh
  run.ps1
  template.dotx
  scripts/
    config.py
    io_utils.py
    translate_ja.py
    preprocess_doc_with_docling.py
    realign_doc_struct_with_llm.py
    clean_doc.py
    chunk_docling_json.py
    translate_chunks.py
    concat_chunks.py
    convert_md_to_docx_with_docling.py
  tests/
    test_clean_doc.py
    test_chunk_docling_json.py
    test_concat_chunks.py
```

すべての Python 関数には docstring を書く。
ユーザ向けの実行方法、環境変数、トラブルシュートは `skills/translate-ja/README.md` に書く。

## 実行インターフェース

各工程は単体実行できる Python スクリプトとして作成する。一括実行用の `run.sh` と `run.ps1` は、下記 7 本の工程スクリプトを正本として順番に呼び出す。

```bash
python3 skills/translate-ja/scripts/preprocess_doc_with_docling.py \
  --input ./docs/source/source.pdf \
  --output ./docs/source/output/source.bronze.json
```

一括実行:

```bash
./skills/translate-ja/run.sh --input ./docs/source/source.pdf
```

```powershell
./skills/translate-ja/run.ps1 -InputPath ./docs/source/source.pdf
```

`run.sh` と `run.ps1` は、各工程の期待成果物が存在する場合、その工程をスキップする。再実行したい場合は `--force` または `-Force` を指定する。Docling の参照画像 zipball と整合させるため、一括実行時は `output/artifacts/`、`output/chunks-en/`、`output/chunks-ja/`、`output/reports/`、`output/logs/` を作成する。

Python は `PYTHON_BIN` が指定されていればそれを使う。未指定でプロジェクトの `.venv/bin/python3` が存在する場合は `.venv/bin/python3` を優先し、最後に `python3` / `python` へフォールバックする。

一括実行では Docling 前処理に async endpoint を使う。重い PDF でも task status を poll しながら待つため、同期 HTTP リクエストで長時間無音になることを避ける。

翻訳用語辞書を使う場合は UTF-8 CSV を渡す。

```bash
./skills/translate-ja/run.sh --input ./docs/source/source.pdf --dictionary-csv ./docs/source/dictionary.csv
```

```powershell
./skills/translate-ja/run.ps1 -InputPath ./docs/source/source.pdf -DictionaryCsv ./docs/source/dictionary.csv
```

CSV の列は `english`, `japanese`, `genre`, `description` である。`english` と `japanese` は必須で、同じ `english` が複数ある場合は後の行を採用する。

主な CLI オプション:

- `--input`: 各工程の入力ファイル。必須。
- `--output`: 各工程の出力ファイルまたは出力ディレクトリ。必須。
- `--workdir`: 必要な中間成果物の保存先。省略時は入力ファイルと同じ階層の `output/`。
- `--target-lang`: 翻訳先。初期実装は `ja` のみ。
- `--min-chars`: 通常チャンクを連結する目安の下限。既定値 `1000`。
- `--max-chars`: 通常チャンクの上限。既定値 `2000`。
- `--docling-timeout`: Docling 変換の最大待ち時間。既定値 `21600` 秒。
- `--openai-timeout`: OpenAI Python クライアントの timeout 秒数。既定値は `OPENAI_TIMEOUT_SECONDS`、未設定時は `1800`。
- `--dictionary-csv`: 翻訳時に使う用語辞書 CSV。省略時は `TRANSLATE_JA_DICTIONARY_CSV` を見る。
- `--template`: `convert_md_to_docx_with_docling.py` で使う `template.dotx` のパス。
- `--force`: 既存の中間成果物や manifest を再利用せず、対象工程を再実行する。

工程とファイル名の対応:

1. PDF/Word/etc から Docling schema JSON: `preprocess_doc_with_docling.py`
2. Docling schema JSON から構造補正済み Docling schema JSON: `realign_doc_struct_with_llm.py`
3. Docling schema JSON から清掃済み Docling schema JSON: `clean_doc.py`
4. Docling schema JSON から Chunk JSONL: `chunk_docling_json.py`
5. Chunk JSONL EN から Chunk JSONL JA: `translate_chunks.py`
6. Chunk JSONL JA から Markdown Full-JA: `concat_chunks.py`
7. Markdown Full-JA から Word docx: `convert_md_to_docx_with_docling.py`

### ページ追跡ログ

工程 2 以降は、可能な範囲で処理単位とページ番号をログへ出す。

- 構造補正: `unit=page-0001 page=1 attempt=1` のように開始、失敗、リトライ、成功を出す。
- テキスト成形: 変更した Docling node の `ref` と `pages` を出す。
- チャンク生成: 生成した `chunk`、`kind`、`pages`、元 node refs、文字数を出す。
- 翻訳: `chunk`、`pages`、`attempt` を開始、失敗、リトライ、成功、原文 fallback に出す。
- Markdown 連結: 原文 fallback chunk と Markdown 警告を `chunk`、`pages` 付きで出す。
- Word 変換: ページ単位の入力を持たないため、文書単位の開始と失敗情報を出す。

連続ページは `pages=3-5,8` のように短縮し、ページ情報がない場合は `pages=unknown` とする。

### LLM応答フォールバック

構造補正工程は Chat Completions を `stream=True` で呼び出す。OpenAI 互換サーバーによって stream chunk の本文が空になる場合は、同じリクエストを非 stream で 1 回取り直す。

LLM が補正 JSON を Markdown の `json` コードフェンスで包む、または短い前置きを付ける場合でも、`patches` object を抽出して処理する。本文が本当に空の場合は `LLM response was empty` として manifest に記録する。

例:

```bash
python3 skills/translate-ja/scripts/clean_doc.py \
  --input ./docs/source/output/source.silver.json \
  --output ./docs/source/output/source.gold.json \
  --report ./docs/source/output/reports/preprocess_report.json
```

## 処理フロー

### 1. ドキュメント構造解析

入力ファイルを Docling Serve に送信し、Docling schema JSON、ドキュメント内画像、ページ画像を取得する。

初回解析では `to_formats` に少なくとも `json` を指定する。Markdown 全文は生成しない。後段では補正済み Docling schema JSON から直接 Chunk JSONL を生成する。
今回は翻訳タスクが主目的のため、Docling の画像説明は既定で OFF にする。ページ画像は構造補正の根拠として保存するが、画像ごとの説明生成は行わない。
Docling Serve へは `image_export_mode: "referenced"` と zip 返却を要求し、zip 内の Docling schema JSON を `source.bronze.json` として保存する。参照画像は zip 内の `artifacts/` をそのまま `source.bronze.json` と同じ階層へ展開する。
JSON 内の画像 URI は `artifacts/...` の相対パスとして扱い、`source.bronze.json` の親ディレクトリを基準に解決する。`pages/`、`pictures/`、`--pages-dir`、`--pictures-dir` は設けない。

推奨 Docling オプション:

```json
{
  "to_formats": ["json"],
  "do_ocr": true,
  "force_ocr": false,
  "ocr_preset": "tesseract",
  "ocr_lang": ["jpn", "jpn_vert", "eng"],
  "document_timeout": 21600,
  "do_picture_description": false,
  "image_export_mode": "referenced"
}
```

実装では API キーを設定ファイルやログに直書きしない。上記 JSON は仕様上の形を示す例であり、実行時は `.env` から値を組み立てる。画像説明を将来 ON にする場合は、翻訳とは別オプションとして明示的に有効化し、コストと処理時間の増加を README に記載する。

### 2. LLM/VLM による Docling schema 補正

`source.bronze.json` とページ画像を入力し、LLM/VLM で構造補正した `source.silver.json` を生成する。

補正対象:

- 見出しレベルのずれ。
- 箇条書きと本文の誤分類。
- ページをまたいだ見出しと本文の結合ミス。
- 表タイトル、図タイトル、脚注の読み順。
- OCR による明らかな文字化け。ただし推測補完はしない。

補正ルール:

- Docling schema の互換性を壊さない。
- 原文にない情報を追加しない。
- テキスト内容の翻訳はこの段階では行わない。
- 変更箇所、理由、確信度を `manifest.realign.json` に必ず残す。
- `manifest.realign.json` は、後から人が変更理由を追える監査ログとしても扱う。
- LLM 出力は JSON Schema で検証し、不正な場合は最大 10 回までリトライする。
- `manifest.realign.json` を output ファイルと同じ階層に保存する。
- `manifest.realign.json` は、ページ単位または見出しブロック単位の補正状態、入力 JSON のハッシュ、ページ画像ハッシュ、モデル、プロンプトバージョン、リトライ回数、補正パッチ、補正理由、確信度、適用結果を記録する。
- 再実行時は `manifest.realign.json` を読み、成功済みの補正単位をスキップして未完了単位から再開する。
- 補正途中で中断された場合、最後に成功した補正単位までのパッチを再利用し、`running` のまま残った単位は再実行する。

アドバイス: 最初から文書全体を 1 回で補正しようとせず、ページ単位または見出しブロック単位で補正パッチを生成し、Python 側で Docling JSON に適用する設計が堅い。LLM に完全な JSON 全体を書き戻させると、ID、参照、画像メタデータが壊れやすい。

### 3. Python 前処理によるテキスト成形

`source.silver.json` のテキストノードを Python で成形し、`source.gold.json` を生成する。

初期ルール:

- 半角三点リーダ相当の `...` が 3 点を超えて連続する場合は `...` に縮約する。
- 全角中点の `・・・` が 3 点を超えて連続する場合は `・・・` に縮約する。
- ダッシュ、罫線、同一記号が過剰に連続する場合は意味を損なわない範囲で短縮する。
- 連続空白は、コードブロック、表、数式、整形済みテキストを除き 1 つに寄せる。
- 空行は Markdown 変換後の構造に影響しない範囲で最大 2 行にする。
- URL、コード、数式、表セル内の意味を持つ記号列は変更しない。

`preprocess_report.json` には、ルール名、対象ノード ID、変更前後の文字数、サンプルを記録する。

アドバイス: 文字数削減は翻訳品質を上げる一方で、契約書、仕様書、コード例では記号の意味が強い。ノード種別ごとに許可ルールを分け、最初は保守的に実装する。

### 4. Docling schema JSON から Chunk JSONL 生成

`source.gold.json` を読み、`chunks-en/chunks.source.jsonl` を生成する。Docling schema JSON から全文 Markdown を生成する工程は置かない。

`chunks-en/chunks.source.jsonl` が翻訳前チャンクの正本である。個別の `chunk-0001.md` のようなファイルは既定では作成しない。必要な場合だけ、デバッグ用オプションで出力する。

チャンク仕様:

- チャンクは `header_path` と `source_text` をセットで持つ。
- `header_path` は現在位置までの見出し配列とする。
- `source_text` は、Docling schema の対象ブロックをチャンク単位で Markdown 断片としてレンダリングした文字列とする。
- 通常本文は `min_chars` から `max_chars` の範囲を目安にする。ただし、文字数だけで切らず、見出し階層とブロック種別を優先する。
- 見出しとその直後の本文、表、コード、画像説明などの本文ブロックは同じチャンクに入れる。
- チャンク末尾に次セクションの見出しだけが残る分割は禁止する。見出しだけが末尾に来そうな場合は、その見出しを次チャンクへ送るか、直後の本文ブロックまで同じチャンクに含める。
- 見出しだけのチャンクは、文書末尾に本文が存在しない場合など、原文構造として本当に見出し単独である場合に限る。
- 表は文字数に関係なく 1 チャンクとする。
- fenced code block は文字数に関係なく 1 チャンクとし、翻訳しない。
- HTML ブロックも壊れやすいため、原則 1 チャンクとする。
- 1 チャンクが `max_chars` を超える場合でも、表、コード、HTML は分割しない。
- ページ番号、Docling node id、画像参照、表参照など、原文へ戻るためのメタデータをチャンクに残す。

JSONL 形式:

```json
{
  "chunk_id": "chunk-0001",
  "kind": "text",
  "header_path": ["# Title", "## Section"],
  "source_text": "原文...",
  "translatable": true,
  "char_count": 1234,
  "source_node_refs": ["#/texts/12", "#/texts/13"],
  "page_numbers": [3],
  "assets": []
}
```

`kind` は `text`、`table`、`code`、`html`、`image` のいずれかを初期値とする。

アドバイス: Chunk JSONL を正本にすると、Docling Serve が補正済み schema JSON を Markdown 再エクスポートできるかに依存しなくなる。Python 側には、Docling block をチャンク単位の Markdown 断片へ変換する小さな renderer を実装する。

### 5. チャンクの保存

`chunks-en/chunks.source.jsonl` は再開可能な中間形式として保存する。翻訳前に次の検証を行う。

- `chunk_id` が一意である。
- `source_text` が空の翻訳対象チャンクを含まない。
- `source_node_refs` が空でない。
- コードブロックの開始と終了 fence が対応している。
- 表チャンクが Markdown 表として破損していない。

### 6. LLM によるチャンク翻訳

`chunks-en/chunks.source.jsonl` を読み、`chunks-ja/` と `chunks-ja/chunks.ja.jsonl` を生成する。
`translate_chunks.py` は output ディレクトリ直下に `manifest.translate.json` を保存し、チャンク単位で進捗を管理する。

`translate_ja.py` は翻訳用の共通モジュールとし、次の列を持つ UTF-8 CSV 用語辞書を読み込めるようにする。

```csv
"english", "japanese", "genre", "description"
"DoD", "米国国防総省", "軍事用語", "米国の1省庁"
```

辞書 CSV の要件:

- ヘッダー名は `english`、`japanese`、`genre`、`description` とする。ヘッダーや値の前後空白は読み込み時に除去する。
- `english` と `japanese` は必須とし、空行は無視する。
- 同じ `english` が複数回出現した場合は後勝ちとし、`translate_chunks.py` の manifest に辞書ファイルのパス、sha256、登録語数を保存する。
- 翻訳プロンプトには、該当チャンクに出現する用語を優先し、必要に応じて辞書全体を上限件数内で渡す。
- 辞書は訳語統一の指示であり、原文に存在しない語を追加する根拠として使わない。

翻訳ルール:

- 出力は日本語。
- Markdown 構造、見出し記号、箇条書き、表、リンク、画像参照、コード fence を維持する。
- コードブロックは翻訳しない。必要なら直前または直後の説明文だけ翻訳する。
- 表は列数、区切り行、セル数を維持する。セルの自然言語部分のみ翻訳する。
- 固有名詞、製品名、API 名、コマンド、ファイルパス、URL、環境変数は原文を保持する。
- 翻訳困難な箇所は勝手に要約せず、原文を残す。
- 翻訳後に Markdown 構造検証を実行し、壊れていれば最大 10 回までリトライする。
- LLM 呼び出しは stream 処理を有効化する。
- `LOG_LEVEL=DEBUG` のとき、stream で受信した差分テキストをチャンク ID とともに逐次 logger へ出力する。

`chunks-ja/chunks.ja.jsonl` 形式:

```json
{
  "chunk_id": "chunk-0001",
  "kind": "text",
  "header_path": ["# Title", "## Section"],
  "source_text": "原文...",
  "translated_text": "日本語訳...",
  "translatable": true,
  "model": "google/gemma4:31b",
  "status": "success"
}
```

アドバイス: `temperature` は `0.0` を既定にする。翻訳では創造性より再現性を優先する。

### 7. 翻訳済みチャンクの保存

翻訳済みチャンクは 1 チャンクごとに即時追記する。失敗時は `status: "failed"` とエラー要約を残し、再実行時は成功済みチャンクをスキップできるようにする。
`manifest.translate.json` も 1 チャンクごとに更新する。

再試行方針:

- 一時的な API エラーは指数バックオフで最大 10 回リトライする。
- Markdown 構造破損は修復プロンプトで最大 10 回リトライする。
- それでも失敗した場合は原文を `translated_text` に入れ、`status: "fallback_source"` とする。
- 再実行時は `manifest.translate.json` と既存の翻訳済み出力を突合し、`success`、`skipped`、`fallback_source` のチャンクは既定で再実行しない。
- `running` のまま残ったチャンクは、前回実行が中断されたものとして再実行する。
- `failed` のチャンクは既定で再試行対象にする。ただし最大試行回数を超えたものは `--retry-failed` 指定時のみ再試行する。

### 8. チャンク連結

`chunks-ja/chunks.ja.jsonl` を `source.ja.md` に連結する。

連結ルール:

- `chunk_id` 順を維持する。
- 見出しは重複生成しない。
- チャンク境界では空行を 1 から 2 行に整える。
- 表、コードブロック、HTML ブロックの前後には空行を確保する。
- `Chunks(JS)` という表記がある場合は `Chunks(JA)` の誤記として扱う。

### 9. 日本語 Markdown の検証

Word 変換前に `source.ja.md` を検証する。

- fenced code block が閉じている。
- Markdown 表の列数が各行で一致する。
- 画像参照とリンク構文が壊れていない。
- 原文チャンク数と翻訳チャンク数が一致する。
- `fallback_source` の件数をレポートする。

### 10. Word 変換

`source.ja.md` を pandoc で `source.ja.docx` に変換する。

実行は `convert_md_to_docx_with_docling.py` から行う。

```bash
pandoc source.ja.md \
  --from markdown \
  --to docx \
  --reference-doc skills/translate-ja/template.dotx \
  --output source.ja.docx
```

Python 実装では次を満たす。

- `subprocess.run([...], check=True, text=True, capture_output=True)` を使う。
- `template.dotx` が存在しない場合は分かりやすいエラーを出す。
- pandoc が存在しない場合は Word 変換だけ失敗として扱い、Markdown 成果物は成功として残す。
- pandoc の stdout/stderr は `logs/run.jsonl` に保存する。ただし API キーなどの秘密情報は記録しない。

## Python モジュール責務

### `config.py`

- `.env` と環境変数を読み込む。
- 必須値の不足を検証する。
- API キーをログ出力用にマスクする。
- Langfuse 関連環境変数を optional として読み込む。
- Langfuse が設定されている場合だけ、LLM リクエスト用の非秘密トレースヘッダーを組み立てられる設定値を返す。

### `io_utils.py`

- JSON、JSONL、Markdown の読み書きを扱う。
- ログ用の秘密情報マスクを提供する。
- 作業ディレクトリと親ディレクトリを作成する。

### `translate_ja.py`

- UTF-8 CSV 用語辞書を読み込む。
- `english`、`japanese`、`genre`、`description` の列を検証する。
- チャンク本文に出現する用語を抽出し、翻訳プロンプトへ入れる用語リストを生成する。
- CSV 辞書の sha256、登録語数、重複後の有効語数を返す。
- 辞書値を manifest に保存する場合も API キーや秘密情報とは別扱いにし、入力文書と同様にユーザがアクセス制御すべきデータとして README に明記する。

### `preprocess_doc_with_docling.py`

- Docling Serve v1 API の同期、非同期変換を扱う。
- `X-Api-Key` ヘッダーを付ける。
- PDF、Word、PowerPoint、HTML、画像などの入力ファイルから Docling schema JSON を生成する。
- `image_export_mode: "referenced"` の zip 返却を前提に、Docling schema JSON を output ファイルへ保存し、zip 内の `artifacts/` を output ファイルと同じ階層に展開する。
- ページ画像とドキュメント内画像はどちらも `artifacts/` 配下に置き、JSON 内の相対 URI と整合させる。
- 画像説明は既定で OFF にする。

### `realign_doc_struct_with_llm.py`

- Docling JSON とページ画像を LLM/VLM に渡す。
- OpenAI Python クライアントの timeout は既定 `1800` 秒、`max_retries` は `0`、stream は有効にする。
- アプリ側の LLM 出力修復、構造検証、API 失敗時の再試行は最大 `10` 回とする。
- `LOG_LEVEL=DEBUG` のとき、stream 受信差分を逐次 logger へ出力する。
- 補正パッチを受け取り、Python 側で schema に適用する。
- JSON Schema 検証を行う。
- output ファイルと同じ階層に `manifest.realign.json` を作成し、補正単位ごとの進捗、リトライ、適用済みパッチ、補正理由、確信度を記録する。
- `correction_report.json` は作成せず、必要な事後解析情報は `manifest.realign.json` に統合する。
- 再実行時は `manifest.realign.json` から未完了の補正単位を判定し、途中から再開する。

### `clean_doc.py`

- Docling JSON のテキストノードだけを対象に文字列成形する。
- ノード種別ごとの除外制御を行う。
- 変更レポートを出力する。

### `chunk_docling_json.py`

- 清掃済み Docling schema JSON を読み、見出しパス付き Chunk JSONL へ分割する。
- Docling block をチャンク単位の Markdown 断片へレンダリングする。
- `chunks-en/chunks.source.jsonl` を正本として出力する。
- 表、コード、HTML 相当のブロックを不可分チャンクとして扱う。
- 見出しだけがチャンク末尾に来ないように、見出しから本文までの構成を保つ。
- `source_node_refs`、`page_numbers`、`assets` などの原文対応メタデータを残す。

### `translate_chunks.py`

- OpenAI 互換 API でチャンクを翻訳する。
- OpenAI Python クライアントの timeout は既定 `1800` 秒、`max_retries` は `0`、stream は有効にする。
- アプリ側の LLM 出力修復、構造検証、API 失敗時の再試行は最大 `10` 回とする。
- `LOG_LEVEL=DEBUG` のとき、stream 受信差分を逐次 logger へ出力する。
- 成功済みチャンクのスキップ、再試行、構造検証を行う。
- JSONL に逐次保存する。
- output ディレクトリ直下に `manifest.translate.json` を作成し、チャンク単位の進捗、リトライ、出力ファイル、フォールバック状態を記録する。
- 再実行時は `manifest.translate.json` と既存出力を突合し、未完了または再試行対象のチャンクから再開する。

### `concat_chunks.py`

- 翻訳済みチャンクを順に連結する。
- 見出し重複や空行を整える。
- 最終 Markdown 検証を行う。

### `convert_md_to_docx_with_docling.py`

- ファイル名は `convert_md_to_docx_with_docling.py` とする。
- 実際の Word 変換は pandoc を Python から実行する。
- `template.dotx` を使った Word 変換を行う。
- Python 側で docx スタイルを個別編集しない。
- pandoc 未導入時のエラーを扱う。

## コーディングルール

すべての Python スクリプトは次のルールに従う。

- 標準出力への進捗表示に `print()` を使わず、`logging` の logger を使う。
- すべての関数に日本語 docstring を書く。
- コード内コメントは日本語で書く。
- `if __name__ == "__main__":` では `main()` を呼び出す。
- `if __name__ == "__main__":` では `perf_counter()` で処理時間を計測し、logger に記録する。
- 引数解析は `main()` 内で `argparse` を使って行う。専用の引数解析関数は作らない。
- `main()` は `ExitCode` を返し、プロセス終了時に OS へ exit code として渡す。
- 例外種別を過剰に増やさない。例外の種類を細かく作るより、例外発生箇所、対象ファイル、工程名がログとメッセージから分かるようにする。
- API キー、Authorization ヘッダー、`.env` の秘密値は logger、例外、レポートに出さない。

基本形:

```python
if __name__ == "__main__":
    started_at = perf_counter()
    try:
        exit_code = main()
    finally:
        LOGGER.info("処理時間 %.3f 秒", perf_counter() - started_at)
    raise SystemExit(exit_code)
```

## LLM プロンプト方針

構造補正プロンプト:

- 入力 Docling JSON の対象ページまたは対象ブロックを示す。
- ページ画像を根拠として、見出し階層、読み順、表題、脚注のみを補正するよう指示する。
- 出力は補正パッチ JSON のみにする。
- 推測禁止、追加情報禁止、翻訳禁止を明記する。

翻訳プロンプト:

- Markdown 構造を維持する。
- 固有名詞、コード、URL、環境変数、ファイルパスは保持する。
- 表の列数とコード fence を壊さない。
- 原文にない説明を加えない。
- 出力は翻訳済み Markdown 本文だけにする。

## エラー処理

- 各工程は入力ファイルの存在と JSON 妥当性を最初に検証する。
- 外部 API エラーはステータスコード、リトライ回数、対象工程をログに残す。
- 秘密情報はログ、例外、レポートに出さない。
- 中間成果物が存在し検証に通る場合は再利用する。
- LLM/VLM を使う工程は manifest を再開判定の正本として扱う。
- manifest が存在しないが出力が存在する場合は、出力を検証して manifest を再構築できる場合だけ再開する。再構築できない場合は `--force` を要求する。
- manifest 更新中の破損に備え、`manifest.*.json.tmp` へ書いてから `manifest.*.json` へ rename する。
- 最終 Word 変換に失敗しても、`source.ja.md` が生成できていれば処理全体は部分成功として扱う。

## テスト方針

初期実装で必須のテスト:

- 三点、全角中点、過剰ダッシュの縮約。
- URL、コード、数式、表セルが前処理で壊れないこと。
- Docling schema の見出しパスが正しくチャンクへ付与されること。
- チャンク末尾に見出しだけが残らないこと。
- 見出しから本文への構成がチャンク境界で崩れないこと。
- 表とコードブロック相当の Docling block が文字数上限を超えても分割されないこと。
- `manifest.realign.json` に基づき、構造補正が未完了単位から再開できること。
- `manifest.translate.json` に基づき、翻訳が未完了チャンクから再開できること。
- `running` のまま残った manifest 単位が再実行対象になること。
- OpenAI Python クライアントが timeout `1800` 秒、`max_retries=0`、`stream=True` で呼び出されること。
- アプリ側リトライが最大 `10` 回に制御されること。
- `LOG_LEVEL=DEBUG` のとき、stream 差分が logger に逐次出力されること。
- Langfuse 環境変数が未設定の場合、Langfuse ヘッダーが付与されないこと。
- Langfuse 環境変数が設定済みの場合、`langfuse_trace_id`、`langfuse_generation_id` などの非秘密ヘッダーが付与されること。
- `LANGFUSE_SECRET_KEY` が logger、manifest、例外、ヘッダー出力ログに含まれないこと。
- 翻訳済みチャンクの連結で見出しが重複しないこと。
- `fallback_source` を含む JSONL でも Markdown が生成されること。

外部サービスを使うテストは、単体テストではモックにする。`.env` に有効な値が入っていても、テストや CI は Docling Serve、OpenAI 互換 API、pandoc に依存しないことを基本にする。実サービスを使う統合テストは別マーカーに分け、明示的に指定された場合だけ実行する。

## 実装順序

1. `config.py`、`io_utils.py`、JSONL 読み書き、ログ基盤を作る。
2. `clean_doc.py` と単体テストを作る。
3. `chunk_docling_json.py` と単体テストを作る。
4. `translate_chunks.py` をモック可能な OpenAI 互換クライアントとして作る。
5. `concat_chunks.py` と単体テストを作る。
6. `convert_md_to_docx_with_docling.py` を作り、pandoc 未導入時の挙動を固める。
7. `preprocess_doc_with_docling.py` を Docling Serve v1 API に接続する。
8. `realign_doc_struct_with_llm.py` を最小パッチ方式で作る。
9. `SKILL.md` に実行方法と注意点を書く。
10. `README.md` にユーザ向けの実行手順、環境変数、入出力、機密文書翻訳時のログ注意、トラブルシュートを書く。

## 追加アドバイス

- 今回は翻訳タスクなので、Docling の画像説明は既定で OFF にする。将来 ON にする場合も、翻訳品質、コスト、処理時間への影響を README に明記して明示オプション化する。
- 構造補正は品質差が出やすい工程なので、`manifest.realign.json` を監査ログとして必ず残し、人が後から変更理由を追えるようにする。
- Chunk JSONL 生成は翻訳品質に直結する。文字数だけで切らず、見出し階層とブロック種別を優先し、見出しだけがチャンク末尾に残る分割を避ける。
- Word 変換の見た目は `template.dotx` に寄せる。Python 側で docx スタイルを個別編集しない。
- `.env` の値は有効でも、テストや CI では外部サービスに依存しないようモックを基本にする。
- ユーザ向けの説明は `skills/translate-ja/README.md` に書く。内容は利用手順、環境変数、入出力ファイル、トラブルシュートを含める。

## 参考

- Docling Serve REST API: https://docling-project.github.io/docling/usage/api_server/rest_api/
- Docling Serve GitHub: https://github.com/docling-project/docling-serve
