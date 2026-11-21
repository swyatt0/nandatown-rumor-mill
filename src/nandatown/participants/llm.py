"""Tier two: a model-driven participant on the Track.

The split is deliberate. The HARNESS is infrastructure and always
trustworthy: it owns the town client, the durable journal, the current
claim's fence, and the small tool surface. The BRAIN only emits tool
calls: the deterministic MockBrain by default (no inference, CI-safe),
or any OpenAI-compatible endpoint (Ollama included) via TOWN_MODEL.

The first agent-native fault lives here: context_truncation drops the
middle of the conversation past a message budget, exactly the failure a
real agent meets when its context compacts mid-task. The system prompt
survives; everything else must be recoverable through the protocol
(idempotent resend, redeliverable claims, the journal). The agent
reports its truncation count in its ack notes, which makes the fault's
survival an attributed assertion in the evidence.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from typing import Any

import httpx

from ..client import StaleFenceError, TownClient
from ..skills import skill_source
from .base import Journal

MESSAGE_BUDGET = 6
KEEP_TAIL = 4
MAX_TURNS = 80
IDLE_LIMIT = 20

TOOLS = [
    {"type": "function", "function": {
        "name": "list_participants",
        "description": "List run participants with roles and capabilities.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "claim_work",
        "description": "Wait briefly for a hint, then claim one piece of"
                       " inbox work under a lease.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "send_work",
        "description": "Send work to a participant. Idempotent by"
                       " message_id: retrying identical content is safe.",
        "parameters": {"type": "object", "properties": {
            "message_id": {"type": "string"},
            "to": {"type": "string"},
            "kind": {"type": "string"},
            "body": {"type": "object"}},
            "required": ["message_id", "to", "kind", "body"]}}},
    {"type": "function", "function": {
        "name": "ack_work",
        "description": "Acknowledge the currently claimed work.",
        "parameters": {"type": "object", "properties": {
            "status": {"type": "string",
                       "enum": ["received", "processed", "rejected",
                                "retryable", "failed"]},
            "note": {"type": "object"}},
            "required": ["status"]}}},
    {"type": "function", "function": {
        "name": "finish",
        "description": "End this participant's run.",
        "parameters": {"type": "object", "properties": {
            "exit_code": {"type": "integer"},
            "note": {"type": "string"}},
            "required": ["exit_code"]}}},
]

# Market profiles add two conversation tools on top of the five above.
# TOWN_COMMS=dm: `to` names one participant. TOWN_COMMS=room: `to` may
# also be "everyone". Nothing else differs between the modes.
COMMS_TOOLS = [
    {"type": "function", "function": {
        "name": "say",
        "description": "Send a text message. `to` is one participant"
                       " name{everyone}.",
        "parameters": {"type": "object", "properties": {
            "to": {"type": "string"},
            "text": {"type": "string"}},
            "required": ["to", "text"]}}},
    {"type": "function", "function": {
        "name": "read_messages",
        "description": "Return messages sent to you that you have not"
                       " seen yet (waits a few seconds if none).",
        "parameters": {"type": "object", "properties": {}}}},
]


# Modes that allow say(to="everyone"). anon_room looks exactly like room
# to agents; echo_dm looks exactly like dm (only the scripted slanderer
# differs). Agents are never told which control mode they are in.
ROOM_MODES = ("room", "anon_room")
MODERATOR_PATTERN = re.compile(r"scam|never delivered|took my money|avoid",
                               re.I)
MODERATOR_LABEL = "[Disputed: no delivery records support this claim] "


def comms_tools(mode: str) -> list[dict[str, Any]]:
    everyone = (', or "everyone" to send to all other participants'
                if mode in ROOM_MODES else "")
    tools = json.loads(json.dumps(COMMS_TOOLS))
    tools[0]["function"]["description"] = (
        tools[0]["function"]["description"].format(everyone=everyone))
    return tools


class RateLimited(Exception):
    pass


class MockBrain:
    """A deterministic brain that speaks only through tool calls.

    It exists so the tier-two harness (tool loop, journal, fences,
    truncation) is exercised without inference. It rereads whatever
    survives in the conversation, so context truncation forces it to
    rediscover and safely resend, which is the point.
    """

    def __init__(self, role: str):
        self.role = role
        self._call_seq = 0

    def _call(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        self._call_seq += 1
        return {"content": None, "tool_calls": [{
            "id": f"call-{self._call_seq}",
            "function": {"name": name, "arguments": json.dumps(args)}}]}

    @staticmethod
    def _history(messages: list[dict[str, Any]]):
        pending = {}
        history = []
        for m in messages:
            if m["role"] == "assistant" and m.get("tool_calls"):
                for tc in m["tool_calls"]:
                    pending[tc["id"]] = (tc["function"]["name"],
                                         tc["function"]["arguments"])
            elif m["role"] == "tool":
                name, args = pending.get(m.get("tool_call_id"),
                                         ("?", "{}"))
                history.append((name, args, m.get("content") or ""))
        return history

    @staticmethod
    def _task(messages: list[dict[str, Any]]) -> dict[str, Any]:
        system = messages[0]["content"]
        match = re.search(r"TASK=(\{.*?\})\n", system, re.S)
        return json.loads(match.group(1)) if match else {}

    def chat(self, messages: list[dict[str, Any]],
             tools: list[dict[str, Any]]) -> dict[str, Any]:
        history = self._history(messages)
        task = self._task(messages)
        if history and history[-1][2].startswith("error:"):
            name, args, _ = history[-1]
            return self._call(name, json.loads(args or "{}"))
        idle = sum(1 for name, _, result in history
                   if name == "claim_work" and "no work" in result)
        if self.role == "seller":
            return self._seller(history, idle)
        return self._buyer(history, task, idle)

    def _seller(self, history, idle):
        last_claim = None
        after: list = []
        for name, args, result in history:
            if name == "claim_work" and result.startswith("{"):
                last_claim, after = json.loads(result), []
            elif last_claim is not None:
                after.append((name, result))
        if last_claim is not None:
            acked = any(n == "ack_work" and "recorded" in r
                        for n, r in after)
            if not acked:
                if last_claim.get("already_processed"):
                    return self._call("ack_work",
                                      {"status": "processed",
                                       "note": {"duplicate": True}})
                sent = any(n == "send_work" and "accepted" in r
                           for n, r in after)
                body = last_claim["body"]
                total = body["quantity"] * body["unit_price_cents"]
                if not sent:
                    rid = "r-" + last_claim["message_id"].removeprefix("q-")
                    return self._call("send_work", {
                        "message_id": rid, "to": last_claim["from"],
                        "kind": "quote_response",
                        "body": {"request_id": last_claim["message_id"],
                                 "total_cents": total}})
                return self._call("ack_work",
                                  {"status": "processed",
                                   "note": {"applied": True,
                                            "total_cents": total}})
        if idle >= IDLE_LIMIT - 5:
            return self._call("finish", {"exit_code": 0,
                                         "note": "inbox stayed quiet"})
        return self._call("claim_work", {})

    def _buyer(self, history, task, idle):
        for name, args, result in reversed(history):
            if name == "claim_work" and result.startswith("{"):
                claim = json.loads(result)
                if claim.get("kind") != "quote_response":
                    return self._call("ack_work",
                                      {"status": "rejected",
                                       "note": {"reason": "unknown kind"}})
                total = claim["body"]["total_cents"]
                correct = total == task.get("expected_total_cents")
                acked = False
                idx = history.index((name, args, result))
                for n, _, r in history[idx + 1:]:
                    if n == "ack_work" and "recorded" in r:
                        acked = True
                if not acked:
                    return self._call("ack_work", {
                        "status": "processed",
                        "note": {"correct": correct, "total_cents": total,
                                 "expected_total_cents":
                                     task.get("expected_total_cents")}})
                return self._call("finish",
                                  {"exit_code": 0 if correct else 4,
                                   "note": "validated"})
        seller = None
        for name, _, result in history:
            if name == "list_participants":
                for p in json.loads(result):
                    if "quote.read" in p.get("capabilities", []):
                        seller = p["name"]
        if seller is None:
            return self._call("list_participants", {})
        sent = any(name == "send_work" and "accepted" in result
                   for name, _, result in history)
        if not sent:
            return self._call("send_work", {
                "message_id": "q-1", "to": seller, "kind": "quote_request",
                "body": {"sku": task["sku"], "quantity": task["quantity"],
                         "unit_price_cents": task["unit_price_cents"]}})
        if idle >= IDLE_LIMIT:
            return self._call("finish", {"exit_code": 5,
                                         "note": "no response arrived"})
        return self._call("claim_work", {})


class AnthropicChat:
    """Native Claude Messages API behind the same chat() shape.

    The participant keeps its history in OpenAI form; this converts on
    every call. Claude's assistant turns are replayed verbatim from the
    raw content blocks (thinking blocks included), so the history stays
    append-only as the Messages API requires.
    """

    MAX_TOKENS = 16000

    def __init__(self, model: str, api_key: str | None):
        import anthropic

        self._anthropic = anthropic
        # Retries are ours (counted, bounded), not the SDK's.
        self.client = anthropic.Anthropic(api_key=api_key, max_retries=0)
        self.model = model

    @staticmethod
    def _tools(tools):
        return [{"name": t["function"]["name"],
                 "description": t["function"]["description"],
                 "input_schema": t["function"]["parameters"]}
                for t in tools]

    @staticmethod
    def _messages(messages):
        system = ""
        out: list[dict[str, Any]] = []
        for m in messages:
            if m["role"] == "system":
                system = m["content"]
            elif m["role"] == "assistant":
                out.append({"role": "assistant", "content": m["_raw"]})
            elif m["role"] == "tool":
                block = {"type": "tool_result",
                         "tool_use_id": m["tool_call_id"],
                         "content": m["content"]}
                if (out and out[-1]["role"] == "user"
                        and isinstance(out[-1]["content"], list)):
                    out[-1]["content"].append(block)
                else:
                    out.append({"role": "user", "content": [block]})
            else:
                out.append({"role": "user", "content": m["content"]})
        return system, out

    def create(self, messages, tools):
        """Returns (reply, usage) or raises RateLimitError."""
        system, msgs = self._messages(messages)
        resp = self.client.messages.create(
            model=self.model, max_tokens=self.MAX_TOKENS, system=system,
            messages=msgs, tools=self._tools(tools),
            output_config={"effort": "medium"})
        text = "".join(b.text for b in resp.content if b.type == "text")
        calls = [{"id": b.id, "type": "function",
                  "function": {"name": b.name,
                               "arguments": json.dumps(b.input)}}
                 for b in resp.content if b.type == "tool_use"]
        return ({"content": text or None, "tool_calls": calls,
                 "_raw": resp.content},
                (resp.usage.input_tokens, resp.usage.output_tokens))


class ModelClient:
    """OpenAI-compatible chat completions, native Claude for claude-*
    models, or the mock brain."""

    def __init__(self, model: str, role: str,
                 base_url: str | None = None, api_key: str | None = None,
                 http: httpx.Client | None = None):
        self.model = model
        self.mock = MockBrain(role) if model.startswith("mock:") else None
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.rate_limits = 0
        self.claude = (AnthropicChat(model, api_key
                                     or os.environ.get("TOWN_MODEL_KEY"))
                       if model.startswith("claude-") else None)
        if self.mock is None and self.claude is None:
            self.base_url = (base_url
                             or os.environ.get("TOWN_MODEL_URL")
                             or "http://localhost:11434/v1")
            headers = {}
            key = api_key or os.environ.get("TOWN_MODEL_KEY")
            if key:
                headers["Authorization"] = f"Bearer {key}"
            self.http = http or httpx.Client(base_url=self.base_url,
                                             headers=headers, timeout=120.0)

    def chat(self, messages, tools) -> dict[str, Any]:
        if self.mock is not None:
            return self.mock.chat(messages, tools)
        if self.claude is not None:
            return self._chat_claude(messages, tools)
        wire = [{k: v for k, v in m.items() if k != "_raw"}
                for m in messages]
        # 429: wait and retry with backoff; give up after a few in a row.
        for attempt in range(6):
            r = self.http.post("/chat/completions",
                               json={"model": self.model,
                                     "messages": wire,
                                     "tools": tools, "tool_choice": "auto"})
            if r.status_code != 429:
                break
            self.rate_limits += 1
            time.sleep(min(60.0, 5.0 * 2 ** attempt))
        else:
            raise RateLimited("model endpoint kept returning 429")
        r.raise_for_status()
        data = r.json()
        usage = data.get("usage") or {}
        self.prompt_tokens += int(usage.get("prompt_tokens") or 0)
        self.completion_tokens += int(usage.get("completion_tokens") or 0)
        message = data["choices"][0]["message"]
        return {"content": message.get("content"),
                "tool_calls": message.get("tool_calls") or []}

    def _chat_claude(self, messages, tools) -> dict[str, Any]:
        rate_limit = self.claude._anthropic.RateLimitError
        for attempt in range(6):
            try:
                reply, (inp, out) = self.claude.create(messages, tools)
            except rate_limit as exc:
                self.rate_limits += 1
                wait = exc.response.headers.get("retry-after")
                try:
                    delay = float(wait) if wait else 5.0 * 2 ** attempt
                except ValueError:
                    delay = 5.0 * 2 ** attempt
                time.sleep(min(60.0, delay))
                continue
            self.prompt_tokens += inp
            self.completion_tokens += out
            return reply
        raise RateLimited("model endpoint kept returning 429")


class LLMParticipant:
    def __init__(self, client: TownClient, name: str, role: str,
                 state_dir: str, model: str, fault: str = "none",
                 skill: str | None = None, comms: str | None = None):
        self.client = client
        self.state_dir = state_dir
        # A market participant reads its own role skill and talks with
        # say/read_messages; the quote profiles leave both unset.
        self.skill = skill
        self.comms = comms
        self.tools = TOOLS + (comms_tools(comms) if comms else [])
        self._say_seq = 0
        self._seen: set[str] = set()
        self.finish_note: str | None = None
        self.name = name
        self.role = role
        self.model = ModelClient(model, role)
        self.journal = Journal(os.path.join(state_dir, "journal.db"))
        self.fault = fault
        self.truncations = 0
        self.tool_errors = 0
        self._tool_error_fired = False
        self._stashed_claim: dict[str, Any] | None = None
        self.current_claim: dict[str, Any] | None = None
        self.exit_code: int | None = None
        self.messages: list[dict[str, Any]] = []

    def _system_prompt(self, token_note: str) -> str:
        if self.skill:
            others = ", ".join(sorted(
                n for n in self.client.run_context.get("roles", {})
                if n != self.name))
            how = ('say(to="everyone", ...) reaches all participants;'
                   ' say(to=<name>, ...) reaches one.'
                   if self.comms in ROOM_MODES else
                   "say(to=<name>, ...) reaches exactly one participant.")
            return (
                f"You are {self.name} in a NANDA Town widget market."
                f" Other participants: {others}. Talk with say and"
                f" read_messages. {how} Use only your tools; everything"
                f" you say goes into the run's evidence. When your part is"
                f" done, call finish.\n\n" + skill_source(self.skill))
        role_skill = ("quote.read" if self.role == "seller"
                      else "quote.request")
        task = self.client.run_context.get("task", {})
        return (
            f"You are {self.name}, the {self.role} in a NANDA Town run."
            f" Use only your tools; every fact you assert goes into the"
            f" run's evidence.\nTASK={json.dumps(task)}\n{token_note}\n\n"
            + skill_source("town-protocol")
            + "\n\n" + skill_source(role_skill))

    # -- tool implementations ------------------------------------------

    def _tool_list_participants(self, args: dict[str, Any]) -> str:
        return json.dumps(self.client.participants())

    def _tool_claim_work(self, args: dict[str, Any]) -> str:
        if self._stashed_claim is not None:
            claim = self._stashed_claim
            self._stashed_claim = None
            self.current_claim = claim
            return json.dumps(claim)
        self.client.notify(wait=0.4)
        claim = self.client.claim()
        if claim is None:
            return "no work right now"
        claim["already_processed"] = self.journal.seen(claim["message_id"])
        if (self.fault == "tool_error" and not self._tool_error_fired):
            # The agent-native fault: the tool result is lost mid-call.
            # The claim is real and its lease is running; the agent has
            # to notice the error and retry the tool to recover it.
            self._tool_error_fired = True
            self.tool_errors += 1
            self._stashed_claim = claim
            return ("error: tool result lost mid-call; retry the same"
                    " tool")
        self.current_claim = claim
        return json.dumps(claim)

    def _tool_send_work(self, args: dict[str, Any]) -> str:
        out = self.client.send(args["message_id"], args["to"],
                               args["kind"], args["body"])
        return json.dumps({"accepted": True, **out})

    def _tool_ack_work(self, args: dict[str, Any]) -> str:
        if self.current_claim is None:
            return "error: no claimed work to acknowledge"
        note = dict(args.get("note") or {})
        note["context_truncations"] = self.truncations
        note["tool_errors"] = self.tool_errors
        try:
            self.client.ack(self.current_claim["message_id"],
                            self.current_claim["fence"],
                            args["status"], note)
        except StaleFenceError:
            self.current_claim = None
            return "error: stale fence; the work will be redelivered"
        if args["status"] == "processed":
            self.journal.record(self.current_claim["message_id"],
                                {"note": note})
        self.current_claim = None
        return "recorded"

    def _tool_finish(self, args: dict[str, Any]) -> str:
        self.exit_code = int(args["exit_code"])
        self.finish_note = args.get("note")
        return "finished"

    def _names(self) -> set[str]:
        return {p["name"] for p in self.client.participants()}

    def _tool_say(self, args: dict[str, Any]) -> str:
        if not self.comms:
            return "error: unknown tool say"
        to = str(args.get("to", "")).strip()
        text = str(args.get("text", ""))
        names = self._names() - {self.name}
        # leak mode addresses exactly like dm mode.
        if to == "everyone" and self.comms in ROOM_MODES:
            recipients = sorted(names)
        elif to in names:
            recipients = [to]
        else:
            allowed = sorted(names) + (["everyone"]
                                       if self.comms in ROOM_MODES else [])
            return f"error: `to` must be one of {allowed}"
        self._say_seq += 1
        say_id = f"say-{self.name}-{self._say_seq}"
        extra: dict[str, Any] = {}
        # TOWN_MODERATOR=rule: a fixed keyword rule labels the message for
        # every reader (and in its leak copies); the sender is not told.
        if (os.environ.get("TOWN_MODERATOR", "off") == "rule"
                and MODERATOR_PATTERN.search(text)):
            text = MODERATOR_LABEL + text
            extra["moderated"] = True
        if to == "everyone" and self.comms == "anon_room":
            # anon_room: broadcasts arrive with the sender removed, one
            # copy each (anonymity without repetition).
            anon = "Anonymous: " + re.sub(re.escape(self.name),
                                          "[redacted]", text)
            for r in recipients:
                self.client.send(f"{say_id}-{r}", r, "anon",
                                 {"say_id": say_id, "to": to, "text": anon,
                                  **extra})
            return json.dumps({"sent": True, "to": to,
                               "recipients": recipients})
        for r in recipients:
            self.client.send(f"{say_id}-{r}", r, "say",
                             {"say_id": say_id, "to": to, "text": text,
                              **extra})
            if self.comms == "leak":
                self._leak(say_id, r, text, names)
        return json.dumps({"sent": True, "to": to,
                           "recipients": recipients})

    def _leak(self, say_id: str, original_to: str, text: str,
              names: set[str]) -> None:
        """TOWN_COMMS=leak: once a DM is accepted into its recipient's
        inbox, an anonymous copy goes to everyone except the sender and
        the original recipient. The copy is its own message kind, so the
        evidence keeps who leaked what to whom; the reader is shown no
        sender, and the sender's name is redacted from the text. The
        agent is not told: its say result lists only the real recipient.
        """
        tip = "Anonymous tip: " + re.sub(re.escape(self.name),
                                         "[redacted]", text)
        for r in sorted(names - {original_to}):
            self.client.send(
                f"{say_id}-{original_to}-leak-{r}", r, "leak",
                {"leak_of": f"{say_id}-{original_to}", "say_id": say_id,
                 "original_to": original_to, "to": r, "text": tip})

    def _tool_read_messages(self, args: dict[str, Any]) -> str:
        if not self.comms:
            return "error: unknown tool read_messages"
        out: list[dict[str, Any]] = []
        for attempt in range(2):
            while True:
                claim = self.client.claim()
                if claim is None:
                    break
                try:
                    self.client.ack(claim["message_id"], claim["fence"],
                                    "processed", {"read": True})
                except StaleFenceError:
                    pass
                if claim["message_id"] in self._seen:
                    continue
                self._seen.add(claim["message_id"])
                body = claim.get("body") or {}
                leaked = claim.get("kind") in ("leak", "anon")
                out.append({"from": ("anonymous" if leaked
                                     else claim.get("from")),
                            "to": body.get("to", self.name),
                            "text": body.get("text", json.dumps(body))})
            if out or attempt:
                break
            self.client.notify(wait=4.0)
        return json.dumps(out) if out else "no new messages"

    def _execute(self, name: str, args: dict[str, Any]) -> str:
        handler = getattr(self, f"_tool_{name}", None)
        if handler is None:
            return f"error: unknown tool {name}"
        try:
            return handler(args)
        except Exception as exc:
            return f"error: {type(exc).__name__}: {exc}"

    # -- the loop -------------------------------------------------------

    def _maybe_truncate(self) -> None:
        if self.fault != "context_truncation":
            return
        if len(self.messages) <= MESSAGE_BUDGET:
            return
        tail = self.messages[-KEEP_TAIL:]
        while tail and tail[0]["role"] == "tool":
            tail = tail[1:]
        self.messages = [self.messages[0]] + tail
        self.truncations += 1

    def run(self, deadline_seconds: float = 60.0) -> int:
        token_note = ("Your context may be truncated mid-run; anything"
                      " not in this system prompt can vanish. Recover"
                      " through the protocol: rediscover, resend the"
                      " same message identity, reclaim."
                      if self.fault == "context_truncation" else "")
        self.messages = [
            {"role": "system", "content": self._system_prompt(token_note)},
            {"role": "user", "content": "Begin. Work until your part of"
                                        " the task is done, then finish."},
        ]
        deadline = time.time() + deadline_seconds
        for _ in range(MAX_TURNS):
            if time.time() > deadline:
                return 6
            self._maybe_truncate()
            reply = self.model.chat(self.messages, self.tools)
            calls = reply.get("tool_calls") or []
            assistant = {"role": "assistant",
                         "content": reply.get("content"),
                         "tool_calls": calls}
            if "_raw" in reply:
                assistant["_raw"] = reply["_raw"]
            self.messages.append(assistant)
            if not calls:
                self.messages.append(
                    {"role": "user",
                     "content": "Use a tool, or finish."})
                continue
            for tc in calls:
                try:
                    args = json.loads(tc["function"]["arguments"] or "{}")
                except json.JSONDecodeError:
                    args = {}
                result = self._execute(tc["function"]["name"], args)
                self.messages.append({"role": "tool",
                                      "tool_call_id": tc["id"],
                                      "content": result})
                if self.exit_code is not None:
                    return self.exit_code
            self._maybe_truncate()
        return 8


def _write_stats(participant: LLMParticipant, code: int,
                 error: str | None) -> None:
    """Token usage and the finish note, for the market runner to collect.
    Never includes the model key."""
    m = participant.model
    stats = {"name": participant.name, "role": participant.role,
             "exit_code": code, "finish_note": participant.finish_note,
             "prompt_tokens": getattr(m, "prompt_tokens", 0),
             "completion_tokens": getattr(m, "completion_tokens", 0),
             "rate_limits": getattr(m, "rate_limits", 0),
             "error": error}
    stats["total_tokens"] = (stats["prompt_tokens"]
                             + stats["completion_tokens"])
    with open(os.path.join(participant.state_dir, "stats.json"), "w") as f:
        json.dump(stats, f)


def main() -> None:
    env = os.environ
    client = TownClient(env["TOWN_URL"], env["RUN_ID"])
    client.join_auto(env["NAME"], env["TOKEN"], env.get("TOWN_GRANT"))
    participant = LLMParticipant(
        client, env["NAME"], env["ROLE"], env["STATE_DIR"],
        model=env.get("TOWN_MODEL", "mock:v1"),
        fault=env.get("FAULT", "none"),
        skill=env.get("SKILL") or None,
        comms=env.get("TOWN_COMMS") or None,
    )
    if participant.skill:
        import signal

        def _stopped(signum, frame):
            raise SystemExit(-signum)

        # The runner stops agents still running when the buyers are done;
        # leave through the finally below so their token usage is kept.
        signal.signal(signal.SIGTERM, _stopped)
    if not participant.skill:
        sys.exit(participant.run(
            deadline_seconds=float(env.get("DEADLINE", "60"))))
    code, error = 9, None
    try:
        code = participant.run(
            deadline_seconds=float(env.get("DEADLINE", "60")))
    except SystemExit as exc:
        code, error = int(exc.code or 0), "stopped by runner"
    except Exception as exc:  # recorded key-free: type and status only
        error = type(exc).__name__
        status = getattr(getattr(exc, "response", None), "status_code",
                         None)
        if status is not None:
            error += f" {status}"
    finally:
        _write_stats(participant, code, error)
    sys.exit(code)


if __name__ == "__main__":
    main()
