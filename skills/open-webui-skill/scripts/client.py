"""Open WebUI API helpers for models, chats, knowledge, channels, and files."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any

try:  # pragma: no cover - runtime convenience when HTTPX exists.
    import httpx
except ImportError:  # pragma: no cover - standard-library fallback.
    requests = None
else:
    requests: Any = httpx

DEFAULT_CHANNELS_LIST_PATH = "/api/v1/channels/"
DEFAULT_POST_PATH_TEMPLATE = "/api/v1/channels/{channel_id}/messages/post"
DEFAULT_MODELS_PATH = "/api/models"
DEFAULT_CHATS_PATH = "/api/v1/chats"
DEFAULT_CHATS_ALL_DB_PATH = "/api/v1/chats/all/db"
DEFAULT_KNOWLEDGE_PATH = "/api/v1/knowledge"
DEFAULT_TIMEOUT_SECONDS = 30
DEFAULT_MAX_MESSAGE_CHARS = 20000
DEFAULT_MAX_RETRIES = 3
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{15,}$")
UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


class _UrllibResponse:
    """Minimal response object compatible with the requests response API used here."""

    def __init__(self, status_code: int, text: str):
        """Store an HTTP status code and response text."""
        self.status_code = status_code
        self.text = text

    def json(self) -> Any:
        """Parse the response text as JSON."""
        return json.loads(self.text)


class _UrllibRequests:
    """Small urllib-backed subset of the requests API."""

    @staticmethod
    def get(
        url: str,
        headers: dict[str, str] | None = None,
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> _UrllibResponse:
        """Send a GET request."""
        request = urllib.request.Request(url, headers=headers or {}, method="GET")
        return _UrllibRequests._open(request, timeout)

    @staticmethod
    def post(
        url: str,
        headers: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> _UrllibResponse:
        """Send a POST request with an optional JSON body."""
        body = None if json is None else __import__("json").dumps(json).encode("utf-8")
        request = urllib.request.Request(
            url, data=body, headers=headers or {}, method="POST"
        )
        return _UrllibRequests._open(request, timeout)

    @staticmethod
    def delete(
        url: str,
        headers: dict[str, str] | None = None,
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> _UrllibResponse:
        """Send a DELETE request."""
        request = urllib.request.Request(url, headers=headers or {}, method="DELETE")
        return _UrllibRequests._open(request, timeout)

    @staticmethod
    def _open(request: urllib.request.Request, timeout: int) -> _UrllibResponse:
        """Open a urllib request and normalize HTTP errors into response objects."""
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return _UrllibResponse(
                    response.status, response.read().decode("utf-8", errors="replace")
                )
        except urllib.error.HTTPError as exc:
            return _UrllibResponse(
                exc.code, exc.read().decode("utf-8", errors="replace")
            )


def _http_client() -> Any:
    """Return requests when available, otherwise the urllib fallback."""
    return requests or _UrllibRequests


def _env_int(
    name: str, default: int, *, minimum: int | None = None, maximum: int | None = None
) -> int:
    """Read an integer environment variable with optional bounds."""
    value = os.getenv(name)
    try:
        parsed = int(value) if value else default
    except ValueError:
        parsed = default
    if minimum is not None:
        parsed = max(minimum, parsed)
    if maximum is not None:
        parsed = min(maximum, parsed)
    return parsed


def _base_url() -> str | None:
    """Return the configured Open WebUI base URL without a trailing slash."""
    value = os.getenv("OPEN_WEBUI_BASE_URL", "").strip()
    return value.rstrip("/") if value else None


def _api_key() -> str | None:
    """Return the configured Open WebUI API key."""
    return os.getenv("OPEN_WEBUI_API_KEY", "").strip() or None


def _timeout() -> int:
    """Return the configured HTTP timeout in seconds."""
    return _env_int("OPEN_WEBUI_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS, minimum=1)


def _max_message_chars() -> int:
    """Return the maximum allowed channel post size."""
    return _env_int(
        "OPEN_WEBUI_MAX_MESSAGE_CHARS", DEFAULT_MAX_MESSAGE_CHARS, minimum=1
    )


def _max_retries() -> int:
    """Return the configured retry count for transient failures."""
    return _env_int("OPEN_WEBUI_MAX_RETRIES", DEFAULT_MAX_RETRIES, minimum=1, maximum=3)


def _headers(api_key: str) -> dict[str, str]:
    """Build authentication and JSON headers for Open WebUI API calls."""
    return {
        "Accept": "application/json",
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


def _redact(value: object) -> str:
    """Redact the configured API key from text before returning it."""
    text = str(value)
    api_key = _api_key()
    if api_key:
        text = text.replace(api_key, "[REDACTED]")
    return text


def _config_error() -> dict[str, Any] | None:
    """Return a structured error when required Open WebUI settings are missing."""
    missing = []
    if not _base_url():
        missing.append("OPEN_WEBUI_BASE_URL")
    if not _api_key():
        missing.append("OPEN_WEBUI_API_KEY")
    if missing:
        return {
            "ok": False,
            "error": f"Missing required environment variable(s): {', '.join(missing)}",
        }
    return None


def _parse_response(response: Any) -> tuple[Any | None, str]:
    """Return parsed JSON and raw response text for an HTTP response."""
    text = getattr(response, "text", "") or ""
    try:
        return response.json(), text
    except Exception:
        return None, text


def _error_message(status_code: int, payload: Any, text: str) -> str:
    """Build a redacted human-readable API error message."""
    if isinstance(payload, dict):
        detail = payload.get("detail") or payload.get("error") or payload.get("message")
        if detail:
            return f"Open WebUI API returned HTTP {status_code}: {_redact(detail)}"
    if text:
        return f"Open WebUI API returned HTTP {status_code}: {_redact(text)[:500]}"
    return f"Open WebUI API returned HTTP {status_code}"


def _request(
    method: str, path: str, body: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Send an authenticated Open WebUI request with retry handling."""
    config_error = _config_error()
    if config_error:
        return config_error

    base_url = _base_url()
    api_key = _api_key()
    assert base_url and api_key
    url = f"{base_url}{path}"
    client = _http_client()
    last_error: dict[str, Any] | None = None

    for attempt in range(1, _max_retries() + 1):
        try:
            if method == "GET":
                response = client.get(
                    url, headers=_headers(api_key), timeout=_timeout()
                )
            elif method == "POST":
                response = client.post(
                    url, headers=_headers(api_key), json=body or {}, timeout=_timeout()
                )
            elif method == "DELETE":
                response = client.delete(
                    url, headers=_headers(api_key), timeout=_timeout()
                )
            else:
                return {"ok": False, "error": f"Unsupported HTTP method: {method}"}

            status_code = int(getattr(response, "status_code", 0))
            payload, text = _parse_response(response)
            if 200 <= status_code < 300:
                return {
                    "ok": True,
                    "status_code": status_code,
                    "json": payload,
                    "text": _redact(text),
                }

            last_error = {
                "ok": False,
                "error": _error_message(status_code, payload, text),
                "status_code": status_code,
                "response_text": _redact(text),
            }
            if isinstance(payload, dict):
                last_error["response"] = json.loads(
                    _redact(json.dumps(payload, ensure_ascii=False))
                )
            if status_code < 500 or attempt >= _max_retries():
                return last_error
        except Exception as exc:
            last_error = {"ok": False, "error": _redact(exc)}
            if attempt >= _max_retries():
                return last_error

        time.sleep(0.1 * (2 ** (attempt - 1)))

    return last_error or {"ok": False, "error": "Open WebUI request failed"}


