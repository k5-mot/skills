## Purpose

英語PDFを中断から安全に再開できる工程で日本語へ翻訳し、意味の正確さと論理構造を検証したDOCXだけを利用者向け成果物として生成する。

## ADDED Requirements

### Requirement: 一括翻訳CLI
ユーティリティは`translate`サブコマンドでPDFの解析、正規化、構造判定、翻訳、Review、Markdown生成、DOCX生成を一括実行しなければならない（SHALL）。初版はPDF入力だけを受け付け、Word入力および内部Stage単独実行を公開してはならない（SHALL NOT）。

#### Scenario: PDFを翻訳する
- **WHEN** 利用者が`translate --source source.pdf --output-dir output`を実行する
- **THEN** `output/source/document.ja.docx`が生成される
- **THEN** 公開成果物としてMarkdownや状態ファイルを要求せずにDOCXを利用できる

#### Scenario: 対象外の入力を拒否する
- **WHEN** 利用者がWord文書を翻訳入力として指定する
- **THEN** ユーティリティは対応形式がPDFだけであることを示して終了する

### Requirement: 品質優先順位
翻訳判断は、翻訳の正確さ、構造保持、実行コスト、見た目の順で優先しなければならない（SHALL）。数値、単位、否定、比較、条件、因果関係、URL、パス、コマンド、識別子および製品名を欠落または改変してはならず、原文にない内容を追加してはならない（SHALL NOT）。

#### Scenario: 意味と見た目が競合する
- **WHEN** 原文の意味を正確に表す訳と原PDFの行折り返しを再現する訳が両立しない
- **THEN** 意味を正確に表す訳を採用する

#### Scenario: 重大な改変を検出する
- **WHEN** Review対象へ数値、否定、条件、意味の欠落または原文にない追加を意図的に混入する
- **THEN** 品質ゲートはその改変を検出して未修正の訳を拒否する

### Requirement: 翻訳対象と外部ルール
既定では、タイトル、見出し、段落、リスト項目、表題、表セル、図題、脚注、付録見出しおよび付録本文を翻訳対象にしなければならない（SHALL）。コード、数式、URL、パス、コマンド、識別子および製品名は保護しなければならない（SHALL）。`templates/structure-rules.md`は構造判定だけ、`templates/translation-rules.md`は翻訳だけ、`templates/review-rules.md`はReviewだけへ適用しなければならない（SHALL）。

#### Scenario: 翻訳例外を追加する
- **WHEN** `translation-rules.md`に付録見出しを翻訳しない規則または英語と日本語の併記規則が記載されている
- **THEN** 翻訳工程は保護対象と出力範囲の安全契約を維持したまま、その規則を対象訳へ適用する

#### Scenario: Reviewにも同じ例外が必要になる
- **WHEN** 翻訳規則だけに翻訳例外があり、対応する許容規則が`review-rules.md`にない
- **THEN** Review工程は翻訳規則を暗黙には参照せず、独立したReview規則に基づいて判定する

### Requirement: 用語集
利用者はv4と互換性を持たない簡潔な用語集を任意指定できなければならない（SHALL）。用語集を指定した場合、翻訳とReviewは同じ対応語を使用しなければならない（SHALL）。

#### Scenario: 用語集を適用する
- **WHEN** `source`と`target`を持つUTF-8 CSV用語集が指定される
- **THEN** 翻訳とReviewは該当語へ指定訳を一貫して適用する

### Requirement: 表紙の転記
入力PDFの第1ページを表紙とみなし、150 DPI相当の画像としてDOCX第1ページの印刷可能領域へ縦横比を保って配置しなければならない（SHALL）。表紙ページは翻訳本文から除外しなければならない（SHALL）。

#### Scenario: 表紙付きPDFを処理する
- **WHEN** 複数ページのPDFを翻訳する
- **THEN** DOCXの第1ページにPDF第1ページの画像が配置される
- **THEN** PDF第2ページ以降だけが翻訳本文として表紙後の改ページから出力される

### Requirement: 構造を保持する中間表現
解析結果は見出し、段落、リスト、引用、Alert、コード、数式、表、図、脚注、水平線、および段落内装飾を区別できなければならない（SHALL）。見出し階層、リスト階層とチェック状態、リンク、図表題、表の行列結合、コード言語、ページおよび原文位置を、MarkdownとDOCXの生成に必要な範囲で保持しなければならない（SHALL）。

#### Scenario: JSONからMarkdownを生成する
- **WHEN** 正規化済み文書に見出し、入れ子リスト、表、脚注、リンクおよび強調が含まれる
- **THEN** LLMを追加呼び出しせず、同じ入力から常に同じ意味構造のMarkdownが生成される

