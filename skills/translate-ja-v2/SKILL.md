---
name: translate-ja-v2
description: PDF文書をDoclingで解析し、ページ単位State、Patch、resume、VLM構造補正、OpenAI翻訳、Markdownレンダリングを備えたSPEC v2準拠の日本語翻訳パイプラインを設計・実装・レビューする。Use when Codex builds or revises a Python pdf2md/translate-ja-v2 pipeline, migrates the legacy translate-ja runner to the SPEC v2 architecture, adds stage/state/resume/hash validation, or reviews whether an implementation follows SPEC v2.
---

# translate-ja-v2

SPEC v2 準拠の PDF 解析・整合・日本語翻訳・Markdown 生成パイプラインを作るためのスキル。既存 `translate-ja` の runner 型実装を直接拡張するのではなく、`pdf2md` パッケージ、Workspace、Job/Stage State、Page Artifact、Patch を分離した v2 アーキテクチャとして扱う。

## 最初に読むもの

- 変更やレビューを始める前に [implementation-guide.md](references/implementation-guide.md) を読む。
- Stage 詳細、Normalizer rule、VLM、翻訳、Markdown renderer を実装するときは [stages.md](references/stages.md) を読む。
- 細かな仕様判断や受け入れ条件の確認が必要なときだけ [spec-v2.md](references/spec-v2.md) を読む。
- Python コードを書く、CLI を作る、テスト・lint を整える場合は、利用可能なら `python-dev` も併用し、docstring、logger、pytest、Ruff の方針を合わせる。

## 実装方針

1. `Document`、`State`、`Patch` を混ぜない。
2. Stage 内に resume 判定を分散させず、`StageRunner` が state、retry、atomic write、hash validation を横断的に担当する。
3. Artifact は直接上書きせず、一時ファイル、flush、fsync、`os.replace()` で保存する。
4. `completed` state だけを信用せず、artifact の存在、SHA-256、input hash、config hash を必ず検証する。
5. 原文を翻訳結果で上書きせず、翻訳結果は annotation または別フィールドとして追加する。
6. VLM には全文再生成をさせず、順序、結合、分類、grouping などの構造操作だけを返させる。
7. 外部 API を使う単体テストは mock できる境界に分離する。
8. 各 Phase 終了時点で pytest が通る状態を維持する。

## 推奨ワークフロー

新規実装では次の順に進める。

```text
Phase 1: core / storage / CLI / State / Resume
Phase 2: Parse Stage / Docling integration
Phase 3: Normalization framework / basic rules / Patch
Phase 4: Structure Stage / VLM integration
Phase 5: Translation Stage / OpenAI integration
Phase 6: Markdown rendering
Phase 7: Integration, resume, failure recovery tests
```

途中でユーザーが大きな範囲を依頼した場合も、Phase 単位に区切って実装し、各 Phase の終わりに検証する。

## CLI の外部仕様

最低限、次を提供する。

```bash
pdf2md run input.pdf --target-language ja --output output.md
pdf2md resume <job-id>
pdf2md status <job-id>
pdf2md resume <job-id> --invalidate-from normalize
```

可能であれば、特定ページ再処理も提供する。

```bash
pdf2md resume <job-id> --stage structure --pages 10,11,12
```

## 完了判断

MVP は、任意 PDF の投入、Docling JSON と Page PNG の保存、ページ単位 normalization、Patch 追跡、VLM reading order 補正、OpenAI 翻訳、Markdown 生成、resume、hash 破損検出、config 変更時の downstream invalidate、Unit/Integration test 成功を満たした時点で完了とする。
