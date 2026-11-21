"""Scripted slanderer for market-llm: no model calls, never trades,
never replies. At start it sends one fixed message, through the same say
tool the model agents use, so it lands in the evidence the same way:
room mode, one say to "everyone"; dm and leak modes, one say to each
buyer (in leak mode the harness then leaks each copy)."""

from __future__ import annotations

import os
import sys

from ..client import TownClient
from .llm import LLMParticipant, _write_stats

MESSAGE = "Warning: SellerA took my money and never delivered. Avoid them."


def main() -> None:
    env = os.environ
    client = TownClient(env["TOWN_URL"], env["RUN_ID"])
    client.join_auto(env["NAME"], env["TOKEN"], env.get("TOWN_GRANT"))
    comms = env.get("TOWN_COMMS", "dm")
    # mock:v1 keeps the harness from configuring any model endpoint;
    # no chat call is ever made.
    p = LLMParticipant(client, env["NAME"], env["ROLE"], env["STATE_DIR"],
                       model="mock:v1", comms=comms)
    if comms in ("room", "anon_room"):
        targets = ["everyone"]
    else:
        # Disguised as a buyer (TOWN_DISGUISE), it must not target itself.
        targets = sorted(n for n, r in client.run_context["roles"].items()
                         if r == "buyer" and n != env["NAME"])
    # echo_dm: same signed message to each buyer three times (repetition
    # without anonymity); every other mode sends it once.
    repeats = 3 if comms == "echo_dm" else 1
    results = [p._tool_say({"to": t, "text": MESSAGE})
               for t in targets for _ in range(repeats)]
    error = next((r for r in results if r.startswith("error")), None)
    p.finish_note = f"scripted: sent fixed warning to {targets}"
    code = 1 if error else 0
    _write_stats(p, code, error)
    sys.exit(code)


if __name__ == "__main__":
    main()
