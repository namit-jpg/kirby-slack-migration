import pytest

from slack_migrate.exporter import Exporter
from slack_migrate.importer import Importer, ImportOptions

from .fake_slack import FakeWorkspace
from .test_importer import authorize, run, store, texts  # noqa: F401 - pytest fixture


@pytest.fixture
def ws():
    return FakeWorkspace([
        {"id": "N_A", "email": "alice@x.com"},
        {"id": "N_B", "email": "bob@x.com"},
        {"id": "N_C", "email": "carol@x.com"},
    ])


@pytest.fixture
def dm_store(store):  # noqa: F811 - pytest fixture
    store.save_dms({
        "D1": {"id": "D1", "name": "dm:alice,bob", "is_mpim": False, "members": ["UAAA", "UBBB"], "exported": True},
        "G1": {"id": "G1", "name": "dm:alice,bob,carol", "is_mpim": True, "members": ["UAAA", "UBBB", "UCCC"],
               "exported": True},
        "D2": {"id": "D2", "name": "dm:alice,dave", "is_mpim": False, "members": ["UAAA", "UDDD"], "exported": True},
    })
    store.save_messages("D1", {
        "500.000001": {"ts": "500.000001", "user": "UAAA", "text": "lunch?", "thread_ts": "500.000001",
                       "reply_count": 1, "reactions": [{"name": "yum", "users": ["UBBB"]}], "_pinned": True},
        "500.000002": {"ts": "500.000002", "user": "UBBB", "text": "sure", "thread_ts": "500.000001"},
        "500.000003": {"ts": "500.000003", "bot_id": "B1", "subtype": "bot_message", "text": "reminder"},
        "500.000004": {"ts": "500.000004", "user": "UAAA", "text": "see you in <#C2>"},
    })
    store.save_messages("G1", {
        "600.000001": {"ts": "600.000001", "user": "UCCC", "text": "group hi"},
        "600.000002": {"ts": "600.000002", "user": "UDDD", "text": "departed"},
    })
    store.save_messages("D2", {"700.000001": {"ts": "700.000001", "user": "UAAA", "text": "bye dave"}})
    return store


def dm(ws, *new_ids):
    return next(c for c in ws.channels.values() if c.get("is_dm") and c["members"] == set(new_ids))


def test_dms_open_as_participant_and_resume_in_order(dm_store, ws):
    authorize(dm_store, ("alice@x.com", "N_A"))
    run(dm_store, ws, dms="only")

    assert [c for c in ws.channels.values() if not c.get("is_dm")] == [ws.channels["CGEN"]]  # no channels
    d1 = dm(ws, "N_A", "N_B")
    assert texts(d1) == [("N_A", "lunch?")]  # strict: waits for bob's reply
    assert dm_store.load_state()["dms"]["D1"]["blocked_on"]["author"] == "bob@x.com"
    # Group DM opened by alice, but the first message is carol's.
    assert dm(ws, "N_A", "N_B", "N_C")["messages"] == []
    # Dave will never be in the target, so the 1:1 with him can't exist.
    assert dm_store.load_state()["dms"]["D2"]["skipped"]

    authorize(dm_store, ("bob@x.com", "N_B"), ("carol@x.com", "N_C"))
    run(dm_store, ws)

    assert texts(d1) == [("N_A", "lunch?"), ("N_B", "sure"), ("N_A", f"see you in <#{ws.channel_by_name('proj')['id']}>")]
    assert d1["messages"][1]["thread_ts"] == d1["messages"][0]["ts"]
    assert d1["messages"][0]["reactions"] == {"yum": ["N_B"]} and d1["messages"][0]["pinned"]
    assert dm_store.load_state()["messages"]["D1"]["500.000003"]["status"] == "ignored"  # bot can't post in a DM

    group = dm(ws, "N_A", "N_B", "N_C")
    assert texts(group) == [("N_C", "group hi")]  # departed author dropped, no bot fallback

    # Nothing new: re-running posts nothing.
    before = sum(len(c["messages"]) for c in ws.channels.values())
    run(dm_store, ws)
    assert sum(len(c["messages"]) for c in ws.channels.values()) == before


def test_group_dm_that_would_shrink_to_two_is_skipped(dm_store, ws):
    del ws.users["N_C"]
    authorize(dm_store, ("alice@x.com", "N_A"), ("bob@x.com", "N_B"))
    run(dm_store, ws, dms="only", never_joining={"carol@x.com"})
    assert dm_store.load_state()["dms"]["G1"]["skipped"]
    assert texts(dm(ws, "N_A", "N_B"))[0] == ("N_A", "lunch?")  # the real 1:1 is untouched by the group


