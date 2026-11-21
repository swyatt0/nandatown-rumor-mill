# Modifications

This repository is a modified copy of [NANDA Town](https://github.com/projnanda/nandatown)
(`projnanda/nandatown`), licensed under the Apache License 2.0 (see `LICENSE`).
All original files, copyright and history are retained. The changes below were
made on the `market-llm-experiment` branch, starting from upstream commit
`6f9a7c0`, for the market-llm rumor experiment ("Public Isn't Protection: A
Rumor Test for AI Agent Markets"). The experiment was inspired by the white
paper "Who Watches the Swarm?" (Danenberg, Krishnan & Raskar, 2026). This
work is not affiliated with or endorsed by the NANDA Town authors.

## Modified files

- `src/nandatown/participants/llm.py`: `say` / `read_messages` tools and the
  `TOWN_COMMS` modes (dm, room, leak, anon_room, echo_dm), the `TOWN_MODERATOR`
  rule label, token accounting, bounded 429 retries, and a native Anthropic
  SDK path for `claude-*` models.
- `src/nandatown/profiles.py`: the `market-llm` profiles (sellers, buyers and a
  scripted rumor-spreader, optionally disguised as a buyer).
- `src/nandatown/cli.py`: `nandatown run market-llm` dispatches to the market
  runner.
- `pyproject.toml`: an optional `anthropic` dependency.

## Added files

- `src/nandatown/market.py`: the multi-participant market runner
  (`TOWN_COMMS`, `TOWN_DISGUISE`, `TOWN_BUYERS`, `TOWN_RUMOR`, `TOWN_MODERATOR`).
- `src/nandatown/participants/slanderer.py`, `slanderer_varied.py`,
  `slanderer_replying.py`: scripted (no-model) rumor-spreaders.
- `src/nandatown/skills/builtin/market.*.md`: role skill files.
- `scripts/compare_llm.py`: analysis (choices, timelines, overpayment,
  permutation tests).
- `tests/test_market_comms.py`, `tests/test_slanderer_varied.py`.
- `docs/market-llm-RESULTS.md`, `docs/market-llm-RESULTS-disguised.md`:
  results; the disguised document is the current one.

The full change history is in this branch's git log.
