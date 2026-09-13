"""外部adapterの狭いrequest契約と失敗処理を検証する。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from io import BytesIO
import zipfile

import httpx
import pytest
from PIL import Image

from src.adapters import docling
from src.adapters import langfuse as langfuse_adapter
from src.adapters import libretranslate as libre
from src.adapters import llm, pandoc
from src.config import Settings


class StatusError(RuntimeError):
    """status codeだけを持つ外部API test用例外。"""

    def __init__(self, status_code: int) -> None:
        """status codeを保持して初期化する。

        Args:
            status_code: 模擬HTTP status。

        Returns:
            なし。
        """

        super().__init__(str(status_code))
        self.status_code = status_code


@pytest.mark.parametrize("status", [408, 429, 500, 503])
def test_retry_call_retries_only_temporary_status(status: int) -> None:
    """一時的statusが最大3回以内で再試行されることを確認する。

    Args:
        status: 再試行対象status。

    Returns:
        なし。
    """

    attempts = 0

    def call() -> str:
        """二回失敗して三回目に成功する。

        Returns:
            三回目の成功文字列。
        """

        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise StatusError(status)
        return "ok"

    assert llm.retry_call(call, lambda _delay: None) == "ok"
    assert attempts == 3


def test_retry_call_does_not_retry_other_4xx() -> None:
    """400系恒久errorを一回で返すことを確認する。

    Returns:
        なし。
    """

    attempts = 0

    def call() -> None:
        """400 errorを発生させる。

        Returns:
            なし。
        """

        nonlocal attempts
        attempts += 1
        raise StatusError(400)

    with pytest.raises(StatusError):
        llm.retry_call(call, lambda _delay: None)
    assert attempts == 1


@pytest.mark.parametrize(
    ("responses", "schema"),
    [
        (['{"value":"ok"}'], {"type": "object"}),
        (['```json\n{"value":"ok"}\n```'], {"type": "object"}),
        (
            ["指摘事項はありません。", '{"value":"ok"}'],
            {"type": "object"},
        ),
        (
            ["```json\n{}\n```", '{"value":"ok"}'],
            {"type": "object", "required": ["value"]},
        ),
        (
            ['{"value":"ok"}\n\nこれは追加説明です。'],
            {"type": "object"},
        ),
    ],
)
def test_structured_chat_uses_json_schema_and_accepts_local_model_json(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    responses: list[str],
    schema: dict[str, Any],
) -> None:
    """Chat CompletionsがJSON Schemaを使い、local modelのJSON表現を許容する。

    Args:
        monkeypatch: OpenAI clientを差し替えるfixture。
        settings: 共通Settings fixture。
        responses: local modelが順に返す応答文字列。
        schema: 要求するJSON Schema。

    Returns:
        なし。
    """

    calls: list[dict[str, Any]] = []

    class Completions:
        """Chat Completionsのtest double。"""

        def create(self, **kwargs: Any) -> Any:
            """引数を保存してJSON応答を返す。

            Args:
                kwargs: Chat Completions引数。

            Returns:
                usageを持たない模擬response。
            """

            content = responses[len(calls)]
            calls.append(kwargs)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
            )

    fake = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    monkeypatch.setattr(llm, "_client", lambda _settings: fake)
    result = llm.structured_chat(
        settings, "model", "system", "user", "answer", schema
    )
    assert result == {"value": "ok"}
    assert len(calls) == len(responses)
    assert calls[-1]["response_format"]["type"] == "json_schema"
    assert "tools" not in calls[-1]
    assert "JSON Schema" in calls[-1]["messages"][0]["content"]


def test_langfuse_media_passes_image_content_type(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """LangfuseMediaへfile pathとMIME typeを両方渡すことを確認する。

    Args:
        monkeypatch: LangfuseMediaを差し替えるfixture。
        tmp_path: 画像fileを置く一時directory。

    Returns:
        なし。
    """

    image = tmp_path / "page.png"
    image.write_bytes(b"png")
    monkeypatch.setattr(langfuse_adapter, "LangfuseMedia", dict)
    assert langfuse_adapter.media(image) == {
        "file_path": str(image),
        "content_type": "image/png",
    }


def test_libretranslate_preserves_protected_fragments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LibreTranslate往復でURL、option、識別子を保持することを確認する。

    Args:
        monkeypatch: HTTP requestを差し替えるfixture。

    Returns:
        なし。
    """

    class Response:
        """LibreTranslate responseのtest double。"""

        def raise_for_status(self) -> None:
            """成功responseとして何もしない。

            Returns:
                なし。
            """

        def json(self) -> dict[str, list[str]]:
            """placeholderを維持した訳を返す。

            Returns:
                LibreTranslate互換JSON。
            """

            return {"translatedText": ["参照 __V5_PROTECTED_0__ と __V5_PROTECTED_1__"]}

    monkeypatch.setattr(httpx, "post", lambda *_args, **_kwargs: Response())
    translated = libre.translate_texts(
        "http://libre", None, ["See https://example.com and --force"]
    )
    assert translated == ["参照 https://example.com と --force"]


