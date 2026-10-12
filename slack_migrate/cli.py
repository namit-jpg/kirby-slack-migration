"""Command line entry point: python -m slack_migrate <command>."""

from __future__ import annotations

import argparse
import logging
import os
import sys

from dotenv import load_dotenv
from slack_sdk.errors import SlackApiError

from .exporter import Exporter
from .importer import Importer, ImportOptions, MigrationError
from .oauth_server import OAuthApp, serve
from .slack import error_code, make_client
from .status import report
from .store import Store


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        sys.exit(f"Missing {name} (set it in .env)")
    return value


def _csv(value: str | None) -> set[str] | None:
    return {v.strip().lstrip("#").lower() for v in value.split(",") if v.strip()} if value else None


def _renames(pairs: list[str]) -> dict[str, str]:
    out = {}
    for pair in pairs:
        old, sep, new = pair.partition("=")
        if not sep or not old.strip() or not new.strip():
            sys.exit(f"--rename expects OLD=NEW, got {pair!r}")
        out[old.strip().lstrip("#").lower()] = new.strip().lstrip("#").lower()
    return out


def main(argv: list[str] | None = None) -> None:
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--env-file", default=".env")
    env_file = pre.parse_known_args(argv)[0].env_file
    load_dotenv(env_file, override=True)

    parser = argparse.ArgumentParser(prog="slack_migrate", description="Slack workspace to workspace migration")
    parser.add_argument("--env-file", default=".env", help="settings file (one per migration target)")
    parser.add_argument("--data-dir", default=os.environ.get("MIGRATION_DATA_DIR", "migration_data"))
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("export", help="dump the source workspace to the data dir")
    p.add_argument("--no-files", action="store_true", help="skip downloading attachments and avatars")
    p.add_argument("--channels", help="comma-separated channel names (default: all)")

    p = sub.add_parser("serve-oauth", help="run the page where target users authorize the importer")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=int(os.environ.get("OAUTH_PORT", 3000)))
    p.add_argument("--cert", help="TLS cert (only if not behind an HTTPS tunnel)")
    p.add_argument("--key", help="TLS key")

    sub.add_parser("status", help="show who must still join/authorize and per-channel progress")

    p = sub.add_parser("import", help="recreate the workspace in the target (safe to re-run for deltas)")
    p.add_argument("--mode", choices=["strict", "skip", "impersonate"], default="strict")
    p.add_argument("--channels", help="comma-separated old channel names (default: all)")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--stamp-dates", action="store_true", help="add 'Originally sent dd MMM yyyy' to each message")
    p.add_argument("--allow-broadcasts", action="store_true", help="keep @here/@channel live (they WILL notify)")
    p.add_argument("--no-archive", action="store_true", help="don't archive channels archived in the source")
    p.add_argument("--never-joining", help="comma-separated emails that will never be in the target")
    p.add_argument("--departed", choices=["impersonate", "drop", "wait"], default="impersonate",
                   help="what to do with messages from deactivated / never-joining users")
    p.add_argument("--interval", type=float, default=1.1, help="seconds between posts")
    p.add_argument("--general-as-channel", action="store_true",
                   help="recreate the source default channel by name instead of posting into the target's")
    p.add_argument("--no-invite", action="store_true", help="only add people whose messages get posted")
    p.add_argument("--rename", action="append", default=[], metavar="OLD=NEW",
                   help="create a channel under a different name in the target (repeatable)")

    p = sub.add_parser("sync-profiles", help="copy title/phone/display name/photo onto authorized users")
    p.add_argument("--force", action="store_true")

    sub.add_parser("leave-temp-members", help="final cleanup: remove people added only to post a message")

    p = sub.add_parser("revoke-tokens", help="revoke and delete all stored user tokens (end of migration)")
    p.add_argument("--yes", action="store_true")

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%d %b %Y %H:%M:%S",
    )
    for noisy in ("slack_sdk", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    store = Store(args.data_dir)

    try:
        if args.command == "export":
            token = _require("SOURCE_TOKEN")
            Exporter(make_client(token), token, store, download_files=not args.no_files,
                     team_id=os.environ.get("SOURCE_TEAM_ID")).run(_csv(args.channels))

        elif args.command == "serve-oauth":
            app = OAuthApp(
                store,
                _require("TARGET_CLIENT_ID"),
                _require("TARGET_CLIENT_SECRET"),
                _require("TARGET_REDIRECT_URI"),
            )
            serve(app, args.host, args.port, args.cert, args.key)

        elif args.command == "status":
            print(report(Importer(store, ImportOptions(team_id=os.environ.get("TARGET_TEAM_ID")))))

        elif args.command == "import":
            opts = ImportOptions(
                mode=args.mode,
                channels=_csv(args.channels),
                dry_run=args.dry_run,
                stamp_dates=args.stamp_dates,
                neutralize_broadcasts=not args.allow_broadcasts,
                archive=not args.no_archive,
                post_interval=args.interval,
                never_joining=_csv(args.never_joining) or set(),
                departed=args.departed,
                team_id=os.environ.get("TARGET_TEAM_ID"),
                general_as_channel=args.general_as_channel,
                invite_members=not args.no_invite,
                rename=_renames(args.rename),
            )
            Importer(store, opts).run()

        elif args.command == "sync-profiles":
            Importer(store, ImportOptions(team_id=os.environ.get("TARGET_TEAM_ID"))).sync_profiles(force=args.force)

        elif args.command == "leave-temp-members":
            Importer(store, ImportOptions(team_id=os.environ.get("TARGET_TEAM_ID"))).leave_temp_members()

        elif args.command == "revoke-tokens":
            tokens = store.load_tokens()
            if not args.yes:
                sys.exit(f"This revokes {len(tokens['users'])} user token(s). Re-run with --yes.")
            for email, rec in list(tokens["users"].items()):
                try:
                    make_client(rec["token"]).auth_revoke()
                except SlackApiError as exc:
                    logging.warning("%s: %s", email, error_code(exc))
                del tokens["users"][email]
                logging.info("Revoked %s", email)
            store.save_tokens(tokens)

    except MigrationError as exc:
        sys.exit(f"error: {exc}")
    except SlackApiError as exc:
        sys.exit(f"Slack API error: {error_code(exc)} ({exc})")


if __name__ == "__main__":
    main()