def _path_with_query(path: str, query: dict[str, Any]) -> str:
    """Append non-empty query parameters to a path."""
    clean = {key: value for key, value in query.items() if value is not None}
    if not clean:
        return path
    return f"{path}?{urllib.parse.urlencode(clean)}"


def _ensure_path(path: str) -> str:
    """Ensure a route path starts with a slash."""
    return path if path.startswith("/") else f"/{path}"


def _json_file_or_text(value: str | None) -> dict[str, Any]:
    """Parse a JSON object from inline text or a file path."""
    if not value:
        return {}
    stripped = value.lstrip()
    if stripped.startswith("{"):
        text = value
    else:
        path = Path(value)
        text = path.read_text(encoding="utf-8") if path.exists() else value
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError("JSON payload must be an object")
    return payload


def _items_from_payload(payload: Any, *keys: str) -> list[Any]:
    """Extract a list of items from common Open WebUI response shapes."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in keys:
            value = payload.get(key)
            if isinstance(value, list):
                return value
        data = payload.get("data")
        if isinstance(data, list):
            return data
    return []


def list_models() -> dict[str, Any]:
    """Fetch all models visible to the configured Open WebUI user."""
    path = _ensure_path(os.getenv("OPEN_WEBUI_MODELS_PATH", DEFAULT_MODELS_PATH))
    result = _request("GET", path)
    if not result.get("ok"):
        return result
    payload = result.get("json")
    return {
        "ok": True,
        "models": _items_from_payload(payload, "models"),
        "response": payload,
    }


def get_model(model_id: str) -> dict[str, Any]:
    """Fetch model details by ID/name."""
    path = _path_with_query("/api/v1/models/model", {"id": model_id})
    result = _request("GET", path)
    if not result.get("ok"):
        return result
    return {"ok": True, "model_id": model_id, "model": result.get("json")}


def list_chats(
    skip: int = 0, limit: int = 50, include_archived: bool = False
) -> dict[str, Any]:
    """Fetch chats visible to the configured Open WebUI user."""
    path = _path_with_query(
        DEFAULT_CHATS_PATH,
        {"skip": skip, "limit": limit, "include_archived": include_archived},
    )
    result = _request("GET", path)
    if not result.get("ok"):
        return result
    payload = result.get("json")
    return {
        "ok": True,
        "chats": _items_from_payload(payload, "chats"),
        "response": payload,
    }


def list_all_chats(
    page_size: int = 100, max_pages: int = 100, include_archived: bool = False
) -> dict[str, Any]:
    """Fetch all visible chats by paging until a short page is returned."""
    chats: list[Any] = []
    pages = 0
    for page in range(max_pages):
        skip = page * page_size
        result = list_chats(
            skip=skip, limit=page_size, include_archived=include_archived
        )
        if not result.get("ok"):
            return result
        page_items = result.get("chats", [])
        if not isinstance(page_items, list):
            page_items = []
        chats.extend(page_items)
        pages += 1
        if len(page_items) < page_size:
            break
    return {"ok": True, "chats": chats, "count": len(chats), "pages": pages}


def list_all_db_chats() -> dict[str, Any]:
    """Fetch all chats in the database. Requires an admin API key and admin export access."""
    path = _ensure_path(
        os.getenv("OPEN_WEBUI_CHATS_ALL_DB_PATH", DEFAULT_CHATS_ALL_DB_PATH)
    )
    result = _request("GET", path)
    if not result.get("ok"):
        return result
    payload = result.get("json")
    return {
        "ok": True,
        "chats": _items_from_payload(payload, "chats"),
        "response": payload,
    }


def get_chat(chat_id: str) -> dict[str, Any]:
    """Fetch one chat by ID."""
    result = _request("GET", f"{DEFAULT_CHATS_PATH}/{urllib.parse.quote(chat_id)}")
    if not result.get("ok"):
        return result
    return {"ok": True, "chat_id": chat_id, "chat": result.get("json")}


def create_chat(chat: dict[str, Any]) -> dict[str, Any]:
    """Create a chat using Open WebUI's UI-compatible chat payload."""
    result = _request("POST", f"{DEFAULT_CHATS_PATH}/new", {"chat": chat})
    if not result.get("ok"):
        return result
    payload = result.get("json")
    chat_id = payload.get("id") if isinstance(payload, dict) else None
    return {"ok": True, "chat_id": chat_id, "response": payload}


