"""The market-llm experiment runner: six model agents, one coordinator.

run_town is built around one buyer and one seller, so the market gets its
own small loop: start the coordinator, spawn every participant with its
own skill, wait for the buyers, collect events, intents and per-agent
stats into out_dir/<run_id>/. TOWN_COMMS (dm|room|leak) is the only switch.

TOWN_MODEL_KEY is passed to participant processes through the
environment and is never written, logged or printed here; the saved
files are scanned for it and redacted as a last line of defence.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import sys
import time
from typing import Any

import httpx

from .profiles import MARKET_DEADLINE, MARKET_SKILLS, PROFILES
from .runner import (_BUILTIN_ENV_KEYS, RunnerError, _free_port,
                     _participant_extra_env, _stop_process, _wait_health)

COMMS_MODES = ("dm", "room", "leak", "anon_room", "echo_dm")
MODERATORS = ("off", "rule")
# TOWN_DISGUISE=1 renames the slanderer to this buyer-like name, with role
# "buyer" and no capabilities, so nothing buyers see marks it out.
DISGUISE_NAME = "Buyer4"


def _disguised(profile):
    roles = {(DISGUISE_NAME if n == "Slanderer" else n): r
             for n, r in profile.roles.items()}
    roles[DISGUISE_NAME] = "buyer"
    runtimes = {(DISGUISE_NAME if n == "Slanderer" else n): r
                for n, r in profile.runtimes.items()}
    return profile.model_copy(update={"name": profile.name + "-disguised",
                                      "roles": roles, "runtimes": runtimes})


def _git_commit() -> str | None:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True,
            cwd=os.path.dirname(os.path.abspath(__file__)),
            timeout=5).stdout.strip() or None
    except Exception:
        return None
# After the last buyer finishes, sellers and the slanderer get this long
# to wrap up before they are stopped.
GRACE_SECONDS = 20.0


def run_market(profile_name: str, out_dir: str,
               model: str | None = None,
               comms: str | None = None) -> tuple[str, dict[str, Any]]:
    profile = PROFILES[profile_name]
    comms = comms or os.environ.get("TOWN_COMMS", "dm")
    if comms not in COMMS_MODES:
        raise RunnerError(f"TOWN_COMMS must be one of {COMMS_MODES}")
    moderator = os.environ.get("TOWN_MODERATOR", "off")
    if moderator not in MODERATORS:
        raise RunnerError(f"TOWN_MODERATOR must be one of {MODERATORS}")
    disguise = os.environ.get("TOWN_DISGUISE", "0") == "1"
    slanderer_name = "Slanderer"
    if disguise:
        profile = _disguised(profile)
        slanderer_name = DISGUISE_NAME
    # TOWN_BUYERS=N (disguised only): N model buyers Buyer1..BuyerN and the
    # scripted slanderer as Buyer<N+1>; default 3 leaves everything as is.
    n_buyers = int(os.environ.get("TOWN_BUYERS", "3"))
    if n_buyers != 3:
        if not disguise:
            raise RunnerError("TOWN_BUYERS other than 3 needs TOWN_DISGUISE=1")
        slanderer_name = f"Buyer{n_buyers + 1}"
        names = ([f"Buyer{i}" for i in range(1, n_buyers + 1)]
                 + [slanderer_name])
        roles = {"SellerA": "seller", "SellerB": "seller",
                 **{n: "buyer" for n in names}}
        profile = profile.model_copy(update={
            "name": f"{profile.name}-{n_buyers}",
            "roles": roles, "runtimes": {n: "llm" for n in roles}})
    model = model or os.environ.get("TOWN_MODEL", "mock:v1")
    admin_token = secrets.token_hex(16)
    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    os.makedirs(out_dir, exist_ok=True)
    scratch = os.path.join(out_dir, f".scratch-{secrets.token_hex(4)}")
    os.makedirs(scratch)

    env = {k: os.environ[k] for k in _BUILTIN_ENV_KEYS if k in os.environ}
    env["TOWN_ADMIN_TOKEN"] = admin_token
    admin = httpx.Client(base_url=url, timeout=10.0,
                         headers={"X-Town-Admin": admin_token})
    procs: dict[str, subprocess.Popen] = {}
    coordinator = subprocess.Popen(
        [sys.executable, "-m", "nandatown.coordinator",
         "--db", os.path.join(scratch, "town.db"), "--port", str(port)],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=(os.name == "posix"))
    started = time.time()
    try:
        _wait_health(admin)
        created = admin.post("/runs", json={"profile": profile.model_dump()})
        created.raise_for_status()
        run_id = created.json()["run_id"]
        tokens = created.json()["join_tokens"]

        for name, role in profile.roles.items():
            state = os.path.join(scratch, name)
            os.makedirs(state)
            penv = dict(env)
            penv.pop("TOWN_ADMIN_TOKEN")
            penv.update({"TOWN_URL": url, "RUN_ID": run_id, "NAME": name,
                         "TOKEN": tokens[name], "STATE_DIR": state,
                         "FAULT": "none", "ROLE": role,
                         "SKILL": MARKET_SKILLS.get(name, (
                             "market.buyer" if role == "buyer"
                             and name != slanderer_name
                             else "market.slanderer")),
                         "TOWN_COMMS": comms,
                         "DEADLINE": str(MARKET_DEADLINE)})
            if moderator != "off":
                penv["TOWN_MODERATOR"] = moderator
            is_slanderer = name == slanderer_name
            if not is_slanderer:  # scripted: no model, no key
                penv.update(_participant_extra_env("llm", {}, model))
            with open(os.path.join(state, "stderr.log"), "w") as err:
                # The slanderer is scripted: no model, one fixed message.
                module = "slanderer" if is_slanderer else "llm"
                procs[name] = subprocess.Popen(
                    [sys.executable, "-m", f"nandatown.participants.{module}"],
                    env=penv, stdout=subprocess.DEVNULL, stderr=err,
                    start_new_session=(os.name == "posix"))

        # Only real buyers end the run; a disguised slanderer exits early.
        buyers = [n for n, r in profile.roles.items()
                  if r == "buyer" and n != slanderer_name]
        hard_stop = started + MARKET_DEADLINE + 15.0
        buyers_done_at = None
        while time.time() < hard_stop:
            if all(p.poll() is not None for p in procs.values()):
                break
            if buyers_done_at is None and all(
                    procs[b].poll() is not None for b in buyers):
                buyers_done_at = time.time()
            if (buyers_done_at is not None
                    and time.time() > buyers_done_at + GRACE_SECONDS):
                break
            time.sleep(0.5)
        exit_codes = {n: _stop_process(p) for n, p in procs.items()}
        admin.post(f"/runs/{run_id}/finish").raise_for_status()
        events = admin.get(f"/runs/{run_id}/events").json()["events"]
        intents = admin.get(f"/runs/{run_id}/intents").json()["intents"]

        stats = {}
        for name in profile.roles:
            path = os.path.join(scratch, name, "stats.json")
            if os.path.exists(path):
                with open(path) as f:
                    stats[name] = json.load(f)
            else:
                stats[name] = {"name": name, "exit_code": exit_codes[name],
                               "finish_note": None, "total_tokens": 0,
                               "error": "stopped before writing stats"}
        total_tokens = sum(s.get("total_tokens", 0) for s in stats.values())
        record = {"run_id": run_id, "profile": profile.name,
                  "comms": comms, "model": model,
                  "deadline_seconds": MARKET_DEADLINE,
                  "duration_seconds": round(time.time() - started, 1),
                  "exit_codes": exit_codes, "participants": stats,
                  "total_tokens": total_tokens,
                  "rate_limits": sum(s.get("rate_limits", 0)
                                     for s in stats.values())}
        if disguise or moderator != "off" or comms not in (
                "dm", "room", "leak"):
            # Extra fields only for the new conditions, so the original
            # dm/room/leak record shape is unchanged.
            record.update({"disguise": disguise,
                           "slanderer_name": slanderer_name,
                           "moderator": moderator,
                           "git_commit": _git_commit()})
        if n_buyers != 3:
            record["buyers"] = [n for n, r in profile.roles.items()
                                if r == "buyer" and n != slanderer_name]
        bundle = os.path.join(out_dir, run_id)
        os.makedirs(bundle)
        for fname, data in (("run.json", record), ("events.json", events),
                            ("intents.json", intents)):
            with open(os.path.join(bundle, fname), "w") as f:
                json.dump(data, f, indent=1)
        shutil.move(scratch, os.path.join(bundle, "state"))
        _redact_key(bundle)
        return bundle, record
    finally:
        for p in procs.values():
            _stop_process(p)
        _stop_process(coordinator)
        admin.close()
        if os.path.isdir(scratch):
            shutil.rmtree(scratch, ignore_errors=True)


def _redact_key(root: str) -> None:
    key = os.environ.get("TOWN_MODEL_KEY")
    if not key:
        return
    needle = key.encode()
    for dirpath, _, files in os.walk(root):
        for fname in files:
            path = os.path.join(dirpath, fname)
            with open(path, "rb") as f:
                data = f.read()
            if needle in data:
                with open(path, "wb") as f:
                    f.write(data.replace(needle, b"<redacted>"))
