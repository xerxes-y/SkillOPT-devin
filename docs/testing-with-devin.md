# Testing Memento recall inside the Devin IDE

This guide walks you through verifying that Memento's **automatic recall** works
in a real Devin workspace — i.e. that Devin is handed the relevant team memories
*before it acts*, without the model having to choose to look.

There are two layers to check:

| Layer | Mechanism | What it proves |
|-------|-----------|----------------|
| **Layer 1** | `.devin/rules/memento.md` instructs Devin to call `memory_brief` first | the model *can* recall on demand via the MCP tool |
| **Layer 2** | `.devin/hooks.v1.json` runs `devin-memento-hook` on every event | recall is injected **automatically** — the model can't skip it |

If you only have time for one check, do **Layer 2** (§3) — that's the v0.9.0
feature and the one that can't be faked by a cooperative model.

---

## 0. Prerequisites (one-time)

```bash
# from this repo
pip install -e .          # puts the `devin-memento-hook` console script on PATH
which devin-memento-hook  # must print a path — the hook command Devin will call
```

> If `which` prints nothing, the hooks will silently no-op. The console script
> is defined in `pyproject.toml` (`devin-memento-hook = "mcp_server:run_hook_cli"`).
> It must resolve on the same PATH Devin uses.

Then run the installer to seed the MCP server, rules, and hooks into your
detected Devin workspaces:

```bash
bash install.sh
```

This copies into each `<workspace>/.devin/`:
- `rules/memento.md` — Layer 1 recall-before-act rules
- `hooks.v1.json` — Layer 2 automatic-injection hooks
- `skills/memento-learned/SKILL.md` — the managed skill
…and registers the `memento` MCP server with the Devin CLI.

---

## 1. Confirm the wiring is in place

In the workspace you want to test:

```bash
ls .devin/rules/memento.md .devin/hooks.v1.json
cat .devin/hooks.v1.json        # should list devin-memento-hook on UserPromptSubmit + SessionStart
```

Then, **inside the Devin IDE**, open the command palette / chat and run:

```
/hooks
```

You should see `devin-memento-hook` registered for `UserPromptSubmit` and
`SessionStart`. If it isn't listed, Devin didn't pick up `hooks.v1.json` —
reload the window and re-check.

Also confirm the MCP server is connected (so `memory_save` / `memory_brief`
exist): run `/mcp` and look for **memento**.

---

## 2. Seed a memory worth recalling

Give Devin something specific it could only know from memory. In the Devin chat:

```
Remember this for the team: our checkout-total test is flaky because it's
time-dependent — freeze the clock with freezegun. Save it to memory.
```

Devin should call `memory_save`. Verify it landed:

```bash
python3 mcp_server.py --web      # opens the local dashboard; the memory should appear
# or, headless:
echo '{"hook_event_name":"UserPromptSubmit","prompt":"flaky checkout test"}' \
  | devin-memento-hook | python3 -m json.tool
```

The second command is the exact thing Devin runs — if it returns JSON with your
freezegun note inside `additionalContext`, the store has it.

---

## 3. Verify Layer 2 — automatic recall (the main test)

Start a **fresh Devin session** in the workspace and, *as your very first
message*, give it a task that relates to the seeded memory **without mentioning
the solution**:

```
Fix the flaky checkout total test.
```

✅ **Pass:** Devin already knows to freeze the clock with freezegun, and says so
*before* you tell it — because `devin-memento-hook` injected the memory on
`UserPromptSubmit`. You never asked it to "check memory."

❌ **Fail:** Devin starts investigating from scratch with no awareness of the
freezegun convention.

### Cross-check what was actually injected

To see the literal context Devin received for that prompt:

```bash
echo '{"hook_event_name":"UserPromptSubmit","prompt":"Fix the flaky checkout total test."}' \
  | devin-memento-hook | python3 -m json.tool
```

The `additionalContext` string is byte-for-byte what Devin saw. If your memory
is in there but Devin ignored it, that's a model/prompt issue, not a recall
issue — the injection worked.

### SessionStart (standing lessons)

Open a brand-new session and send any trivial first message. If you've saved
any **lessons** (pinned `source='lesson'` memories), Devin should be aware of
them from message one. Confirm the payload directly:

```bash
echo '{"hook_event_name":"SessionStart"}' | devin-memento-hook | python3 -m json.tool
```

---

## 4. Verify the fail-safe (recall must never block Devin)

A memory bug must never wedge the agent. The hook is designed to emit nothing
and exit 0 on any error:

```bash
echo 'not json'           | devin-memento-hook; echo "exit=$?"   # exit=0, no output
echo '{"hook_event_name":"PreToolUse"}' | devin-memento-hook     # ignored, no output
```

Both should print nothing (beyond the `exit=0`). If either errors or hangs,
that's a regression.

---

## 5. Team mode (optional)

To test shared recall across teammates, point the hook at the shared Postgres
store and a namespace, in the **same environment Devin runs in**:

```bash
export MEMENTO_DB_URL="postgres://…"     # shared store
export MEMENTO_NAMESPACE="team-payments" # this team's scope
```

Now memories saved by one teammate are injected for another on their next
prompt. Verify with the same `echo … | devin-memento-hook` probe above — it
will read from Postgres and filter to the namespace.

---

## Quick automated smoke test

For a fast, self-contained check that doesn't touch your real store or need
Devin running at all:

```bash
bash tests/try_hook.sh        # seeds an isolated temp DB, fires every event type
cat tests/hook_test.log       # the captured output
```

This is the regression harness for the hook itself; §1–§4 above are the
end-to-end check that Devin is actually wired to it.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| `/hooks` doesn't list `devin-memento-hook` | `hooks.v1.json` not seeded / not reloaded | re-run `install.sh`, reload the Devin window |
| Hook listed but injects nothing | `devin-memento-hook` not on PATH → silent no-op | `pip install -e .`; check `which devin-memento-hook` |
| Injection works in CLI probe but Devin ignores it | model/prompt behaviour, not recall | tighten `.devin/rules/memento.md`, or accept it as a model choice |
| Empty `additionalContext` for a known memory | wrong namespace / empty store | unset `MEMENTO_NAMESPACE` or confirm the memory's namespace matches |
| `memory_save` / `memory_brief` missing | MCP server not registered | `devin mcp add memento -- python3 mcp_server.py`, check `/mcp` |