def test_langfuse_redaction_removes_nested_credentials() -> None:
    """trace payloadから認証情報だけが除外されることを確認する。

    Returns:
        なし。
    """

    value = langfuse_adapter.redact_credentials(
        {
            "text": "keep",
            "api_key": "secret",
            "nested": {"password": "x", "source": "body"},
        }
    )
    assert value == {"text": "keep", "nested": {"source": "body"}}


def test_pandoc_capability_check_reports_missing_feature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pandoc option不足をfallbackなしで開始前errorにすることを確認する。

    Args:
        monkeypatch: executable探索とsubprocessを差し替えるfixture。

    Returns:
        なし。
    """

    monkeypatch.setattr(pandoc.shutil, "which", lambda _name: "/bin/pandoc")
    monkeypatch.setattr(
        pandoc.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(stdout="--number-sections"),
    )
    with pytest.raises(RuntimeError, match="list-of-figures"):
        pandoc.check_pandoc()


def test_create_docx_uses_fixed_arguments(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """DOCX生成がreference docと図表目次等の固定引数を渡すことを確認する。

    Args:
        monkeypatch: Pandoc実行を差し替えるfixture。
        tmp_path: pytest一時directory。

    Returns:
        なし。
    """

    calls: list[list[str]] = []
    monkeypatch.setattr(pandoc, "check_pandoc", lambda: None)

    def run(args: list[str], **_kwargs: Any) -> SimpleNamespace:
        """Pandoc引数を記録する。

        Args:
            args: command引数。
            _kwargs: subprocess option。

        Returns:
            成功結果。
        """

        calls.append(args)
        temporary = Path(args[args.index("--output") + 1])
        with zipfile.ZipFile(temporary, "w") as archive:
            archive.writestr("[Content_Types].xml", "types")
            archive.writestr("word/document.xml", "document")
        return SimpleNamespace(stdout="")

    monkeypatch.setattr(pandoc.subprocess, "run", run)
    pandoc.create_docx(
        tmp_path / "in.md", tmp_path / "out.docx", tmp_path / "template.docx"
    )
    command = calls[0]
    assert "--list-of-figures" in command and "--list-of-tables" in command
    assert "docx+native_numbering" in command and "--reference-doc" in command
    assert Path(command[command.index("--output") + 1]) != tmp_path / "out.docx"
    assert zipfile.is_zipfile(tmp_path / "out.docx")


def test_create_docx_failure_preserves_previous_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Pandoc失敗時に既存DOCXとdirectoryを変更しないことを確認する。

    Args:
        monkeypatch: Pandoc実行を差し替えるfixture。
        tmp_path: pytest一時directory。

    Returns:
        なし。
    """

    output = tmp_path / "out.docx"
    output.write_bytes(b"previous")
    monkeypatch.setattr(pandoc, "check_pandoc", lambda: None)

    def fail(args: list[str], **_kwargs: Any) -> None:
        """一時出力へ不完全内容を書いてPandoc失敗を模擬する。

        Args:
            args: command引数。
            _kwargs: subprocess option。

        Returns:
            なし。

        Raises:
            CalledProcessError: 常に送出する。
        """

        Path(args[args.index("--output") + 1]).write_bytes(b"partial")
        raise pandoc.subprocess.CalledProcessError(1, args)

    monkeypatch.setattr(pandoc.subprocess, "run", fail)
    with pytest.raises(pandoc.subprocess.CalledProcessError):
        pandoc.create_docx(tmp_path / "in.md", output, tmp_path / "template.docx")
    assert output.read_bytes() == b"previous"
    assert list(tmp_path.iterdir()) == [output]


def test_docling_extracts_one_json_and_safe_assets(tmp_path: Path) -> None:
    """Docling ZIPからJSONとartifacts配下だけを展開することを確認する。

    Args:
        tmp_path: pytest一時directory。

    Returns:
        なし。
    """

    stream = BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("document.json", '{"schema_name":"DoclingDocument"}')
        archive.writestr("artifacts/image.png", b"png")
        archive.writestr("../escape.txt", b"bad")
    value = docling._extract_result(stream.getvalue(), tmp_path / "assets")
    assert value["schema_name"] == "DoclingDocument"
    assert (tmp_path / "assets" / "image.png").read_bytes() == b"png"
    assert not (tmp_path / "escape.txt").exists()


