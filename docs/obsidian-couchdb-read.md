# Obsidian CouchDB Read

`obsidian-couchdb-read` は Self-hosted LiveSync 形式の CouchDB snapshot を読み、Obsidian Markdown ノートを検索する public CLI である。

## 環境変数

| 変数 | 必須 | 用途 |
|---|---:|---|
| `OBSIDIAN_COUCHDB_DOMAIN` | Yes | CouchDB の domain または HTTP(S) base URL。scheme 省略時は HTTPS を使う |
| `OBSIDIAN_COUCHDB_USER` | Yes | Basic 認証 user |
| `OBSIDIAN_COUCHDB_PASSWORD` | Yes | Basic 認証 password |

database 名は `obsidian` に固定している。資格情報を URL、標準出力、ログへ含めない。

## データ復元

`POST /obsidian/_find` で親文書の metadata だけを取得し、次の条件を満たす文書を選ぶ。

- `type` が `plain`
- `deleted` が `true` ではない
- `path` が `.md` で終わる
- path 要素が `.` で始まらない
- path が `ix:` で始まらない

親文書の `children` を配列順に参照し、必要なノートだけ `POST /obsidian/_all_docs?include_docs=true` で leaf をまとめて読み、各 `type=leaf` 文書の `data` を連結する。参照欠落は検索結果の欠落として黙認せず、処理を失敗させる。

本文検索は token ごとに `_find` で一致する leaf ID だけを取得し、親文書の `children` と照合する。このため、全 leaf 本文を client へ転送しない。CouchDB に該当 index がない場合は server 側 scan になるが、network 転送は候補 ID と最終結果の本文に限定される。

## コマンド契約

- `status`: server version と database 文書件数を返す。
- `list`: path prefix に一致するノート path を返す。
- `search`: 空白区切り token の AND 条件で path と本文を大小文字を区別せず検索する。結果は path 一致を先に並べる。
- `read`: path が完全一致する一つのノートを返す。既定では本文を 100,000 文字まで返す。

すべて JSON を標準出力へ返す。HTTP 通信は通常の `GET` に加え、CouchDB の読み取り専用 query endpoint `_find` と `_all_docs` だけ `POST` を使う。`PUT`、`PATCH`、`DELETE` および document URL への `POST` 経路は持たない。
