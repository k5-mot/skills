## Context

動機は[proposal.md](./proposal.md)を参照する。既存v4はSkillの実行手順、8段のPipeline、LangGraph checkpoint、manifest、Stage別成果物、Qdrant登録、比較Review、Pandoc後のOOXML操作を同じ配下に持つ。新規v5は既存コードを移植するのではなく、`scripts/translate-ja-v5/`へ独立したPythonユーティリティとして構築する。

設計上の制約は次のとおりである。

- 主対象はローカルで稼働するLiteLLMとvLLMであり、モデルのコンテキスト上限は概算50,000 tokenである。
- 品質順位は翻訳の正確さ、構造保持、実行コスト、見た目の順とする。
- 長文書ではページ単位の再開が必要だが、外部へ公開する翻訳成果物はDOCXだけである。
- LLM呼出しの同時実行数は1である。
- v4の内部形式およびCLIとの互換層は作らない。

## Goals / Non-Goals

**Goals:**

- CLI、workflow、文書処理、外部接続を変更理由ごとに分け、1ファイルへ責務を集中させない。
- 正規化後の文書モデルを小さく保ち、JSONからMarkdownを決定的に生成する。
- Pipelineの状態と成果物を一つの`state.json`で説明できるようにする。
- Reviewの条件分岐だけをLangGraphへ閉じ込め、追跡とResumeの責務を分離する。
- Pandocが直接提供するDOCX機能を使い、OOXML操作を追加しない。

**Non-Goals:**

- 汎用ワークフローエンジン、plugin機構、DI container、抽象Repository、Provider factoryは作らない。
- 内部Stageを個別に起動するCLIは作らない。
- v4のmanifest、LangGraph SQLite checkpoint、8列用語集、独自DOCX後処理を再利用しない。
- PDFの座標どおりのレイアウト再現や、高度なWord組版機能を実装しない。
- 初版ではWordを翻訳入力にしない。

## Decisions

### 1. 配置とモジュール境界

次の構成を採用する。

```text
scripts/translate-ja-v5/
├── translate.py
├── templates/
│   ├── template.docx
│   ├── structure-rules.md
│   ├── translation-rules.md
│   └── review-rules.md
├── src/
│   ├── __init__.py
│   ├── config.py
│   ├── model.py
│   ├── state.py
│   ├── budget.py
│   ├── workflows/
│   │   ├── translate.py
│   │   ├── review.py
│   │   ├── compare.py
│   │   └── register.py
│   ├── processing/
│   │   ├── normalize.py
│   │   ├── structure.py
│   │   ├── translation.py
│   │   ├── quality.py
│   │   └── render.py
│   └── adapters/
│       ├── docling.py
│       ├── llm.py
│       ├── libretranslate.py
│       ├── langfuse.py
│       ├── qdrant.py
│       └── pandoc.py
└── tests/
    ├── test_cli.py
    ├── test_model.py
    ├── test_state.py
    ├── test_budget.py
    ├── test_translation_workflow.py
    ├── test_review_workflow.py
    ├── test_registration_workflow.py
    └── test_render.py
```

依存方向は`translate.py → workflows → processing → model`とし、外部I/Oだけを`adapters`へ向ける。adapterは具象関数として実装し、テストでは関数をmonkeypatchする。抽象基底classやfactoryを作らない。

`providers.py`一つへ外部接続を集約する案は、Docling、LLM、Qdrant、Pandocで変更理由と障害特性が異なりファイルが肥大化するため採用しない。一方、Stageごとに細粒度packageを作る案も移動コストが増えるため採用しない。上記の最大3階層を上限とする。

### 2. CLIは三つの利用目的だけを公開する

入口はrootのPython環境から実行できる一つの`translate.py`とする。

```text
python scripts/translate-ja-v5/translate.py translate \
  --source source.pdf --output-dir output \
  [--backend openai|libretranslate] [--glossary glossary.csv] \
  [--dry-run] [--force]

python scripts/translate-ja-v5/translate.py review \
  --source source-en.pdf --destination translation-ja.pdf \
  --output review.md [--force]

python scripts/translate-ja-v5/translate.py register \
  (--doc-path FILE | --doc-dir DIRECTORY)
```