#### Scenario: 内容を持つ未知の要素を受信する
- **WHEN** Doclingが既知要素の子を束ねるだけではなく、可視テキスト、画像、表または数式を自身に持つ未知のleaf要素を返す
- **THEN** Parseは要素のrefとlabelを示して失敗し、内容を黙って本文へ変換または破棄しない

#### Scenario: 内容を持たない未知のgroupを受信する
- **WHEN** 未知のgroupが既知の子要素を束ねるだけで、自身には可視内容を持たない
- **THEN** Parseは既知の子要素を処理して継続する

### Requirement: ページ文脈とコンテキスト予算
ページ翻訳は対象ページだけを出力し、可能な場合は前後1ページの原文を読取専用文脈として使用しなければならない（SHALL）。既定の総コンテキスト予算は概算50,000 tokenとし、出力用に8,192 token、画像がある要求では画像用に4,096 tokenを予約しなければならない（SHALL）。tokenizerに依存せず、1 Unicode文字を約1 tokenとみなす保守的な概算を使用できる（SHALL）。

#### Scenario: 前後文脈が予算を超える
- **WHEN** 対象ページと前後ページの合計が入力予算へ収まらない
- **THEN** 前後ページの文脈を先に短縮し、それでも収まらなければ対象ページをDocling要素境界で分割する
- **THEN** 全分割結果が成功するまで対象ページを完了扱いにしない

#### Scenario: 単一要素が予算を超える
- **WHEN** これ以上安全に分割できない単一要素が入力予算へ収まらない
- **THEN** ユーティリティは対象refを示して失敗する

#### Scenario: APIがコンテキスト超過を返す
- **WHEN** 概算内の要求にAPIがコンテキスト超過を返す
- **THEN** 対象範囲を一度だけ二分して再実行し、同じ原因が続く場合は失敗する

### Requirement: 複数観点Review品質ゲート
翻訳済みページは、決定的検査、忠実性の批評、日本語品質の批評、必要時の最小修正、最終検証を順番に通過しなければならない（SHALL）。批評は訳文を直接書き換えず、修正工程だけが指摘に基づいて必要最小限の変更を行わなければならない（SHALL）。全LLM処理の同時実行数は1でなければならない（SHALL）。

#### Scenario: 初回検証が不合格になる
- **WHEN** 最終検証が修正版を不合格と判定する
- **THEN** 指摘を修正工程へ一度だけ戻して再検証する

#### Scenario: 再検証も不合格になる
- **WHEN** 2回目の最終検証も不合格になる
- **THEN** ページを未完了のままPipelineを失敗させ、検証されていない訳文をDOCXへ含めない

### Requirement: ページ単位Resume
ユーティリティはatomic更新する`.work/state.json`をResumeの正本とし、完了済みページの内部成果物だけを再利用しなければならない（SHALL）。同じ出力先への並行実行を拒否しなければならない（SHALL）。入力PDFのSHA-256が状態と異なる場合は通常実行を拒否しなければならない（SHALL）。

#### Scenario: 中断後に再実行する
- **WHEN** 一部ページの翻訳とReviewが完了した後に処理が中断し、同じ入力と設定で再実行される
- **THEN** 完了済みページを再利用し、最初の未完了ページから処理を続ける

#### Scenario: モデルを変更する
- **WHEN** Structure、TranslationまたはReviewのモデル名が前回状態から変わる
- **THEN** 変更モデルへ依存する工程とその後続工程だけを再実行する

#### Scenario: 入力PDFを変更する
- **WHEN** 同じ出力先で入力PDFのSHA-256が変わり、`--force`が指定されていない
- **THEN** ユーティリティはResumeせず入力変更エラーで終了する

#### Scenario: 強制再実行する
- **WHEN** `--force`が指定される
- **THEN** 入力変更の有無にかかわらず既存内部成果物を再利用せず、Parseから全工程を再構築する

#### Scenario: Rulesや用語集を変更する
- **WHEN** Rules、用語集、Review参照またはスクリプトを変更して通常再実行する
- **THEN** ユーティリティはそれらの内容差分による自動無効化を行わず、利用者は反映が必要な場合に`--force`を使用する

### Requirement: 作業領域と進捗表示
翻訳は公開DOCX以外のResume用成果物を`output/<source-stem>/.work/`へ格納しなければならない（SHALL）。通常実行と`--dry-run`はStage別件数と、再利用、実行、再試行する連続ページ範囲を表示しなければならない（SHALL）。

#### Scenario: 作業成果物を保存する
- **WHEN** 翻訳を実行する
- **THEN** `.work/`に`state.json`、`parsed.json`、`normalized.json`、`structured/`、`translated/`、`reviewed/`および`document.ja.md`が保存される