def update_chat(chat_id: str, chat: dict[str, Any]) -> dict[str, Any]:
    """Update an existing chat with a partial or full chat payload."""
    result = _request(
        "POST", f"{DEFAULT_CHATS_PATH}/{urllib.parse.quote(chat_id)}", {"chat": chat}
    )
    if not result.get("ok"):
        return result
    return {"ok": True, "chat_id": chat_id, "response": result.get("json")}


def post_chat_completion(payload: dict[str, Any]) -> dict[str, Any]:
    """Trigger a model completion for a chat or stateless message list."""
    result = _request("POST", "/api/chat/completions", payload)
    if not result.get("ok"):
        return result
    return {"ok": True, "response": result.get("json"), "text": result.get("text")}


def add_user_message_to_chat(
    chat_id: str, content: str, model: str, *, stream: bool = True
) -> dict[str, Any]:
    """Append a user message plus assistant placeholder, then trigger completion."""
    if not content or not content.strip():
        return {"ok": False, "error": "content is required"}
    chat_result = get_chat(chat_id)
    if not chat_result.get("ok"):
        return chat_result

    chat_payload = chat_result.get("chat")
    chat_data = (
        chat_payload.get("chat")
        if isinstance(chat_payload, dict) and isinstance(chat_payload.get("chat"), dict)
        else chat_payload
    )
    if not isinstance(chat_data, dict):
        return {
            "ok": False,
            "error": "chat payload is not an object",
            "details": chat_payload,
        }

    history = chat_data.setdefault("history", {})
    if not isinstance(history, dict):
        return {"ok": False, "error": "chat history is not an object"}
    messages_by_id = history.setdefault("messages", {})
    if not isinstance(messages_by_id, dict):
        return {"ok": False, "error": "chat history.messages is not an object"}

    previous_id = str(history.get("currentId") or "")
    user_msg_id = str(uuid.uuid4())
    assistant_msg_id = str(uuid.uuid4())
    timestamp = int(time.time())
    user_message = {
        "id": user_msg_id,
        "role": "user",
        "content": content,
        "parentId": previous_id or None,
        "childrenIds": [assistant_msg_id],
        "timestamp": timestamp,
        "models": [model],
    }
    assistant_message = {
        "id": assistant_msg_id,
        "role": "assistant",
        "content": "",
        "parentId": user_msg_id,
        "childrenIds": [],
        "model": model,
        "modelName": model,
        "modelIdx": 0,
        "done": False,
        "timestamp": timestamp + 1,
    }
    if previous_id and isinstance(messages_by_id.get(previous_id), dict):
        previous_children = messages_by_id[previous_id].setdefault("childrenIds", [])
        if isinstance(previous_children, list) and user_msg_id not in previous_children:
            previous_children.append(user_msg_id)
    messages_by_id[user_msg_id] = user_message
    messages_by_id[assistant_msg_id] = assistant_message
    history["currentId"] = assistant_msg_id

    flat_messages = chat_data.setdefault("messages", [])
    if isinstance(flat_messages, list):
        flat_messages.extend([user_message, assistant_message])
    chat_data["models"] = chat_data.get("models") or [model]

    update_result = update_chat(chat_id, chat_data)
    if not update_result.get("ok"):
        return update_result

    completion_messages = []
    for message in (
        flat_messages if isinstance(flat_messages, list) else messages_by_id.values()
    ):
        if (
            isinstance(message, dict)
            and message.get("role") in {"user", "assistant", "system"}
            and message.get("content")
        ):
            completion_messages.append(
                {"role": message["role"], "content": message["content"]}
            )
    if not completion_messages or completion_messages[-1].get("content") != content:
        completion_messages.append({"role": "user", "content": content})

    completion_result = post_chat_completion(
        {
            "chat_id": chat_id,
            "id": assistant_msg_id,
            "messages": completion_messages,
            "model": model,
            "stream": stream,
            "session_id": str(uuid.uuid4()),
        }
    )
    return {
        "ok": completion_result.get("ok", False),
        "chat_id": chat_id,
        "user_message_id": user_msg_id,
        "assistant_message_id": assistant_msg_id,
        "update": update_result,
        "completion": completion_result,
    }


