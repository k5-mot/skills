# PDF文書解析・整合・翻訳・Markdown変換パイプライン 仕様書

## 1. 目的

PDF文書を入力として、Doclingによる文書解析、ルールベースの整合処理、VLMによる構造補正、OpenAI APIによる翻訳、Markdownレンダリングまでを一貫して実行するPythonアプリケーションを構築する。

本システムでは以下を重視する。

* Doclingの文書モデルを可能な限りそのまま利用し、独自文書スキーマの保守コストを抑える
* PDF解析時の誤分割、誤検出、不要記号などを決定論的ルールで補正する
* VLMには原文を書き換えさせず、要素の順序・結合・分類などの構造操作のみを担当させる
* 原文を保持したまま翻訳結果を追加する
* 各処理をStageとして分離する
* Stageおよびページ単位で処理状態を永続化する
* 任意の箇所で異常終了しても、処理済みデータを再利用してresumeできる
* 各補正処理について、何が・なぜ変更されたか追跡可能にする
* 同一入力・同一設定から再現性のある成果物を生成する

---

# 2. システム概要

処理フローは以下とする。

```text
PDF
 │
 ▼
[01 Parse]
DoclingによるPDF解析
 │
 ├─ DoclingDocument
 ├─ Page PNG
 └─ Figure/Image
 │
 ▼
[02 Normalize]
決定論的整合処理
 │
 ├─ 記号正規化
 ├─ テーブルセル補正
 ├─ 誤分割結合
 ├─ ハイフネーション修復
 ├─ ログ・レポート結合
 └─ コードブロック判定
 │
 ▼
[03 Structure]
構造・Reading Order補正
 │
 ├─ Heuristic判定
 └─ 必要な場合のみVLM
 │
 ▼
[04 Translate]
OpenAI APIによる翻訳
 │
 ├─ 翻訳対象分類
 ├─ Chunk作成
 ├─ Structured Output
 └─ 翻訳結果検証
 │
 ▼
[05 Render]
Markdown生成
 │
 ▼
output/document.md
```

Stage間では前Stageの成果物を直接破壊的更新しない。

原則として、

```text
前Stage Artifact
    +
Patch / Annotation
    ↓
次Stage Artifact
```

という考え方を採用する。

---

# 3. 基本設計原則

## 3.1 Document / State / Patch を分離する

以下の3概念を明確に分離する。

### Document

Doclingが解析した文書データおよびStage処理後の文書表現。

可能な限りDoclingDocumentまたはDocling由来JSONを使用する。

### State

処理進捗・retry回数・artifact情報・エラー情報など、実行管理に必要な情報。

文書内容とは分離する。

### Patch

NormalizerやVLMが文書に対して行った変更内容。

例：

* replace_text
* merge
* split
* reorder
* group
* semantic_type変更

元データを直接変更した場合でも、変更履歴としてPatchを必ず残せる構造にする。

---

# 4. 技術スタック

最低限以下を使用する。

```text
Python 3.12+
Docling
Pydantic v2
OpenAI Python Client
asyncio
PyYAML
pytest
```

必要に応じて以下を使用してよい。

```text
tenacity
orjson
structlog
typer
rich
```

CLIライブラリは `Typer` を推奨する。

---

# 5. プロジェクト構成

