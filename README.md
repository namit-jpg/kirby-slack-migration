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
  DMs, group DMs and private channels the bot wasn't invited to are read with each member's own
  token (see below).
* **Importer app** (target workspace): each person authorizes it once through a small OAuth
  page. Their user token (`xoxp-`) is stored under their email. The importer then creates the
  channels, invites the original members, and replays every message using the author's own token.
* **The shared link** has two steps when DM migration is on: **step 1** gives the Exporter app
  read access to that person's DMs and private channels in the old workspace (stored in
  `source/tokens.json`), **step 2** is the Importer authorization above. Slack never lets a bot
  or admin read someone else's DMs, so a person's DMs only move if at least one participant
  completes step 1. A private channel moves if the bot was invited to it or one member did step 1.
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
   **For DMs**, also add the tunnel's redirect URL (step 2) to this app, and copy its Client ID
   and Client Secret into `SOURCE_CLIENT_ID` / `SOURCE_CLIENT_SECRET`. Create the app *inside*
   the client's old workspace (not distributed from another one): Slack heavily rate-limits
   `conversations.history` for distributed non-Marketplace apps. If the workspace requires app
   approval, an admin has to approve it before members can authorize.
2. **Target app**: create a Slack app from [`manifests/target_app.yaml`](manifests/target_app.yaml)
   in the new workspace. Slack only accepts HTTPS redirect URLs, so open a tunnel
   (`ngrok http 3000`, `cloudflared tunnel --url http://localhost:3000`) and set the redirect URL
   in both the manifest and `TARGET_REDIRECT_URI`. Copy the Client ID and Client Secret into `.env`.
   Leave token rotation **off**, or user tokens expire after 12 hours.

## Running a migration

```bash
# 1. Snapshot the old workspace (re-run any time; it overwrites the snapshot)
.venv/bin/python -m slack_migrate export

# 2. Start the authorization page and send its public URL to everyone.
#    Step 1 (old workspace, DMs) appears when SOURCE_CLIENT_ID is set; step 2 is the new workspace.
#    The first person to authorize step 2 (an admin) also installs the bot.
.venv/bin/python -m slack_migrate serve-oauth

# 2b. Once people have done step 1, export again to pick up their DMs and private channels
#     (`--dms only` is enough when you only need the DMs)
.venv/bin/python -m slack_migrate export --dms only

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

### DMs and group DMs

`export` and `import` include DMs by default (`--dms include|skip|only`; with `--channels` the
default is `skip`). Each conversation is exported once, using the first participant who did
step 1. In the new workspace, one authorized participant opens the same DM / group DM
(`conversations.open`) and every message is posted with its author's own token, just like channels.

* A DM waits until **all** its participants are in the new workspace, and each message waits
  until its author has done step 2 (`--mode strict` / `skip` behave as for channels).
* There is no bot fallback inside DMs. Messages from departed users (deactivated or
  `--never-joining`) and from bots are dropped. A 1:1 with a departed user is skipped, as is a
  group DM that would shrink to two people (it would merge into their 1:1).
* DMs with Slackbot or apps are not migrated.
* People who authorized step 2 before DM support was added must authorize step 2 again
  (new `im:write` / `mpim:write` scopes). `import` logs who.
* Likewise, people who did step 1 before private channels were included must do step 1 again
  (new `groups:read` / `groups:history` scopes). `export` logs who.

Other flags: `--channels a,b`, `--stamp-dates` (adds *"Originally sent 12 Mar 2025 14:03 UTC"*),
`--allow-broadcasts`, `--no-archive`, `--interval`.

### Finishing up

```bash
.venv/bin/python -m slack_migrate leave-temp-members   # people added only to post or create a channel
.venv/bin/python -m slack_migrate revoke-tokens --yes  # revoke every stored user token (both workspaces)
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
| DMs and group DMs, with threads, reactions, pins and files | ✅ when a participant did step 1 and everyone is in the target |
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
  text by default. Replayed DMs notify the recipient like a new DM. Run imports before people
  start actively using the new workspace, or ask them to turn on Do Not Disturb.
* **Users** cannot be created through this API. People join through an invite, or through SCIM
  on Business+ and Enterprise Grid plans. Each person must authorize once, because Slack never
  lets one account get a token for someone else.
* **Not migrated**: DMs nobody in them authorized step 1 for, private channels neither the bot
  nor a step 1 member is in, edit history and the "edited"
  marker, custom emoji images, user groups, Canvases, Lists, workflows and huddles. Files that
  the free plan's 90-day limit has hidden are listed as *"(not migrated)"*.
* **Rate limit**: about 1 message per second per channel, so 3,000 messages take roughly 50 minutes.
  Interrupted runs continue where they stopped.

## Security

`migration_data/` holds every exported message, including private DMs. `target/tokens.json`
(mode 600) holds a token that can post as each user, and `source/tokens.json` (mode 600) a
token that can read each user's DMs in the old workspace. Keep this folder on the migration machine only, never commit it, run
`revoke-tokens` when finished, and delete the folder once you're satisfied.

## Tests

```bash
.venv/bin/python -m pytest -q
```

`tests/fake_slack.py` is an in-memory version of the target Web API. The tests cover strict,
skip and impersonate ordering, delta resume, threads, late reactions, pins, files, private
channels, reuse of existing channels, dry runs, and DM export, opening, replay and the two-step
authorization page.
