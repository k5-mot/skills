# translate-ja-v2 実装ガイド

この文書は通常の実装・レビューで最初に読む要約である。現在の正本実装は `scripts/translate.py` である。詳細が必要な場合は `spec-v2.md` を参照するが、エージェントスキルとしては単一スクリプトを優先し、無駄な wrapper や package 階層を増やさない。

## アーキテクチャ

中心方針は `Document != State != Patch` である。

- `Document`: DoclingDocument または Docling 由来 JSON。文書内容だけを持つ。
- `State`: job、stage、page の進捗、retry、artifact hash、error を持つ。文書本文を入れない。
- `Patch`: normalizer や VLM が文書へ加えた差分と理由を持つ。

Pipeline は `Artifact -> Stage -> Artifact` の連鎖として扱う。ただし現行 v2 skill では `StageRunner` の抽象化を作らず、`scripts/translate.py` 内の小さな関数で stage を順に実行する。

## プロジェクト構成

新規実装では、まず skill 内の単一スクリプト構成を採用する。

```text
skills/translate-ja-v2/
├── SKILL.md
├── scripts/
│   └── translate.py
├── references/
└── tests/
```

既存リポジトリへ導入するときは、既存 package manager とテスト構成を尊重する。Python 設定やコード規約は `python-dev` スキルに合わせる。

## Workspace

PDF/Word ごとに出力ディレクトリを作り、次のように保存する。

```text
output-v2/
├── <stem>.docling.json
├── <stem>.normalized.json
├── <stem>.structured.json
├── <stem>.translated.json
├── <stem>.ja.md
├── <stem>.ja.docx
├── artifacts/
└── manifest.json
```

`.docling.json` は Docling Serve 直後、`.normalized.json` は座標による読み順補正と表・コード整形後、`.structured.json` は VLM patch 後、`.translated.json` は `translate_ja_v2` 翻訳フィールド付与後の JSON とする。

## State と Resume

## .env

`.env` は `python-dotenv` で読み込む。主な環境変数は次の通り。

- `DOCLING_SERVER_URL` または `DOCLING_SERVE_URL`
- `DOCLING_API_KEY` または `DOCLING_SERVE_API_KEY`
- `DOCLING_TIMEOUT_SECONDS`
- `OPENAI_BASE_URL`
- `OPENAI_API_KEY`
- `OPENAI_MODEL`
- `OPENAI_TIMEOUT_SECONDS`

## Atomic Write

JSON と artifact は直接本ファイルへ書かない。必ず一時ファイルへ書き、flush、fsync、`os.replace()` の順に保存する。

## 実装順序

1. Docling Serve で JSON と PNG artifacts を作る。
2. JSON の `prov[].page_no` と `prov[].bbox` を使い、ページ順・上から下・左から右の読み順へ決定論的に補正する。同時に表セル、過剰記号、コードブロックを整形する。
3. VLM/LLM に座標補正済み Docling JSON の要約、bbox、`artifacts/` の page PNG を渡し、`set_label`、`set_level`、`reorder_texts` patch だけを返させて、段組みなど座標だけでは曖昧な見出しと本文の位置を補正する。
4. JSON 各要素へ `translate_ja_v2` フィールドを追加する。
5. 見出し・表タイトルは英日併記、本文は和訳のみで Markdown を作る。
6. pandoc で Markdown を Word docx へ変換する。

## CLI

```bash
python skills/translate-ja-v2/scripts/translate.py \
  --input ./docs/source/source.pdf \
  --output-dir ./docs/source/output-v2 \
  --template ./skills/translate-ja-v2/template.dotx
```

`--skip-docx` は pandoc がない環境で JSON/Markdown まで検証したい場合に使う。`--skip-vlm` は第2段階の VLM 構造補正だけを止めた deterministic test に使う。Normalize の座標補正は実行される。

## テスト

Unit test は外部 API を fake client で mock し、normalization、structure patch、translation metadata、renderer を重点的に検証する。

Integration test は、少なくとも次を確認する。

- `.env` を python-dotenv で読み込める。
- Docling Serve から JSON と PNG が保存される。
- VLM 構造補正 prompt に page PNG を添付できる。
- `.translated.json` に `translate_ja_v2` フィールドが残る。
- Markdown は見出し・表タイトルを英日併記、本文を和訳のみにする。
- pandoc で docx を作れる。