def list_knowledge() -> dict[str, Any]:
    """Fetch knowledge bases visible to the configured Open WebUI user."""
    result = _request("GET", DEFAULT_KNOWLEDGE_PATH)
    if not result.get("ok"):
        return result
    payload = result.get("json")
    return {
        "ok": True,
        "knowledge": _items_from_payload(payload, "knowledge"),
        "response": payload,
    }


def get_knowledge(knowledge_id: str) -> dict[str, Any]:
    """Fetch one knowledge base by ID."""
    result = _request(
        "GET", f"{DEFAULT_KNOWLEDGE_PATH}/{urllib.parse.quote(knowledge_id)}"
    )
    if not result.get("ok"):
        return result
    return {"ok": True, "knowledge_id": knowledge_id, "knowledge": result.get("json")}


def list_all_knowledge(include_details: bool = False) -> dict[str, Any]:
    """Fetch all visible knowledge bases, optionally hydrating each by ID."""
    result = list_knowledge()
    if not result.get("ok"):
        return result
    items = result.get("knowledge", [])
    if not include_details:
        return {"ok": True, "knowledge": items, "count": len(items)}
    detailed = []
    for item in items:
        if not isinstance(item, dict) or not item.get("id"):
            detailed.append(item)
            continue
        detail = get_knowledge(str(item["id"]))
        detailed.append(
            detail.get("knowledge")
            if detail.get("ok")
            else {"summary": item, "error": detail}
        )
    return {"ok": True, "knowledge": detailed, "count": len(detailed)}