```text
pdf2md/
├── pyproject.toml
├── README.md
├── .env.example
├── .gitignore
│
├── config/
│   ├── default.yaml
│   ├── normalization.yaml
│   └── prompts/
│       ├── reorder_v1.txt
│       └── translate_v1.txt
│
├── src/
│   └── pdf2md/
│       ├── __init__.py
│       ├── cli.py
│       ├── pipeline.py
│       │
│       ├── core/
│       │   ├── state.py
│       │   ├── stage.py
│       │   ├── checkpoint.py
│       │   ├── artifact.py
│       │   ├── patch.py
│       │   ├── hashing.py
│       │   └── exceptions.py
│       │
│       ├── parsing/
│       │   ├── docling_parser.py
│       │   └── page_renderer.py
│       │
│       ├── normalization/
│       │   ├── normalizer.py
│       │   ├── context.py
│       │   └── rules/
│       │       ├── base.py
│       │       ├── ellipsis.py
│       │       ├── table_artifact.py
│       │       ├── whitespace.py
│       │       ├── hyphenation.py
│       │       ├── fragment_merge.py
│       │       ├── log_block.py
│       │       ├── report_block.py
│       │       └── ascii_table.py
│       │
│       ├── structure/
│       │   ├── detector.py
│       │   ├── heuristics.py
│       │   ├── reorder.py
│       │   └── vlm.py
│       │
│       ├── translation/
│       │   ├── translator.py
│       │   ├── chunker.py
│       │   ├── classifier.py
│       │   ├── glossary.py
│       │   └── validator.py
│       │
│       ├── rendering/
│       │   ├── markdown.py
│       │   ├── table.py
│       │   ├── code.py
│       │   └── image.py
│       │
│       ├── openai/
│       │   ├── client.py
│       │   ├── schemas.py
│       │   ├── retry.py
│       │   └── rate_limit.py
│       │
│       └── storage/
│           ├── workspace.py
│           ├── atomic.py
│           └── json_store.py
│
└── tests/
    ├── unit/
    │   ├── core/
    │   ├── normalization/
    │   ├── structure/
    │   ├── translation/
    │   └── rendering/
    │
    ├── integration/
    │   ├── test_docling_pipeline.py
    │   ├── test_resume.py
    │   └── test_end_to_end.py
    │
    └── fixtures/
        ├── pdf/
        ├── docling/
        ├── normalization/
        ├── logs/
        ├── tables/
        └── expected/
```

---

# 6. Workspace構成

PDFごとにJob IDを発行する。

Job IDはULIDまたはUUIDを利用する。

```text
workspace/
└── <job-id>/
    ├── job.json
    │
    ├── source/
    │   └── input.pdf
    │
    ├── assets/
    │   ├── pages/
    │   │   ├── page_000001.png
    │   │   ├── page_000002.png
    │   │   └── ...
    │   └── figures/
    │
    ├── stages/
    │   ├── 01_parse/
    │   │   ├── state.json
    │   │   ├── document.json
    │   │   └── pages/
    │   │       ├── 000001.json
    │   │       └── ...
    │   │
    │   ├── 02_normalize/
    │   │   ├── state.json
    │   │   ├── patches.jsonl
    │   │   └── pages/
    │   │       ├── 000001.json
    │   │       └── ...
    │   │
    │   ├── 03_structure/
    │   │   ├── state.json
    │   │   ├── patches.jsonl
    │   │   └── pages/
    │   │       ├── 000001.json
    │   │       └── ...
    │   │
    │   ├── 04_translate/
    │   │   ├── state.json
    │   │   └── pages/
    │   │       ├── 000001.json
    │   │       └── ...
    │   │
    │   └── 05_render/
    │       ├── state.json
    │       └── document.md
    │
    ├── logs/
    │   ├── pipeline.jsonl
    │   ├── errors.jsonl
    │   └── api.jsonl
    │
    └── output/
        ├── document.md
        └── manifest.json
```

---

# 7. Job State仕様

`job.json` はジョブ全体の状態のみを管理する。

例：

```json
{
  "schema_version": 1,
  "job_id": "01JABCDEF123",
  "status": "running",
  "current_stage": "translate",
  "source": {
    "path": "source/input.pdf",
    "sha256": "..."
  },
  "stages": {
    "parse": "completed",
    "normalize": "completed",
    "structure": "completed",
    "translate": "running",
    "render": "pending"
  },
  "created_at": "2026-08-13T01:00:00+09:00",
  "updated_at": "2026-08-13T01:30:00+09:00"
}
```

---

# 8. Status定義

以下のStatusを使用する。

```python
class Status(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
```

永続化時に `running` の状態でプロセスが終了した場合、resume時には原則として `pending` に戻す。

---

# 9. Stage State仕様

各Stageに `state.json` を作成する。

例：

```json
{
  "stage": "translate",
  "status": "running",
  "config_hash": "...",
  "input_hash": "...",
  "pages": {
    "1": {
      "status": "completed",
      "attempts": 1,
      "artifact": "pages/000001.json",
      "sha256": "..."
    },
    "2": {
      "status": "failed",
      "attempts": 3,
      "error": {
        "type": "APITimeoutError",
        "message": "Request timed out"
      }
    },
    "3": {
      "status": "pending",
      "attempts": 0
    }
  }
}
```

---

# 10. Resume仕様

以下の条件を満たすPage Artifactのみ完了済みとして扱う。

