## 1. Utility scaffold and configuration

- [x] 1.1 Create `scripts/translate-ja-v5/` with the agreed `src/`, `workflows/`, `processing/`, `adapters/`, `templates/`, and `tests/` layout; update the root Python tool configuration and verify imports, Ruff, and ty run against the new source tree.
- [x] 1.2 Implement the `translate.py` Typer entry point with only `translate`, `review`, and `register`; verify CLI tests cover help text, required arguments, PDF-only translation input, exclusive register inputs, and unknown subcommands.
- [x] 1.3 Implement environment loading and startup validation for Docling, OpenAI/LiteLLM models, context budgets, LibreTranslate, Langfuse, Qdrant, and Pandoc; verify complete, absent-optional, and partially configured cases with unit tests.
- [x] 1.4 Add `templates/structure-rules.md`, `translation-rules.md`, `review-rules.md`, and the default `template.docx`; verify each workflow loads only its assigned Rules and Pandoc can inspect the bundled reference document.

## 2. Document model and durable state

- [x] 2.1 Implement the `Document`, `Page`, `Block`, `Inline`, and `TableCell` models with the agreed tagged values and JSON serialization; verify round-trip tests cover headings, nested/task lists, quotes, Alerts, code, formulas, tables with spans, figures, footnotes, horizontal rules, links, and inline marks.
- [x] 2.2 Implement Docling-to-v5 normalization while preserving page order, stable refs, bbox, assets, captions, and table geometry; verify fixtures cover every supported Docling label and known wrapper/furniture behavior.
- [x] 2.3 Enforce the unknown-element boundary so content-bearing unknown leaves fail with ref and label while empty or grouping-only unknown nodes continue; verify tests cover key-value/form-like text, missing picture assets, empty elements, and groups with known children.
- [x] 2.4 Implement atomic artifact and `state.json` updates plus non-blocking same-output locking; verify interruption tests never expose partial JSON, missing completed artifacts are rerun, and concurrent execution is rejected without stale locks.
- [x] 2.5 Implement the fixed invalidation table and `--force`; verify Structure, Translation/Backend, Review, and Embedding changes invalidate only their specified ranges, input SHA mismatch errors normally, and force rebuilds from Parse.
- [x] 2.6 Implement compact progress planning and display; verify normal and `--dry-run` output show Stage totals and contiguous Reuse/Run/Retry page ranges without one line per page, and dry-run performs no API call or filesystem write.

## 3. External adapters and budgets

- [x] 3.1 Implement the Docling Serve adapter and raw `parsed.json` persistence; verify schema/page/ref validation and retriable versus non-retriable HTTP failures with mocked responses.
- [x] 3.2 Implement the LiteLLM-facing OpenAI Chat Completions adapter with JSON Schema output, optional image messages, missing-usage tolerance, and no fallback modes; verify contract tests against recorded OpenAI-compatible requests and responses.
- [x] 3.3 Implement the shared retry helper for network failures, 408, 429, and 5xx with exponential backoff capped at three attempts; verify other 4xx responses fail after one attempt.
- [x] 3.4 Implement conservative 50,000-token budgeting, neighbor-context trimming, safe element-boundary splitting, and one context-overflow bisection; verify no generated request exceeds its calculated budget and an indivisible oversized element reports its ref.
- [x] 3.5 Implement the LibreTranslate adapter and protected-fragment handling; verify `--backend libretranslate` changes only translation while URLs, paths, commands, code, identifiers, and Structure/Review behavior remain intact.
- [x] 3.6 Implement optional Python-side Langfuse instrumentation for workflow, Stage, page, Review node, LLM payloads, RAG text, patches, findings, and page media; verify absent settings are no-op, partial settings error, credentials are excluded, and telemetry failure does not fail translation.

## 4. Translation and shared Review

- [x] 4.1 Implement sequential page translation with previous/next source pages as read-only context and current-page-only structured output; verify foreign page IDs are rejected and all chunks must complete before a page is marked complete.
- [x] 4.2 Implement the two-column glossary loader and translation protections; verify required columns, duplicate/error handling, terminology consistency, and non-translation of protected content.
- [x] 4.3 Implement the deterministic quality checker for numbers, units, negation indicators, URLs, paths, commands, identifiers, code, and glossary terms; verify seeded invariant violations become findings without an LLM call.
- [x] 4.4 Implement the shared LangGraph Review graph with Fidelity Critic, Japanese Critic, findings merge, Reviser, and Verifier at `max_concurrency=1`; verify node order, findings-only critics, minimal revision path, no-finding path, one retry, and terminal rejection.
- [x] 4.5 Integrate optional Qdrant evidence into Japanese Critic, Reviser, and Verifier; verify cited text and source metadata are propagated, missing configuration runs without RAG, and configured search failure fails the page.
- [x] 4.6 Assemble the translation workflow from Parse through verified page artifacts; verify a mid-document failure resumes from the failed page and no unverified page can reach Markdown or DOCX.