#### Scenario: Dry runを実行する
- **WHEN** `--dry-run`が指定される
- **THEN** 外部APIを呼び出さずファイルも変更せず、現在の状態から再利用・実行・再試行予定を集約表示する

### Requirement: DOCX生成
DOCXは同梱の`templates/template.docx`を既定のreference docとしてPandocで生成し、目次、図目次、表目次および章番号を含めなければならない（SHALL）。Pandoc生成後の独自OOXML書換えを行ってはならない（SHALL NOT）。

#### Scenario: 必要なPandoc機能が利用できる
- **WHEN** Pandocが必要なDOCX writer機能、図表目次オプションおよびnative numberingを提供する
- **THEN** 中間Markdownと既定テンプレートから`document.ja.docx`を生成する

#### Scenario: Pandoc機能が不足する
- **WHEN** Pandocが存在しないか必要な機能を提供しない
- **THEN** 互換fallbackやOOXML補正を試さず、必要条件を示して実行開始前に失敗する

### Requirement: 翻訳Backendとモデル設定
翻訳Backendは`openai`と`libretranslate`から選択でき、既定値を`openai`としなければならない（SHALL）。Structure、Translation、Review、Embeddingのモデル名はそれぞれ独立した環境変数から設定できなければならず、OpenAI互換APIのURLと認証情報は共通設定を使用しなければならない（SHALL）。LibreTranslate選択時もStructureとReviewにはOpenAI互換APIを使用しなければならない（SHALL）。

#### Scenario: OpenAI互換Backendを使用する
- **WHEN** Backendが省略されるか`--backend openai`が指定される
- **THEN** LiteLLM経由のChat Completions APIを使用して翻訳する

#### Scenario: LibreTranslateを使用する
- **WHEN** `--backend libretranslate`が指定され必要な接続設定が存在する
- **THEN** 翻訳だけをLibreTranslateへ切り替え、StructureとReviewのモデル設定を維持する

### Requirement: OpenAI互換API契約
LLM呼出しはLiteLLM経由の`/v1/chat/completions`、JSON Schema structured output、およびStructureでの画像メッセージを利用できなければならない（SHALL）。Responses API、tool callingおよび非structured-output fallbackを必須としてはならない（SHALL NOT）。API応答にusageがなくても処理を継続しなければならない（SHALL）。

#### Scenario: ローカルvLLM routeを使用する
- **WHEN** LiteLLM routeの背後に対応するQwenまたはGemmaモデルが設定される
- **THEN** Chat CompletionsとJSON Schemaの共通契約だけでStructure、TranslationおよびReviewを実行する

### Requirement: 外部呼出しの再試行
通信障害、HTTP 408、429および5xxだけを指数backoffで最大3回再試行し、それ以外の応答は直ちに失敗として扱わなければならない（SHALL）。

#### Scenario: 一時的な障害が解消する
- **WHEN** 外部サービスが一時的に429を返し、最大3回以内に成功する
- **THEN** 同じ処理を継続し成功結果を保存する

#### Scenario: 恒久的な入力エラーが返る
- **WHEN** 外部サービスが再試行対象外の4xxを返す
- **THEN** 再試行せず対象工程を失敗させる

### Requirement: 任意のLangfuse記録
Langfuse認証情報が完全に設定されている場合、実行、工程、ページおよびReview処理を階層的に記録しなければならない（SHALL）。原文、訳文、prompt、response、RAG本文、修正内容およびReview指摘を記録し、認証情報を記録してはならない（SHALL NOT）。ページ画像のuploadは既定で無効とし、`LANGFUSE_MEDIA_ENABLED=true`の場合だけ記録しなければならない（SHALL）。

#### Scenario: Langfuseを設定しない
- **WHEN** Langfuse認証情報が一つも設定されていない
- **THEN** 計装を無効にして翻訳を継続する

#### Scenario: Langfuse設定が不完全である
- **WHEN** Langfuse認証情報の一部だけが設定されている
- **THEN** 実行開始前に設定エラーを返す

#### Scenario: 記録処理が失敗する
- **WHEN** 翻訳中にLangfuseへの送信が失敗する
- **THEN** 警告を表示し、翻訳結果の生成は継続する

### Requirement: v4非互換と非目標
v5はv4のCLI引数、中間JSON、manifestおよび8列用語集との互換性を提供してはならない（SHALL NOT）。PDFの見た目の厳密な再現、章・付録ごとのページ番号再開始、章別動的header/footer、高度なDOCX相互参照および独自OOXML後処理を初版の要件としてはならない（SHALL NOT）。

#### Scenario: v4成果物を指定する
- **WHEN** v4のmanifestまたは中間成果物だけをv5のResume入力として与える
- **THEN** v5はそれらを再利用せず、新しい出力先または`--force`による実行を要求する