```text
state.status == completed
AND
artifact file exists
AND
artifact sha256 == state.sha256
AND
stage config_hash is valid
AND
input_hash is valid
```

いずれかを満たさない場合、そのページを再処理する。

Resume時には以下を実施する。

```text
1. job.json読込
2. source PDFのhash確認
3. Stage config hash確認
4. stale running stateをpendingへ戻す
5. Artifact存在確認
6. Artifact SHA-256確認
7. 未完了Pageのみ処理
```

---

# 11. Config Hash

Stageの動作に影響する設定はcanonical JSON化し、SHA-256を算出する。

対象例：

```text
model
prompt_version
target_language
normalization settings
VLM settings
chunk size
glossary version
```

Stage設定が変更された場合、そのStage以降はinvalidateする。

例：

```text
normalize config変更

parse       → reuse
normalize   → invalidate
structure   → invalidate
translate   → invalidate
render      → invalidate
```

---

# 12. Atomic Write

JSONや成果物は直接本ファイルに書き込まない。

必ず以下の順序とする。

```text
temporary fileへwrite
↓
flush
↓
fsync
↓
os.replace()
```

例：

```python
def atomic_write_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")

    with tmp.open("w", encoding="utf-8") as f:
        json.dump(
            data,
            f,
            ensure_ascii=False,
            indent=2,
        )
        f.flush()
        os.fsync(f.fileno())

    os.replace(tmp, path)
```

---

# 13. Stageインターフェース

各Stageは共通インターフェースを持つ。

概念例：

```python
class Stage(ABC):
    name: str
    version: str

    @abstractmethod
    async def process_page(
        self,
        context: PageContext,
    ) -> PageArtifact:
        ...
```

Stage内部ではresume制御を実装しない。

resume、state更新、retry、artifact writeは共通 `StageRunner` が担当する。

---

# 14. StageRunner

概念フロー：

```text
PageState確認
   │
   ├─ completed + valid → skip
   │
   └─ pending / failed
         │
         ▼
      running
         │
         ▼
    process_page()
         │
      ┌──┴──┐
      │     │
   success error
      │     │
      ▼     ▼
 artifact failed
 atomic
 write
      │
      ▼
 hash
      │
      ▼
 completed
```

State更新は各重要状態遷移時に永続化する。

---

# 15. Parse Stage

## 15.1 入力

```text
source/input.pdf
```

## 15.2 処理

DoclingでPDFを解析する。

必要な機能：

* OCR
* Layout解析
* Table structure
* Picture extraction
* Bounding box
* Provenance
* Reading order

## 15.3 出力

```text
stages/01_parse/document.json
stages/01_parse/pages/*.json
assets/pages/*.png
assets/figures/*
```

Doclingのdocument JSONを可能な限りそのまま保存する。

独自のCanonical Document Schemaへの全面変換は行わない。

---

# 16. Normalize Stage

Normalizerは決定論的ルールを優先する。

実装形式：

```python
class NormalizationRule(Protocol):
    name: str
    version: str

    def analyze(
        self,
        context: NormalizationContext,
    ) -> list[Patch]:
        ...
```

---

# 17. Ellipsis整合ルール

本文について、ASCII `.` が4個以上連続している場合、原則 `...` に短縮する。

例：

```text
"Hello........ world"
```

↓

```text
"Hello... world"
```

コード・ログ・数式・URL・identifierでは適用しない。

---

# 18. Table Artifactルール

テーブルセルが記号のみで構成されている場合、不要なartifactと判定して空文字へ変換可能とする。

対象例：

```text
...
....
......
------
————
……
```

ただし以下のような意味のある文字列は削除してはならない。

```text
Version...
A-1
1.2.3
```

判定は「セル全文が対象記号のみ」である場合を基本とする。

---

# 19. Fragment Merge

Doclingが本文を意図せず複数要素へ分割したケースを検出し、結合する。

判定要素：

* 同一ページ
* 同一column
* bbox間隔
* x位置
* label
* font/style情報
* 文末記号
* 後続要素が見出しではない
* semantic type

スコア方式として実装してよい。

一定閾値以上なら自動結合する。

---

# 20. Hyphenation Repair

PDF改行由来のハイフネーションを修復する。

例：

```text
inter-
national
```

↓

```text
international
```

ただし本来の複合語を誤って削除しないよう、保守的に判定する。

---

# 21. Log Block Detection

