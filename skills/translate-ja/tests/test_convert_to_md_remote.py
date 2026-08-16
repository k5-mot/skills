"""convert_to_md_remote.py の単体テスト。"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import convert_to_md_remote as remote  # noqa: E402


class FakeResponse:
    """requests.Response の最小 fake を表す。

    Args:
        status_code: HTTP status code。
        text: response text。
        payload: json() が返す値。

    Returns:
        request_convert のテストに使える response object。
    """

    def __init__(
        self, status_code: int, text: str = "", payload: object | None = None
    ) -> None:
        """FakeResponse を初期化する。

        Args:
            status_code: HTTP status code。
            text: response text。
            payload: json() が返す値。

        Returns:
            なし。
        """

        self.status_code = status_code
        self.text = text
        self._payload = payload

    def json(self) -> object:
        """fake JSON payload を返す。

        Returns:
            json() の戻り値。
        """

        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeSettings:
    """Docling 設定の最小 fake を表す。

    Args:
        なし。

    Returns:
        require_docling_settings の戻り値相当の object。
    """

    server_url = "http://docling.test"
    api_key = "secret"
    timeout_seconds = 60


def test_docling_markdown_payload_disables_ocr_by_default(monkeypatch) -> None:  # noqa: ANN001
    """Markdown 変換 payload は既定で OCR を無効にする。

    Args:
        monkeypatch: 環境変数を一時変更する pytest fixture。

    Returns:
        なし。
    """

    monkeypatch.delenv("DOCLING_DO_OCR", raising=False)
    monkeypatch.delenv("DOCLING_FORCE_OCR", raising=False)

    payload = remote.docling_markdown_payload(120)

    assert ("to_formats", "md") in payload
    assert ("do_ocr", "false") in payload
    assert ("force_ocr", "false") in payload
    assert not any(key == "ocr_lang" for key, _value in payload)


def test_docling_markdown_payload_adds_ocr_languages(monkeypatch) -> None:  # noqa: ANN001
    """OCR 有効時は言語指定を payload に追加する。

    Args:
        monkeypatch: 環境変数を一時変更する pytest fixture。

    Returns:
        なし。
    """

    monkeypatch.setenv("DOCLING_DO_OCR", "true")
    monkeypatch.setenv("DOCLING_OCR_LANGS", "eng,jpn")

    payload = remote.docling_markdown_payload(120)

    assert ("do_ocr", "true") in payload
    assert ("ocr_preset", "tesseract") in payload
    assert ("ocr_lang", "eng") in payload
    assert ("ocr_lang", "jpn") in payload


def test_extract_zip_result_writes_markdown_and_artifacts(tmp_path: Path) -> None:
    """Docling ZIP から Markdown と artifacts を保存する。

    Args:
        tmp_path: 一時ファイルを作成する pytest fixture。

    Returns:
        なし。
    """

    zip_path = tmp_path / "result.zip"
    output_path = tmp_path / "out" / "source.md"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("source.md", "# Title\n\nBody\n")
        archive.writestr("artifacts/page_000001.png", b"png")

    remote.extract_zip_result(zip_path, output_path)

    assert output_path.read_text(encoding="utf-8") == "# Title\n\nBody\n"
    assert (tmp_path / "out" / "artifacts" / "page_000001.png").read_bytes() == b"png"


def test_request_convert_falls_back_to_file_field(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    """Docling が files field を受けない場合は file field で再送する。

    Args:
        tmp_path: 一時ファイルを作成する pytest fixture。
        monkeypatch: 依存関数を差し替える pytest fixture。

    Returns:
        なし。
    """

    input_path = tmp_path / "source.pdf"
    input_path.write_bytes(b"%PDF-1.4")
    calls: list[str] = []

    def fake_post(_endpoint, **kwargs):  # noqa: ANN001, ANN003, ANN202
        """requests.post の fake を返す。

        Args:
            _endpoint: 呼び出し先 endpoint。
            **kwargs: requests.post に渡された引数。

        Returns:
            FakeResponse。
        """

        file_field = next(iter(kwargs["files"].keys()))
        calls.append(file_field)
        if file_field == "files":
            return FakeResponse(
                422,
                payload={
                    "detail": [
                        {
                            "loc": ["body", "files"],
                            "type": "missing",
                        }
                    ]
                },
            )
        return FakeResponse(200, text="ok")

    monkeypatch.setattr(
        remote, "require_docling_settings", lambda **_kwargs: FakeSettings()
    )
    monkeypatch.setattr(remote.requests, "post", fake_post)

    response = remote.request_convert(
        "http://docling.test/v1/convert/file",
        input_path,
        docling_timeout=120,
        request_timeout=10,
    )

    assert response.status_code == 200
    assert calls == ["files", "file"]


def test_convert_to_markdown_creates_output_parent(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    """出力親ディレクトリがない場合も一時 ZIP 保存前に作成する。

    Args:
        tmp_path: 一時ファイルを作成する pytest fixture。
        monkeypatch: 依存関数を差し替える pytest fixture。

    Returns:
        なし。
    """

    input_path = tmp_path / "source.pdf"
    output_path = tmp_path / "missing" / "source.md"
    input_path.write_bytes(b"%PDF-1.4")

    def fake_post_convert_sync(
        _input_path: Path, output_zip: Path, *, docling_timeout: int
    ) -> None:
        """Docling 同期変換の代わりに ZIP 保存可否を検証する。

        Args:
            _input_path: 変換対象ファイル。
            output_zip: ZIP 保存先。
            docling_timeout: Docling 側の timeout 秒数。

        Returns:
            なし。
        """

        assert docling_timeout == 120
        assert output_zip.parent.exists()
        output_zip.write_bytes(b"zip")

    def fake_extract_zip_result(_zip_path: Path, output_path: Path) -> None:
        """Docling ZIP 展開の代わりに Markdown を保存する。

        Args:
            _zip_path: ZIP 保存先。
            output_path: Markdown 出力先。

        Returns:
            なし。
        """

        output_path.write_text("# ok\n", encoding="utf-8")

    monkeypatch.setattr(remote, "post_convert_sync", fake_post_convert_sync)
    monkeypatch.setattr(remote, "extract_zip_result", fake_extract_zip_result)

    remote.convert_to_markdown(
        input_path, output_path, docling_timeout=120, force_async=False
    )

    assert output_path.read_text(encoding="utf-8") == "# ok\n"
