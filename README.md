# Slack workspace migration

Moves one Slack workspace into another. Users are matched by email address. Each message is
posted with its author's own user token, so it shows that person's name and avatar, with no
APP badge.

```
 OLD WORKSPACE                     local disk                         NEW WORKSPACE
┌──────────────────┐   export   ┌──────────────────────┐   import   ┌──────────────────────┐
│ Migration        │──────────▶ │ migration_data/      │──────────▶ │ Migration Importer   │
│ Exporter (bot)   │            │  export/  (snapshot) │            │  bot token + 1 user  │
└──────────────────┘            │  target/tokens.json  │◀── OAuth ──│  token per person    │
                                │  target/state.json   │            └──────────────────────┘
                                └──────────────────────┘
```

* **Exporter app** (source workspace): reads users, channels (public and private), members,
  topics, messages, threads, reactions, pins, files and avatars into `migration_data/export/`.
* **Importer app** (target workspace): each person authorizes it once through a small OAuth
  page. Their user token (`xoxp-`) is stored under their email. The importer then creates the
  channels, invites the original members, and replays every message using the author's own token.
* **Mapping**: `target/state.json` records old channel ID → new channel ID and, for every
  message, old `ts` → new `ts`. Threads, reactions, pins and `#channel` mentions are all rebuilt
  from this mapping. Because of it, every command can be re-run safely, and re-running is how
  delta migration works.

## Setup

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env
```

1. **Source app**: create a Slack app from [`manifests/source_app.yaml`](manifests/source_app.yaml)
   in the old workspace and install it. Put the bot token in `SOURCE_TOKEN`. The bot joins public
   channels on its own. Private channels need `/invite @Migration Exporter`. Alternatively, use an
   admin's user token, which reads every channel that admin belongs to.
2. **Target app**: create a Slack app from [`manifests/target_app.yaml`](manifests/target_app.yaml)
   in the new workspace. Slack only accepts HTTPS redirect URLs, so open a tunnel
   (`ngrok http 3000`, `cloudflared tunnel --url http://localhost:3000`) and set the redirect URL
   in both the manifest and `TARGET_REDIRECT_URI`. Copy the Client ID and Client Secret into `.env`.
   Leave token rotation **off**, or user tokens expire after 12 hours.

## Running a migration

```bash
# 1. Snapshot the old workspace (re-run any time; it overwrites the snapshot)
.venv/bin/python -m slack_migrate export

# 2. Start the authorization page and send its public URL to everyone in the new workspace.
#    The first person to authorize (an admin) also installs the bot.
.venv/bin/python -m slack_migrate serve-oauth

# 3. See who has joined / authorized and what each channel is waiting on
.venv/bin/python -m slack_migrate status

# 4. Preview, then run
.venv/bin/python -m slack_migrate import --dry-run
.venv/bin/python -m slack_migrate import

# 5. Optional: copy display name, title, phone, pronouns and photo onto authorized users
.venv/bin/python -m slack_migrate sync-profiles
```

### Delta migration

When more people join and authorize, or the old workspace gets new messages, run:

```bash
.venv/bin/python -m slack_migrate export   # only if the old workspace has new messages
.venv/bin/python -m slack_migrate import
```

Messages that were already posted are skipped. Reactions from people who authorized late are
added to messages posted earlier. New members are invited to their channels.

### Authors who aren't in the new workspace yet (`--mode`)

| mode | behaviour | channel order |
|---|---|---|
| `strict` (default) | A channel stops at the first message whose author hasn't authorized. The next run continues from there. | exact |
| `skip` | Posts everything it can now. Waiting messages (and replies in their threads) are posted in a later run. | late messages appear at the bottom |
| `impersonate` | Waiting authors are posted through the bot under their name and avatar. | exact, but those messages show **APP** |

Deactivated users, and anyone passed with `--never-joining a@x.com,b@x.com`, are always
posted through the bot (`--departed impersonate`, the default) or dropped (`--departed drop`).
Without this, `strict` mode would wait on them forever.

Other flags: `--channels a,b`, `--stamp-dates` (adds *"Originally sent 12 Mar 2025 14:03 UTC"*),
`--allow-broadcasts`, `--no-archive`, `--interval`.

### Finishing up

```bash
.venv/bin/python -m slack_migrate leave-temp-members   # people added only to post or create a channel
.venv/bin/python -m slack_migrate revoke-tokens --yes  # revoke every stored user token
```

## What carries over

| Item | Result |
|---|---|
| Message author | ✅ posted with the user's own token: real name and avatar, no APP badge |
| Public / private channels, topic, purpose, archived state | ✅ an existing channel with the same name is reused; `#general` maps to the target's default channel |
| Channel membership | ✅ original members who exist in the target are invited |
| Threads, "also send to channel" replies, `/me` messages | ✅ |
| `@user` / `#channel` mentions in text and rich-text blocks | ✅ rewritten to new IDs; anything unmapped becomes plain `@name` |
| Reactions | ✅ added by each reacting user. Custom emoji only work if the same emoji exists in the target |
| Pins, files (re-uploaded by the author), bot/integration messages (via the bot) | ✅ |
| Profiles (display name, title, phone, pronouns, photo) | ✅ with `sync-profiles` |

## Limitations

* **Original timestamps cannot be kept.** No Slack posting API accepts a past timestamp, so every
  message shows the time it was imported. Order within a channel is kept. `--stamp-dates` adds the
  original date to each message.
  The only way to keep real timestamps is Slack's own *Import data* tool (Admin → Import/Export)
  using a Slack export ZIP. It also matches users by email. However, an admin has to run it by
  hand each time, and it does not support the incremental token-based delta flow used here.
* **Notifications**: `@mentions` in old messages notify people again when replayed, and each
  replayed message counts as unread. `@here`, `@channel` and `@everyone` are converted to plain
  text by default. Run imports before people start actively using the new workspace, or ask them
  to turn on Do Not Disturb.
* **Users** cannot be created through this API. People join through an invite, or through SCIM
  on Business+ and Enterprise Grid plans. Each person must authorize once, because Slack never
  lets one account get a token for someone else.
* **Not migrated**: DMs and group DMs (a bot cannot read them), edit history and the "edited"
  marker, custom emoji images, user groups, Canvases, Lists, workflows and huddles. Files that
  the free plan's 90-day limit has hidden are listed as *"(not migrated)"*.
* **Rate limit**: about 1 message per second per channel, so 3,000 messages take roughly 50 minutes.
  Interrupted runs continue where they stopped.

## Security

`migration_data/` holds every exported message and, in `target/tokens.json` (mode 600), a token
that can post as each user. Keep this folder on the migration machine only, never commit it, run
`revoke-tokens` when finished, and delete the folder once you're satisfied.

## Tests

```bash
.venv/bin/python -m pytest -q
```

`tests/fake_slack.py` is an in-memory version of the target Web API. The tests cover strict,
skip and impersonate ordering, delta resume, threads, late reactions, pins, files, private
channels, reuse of existing channels and dry runs.