def _channels_from_payload(payload: Any) -> list[dict[str, Any]]:
    """Normalize channel list payloads into dictionaries with id and name."""
    if isinstance(payload, list):
        candidates = payload
    elif isinstance(payload, dict):
        candidates = (
            payload.get("channels")
            if isinstance(payload.get("channels"), list)
            else payload.get("data", [])
        )
    else:
        candidates = []

    channels: list[dict[str, Any]] = []
    for item in candidates or []:
        if not isinstance(item, dict):
            continue
        channel_id = item.get("id") or item.get("channel_id")
        if not channel_id:
            continue
        channels.append(
            {
                "id": str(channel_id),
                "name": str(item.get("name") or item.get("title") or ""),
                "description": item.get("description"),
                "raw": item,
            }
        )
    return channels


def list_channels() -> dict[str, Any]:
    """Fetch channels visible to the configured Open WebUI user."""
    path = os.getenv("OPEN_WEBUI_CHANNELS_LIST_PATH", DEFAULT_CHANNELS_LIST_PATH)
    if not path.startswith("/"):
        path = f"/{path}"
    result = _request("GET", path)
    if not result.get("ok"):
        return result
    return {"ok": True, "channels": _channels_from_payload(result.get("json"))}


def _normalize_channel_name(channel: str) -> str:
    """Normalize a user-facing channel name."""
    return channel.strip().lstrip("#").strip()


def _looks_like_channel_id(channel: str) -> bool:
    """Return whether a channel string looks like an Open WebUI channel ID."""
    value = channel.strip()
    return bool(UUID_RE.match(value) or ID_RE.match(value))


def resolve_channel_id(channel: str) -> dict[str, Any]:
    """Resolve a channel ID or #channel-name to an Open WebUI channel ID."""
    if not channel or not channel.strip():
        default_channel = os.getenv("OPEN_WEBUI_DEFAULT_CHANNEL", "").strip()
        if not default_channel:
            return {"ok": False, "error": "channel is required"}
        channel = default_channel

    value = channel.strip()
    if not value.startswith("#") and _looks_like_channel_id(value):
        return {"ok": True, "channel_id": value, "channel_name": None}

    channel_name = _normalize_channel_name(value)
    channels_result = list_channels()
    if not channels_result.get("ok"):
        return {
            "ok": False,
            "error": channels_result.get("error", "failed to list channels"),
            "status_code": channels_result.get("status_code"),
            "response_text": channels_result.get("response_text"),
        }

    for item in channels_result.get("channels", []):
        if item.get("name") == channel_name:
            return {
                "ok": True,
                "channel_id": str(item["id"]),
                "channel_name": channel_name,
            }

    return {"ok": False, "error": f"channel not found: {channel_name}"}


def _post_path(channel_id: str) -> str:
    """Build the channel message post path for a channel ID."""
    template = os.getenv(
        "OPEN_WEBUI_CHANNELS_POST_PATH_TEMPLATE", DEFAULT_POST_PATH_TEMPLATE
    )
    path = template.format(channel_id=channel_id)
    return path if path.startswith("/") else f"/{path}"


def _message_id(payload: Any) -> str | None:
    """Extract a message ID from common post response shapes."""
    if not isinstance(payload, dict):
        return None
    for key in ("id", "message_id"):
        if payload.get(key):
            return str(payload[key])
    data = payload.get("data")
    if isinstance(data, dict):
        for key in ("id", "message_id"):
            if data.get(key):
                return str(data[key])
    return None


def get_channel(channel: str) -> dict[str, Any]:
    """Fetch channel metadata by channel ID or name."""
    resolved = resolve_channel_id(channel)
    if not resolved.get("ok"):
        return resolved
    channel_id = str(resolved["channel_id"])
    result = _request("GET", f"/api/v1/channels/{channel_id}")
    if not result.get("ok"):
        return result
    return {"ok": True, "channel_id": channel_id, "channel": result.get("json")}


