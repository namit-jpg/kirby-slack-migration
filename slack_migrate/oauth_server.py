"""Target-workspace app: a tiny OAuth server that collects one user token per person.

Messages can only appear as genuinely sent by a person (no APP badge, real avatar/name)
when they're posted with that person's own user token (xoxp-). Each user in the new
workspace opens this page once and clicks "Authorize"; we store their token keyed by email.

The first person to authorize (normally a workspace admin) also installs the app, which
gives us the bot token used for read-only lookups and the optional bot fallback.
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
]

PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Workspace migration</title>
<style>body{{font:16px/1.5 -apple-system,system-ui,sans-serif;max-width:560px;margin:64px auto;padding:0 16px;color:#1d1c1d}}
a.btn{{display:inline-block;background:#4a154b;color:#fff;padding:12px 20px;border-radius:6px;text-decoration:none;font-weight:600}}
.muted{{color:#616061;font-size:14px}}</style></head><body>{body}</body></html>"""


class OAuthApp:
    def __init__(self, store: Store, client_id: str, client_secret: str, redirect_uri: str):
        self.store = store
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.callback_path = urllib.parse.urlparse(redirect_uri).path or "/"
        self._states: dict[str, float] = {}

    def authorize_url(self) -> str:
        state = secrets.token_urlsafe(24)
        self._states[state] = time.time()
        params = {
            "client_id": self.client_id,
            "scope": ",".join(BOT_SCOPES),
            "user_scope": ",".join(USER_SCOPES),
            "redirect_uri": self.redirect_uri,
            "state": state,
        }
        return "https://slack.com/oauth/v2/authorize?" + urllib.parse.urlencode(params)

    def _consume_state(self, state: str) -> bool:
        created = self._states.pop(state, None)
        return created is not None and time.time() - created < 15 * 60

    def landing_page(self) -> str:
        url = html.escape(self.authorize_url())
        return PAGE.format(
            body=f"""<h1>Move your messages to the new workspace</h1>
<p>Authorizing lets the migration post your old messages <b>as you</b>, so they look exactly
like you wrote them. Your token is stored only on the migration machine and is revoked after
the migration finishes.</p>
<p><a class="btn" href="{url}">Authorize with Slack</a></p>
<p class="muted">Sign in to the <b>new</b> workspace when Slack asks.</p>"""
        )

    def handle_callback(self, query: dict[str, list[str]]) -> tuple[int, str]:
        if "error" in query:
            return 400, PAGE.format(body=f"<h1>Authorization cancelled</h1><p>{html.escape(query['error'][0])}</p>")
        code = (query.get("code") or [""])[0]
        state = (query.get("state") or [""])[0]
        if not code or not self._consume_state(state):
            return 400, PAGE.format(body='<h1>Link expired</h1><p><a href="/">Start again</a>.</p>')

        try:
            resp = WebClient().oauth_v2_access(
                client_id=self.client_id,
                client_secret=self.client_secret,
                code=code,
                redirect_uri=self.redirect_uri,
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

        tokens = self.store.load_tokens()
        tokens["users"][email] = {
            "user_id": authed["id"],
            "team_id": (resp.get("team") or {}).get("id") or (resp.get("enterprise") or {}).get("id"),
            "token": user_token,
            "scopes": authed.get("scope"),
            "authorized_at": datetime.now(timezone.utc).isoformat(),
        }
        if resp.get("access_token"):
            tokens["bot"] = {
                "token": resp["access_token"],
                "bot_user_id": resp.get("bot_user_id"),
                "team_id": (resp.get("team") or {}).get("id") or (resp.get("enterprise") or {}).get("id"),
            }
            tokens["installer_email"] = tokens.get("installer_email") or email
        self.store.save_tokens(tokens)
        log.info("Authorized %s (%d users total)", email, len(tokens["users"]))

        name = html.escape(profile.get("display_name") or profile.get("real_name") or email)
        return 200, PAGE.format(
            body=f"<h1>Thanks, {name} ✅</h1><p>You're all set ({today()}). You can close this tab.</p>"
        )


def serve(app: OAuthApp, host: str, port: int, certfile: str | None = None, keyfile: str | None = None) -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server API
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == app.callback_path:
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
    log.info("OAuth server on %s://%s:%d  (public redirect: %s)", scheme, host, port, app.redirect_uri)
    log.info("Share the public base URL of the redirect with every user of the new workspace.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
