import pytest

from slack_migrate.importer import Importer, ImportOptions
from slack_migrate.store import Store

from .fake_slack import FakeWorkspace


def _user(uid, email, name, deleted=False):
    return {"id": uid, "name": name, "real_name": name.title(), "display_name": name, "email": email,
            "image": f"https://img/{uid}.png", "is_bot": False, "deleted": deleted}


SOURCE_USERS = {
    "UAAA": _user("UAAA", "alice@x.com", "alice"),
    "UBBB": _user("UBBB", "bob@x.com", "bob"),
    "UCCC": _user("UCCC", "carol@x.com", "carol"),
    "UDDD": _user("UDDD", "dave@x.com", "dave", deleted=True),
}


def _channel(cid, name, members, creator, private=False, general=False, archived=False, topic=""):
    return {"id": cid, "name": name, "is_private": private, "is_general": general, "is_archived": archived,
            "creator": creator, "created": int(cid[1:]) if cid[1:].isdigit() else 0, "topic": topic,
            "purpose": "", "members": members, "exported": True}


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.delenv("TARGET_BOT_TOKEN", raising=False)
    s = Store(tmp_path)
    s.save_meta({"team": "Old Co", "exported_on": "01 Oct 2026"})
    s.save_users(SOURCE_USERS)
    s.save_channels({
        "C0": _channel("C0", "general", ["UAAA", "UBBB", "UCCC"], "UAAA", general=True),
        "C2": _channel("C2", "proj", ["UAAA", "UBBB", "UCCC"], "UBBB", topic="Ship it with <@UBBB>"),
        "C3": _channel("C3", "secret", ["UAAA", "UCCC"], "UCCC", private=True),
    })
    s.save_messages("C0", {
        "100.000001": {"ts": "100.000001", "user": "UAAA", "text": "hi <@UBBB> <!channel>"},
        "100.000002": {"ts": "100.000002", "user": "UCCC", "text": "hello"},
    })
    attachment = tmp_path / "export" / "files" / "F1" / "plan.pdf"
    attachment.parent.mkdir(parents=True)
    attachment.write_bytes(b"%PDF")
    s.save_messages("C2", {
        "200.000001": {"ts": "200.000001", "user": "UAAA", "text": "kickoff", "thread_ts": "200.000001",
                       "reply_count": 1, "_pinned": True,
                       "reactions": [{"name": "tada", "users": ["UAAA", "UBBB"]},
                                     {"name": "custom-party", "users": ["UAAA"]}]},
        "200.000002": {"ts": "200.000002", "user": "UBBB", "text": "on it", "thread_ts": "200.000001"},
        "200.000003": {"ts": "200.000003", "user": "UAAA", "text": "see <#C3>"},
        "200.000004": {"ts": "200.000004", "user": "UCCC", "subtype": "channel_join", "text": "joined"},
        "200.000005": {"ts": "200.000005", "user": "UDDD", "text": "old news"},
        "200.000006": {"ts": "200.000006", "user": "UAAA", "subtype": "file_share", "text": "the plan",
                       "files": [{"id": "F1", "name": "plan.pdf", "_local_path": "files/F1/plan.pdf"},
                                 {"id": "F2", "name": "gone.png", "mode": "tombstone"}]},
    })
    s.save_messages("C3", {
        "300.000001": {"ts": "300.000001", "user": "UCCC", "text": "psst", "thread_ts": "300.000001", "reply_count": 1},
        "300.000002": {"ts": "300.000002", "user": "UAAA", "text": "ok", "thread_ts": "300.000001"},
    })
    return s


@pytest.fixture
def ws():
    return FakeWorkspace([
        {"id": "N_A", "email": "alice@x.com"},
        {"id": "N_B", "email": "bob@x.com"},
        {"id": "N_C", "email": "carol@x.com"},
    ])


def authorize(store, *pairs):
    tokens = store.load_tokens()
    tokens["bot"] = {"token": "bot"}
    tokens["installer_email"] = tokens["installer_email"] or pairs[0][0]
    for email, new_id in pairs:
        tokens["users"][email] = {"token": f"user:{new_id}", "user_id": new_id}
    store.save_tokens(tokens)


def run(store, ws, **opts):
    Importer(store, ImportOptions(post_interval=0, **opts), client_factory=ws.client, sleep=lambda _: None).run()


def texts(ch):
    return [(m["user"], m["text"]) for m in ch["messages"]]


def test_strict_mode_pauses_then_delta_completes_in_order(store, ws):
    authorize(store, ("alice@x.com", "N_A"))
    run(store, ws)

    general = ws.channels["CGEN"]
    assert texts(general) == [("N_A", "hi <@N_B> @channel")]  # live mention, broadcast neutralised

    proj = ws.channel_by_name("proj")
    assert proj["members"] == {"N_A", "N_B", "N_C"}
    assert proj["topic"] == "Ship it with <@N_B>"
    assert texts(proj) == [("N_A", "kickoff")]  # stops at bob's reply: order is preserved
    kickoff = proj["messages"][0]
    assert kickoff["reactions"] == {"tada": ["N_A"]} and kickoff["pinned"]

    secret = ws.channel_by_name("secret")
    assert secret["is_private"] and secret["members"] == {"N_A", "N_C"}
    assert secret["messages"] == []

    state = store.load_state()
    assert state["channels"]["C2"]["blocked_on"]["author"] == "bob@x.com"

    # Delta: bob and carol authorize later; re-running continues where it stopped.
    authorize(store, ("bob@x.com", "N_B"), ("carol@x.com", "N_C"))
    run(store, ws)

    assert texts(general) == [("N_A", "hi <@N_B> @channel"), ("N_C", "hello")]
    new_secret_id = secret["id"]
    assert texts(proj) == [
        ("N_A", "kickoff"),
        ("N_B", "on it"),
        ("N_A", f"see <#{new_secret_id}>"),
        ("N_BOT", "old news"),  # deactivated author -> bot with their name
        ("N_A", "the plan\n📎 _gone.png (not migrated)_"),
    ]
    assert proj["messages"][1]["thread_ts"] == kickoff["ts"]
    assert proj["messages"][3]["username"] == "dave"
    assert proj["messages"][4]["files"] == ["plan.pdf"]
    assert kickoff["reactions"] == {"tada": ["N_A", "N_B"]}  # bob's reaction back-filled

    assert texts(secret) == [("N_C", "psst"), ("N_A", "ok")]
    assert secret["messages"][1]["thread_ts"] == secret["messages"][0]["ts"]

    # Third run with nothing new is a no-op.
    before = sum(len(c["messages"]) for c in ws.channels.values())
    run(store, ws)
    assert sum(len(c["messages"]) for c in ws.channels.values()) == before


