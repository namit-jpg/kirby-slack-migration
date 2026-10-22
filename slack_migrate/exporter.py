"""Source-workspace app: dump users, channels, DMs, messages, threads, reactions, pins and files.

Channels are read with SOURCE_TOKEN. DMs, group DMs and private channels the bot wasn't invited
to can only be read with a member's own user token, collected per person by ``serve-oauth``
(step 1 of the authorization page). Each conversation is exported once, with the token of
whichever member comes first.

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

from .slack import ClientFactory, error_code, make_client, paginate, today
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
    def __init__(
        self,
        client,
        token: str,
        store: Store,
        download_files: bool = True,
        team_id: str | None = None,
        client_factory: ClientFactory = make_client,
    ):
        self.client = client
        self.client_factory = client_factory
        # Enterprise Grid org-level tokens must say which workspace they mean.
        self.team = {"team_id": team_id} if team_id else {}
        self.token = token
        self.store = store
        self.download_files = download_files

    def run(self, only_channels: set[str] | None = None, channels: bool = True, dms: bool = True) -> None:
        auth = self.client.auth_test()
        log.info("Exporting workspace %s as %s", auth.get("team"), auth.get("user"))

        users = self.export_users()
        if channels:
            exported_ids = self.export_channels(only_channels)
            self.export_private_channels(only_channels, exported_ids)
        if dms:
            self.export_dms(users)

        exported = [c for c in self.store.load_channels().values() if c.get("exported")]
        dm_count = sum(1 for d in self.store.load_dms().values() if d.get("exported"))
        self.store.save_meta(
            {
                "team_id": auth.get("team_id"),
                "team": auth.get("team"),
                "url": auth.get("url"),
                "exported_at": datetime.now(timezone.utc).isoformat(),
                "exported_on": today(),
                "users": len(users),
                "channels": len(exported),
                "dms": dm_count,
            }
        )
        log.info("Export done: %d users, %d channels, %d DMs", len(users), len(exported), dm_count)

    def export_channels(self, only_channels: set[str] | None = None) -> set[str]:
        """Export every channel the bot can see; returns the IDs exported in this run."""
        channels = self.store.load_channels()
        exported: set[str] = set()
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
            if channels[ch["id"]]["exported"]:
                exported.add(ch["id"])

        skipped = [c for c in channels.values() if not c.get("exported")]
        for c in skipped:
            log.warning("Not exported: #%s (%s)", c["name"], c.get("skip_reason"))
        return exported

    def export_private_channels(self, only_channels: set[str] | None, exported: set[str]) -> None:
        """Export private channels the bot isn't in, with the token of a member who did step 1."""
        tokens = self.store.load_source_tokens()["users"]
        if not tokens:
            return
        channels = self.store.load_channels()
        for email, rec in sorted(tokens.items()):
            if "groups:history" not in (rec.get("scopes") or "").split(","):
                log.warning("%s authorized step 1 before private channels were included; "
                            "they need to authorize step 1 again", email)
                continue
            client = self.client_factory(rec["token"])
            try:
                convs = list(paginate(client.conversations_list, "channels", types="private_channel",
                                      exclude_archived=False, limit=200, **self.team))
            except SlackApiError as exc:
                log.warning("Could not list private channels for %s (%s)", email, error_code(exc))
                continue
            count = 0
            for ch in convs:
                if ch["id"] in exported or (only_channels and ch["name"] not in only_channels):
                    continue
                channels[ch["id"]] = self.export_channel(ch, client, rec["token"])
                self.store.save_channels(channels)
                if channels[ch["id"]]["exported"]:
                    exported.add(ch["id"])
                    count += 1
            log.info("Private channels via %s: %d exported", email, count)

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

    def export_channel(self, ch: dict, client=None, token: str | None = None) -> dict:
        client = client or self.client
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
                client.conversations_join(channel=ch["id"])
            except SlackApiError as exc:
                rec["skip_reason"] = f"could not join: {error_code(exc)}"
                return rec

        try:
            rec["members"] = list(paginate(client.conversations_members, "members", channel=ch["id"], limit=200))
        except SlackApiError as exc:
            log.warning("#%s: could not list members (%s)", rec["name"], error_code(exc))

        try:
            messages = self.export_messages(ch["id"], client, token)
        except SlackApiError as exc:
            rec["skip_reason"] = f"history failed: {error_code(exc)}"
            return rec

        self.store.save_messages(ch["id"], messages)
        rec["exported"] = True
        rec["message_count"] = len(messages)
        log.info("#%s: %d messages, %d members", rec["name"], len(messages), len(rec["members"]))
        return rec

    def export_messages(self, channel_id: str, client=None, token: str | None = None) -> dict[str, dict]:
        client = client or self.client
        messages: dict[str, dict] = {}
        for m in paginate(client.conversations_history, "messages", channel=channel_id, limit=200):
            messages[m["ts"]] = m
            if m.get("reply_count") and m.get("thread_ts") == m["ts"]:
                for reply in paginate(
                    client.conversations_replies, "messages", channel=channel_id, ts=m["ts"], limit=200
                ):
                    if reply["ts"] != m["ts"]:
                        messages[reply["ts"]] = reply

        for ts in self._pinned(channel_id, client):
            if ts in messages:
                messages[ts]["_pinned"] = True

        if self.download_files:
            for m in messages.values():
                for f in m.get("files") or []:
                    f["_local_path"] = self._download_file(f, token or self.token)
        return messages

    def _pinned(self, channel_id: str, client=None) -> set[str]:
        try:
            items = (client or self.client).pins_list(channel=channel_id).get("items") or []
        except SlackApiError as exc:
            log.warning("pins.list failed for %s: %s", channel_id, error_code(exc))
            return set()
        return {i["message"]["ts"] for i in items if i.get("type") == "message" and i.get("message")}

    def _download_file(self, f: dict, token: str) -> str | None:
        url = f.get("url_private_download") or f.get("url_private")
        if f.get("mode") in UNDOWNLOADABLE_FILE_MODES or not url:
            return None
        dest = self.store.files_dir / f["id"] / _safe_name(f.get("name") or f["id"])
        rel = str(dest.relative_to(self.store.export_dir))
        if dest.exists():
            return rel
        try:
            if _download(url, token, dest):
                return rel
            log.warning("File %s: got an HTML page instead of the file (missing files:read?)", f.get("name"))
        except Exception as exc:  # noqa: BLE001 - one bad file shouldn't sink the export
            log.warning("File %s download failed: %s", f.get("name"), exc)
        return None

    # ---- direct messages ----------------------------------------------------------

    def export_dms(self, users: dict[str, dict]) -> None:
        """Export every DM and group DM visible to the people who authorized step 1."""
        tokens = self.store.load_source_tokens()["users"]
        if not tokens:
            log.info("No DM authorizations yet (step 1 of serve-oauth); skipping DMs")
            return
        dms = self.store.load_dms()
        seen: set[str] = set()
        for email, rec in sorted(tokens.items()):
            client = self.client_factory(rec["token"])
            try:
                client.auth_test()
            except SlackApiError as exc:
                log.warning("DM token for %s unusable (%s); they need to authorize step 1 again", email, error_code(exc))
                continue
            count = 0
            try:
                # Org-level (Enterprise Grid) user tokens need the workspace too.
                convs = list(paginate(client.conversations_list, "channels", types="im,mpim", limit=200, **self.team))
            except SlackApiError as exc:
                log.warning("Could not list DMs for %s (%s)", email, error_code(exc))
                continue
            for conv in convs:
                if conv["id"] in seen:
                    continue
                rec_dm = self.export_dm(client, rec["token"], rec["user_id"], conv, users)
                if rec_dm is None:
                    continue
                seen.add(conv["id"])
                dms[conv["id"]] = rec_dm
                self.store.save_dms(dms)
                count += rec_dm.get("exported", False)
            log.info("DMs via %s: %d conversation(s) exported", email, count)

    def export_dm(self, client, token: str, self_id: str, conv: dict, users: dict[str, dict]) -> dict | None:
        if conv.get("is_im"):
            members = [self_id] if conv.get("user") == self_id else [self_id, conv.get("user")]
        else:
            try:
                members = list(paginate(client.conversations_members, "members", channel=conv["id"], limit=200))
            except SlackApiError as exc:
                log.warning("Group DM %s: could not list members (%s)", conv["id"], error_code(exc))
                return None
        # Conversations with Slackbot or app bots can't be recreated as a person-to-person DM.
        if any((users.get(m) or {}).get("is_bot") or m == "USLACKBOT" for m in members):
            return None

        names = [(users.get(m) or {}).get("name") or m for m in members]
        rec = {
            "id": conv["id"],
            "name": "dm:" + ",".join(sorted(names)),
            "is_mpim": bool(conv.get("is_mpim")),
            "members": members,
            "created": conv.get("created"),
            "exported": False,
            "skip_reason": None,
        }
        try:
            messages = self.export_messages(conv["id"], client, token)
        except SlackApiError as exc:
            rec["skip_reason"] = f"history failed: {error_code(exc)}"
            return rec
        if not messages:
            return None
        self.store.save_messages(conv["id"], messages)
        rec["exported"] = True
        rec["message_count"] = len(messages)
        return rec
