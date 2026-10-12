"""Readiness/progress report: who still has to join or authorize, and what each channel waits on."""

from __future__ import annotations

from .importer import IMPORTABLE_SUBTYPES, POSTED, Importer, MigrationError, _display_name


def _table(headers: list[str], rows: list[list[str]]) -> str:
    widths = [max(len(str(x)) for x in col) for col in zip(headers, *rows)]
    line = lambda r: "  ".join(str(v).ljust(w) for v, w in zip(r, widths))  # noqa: E731
    return "\n".join([line(headers), line(["-" * w for w in widths]), *map(line, rows)])


def report(importer: Importer) -> str:
    out: list[str] = []
    meta = importer.store.load_meta()
    if not meta:
        return "No export found. Run `export` first."
    out.append(f"Source: {meta.get('team')}  exported on {meta.get('exported_on')}")

    try:
        importer.connect()
        connected = True
    except MigrationError as exc:
        out.append(f"Target: not connected ({exc})")
        connected = False

    def yn(flag: bool) -> str:
        return ("yes" if flag else "NO") if connected else "?"

    rows = []
    for u in sorted(importer.users.values(), key=lambda u: (u["deleted"], u.get("email") or "")):
        if u["is_bot"]:
            continue
        email = u.get("email") or "(no email)"
        rows.append(
            [
                email,
                _display_name(u),
                "deactivated" if u["deleted"] else "active",
                yn(email in importer.new_by_email),
                yn(importer.user_client(email) is not None),
            ]
        )
    out += ["", "USERS", _table(["email", "name", "source", "in target", "authorized"], rows)]

    rows = []
    for ch in importer.selected_channels():
        msgs = importer.store.load_messages(ch["id"])
        importable = [m for m in msgs.values() if m.get("subtype") in IMPORTABLE_SUBTYPES]
        posted = importer.state["messages"].get(ch["id"], {})
        done = sum(1 for m in importable if (posted.get(m["ts"]) or {}).get("status") == POSTED)
        cst = importer.state["channels"].get(ch["id"]) or {}
        blocked = cst.get("blocked_on")
        rows.append(
            [
                ("🔒" if ch["is_private"] else "#") + ch["name"],
                "yes" if cst.get("new_id") else "no",
                f"{done}/{len(importable)}",
                f"{blocked['author']} ({blocked['date']})" if blocked else "",
            ]
        )
    skipped = [c for c in importer.channels.values() if not c.get("exported")]
    out += ["", "CHANNELS", _table(["channel", "created", "posted", "waiting on"], rows)]
    if skipped:
        out += ["", "Not exported: " + ", ".join(f"#{c['name']} ({c.get('skip_reason')})" for c in skipped)]
    return "\n".join(out)
