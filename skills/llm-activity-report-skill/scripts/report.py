"""Collect Open WebUI activity and draft Japanese activity report metadata."""

from __future__ import annotations

import argparse
import base64
import importlib.util
import json
import os
import sys
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast


def _load_open_webui_client() -> Any:
    """Load the sibling open-webui-skill client module."""
    client_path = (
        Path(__file__).resolve().parents[2]
        / "open-webui-skill"
        / "scripts"
        / "client.py"
    )
    spec = importlib.util.spec_from_file_location(
        "openwebui_client_for_activity_report", client_path
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"Unable to load open-webui-skill client from {client_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_open_webui_client = _load_open_webui_client()
list_channel_messages = _open_webui_client.list_channel_messages
list_channels = _open_webui_client.list_channels
list_all_channel_messages = _open_webui_client.list_all_channel_messages
list_all_chats = _open_webui_client.list_all_chats
list_all_db_chats = getattr(_open_webui_client, "list_all_db_chats", None)
get_chat = _open_webui_client.get_chat
list_all_knowledge = _open_webui_client.list_all_knowledge


def _parse_time(value: str | None) -> datetime | None:
    """Parse an ISO-like timestamp into UTC."""
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _message_time_ns(message: dict[str, Any]) -> datetime | None:
    """Parse an Open WebUI nanosecond message timestamp."""
    value = message.get("created_at")
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(int(value) / 1_000_000_000, tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def _parse_openwebui_time(value: Any) -> datetime | None:
    """Parse common Open WebUI timestamp formats into UTC."""
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(text)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except ValueError:
            try:
                number = float(text)
            except ValueError:
                return None
        else:
            return None
    else:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None

    if number > 1_000_000_000_000_000_000:
        number = number / 1_000_000_000
    elif number > 1_000_000_000_000:
        number = number / 1000
    try:
        return datetime.fromtimestamp(number, tz=timezone.utc)
    except (OSError, OverflowError, ValueError):
        return None


def _frontend_base_url() -> str:
    """Return the browser-facing Open WebUI base URL."""
    return (
        os.getenv("OPEN_WEBUI_PUBLIC_URL", "").strip()
        or os.getenv("OPEN_WEBUI_EXTERNAL_URL", "").strip()
    ).rstrip("/")


def _frontend_url(path: str) -> str:
    """Build a browser-facing URL for an Open WebUI path."""
    base_url = _frontend_base_url()
    if base_url:
        return f"{base_url}{path}"
    return path


def _item_time(item: dict[str, Any]) -> datetime | None:
    """Find the first parseable timestamp on an Open WebUI item."""
    for key in (
        "created_at",
        "createdAt",
        "created",
        "timestamp",
        "time",
        "updated_at",
        "updatedAt",
    ):
        parsed = _parse_openwebui_time(item.get(key))
        if parsed is not None:
            return parsed
    return None


def _in_range(dt: datetime | None, since_dt: datetime, until_dt: datetime) -> bool:
    """Return whether a timestamp falls inside the inclusive report range."""
    return dt is not None and since_dt <= dt <= until_dt


def _default_since() -> datetime:
    """Return the default report start time."""
    return datetime.now(timezone.utc) - timedelta(hours=24)


def _user_label(message: dict[str, Any]) -> str:
    """Return a display label for a channel message user."""
    user = message.get("user")
    if isinstance(user, dict):
        return str(
            user.get("name")
            or user.get("email")
            or user.get("id")
            or message.get("user_id")
            or "unknown"
        )
    return str(message.get("user_id") or "unknown")


def _extract_file_ids(message: dict[str, Any]) -> list[str]:
    """Extract attached Open WebUI file IDs from a message."""
    file_ids: list[str] = []
    data = message.get("data")
    if isinstance(data, dict):
        for item in data.get("files") or []:
            if isinstance(item, dict) and item.get("id"):
                file_ids.append(str(item["id"]))
    return file_ids


def _message_text(message: dict[str, Any]) -> str:
    """Return single-line text content for a message."""
    return (
        str(message.get("content") or message.get("text") or "")
        .strip()
        .replace("\n", " ")
    )


def _chat_owner(chat: dict[str, Any]) -> str:
    """Return a display label for a chat owner."""
    user = chat.get("user")
    if isinstance(user, dict):
        return str(
            user.get("name")
            or user.get("email")
            or user.get("id")
            or chat.get("user_id")
            or "unknown"
        )
    return str(
        chat.get("user_name")
        or chat.get("user_email")
        or chat.get("user_id")
        or chat.get("owner")
        or "unknown"
    )


def _chat_title(chat: dict[str, Any]) -> str:
    """Return a display title for a chat."""
    chat_data = chat.get("chat")
    data = cast(dict[str, Any], chat_data) if isinstance(chat_data, dict) else chat
    return str(
        data.get("title")
        or chat.get("title")
        or chat.get("name")
        or chat.get("id")
        or "タイトル未設定"
    )


def _chat_messages(chat: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract chat messages from common Open WebUI history shapes."""
    data = chat.get("chat") if isinstance(chat.get("chat"), dict) else chat
    messages = data.get("messages") if isinstance(data, dict) else None
    if isinstance(messages, list):
        return [message for message in messages if isinstance(message, dict)]
    if isinstance(messages, dict):
        return [message for message in messages.values() if isinstance(message, dict)]
    history = data.get("history") if isinstance(data, dict) else None
    if isinstance(history, list):
        return [message for message in history if isinstance(message, dict)]
    if isinstance(history, dict) and isinstance(history.get("messages"), dict):
        return [
            message
            for message in history["messages"].values()
            if isinstance(message, dict)
        ]
    if isinstance(history, dict) and isinstance(history.get("messages"), list):
        return [message for message in history["messages"] if isinstance(message, dict)]
    return []


def _knowledge_name(item: dict[str, Any]) -> str:
    """Return a display name for a knowledge item."""
    return str(item.get("name") or item.get("title") or item.get("id") or "名称未設定")


def _dedupe_preserving_order(values: list[str]) -> list[str]:
    """Deduplicate strings while preserving their original order."""
    seen: set[str] = set()
    deduped: list[str] = []
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        deduped.append(value)
    return deduped


def _channel_url(channel_id: str) -> str:
    """Build a browser-facing channel URL."""
    quoted_id = urllib.parse.quote(str(channel_id), safe="")
    return _frontend_url(f"/channels/{quoted_id}")


def _channel_label(message: dict[str, Any], channel_id: str) -> str:
    """Return a Markdown label for a channel."""
    name = str(message.get("_channel_name") or "").strip().lstrip("#")
    if name:
        return f"#{name}"
    return f"#{channel_id}"


def _channel_markdown_link(message: dict[str, Any], channel_id: str) -> str:
    """Build a Markdown link for a channel without a message ID."""
    url = _channel_url(channel_id)
    return f"[{_channel_label(message, channel_id)}]({url})"


def _channel_links(messages: list[dict[str, Any]], limit: int = 5) -> str:
    """Return deduplicated channel Markdown links for messages."""
    links_by_url: dict[str, str] = {}
    for message in messages:
        channel_id = message.get("_channel_id")
        if channel_id:
            channel_id_text = str(channel_id)
            links_by_url.setdefault(
                _channel_url(channel_id_text),
                _channel_markdown_link(message, channel_id_text),
            )
    return ", ".join(list(links_by_url.values())[:limit]) or "なし"


def _truncate_text(text: str, limit: int) -> str:
    """Trim long source text for report input."""
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 20)].rstrip() + "\n...[truncated]"


def _summary_input(
    messages: list[dict[str, Any]],
    chat_messages: list[dict[str, Any]],
    knowledge_items: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build normalized source data for Agent-side summarization."""
    channel_items = []
    for message in messages:
        created = message.get("_created_dt")
        channel_items.append(
            {
                "channel": message.get("_channel_name")
                or message.get("_channel_id")
                or "unknown",
                "channel_id": message.get("_channel_id"),
                "channel_url": _channel_url(str(message.get("_channel_id")))
                if message.get("_channel_id")
                else None,
                "user": _user_label(message),
                "created_at": created.isoformat()
                if isinstance(created, datetime)
                else None,
                "content": _truncate_text(_message_text(message), 4000),
            }
        )
    chat_items = []
    for message in chat_messages:
        created = message.get("_created_dt")
        chat_items.append(
            {
                "user": message.get("_chat_owner") or "unknown",
                "created_at": created.isoformat()
                if isinstance(created, datetime)
                else None,
                "content": _truncate_text(_message_text(message), 4000),
            }
        )
    knowledge = []
    for item in knowledge_items:
        created = item.get("_created_dt")
        knowledge.append(
            {
                "name": _knowledge_name(item),
                "created_at": created.isoformat()
                if isinstance(created, datetime)
                else None,
                "id": item.get("id"),
            }
        )
    return {
        "channel_messages": channel_items,
        "chat_messages": chat_items,
        "knowledge": knowledge,
    }


def _collect_openwebui_activity(
    since_dt: datetime,
    until_dt: datetime,
    *,
    page_size: int = 100,
    max_pages: int = 100,
    include_threads: bool = True,
    include_archived_chats: bool = True,
) -> dict[str, Any]:
    """Collect channel messages, chat messages, and knowledge items in range."""
    errors: list[str] = []

    channels: list[dict[str, Any]] = []
    channel_messages: list[dict[str, Any]] = []
    channels_result = list_channels()
    if channels_result.get("ok"):
        channels = [
            item
            for item in channels_result.get("channels", [])
            if isinstance(item, dict)
        ]
        for channel in channels:
            channel_id = str(channel.get("id") or "")
            if not channel_id:
                continue
            messages_result = list_all_channel_messages(
                channel_id,
                page_size=page_size,
                max_pages=max_pages,
                include_threads=include_threads,
            )
            if not messages_result.get("ok"):
                errors.append(
                    f"channel:{channel.get('name') or channel_id}: {messages_result.get('error', '取得失敗')}"
                )
                continue
            for message in messages_result.get("messages", []):
                if not isinstance(message, dict):
                    continue
                created = _message_time_ns(message) or _item_time(message)
                if _in_range(created, since_dt, until_dt):
                    enriched = dict(message)
                    enriched["_channel_id"] = channel_id
                    enriched["_channel_name"] = channel.get("name") or channel_id
                    enriched["_created_dt"] = created
                    channel_messages.append(enriched)
    else:
        errors.append(f"channels: {channels_result.get('error', '取得失敗')}")

    chats: list[dict[str, Any]] = []
    chat_messages: list[dict[str, Any]] = []
    chats_result = (
        list_all_db_chats()
        if callable(list_all_db_chats)
        else {"ok": False, "error": "list_all_db_chats is unavailable"}
    )
    using_db_chats = bool(chats_result.get("ok"))
    if not using_db_chats:
        errors.append(
            "chats/all/db: "
            + str(
                chats_result.get("error")
                or "管理者向け全チャット取得に失敗したため、APIキーから見えるチャット一覧にフォールバックしました。"
            )
        )
        chats_result = list_all_chats(
            page_size=page_size,
            max_pages=max_pages,
            include_archived=include_archived_chats,
        )
    if chats_result.get("ok"):
        chat_summaries = chats_result.get("chats", [])
        if not isinstance(chat_summaries, list):
            chat_summaries = []
        for chat_summary in chat_summaries:
            if not isinstance(chat_summary, dict):
                continue
            chat_id = str(chat_summary.get("id") or chat_summary.get("chat_id") or "")
            detail = (
                {"ok": True, "chat": chat_summary}
                if using_db_chats
                else get_chat(chat_id)
                if chat_id
                else {"ok": True, "chat": chat_summary}
            )
            if not detail.get("ok"):
                errors.append(f"chat:{chat_id}: {detail.get('error', '取得失敗')}")
                continue
            chat = detail.get("chat")
            if not isinstance(chat, dict):
                continue
            chat.setdefault("id", chat_id or chat.get("id"))
            chat_time = _item_time(chat) or _item_time(chat_summary)
            messages_in_range = []
            for message in _chat_messages(chat):
                msg_time = _item_time(message) or chat_time
                if _in_range(msg_time, since_dt, until_dt):
                    enriched = dict(message)
                    enriched["_chat_id"] = str(chat.get("id") or chat_id)
                    enriched["_chat_title"] = _chat_title(chat)
                    enriched["_chat_owner"] = _chat_owner(chat)
                    enriched["_created_dt"] = msg_time
                    messages_in_range.append(enriched)
                    chat_messages.append(enriched)
            if messages_in_range or _in_range(chat_time, since_dt, until_dt):
                chat["_messages_in_range"] = messages_in_range
                chats.append(chat)
    else:
        errors.append(f"chats: {chats_result.get('error', '取得失敗')}")

    knowledge_items: list[dict[str, Any]] = []
    knowledge_result = list_all_knowledge(include_details=True)
    if knowledge_result.get("ok"):
        for item in knowledge_result.get("knowledge", []):
            if not isinstance(item, dict):
                continue
            created = _item_time(item)
            if _in_range(created, since_dt, until_dt):
                enriched = dict(item)
                enriched["_created_dt"] = created
                knowledge_items.append(enriched)
    else:
        errors.append(f"knowledge: {knowledge_result.get('error', '取得失敗')}")

    return {
        "channels": channels,
        "channel_messages": channel_messages,
        "chats": chats,
        "chat_messages": chat_messages,
        "knowledge": knowledge_items,
        "errors": errors,
    }


def _fetch_langfuse_traces(
    since_dt: datetime, until_dt: datetime, limit: int = 50
) -> dict[str, Any]:
    """Fetch Langfuse traces for the report window when credentials are configured."""
    host = os.getenv("LANGFUSE_HOST", "").rstrip("/")
    public_key = os.getenv("LANGFUSE_PUBLIC_KEY", "")
    secret_key = os.getenv("LANGFUSE_SECRET_KEY", "")
    if not (host and public_key and secret_key):
        return {
            "ok": False,
            "error": "この実行ではLangfuse認証情報が設定されていません。",
        }

    query = urllib.parse.urlencode(
        {
            "fromTimestamp": since_dt.isoformat().replace("+00:00", "Z"),
            "toTimestamp": until_dt.isoformat().replace("+00:00", "Z"),
            "limit": min(max(limit, 1), 100),
        }
    )
    token = base64.b64encode(f"{public_key}:{secret_key}".encode("utf-8")).decode(
        "ascii"
    )
    request = urllib.request.Request(
        f"{host}/api/public/traces?{query}",
        headers={"Authorization": f"Basic {token}", "Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
        data = payload.get("data") if isinstance(payload, dict) else payload
        traces = data if isinstance(data, list) else []
        return {"ok": True, "traces": traces, "count": len(traces)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _langfuse_lines(since_dt: datetime, until_dt: datetime) -> list[str]:
    """Format Langfuse trace counts as Markdown lines."""
    traces_result = _fetch_langfuse_traces(since_dt, until_dt)
    if not traces_result.get("ok"):
        return [
            f"- {traces_result.get('error', 'Langfuseデータを取得できませんでした。')}"
        ]

    traces = traces_result.get("traces", [])
    names = Counter(
        str(trace.get("name") or "unnamed")
        for trace in traces
        if isinstance(trace, dict)
    )
    users = Counter(
        str(trace.get("userId") or "unknown")
        for trace in traces
        if isinstance(trace, dict)
    )
    lines = [f"- 確認したトレース数: {len(traces)}"]
    if names:
        lines.append(
            "- 主なトレース名: "
            + ", ".join(f"{name} ({count})" for name, count in names.most_common(5))
        )
    if users:
        lines.append(
            "- 主なLangfuseユーザー: "
            + ", ".join(f"{user} ({count})" for user, count in users.most_common(5))
        )
    if not traces:
        lines.append("- 指定期間のLangfuseトレースは返されませんでした。")
    return lines


def generate_activity_report(
    channel: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 100,
    include_threads: bool = True,
    include_langfuse: bool = False,
    include_all_openwebui: bool = True,
    page_size: int = 100,
    max_pages: int = 100,
    include_archived_chats: bool = True,
) -> dict[str, Any]:
    """Fetch Open WebUI activity and produce a deterministic Markdown draft plus source data."""
    try:
        since_dt = _parse_time(since) or _default_since()
        until_dt = _parse_time(until) or datetime.now(timezone.utc)
        if include_all_openwebui:
            activity = _collect_openwebui_activity(
                since_dt,
                until_dt,
                page_size=page_size,
                max_pages=max_pages,
                include_threads=include_threads,
                include_archived_chats=include_archived_chats,
            )
            messages = activity["channel_messages"]
            channels = activity["channels"]
            chats = activity["chats"]
            chat_messages = activity["chat_messages"]
            knowledge_items = activity["knowledge"]
            errors = activity["errors"]
            channel_id = "all"
        else:
            target_channel = channel or os.getenv(
                "OPEN_WEBUI_DEFAULT_CHANNEL", "report"
            )
            messages_result = list_channel_messages(
                target_channel, limit=limit, include_threads=include_threads
            )
            if not messages_result.get("ok"):
                return {
                    "ok": False,
                    "error": messages_result.get(
                        "error", "チャンネル投稿の取得に失敗しました"
                    ),
                    "details": messages_result,
                }

            messages = []
            for message in messages_result.get("messages", []):
                if not isinstance(message, dict):
                    continue
                created = _message_time_ns(message) or _item_time(message)
                if _in_range(created, since_dt, until_dt):
                    enriched = dict(message)
                    enriched["_channel_id"] = messages_result.get(
                        "channel_id", target_channel
                    )
                    enriched["_channel_name"] = target_channel
                    enriched["_created_dt"] = created
                    messages.append(enriched)
            channels = [
                {
                    "id": messages_result.get("channel_id", target_channel),
                    "name": target_channel,
                }
            ]
            chats = []
            chat_messages = []
            knowledge_items = []
            errors = []
            channel_id = messages_result.get("channel_id", target_channel)

        by_user: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for message in messages:
            by_user[_user_label(message)].append(message)

        chat_by_user = Counter(
            str(message.get("_chat_owner") or "unknown") for message in chat_messages
        )
        chat_message_count = len(chat_messages)
        chat_count = len(chats)
        knowledge_count = len(knowledge_items)
        total_activity_count = len(messages) + chat_message_count + knowledge_count
        report_date = until_dt.astimezone().date().isoformat()
        lines = [
            "---",
            f"## 📊 アクティビティレポート ({report_date})",
            "",
            f"- 🔢 集計対象イベント数：{total_activity_count}件",
            f"- 📅 集計期間：{since_dt.isoformat()} 〜 {until_dt.isoformat()}",
            f"- 💬 チャネル投稿：{len(messages)}件 / 🤖 チャットメッセージ：{chat_message_count}件 / 📚 新規ナレッジ：{knowledge_count}件",
            "",
            "📝 取得した投稿本文をもとに、Skill呼び出し元のAgentが全体傾向を総合要約してください。",
            "",
            "### 🔥 本日の主要イベント",
        ]

        sorted_users = sorted(
            by_user.items(), key=lambda item: (-len(item[1]), item[0])
        )
        event_index = 1
        if messages:
            channel_counts = Counter(
                str(
                    message.get("_channel_name")
                    or message.get("_channel_id")
                    or "unknown"
                )
                for message in messages
            )
            top_channels = ", ".join(
                f"{name} ({count})" for name, count in channel_counts.most_common(5)
            )
            lines.extend(
                [
                    f"{event_index}. チャネル横断の活動",
                    f"   - 🧭 概要：{len(channels)}件のチャネルを確認し、期間内の投稿 {len(messages)} 件を集計しました。主要チャネルは {top_channels or 'なし'} です。",
                    f"   - 👥 関連ユーザー：{', '.join(user for user, _ in sorted_users[:5]) or 'なし'}",
                    f"   - 🔗 URL：{_channel_links(messages)}",
                ]
            )
            event_index += 1
        if chat_messages:
            top_chat_users = ", ".join(
                f"{user} ({count})" for user, count in chat_by_user.most_common(5)
            )
            lines.extend(
                [
                    f"{event_index}. ユーザーチャットの活動",
                    f"   - 🧭 概要：{chat_count}件のチャットから期間内のメッセージ {chat_message_count} 件を集計しました。",
                    f"   - 👥 関連ユーザー：{top_chat_users or 'unknown'}",
                    "   - 🔗 URL：なし（チャットは個人情報保護のためリンク非掲載）",
                ]
            )
            event_index += 1
        if knowledge_items:
            names = ", ".join(_knowledge_name(item) for item in knowledge_items[:5])
            lines.extend(
                [
                    f"{event_index}. 新規ナレッジの追加",
                    f"   - 🧭 概要：期間内に {knowledge_count} 件のナレッジが追加されました。主なナレッジは {names} です。",
                    "   - 👥 関連ユーザー：未特定（ナレッジ詳細確認）",
                    f"   - 🔗 URL：{', '.join(str(item.get('url') or item.get('id') or 'なし') for item in knowledge_items[:5])}",
                ]
            )
            event_index += 1
        if not messages and not chat_messages and not knowledge_items:
            lines.extend(
                [
                    "1. 主要イベントなし",
                    "   - 🧭 概要：指定期間内のチャネル投稿、チャット、ナレッジ追加は見つかりませんでした。",
                    "   - 👥 関連ユーザー：なし",
                    "   - 🔗 URL：なし",
                ]
            )
        elif len(lines) < 14 and sorted_users:
            for index, (user, user_messages) in enumerate(sorted_users[:3], start=1):
                snippets = [
                    str(msg.get("content") or "").strip().replace("\n", " ")[:100]
                    for msg in user_messages[:2]
                ]
                summary = (
                    " / ".join([text for text in snippets if text])
                    or "投稿内容の要約を生成できませんでした。"
                )
                related_posts = _channel_links(user_messages[:3])
                lines.extend(
                    [
                        f"{index}. {user}の主要アクティビティ",
                        f"   - 🧭 概要：{summary}",
                        f"   - 👥 関連ユーザー：{user}",
                        f"   - 🔗 URL：{related_posts}",
                    ]
                )
        action_words = Counter()
        for message in [*messages, *chat_messages]:
            text = _message_text(message).lower()
            for word in (
                "deploy",
                "fix",
                "review",
                "investigate",
                "release",
                "error",
                "incident",
                "cost",
                "model",
                "障害",
                "費用",
                "調査",
                "修正",
                "リリース",
            ):
                if word in text:
                    action_words[word] += 1

        lines.extend(["", "### ✅ 推奨アクションアイテム"])
        if action_words:
            for index, (word, count) in enumerate(action_words.most_common(3), start=1):
                lines.extend(
                    [
                        f"{index}. `{word}` に関する確認",
                        f"   - 🧭 概要：`{word}` が {count} 件の投稿で言及されています。担当者と期限を確認してください。",
                        "   - 👥 担当者：未定（チャンネルオーナー確認）",
                        "   - ⏰ 期限：次回定例まで",
                    ]
                )
        else:
            lines.extend(
                [
                    "1. 継続監視",
                    "   - 🧭 概要：目立ったアクションまたはリスク関連キーワードは検出されませんでした。",
                    "   - 👥 担当者：チャンネル参加者全員",
                    "   - ⏰ 期限：次回レポートまで",
                ]
            )

        lines.extend(["", "### ⚠️ 潜在的なリスク"])
        risk_words = [
            (word, count)
            for word, count in action_words.items()
            if word in {"error", "incident", "障害"}
        ]
        if risk_words:
            for index, (word, count) in enumerate(risk_words[:3], start=1):
                lines.extend(
                    [
                        f"{index}. `{word}` の継続監視が必要",
                        f"   - 🧭 概要：`{word}` が {count} 件の投稿で言及されています。根本原因の特定状況を確認してください。",
                        "   - 👥 関連ユーザー：未特定（該当スレッド確認）",
                        f"   - 🔗 URL：{_channel_links(messages)}",
                    ]
                )
        else:
            lines.extend(
                [
                    "1. 顕在化した重大リスクなし",
                    "   - 🧭 概要：エラー・障害系キーワードの集中は確認されませんでした。",
                    "   - 👥 関連ユーザー：なし",
                    "   - 🔗 URL：なし",
                ]
            )

        lines.extend(["", "### 👥 ユーザー別アクティビティ"])
        combined_users = sorted(set(by_user) | set(chat_by_user))
        if combined_users:
            for user in combined_users:
                user_messages = by_user.get(user, [])
                lines.append(
                    f"- {user}：🗨️ {len(user_messages)}件のチャンネル投稿、🤖 {chat_by_user.get(user, 0)}件のチャット利用"
                )
        else:
            lines.append("- 対象ユーザーの投稿はありませんでした。")

        lines.extend(["", "### 📚 新規ナレッジ"])
        if knowledge_items:
            for item in knowledge_items[:10]:
                created = item.get("_created_dt")
                created_text = (
                    created.isoformat() if isinstance(created, datetime) else "日時不明"
                )
                lines.append(f"- {_knowledge_name(item)}：{created_text}")
        else:
            lines.append("- 指定期間内に追加されたナレッジはありませんでした。")

        if errors:
            lines.extend(["", "### ⚠️ 取得時の注意"])
            for error in errors[:10]:
                lines.append(f"- {error}")

        # if include_langfuse:
        #     lines.extend(["", "### 🔍 Langfuse補足", *_langfuse_lines(since_dt, until_dt)])
        return {
            "ok": True,
            "content_markdown": "\n".join(lines),
            "metadata": {
                "channel": channel,
                "channel_id": channel_id,
                "since": since_dt.isoformat(),
                "until": until_dt.isoformat(),
                "messages": len(messages),
                "channels": len(channels),
                "chats": chat_count,
                "chat_messages": chat_message_count,
                "knowledge": knowledge_count,
                "errors": len(errors),
            },
            "summary_input": _summary_input(messages, chat_messages, knowledge_items),
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": str(exc),
            "exception_type": exc.__class__.__name__,
        }


def _build_parser() -> argparse.ArgumentParser:
    """Build the activity report helper CLI parser."""
    parser = argparse.ArgumentParser(
        description="Open WebUI全体から活動レポート用の投稿本文と機械的な下書きを収集します。"
    )
    parser.add_argument(
        "--channel", default=os.getenv("OPEN_WEBUI_DEFAULT_CHANNEL", "report")
    )
    parser.add_argument("--since")
    parser.add_argument("--until")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--max-pages", type=int, default=100)
    parser.add_argument("--no-threads", action="store_true")
    parser.add_argument("--no-langfuse", action="store_true")
    parser.add_argument(
        "--single-channel",
        action="store_true",
        help="旧形式の単一チャネル集計に限定します。",
    )
    parser.add_argument("--exclude-archived-chats", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the activity report helper CLI."""
    args = _build_parser().parse_args(argv)
    result = generate_activity_report(
        channel=args.channel,
        since=args.since,
        until=args.until,
        limit=args.limit,
        include_threads=not args.no_threads,
        include_langfuse=not args.no_langfuse,
        include_all_openwebui=not args.single_channel,
        page_size=args.page_size,
        max_pages=args.max_pages,
        include_archived_chats=not args.exclude_archived_chats,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
