"""Local on-disk storage for the export (source side) and import state (target side).

Layout under the data dir:

    export/
      meta.json                 workspace info + when the export ran
      users.json                {old_user_id: user record}
      channels.json             {old_channel_id: channel record}
      dms.json                  {old_dm_id: DM / group DM record}
      messages/<channel>.json   {ts: raw Slack message (+ _pinned, files[]._local_path)}
      files/<file_id>/<name>    downloaded attachments
      avatars/<user_id>.<ext>   profile photos
    source/
      tokens.json               per-user OAuth tokens for the source workspace, used to read DMs (chmod 600)
    target/
      tokens.json               OAuth tokens for the target workspace (chmod 600)
      state.json                what has been created/posted so far (makes runs resumable)
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def _write_json(path: Path, data: Any, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


class Store:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.export_dir = self.root / "export"
        self.source_dir = self.root / "source"
        self.target_dir = self.root / "target"

    # ---- export (source workspace) ------------------------------------------------

    @property
    def files_dir(self) -> Path:
        return self.export_dir / "files"

    @property
    def avatars_dir(self) -> Path:
        return self.export_dir / "avatars"

    def load_meta(self) -> dict:
        return _read_json(self.export_dir / "meta.json", {})

    def save_meta(self, meta: dict) -> None:
        _write_json(self.export_dir / "meta.json", meta)

    def load_users(self) -> dict[str, dict]:
        return _read_json(self.export_dir / "users.json", {})

    def save_users(self, users: dict[str, dict]) -> None:
        _write_json(self.export_dir / "users.json", users)

    def load_channels(self) -> dict[str, dict]:
        return _read_json(self.export_dir / "channels.json", {})

    def save_channels(self, channels: dict[str, dict]) -> None:
        _write_json(self.export_dir / "channels.json", channels)

    def load_dms(self) -> dict[str, dict]:
        return _read_json(self.export_dir / "dms.json", {})

    def save_dms(self, dms: dict[str, dict]) -> None:
        _write_json(self.export_dir / "dms.json", dms)

    def load_source_tokens(self) -> dict:
        tokens = _read_json(self.source_dir / "tokens.json", {})
        tokens.setdefault("users", {})
        return tokens

    def save_source_tokens(self, tokens: dict) -> None:
        _write_json(self.source_dir / "tokens.json", tokens, mode=0o600)

    def load_messages(self, channel_id: str) -> dict[str, dict]:
        return _read_json(self.export_dir / "messages" / f"{channel_id}.json", {})

    def save_messages(self, channel_id: str, messages: dict[str, dict]) -> None:
        _write_json(self.export_dir / "messages" / f"{channel_id}.json", messages)

    # ---- target workspace ---------------------------------------------------------

    def load_tokens(self) -> dict:
        tokens = _read_json(self.target_dir / "tokens.json", {})
        tokens.setdefault("bot", None)
        tokens.setdefault("installer_email", None)
        tokens.setdefault("users", {})
        return tokens

    def save_tokens(self, tokens: dict) -> None:
        _write_json(self.target_dir / "tokens.json", tokens, mode=0o600)

    def load_state(self) -> dict:
        state = _read_json(self.target_dir / "state.json", {})
        state.setdefault("version", 1)
        state.setdefault("channels", {})
        state.setdefault("messages", {})
        state.setdefault("profiles", {})
        state.setdefault("dms", {})
        return state

    def save_state(self, state: dict) -> None:
        _write_json(self.target_dir / "state.json", state)
