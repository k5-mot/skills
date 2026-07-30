# translate-ja runner

`skills/translate-ja/run.sh` と `skills/translate-ja/run.ps1` は、translate-ja の 7 工程を順番に呼び出すユーティリティです。

## 使い方

```bash
./skills/translate-ja/run.sh --input ./docs/source/source.pdf
```

```powershell
./skills/translate-ja/run.ps1 -InputPath ./docs/source/source.pdf
```

各工程は期待する成果物が既に存在する場合にスキップされます。再生成したい場合は `--force` または `-Force` を指定します。

Python は `PYTHON_BIN` が指定されていればそれを使います。未指定でプロジェクトの `.venv/bin/python3` が存在する場合は `.venv/bin/python3` を優先し、最後に `python3` / `python` へフォールバックします。

一括実行では Docling 前処理に async endpoint を使います。重い PDF でも task status を poll しながら待つため、同期 HTTP リクエストで長時間無音になることを避けます。

翻訳用語辞書を使う場合は UTF-8 CSV を渡します。

```bash
./skills/translate-ja/run.sh --input ./docs/source/source.pdf --dictionary-csv ./docs/source/dictionary.csv
```

```powershell
./skills/translate-ja/run.ps1 -InputPath ./docs/source/source.pdf -DictionaryCsv ./docs/source/dictionary.csv
```

CSV の列は `english`, `japanese`, `genre`, `description` です。`english` と `japanese` は必須で、同じ `english` が複数ある場合は後の行が使われます。

## 出力構成

出力先を指定しない場合、入力ファイルと同じ階層の `output/` を使います。

```text
output/
  source.bronze.json
  source.silver.json
  source.gold.json
  source.ja.md
  source.ja.docx
  artifacts/
  chunks-en/
  chunks-ja/
  reports/
  logs/
```

`artifacts/` は Docling Serve の `image_export_mode: referenced` と zip 返却に合わせた参照画像ディレクトリです。詳細は [translate-ja-docling-artifacts.md](/workspaces/agent-skills/docs/translate-ja-docling-artifacts.md:1) を参照してください。

## LLM 応答の扱い

構造補正工程は Chat Completions を `stream=True` で呼び出します。OpenAI 互換サーバーによって stream chunk の本文が空になる場合は、同じリクエストを非 stream で 1 回取り直します。

LLM が補正 JSON を Markdown の `json` コードフェンスで包む、または短い前置きを付ける場合でも、`patches` object を抽出して処理します。本文が本当に空の場合は `LLM response was empty` として manifest に記録されます。

## ページ追跡ログ

工程 2 以降は、可能な範囲で処理単位とページ番号をログへ出します。

- 構造補正: `unit=page-0001 page=1 attempt=1` のように開始、失敗、リトライ、成功を出します。
- テキスト成形: 変更した Docling node の `ref` と `pages` を出します。
- チャンク生成: 生成した `chunk`、`kind`、`pages`、元 node refs、文字数を出します。
- 翻訳: `chunk`、`pages`、`attempt` を開始、失敗、リトライ、成功、原文 fallback に出します。
- Markdown 連結: 原文 fallback chunk と Markdown 警告を `chunk`、`pages` 付きで出します。
- Word 変換: ページ単位の入力を持たないため、文書単位の開始と失敗情報を出します。

連続ページは `pages=3-5,8` のように短縮され、ページ情報がない場合は `pages=unknown` になります。
