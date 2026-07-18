import importlib.util
from pathlib import Path


def load_client():
    """テスト対象の client モジュールを読み込む。"""
    path = Path(__file__).resolve().parents[1] / "scripts" / "client.py"
    spec = importlib.util.spec_from_file_location("ow_client_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


client = load_client()


class FakeResponse:
    """Open WebUI API レスポンスのテスト用ダブル。"""

    def __init__(self, status_code, payload=None, text=None):
        """HTTP ステータス、JSON ペイロード、本文を保持する。"""
        self.status_code = status_code
        self._payload = payload
        self.text = text if text is not None else ""

    def json(self):
        """設定されたペイロードを JSON として返す。"""
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeRequests:
    """requests 互換のテスト用 HTTP クライアント。"""

    def __init__(self):
        """呼び出し記録とレスポンスキューを初期化する。"""
        self.calls = []
        self.get_queue = []
        self.post_queue = []

    def get(self, url, headers=None, timeout=None):
        """GET 呼び出しを記録し、キュー先頭のレスポンスを返す。"""
        self.calls.append(("GET", url, headers, timeout, None))
        return self.get_queue.pop(0)

    def post(self, url, headers=None, json=None, timeout=None):
        """POST 呼び出しを記録し、キュー先頭のレスポンスを返す。"""
        self.calls.append(("POST", url, headers, timeout, json))
        return self.post_queue.pop(0)


def set_required_env(monkeypatch):
    """Open WebUI API 呼び出しに必要な環境変数を設定する。"""
    monkeypatch.setenv("OPEN_WEBUI_BASE_URL", "https://openwebui.example.test")
    monkeypatch.setenv("OPEN_WEBUI_API_KEY", "secret-api-key")
    monkeypatch.setenv("OPEN_WEBUI_MAX_RETRIES", "1")


def test_list_channels_calls_get(monkeypatch):
    """チャネル一覧取得が期待する GET リクエストを行うことを検証する。"""
    set_required_env(monkeypatch)
    fake = FakeRequests()
    fake.get_queue.append(FakeResponse(200, {"channels": [{"id": "ch1", "name": "daily-report"}]}))
    monkeypatch.setattr(client, "requests", fake)

    result = client.list_channels()

    assert result["ok"] is True
    assert result["channels"][0]["id"] == "ch1"
    assert result["channels"][0]["name"] == "daily-report"
    assert result["channels"][0]["description"] is None
    method, url, headers, timeout, _ = fake.calls[0]
    assert method == "GET"
    assert url == "https://openwebui.example.test/api/v1/channels/"
    assert headers["Authorization"] == "Bearer secret-api-key"
    assert headers["Accept"] == "application/json"
    assert timeout == 30


def test_resolve_channel_id_from_name(monkeypatch):
    """チャネル名からチャネル ID を解決できることを検証する。"""
    set_required_env(monkeypatch)
    fake = FakeRequests()
    fake.get_queue.append(FakeResponse(200, [{"id": "ch_daily", "name": "daily-report"}]))
    monkeypatch.setattr(client, "requests", fake)

    result = client.resolve_channel_id("#daily-report")

    assert result["ok"] is True
    assert result["channel_id"] == "ch_daily"
    assert result["channel_name"] == "daily-report"


def test_resolve_missing_channel_returns_false(monkeypatch):
    """存在しないチャネル名の解決が失敗として返ることを検証する。"""
    set_required_env(monkeypatch)
    fake = FakeRequests()
    fake.get_queue.append(FakeResponse(200, {"channels": [{"id": "ch1", "name": "other"}]}))
    monkeypatch.setattr(client, "requests", fake)

    result = client.resolve_channel_id("#daily-report")

    assert result["ok"] is False
    assert "channel not found" in result["error"]


def test_post_message_calls_post(monkeypatch):
    """チャネル投稿が期待する POST リクエストを行うことを検証する。"""
    set_required_env(monkeypatch)
    fake = FakeRequests()
    fake.get_queue.append(FakeResponse(200, {"channels": [{"id": "ch_daily", "name": "daily-report"}]}))
    fake.post_queue.append(FakeResponse(200, {"id": "msg1"}))
    monkeypatch.setattr(client, "requests", fake)

    result = client.post_message("#daily-report", "## Report", metadata={"report_type": "daily"})

    assert result["ok"] is True
    assert result["message_id"] == "msg1"
    post_call = fake.calls[1]
    assert post_call[0] == "POST"
    assert post_call[1] == "https://openwebui.example.test/api/v1/channels/ch_daily/messages/post"
    assert post_call[4]["content"] == "## Report"
    assert post_call[4]["meta"]["skill"] == "open-webui-skill"


def test_post_message_to_thread_sets_parent_id(monkeypatch):
    """スレッド返信時に parent_id が設定されることを検証する。"""
    set_required_env(monkeypatch)
    fake = FakeRequests()
    fake.post_queue.append(FakeResponse(200, {"id": "reply1"}))
    monkeypatch.setattr(client, "requests", fake)

    result = client.post_message("channel-id-1234567890", "thread reply", thread_id="msg-parent")

    assert result["ok"] is True
    assert fake.calls[0][4]["parent_id"] == "msg-parent"


def test_list_channel_messages_calls_get(monkeypatch):
    """チャネルメッセージ取得がページング付き GET を行うことを検証する。"""
    set_required_env(monkeypatch)
    fake = FakeRequests()
    fake.get_queue.append(FakeResponse(200, [{"id": "ch_daily", "name": "daily-report"}]))
    fake.get_queue.append(FakeResponse(200, [{"id": "msg1", "content": "hello", "created_at": 1}]))
    monkeypatch.setattr(client, "requests", fake)

    result = client.list_channel_messages("#daily-report", limit=10)

    assert result["ok"] is True
    assert result["channel_id"] == "ch_daily"
    assert result["messages"][0]["id"] == "msg1"
    assert fake.calls[1][1] == "https://openwebui.example.test/api/v1/channels/ch_daily/messages?skip=0&limit=10"


def test_list_all_channel_messages_pages_until_short_page(monkeypatch):
    """短いページに到達するまで全チャネルメッセージを取得することを検証する。"""
    set_required_env(monkeypatch)
    fake = FakeRequests()
    fake.get_queue.append(FakeResponse(200, [{"id": "m1"}, {"id": "m2"}]))
    fake.get_queue.append(FakeResponse(200, [{"id": "m3"}]))
    monkeypatch.setattr(client, "requests", fake)

    result = client.list_all_channel_messages("channel-id-1234567890", page_size=2)

    assert result["ok"] is True
    assert result["count"] == 3
    assert fake.calls[0][1] == "https://openwebui.example.test/api/v1/channels/channel-id-1234567890/messages?skip=0&limit=2"
    assert fake.calls[1][1] == "https://openwebui.example.test/api/v1/channels/channel-id-1234567890/messages?skip=2&limit=2"


def test_get_file_content_calls_data_content(monkeypatch):
    """ファイル本文取得が抽出済み本文エンドポイントを使うことを検証する。"""
    set_required_env(monkeypatch)
    fake = FakeRequests()
    fake.get_queue.append(FakeResponse(200, {"content": "file text"}))
    monkeypatch.setattr(client, "requests", fake)

    result = client.get_file_content("file1")

    assert result["ok"] is True
    assert result["content"] == "file text"
    assert fake.calls[0][1] == "https://openwebui.example.test/api/v1/files/file1/data/content"


def test_list_models_calls_api_models(monkeypatch):
    """モデル一覧取得が /api/models を呼び出すことを検証する。"""
    set_required_env(monkeypatch)
    fake = FakeRequests()
    fake.get_queue.append(FakeResponse(200, {"models": [{"id": "llama3.2"}]}))
    monkeypatch.setattr(client, "requests", fake)

    result = client.list_models()

    assert result["ok"] is True
    assert result["models"][0]["id"] == "llama3.2"
    assert fake.calls[0][1] == "https://openwebui.example.test/api/models"


def test_list_all_chats_pages_until_short_page(monkeypatch):
    """短いページに到達するまでチャット一覧を取得することを検証する。"""
    set_required_env(monkeypatch)
    fake = FakeRequests()
    fake.get_queue.append(FakeResponse(200, {"chats": [{"id": "chat1"}, {"id": "chat2"}]}))
    fake.get_queue.append(FakeResponse(200, {"chats": [{"id": "chat3"}]}))
    monkeypatch.setattr(client, "requests", fake)

    result = client.list_all_chats(page_size=2)

    assert result["ok"] is True
    assert result["count"] == 3
    assert fake.calls[0][1] == "https://openwebui.example.test/api/v1/chats?skip=0&limit=2&include_archived=False"
    assert fake.calls[1][1] == "https://openwebui.example.test/api/v1/chats?skip=2&limit=2&include_archived=False"


def test_add_user_message_to_chat_updates_then_completes(monkeypatch):
    """既存チャット更新後に completion を呼び出すことを検証する。"""
    set_required_env(monkeypatch)
    fake = FakeRequests()
    fake.get_queue.append(
        FakeResponse(
            200,
            {
                "id": "chat1",
                "chat": {
                    "messages": [{"id": "old-a", "role": "assistant", "content": "hello", "childrenIds": []}],
                    "history": {
                        "currentId": "old-a",
                        "messages": {"old-a": {"id": "old-a", "role": "assistant", "content": "hello", "childrenIds": []}},
                    },
                },
            },
        )
    )
    fake.post_queue.append(FakeResponse(200, {"id": "chat1"}))
    fake.post_queue.append(FakeResponse(200, {"content": "done"}))
    monkeypatch.setattr(client, "requests", fake)

    result = client.add_user_message_to_chat("chat1", "next question", "llama3.2", stream=False)

    assert result["ok"] is True
    update_call = fake.calls[1]
    completion_call = fake.calls[2]
    assert update_call[1] == "https://openwebui.example.test/api/v1/chats/chat1"
    assert update_call[4]["chat"]["history"]["currentId"] == result["assistant_message_id"]
    assert completion_call[1] == "https://openwebui.example.test/api/chat/completions"
    assert completion_call[4]["chat_id"] == "chat1"
    assert completion_call[4]["id"] == result["assistant_message_id"]


def test_list_all_knowledge_hydrates_details(monkeypatch):
    """ナレッジ一覧が詳細情報で補完されることを検証する。"""
    set_required_env(monkeypatch)
    fake = FakeRequests()
    fake.get_queue.append(FakeResponse(200, [{"id": "kb1"}, {"id": "kb2"}]))
    fake.get_queue.append(FakeResponse(200, {"id": "kb1", "name": "One"}))
    fake.get_queue.append(FakeResponse(200, {"id": "kb2", "name": "Two"}))
    monkeypatch.setattr(client, "requests", fake)

    result = client.list_all_knowledge(include_details=True)

    assert result["ok"] is True
    assert result["knowledge"][0]["name"] == "One"
    assert fake.calls[0][1] == "https://openwebui.example.test/api/v1/knowledge"
    assert fake.calls[1][1] == "https://openwebui.example.test/api/v1/knowledge/kb1"


def test_empty_content_is_not_sent(monkeypatch):
    """空本文の投稿では API を呼ばないことを検証する。"""
    set_required_env(monkeypatch)
    fake = FakeRequests()
    monkeypatch.setattr(client, "requests", fake)

    result = client.post_message("channel-id-1234567890", "")

    assert result["ok"] is False
    assert fake.calls == []


def test_http_error_statuses_return_false(monkeypatch):
    """HTTP エラーが ok=false として返ることを検証する。"""
    for status_code in [401, 403, 404, 500]:
        set_required_env(monkeypatch)
        fake = FakeRequests()
        fake.get_queue.append(FakeResponse(status_code, {"error": "failed"}, "failed"))
        monkeypatch.setattr(client, "requests", fake)

        result = client.list_channels()

        assert result["ok"] is False
        assert result["status_code"] == status_code


def test_api_key_is_redacted_from_error(monkeypatch):
    """エラーレスポンスから API キーが伏せられることを検証する。"""
    set_required_env(monkeypatch)
    fake = FakeRequests()
    fake.get_queue.append(FakeResponse(401, None, "bad key secret-api-key"))
    monkeypatch.setattr(client, "requests", fake)

    result = client.list_channels()

    assert result["ok"] is False
    assert "secret-api-key" not in result["response_text"]
    assert "[REDACTED]" in result["response_text"]


def test_post_endpoint_template_can_be_overridden(monkeypatch):
    """投稿エンドポイントテンプレートを環境変数で差し替えられることを検証する。"""
    set_required_env(monkeypatch)
    monkeypatch.setenv("OPEN_WEBUI_CHANNELS_POST_PATH_TEMPLATE", "/api/channels/{channel_id}/posts")
    fake = FakeRequests()
    fake.post_queue.append(FakeResponse(200, {"message_id": "msg2"}))
    monkeypatch.setattr(client, "requests", fake)

    result = client.post_message("channel-id-1234567890", "hello")

    assert result["ok"] is True
    assert fake.calls[0][1] == "https://openwebui.example.test/api/channels/channel-id-1234567890/posts"


def test_dry_run_does_not_call_api(monkeypatch):
    """dry-run 投稿では API を呼ばずリクエスト形状を返すことを検証する。"""
    set_required_env(monkeypatch)
    fake = FakeRequests()
    monkeypatch.setattr(client, "requests", fake)

    result = client.post_message("channel-id-1234567890", "hello", dry_run=True)

    assert result["ok"] is True
    assert result["dry_run"] is True
    assert fake.calls == []


def test_dry_run_channel_name_does_not_resolve_with_api(monkeypatch):
    """dry-run のチャネル名指定では API 解決を行わないことを検証する。"""
    monkeypatch.delenv("OPEN_WEBUI_BASE_URL", raising=False)
    monkeypatch.delenv("OPEN_WEBUI_API_KEY", raising=False)
    fake = FakeRequests()
    monkeypatch.setattr(client, "requests", fake)

    result = client.post_message("#daily-report", "hello", dry_run=True)

    assert result["ok"] is True
    assert result["channel_id"] == "daily-report"
    assert result["request"]["requires_resolution"] is True
    assert fake.calls == []