ログとして検出された連続要素を単一ブロックへまとめる。

例：

```text
2026-08-13 10:00:00 INFO Starting
2026-08-13 10:00:01 INFO Connected
2026-08-13 10:00:02 ERROR Failed
```

↓

````markdown
```text
2026-08-13 10:00:00 INFO Starting
2026-08-13 10:00:01 INFO Connected
2026-08-13 10:00:02 ERROR Failed
````

````

内部表現上は即座にMarkdown文字列へ変換せず、

```text
semantic_type = log
group_type = code
````

などのAnnotation/Patchとして保持すること。

---

# 22. Stack Trace対応

以下のような複数要素を1ブロックにまとめる。

```text
Traceback (most recent call last):
  File "app.py", line 10, in run
    start()
RuntimeError: failed
```

開始行だけではなく後続行も状態機械的に取り込む。

---

# 23. Report Block Detection

以下のような診断・レポート形式の連続行をコードブロックとして扱えること。

```text
CPU Usage      : 85%
Memory Usage   : 10.4 GB
Disk Usage     : 71%
Process Count  : 214
```

複数行で同様の構造が継続していることを判定条件に含める。

---

# 24. ASCII Table Detection

以下のような等幅テキスト表もコードブロックまたは専用semantic typeとしてまとめる。

```text
NAME       PID   CPU   MEMORY
python     1234  10%   500M
postgres   2234   5%   800M
```

判定候補：

* 2個以上の連続スペース
* column開始位置の一致
* 2行以上継続
* 文字数・delimiterの類似性

---

# 25. Patch仕様

Patchは少なくとも以下を保持する。

```json
{
  "id": "patch-id",
  "page": 3,
  "processor": "rule",
  "rule": "ellipsis",
  "rule_version": "1",
  "operation": "replace_text",
  "target": "#/texts/31",
  "before": ".......",
  "after": "...",
  "confidence": 1.0
}
```

VLMによるPatch：

```json
{
  "id": "patch-id",
  "page": 12,
  "processor": "vlm",
  "operation": "merge",
  "targets": [
    "#/texts/100",
    "#/texts/101"
  ],
  "confidence": 0.93
}
```

Patchログは原則 `patches.jsonl` に追記する。

---

# 26. Structure Stage

Normalize後にReading Orderおよび文書構造を確認する。

処理順：

```text
Heuristic analysis
      │
      ├─ confidence sufficiently high
      │       ↓
      │     apply
      │
      └─ ambiguous
              ↓
             VLM
```

単純なページはVLMへ送信しない。

---

# 27. VLM責務

VLMには原文を書き換えさせない。

VLMが返却可能な操作を原則以下に限定する。

```text
reorder
merge
group
semantic classification
hierarchy correction
```

例：

```json
{
  "page": 12,
  "reading_order": [
    "p12-e001",
    "p12-e004",
    "p12-e002",
    "p12-e003"
  ]
}
```

または：

```json
{
  "operations": [
    {
      "operation": "merge",
      "ids": ["p12-e004", "p12-e005"]
    },
    {
      "operation": "group_as_code",
      "ids": [
        "p12-e008",
        "p12-e009",
        "p12-e010"
      ]
    }
  ]
}
```

Structured Outputを使用し、自由形式テキストの返却は避ける。

---

# 28. VLM Validation

VLM結果には必ずアプリケーション側Validationを行う。

Reading Orderでは最低限：

```text
元ID集合 == 返却ID集合
重複IDなし
未知IDなし
欠落IDなし
```

を検証する。

不正な場合はretryまたはfailedとする。

---

# 29. Translation Stage

翻訳対象はsemantic typeで決定する。

原則翻訳する：

```text
title
heading
paragraph
list item
caption
footnote
table natural-language cell
```

原則翻訳しない：

```text
code
log
formula
URL
email
file path
identifier
numeric-only value
technical token
```

---

# 30. 翻訳時の原文保持

原文を上書きしない。

概念上：

```json
{
  "source_text": "This is a test.",
  "translated_text": "これはテストです。"
}
```

Doclingモデルへ直接フィールド追加できない場合は、Sidecar Translation Artifactとして管理する。

例：

```json
{
  "translations": {
    "#/texts/31": {
      "source_text": "This is a test.",
      "translated_text": "これはテストです。"
    }
  }
}
```

---

# 31. Translation Chunking