def test_dms_skipped_when_channels_selected_or_dry_run(dm_store, ws):
    authorize(dm_store, ("alice@x.com", "N_A"), ("bob@x.com", "N_B"))
    run(dm_store, ws, channels={"proj"})
    assert not any(c.get("is_dm") for c in ws.channels.values())

    run(dm_store, ws, dms="only", dry_run=True)
    assert not any(c.get("is_dm") for c in ws.channels.values())
    assert dm_store.load_state()["dms"] == {}


def test_dm_waits_for_participant_to_join_target(dm_store, ws):
    del ws.users["N_C"]  # carol hasn't joined the new workspace yet (but isn't departed)
    authorize(dm_store, ("alice@x.com", "N_A"))
    Importer(dm_store, ImportOptions(post_interval=0, dms="only"), client_factory=ws.client, sleep=lambda _: None).run()
    state = dm_store.load_state()["dms"]
    assert "G1" not in state or not state["G1"]["new_id"]
    assert state["D1"]["new_id"]


# ---- export ------------------------------------------------------------------------


class FakeSource:
    """Source workspace as seen by one user's token."""

    convs = {
        "D1": {"id": "D1", "is_im": True, "user": "UBBB", "created": 1},
        "DSB": {"id": "DSB", "is_im": True, "user": "USLACKBOT", "created": 1},
        "G1": {"id": "G1", "is_mpim": True, "created": 2},
    }
    history = {
        "D1": [{"ts": "1.1", "user": "UAAA", "text": "hi", "thread_ts": "1.1", "reply_count": 1}],
        "DSB": [{"ts": "1.2", "user": "USLACKBOT", "text": "tip"}],
        "G1": [{"ts": "2.1", "user": "UCCC", "text": "yo"}],
    }

    def __init__(self, token):
        self.token = token
        self.user_id = token.split(":")[1]
        self.calls = []

    def auth_test(self):
        return {"user_id": self.user_id, "team": "Old Co", "team_id": "TOLD"}

    def users_list(self, **_):
        return {"members": [
            {"id": "UAAA", "name": "alice", "profile": {"email": "alice@x.com"}},
            {"id": "UBBB", "name": "bob", "profile": {"email": "bob@x.com"}},
            {"id": "UCCC", "name": "carol", "profile": {"email": "carol@x.com"}},
            {"id": "USLACKBOT", "name": "slackbot", "is_bot": True, "profile": {}},
        ]}

    def conversations_list(self, types, **_):
        assert types == "im,mpim"
        return {"channels": list(self.convs.values())}

    def conversations_members(self, channel, **_):
        return {"members": ["UAAA", "UBBB", "UCCC"]}

    def conversations_history(self, channel, **_):
        self.calls.append(channel)
        return {"messages": self.history[channel]}

    def conversations_replies(self, channel, ts, **_):
        return {"messages": self.history[channel] + [{"ts": "1.5", "user": "UBBB", "text": "re", "thread_ts": ts}]}

    def pins_list(self, channel):
        return {"items": [{"type": "message", "message": {"ts": "2.1"}}]} if channel == "G1" else {"items": []}


def test_export_dms_once_per_conversation(store):  # noqa: F811
    tokens = store.load_source_tokens()
    tokens["users"] = {"alice@x.com": {"token": "src:UAAA", "user_id": "UAAA"},
                       "bob@x.com": {"token": "src:UBBB", "user_id": "UBBB"}}
    store.save_source_tokens(tokens)
    clients = {}

    def factory(token):
        return clients.setdefault(token, FakeSource(token))

    admin = FakeSource("src:UAAA")
    Exporter(admin, "admin", store, download_files=False, client_factory=factory).run(channels=False)

    dms = store.load_dms()
    assert set(dms) == {"D1", "G1"}  # Slackbot DM left out
    assert dms["D1"]["members"] == ["UAAA", "UBBB"] and dms["D1"]["name"] == "dm:alice,bob"
    assert dms["G1"]["is_mpim"] and dms["G1"]["members"] == ["UAAA", "UBBB", "UCCC"]
    assert set(store.load_messages("D1")) == {"1.1", "1.5"}
    assert store.load_messages("G1")["2.1"]["_pinned"]
    # Alice's token exported both; bob's token had nothing new to read.
    assert clients["src:UBBB"].calls == []
    assert store.load_meta()["dms"] == 2


class FakePrivateSource(FakeSource):
    """A member's token: sees the private channels they belong to."""

    privates = {
        "P1": {"id": "P1", "name": "leadership", "is_private": True, "is_member": True, "creator": "UAAA",
               "created": 3, "topic": {"value": "board"}},
        "PBOT": {"id": "PBOT", "name": "bot-invited", "is_private": True, "is_member": True, "created": 4},
    }
    history = {**FakeSource.history, "P1": [{"ts": "3.1", "user": "UAAA", "text": "q3 plan"}],
               "PBOT": [{"ts": "4.1", "user": "UBBB", "text": "already exported"}]}

    def conversations_list(self, types, team_id=None, **_):
        assert types == "private_channel" and team_id == "TOLD"
        return {"channels": list(self.privates.values())}

    def conversations_members(self, channel, **_):
        return {"members": ["UAAA", "UBBB"]}


