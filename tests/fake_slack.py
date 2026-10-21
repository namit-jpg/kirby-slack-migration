"""In-memory stand-in for the target workspace's Web API, enough to exercise the importer."""

from __future__ import annotations

from slack_sdk.errors import SlackApiError


def fail(code: str):
    raise SlackApiError(code, {"ok": False, "error": code})


class FakeWorkspace:
    def __init__(self, users: list[dict]):
        # users: [{"id": "N_A", "email": "a@x.com"}]
        self.users = {u["id"]: u for u in users}
        self.bot_user_id = "N_BOT"
        self.channels: dict[str, dict] = {
            "CGEN": {"id": "CGEN", "name": "general", "is_private": False, "is_general": True,
                     "members": set(self.users), "messages": [], "archived": False, "topic": "", "purpose": ""}
        }
        self.files: dict[str, dict] = {}
        self._ts = 2_000_000_000
        self._ids = 0

    def next_ts(self) -> str:
        self._ts += 1
        return f"{self._ts}.000100"

    def next_id(self, prefix: str) -> str:
        self._ids += 1
        return f"{prefix}{self._ids:03d}"

    def client(self, token: str) -> "FakeClient":
        return FakeClient(self, token)

    def channel_by_name(self, name: str) -> dict:
        return next(c for c in self.channels.values() if c["name"] == name)