内部工程の再利用判断をCLI利用者へ漏らさないため、`parse`や`normalize`等の公開サブコマンドは採用しない。用語集はv4形式を引き継がず、必須列`source,target`と任意列`notes`を持つUTF-8 CSVに限定する。

### 3. Pipeline状態は`state.json`、Review分岐はLangGraph

翻訳の上位工程は`parse → normalize → structure → translate → review → markdown → docx`という直線的なartifact処理である。この全体をLangGraph checkpointへ載せると、checkpointと成果物manifestのどちらが正本かを決める追加設計が必要になる。v5では`.work/state.json`だけを正本とし、各artifactを一時ファイルへ完全に書いた後、`os.replace`でartifact、最後にstateの順でatomic更新する。stateが完了を示してもartifactが存在しないかJSONとして読めない場合は、その単位を未完了へ戻す。個別artifactのhash台帳は作らない。

同一出力先の排他は`.work/` directory自身へのOS advisory lockをnon-blockingで取得して実現する。PIDを正本にするlock file方式と異なり、余分な成果物や異常終了後のstale lock掃除を必要としない。`--dry-run`は状態を読むだけで一切書き込まない。

Reviewは「指摘の有無」「Verifier合否」「一度だけの差戻し」という実際の分岐とloopを持つため、ここだけLangGraphを使用する。LangGraph checkpointは使用せず、graph入出力をページ単位artifactとしてstateから管理する。この境界により、graphはReviewロジックを可視化し、stateはprocessをまたぐResumeを担当する。

### 4. LangGraphをPipeline全体へ使わなくてもLangfuseを記録できる

LangfuseのPython SDKによる関数観測とLLM client統合を使い、top-level workflow、内部工程、ページ、Review nodeの順にspanを入れ子化する。LangfuseはLangGraphの有無ではなく、観測対象関数とLLM呼出しを基準にtraceを構成できるため、単純な関数呼出しとの相性は問題にならない。

LiteLLM proxy側のcallbackとPython側計装を併用すると同じLLM呼出しが二重記録されるため、v5の契約はPython側だけとする。設定がない場合はno-op、不完全な場合は開始前エラー、送信障害は警告として主処理を継続する。機密情報は除外するが、利用者の明示要件に従い原文、訳文、prompt、response、RAG本文、修正、指摘およびページ画像は記録する。

### 5. 小さな文書モデルへ早期変換する

Docling固有schemaを全工程へ流さず、raw responseを`parsed.json`へ保存した直後に次の概念へ変換して`normalized.json`へ保存する。

```text
Document
└── pages: Page[]
    ├── number, width, height
    └── blocks: Block[]
        ├── id, order, kind, bbox
        ├── source: Inline[]
        ├── translated: Inline[] | null
        ├── reviewed: Inline[] | null
        ├── level / ordered / checked / language / alert_kind
        ├── asset_path / alt_text / caption
        └── cells: TableCell[]

Inline
├── id, text
├── marks: strong | emphasis | strikethrough | underline | subscript | superscript
└── kind: text | code | link | line_break

TableCell
├── row, column, rowspan, colspan, header
└── source / translated / reviewed: Inline[]
```

`Block.kind`は`paragraph`、`heading`、`list_item`、`blockquote`、`alert`、`code`、`formula`、`table`、`figure`、`footnote`、`horizontal_rule`へ限定する。GitHub Markdownにあるmention、Issue参照、emoji、色表記は通常のtextまたはlinkとして保持する。Markdown escapeはRendererの責務とし、PDFに表示されないHTML commentは復元しない。

Inlineには安定IDを付け、通常textだけを翻訳対象とする。codeとlink URLは保護したまま再結合する。再帰的Inline ASTではなく、marksを持つ平坦なspan列にすることでJSONとRendererを単純にする。

Doclingの未知labelは一律拒否しない。既知の子refを束ねるだけのgroup、空要素、明示的に除外するpage header/footer、Pandocで再生成するdocument indexは処理できる。可視text、画像、表または数式を自身に持つ未知leafだけをrefとlabel付きエラーにし、silent data lossを防ぐ。

### 6. Markdownは決定的Rendererで生成する

