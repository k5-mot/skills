# translate-ja-v5

`translate-ja-v5`は、英語PDFを日本語DOCXへ変換し、既存翻訳の比較ReviewとReview用参照文書の登録も行うPythonユーティリティです。Agent Skillではありません。

品質判断は、翻訳の正確さ、構造保持、実行コスト、見た目の順で優先します。v4のCLI、中間JSON、manifest、8列用語集との互換性はありません。

## 必要な環境

Python依存関係はrepository rootで同期します。

```bash
uv sync --all-groups
```

翻訳には次が必要です。

- Docling Serve
- LiteLLM経由で到達できるOpenAI互換`/v1/chat/completions`
- JSON Schema structured outputと画像messageに対応するvLLM model
- `--list-of-figures`、`--list-of-tables`、`docx+native_numbering`に対応するPandoc

想定modelはQwen/Gemma系ですが、model名は固定していません。Word入力は初版対象外です。JSON Schemaをsystem promptにも明記し、JSON objectのほか、local modelが返す外側の`json`code fenceも受理します。先頭JSON後の追加説明は出力に使用しません。非JSONまたは必須キー不足の応答は修正promptで一度だけ再生成します。翻訳は各IDを独立して扱い、前後に続く文の断片でも補完やID間の移動・統合を禁止します。翻訳JSONのID集合、空文字、保護placeholderに違反した応答も、違反理由を示して一度だけ再生成します。

## 環境変数

| 変数 | 用途 | 必須条件 |
|---|---|---|
| `DOCLING_URL` | Docling Serve URL | `translate` |
| `DOCLING_API_KEY` | Docling認証 | 接続先が要求する場合 |
| `OPENAI_BASE_URL` | LiteLLMのOpenAI互換base URL | 全command |
| `OPENAI_API_KEY` | LiteLLM認証 | 全command |
| `OPENAI_STRUCTURE_MODEL` | 文書構造判定 | `translate` |
| `OPENAI_TRANSLATION_MODEL` | OpenAI backendの翻訳 | `translate --backend openai` |
| `OPENAI_REVIEW_MODEL` | 翻訳内・standalone Review | `translate`、`review` |
| `OPENAI_EMBEDDING_MODEL` | Qdrant検索・登録 | Qdrant使用時、`register` |
| `LIBRETRANSLATE_URL` | LibreTranslate URL | `--backend libretranslate` |
| `LIBRETRANSLATE_API_KEY` | LibreTranslate認証 | 接続先が要求する場合 |
| `QDRANT_URL` | Qdrant URL | Qdrant使用時、`register` |
| `QDRANT_API_KEY` | Qdrant認証 | 接続先が要求する場合 |
| `QDRANT_COLLECTION` | 既存Review collection | Qdrant使用時、`register` |
| `LANGFUSE_PUBLIC_KEY` | Langfuse public key | 任意、secret keyと対で指定 |
| `LANGFUSE_SECRET_KEY` | Langfuse secret key | 任意、public keyと対で指定 |
| `LANGFUSE_BASE_URL` | self-hosted Langfuse URL | 任意 |
| `LLM_CONTEXT_TOKENS` | 総context予算 | 任意、既定`50000` |
| `LLM_OUTPUT_TOKENS` | 出力予約 | 任意、既定`8192` |
| `LLM_IMAGE_TOKENS` | 画像予約 | 任意、既定`4096` |

Langfuseの認証情報が設定されている場合、原文、訳文、Rules、prompt、response、RAG本文、修正、Review指摘、ページ画像を含む文書内容全体を記録します。機密文書では、Langfuseの保存先とアクセス制御を確認するか、認証情報を設定せずに実行してください。認証情報自体はtraceへ記録しません。

## 翻訳

```bash
uv run python scripts/translate-ja-v5/translate.py translate \
  --source source.pdf \
  --output-dir output \
  --backend openai
```

LibreTranslateへ翻訳だけを切り替える場合は`--backend libretranslate`を指定します。StructureとReviewは引き続きOpenAI互換APIを使用します。

用語集は`source,target`必須列と`notes`任意列を持つUTF-8 CSVです。

```bash
uv run python scripts/translate-ja-v5/translate.py translate \
  --source source.pdf \
  --output-dir output \
  --glossary glossary.csv
```

成果物は入力stemごとに配置されます。

```text
output/<source-stem>/
├── document.ja.docx
└── .work/
    ├── state.json
    ├── parsed.json
    ├── normalized.json
    ├── structured/
    ├── translated/
    ├── reviewed/
    └── document.ja.md
```

PDF第1ページは150 DPI相当の表紙画像としてDOCX第1ページへ入り、翻訳本文はPDF第2ページから始まります。DOCXのstyle、余白、用紙、header/footerは`scripts/translate-ja-v5/templates/template.docx`で管理します。Pandoc生成後のOOXML書換えは行いません。

