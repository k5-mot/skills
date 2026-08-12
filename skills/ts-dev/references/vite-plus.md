# Vite+工程

Vite+ を使うTypeScriptフロントエンドではこのreferenceを読む。Vite+ は Vite とは別の統合ツールチェーンとして扱い、`vp` CLI を正本にする。

## 前提

Vite+ は Vite、Rolldown、Vitest、tsdown、Oxlint、Oxfmt、Vite Task をまとめた統合ツールチェーンである。runtime管理、package管理、frontend tooling はグローバルCLIの `vp` から扱う。

Docs はローカルの `node_modules/vite-plus/docs`、または https://viteplus.dev/guide/ を参照する。

## コマンド選択

- 開発サーバーは `vp dev` を使う。
- build は `vp build` を使う。
- format、lint、type check、test のまとまった確認には `vp check` と `vp test` を使う。
- 個別コマンドの確認には `vp help` または `vp <command> --help` を使う。
- `vite.config.ts` tasks や `package.json` scripts が必要な場合は `vp run <script>` で実行する。

## レビュー前チェック

- remote changes を取り込んだ後、作業前に `vp install` を実行する。
- 変更後は `vp check` と `vp test` を実行する。
- 追加検証が必要な `vite.config.ts` tasks または `package.json` scripts がないか確認する。
- setup、runtime、package-manager の挙動が怪しい場合は `vp env doctor` を実行し、出力を調査メモや質問に含める。
