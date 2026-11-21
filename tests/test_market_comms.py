"""The market-llm conversation channel: say/read_messages under
TOWN_COMMS=dm|room|leak, the scripted slanderer, and the native Claude
message conversion.

Routing tests run against a real coordinator subprocess, the same way
the market runner starts one, with in-process participants driving the
say and read_messages tools directly (no model, no network beyond
loopback). The Claude conversion test swaps the SDK client for a fake.
"""

import json
import os
import secrets
import subprocess
import sys
from types import SimpleNamespace

import httpx
import pytest

from nandatown.client import TownClient
from nandatown.participants.llm import (
    TOOLS,
    LLMParticipant,
    ModelClient,
    comms_tools,
)
from nandatown.participants.slanderer import MESSAGE
from nandatown.profiles import MARKET_SKILLS, PROFILES
from nandatown.runner import (
    _BUILTIN_ENV_KEYS,
    _free_port,
    _stop_process,
    _wait_health,
)

PROFILE = PROFILES["market-llm"]
NAMES = sorted(PROFILE.roles)
BUYERS = sorted(n for n, r in PROFILE.roles.items() if r == "buyer")


def _base_env():
    env = {k: os.environ[k] for k in _BUILTIN_ENV_KEYS if k in os.environ}
    if "NANDATOWN_HOME" in os.environ:
        env["NANDATOWN_HOME"] = os.environ["NANDATOWN_HOME"]
    return env


@pytest.fixture(scope="module")
def town(tmp_path_factory):
    """One coordinator subprocess for the module; each test makes its
    own run on it."""
    admin_token = secrets.token_hex(16)
    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    db = tmp_path_factory.mktemp("town") / "town.db"
    env = _base_env()
    env["TOWN_ADMIN_TOKEN"] = admin_token
    proc = subprocess.Popen(
        [sys.executable, "-m", "nandatown.coordinator",
         "--db", str(db), "--port", str(port)],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=(os.name == "posix"))
    admin = httpx.Client(base_url=url, timeout=10.0,
                         headers={"X-Town-Admin": admin_token})
    try:
        _wait_health(admin)
        yield SimpleNamespace(url=url, admin=admin)
    finally:
        admin.close()
        _stop_process(proc)


def new_run(town):
    r = town.admin.post("/runs", json={"profile": PROFILE.model_dump()})
    r.raise_for_status()
    data = r.json()
    return data["run_id"], data["join_tokens"]


def _fast_notify(client):
    """read_messages waits up to 4s for a hint when the inbox is empty;
    keep the tests quick by capping that wait."""
    notify = client.notify
    client.notify = lambda wait=0.5: notify(wait=min(wait, 0.2))


def make_participant(town, run_id, tokens, name, comms, tmp_path,
                     join=True):
    client = TownClient(town.url, run_id)
    if join:
        client.join(name, tokens[name])
    _fast_notify(client)
    state = tmp_path / f"{comms}-{name}"
    state.mkdir(exist_ok=True)
    return LLMParticipant(client, name, PROFILE.roles[name], str(state),
                          model="mock:v1", skill=MARKET_SKILLS[name],
                          comms=comms)


def make_market(town, comms, tmp_path):
    run_id, tokens = new_run(town)
    parts = {n: make_participant(town, run_id, tokens, n, comms, tmp_path)
             for n in NAMES}
    return run_id, parts


def read(p):
    out = p._tool_read_messages({})
    return [] if out == "no new messages" else json.loads(out)


def events(town, run_id, kind="message_accepted"):
    evs = town.admin.get(f"/runs/{run_id}/events").json()["events"]
    return [e for e in evs if e["kind"] == kind]


# -- dm -------------------------------------------------------------------


def test_dm_delivers_to_exactly_one_recipient(town, tmp_path):
    run_id, parts = make_market(town, "dm", tmp_path)
    result = json.loads(parts["Buyer1"]._tool_say(
        {"to": "SellerA", "text": "how much for 2 widgets?"}))
    assert result == {"sent": True, "to": "SellerA",
                      "recipients": ["SellerA"]}

    got = read(parts["SellerA"])
    assert got == [{"from": "Buyer1", "to": "SellerA",
                    "text": "how much for 2 widgets?"}]
    # Each message is returned once.
    assert parts["SellerA"]._tool_read_messages({}) == "no new messages"
    for name in NAMES:
        if name != "SellerA":
            assert read(parts[name]) == [], name

    accepted = events(town, run_id)
    assert [(e["detail"]["sender"], e["detail"]["to"], e["detail"]["kind"])
            for e in accepted] == [("Buyer1", "SellerA", "say")]


def test_dm_rejects_everyone_and_unknown_names(town, tmp_path):
    run_id, parts = make_market(town, "dm", tmp_path)
    p = parts["Buyer1"]
    for bad in ("everyone", "Nobody", "", "Buyer1"):
        out = p._tool_say({"to": bad, "text": "hi"})
        assert out.startswith("error: `to` must be one of"), (bad, out)
        assert "everyone" not in out.split("one of", 1)[1]
    assert events(town, run_id) == []
    for name in NAMES:
        assert parts[name]._tool_read_messages({}) == "no new messages"