通常の再実行は`.work/state.json`を読み、完了済みページを再利用します。入力PDFが変わった場合は誤Resumeを避けるためエラーになります。全工程をやり直す場合は`--force`を指定します。

完了状態に対応する内部JSONが欠損または破損している場合は、その成果物と必要な後工程だけを再実行します。比較ReviewのPDF抽出結果と対応付けも再利用されます。

```bash
uv run python scripts/translate-ja-v5/translate.py translate \
  --source source.pdf --output-dir output --force
```

APIやfileを変更せず、再利用・実行予定だけを見る場合は`--dry-run`を使用します。

```bash
uv run python scripts/translate-ja-v5/translate.py translate \
  --source source.pdf --output-dir output --dry-run
```

## Rules

同梱Rulesは用途ごとに完全に分離されています。

- `templates/structure-rules.md`: Structureだけ
- `templates/translation-rules.md`: Translationだけ
- `templates/review-rules.md`: 翻訳内Reviewとstandalone Reviewだけ

付録見出しを翻訳しない、英語と日本語を併記する、といった例外は`translation-rules.md`へ追加します。同じ例外をReviewでも許容する場合は、対応する規則を`review-rules.md`にも明記します。Rulesや用語集の変更は自動的なResume無効化対象ではないため、既存成果物へ反映する場合は`--force`を使用します。

全大文字という理由だけでは本文や見出しを保護しません。URL、path、command option、コード形式の識別子は機械的に保護し、略語や製品名はTranslation RulesとReviewで維持します。複数語の英語原文がそのまま訳文へ返された場合は未翻訳としてReview対象になります。

## Standalone Review

```bash
uv run python scripts/translate-ja-v5/translate.py review \
  --source source-en.pdf \
  --destination translation-ja.pdf \
  --output review.md
```

公開成果物は指定した`review.md`だけです。Resume用成果物は同じdirectoryの`.work-review/`へ保存されます。

```text
<output-directory>/
├── review.md
└── .work-review/
    ├── state.json
    ├── source/
    ├── destination/
    ├── aligned.json
    └── reviewed/
```

Reviewは決定的検査、Fidelity Critic、Japanese Critic、Reviser、Verifierを直列実行します。CriticとVerifierの指摘応答は最大8件かつ2,048 tokenに制限し、Reviserには通常の`LLM_OUTPUT_TOKENS`を使います。原文自体が文の断片なら主節を推測して補完せず、断片であることだけを自然さの問題にはしません。Verifierが不合格にした場合は一度だけ再修正し、再び不合格なら処理を失敗させます。失敗messageにはInline IDと末尾3件までの指摘理由を含めます。Qdrantが設定されていればJapanese Criticが参照本文とsource metadataを検索し、根拠をReviserとVerifierにも渡します。

## Review参照文書の登録

単一fileまたはdirectoryのどちらか一方を指定します。

```bash
uv run python scripts/translate-ja-v5/translate.py register --doc-path reference.pdf
uv run python scripts/translate-ja-v5/translate.py register --doc-dir references/
```

対応形式はPDF、DOCX、Markdown、UTF-8 textです。directoryは再帰探索され、隠しfileと空fileは無視されます。同じsourceが更新された場合は、新revisionをすべてupsertして取得確認した後、同じsourceの旧revisionだけを削除します。新revision登録に失敗した場合は旧revisionを残します。

Docling ServeへはPDFを最大10ページずつ送り、結果の参照、ページ番号、asset URIを全体文書用に再構成します。非同期完了statusは、現行の`task_status`と旧形式の`status`の両方に対応します。実行中は`Docling: chunk 1/36 pages 1-10 started`や`Docling: started`のように、チャンク進捗とstatus変化を表示します。Structure、Translate、Reviewは実行対象ページの開始・完了を表示し、LiteLLM呼出しは`LLM: translations attempt 1/2 started`のようにschema名と構造化応答の再生成状況を表示します。Docling Serve、LiteLLM、LibreTranslate、Qdrantへの通信障害、408、429、5xxは最大3回まで指数backoffで再試行します。最終DOCXは一時packageの整合性を確認してからatomic置換するため、Pandoc失敗時に既存DOCXを上書きしません。

## 制約

- PDFの座標どおりの見た目は再現しません。
- 章・付録ごとのpage number再開始、章別の動的header/footer、高度なWord相互参照は対象外です。
- Rules、用語集、Qdrant内容、script変更は自動無効化しません。必要時は`--force`を使用します。
- Qdrantを設定した状態で検索が失敗した場合、根拠なしReviewへ切り替えず対象処理を失敗させます。

v3/v4から維持した機能、意図的に採用しない機能、未移行機能は[機能差分監査](./translate-ja-v5-compatibility-audit.md)に記録しています。