class FakeBot(FakeSource):
    """SOURCE_TOKEN: only sees the private channel it was invited to."""

    def conversations_list(self, types, **_):
        return {"channels": [FakePrivateSource.privates["PBOT"]]}

    def conversations_members(self, channel, **_):
        return {"members": ["UBBB"]}

    history = FakePrivateSource.history


def test_export_private_channels_with_step1_tokens(store, caplog):  # noqa: F811
    tokens = store.load_source_tokens()
    tokens["users"] = {"alice@x.com": {"token": "src:UAAA", "user_id": "UAAA", "scopes": "im:history,groups:history"},
                       "old@x.com": {"token": "src:UOLD", "user_id": "UOLD", "scopes": "im:history"}}
    store.save_source_tokens(tokens)
    clients = {}

    def factory(token):
        return clients.setdefault(token, FakePrivateSource(token))

    bot = FakeBot("bot:UBOT")
    Exporter(bot, "bot", store, download_files=False, team_id="TOLD", client_factory=factory).run(dms=False)

    channels = store.load_channels()
    assert channels["P1"]["exported"] and channels["P1"]["is_private"] and channels["P1"]["topic"] == "board"
    assert channels["P1"]["members"] == ["UAAA", "UBBB"]
    assert set(store.load_messages("P1")) == {"3.1"}
    # The bot already exported PBOT, so alice's token doesn't read it again.
    assert clients["src:UAAA"].calls == ["P1"] and channels["PBOT"]["members"] == ["UBBB"]
    # A token from before private channels were added is skipped with a hint to re-authorize.
    assert "src:UOLD" not in clients and "old@x.com authorized step 1 before private channels" in caplog.text


# ---- OAuth page ------------------------------------------------------------------------


def test_oauth_routes_source_and_target_tokens(store, monkeypatch):  # noqa: F811
    from urllib.parse import parse_qs, urlparse

    from slack_migrate import oauth_server

    class FakeWebClient:
        def __init__(self, token=None):
            self.token = token

        def oauth_v2_access(self, client_id, **_):
            side = "src" if client_id == "SRC" else "tgt"
            resp = {"authed_user": {"id": f"U_{side}", "access_token": f"xoxp-{side}", "scope": "x"},
                    "team": {"id": f"T_{side}"}}
            if side == "tgt":
                resp["access_token"] = "xoxb-tgt"
            return resp

        def users_info(self, user):
            return {"user": {"profile": {"email": "Alice@X.com", "real_name": "Alice"}}}

    monkeypatch.setattr(oauth_server, "WebClient", FakeWebClient)
    app = oauth_server.OAuthApp(store, "TGT", "s", "https://h/slack/oauth/callback", "SRC", "s2")
    page = app.landing_page()
    assert "Step 1" in page and "im%3Ahistory" in page and "groups%3Ahistory" in page and "im%3Awrite" in page

    def state(side):
        return parse_qs(urlparse(app.authorize_url(side)).query)["state"][0]

    status, body = app.handle_callback({"code": ["c"], "state": [state("source")]})
    assert status == 200 and "Step 1 done" in body
    assert store.load_source_tokens()["users"]["alice@x.com"]["token"] == "xoxp-src"
    assert store.load_tokens()["users"] == {}

    status, body = app.handle_callback({"code": ["c"], "state": [state("target")]})
    assert status == 200 and "Haven't done step 1" not in body
    assert store.load_tokens()["users"]["alice@x.com"]["token"] == "xoxp-tgt"
    assert store.load_tokens()["bot"]["token"] == "xoxb-tgt"

    # A state can't be replayed.
    s = state("source")
    app.handle_callback({"code": ["c"], "state": [s]})
    assert app.handle_callback({"code": ["c"], "state": [s]})[0] == 400

    # Without a source app the page is the original single step.
    assert "Step 1" not in oauth_server.OAuthApp(store, "TGT", "s", "https://h/cb").landing_page()


def test_status_lists_dms_and_dm_access(dm_store, ws):
    from slack_migrate.status import report

    tokens = dm_store.load_source_tokens()
    tokens["users"]["alice@x.com"] = {"token": "src:UAAA", "user_id": "UAAA"}
    dm_store.save_source_tokens(tokens)
    authorize(dm_store, ("alice@x.com", "N_A"))
    run(dm_store, ws, dms="only")
    out = report(Importer(dm_store, client_factory=ws.client))
    assert "DMS (1 people gave DM access)" in out
    assert "dm:alice,bob" in out and "1/4" in out and "bob@x.com" in out
    assert "a participant will never be in the new workspace" in out
    print(out)
