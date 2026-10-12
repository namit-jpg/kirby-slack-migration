"""Thin helpers around slack_sdk: client construction, pagination, error codes."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Iterator

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from slack_sdk.http_retry.builtin_handlers import RateLimitErrorRetryHandler

ClientFactory = Callable[[str], Any]


def make_client(token: str) -> WebClient:
    client = WebClient(token=token, timeout=60)
    # Slack answers 429 + Retry-After when we go too fast; sleep and retry instead of failing.
    client.retry_handlers.append(RateLimitErrorRetryHandler(max_retry_count=10))
    return client


def paginate(method: Callable[..., Any], key: str, **kwargs: Any) -> Iterator[dict]:
    cursor = None
    while True:
        params = dict(kwargs)
        if cursor:
            params["cursor"] = cursor
        resp = method(**params)
        yield from resp.get(key) or []
        cursor = (resp.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            return


def error_code(exc: SlackApiError) -> str:
    try:
        return exc.response.get("error") or ""
    except Exception:  # noqa: BLE001 - response shape is not guaranteed
        return ""


def format_date(ts: str | float, with_time: bool = False) -> str:
    """Render a Slack ts / epoch as dd MMM yyyy (optionally with HH:MM UTC)."""
    dt = datetime.fromtimestamp(float(ts), tz=timezone.utc)
    return dt.strftime("%d %b %Y %H:%M UTC" if with_time else "%d %b %Y")


def today() -> str:
    return datetime.now(timezone.utc).strftime("%d %b %Y")