@pytest.mark.parametrize("status_field", ["task_status", "status"])
def test_docling_accepts_status_fields_after_poll_retry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    status_field: str,
) -> None:
    """新旧Doclingのstatusを受理し、状態変化だけ表示する。

    Args:
        monkeypatch: HTTP呼出しと待機を差し替えるfixture。
        tmp_path: pytest一時directory。
        capsys: 標準出力を収集するfixture。
        status_field: Docling versionごとの完了status field名。

    Returns:
        なし。
    """

    source = tmp_path / "source.pdf"
    source.write_bytes(b"pdf")
    stream = BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr(
            "document.json",
            '{"schema_name":"DoclingDocument","pages":{"1":{}}}',
        )
    polls = 0

    class Response:
        """Docling API responseのtest double。"""

        def __init__(
            self,
            body: dict[str, str] | None = None,
            content: bytes = b"",
            status: int = 200,
        ) -> None:
            """JSON、binary、statusを保持する。

            Args:
                body: JSON応答。
                content: ZIP応答。
                status: HTTP status。

            Returns:
                なし。
            """

            self.body = body or {}
            self.content = content
            self.status = status

        def raise_for_status(self) -> None:
            """一時statusならtest用HTTP例外を送出する。

            Returns:
                なし。

            Raises:
                StatusError: statusが成功でない場合。
            """

            if self.status >= 400:
                raise StatusError(self.status)

        def json(self) -> dict[str, str]:
            """保持したJSON応答を返す。

            Returns:
                JSON object。
            """

            return self.body

    def get(url: str, **_kwargs: Any) -> Response:
        """初回pollだけ503にし、その後は完了結果を返す。

        Args:
            url: Docling endpoint URL。
            _kwargs: 未使用HTTP引数。

        Returns:
            endpointに対応するresponse。
        """

        nonlocal polls
        if "/status/" in url:
            polls += 1
            statuses = ["queued", "queued", "queued", "started", "success"]
            return Response(
                {status_field: statuses[polls - 1]},
                status=503 if polls == 1 else 200,
            )
        return Response(content=stream.getvalue())

    actual_retry = docling.retry_call
    monkeypatch.setattr(
        docling,
        "retry_call",
        lambda call: actual_retry(call, lambda _delay: None),
    )
    monkeypatch.setattr(
        docling.httpx,
        "post",
        lambda *_args, **_kwargs: Response({"task_id": "task"}),
    )
    monkeypatch.setattr(docling.httpx, "get", get)
    monkeypatch.setattr(docling.time, "sleep", lambda _delay: None)
    result = docling.convert_pdf(
        source,
        tmp_path / "parsed.json",
        tmp_path / "assets",
        "http://docling",
    )
    assert result["schema_name"] == "DoclingDocument"
    assert polls == 5
    assert capsys.readouterr().out == (
        "Docling: queued\nDocling: started\nDocling: success\n"
    )


def test_langfuse_failure_is_only_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Langfuse flush障害が主処理へ例外を送らないことを確認する。

    Args:
        monkeypatch: Langfuse設定とclientを差し替えるfixture。
        caplog: warning logを収集するfixture。

    Returns:
        なし。
    """

    monkeypatch.setattr(langfuse_adapter, "_enabled", lambda: True)

    class Client:
        """flush時に失敗するLangfuse client。"""

        def flush(self) -> None:
            """通信障害を模擬する。

            Returns:
                なし。
            """

            raise RuntimeError("offline")

    monkeypatch.setattr(langfuse_adapter, "get_client", lambda: Client())
    langfuse_adapter.flush_safely()
    assert "offline" in caplog.text


def test_render_cover_uses_first_page_and_requested_dpi(tmp_path: Path) -> None:
    """二ページPDFの第1ページだけを150 DPI相当PNGへ変換する。

    Args:
        tmp_path: pytest一時directory。

    Returns:
        なし。
    """

    source = tmp_path / "source.pdf"
    first = Image.new("RGB", (72, 72), "red")
    second = Image.new("RGB", (72, 72), "blue")
    first.save(
        source, format="PDF", save_all=True, append_images=[second], resolution=72
    )
    output = pandoc.render_cover(source, tmp_path / "cover.png", 150)
    with Image.open(output) as image:
        assert 140 <= image.width <= 160
        pixel = image.convert("RGB").getpixel((image.width // 2, image.height // 2))
        assert isinstance(pixel, tuple) and pixel[0] > 200