`processing/render.py`は文書モデルを順番に走査し、Pandoc Markdownへ直接変換する。見出し、強調、引用、fenced code、link、画像、入れ子list、task list、table、footnote、Alertおよび水平線は文書モデルのtagから出力する。underline等、標準Markdownだけで表せない装飾はPandocのSpan属性と`template.docx`内の対応styleへ写像する。

LLMにMarkdown全体を生成させる案は、構造の欠落、code fence破損、表列数の変化および再実行時の非決定性が増えるため採用しない。Rendererは未閉鎖fence、表列数、存在しないasset、未解決内部参照および制御文字をDOCX生成前に検証する。

### 7. ページ翻訳と50,000 token予算

コンテキスト予算は環境変数`LLM_CONTEXT_TOKENS=50000`、`LLM_OUTPUT_TOKENS=8192`、`LLM_IMAGE_TOKENS=4096`を既定値とする。画像を含まない要求では画像予約を引かない。個別tokenizerを導入せず、prompt、Rules、用語集、RAG、対象本文および前後文脈のUnicode文字数をtoken数の保守的近似として合計する。

翻訳対象ページには前後1ページの原文を読取専用contextとして添付し、response schemaは対象ページのIDだけを許可する。予算超過時は隣接ページcontextを末端から縮め、それでも超える場合は対象ページをBlock境界、次にTableCellやInline境界で分割する。単一保護要素は途中分割しない。APIが実際のcontext超過を返した場合の二分は一度だけに制限し、無限なfallbackを作らない。

ページは全chunkの翻訳とReviewが成功した時点だけで完了とする。これにより、Resumeの再利用単位は利用者が理解しやすいページのまま維持される。

### 8. Review graphは指摘と修正を分離する

翻訳内Reviewとstandalone Reviewは次のgraph builderを共有する。

```text
Deterministic Checker
        ↓
Fidelity Critic
        ↓
Japanese Critic ── Qdrant検索根拠
        ↓
Findings merge
   ├─ 指摘なし ───────────────┐
   └─ 指摘あり → Reviser      │
                         ↓     │
                       Verifier
                    ├─ 合格 → 完了
                    └─ 不合格かつ未再試行
                         → Reviser → Verifier
                    └─ 再び不合格 → 失敗
```

`max_concurrency=1`とし、Fidelity CriticとJapanese Criticも直列に実行する。Checkerは数値、単位、URL、path、identifier、codeおよび用語集の機械的invariantを検査する。二つのCriticはfindingsだけを返し、Reviserだけが訳を変更する。Verifierは原文、元訳、候補訳、全findingsとRAG根拠を比較する。

Qdrant未設定は有効なno-RAG構成である。一方、設定済み接続の検索失敗をno-RAGへfallbackすると、同じ設定でも品質契約が変わるためページを失敗させる。

### 9. Resumeの無効化はモデル依存だけを自動化する

`state.json`はschema version、入力SHA-256、backend、使用model名、ページごとの各工程status、最後のerrorを持つ。artifact pathは固定directoryとページ番号から決定的に導出し、stateへ重複保存しない。モデル変更時の無効化は次の固定表で行う。

| 変更 | 再実行範囲 |
|---|---|
| Structure model | structure以降 |
| Translation modelまたはBackend | translate以降 |
| Review model | review以降 |
| Embedding model | 次回のQdrant検索またはregisterのみ |
| 入力PDF | 通常はエラー、`--force`ならparse以降 |

Rules、用語集、Qdrant collection内容およびスクリプトversionのhash追跡は行わない。これらを自動追跡すると依存関係と移行処理が再びmanifest化するため、反映操作を`--force`一つへ集約する。

### 10. 外部adapterの契約を狭くする

`adapters/llm.py`はOpenAI SDKからLiteLLMの`/v1/chat/completions`だけを呼び、JSON Schema structured outputと画像messageだけを使用する。Responses API、tool calling、provider固有option、usage必須化およびstructured output失敗時の別形式fallbackは実装しない。想定backendはvLLM上のQwen/Gemmaだが、model名は固定しない。

モデル設定は`OPENAI_STRUCTURE_MODEL`、`OPENAI_TRANSLATION_MODEL`、`OPENAI_REVIEW_MODEL`、`OPENAI_EMBEDDING_MODEL`に分離し、`OPENAI_BASE_URL`と`OPENAI_API_KEY`を共有する。比較Reviewは翻訳内Reviewと同じ`OPENAI_REVIEW_MODEL`を使用する。

