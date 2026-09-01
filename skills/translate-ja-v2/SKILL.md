---
name: translate-ja-v2
description: PDF/Word文書をpython-dotenvで読み込んだ.env設定、Docling Serve、OpenAI互換API、pandocでJSON/PNG、構造補正、要素単位日本語翻訳、Markdown、Word docxへ変換するtranslate-ja-v2パイプラインを実行・修正・レビューする。Use when Codex translates documents with the bundled Python script, revises scripts/translate.py, adjusts Docling/OpenAI/pandoc handling, or reviews whether heading/table bilingual output, body Japanese-only output, table/code cleanup, and VLM structure correction work.
---

# translate-ja-v2

PDF/Word から Docling Serve で JSON/PNG を作り、JSON 上で表・コードブロック整形、VLM 構造補正、要素単位翻訳、Markdown 生成、Word docx 変換まで行うスキル。エージェントスキルの一部として使いやすいよう、正本の Python 実装は [scripts/translate.py](scripts/translate.py) に集約する。

## 最初に読むもの

- 実行・修正を始める前に [implementation-guide.md](references/implementation-guide.md) を読む。
- Stage 詳細、Normalizer rule、VLM、翻訳、Markdown renderer を修正するときは [stages.md](references/stages.md) を読む。
- 細かな仕様判断や受け入れ条件の確認が必要なときだけ [spec-v2.md](references/spec-v2.md) を読む。
- Python コードを書く、CLI を直す、テストを追加する場合は、利用可能なら `python-dev` も併用し、docstring、logger、pytest、Ruff の方針を合わせる。

## 実行

`.env` に Docling Serve と OpenAI 互換 API の環境変数を用意してから実行する。`.env` は `python-dotenv` 経由で読み込む。

```bash
uv run python skills/translate-ja-v2/scripts/translate.py \
  --input ./docs/source/source.pdf \
  --output-dir ./outputs/source \
  --template ./skills/translate-ja-v2/examples/template.dotx
```

Word 変換を後回しにする場合だけ `--skip-docx` を使う。構造補正を明示的に止める検証では `--skip-vlm` を使う。
Docling 変換は常に `/v1/convert/file/async` を使い、polling ごとに poll 回数と status を logging する。
同じコマンドを再実行した場合は manifest と成果物 hash を検証し、Structure、Translate、Review は要素別進捗から、その他は工程の完了状態から Resume する。`--force` は Parse を必ず再実行する。

## 出力

```text
outputs/<stem>/
├── <stem>.json
├── document.normalized.json
├── document.structured.json
├── document.translated.json
├── document.reviewed.json
├── document.ja.md
├── document.ja.docx
├── artifacts/
└── manifest.json
```

Docling ServeのZIP内にある唯一のJSONを、入力ファイルと同じstemの `<stem>.json` として保存する。

## 実装方針

1. 無駄な wrapper や package 階層を増やさず、[scripts/translate.py](scripts/translate.py) 内の具体的な stage クラスを修正する。共通基底クラスや factory は、複数実装の差し替えが必要になるまで追加しない。
2. JSON 原文は上書きせず、翻訳情報は `translate_ja_v2` フィールドへ追加する。
3. 見出しと表タイトルは `英語 / 日本語` で Markdown に出す。
4. 本文は日本語訳のみ Markdown に出す。
5. コード、URL、パス、コマンド、識別子は翻訳せず保護する。
6. Normalize で `prov[].page_no` と `prov[].bbox` を使って読み順を座標補正し、その後に VLM 補正を行う2段階構成にする。
7. VLM には座標補正済みの Docling JSON 要約、bbox、`pages[].image.uri` が指す page PNG を渡し、全文再生成をさせず、`set_label`、`set_level`、`reorder_texts` のような構造 patch だけを返させる。
8. ファイル保存は一時ファイル、flush、fsync、`os.replace()` で行う。
9. 外部 API を使う単体テストは fake client で検証する。
10. `manifest.json` には工程ごとの入力・設定・出力 hash と状態を保存し、Structure、Translate、Review に限って要素 ID ごとの完了状態も保存する。

## 完了判断

MVP は、PDF/Word の投入、Docling JSON/PNG 保存、表・コード整形、VLM による見出し/本文の位置補正、JSON 要素への翻訳フィールド追加、見出し/表タイトルの英日併記、本文の和訳のみ Markdown、pandoc による docx 生成、単体テスト成功を満たした時点で完了とする。
