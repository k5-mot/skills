## Purpose

翻訳Reviewで参照する文書を検索可能なchunkとしてQdrantへ登録し、同じsourceの更新時にも旧版を失わない安全なrevision置換を提供する。

## ADDED Requirements

### Requirement: 参照文書登録CLI
ユーティリティは`register`サブコマンドで単一ファイルまたはdirectoryの一方を受け付け、PDF、DOCX、MarkdownおよびUTF-8テキストを既存Qdrant collectionへ登録しなければならない（SHALL）。directory指定時は再帰的に探索し、隠しファイルと空ファイルを除外しなければならない（SHALL）。

#### Scenario: 単一文書を登録する
- **WHEN** 利用者が`register --doc-path reference.pdf`を実行する
- **THEN** 文書がReview検索可能なchunkとして設定済みcollectionへ登録される

#### Scenario: directoryを登録する
- **WHEN** 利用者が`register --doc-dir references`を実行する
- **THEN** 対応形式の非表示でない非空ファイルが再帰的に登録される

#### Scenario: 入力指定が排他的でない
- **WHEN** `--doc-path`と`--doc-dir`の両方またはいずれも指定されない
- **THEN** 利用者へ一方だけを指定するよう示して終了する

### Requirement: 意味境界を優先するchunk
文書は見出しと段落の境界を優先して約1,000 tokenごとに分割し、隣接chunkへ約100 tokenを重複させなければならない（SHALL）。各pointはsource、形式、文書SHA-256によるrevision、unit番号およびchunk番号をmetadataとして保持しなければならない（SHALL）。

#### Scenario: 長い文書を登録する
- **WHEN** 文書が1,000 tokenを大きく超える
- **THEN** 見出しまたは段落を不必要に分断しない複数chunkが順序情報と重複を伴って登録される

### Requirement: sourceとrevision
sourceは登録対象rootからの正規化された相対パスで識別し、revisionはファイル内容のSHA-256で識別しなければならない（SHALL）。同じcollection内の別sourceを置換対象に含めてはならない（SHALL NOT）。

#### Scenario: 同名内容を再登録する
- **WHEN** 同じsourceかつ同じrevisionの文書を再登録する
- **THEN** 重複revisionを増やさず同じ登録結果へ収束する

#### Scenario: 同じsourceを更新する
- **WHEN** sourceが同じで内容SHA-256が変わった文書を登録する
- **THEN** collection名とsourceを維持し、新しいrevisionへ置換する

### Requirement: 安全な自動置換
更新文書は新revisionの全pointをupsertして検証した後に限り、同じsourceの旧revisionを削除しなければならない（SHALL）。新revisionの登録に失敗した場合は旧revisionを保持しなければならない（SHALL）。

#### Scenario: 新revisionの登録が失敗する
- **WHEN** 新revisionの一部または全部をupsertできない
- **THEN** 旧revisionを削除せずregisterを失敗終了させる

#### Scenario: 旧revisionの削除が失敗する
- **WHEN** 新revisionの検証後に旧revisionの削除が失敗する
- **THEN** 新旧revisionが一時的に共存した状態でregisterを失敗終了させる
- **THEN** 同じコマンドの再実行によって新revisionを維持したまま旧revisionの削除へ再試行できる

### Requirement: 固定された登録方針
collection名とEmbeddingモデルは環境変数で設定し、chunk size、overlapおよび置換方式をCLIの調整項目として公開してはならない（SHALL NOT）。

#### Scenario: 登録を実行する
- **WHEN** 必須のQdrant接続、collectionおよびEmbeddingモデル設定が存在する
- **THEN** 固定された既定のchunk方針で文書を登録する

#### Scenario: 必須設定が欠ける
- **WHEN** QdrantまたはEmbeddingの必須設定が欠ける
- **THEN** 文書やcollectionを変更する前に設定エラーで終了する
