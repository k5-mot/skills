# translate-ja-v2 実装ガイド

この文書は通常の実装・レビューで最初に読む要約である。詳細が必要な場合は `spec-v2.md` を正とする。

## アーキテクチャ

中心方針は `Document != State != Patch` である。

- `Document`: DoclingDocument または Docling 由来 JSON。文書内容だけを持つ。
- `State`: job、stage、page の進捗、retry、artifact hash、error を持つ。文書本文を入れない。
- `Patch`: normalizer や VLM が文書へ加えた差分と理由を持つ。

Pipeline は `Artifact -> Stage -> Artifact` の連鎖として扱う。Stage は文書処理だけを担当し、resume、state 更新、retry、atomic write、hash validation は共通 `StageRunner` へ集約する。

## プロジェクト構成

新規実装では `pdf2md` パッケージ構成を採用する。

```text
pdf2md/
├── config/
│   ├── default.yaml
│   ├── normalization.yaml
│   └── prompts/
├── src/pdf2md/
│   ├── cli.py
│   ├── pipeline.py
│   ├── core/
│   ├── parsing/
│   ├── normalization/
│   ├── structure/
│   ├── translation/
│   ├── rendering/
│   ├── openai/
│   └── storage/
└── tests/
```

既存リポジトリへ導入するときは、既存 package manager とテスト構成を尊重する。Python 設定やコード規約は `python-dev` スキルに合わせる。

## Workspace

PDF ごとに ULID または UUID の Job ID を発行し、次のように保存する。

```text
workspace/<job-id>/
├── job.json
├── source/input.pdf
├── assets/pages/
├── assets/figures/
├── stages/
│   ├── 01_parse/
│   ├── 02_normalize/
│   ├── 03_structure/
│   ├── 04_translate/
│   └── 05_render/
├── logs/
└── output/
```

各 stage には `state.json` を置き、ページ単位の artifact は `pages/000001.json` のように分ける。Markdown の最終成果物は `output/document.md` に置く。

## State と Resume

Status は `pending`、`running`、`completed`、`failed`、`skipped` を使う。resume 時に `running` のまま残っている state は stale とみなし、原則 `pending` へ戻す。

完了済み page artifact として再利用できる条件は次のすべてである。

```text
state.status == completed
artifact file exists
artifact sha256 == state.sha256
stage config_hash is valid
input_hash is valid
```

Stage 設定が変わった場合は、その stage 以降だけを invalidate する。たとえば normalize 設定変更時は parse を reuse し、normalize 以降を再処理する。

## Atomic Write

JSON と artifact は直接本ファイルへ書かない。必ず一時ファイルへ書き、flush、fsync、`os.replace()` の順に保存する。

## 実装順序

1. core、storage、CLI、State、Resume
2. Parse Stage と Docling integration
3. Normalization framework、基本 rules、Patch
4. Structure Stage と VLM integration
5. Translation Stage と OpenAI integration
6. Markdown rendering
7. Integration、resume、failure recovery tests

各 Phase の終わりに pytest を通し、壊れた中間状態を残さない。

## CLI

最低限の CLI は次の通り。

```bash
pdf2md run input.pdf --target-language ja --output output.md
pdf2md resume <job-id>
pdf2md status <job-id>
pdf2md resume <job-id> --invalidate-from normalize
```

可能なら次も実装する。

```bash
pdf2md resume <job-id> --stage structure --pages 10,11,12
```

## テスト

Unit test は外部 API を mock し、normalization rule、hash validation、state transition、resume 判定、renderer を重点的に検証する。

Integration test は、少なくとも次を確認する。

- 強制終了後に resume できる。
- completed page を不要に再処理しない。
- artifact 破損を hash 検証で検出する。
- config 変更で必要な stage 以降だけ invalidate される。
- 最終 Markdown まで原文が保持される。
