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