## 5. Markdown, cover, and DOCX

- [x] 5.1 Implement the deterministic Pandoc-Markdown renderer for all Block and Inline variants; verify snapshot tests preserve ordering, escaping, fenced code, table columns/spans, assets, internal references, footnotes, Alerts, task lists, and inline styles without an LLM call.
- [x] 5.2 Render PDF page 1 at 150 DPI and emit it as a proportionally fitted cover followed by a page break while excluding it from translated blocks; verify a multi-page fixture produces one cover image and begins translated content at PDF page 2.
- [x] 5.3 Implement pre-render validation for unresolved assets/references, control characters, code fences, and table shape; verify invalid Markdown inputs fail before Pandoc invocation with the responsible block ref.
- [x] 5.4 Implement the Pandoc capability check and fixed DOCX command using `template.docx`, TOC, figure/table lists, section numbering, and native numbering; verify missing features fail before processing and generated DOCX receives no post-generation OOXML mutation.
- [x] 5.5 Verify the final translation layout contains only `document.ja.docx` plus the specified `.work/` tree, and confirm the public DOCX opens successfully with the cover, contents, figures, tables, footnotes, and template styles.

## 6. Standalone bilingual Review

- [x] 6.1 Implement parsing and alignment of source English and destination Japanese PDFs into resumable Review units; verify reordered, split, missing, and extra destination sections produce explicit alignment results in `.work-review/aligned.json`.
- [x] 6.2 Reuse the shared Review graph with `review-rules.md` and optional Qdrant evidence to generate severity, location, rationale, citation, and correction proposal; verify translation and standalone paths use the same node logic and remain sequential.
- [x] 6.3 Implement standalone Review state, locking, input-hash rejection, force behavior, and `review.md` rendering; verify interrupted runs reuse completed units and the only public output is the requested Markdown file beside `.work-review/`.

## 7. Qdrant reference registration

- [x] 7.1 Implement PDF, DOCX, Markdown, and UTF-8 text extraction plus recursive hidden/empty-file filtering; verify each supported format produces ordered text units and unsupported or invalid files report actionable errors.
- [x] 7.2 Implement heading/paragraph-aware chunks near 1,000 tokens with 100-token overlap and deterministic source/revision/chunk metadata; verify boundary, overlap, relative-path source, and stable point-ID tests.
- [x] 7.3 Implement direct OpenAI-compatible embedding and Qdrant upsert, complete-new-revision verification, then same-source old-revision deletion; verify failed upsert preserves old points, failed deletion leaves recoverable coexistence, rerun converges, and other sources remain untouched.

## 8. Acceptance and documentation

- [x] 8.1 Add a 50,000-token acceptance fixture with an oversized page, chunked translation, Review, interruption, and Resume; verify all pages complete without over-budget requests or redoing completed pages.
- [x] 8.2 Add seeded accuracy regression cases for omissions, additions, meaning changes, negation, comparison, conditions, numbers, units, URLs, paths, identifiers, and glossary terms; verify every critical defect is detected and the v5 critical-result set is no worse than the retained v4 baseline fixture.
- [x] 8.3 Add end-to-end tests for OpenAI and LibreTranslate translation, standalone Review, Langfuse no-op/failure, Qdrant RAG, register replacement, and Pandoc DOCX output using local fakes where external services are unavailable; verify the complete test suite, Ruff, and ty pass.
- [x] 8.4 Document installation, environment variables, all three CLI commands, output trees, Resume/force rules, Rules separation, Pandoc requirements, optional services, and the fact that configured Langfuse records full document content under `docs/`; verify every documented command matches CLI help.
- [x] 8.5 Run `openspec validate add-translate-ja-v5-utility --strict` and the repository test/lint/typecheck commands, then record the successful verification before requesting implementation review.

## Verification

- `openspec validate add-translate-ja-v5-utility --strict`: pass
- `uv run pytest -q`: 334 passed（実Pandocを有効化）
- `uv run ruff check .`: pass
- `uv run ty check scripts/translate-ja-v5/src scripts/translate-ja-v5/translate.py scripts/translate-ja-v5/tests`: pass
- `uv run ty check .`: v5外の既存`skills/translate-ja-v4/tests/test_pipeline.py:322`に2 diagnostics
