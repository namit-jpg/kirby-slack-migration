"""Source-workspace app: dump users, channels, messages, threads, reactions, pins and files.

Every run is a full re-export that overwrites the previous snapshot. The importer keys its
progress on the original message ``ts``, so re-exporting is safe and is exactly how a
delta migration picks up messages written in the old workspace after the first pass.
"""

from __future__ import annotations

import logging
import re
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from slack_sdk.errors import SlackApiError

from .slack import error_code, paginate, today
from .store import Store

log = logging.getLogger(__name__)

UNDOWNLOADABLE_FILE_MODES = {"tombstone", "hidden_by_limit", "external"}


def _user_record(u: dict) -> dict:
    p = u.get("profile") or {}
    return {
        "id": u["id"],
        "name": u.get("name"),
        "real_name": p.get("real_name") or u.get("real_name"),
        "display_name": p.get("display_name"),
        "first_name": p.get("first_name"),
        "last_name": p.get("last_name"),
        "title": p.get("title"),
        "phone": p.get("phone"),
        "pronouns": p.get("pronouns"),
        "email": (p.get("email") or "").strip().lower() or None,
        "image": p.get("image_192") or p.get("image_72"),
        "image_original": p.get("image_original") or p.get("image_512"),
        "tz": u.get("tz"),
        "is_bot": bool(u.get("is_bot")) or u["id"] == "USLACKBOT",
        "is_admin": bool(u.get("is_admin")),
        "is_owner": bool(u.get("is_owner")),
        "is_restricted": bool(u.get("is_restricted")),
        "deleted": bool(u.get("deleted")),
    }


def _safe_name(name: str) -> str:
    return re.sub(r"[^\w.\- ]+", "_", name).strip() or "file"


def _download(url: str, token: str, dest: Path) -> bool:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        # Slack serves its HTML login page instead of the file when the token lacks files:read.
        if "text/html" in (resp.headers.get("Content-Type") or "") and dest.suffix.lower() not in {".html", ".htm"}:
            return False
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".part")
        with open(tmp, "wb") as fh:
            while chunk := resp.read(1 << 16):
                fh.write(chunk)
        tmp.replace(dest)
    return True


