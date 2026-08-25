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

- Pipeline phases are concrete `ParseStage`, `NormalizeStage`, `StructureStage`, `TranslateStage`, `RenderStage`, and `DocxStage` classes in `scripts/translate.py`; `run_pipeline()` owns their execution order. Do not add a common stage base class, factory, or generic runner unless interchangeable implementations require one.
- Docling PDF/Word conversion must use `/v1/convert/file/async`; do not call `/v1/convert/file` for PDF-to-JSON conversion.
- Docling requests must always enable table structure, table cell matching, code enrichment, and formula enrichment, using `accurate` table mode.
- Docling async polling logs must include the poll count and status on every poll.
- Normalize must correct text reading order from `prov[].page_no` and `prov[].bbox` before Structure invokes the VLM. `--skip-vlm` skips only the second-stage VLM correction, not coordinate correction.
- Structure sends one page image and that page's text JSON to the VLM first. If the page text context exceeds 50,000 characters, it falls back to adjacent two-element merge checks, then all-pairs two-element order checks with the page image.
- Translate keeps one Docling element per LLM request. Optional CSV glossary entries are filtered per source text before being passed to the LLM with translation rules.
- translate-ja-v2 timeouts, OCR settings, OpenAI retry settings, OpenAI text context limit, VLM image limit, and log level are fixed constants in `scripts/translate.py`, not environment variables.