1要素ずつ翻訳せず、意味的まとまりでchunk化する。

例：

```text
section heading
paragraph
paragraph
paragraph
```

を1 chunkとする。

ただし各要素IDは保持する。

入力例：

```json
{
  "context": {
    "document_title": "...",
    "section_title": "Experimental Results"
  },
  "items": [
    {
      "id": "p10-e21",
      "text": "..."
    },
    {
      "id": "p10-e22",
      "text": "..."
    }
  ]
}
```

出力：

```json
{
  "translations": [
    {
      "id": "p10-e21",
      "translated_text": "..."
    },
    {
      "id": "p10-e22",
      "translated_text": "..."
    }
  ]
}
```

IDの追加・削除・変更は禁止する。

---

# 32. OpenAI APIエラー処理

以下をretry対象として扱える構造にする。

```text
timeout
connection error
rate limit
5xx
temporary service failure
```

以下は原則retry回数を限定する。

```text
schema validation failure
malformed response
unexpected IDs
```

設定可能な最大retry回数を設ける。

指数バックオフを使用する。

---

# 33. Translation Validation

最低限以下を検証する。

```text
入力ID集合 == 出力ID集合
重複IDなし
translated_text欠落なし
source_text変更なし
```

必要に応じて以下も検証可能とする。

```text
URL保持
数値保持
単位保持
コード保持
placeholder保持
```

---

# 34. Render Stage

独自Markdown Rendererを実装する。

理由：

* Normalize結果を反映する必要がある
* VLMによる構造補正を反映する必要がある
* 翻訳結果を優先する必要がある
* log/report/code blockを正しく処理する必要がある

要素種別ごとのRendererを分離する。

```text
markdown.py
table.py
code.py
image.py
```

---

# 35. Markdown出力優先順位

テキスト要素については、

```text
translated_text
↓ なければ
source_text
```

を使用する。

---

# 36. Markdownコードブロック

`semantic_type` が以下の場合、原則コードブロックとして出力する。

```text
code
log
report
ascii_table
```

デフォルトlanguageは `text`。

---

# 37. Markdown Table

Doclingが正しくTableとして解析した場合はMarkdown tableを生成する。

ただし複雑なrowspan/colspan等でMarkdown変換による情報欠落が大きい場合は、設定によりHTML Table出力を許可する。

---

# 38. 設定ファイル

`config/default.yaml`

例：

```yaml
pipeline:
  concurrency: 4

source:
  language: auto

translation:
  target_language: ja
  model: ""
  max_retries: 3

structure:
  use_vlm: true
  confidence_threshold: 0.90

render:
  table_format: markdown
  code_language_default: text
```

モデル名はハードコードしない。

---

# 39. Normalization設定

`config/normalization.yaml`

例：

```yaml
ellipsis:
  enabled: true

  paragraph:
    min_run: 4
    replacement: "..."

  table:
    punctuation_only_cell:
      enabled: true

fragment_merge:
  enabled: true
  max_vertical_gap: 12
  same_column_tolerance: 8

hyphenation:
  enabled: true

log_block:
  enabled: true
  min_lines: 2

report_block:
  enabled: true
  min_lines: 2

ascii_table:
  enabled: true
  min_rows: 2
```

---

# 40. CLI仕様

最低限以下を実装する。

## 新規処理

```bash
pdf2md run input.pdf \
  --target-language ja \
  --output output.md
```

処理開始時にJob IDを出力する。

## Resume

```bash
pdf2md resume <job-id>
```

## 状態確認

```bash
pdf2md status <job-id>
```

## Stage以降の再処理

```bash
pdf2md resume <job-id> \
  --invalidate-from normalize
```

## 特定ページ再処理

可能であれば以下も実装する。

```bash
pdf2md resume <job-id> \
  --stage structure \
  --pages 10,11,12
```

---

# 41. Logging

人間向けプレーンログだけでなくJSONLログを保存する。

`pipeline.jsonl` 例：

```json
{
  "timestamp": "...",
  "level": "INFO",
  "job_id": "...",
  "stage": "translate",
  "page": 42,
  "event": "page_completed",
  "duration_ms": 1021
}
```

`errors.jsonl`：

```json
{
  "timestamp": "...",
  "job_id": "...",
  "stage": "structure",
  "page": 55,
  "error_type": "SchemaValidationError",
  "message": "..."
}
```