def list_channel_messages(
    channel: str,
    skip: int = 0,
    limit: int = 50,
    include_threads: bool = False,
) -> dict[str, Any]:
    """Fetch channel messages, optionally including thread replies per message."""
    resolved = resolve_channel_id(channel)
    if not resolved.get("ok"):
        return resolved
    channel_id = str(resolved["channel_id"])
    path = _path_with_query(
        f"/api/v1/channels/{channel_id}/messages", {"skip": skip, "limit": limit}
    )
    result = _request("GET", path)
    if not result.get("ok"):
        return result

    messages = result.get("json")
    if not isinstance(messages, list):
        messages = []
    if include_threads:
        for message in messages:
            if not isinstance(message, dict) or not message.get("id"):
                continue
            thread = get_message_thread(channel_id, str(message["id"]))
            if thread.get("ok"):
                message["thread"] = thread.get("messages", [])
    return {"ok": True, "channel_id": channel_id, "messages": messages}


def list_all_channel_messages(
    channel: str,
    page_size: int = 100,
    max_pages: int = 100,
    include_threads: bool = False,
) -> dict[str, Any]:
    """Fetch all visible messages for one channel by paging until a short page is returned."""
    resolved = resolve_channel_id(channel)
    if not resolved.get("ok"):
        return resolved
    channel_id = str(resolved["channel_id"])
    messages: list[Any] = []
    pages = 0
    for page in range(max_pages):
        skip = page * page_size
        result = list_channel_messages(
            channel_id, skip=skip, limit=page_size, include_threads=include_threads
        )
        if not result.get("ok"):
            return result
        page_items = result.get("messages", [])
        if not isinstance(page_items, list):
            page_items = []
        messages.extend(page_items)
        pages += 1
        if len(page_items) < page_size:
            break
    return {
        "ok": True,
        "channel_id": channel_id,
        "messages": messages,
        "count": len(messages),
        "pages": pages,
    }


def get_message_thread(
    channel: str, message_id: str, skip: int = 0, limit: int = 50
) -> dict[str, Any]:
    """Fetch replies for a channel message thread."""
    resolved = resolve_channel_id(channel)
    if not resolved.get("ok"):
        return resolved
    channel_id = str(resolved["channel_id"])
    path = _path_with_query(
        f"/api/v1/channels/{channel_id}/messages/{message_id}/thread",
        {"skip": skip, "limit": limit},
    )
    result = _request("GET", path)
    if not result.get("ok"):
        return result
    messages = result.get("json")
    return {
        "ok": True,
        "channel_id": channel_id,
        "message_id": message_id,
        "messages": messages if isinstance(messages, list) else [],
    }


def get_file_metadata(file_id: str) -> dict[str, Any]:
    """Fetch metadata for an Open WebUI file."""
    result = _request("GET", f"/api/v1/files/{urllib.parse.quote(file_id)}")
    if not result.get("ok"):
        return result
    return {"ok": True, "file_id": file_id, "file": result.get("json")}


def get_file_content(file_id: str, *, data_content: bool = True) -> dict[str, Any]:
    """Fetch extracted text content, falling back to raw file endpoint when needed."""
    encoded = urllib.parse.quote(file_id)
    if data_content:
        result = _request("GET", f"/api/v1/files/{encoded}/data/content")
        if result.get("ok"):
            payload = result.get("json")
            content = payload.get("content") if isinstance(payload, dict) else None
            return {
                "ok": True,
                "file_id": file_id,
                "content": content or "",
                "response": payload,
            }
    result = _request("GET", f"/api/v1/files/{encoded}/content")
    if not result.get("ok"):
        return result
    return {
        "ok": True,
        "file_id": file_id,
        "content": result.get("text", ""),
        "response": result.get("json"),
    }


