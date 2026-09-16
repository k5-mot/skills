# translate-ja

Docling Serve と OpenAI 互換 API を使い、PDF、Word などの文書を日本語 Markdown と Word docx に翻訳するスキルです。

## 実行方法

入力ファイルを `./docs/source/source.pdf` に置く場合:

```bash
./skills/translate-ja/run.sh --input ./docs/source/source.pdf
```

PowerShell:

```powershell
./skills/translate-ja/run.ps1 -InputPath ./docs/source/source.pdf
```

出力先を変える場合:

```bash
./skills/translate-ja/run.sh \
  --input ./docs/source/source.pdf \
  --output-dir ./docs/source/output \
  --dictionary-csv ./docs/source/dictionary.csv \
  --template ./skills/translate-ja/template.dotx
```

既に成果物がある工程はスキップされます。最初から作り直す場合は `--force` または `-Force` を付けます。

## 出力

中間成果物と最終成果物は `output/` 以下に保存します。

```text
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

`artifacts/` は Docling Serve の `image_export_mode: referenced` の zipball から展開される参照画像置き場です。JSON 内の画像 URI は `source.bronze.json` の親ディレクトリを基準に解決します。

## 環境変数

`.env.sample` を参考に `.env` を用意してください。`.env` は `run.sh` / `run.ps1` が自動で読み込みます。

主な値:

- `DOCLING_SERVER_URL`: Docling Serve の URL。
- `DOCLING_API_KEY`: Docling Serve の API キー。
- `OPENAI_BASE_URL`: OpenAI 互換 API の Base URL。
- `OPENAI_API_KEY`: OpenAI 互換 API の API キー。
- `OPENAI_MODEL`: 翻訳、構造補正に使うモデル。
- `LANGFUSE_PUBLIC_KEY`、`LANGFUSE_SECRET_KEY`、`LANGFUSE_OTEL_HOST`: 任意のLangfuse連携。

timeoutは固定既定値またはCLI引数で指定し、用語辞書は`--dictionary-csv`だけで指定します。上記以外の環境変数や旧別名は読みません。

## 用語辞書 CSV

`translate_chunks.py` は、次の形式の UTF-8 CSV を読み込めます。

```csv
"english", "japanese", "genre", "description"
"DoD", "米国国防総省", "軍事用語", "米国の1省庁"
```

`english` と `japanese` は必須です。同じ `english` が複数回ある場合は後の行を使います。辞書 CSV のパス、sha256、登録語数は `chunks-ja/manifest.translate.json` に保存されます。

## 機密文書の注意

機密文書を翻訳する場合は、外部 API 送信先、ログ保存先、生成物の保管先を必ず確認してください。ログには原文や翻訳文が含まれうるため、保存先のアクセス制御を確認してください。

API キーや Authorization ヘッダーはログ、manifest、例外に出さない実装方針です。ただし、入力文書、翻訳結果、Docling JSON、チャンク JSONL、辞書 CSV、`artifacts/` の画像は機密情報そのものになりえます。

## 必須パッケージ

```bash
pip install requests openai python-dotenv
```

Word 変換には pandoc と `template.dotx` を使います。

## トラブルシュート

- Docling の画像が見つからない場合: `output/artifacts/` が存在し、JSON 内の URI が `artifacts/...` になっているか確認してください。
- 途中で止めた場合: `manifest.realign.json` または `chunks-ja/manifest.translate.json` があれば、再実行時に未完了単位から再開します。
- 既存成果物を使いたくない場合: `--force` または `-Force` を指定してください。
- PowerShell で実行できない場合: 実行ポリシーとカレントディレクトリを確認してください。