class FakeClient:
    def __init__(self, ws: FakeWorkspace, token: str):
        self.ws = ws
        self.token = token
        if token == "bot":
            self.user_id = ws.bot_user_id
        elif token.startswith("user:"):
            self.user_id = token.split(":", 1)[1]
        else:
            self.user_id = None

    @property
    def is_bot(self) -> bool:
        return self.token == "bot"

    def _ch(self, channel: str) -> dict:
        ch = self.ws.channels.get(channel)
        if not ch:
            fail("channel_not_found")
        if ch["archived"]:
            fail("is_archived")
        return ch

    def _require_member(self, ch: dict) -> None:
        if self.user_id not in ch["members"]:
            fail("not_in_channel")

    # ---- auth / users ---------------------------------------------------------------
    def auth_test(self):
        if self.user_id is None:
            fail("invalid_auth")
        return {"ok": True, "user_id": self.user_id, "team_id": "TNEW"}

    def users_list(self, **_):
        members = [{"id": u["id"], "profile": {"email": u["email"]}, "deleted": False} for u in self.ws.users.values()]
        return {"members": members}

    # ---- conversations ----------------------------------------------------------------
    def conversations_list(self, **_):
        visible = [
            {"id": c["id"], "name": c["name"], "is_private": c["is_private"], "is_general": c.get("is_general", False),
             "is_member": self.user_id in c["members"]}
            for c in self.ws.channels.values()
            if not c["is_private"] or self.user_id in c["members"]
        ]
        return {"channels": visible}

    def conversations_create(self, name: str, is_private: bool, **_):
        if any(c["name"] == name for c in self.ws.channels.values()):
            fail("name_taken")
        cid = self.ws.next_id("G" if is_private else "C")
        self.ws.channels[cid] = {"id": cid, "name": name, "is_private": is_private, "members": {self.user_id},
                                 "messages": [], "archived": False, "topic": "", "purpose": "", "creator": self.user_id}
        return {"channel": {"id": cid}}

    def conversations_join(self, channel: str):
        ch = self._ch(channel)
        if ch["is_private"]:
            fail("method_not_supported_for_channel_type")
        ch["members"].add(self.user_id)
        return {"ok": True}

    def conversations_invite(self, channel: str, users: str, force: bool = False):
        ch = self._ch(channel)
        self._require_member(ch)
        errors = []
        for uid in users.split(","):
            if uid == self.user_id:
                errors.append({"user": uid, "error": "cant_invite_self"})
            elif uid in ch["members"]:
                errors.append({"user": uid, "error": "already_in_channel"})
            else:
                ch["members"].add(uid)
        if errors and not force:
            fail(errors[0]["error"])
        return {"ok": True, "errors": errors}

    def conversations_open(self, users: str):
        if self.is_bot:
            fail("not_allowed_token_type")
        members = set(users.split(",")) | {self.user_id}
        if not members <= set(self.ws.users):
            fail("user_not_found")
        for ch in self.ws.channels.values():
            if ch.get("is_dm") and ch["members"] == members:
                return {"channel": {"id": ch["id"]}}
        cid = self.ws.next_id("D" if len(members) <= 2 else "M")
        self.ws.channels[cid] = {"id": cid, "name": "", "is_private": True, "is_dm": True, "members": members,
                                 "messages": [], "archived": False, "topic": "", "purpose": ""}
        return {"channel": {"id": cid}}

    def conversations_setTopic(self, channel: str, topic: str):
        self._ch(channel)["topic"] = topic
        return {"ok": True}

    def conversations_setPurpose(self, channel: str, purpose: str):
        self._ch(channel)["purpose"] = purpose
        return {"ok": True}

    def conversations_archive(self, channel: str):
        self._ch(channel)["archived"] = True
        return {"ok": True}

    def conversations_leave(self, channel: str):
        self._ch(channel)["members"].discard(self.user_id)
        return {"ok": True}

    # ---- messages ---------------------------------------------------------------------
    def chat_postMessage(self, channel: str, text: str, thread_ts=None, blocks=None, username=None,
                         icon_url=None, reply_broadcast=None, attachments=None, **_):
        ch = self._ch(channel)
        if not (self.is_bot and not ch["is_private"]):
            self._require_member(ch)
        if thread_ts and not any(m["ts"] == thread_ts for m in ch["messages"]):
            fail("thread_not_found")
        msg = {"ts": self.ws.next_ts(), "user": self.user_id, "text": text, "thread_ts": thread_ts,
               "blocks": blocks, "username": username if self.is_bot else None, "reactions": {},
               "reply_broadcast": reply_broadcast, "pinned": False}
        ch["messages"].append(msg)
        return {"ok": True, "ts": msg["ts"], "channel": channel}

    def chat_meMessage(self, channel: str, text: str):
        resp = self.chat_postMessage(channel=channel, text=text)
        resp_msg = self._find(channel, resp["ts"])
        resp_msg["me"] = True
        return resp

    def _find(self, channel: str, ts: str) -> dict:
        msg = next((m for m in self._ch(channel)["messages"] if m["ts"] == ts), None)
        if not msg:
            fail("message_not_found")
        return msg

    def reactions_add(self, channel: str, timestamp: str, name: str):
        ch = self._ch(channel)
        self._require_member(ch)
        if name.startswith("custom-"):
            fail("invalid_name")
        users = self._find(channel, timestamp)["reactions"].setdefault(name, [])
        if self.user_id in users:
            fail("already_reacted")
        users.append(self.user_id)
        return {"ok": True}

    def pins_add(self, channel: str, timestamp: str):
        msg = self._find(channel, timestamp)
        if msg["pinned"]:
            fail("already_pinned")
        msg["pinned"] = True
        return {"ok": True}

    # ---- files ------------------------------------------------------------------------
    def files_upload_v2(self, channel: str, file_uploads: list[dict], initial_comment: str = "", thread_ts=None):
        resp = self.chat_postMessage(channel=channel, text=initial_comment, thread_ts=thread_ts)
        fid = self.ws.next_id("F")
        self.ws.files[fid] = {"id": fid, "names": [u["filename"] for u in file_uploads],
                              "shares": {"public": {channel: [{"ts": resp["ts"]}]}}}
        self._find(channel, resp["ts"])["files"] = [u["filename"] for u in file_uploads]
        return {"ok": True, "files": [{"id": fid}]}

    def files_info(self, file: str):
        return {"file": self.ws.files[file]}