def post_message(
    channel: str,
    content: str,
    thread_id: str | None = None,
    metadata: dict | None = None,
    data: dict | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Post Markdown content to an Open WebUI channel."""
    if not content or not content.strip():
        return {"ok": False, "error": "content is required"}
    max_chars = _max_message_chars()
    if len(content) > max_chars:
        return {
            "ok": False,
            "error": f"content exceeds OPEN_WEBUI_MAX_MESSAGE_CHARS ({max_chars})",
        }

    meta = {"source": "hermes-agent", "skill": "open-webui-skill", **(metadata or {})}
    request_body: dict[str, Any] = {"content": content, "meta": meta}
    if data:
        request_body["data"] = data
    if thread_id:
        request_body["parent_id"] = thread_id

    if dry_run:
        preview_channel = (
            channel.strip()
            if channel
            else os.getenv("OPEN_WEBUI_DEFAULT_CHANNEL", "").strip()
        )
        if not preview_channel:
            return {"ok": False, "error": "channel is required"}
        preview_channel_id = (
            preview_channel
            if _looks_like_channel_id(preview_channel)
            else _normalize_channel_name(preview_channel)
        )
        return {
            "ok": True,
            "dry_run": True,
            "channel_id": preview_channel_id,
            "request": {
                "method": "POST",
                "path": _post_path(preview_channel_id),
                "channel_id": preview_channel_id,
                "body": request_body,
                "requires_resolution": not _looks_like_channel_id(preview_channel),
            },
        }

    resolved = resolve_channel_id(channel)
    if not resolved.get("ok"):
        return resolved

    channel_id = str(resolved["channel_id"])
    result = _request("POST", _post_path(channel_id), request_body)
    if not result.get("ok"):
        return result

    payload = result.get("json")
    response: dict[str, Any] = payload if isinstance(payload, dict) else {}
    output = {
        "ok": True,
        "channel_id": channel_id,
        "message_id": _message_id(payload),
        "response": response,
    }
    if not response and result.get("text"):
        output["response_text"] = str(result["text"])
    return output


def _read_content(path_or_text: str | None) -> str:
    """Read Markdown content from a path or return inline text."""
    if not path_or_text:
        return ""
    path = Path(path_or_text)
    if path.exists():
        return path.read_text(encoding="utf-8")
    return path_or_text


def _add_channel_subcommands(subparsers: argparse._SubParsersAction) -> None:
    """Register channel-related CLI subcommands."""
    channels_parser = subparsers.add_parser("channels")
    channels_subparsers = channels_parser.add_subparsers(
        dest="channels_command", required=True
    )

    channels_subparsers.add_parser("list")

    channel_get_parser = channels_subparsers.add_parser("get")
    channel_get_parser.add_argument("--channel", required=True)

    channel_resolve_parser = channels_subparsers.add_parser("resolve")
    channel_resolve_parser.add_argument("--channel", required=True)

    channel_messages_parser = channels_subparsers.add_parser("messages")
    channel_messages_parser.add_argument("--channel", required=True)
    channel_messages_parser.add_argument("--skip", type=int, default=0)
    channel_messages_parser.add_argument("--limit", type=int, default=50)
    channel_messages_parser.add_argument("--include-threads", action="store_true")
    channel_messages_parser.add_argument("--all", action="store_true")
    channel_messages_parser.add_argument("--page-size", type=int, default=100)
    channel_messages_parser.add_argument("--max-pages", type=int, default=100)

    channel_thread_parser = channels_subparsers.add_parser("thread")
    channel_thread_parser.add_argument("--channel", required=True)
    channel_thread_parser.add_argument("--message-id", required=True)
    channel_thread_parser.add_argument("--skip", type=int, default=0)
    channel_thread_parser.add_argument("--limit", type=int, default=50)

    channel_file_parser = channels_subparsers.add_parser("file-content")
    channel_file_parser.add_argument("--file-id", required=True)
    channel_file_parser.add_argument("--raw", action="store_true")

    channel_post_parser = channels_subparsers.add_parser("post")
    channel_post_parser.add_argument("--channel", required=True)
    channel_post_parser.add_argument("--file")
    channel_post_parser.add_argument("--content")
    channel_post_parser.add_argument("--thread-id")
    channel_post_parser.add_argument("--dry-run", action="store_true")


def _handle_channels_command(args: argparse.Namespace) -> dict[str, Any]:
    """Dispatch parsed channel CLI arguments to helper functions."""
    if args.channels_command == "list":
        return list_channels()
    if args.channels_command == "get":
        return get_channel(args.channel)
    if args.channels_command == "resolve":
        return resolve_channel_id(args.channel)
    if args.channels_command == "messages":
        if args.all:
            return list_all_channel_messages(
                args.channel,
                page_size=args.page_size,
                max_pages=args.max_pages,
                include_threads=args.include_threads,
            )
        return list_channel_messages(
            args.channel,
            skip=args.skip,
            limit=args.limit,
            include_threads=args.include_threads,
        )
    if args.channels_command == "thread":
        return get_message_thread(
            args.channel, args.message_id, skip=args.skip, limit=args.limit
        )
    if args.channels_command == "file-content":
        return get_file_content(args.file_id, data_content=not args.raw)
    if args.channels_command == "post":
        content = _read_content(args.file) if args.file else _read_content(args.content)
        return post_message(
            args.channel, content, thread_id=args.thread_id, dry_run=args.dry_run
        )
    return {"ok": False, "error": f"unknown channels command: {args.channels_command}"}


def _build_parser() -> argparse.ArgumentParser:
    """Build the Open WebUI helper CLI parser."""
    parser = argparse.ArgumentParser(description="Open WebUI helper.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    models_parser = subparsers.add_parser("models")
    models_subparsers = models_parser.add_subparsers(
        dest="models_command", required=True
    )
    models_subparsers.add_parser("list")
    model_get_parser = models_subparsers.add_parser("get")
    model_get_parser.add_argument("--model", required=True)

    chats_parser = subparsers.add_parser("chats")
    chats_subparsers = chats_parser.add_subparsers(dest="chats_command", required=True)
    chats_list_parser = chats_subparsers.add_parser("list")
    chats_list_parser.add_argument("--skip", type=int, default=0)
    chats_list_parser.add_argument("--limit", type=int, default=50)
    chats_list_parser.add_argument("--include-archived", action="store_true")
    chats_list_parser.add_argument("--all", action="store_true")
    chats_list_parser.add_argument("--all-users", action="store_true")
    chats_list_parser.add_argument("--page-size", type=int, default=100)
    chats_list_parser.add_argument("--max-pages", type=int, default=100)
    chat_get_parser = chats_subparsers.add_parser("get")
    chat_get_parser.add_argument("--chat-id", required=True)
    chat_create_parser = chats_subparsers.add_parser("create")
    chat_create_parser.add_argument("--chat-json", required=True)
    chat_update_parser = chats_subparsers.add_parser("update")
    chat_update_parser.add_argument("--chat-id", required=True)
    chat_update_parser.add_argument("--chat-json", required=True)
    chat_post_parser = chats_subparsers.add_parser("post")
    chat_post_parser.add_argument("--chat-id", required=True)
    chat_post_parser.add_argument("--content", required=True)
    chat_post_parser.add_argument("--model", required=True)
    chat_post_parser.add_argument("--no-stream", action="store_true")
    chat_completion_parser = chats_subparsers.add_parser("completion")
    chat_completion_parser.add_argument("--payload-json", required=True)

    knowledge_parser = subparsers.add_parser("knowledge")
    knowledge_subparsers = knowledge_parser.add_subparsers(
        dest="knowledge_command", required=True
    )
    knowledge_list_parser = knowledge_subparsers.add_parser("list")
    knowledge_list_parser.add_argument("--details", action="store_true")
    knowledge_get_parser = knowledge_subparsers.add_parser("get")
    knowledge_get_parser.add_argument("--knowledge-id", required=True)

    _add_channel_subcommands(subparsers)

    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the Open WebUI helper CLI."""
    args = _build_parser().parse_args(argv)
    if args.command == "models":
        if args.models_command == "list":
            result = list_models()
        elif args.models_command == "get":
            result = get_model(args.model)
        else:
            result = {
                "ok": False,
                "error": f"unknown models command: {args.models_command}",
            }
    elif args.command == "chats":
        if args.chats_command == "list":
            if args.all_users:
                result = list_all_db_chats()
            elif args.all:
                result = list_all_chats(
                    page_size=args.page_size,
                    max_pages=args.max_pages,
                    include_archived=args.include_archived,
                )
            else:
                result = list_chats(
                    skip=args.skip,
                    limit=args.limit,
                    include_archived=args.include_archived,
                )
        elif args.chats_command == "get":
            result = get_chat(args.chat_id)
        elif args.chats_command == "create":
            result = create_chat(_json_file_or_text(args.chat_json))
        elif args.chats_command == "update":
            result = update_chat(args.chat_id, _json_file_or_text(args.chat_json))
        elif args.chats_command == "post":
            result = add_user_message_to_chat(
                args.chat_id, args.content, args.model, stream=not args.no_stream
            )
        elif args.chats_command == "completion":
            result = post_chat_completion(_json_file_or_text(args.payload_json))
        else:
            result = {
                "ok": False,
                "error": f"unknown chats command: {args.chats_command}",
            }
    elif args.command == "knowledge":
        if args.knowledge_command == "list":
            result = list_all_knowledge(include_details=args.details)
        elif args.knowledge_command == "get":
            result = get_knowledge(args.knowledge_id)
        else:
            result = {
                "ok": False,
                "error": f"unknown knowledge command: {args.knowledge_command}",
            }
    elif args.command == "channels":
        result = _handle_channels_command(args)
    else:
        result = {"ok": False, "error": f"unknown command: {args.command}"}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
