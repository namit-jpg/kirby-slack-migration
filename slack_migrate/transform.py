"""Rewrites old-workspace IDs inside message text and Block Kit payloads.

Mentions of users/channels that exist in the target become live mentions with the new IDs.
Anything we can't map degrades to plain text (``@Name`` / ``#name``) so the message still
reads correctly. ``@here`` / ``@channel`` / ``@everyone`` are neutralised by default so a
migration doesn't page the whole company for messages that are months old.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import Any

USER_RE = re.compile(r"<@([UW][A-Z0-9]+)(?:\|([^>]*))?>")
CHANNEL_RE = re.compile(r"<#([CG][A-Z0-9]+)(?:\|([^>]*))?>")
SUBTEAM_RE = re.compile(r"<!subteam\^([A-Z0-9]+)(?:\|([^>]*))?>")
BROADCAST_RE = re.compile(r"<!(here|channel|everyone)(?:\|[^>]*)?>")


@dataclass
class Mapper:
    user_ids: dict[str, str] = field(default_factory=dict)  # old user id -> new user id
    user_names: dict[str, str] = field(default_factory=dict)  # old user id -> display name
    channel_ids: dict[str, str] = field(default_factory=dict)  # old channel id -> new channel id
    channel_names: dict[str, str] = field(default_factory=dict)  # old channel id -> name
    neutralize_broadcasts: bool = True

    # ---- mrkdwn text ---------------------------------------------------------------

    def text(self, value: str | None) -> str:
        if not value:
            return ""

        def user(m: re.Match) -> str:
            new = self.user_ids.get(m.group(1))
            if new:
                return f"<@{new}>"
            return "@" + (self.user_names.get(m.group(1)) or m.group(2) or "unknown-user")

        def channel(m: re.Match) -> str:
            new = self.channel_ids.get(m.group(1))
            if new:
                return f"<#{new}>"
            return "#" + (self.channel_names.get(m.group(1)) or m.group(2) or "unknown-channel")

        def subteam(m: re.Match) -> str:
            label = m.group(2) or "group"
            return label if label.startswith("@") else f"@{label}"

        def broadcast(m: re.Match) -> str:
            return f"@{m.group(1)}" if self.neutralize_broadcasts else m.group(0)

        value = USER_RE.sub(user, value)
        value = CHANNEL_RE.sub(channel, value)
        value = SUBTEAM_RE.sub(subteam, value)
        value = BROADCAST_RE.sub(broadcast, value)
        return value

    # ---- Block Kit -----------------------------------------------------------------

    def blocks(self, blocks: list[dict] | None) -> list[dict] | None:
        if not blocks:
            return None
        return self._walk(copy.deepcopy(blocks))

    def _walk(self, node: Any) -> Any:
        if isinstance(node, list):
            return [self._walk(item) for item in node]
        if not isinstance(node, dict):
            return node

        kind = node.get("type")
        style = {"style": node["style"]} if "style" in node else {}
        if kind == "user" and "user_id" in node:
            new = self.user_ids.get(node["user_id"])
            if new:
                return {**node, "user_id": new}
            name = self.user_names.get(node["user_id"]) or "unknown-user"
            return {"type": "text", "text": f"@{name}", **style}
        if kind == "channel" and "channel_id" in node:
            new = self.channel_ids.get(node["channel_id"])
            if new:
                return {**node, "channel_id": new}
            name = self.channel_names.get(node["channel_id"]) or "unknown-channel"
            return {"type": "text", "text": f"#{name}", **style}
        if kind == "usergroup":
            return {"type": "text", "text": "@group", **style}
        if kind == "broadcast" and self.neutralize_broadcasts:
            return {"type": "text", "text": f"@{node.get('range', 'here')}", **style}
        if kind == "mrkdwn" and isinstance(node.get("text"), str):
            return {**node, "text": self.text(node["text"])}
        return {key: self._walk(value) for key, value in node.items()}
