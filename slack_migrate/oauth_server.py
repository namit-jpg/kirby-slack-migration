"""A tiny OAuth server that collects user tokens per person, keyed by email.

Messages can only appear as genuinely sent by a person (no APP badge, real avatar/name)
when they're posted with that person's own user token (xoxp-). Each person opens this page
once and goes through two steps:

1. **Old workspace** (only when SOURCE_CLIENT_ID is set): read-only access to their DMs and
   group DMs. Slack never lets a bot read DMs, so this is the only way to migrate them.
2. **New workspace**: post messages, reactions and files as them, and open their DMs.

The first person to authorize step 2 (normally a workspace admin) also installs the target
app, which gives us the bot token used for read-only lookups and the optional bot fallback.
"""

from __future__ import annotations

import html
import logging
import secrets
import ssl
import time
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from .slack import error_code, today
from .store import Store

log = logging.getLogger(__name__)

# Old workspace, user token only: enough to read each person's DMs and group DMs.
SOURCE_USER_SCOPES = [
    "im:read",
    "im:history",
    "mpim:read",
    "mpim:history",
    "users:read",
    "users:read.email",
    "files:read",
    "pins:read",
]

BOT_SCOPES = [
    "users:read",
    "users:read.email",
    "channels:read",
    "groups:read",
    "chat:write",
    "chat:write.public",
    "chat:write.customize",
]

USER_SCOPES = [
    "chat:write",
    "channels:read",
    "channels:write",
    "groups:read",
    "groups:write",
    "reactions:write",
    "files:read",
    "files:write",
    "pins:write",
    "users:read",
    "users:read.email",
    "users.profile:write",
    "im:write",
    "mpim:write",
]

PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Workspace migration</title>
<style>body{{font:16px/1.5 -apple-system,system-ui,sans-serif;max-width:560px;margin:64px auto;padding:0 16px;color:#1d1c1d}}
a.btn{{display:inline-block;background:#4a154b;color:#fff;padding:12px 20px;border-radius:6px;text-decoration:none;font-weight:600}}
.muted{{color:#616061;font-size:14px}}</style></head><body>{body}</body></html>"""


SOURCE, TARGET = "source", "target"


class OAuthApp:
    def __init__(
        self,
        store: Store,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        source_client_id: str | None = None,
        source_client_secret: str | None = None,
        source_redirect_uri: str | None = None,
        source_workspace_name: str | None = None,
        target_workspace_name: str | None = None,
    ):
        self.store = store
        self.workspace_names = {SOURCE: source_workspace_name, TARGET: target_workspace_name}
        self.apps = {TARGET: (client_id, client_secret, redirect_uri)}
        if source_client_id and source_client_secret:
            self.apps[SOURCE] = (source_client_id, source_client_secret, source_redirect_uri or redirect_uri)
        self.callback_paths = {urllib.parse.urlparse(uri).path or "/" for _, _, uri in self.apps.values()}
        self._states: dict[str, tuple[str, float]] = {}

    @property
    def collects_dms(self) -> bool:
        return SOURCE in self.apps

    def authorize_url(self, side: str = TARGET) -> str:
        client_id, _, redirect_uri = self.apps[side]
        now = time.time()
        # The landing page mints a state per button on every view; forget the stale ones.
        self._states = {k: v for k, v in self._states.items() if now - v[1] < 15 * 60}
        state = secrets.token_urlsafe(24)
        self._states[state] = (side, now)
        params = {"client_id": client_id, "redirect_uri": redirect_uri, "state": state}
        if side == SOURCE:
            params["user_scope"] = ",".join(SOURCE_USER_SCOPES)
        else:
            params["scope"] = ",".join(BOT_SCOPES)
            params["user_scope"] = ",".join(USER_SCOPES)
        return "https://slack.com/oauth/v2/authorize?" + urllib.parse.urlencode(params)

    def _consume_state(self, state: str) -> str | None:
        side, created = self._states.pop(state, (None, 0.0))
        return side if side and time.time() - created < 15 * 60 else None

    def _target_button(self) -> str:
        return f'<a class="btn" href="{html.escape(self.authorize_url(TARGET))}">Authorize the new workspace</a>'

    def _signin_hint(self, side: str) -> str:
        which = "old" if side == SOURCE else "new"
        name = self.workspace_names.get(side)
        if not name:
            return f'<p class="muted">Sign in to the <b>{which}</b> workspace when Slack asks.</p>'
        # Both orgs can list several similarly named workspaces in Slack's picker.
        return (f'<p class="muted">On Slack\'s page, open the workspace dropdown at the top right and choose '
                f'exactly <b>{html.escape(name)}</b> (the {which} workspace) before clicking Allow. '
                f'A different workspace, even one with a similar name, won\'t work.</p>')

    def landing_page(self) -> str:
        if not self.collects_dms:
            return PAGE.format(
                body=f"""<h1>Move your messages to the new workspace</h1>
<p>Authorizing lets the migration post your old messages <b>as you</b>, so they look exactly
like you wrote them. Your token is stored only on the migration machine and is revoked after
the migration finishes.</p>
<p>{self._target_button()}</p>
{self._signin_hint(TARGET)}"""
            )
        source_url = html.escape(self.authorize_url(SOURCE))
        return PAGE.format(
            body=f"""<h1>Move your messages to the new workspace</h1>
<p>Two quick steps. Your tokens are stored only on the migration machine and are revoked after
the migration finishes.</p>
<h2>Step 1: old workspace</h2>
<p>Lets the migration <b>read</b> your direct messages and group DMs so they can be recreated.
Slack doesn't let anyone else read them, so without this step your DMs stay behind.</p>
<p><a class="btn" href="{source_url}">Authorize the old workspace</a></p>
{self._signin_hint(SOURCE)}
<h2>Step 2: new workspace</h2>
<p>Lets the migration post your old messages <b>as you</b>, so they look exactly like you wrote them.</p>
<p>{self._target_button()}</p>
{self._signin_hint(TARGET)}"""
        )

    def handle_callback(self, query: dict[str, list[str]]) -> tuple[int, str]:
        if "error" in query:
            return 400, PAGE.format(body=f"<h1>Authorization cancelled</h1><p>{html.escape(query['error'][0])}</p>")
        code = (query.get("code") or [""])[0]
        side = self._consume_state((query.get("state") or [""])[0])
        if not code or not side:
            return 400, PAGE.format(body='<h1>Link expired</h1><p><a href="/">Start again</a>.</p>')

        client_id, client_secret, redirect_uri = self.apps[side]
        try:
            resp = WebClient().oauth_v2_access(
                client_id=client_id,
                client_secret=client_secret,
                code=code,
                redirect_uri=redirect_uri,
            )
        except SlackApiError as exc:
            log.error("oauth.v2.access failed: %s", error_code(exc))
            return 502, PAGE.format(body=f"<h1>Slack refused the authorization</h1><p>{html.escape(error_code(exc))}</p>")

        authed = resp.get("authed_user") or {}
        user_token = authed.get("access_token")
        if not user_token:
            return 400, PAGE.format(body="<h1>No user token returned</h1><p>Check the app's user scopes.</p>")

        profile = WebClient(token=user_token).users_info(user=authed["id"])["user"]["profile"]
        email = (profile.get("email") or "").strip().lower()
        if not email:
            return 400, PAGE.format(body="<h1>Couldn't read your email</h1><p>users:read.email is required.</p>")
        name = html.escape(profile.get("display_name") or profile.get("real_name") or email)
        record = {
            "user_id": authed["id"],
            "team_id": (resp.get("team") or {}).get("id") or (resp.get("enterprise") or {}).get("id"),
            "token": user_token,
            "scopes": authed.get("scope"),
            "authorized_at": datetime.now(timezone.utc).isoformat(),
        }

        if side == SOURCE:
            source_tokens = self.store.load_source_tokens()
            source_tokens["users"][email] = record
            self.store.save_source_tokens(source_tokens)
            log.info("DM access from %s (%d users total)", email, len(source_tokens["users"]))
            return 200, PAGE.format(
                body=f"""<h1>Step 1 done, {name} ✅</h1>
<p>Now the second step: sign in to the <b>new</b> workspace.</p>
<p>{self._target_button()}</p>"""
            )

        tokens = self.store.load_tokens()
        tokens["users"][email] = record
        if resp.get("access_token"):
            tokens["bot"] = {
                "token": resp["access_token"],
                "bot_user_id": resp.get("bot_user_id"),
                "team_id": (resp.get("team") or {}).get("id") or (resp.get("enterprise") or {}).get("id"),
            }
            tokens["installer_email"] = tokens.get("installer_email") or email
        self.store.save_tokens(tokens)
        log.info("Authorized %s (%d users total)", email, len(tokens["users"]))

        missing_step1 = self.collects_dms and email not in self.store.load_source_tokens()["users"]
        step1 = (
            f'<p class="muted">Haven\'t done step 1 yet? <a href="/">Authorize the old workspace</a> '
            "so your DMs come across too.</p>"
            if missing_step1
            else ""
        )
        return 200, PAGE.format(
            body=f"<h1>Thanks, {name} ✅</h1><p>You're all set ({today()}). You can close this tab.</p>{step1}"
        )


def serve(app: OAuthApp, host: str, port: int, certfile: str | None = None, keyfile: str | None = None) -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server API
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path in app.callback_paths:
                status, body = app.handle_callback(urllib.parse.parse_qs(parsed.query))
            elif parsed.path == "/":
                status, body = 200, app.landing_page()
            else:
                status, body = 404, PAGE.format(body="<h1>Not found</h1>")
            payload = body.encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, fmt: str, *args) -> None:
            # Default logging would print the OAuth code from the query string.
            log.debug("%s %s", self.command, urllib.parse.urlparse(self.path).path)

    server = ThreadingHTTPServer((host, port), Handler)
    scheme = "http"
    if certfile:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile, keyfile)
        server.socket = ctx.wrap_socket(server.socket, server_side=True)
        scheme = "https"
    log.info("OAuth server on %s://%s:%d  (public redirect: %s)", scheme, host, port, app.apps[TARGET][2])
    if not app.collects_dms:
        log.info("SOURCE_CLIENT_ID not set: DMs will not be collected (step 1 is hidden)")
    log.info("Share the public base URL of the redirect with every user of the new workspace.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