class Exporter:
    def __init__(self, client, token: str, store: Store, download_files: bool = True, team_id: str | None = None):
        self.client = client
        # Enterprise Grid org-level tokens must say which workspace they mean.
        self.team = {"team_id": team_id} if team_id else {}
        self.token = token
        self.store = store
        self.download_files = download_files

    def run(self, only_channels: set[str] | None = None) -> None:
        auth = self.client.auth_test()
        log.info("Exporting workspace %s as %s", auth.get("team"), auth.get("user"))

        users = self.export_users()
        channels = self.store.load_channels()
        for ch in paginate(
            self.client.conversations_list,
            "channels",
            types="public_channel,private_channel",
            exclude_archived=False,
            limit=200,
            **self.team,
        ):
            if only_channels and ch["name"] not in only_channels:
                continue
            channels[ch["id"]] = self.export_channel(ch)
            self.store.save_channels(channels)

        exported = [c for c in channels.values() if c.get("exported")]
        self.store.save_meta(
            {
                "team_id": auth.get("team_id"),
                "team": auth.get("team"),
                "url": auth.get("url"),
                "exported_at": datetime.now(timezone.utc).isoformat(),
                "exported_on": today(),
                "users": len(users),
                "channels": len(exported),
            }
        )
        skipped = [c for c in channels.values() if not c.get("exported")]
        log.info("Export done: %d users, %d channels", len(users), len(exported))
        for c in skipped:
            log.warning("Not exported: #%s (%s)", c["name"], c.get("skip_reason"))

    # ---- users -------------------------------------------------------------------

    def export_users(self) -> dict[str, dict]:
        users: dict[str, dict] = {}
        for u in paginate(self.client.users_list, "members", limit=200, **self.team):
            rec = _user_record(u)
            if self.download_files and rec["image_original"] and not rec["is_bot"]:
                rec["_avatar_path"] = self._download_avatar(rec)
            users[rec["id"]] = rec
        self.store.save_users(users)
        humans = [u for u in users.values() if not u["is_bot"]]
        missing_email = [u["name"] for u in humans if not u["email"] and not u["deleted"]]
        log.info("Exported %d users (%d humans)", len(users), len(humans))
        if missing_email:
            log.warning("Users without an email (add users:read.email scope?): %s", ", ".join(missing_email))
        return users

    def _download_avatar(self, rec: dict) -> str | None:
        url = rec["image_original"]
        ext = Path(url.split("?")[0]).suffix or ".jpg"
        dest = self.store.avatars_dir / f"{rec['id']}{ext}"
        rel = str(dest.relative_to(self.store.export_dir))
        if dest.exists():
            return rel
        try:
            return rel if _download(url, self.token, dest) else None
        except Exception as exc:  # noqa: BLE001 - avatars are best-effort
            log.warning("Avatar download failed for %s: %s", rec["name"], exc)
            return None

    # ---- channels ----------------------------------------------------------------

    def export_channel(self, ch: dict) -> dict:
        rec = {
            "id": ch["id"],
            "name": ch["name"],
            "is_private": bool(ch.get("is_private")),
            "is_archived": bool(ch.get("is_archived")),
            "is_general": bool(ch.get("is_general")),
            "creator": ch.get("creator"),
            "created": ch.get("created"),
            "topic": (ch.get("topic") or {}).get("value") or "",
            "purpose": (ch.get("purpose") or {}).get("value") or "",
            "members": [],
            "exported": False,
            "skip_reason": None,
        }

        if not ch.get("is_member"):
            if rec["is_private"] or rec["is_archived"]:
                rec["skip_reason"] = "app is not a member; invite it (or export with an admin user token)"
                return rec
            try:
                self.client.conversations_join(channel=ch["id"])
            except SlackApiError as exc:
                rec["skip_reason"] = f"could not join: {error_code(exc)}"
                return rec

        try:
            rec["members"] = list(paginate(self.client.conversations_members, "members", channel=ch["id"], limit=200))
        except SlackApiError as exc:
            log.warning("#%s: could not list members (%s)", rec["name"], error_code(exc))

        try:
            messages = self.export_messages(ch["id"])
        except SlackApiError as exc:
            rec["skip_reason"] = f"history failed: {error_code(exc)}"
            return rec

        self.store.save_messages(ch["id"], messages)
        rec["exported"] = True
        rec["message_count"] = len(messages)
        log.info("#%s: %d messages, %d members", rec["name"], len(messages), len(rec["members"]))
        return rec

    def export_messages(self, channel_id: str) -> dict[str, dict]:
        messages: dict[str, dict] = {}
        for m in paginate(self.client.conversations_history, "messages", channel=channel_id, limit=200):
            messages[m["ts"]] = m
            if m.get("reply_count") and m.get("thread_ts") == m["ts"]:
                for reply in paginate(
                    self.client.conversations_replies, "messages", channel=channel_id, ts=m["ts"], limit=200
                ):
                    if reply["ts"] != m["ts"]:
                        messages[reply["ts"]] = reply

        for ts in self._pinned(channel_id):
            if ts in messages:
                messages[ts]["_pinned"] = True

        if self.download_files:
            for m in messages.values():
                for f in m.get("files") or []:
                    f["_local_path"] = self._download_file(f)
        return messages

    def _pinned(self, channel_id: str) -> set[str]:
        try:
            items = self.client.pins_list(channel=channel_id).get("items") or []
        except SlackApiError as exc:
            log.warning("pins.list failed for %s: %s", channel_id, error_code(exc))
            return set()
        return {i["message"]["ts"] for i in items if i.get("type") == "message" and i.get("message")}

    def _download_file(self, f: dict) -> str | None:
        url = f.get("url_private_download") or f.get("url_private")
        if f.get("mode") in UNDOWNLOADABLE_FILE_MODES or not url:
            return None
        dest = self.store.files_dir / f["id"] / _safe_name(f.get("name") or f["id"])
        rel = str(dest.relative_to(self.store.export_dir))
        if dest.exists():
            return rel
        try:
            if _download(url, self.token, dest):
                return rel
            log.warning("File %s: got an HTML page instead of the file (missing files:read?)", f.get("name"))
        except Exception as exc:  # noqa: BLE001 - one bad file shouldn't sink the export
            log.warning("File %s download failed: %s", f.get("name"), exc)
        return None
