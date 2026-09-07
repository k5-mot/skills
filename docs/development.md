# Development Guide

## Setup

```bash
### 1. k5-motスキルをインストールする。
pnpm dlx skills@latest add k5-mot/skills python-dev ts-dev init-project --agent universal -y

### 2. Matt Pocock Agent Skillsをインストールする。
pnpm dlx skills@latest add mattpocock/skills code-review codebase-design diagnosing-bugs domain-modeling grill-me grill-with-docs grilling handoff implement improve-codebase-architecture prototype research resolving-merge-conflicts setup-matt-pocock-skills tdd teach to-spec to-tickets triage wayfinder writing-great-skills --agent universal -y

### 3. graphifyをプロジェクトへインストールする。
uvx --from graphifyy graphify install --project --platform agents

### 4. OpenSpecを初期化する。
pnpm dlx @fission-ai/openspec@latest init --tools agents --profile custom --force --no-animation
```

## Python Dev Conventions

- Prefer Typer over argparse for CLI parsing.
- Prefer pydantic.BaseModel over dataclass for structured data and settings.
- Prefer Playwright over Selenium, Polars over pandas, HTTPX over requests, and marimo over Jupyter Notebook.
- Logger messages must be written in English.
- Logging formats must include the source file, function, and line, for example `%(pathname)s`, `%(funcName)s`, and `%(lineno)d`.
- Use `sys.exit(...)` for CLI termination instead of directly raising `SystemExit`; avoid `os._exit` for normal CLI shutdown.

## translate-ja-v2 Runtime Settings

- Translate（両backend）とReviewの `--max-batch-elements` は既定値 `0` で固定件数制限なし、正数で任意の件数上限を指定する。文字数・推定応答量による動的分割は引き続き有効である。LLM Translate・Reviewは通常retry後の一時的API障害、入力容量超過、不正生成応答で失敗したバッチを二分する。単一要素のAPI障害や認証・設定不備は停止し、後続バッチの件数上限は変更しない。詳細は [実装仕様](../skills/translate-ja-v2/references/spec.md) を参照する。
- Pipeline phases are concrete stage classes in `scripts/translate.py`; `run_pipeline()` owns their execution order. Stage固有処理はprivate methodへ置き、module関数には複数Stageで共有するutilityだけを置く。Translate backendだけは共通の`Translate`基底を`TranslateLLM`と`TranslateLibre`が継承し、`TranslateStage`がCLI指定から一方を選ぶ。
- Each input uses `outputs/<stem>/`; stage files are named `document.json`, `document.normalized.json`, `document.structured.json`, `document.translated.json`, `document.reviewed.json`, `document.ja.md`, and `document.ja.docx`.
- Docling PDF/Word conversion must use `/v1/convert/file/async`; do not call `/v1/convert/file` for PDF-to-JSON conversion.
- Docling requests must always enable table structure, table cell matching, code enrichment, and formula enrichment, using `accurate` table mode.
- Docling async polling logs must include the poll count and status on every poll.
- Normalize must correct text reading order from `prov[].page_no` and `prov[].bbox` before Structure invokes the VLM. `--skip-vlm` skips only the second-stage VLM correction, not coordinate correction.
- Structure resolves the exact page image from `pages[].image.uri` and sends only that image with the page's text JSON. It may correct existing heading levels, headings misdetected from captions, code labels, adjacent code fragments, and table-cell inline-code spans, but never rewrites source text or reading order. A missing URI or file falls back to text-only input, never to an unrelated image. If the page text context exceeds `--context-chars` (50,000 by default), it falls back to adjacent two-element structure checks with the same page image. Clean must remain after Structure so detected code spans are protected from punctuation compaction.
- Translate groups a heading with its subordinate headings and body until a heading at the same or a higher level, batches blocks up to `--batch-chars` source characters (1,500 by default), and batches each table when it fits. Code and protected table cells are excluded. Batch response IDs must exactly match request IDs before translations are applied; empty, malformed, or partial multi-item responses are split at item boundaries and retried. Optional CSV glossary entries are filtered per source text before being passed to the LLM with translation rules.
- ReviewはFidelityとTerminologyの独立Reviewerを使い、両案が異なる要素だけAdjudicatorで裁定する。単一Reviewer経路は持たない。Qdrant batch-query evidenceは`--review-rag`で任意に有効化し、取得本文はpromptだけに使い、成果物にはsource ID、locator、score、提案、最終判断を保存する。
- After pandoc creates a DOCX, DocxStage overrides paragraph-after and following paragraph-before spacing only between directly consecutive headings. It must not insert or remove empty paragraphs or alter the normal spacing from the final heading to body content.
- translate-ja-v2 timeouts, OCR settings, OpenAI retry settings, output token limits, and log level are fixed in `scripts/translate.py`, not environment variables. OpenAI request text and translation batch character limits are CLI options.