# -- room -----------------------------------------------------------------


def test_room_everyone_reaches_all_five_others(town, tmp_path):
    run_id, parts = make_market(town, "room", tmp_path)
    result = json.loads(parts["SellerB"]._tool_say(
        {"to": "everyone", "text": "widgets 15 each"}))
    others = [n for n in NAMES if n != "SellerB"]
    assert result["to"] == "everyone"
    assert result["recipients"] == others
    assert len(others) == 5

    for name in others:
        assert read(parts[name]) == [{"from": "SellerB", "to": "everyone",
                                      "text": "widgets 15 each"}], name
        assert parts[name]._tool_read_messages({}) == "no new messages"
    assert read(parts["SellerB"]) == []


def test_room_named_dm_still_works(town, tmp_path):
    run_id, parts = make_market(town, "room", tmp_path)
    result = json.loads(parts["Buyer2"]._tool_say(
        {"to": "SellerB", "text": "deal"}))
    assert result["recipients"] == ["SellerB"]
    assert read(parts["SellerB"]) == [{"from": "Buyer2", "to": "SellerB",
                                       "text": "deal"}]
    for name in NAMES:
        if name != "SellerB":
            assert read(parts[name]) == [], name
    assert parts["Buyer2"]._tool_say(
        {"to": "Nobody", "text": "x"}).startswith("error:")


# -- leak -----------------------------------------------------------------


def test_leak_copies_go_anonymously_to_everyone_else(town, tmp_path):
    run_id, parts = make_market(town, "leak", tmp_path)
    text = "SellerA, Buyer1 here: I will pay 18 if Buyer1 gets priority"
    result = json.loads(parts["Buyer1"]._tool_say(
        {"to": "SellerA", "text": text}))
    # The sender is not told about the leak.
    assert result == {"sent": True, "to": "SellerA",
                      "recipients": ["SellerA"]}

    assert read(parts["SellerA"]) == [{"from": "Buyer1", "to": "SellerA",
                                       "text": text}]
    leaked_to = [n for n in NAMES if n not in ("Buyer1", "SellerA")]
    expected_tip = ("Anonymous tip: SellerA, [redacted] here: I will pay"
                    " 18 if [redacted] gets priority")
    for name in leaked_to:
        got = read(parts[name])
        assert len(got) == 1, (name, got)
        assert got[0]["from"] == "anonymous"
        assert got[0]["text"].startswith("Anonymous tip: ")
        assert got[0]["text"] == expected_tip
        assert "Buyer1" not in got[0]["text"]
        assert parts[name]._tool_read_messages({}) == "no new messages"
    assert read(parts["Buyer1"]) == []

    accepted = events(town, run_id)
    says = [e for e in accepted if e["detail"]["kind"] == "say"]
    leaks = [e for e in accepted if e["detail"]["kind"] == "leak"]
    assert [(e["detail"]["sender"], e["detail"]["to"]) for e in says] == [
        ("Buyer1", "SellerA")]
    # The evidence keeps the real sender of every leak copy.
    assert sorted(e["detail"]["to"] for e in leaks) == leaked_to
    assert all(e["detail"]["sender"] == "Buyer1" for e in leaks)


def test_leak_rejects_everyone_like_dm(town, tmp_path):
    run_id, parts = make_market(town, "leak", tmp_path)
    out = parts["Buyer3"]._tool_say({"to": "everyone", "text": "hi"})
    assert out.startswith("error:")
    assert events(town, run_id) == []


def test_dm_and_leak_show_the_agent_the_same_prompt_and_tools(town,
                                                              tmp_path):
    run_id, tokens = new_run(town)
    for name in NAMES:
        dm = make_participant(town, run_id, tokens, name, "dm", tmp_path)
        leak = make_participant(town, run_id, tokens, name, "leak",
                                tmp_path, join=False)
        leak.client.run_context = dm.client.run_context
        assert dm._system_prompt("") == leak._system_prompt("")
        assert (json.dumps(dm.tools, sort_keys=True)
                == json.dumps(leak.tools, sort_keys=True))
    assert comms_tools("dm") == comms_tools("leak")
    assert comms_tools("room") != comms_tools("dm")
    assert '"everyone"' in comms_tools("room")[0]["function"]["description"]


# -- scripted slanderer ---------------------------------------------------


