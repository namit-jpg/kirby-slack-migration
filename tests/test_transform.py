from slack_migrate.transform import Mapper

M = Mapper(
    user_ids={"U1": "N1"},
    user_names={"U1": "alice", "U2": "bob"},
    channel_ids={"C1": "D1"},
    channel_names={"C1": "general", "C2": "random"},
)


def test_text_rewrites_and_fallbacks():
    src = "hey <@U1> and <@U2|bob> in <#C1|general> / <#C2> <!subteam^S1|@eng> <!here>"
    assert M.text(src) == "hey <@N1> and @bob in <#D1> / #random @eng @here"


def test_broadcasts_kept_when_allowed():
    m = Mapper(neutralize_broadcasts=False)
    assert m.text("<!channel> hi") == "<!channel> hi"


def test_blocks_rewrite_rich_text():
    blocks = [{"type": "rich_text", "elements": [{"type": "rich_text_section", "elements": [
        {"type": "user", "user_id": "U1"},
        {"type": "user", "user_id": "U2", "style": {"bold": True}},
        {"type": "channel", "channel_id": "C2"},
        {"type": "broadcast", "range": "channel"},
        {"type": "text", "text": "ok"},
    ]}]}]
    out = M.blocks(blocks)[0]["elements"][0]["elements"]
    assert out == [
        {"type": "user", "user_id": "N1"},
        {"type": "text", "text": "@bob", "style": {"bold": True}},
        {"type": "text", "text": "#random"},
        {"type": "text", "text": "@channel"},
        {"type": "text", "text": "ok"},
    ]
    assert blocks[0]["elements"][0]["elements"][0]["user_id"] == "U1"  # input untouched


def test_mrkdwn_in_section_blocks():
    out = M.blocks([{"type": "section", "text": {"type": "mrkdwn", "text": "<@U1>"}}])
    assert out[0]["text"]["text"] == "<@N1>"
