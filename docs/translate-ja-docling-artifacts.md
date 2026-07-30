# Docling Serve referenced image artifacts 調査メモ

調査日: 2026-07-30

## 結論

`image_export_mode: "referenced"` かつ zip 返却を使う場合、Docling Serve / Jobkit はエクスポート本文と同じ階層に `artifacts/` ディレクトリを置き、その中へ参照画像を PNG として保存する実装になっている。

単一ファイルを `/v1/convert/file` で `to_formats=["json"]`, `image_export_mode="referenced"`, `target_type="zip"` として変換した場合、想定される zip 内レイアウトは概ね以下。

```text
converted_docs.zip
├── <source-stem>.json
└── artifacts/
    ├── image_000000_<hash>.png
    ├── image_000001_<hash>.png
    ├── page_000001_<hash>.png
    └── ...
```

Markdown も同時に出す場合は、同じ `artifacts/` を参照する。

```text
converted_docs.zip
├── <source-stem>.json
├── <source-stem>.md
└── artifacts/
    └── ...
```

## 根拠

- Docling 公式 REST API ページでは、`/v1/convert/file` は multipart upload 用 endpoint で、`image_export_mode` は `placeholder` / `embedded` / `referenced` を取る。zip target を要求した場合、または複数ファイルが生成される場合は zip archive が返る。出典: [Docling REST API](https://docling-project.github.io/docling/usage/api_server/rest_api/)
- Docling Serve v1 では、旧 `options.return_as_file` ではなく `target: {"kind": "zip"}` を使う、と migration docs に明記されている。`/v1/convert/file` の multipart 実装では form field の `target_type` を受け取り、`target_type == zip` のとき `ZipTarget()` に解決する。出典: [v1_migration.md](https://github.com/docling-project/docling-serve/blob/ef977310190ac72f6e190267750e9a6dce96aa9a/docs/v1_migration.md#L47-L68), [app.py](https://github.com/docling-project/docling-serve/blob/ef977310190ac72f6e190267750e9a6dce96aa9a/docling_serve/app.py#L907-L938)
- Docling Serve は `ZipArchiveResult` を `application/zip`、attachment filename `converted_docs.zip` として返す。出典: [response_preparation.py](https://github.com/docling-project/docling-serve/blob/ef977310190ac72f6e190267750e9a6dce96aa9a/docling_serve/response_preparation.py#L44-L50)
- zip の中身は docling-jobkit 側で作られる。`_materialize_document_exports()` は `artifacts_dir = Path("artifacts")` を固定し、JSON/HTML/Markdown の `save_as_*` に渡す。通常の zip export では `bundle_resources=False` で `output_dir` にファイル群を書き、`shutil.make_archive(..., root_dir=output_dir)` で zip 化する。出典: [export.py](https://github.com/docling-project/docling-jobkit/blob/477b4336fce267513ebb90f9ea97add4d5a16e75/docling_jobkit/convert/export.py#L69-L131), [results.py](https://github.com/docling-project/docling-jobkit/blob/477b4336fce267513ebb90f9ea97add4d5a16e75/docling_jobkit/convert/results.py#L291-L320), [results.py](https://github.com/docling-project/docling-jobkit/blob/477b4336fce267513ebb90f9ea97add4d5a16e75/docling_jobkit/convert/results.py#L862-L880)
- docling-core は `referenced` のとき、画像を PNG として保存し、本文側には保存先への相対 URI を入れる。画像ファイル名は picture が `image_<連番6桁>_<hash>.png`、page image が `page_<page_no 6桁>_<hash>.png`。`save_as_json()` は `include_page_images=True` で `_make_copy_with_refmode()` を呼ぶ。出典: [document.py](https://github.com/docling-project/docling-core/blob/6b8d35ff28395155fb68ac1e03e66c4e0097ee31/docling_core/types/doc/document.py#L3364-L3444), [document.py](https://github.com/docling-project/docling-core/blob/6b8d35ff28395155fb68ac1e03e66c4e0097ee31/docling_core/types/doc/document.py#L3490-L3513), [document.py](https://github.com/docling-project/docling-core/blob/6b8d35ff28395155fb68ac1e03e66c4e0097ee31/docling_core/types/doc/document.py#L3914-L3960)
- Docling Serve の regression test でも、`/v1/convert/file` に `image_export_mode="referenced"` と `target_type="zip"` を渡し、zip 内の `.json` に含まれる `PictureItem.image.uri` が zip の `namelist` に存在することを検証している。つまり JSON 内の参照パスは zip root から見た相対パスとして解決される。出典: [test_fastapi_endpoints.py](https://github.com/docling-project/docling-serve/blob/ef977310190ac72f6e190267750e9a6dce96aa9a/tests/test_fastapi_endpoints.py#L182-L214)

## 注意点

- `DOCLING_SERVE_ARTIFACTS_PATH` / `--artifacts-path` はモデル重みをロードするためのパスであり、`image_export_mode="referenced"` の出力画像ディレクトリ指定ではない。出典: [configuration.md](https://github.com/docling-project/docling-serve/blob/ef977310190ac72f6e190267750e9a6dce96aa9a/docs/configuration.md#L36-L43)
- `save_as_json(..., artifacts_dir=None)` の docling-core 既定では `<stem>_artifacts/` が使われるが、docling-jobkit / docling-serve 経由の export では明示的に `Path("artifacts")` が渡される。そのため、このスキルで zipball と噛み合わせるなら、`pages/` / `pictures/` を独自に分けるより、Docling 由来の `artifacts/` を `source.bronze.json` と同階層に展開・保持する構成が自然。

## translate-ja への示唆

- `preprocess_doc_with_docling.py` は zip 返却を前提にし、zip 内の `*.json` を `source.bronze.json` として保存し、zip 内の `artifacts/` を同じ出力ディレクトリへ展開するのがよい。
- 成果物例:

```text
output/
├── source.bronze.json
└── artifacts/
    ├── image_000000_<hash>.png
    ├── image_000001_<hash>.png
    ├── page_000001_<hash>.png
    └── ...
```

- JSON 内の `image.uri` は `artifacts/...png` のような相対パスとして扱い、`source.bronze.json` の親ディレクトリを基準に解決するのが docling-serve zipball と整合する。