APIログにAPIキーや全文原文を不用意に保存しない。

---

# 42. 並列処理

Page処理はasync化可能とする。

ただしStage間は原則順序依存とする。

```text
Parse
 ↓
Normalize pages parallel
 ↓
Structure pages parallel
 ↓
Translate chunks/pages parallel
 ↓
Render
```

APIアクセスにはSemaphoreを利用する。

```python
semaphore = asyncio.Semaphore(config.concurrency)
```

Concurrencyは設定値とする。

---

# 43. Page Artifact保存

巨大なdocument JSONを各ページ処理ごとに全書き換えしない。

ページ処理は、

```text
pages/000001.json
pages/000002.json
...
```

として個別に保存する。

これによりresume粒度をページ単位にする。

---

# 44. Artifact Integrity

各Page Artifact保存後にSHA-256を算出する。

Stateへ保存する。

```json
{
  "status": "completed",
  "artifact": "pages/000042.json",
  "sha256": "..."
}
```

Resume時にhashが一致しないArtifactは無効扱いとする。

---

# 45. Stage Dependency

Stage依存関係：

```text
parse
  ↓
normalize
  ↓
structure
  ↓
translate
  ↓
render
```

上流Stageがinvalidateされた場合、すべての下流Stageをinvalidateする。

---

# 46. Rule Versioning

Normalization Ruleは `name` と `version` を持つ。

例：

```python
class EllipsisRule:
    name = "ellipsis"
    version = "1"
```

Rule versionをNormalization Stageのconfig hashに含める。

ルール実装を変更した際に古いartifactを誤利用しないこと。

---

# 47. Prompt Versioning

OpenAI APIで利用するpromptも明示的にversion管理する。

```text
config/prompts/reorder_v1.txt
config/prompts/translate_v1.txt
```

Prompt内容またはversionをconfig hashに含める。

---

# 48. テスト方針

## Unit Test

以下を重点的にテストする。

### Ellipsis

```text
"....."       → "..."
"Hello......" → "Hello..."
```

コード等は変更しない。

### Table Artifact

```text
"......" → ""
"------" → ""
```

ただし：

```text
"Version..." → unchanged
```

### Fragment Merge

結合対象・非対象双方をfixture化する。

### Log Block

複数ログ要素が1ブロックになること。

### Stack Trace

複数要素のTracebackが1ブロックになること。

### Resume

途中状態から処理済みページを再実行しないこと。

### Hash Corruption

Stateがcompletedでもartifact hashが違えば再実行すること。

---

# 49. Golden Test

Normalizerについては以下の形式を推奨する。

```text
tests/fixtures/normalization/
├── ellipsis/
│   ├── 001_input.json
│   └── 001_expected.json
├── fragment_merge/
├── logs/
├── reports/
└── tables/
```

実運用で誤変換が見つかった場合、そのケースをfixtureとして追加してからルール修正を行う。

---

# 50. End-to-End Test

小規模なテストPDFを用意し、

```text
PDF
↓
Parse
↓
Normalize
↓
Structure
↓
Translate
↓
Markdown
```

まで実行する。

OpenAI APIは通常テストではMock可能な構造にする。

---

# 51. OpenAI依存の抽象化

ドメインロジックからOpenAI Clientを直接呼ばない。

例えば：

```python
class StructureModel(Protocol):
    async def analyze(
        self,
        request: StructureRequest,
    ) -> StructureResult:
        ...
```

```python
class TranslationModel(Protocol):
    async def translate(
        self,
        request: TranslationRequest,
    ) -> TranslationResult:
        ...
```

OpenAI実装はInfrastructure層へ配置する。

これにより将来的にモデル・Provider変更を容易にする。

---

# 52. 非機能要件

## 再実行性

任意Stage・ページからresumeできること。

## 冪等性

同一入力・同一設定でcompleted済みArtifactを再生成しないこと。

## 追跡可能性

補正内容をPatchから追跡できること。

## 可観測性

Stage、Page、処理時間、retry、errorをログから確認可能であること。

## 拡張性

Normalization Rule追加時にPipeline本体の変更を不要とすること。

## 保守性

Doclingの内部データ構造を独自モデルへ全面コピーしないこと。

---

# 53. MVP実装範囲

最初の実装では以下を完成させる。