通信retryは共通の小さな関数で包み、network error、408、429、5xxだけを指数backoffで最大3回試す。各adapter独自のretry policyやCLI調整値は作らない。

### 11. DOCXはPandoc出力を完成品とする

表紙PNGを150 DPI相当で生成し、Markdown先頭へ印刷可能領域に収まる画像と改ページを挿入する。以降はPDF第2ページからの翻訳本文を置く。Pandocは概ね次の固定引数で実行する。

```text
pandoc document.ja.md
  --from markdown
  --to docx+native_numbering
  --standalone
  --reference-doc templates/template.docx
  --toc
  --toc-depth 6
  --list-of-figures
  --list-of-tables
  --number-sections
  --output document.ja.docx
```

開始時検査はPandocの存在と上記で利用するoptionおよびwriter extensionに限定する。version表や互換fallbackは持たない。reference docの本文はPandocに取り込まれないため、`template.docx`にはstyle、余白、用紙、header/footer等の設定だけを同梱する。Pandocで直接得られない章別page numbering等は非目標とする。

Pandocは最終pathへ直接書かず、同じdirectoryの一時DOCXへ出力する。必須package entryとZIP CRCを検証してから`os.replace`で公開DOCXを置換し、失敗時は以前の成果物を保持する。

### 12. Qdrantのrevision置換

`register`はPDFをDoclingまたはPDF text抽出、DOCXをPandoc、Markdownとtextを直接読込みし、見出し・段落境界を優先して約1,000 token、100 token overlapへ分割する。LangChain vector storeを介さず、OpenAI互換Embedding APIとQdrant clientを直接使う。これにより登録だけのための抽象Document層を増やさない。

sourceは単一fileならfile名、directoryなら指定directoryからの相対pathとする。revisionはfile SHA-256、point IDはsource、revision、chunk番号から決定的に生成する。更新時は次の順序を守る。

1. 新revisionの全pointをupsertする。
2. 期待するpoint IDがすべて取得可能であることを検証する。
3. 同じsourceかつ異なるrevisionのpointだけを削除する。

新revision登録失敗時は旧revisionが残る。削除失敗時は新旧が一時共存するが、同じregisterを再実行すれば決定的IDにより重複を増やさず削除へ再到達できる。collection自体や別sourceは変更しない。

## Risks / Trade-offs

- [文字数によるtoken概算が実token数とずれる] → 保守的に1文字1 tokenで見積り、API超過時は一度だけ安全境界で二分する受入テストを置く。
- [表やInlineの変換で構造を失う] → raw Docling JSONを残し、正規化fixtureとMarkdown snapshotで各対応要素を検証する。内容を持つ未知leafは明示エラーにする。
- [表紙を画像化するとDOCX容量が増える] → 第1ページだけを150 DPIへ限定し、追加の全ページ画像埋込みは行わない。
- [Langfuseへ機密文書本文が送られる] → 設定時は全文記録されることを利用者向け文書へ明記し、未設定を既定のno-opにする。認証情報だけは必ず除外する。
- [Qdrant削除失敗中に二つのrevisionが検索される] → registerを失敗終了し、運用者が成功するまで再実行できるidempotent設計にする。
- [PandocだけではPDFの見た目を再現できない] → 見た目を最下位優先とし、templateによるstyleと構造保持だけを受入対象にする。
- [Reviewの直列化で処理時間が伸びる] → ローカルLLMの安定性と翻訳精度を優先し、並列度設定を公開しない。

## Migration Plan

1. v4を変更せず、v5を新しいpathとCLIで追加する。
2. v4 fixtureから代表的なPDFと重大な誤訳例だけをv5の回帰testへ移し、内部形式は移行しない。
3. 50,000 token予算、途中失敗、モデル変更、入力変更、`--force`を含むend-to-end testを通す。
4. v5の利用方法と全文Langfuse記録の注意を`docs/`へ追加する。
5. v5が不適合な場合は新規directoryだけを取り下げ、v4利用へ戻す。v4データの変換やrollback migrationは不要とする。