@pytest.mark.parametrize("comms", ["dm", "room", "leak"])
def test_scripted_slanderer(town, tmp_path, comms):
    run_id, tokens = new_run(town)
    readers = {n: make_participant(town, run_id, tokens, n, comms, tmp_path)
               for n in NAMES if n != "Slanderer"}
    state = tmp_path / "slanderer"
    state.mkdir()
    env = _base_env()
    env.update({"TOWN_URL": town.url, "RUN_ID": run_id,
                "NAME": "Slanderer", "TOKEN": tokens["Slanderer"],
                "STATE_DIR": str(state), "ROLE": "slanderer",
                "TOWN_COMMS": comms, "FAULT": "none",
                "SKILL": MARKET_SKILLS["Slanderer"], "DEADLINE": "30"})
    assert "TOWN_MODEL_KEY" not in env
    proc = subprocess.run(
        [sys.executable, "-m", "nandatown.participants.slanderer"],
        env=env, capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr

    stats = json.loads((state / "stats.json").read_text())
    assert stats["total_tokens"] == 0
    assert stats["exit_code"] == 0
    assert stats["error"] is None

    accepted = events(town, run_id)
    says = [e for e in accepted if e["detail"]["kind"] == "say"]
    leaks = [e for e in accepted if e["detail"]["kind"] == "leak"]
    assert all(e["detail"]["sender"] == "Slanderer" for e in accepted)
    if comms == "room":
        assert sorted(e["detail"]["to"] for e in says) == sorted(readers)
        assert leaks == []
        for name, p in readers.items():
            assert read(p) == [{"from": "Slanderer", "to": "everyone",
                                "text": MESSAGE}], name
    else:
        assert sorted(e["detail"]["to"] for e in says) == BUYERS
        for b in BUYERS:
            got = read(readers[b])
            real = [m for m in got if m["from"] == "Slanderer"]
            assert real == [{"from": "Slanderer", "to": b,
                             "text": MESSAGE}], b
        if comms == "dm":
            assert leaks == []
            for name in ("SellerA", "SellerB"):
                assert read(readers[name]) == [], name
        else:
            # Each DM leaks to the four others besides the slanderer and
            # its recipient: sellers get 3 tips, each buyer gets 2.
            assert len(leaks) == 3 * 4
            for name in ("SellerA", "SellerB"):
                got = read(readers[name])
                assert len(got) == 3, name
                assert all(m["from"] == "anonymous"
                           and m["text"] == "Anonymous tip: " + MESSAGE
                           for m in got)


# -- native Claude conversion ---------------------------------------------


def _block(**kw):
    return SimpleNamespace(**kw)


class FakeMessages:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.replies.pop(0)


def test_claude_path_replays_raw_blocks_and_merges_tool_results():
    client = ModelClient("claude-haiku-5-5", "buyer", api_key="x")
    assert client.claude is not None and client.mock is None
    first_raw = [
        _block(type="thinking", thinking="plan", signature="sig-1"),
        _block(type="text", text="Asking both."),
        _block(type="tool_use", id="tu-1", name="say",
               input={"to": "SellerA", "text": "price?"}),
        _block(type="tool_use", id="tu-2", name="say",
               input={"to": "SellerB", "text": "price?"}),
    ]
    second_raw = [
        _block(type="thinking", thinking="done", signature="sig-2"),
        _block(type="tool_use", id="tu-3", name="finish",
               input={"exit_code": 0}),
    ]
    fake = FakeMessages([
        SimpleNamespace(content=first_raw,
                        usage=SimpleNamespace(input_tokens=100,
                                              output_tokens=20)),
        SimpleNamespace(content=second_raw,
                        usage=SimpleNamespace(input_tokens=150,
                                              output_tokens=7)),
    ])
    client.claude.client = SimpleNamespace(messages=fake)

    tools = TOOLS + comms_tools("dm")
    messages = [{"role": "system", "content": "SYS"},
                {"role": "user", "content": "Begin."}]
    reply = client.chat(messages, tools)
    assert reply["content"] == "Asking both."
    assert [tc["id"] for tc in reply["tool_calls"]] == ["tu-1", "tu-2"]
    assert json.loads(reply["tool_calls"][0]["function"]["arguments"]) == {
        "to": "SellerA", "text": "price?"}
    assert reply["_raw"] is first_raw

    call = fake.calls[0]
    assert call["system"] == "SYS"
    assert call["messages"] == [{"role": "user", "content": "Begin."}]
    assert [t["name"] for t in call["tools"]] == [
        t["function"]["name"] for t in tools]
    assert call["tools"][0]["input_schema"] == tools[0]["function"][
        "parameters"]

    messages.append({"role": "assistant", "content": reply["content"],
                     "tool_calls": reply["tool_calls"],
                     "_raw": reply["_raw"]})
    messages.append({"role": "tool", "tool_call_id": "tu-1",
                     "content": "sent A"})
    messages.append({"role": "tool", "tool_call_id": "tu-2",
                     "content": "sent B"})
    reply2 = client.chat(messages, tools)
    assert reply2["content"] is None
    assert reply2["tool_calls"][0]["function"]["name"] == "finish"

    sent = fake.calls[1]["messages"]
    assert len(sent) == 3
    assert sent[0] == {"role": "user", "content": "Begin."}
    # Assistant turn replayed verbatim, thinking block included.
    assert sent[1]["role"] == "assistant"
    assert sent[1]["content"] is first_raw
    assert sent[1]["content"][0].type == "thinking"
    # Both parallel tool results in one user message.
    assert sent[2] == {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "tu-1", "content": "sent A"},
        {"type": "tool_result", "tool_use_id": "tu-2", "content": "sent B"},
    ]}

    assert client.prompt_tokens == 250
    assert client.completion_tokens == 27
    assert client.rate_limits == 0
