# translate-ja runner

`skills/translate-ja/run.sh` と `skills/translate-ja/run.ps1` は、translate-ja の 7 工程を順番に呼び出すユーティリティです。

## 使い方

```bash
./skills/translate-ja/run.sh --input ./docs/source/source.pdf
```

```powershell
./skills/translate-ja/run.ps1 -InputPath ./docs/source/source.pdf
```

各工程は期待する成果物が既に存在する場合にスキップされます。再生成したい場合は `--force` または `-Force` を指定します。

## 出力構成

出力先を指定しない場合、入力ファイルと同じ階層の `output/` を使います。

```text
output/
  source.bronze.json
  source.silver.json
  source.gold.json
  source.ja.md
  source.ja.docx
  artifacts/
  chunks-en/
  chunks-ja/
  reports/
  logs/
```

`artifacts/` は Docling Serve の `image_export_mode: referenced` と zip 返却に合わせた参照画像ディレクトリです。詳細は [translate-ja-docling-artifacts.md](/workspaces/agent-skills/docs/translate-ja-docling-artifacts.md:1) を参照してください。
