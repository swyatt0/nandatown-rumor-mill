"""The varied-wording scripted slanderer, run as a real subprocess
against a real coordinator: dm and leak modes give each buyer its own
wording, room mode broadcasts the static message, no tokens are used."""

import json
import os
import secrets
import subprocess
import sys

import httpx
import pytest

from nandatown.participants.slanderer import MESSAGE as STATIC_MESSAGE
from nandatown.participants.slanderer_varied import VARIANTS
from nandatown.profiles import PROFILES
from nandatown.runner import _free_port, _stop_process, _wait_health

PROFILE = PROFILES["market-llm"]
BUYERS = sorted(n for n, r in PROFILE.roles.items() if r == "buyer")


def _run_slanderer(tmp_path, comms):
    admin_token = secrets.token_hex(16)
    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    env = dict(os.environ)
    env["TOWN_ADMIN_TOKEN"] = admin_token
    coordinator = subprocess.Popen(
        [sys.executable, "-m", "nandatown.coordinator",
         "--db", str(tmp_path / "town.db"), "--port", str(port)],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=(os.name == "posix"))
    admin = httpx.Client(base_url=url, timeout=10.0,
                         headers={"X-Town-Admin": admin_token})
    try:
        _wait_health(admin)
        created = admin.post("/runs", json={"profile": PROFILE.model_dump()})
        created.raise_for_status()
        run_id = created.json()["run_id"]
        tokens = created.json()["join_tokens"]
        state = tmp_path / "Slanderer"
        state.mkdir()
        penv = dict(env)
        penv.pop("TOWN_ADMIN_TOKEN")
        penv.update({"TOWN_URL": url, "RUN_ID": run_id, "NAME": "Slanderer",
                     "TOKEN": tokens["Slanderer"], "STATE_DIR": str(state),
                     "FAULT": "none", "ROLE": "slanderer",
                     "TOWN_COMMS": comms})
        proc = subprocess.run(
            [sys.executable, "-m", "nandatown.participants.slanderer_varied"],
            env=penv, capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, proc.stderr
        intents = admin.get(f"/runs/{run_id}/intents").json()["intents"]
        sends = [i["payload"] for i in intents
                 if i["action"] == "send" and i["actor"] == "Slanderer"]
        stats = json.loads((state / "stats.json").read_text())
        return sends, stats
    finally:
        admin.close()
        _stop_process(coordinator)


def test_variants_shape():
    assert len(VARIANTS) == 3
    assert len(set(VARIANTS)) == 3
    assert VARIANTS[0] == STATIC_MESSAGE
    assert len(BUYERS) == 3


@pytest.mark.parametrize("comms", ["dm", "leak"])
def test_each_buyer_gets_a_distinct_wording(tmp_path, comms):
    sends, stats = _run_slanderer(tmp_path, comms)
    says = [s for s in sends if s["kind"] == "say"]
    assert [s["to"] for s in says] == BUYERS
    texts = {s["to"]: s["body"]["text"] for s in says}
    # Deterministic: sorted buyer i gets variant i.
    assert texts == dict(zip(BUYERS, VARIANTS))
    assert set(texts.values()) == set(VARIANTS)
    leaks = [s for s in sends if s["kind"] == "leak"]
    if comms == "dm":
        assert leaks == []
    else:
        # Each DM is leaked, as an anonymous tip of that same wording, to
        # everyone but the slanderer and the original recipient.
        others = sorted(n for n in PROFILE.roles if n != "Slanderer")
        for buyer in BUYERS:
            copies = [s for s in leaks
                      if s["body"]["original_to"] == buyer]
            assert sorted(s["to"] for s in copies) == sorted(
                set(others) - {buyer})
            for s in copies:
                assert s["body"]["text"] == "Anonymous tip: " + texts[buyer]
    assert stats["total_tokens"] == 0
    assert stats["exit_code"] == 0
    assert stats["error"] is None


def test_room_mode_broadcasts_the_static_message(tmp_path):
    sends, stats = _run_slanderer(tmp_path, "room")
    says = [s for s in sends if s["kind"] == "say"]
    assert {s["body"]["say_id"] for s in says} == {"say-Slanderer-1"}
    assert all(s["body"]["to"] == "everyone" for s in says)
    assert all(s["body"]["text"] == VARIANTS[0] for s in says)
    assert sorted(s["to"] for s in says) == sorted(
        n for n in PROFILE.roles if n != "Slanderer")
    assert not [s for s in sends if s["kind"] == "leak"]
    assert stats["total_tokens"] == 0
    assert stats["exit_code"] == 0