* [ ] CLI
* [ ] Workspace Manager
* [ ] JobState
* [ ] StageState
* [ ] Atomic Write
* [ ] SHA-256 Artifact Validation
* [ ] Resume
* [ ] Parse Stage
* [ ] Docling JSON保存
* [ ] Page PNG生成
* [ ] Normalize Stage
* [ ] Ellipsis Rule
* [ ] Table Artifact Rule
* [ ] Fragment Merge Rule
* [ ] Log Block Rule
* [ ] Patch記録
* [ ] Structure Stage
* [ ] VLM Structured Output
* [ ] Reading Order Validation
* [ ] Translate Stage
* [ ] 翻訳対象Classifier
* [ ] Chunker
* [ ] Structured Translation Output
* [ ] Translation Validation
* [ ] Render Stage
* [ ] Markdown Renderer
* [ ] Resume Integration Test

---

# 54. MVP後の拡張

以下はMVP完成後に追加する。

* [ ] Hyphenation高度化
* [ ] Stack Trace Detector高度化
* [ ] Report Detector
* [ ] ASCII Table Detector
* [ ] Glossary
* [ ] Translation Memory
* [ ] VLM利用ページ自動判定
* [ ] confidence-based fallback
* [ ] Stage parallelization最適化
* [ ] Token / API Cost集計
* [ ] HTML Table fallback
* [ ] Figure Caption linking
* [ ] 数式保護強化
* [ ] CLIでページ指定再実行
* [ ] Workspace cleanup command

---

# 55. 実装順序

Codexは以下の順序で実装すること。

```text
Phase 1
core/
storage/
CLI
State / Resume

Phase 2
Parse Stage
Docling integration

Phase 3
Normalization framework
基本Rules
Patch

Phase 4
Structure Stage
VLM integration

Phase 5
Translation Stage
OpenAI integration

Phase 6
Markdown rendering

Phase 7
Integration tests
Resume tests
Failure recovery tests
```

最初から全Stageを一括実装せず、各Phase終了時にpytestが通る状態を維持する。

---

# 56. Codexへの実装上の制約

以下を遵守すること。

1. Doclingの文書モデルを無意味に独自Pydanticモデルへコピーしない。
2. 文書内容と実行Stateを同一モデルにしない。
3. Normalizer Rule内で直接ファイルI/Oを行わない。
4. Stage内にresume判定を分散させない。
5. API呼び出しコードをTranslation/Structureのドメインロジックへ直書きしない。
6. 原文を翻訳結果で上書きしない。
7. VLMに全文を再生成させない。
8. VLM結果をValidationなしで適用しない。
9. Artifactを非atomicに保存しない。
10. completedのStateだけを信用せずartifact存在・hashも確認する。
11. モデル名・prompt・閾値をハードコードしない。
12. Rule追加のためにNormalizer本体の大規模修正を必要としない構造にする。
13. 外部APIを使うUnit TestはMockできるようにする。
14. 既存のStage Artifactは原則immutableとして扱う。
15. 障害発生時に既に完了したページを失わない。

---

# 57. 完了条件

MVPは以下を満たした時点で完了とする。

1. 任意のPDFをCLIから投入できる。
2. Docling解析結果とPage PNGが保存される。
3. Normalization Ruleがページ単位で適用される。
4. 各補正内容がPatchとして追跡できる。
5. VLMによりReading Orderを補正できる。
6. OpenAI APIで翻訳できる。
7. Markdownを生成できる。
8. 任意Stageで強制終了後、resumeできる。
9. completed済みページをresume時に再処理しない。
10. 壊れたArtifactはhash検証で検出し再処理する。
11. 設定変更時に必要なStage以降のみinvalidateされる。
12. 原文が最終成果物生成まで保持される。
13. Unit TestおよびIntegration Testが成功する。

---

# 58. 最重要アーキテクチャ方針

本システムの中心思想は以下とする。

```text
DoclingDocument
      │
      ├── Stateではない
      │
      ├── Patchではない
      │
      ▼
Document Artifact

State
      │
      └── 実行状況のみ管理

Patch
      │
      └── 文書への変更理由と差分のみ管理
```

すなわち、

```text
Document ≠ State ≠ Patch
```

を維持する。

Pipelineは、

```text
Artifact
   ↓
Stage
   ↓
Artifact
```

の連鎖として扱い、

```text
StageRunner
```

がState、retry、resume、atomic write、hash validationを横断的に担当する。

この構造を崩さずに実装すること。
