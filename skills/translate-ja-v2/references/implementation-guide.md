# translate-ja-v2 実装ガイド

この文書は通常の実装・レビューで最初に読む要約である。現在の正本実装は `scripts/translate.py` である。詳細が必要な場合は `spec-v2.md` を参照するが、エージェントスキルとしては単一スクリプトを優先し、無駄な wrapper や package 階層を増やさない。

## アーキテクチャ

中心方針は `Document != State != Patch` である。

- `Document`: DoclingDocument または Docling 由来 JSON。文書内容だけを持つ。
- `State`: job、stage、page の進捗、retry、artifact hash、error を持つ。文書本文を入れない。
- `Patch`: normalizer や VLM が文書へ加えた差分と理由を持つ。

Pipeline は `Artifact -> Stage -> Artifact` の連鎖として扱う。現行 v2 skill では `ParseStage`、`NormalizeStage`、`StructureStage`、`TranslateStage`、`RenderStage`、`DocxStage` の具体クラスが各境界を担当し、`run_pipeline()` が順番に `run()` を呼ぶ。共通基底クラス、factory、汎用 `StageRunner` は、差し替え可能な実装が必要になるまで追加しない。

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
outputs/<stem>/
├── document.json
├── document.normalized.json
├── document.structured.json
├── document.translated.json
├── document.reviewed.json
├── document.ja.md
├── document.ja.docx
├── artifacts/
└── manifest.json
```

`document.json` は Docling Serve 直後、`document.normalized.json` は座標による読み順補正と表・コード整形後、`document.structured.json` は VLM patch 後、`document.translated.json` は `translate_ja_v2` 翻訳フィールド付与後の JSON とする。

## State と Resume

`manifest.json` を進捗の正本とし、各工程について状態、入力 hash、設定 hash、出力 hash を保存する。既存成果物は hash が完了記録と一致するときだけ再利用する。入力や設定が変化した工程は再実行し、その成果物を入力にする後続工程も自然に再実行される。

- `StructureStage`: 成功したページ内の text ref ごとに完了状態を保存し、未完了要素を含む最初のページから再開する。
- `TranslateStage`: 本文、見出し、表タイトル、表セルの ID ごとに完了状態を保存し、未完了要素から再開する。
- `ReviewStage`: レビュー対象 ID ごとに完了状態を保存し、未完了要素から再開する。
- `ParseStage`、`NormalizeStage`、`RenderStage`、`DocxStage`: 工程単位の完了状態だけを保存する。

部分成果物は各工程の通常の出力 JSON に atomic write する。専用の checkpoint ファイルや状態データベースは追加しない。

## .env

`.env` は `python-dotenv` で読み込む。主な環境変数は次の通り。

- `DOCLING_SERVER_URL` または `DOCLING_SERVE_URL`
- `DOCLING_API_KEY` または `DOCLING_SERVE_API_KEY`
- `OPENAI_BASE_URL`
- `OPENAI_API_KEY`
- `OPENAI_MODEL`

timeout、OCR、OpenAI retry、ログレベルは `scripts/translate.py` 内の固定値とする。

## Atomic Write

JSON と artifact は直接本ファイルへ書かない。必ず一時ファイルへ書き、flush、fsync、`os.replace()` の順に保存する。

## 実装順序

1. Docling Serve で JSON と PNG artifacts を作る。
2. JSON の `prov[].page_no` と `prov[].bbox` を使い、ページ順・上から下・左から右の読み順へ決定論的に補正する。同時に表セル、過剰記号、コードブロックを整形する。
3. VLM/LLM に座標補正済み Docling JSON の要約、bbox、`pages[].image.uri` が指す page PNG を渡し、`set_label`、`set_level`、`reorder_texts` patch だけを返させて、段組みなど座標だけでは曖昧な見出しと本文の位置を補正する。URIをファイル名から推測せず、URIや画像がない場合はテキストだけを渡す。
4. JSON 各要素へ `translate_ja_v2` フィールドを追加する。
5. 見出し・表タイトルは英日併記、本文は和訳のみで Markdown を作る。
6. pandoc で Markdown を Word docx へ変換する。

## CLI

```bash
python skills/translate-ja-v2/scripts/translate.py \
  --input ./docs/source/source.pdf \
  --output-dir ./outputs/source \
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