def test_skip_mode_posts_what_it_can(store, ws):
    authorize(store, ("alice@x.com", "N_A"))
    run(store, ws, mode="skip")
    proj = ws.channel_by_name("proj")
    assert [t for _, t in texts(proj)] == ["kickoff", "see <#G002>", "old news", "the plan\n📎 _gone.png (not migrated)_"]

    authorize(store, ("bob@x.com", "N_B"))
    run(store, ws, mode="skip")
    assert texts(proj)[-1] == ("N_B", "on it")
    assert proj["messages"][-1]["thread_ts"] == proj["messages"][0]["ts"]


def test_impersonate_mode_and_drop_departed(store, ws):
    authorize(store, ("alice@x.com", "N_A"))
    run(store, ws, mode="impersonate", departed="drop", channels={"proj"})
    proj = ws.channel_by_name("proj")
    assert [(m["user"], m["username"], m["text"]) for m in proj["messages"][:2]] == [
        ("N_A", None, "kickoff"),
        ("N_BOT", "bob", "on it"),
    ]
    assert "old news" not in [m["text"] for m in proj["messages"]]


def test_dry_run_changes_nothing(store, ws):
    authorize(store, ("alice@x.com", "N_A"))
    run(store, ws, dry_run=True)
    assert set(ws.channels) == {"CGEN"} and ws.channels["CGEN"]["messages"] == []
    assert store.load_state()["messages"] == {}


def test_existing_channel_name_is_reused(store, ws):
    authorize(store, ("alice@x.com", "N_A"), ("bob@x.com", "N_B"), ("carol@x.com", "N_C"))
    ws.client("user:N_C").conversations_create(name="proj", is_private=False)
    run(store, ws, channels={"proj"})
    assert [c["name"] for c in ws.channels.values()].count("proj") == 1
    assert len(ws.channel_by_name("proj")["messages"]) == 5


def test_leave_temp_members(store, ws):
    # Only bob has authorized and he was never in #secret, so he has to create it.
    authorize(store, ("bob@x.com", "N_B"))
    run(store, ws, channels={"secret"})
    secret = ws.channel_by_name("secret")
    assert "N_B" in secret["members"]  # bob had to create it; carol was invited
    Importer(store, ImportOptions(channels={"secret"}), client_factory=ws.client).leave_temp_members()
    assert "N_B" not in secret["members"] and "N_C" in secret["members"]


def test_status_report(store, ws):
    from slack_migrate.status import report

    authorize(store, ("alice@x.com", "N_A"))
    run(store, ws)
    out = report(Importer(store, client_factory=ws.client))
    assert "bob@x.com" in out and "#proj" in out and "1/5" in out
    print(out)



def test_general_as_channel_without_invites(store, ws):
    # Source default channel renamed (as in the real demo) and recreated by name, not mapped to target #general.
    channels = store.load_channels()
    channels["C0"]["name"] = "migration-test"
    store.save_channels(channels)
    authorize(store, ("alice@x.com", "N_A"))
    run(store, ws, channels={"migration-test"}, general_as_channel=True, invite_members=False, mode="skip")

    assert ws.channels["CGEN"]["messages"] == []
    copy = ws.channel_by_name("migration-test")
    assert texts(copy) == [("N_A", "hi <@N_B> @channel")]
    assert copy["members"] == {"N_A"}  # nobody else was invited


def test_departed_wait_and_deactivated_but_present_in_target(store, ws):
    authorize(store, ("alice@x.com", "N_A"))
    run(store, ws, channels={"proj"}, mode="skip", departed="wait")
    assert "old news" not in [m["text"] for m in ws.channel_by_name("proj")["messages"]]

    # dave was deactivated in the source but has an account in the target: he waits, then posts as himself.
    ws.users["N_D"] = {"id": "N_D", "email": "dave@x.com"}
    authorize(store, ("dave@x.com", "N_D"))
    run(store, ws, channels={"proj"}, mode="skip")
    assert ("N_D", "old news") in texts(ws.channel_by_name("proj"))


def test_rename_avoids_reusing_existing_channel(store, ws):
    authorize(store, ("alice@x.com", "N_A"), ("bob@x.com", "N_B"), ("carol@x.com", "N_C"))
    ws.client("user:N_C").conversations_create(name="proj", is_private=False)
    run(store, ws, channels={"proj"}, rename={"proj": "proj-from-old"})
    assert ws.channel_by_name("proj")["messages"] == []
    assert len(ws.channel_by_name("proj-from-old")["messages"]) == 5
