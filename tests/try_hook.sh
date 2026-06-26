#!/usr/bin/env bash
# try_hook.sh — manual test harness for Layer 2 automatic recall (v0.9.0).
#
# It proves the `devin-memento-hook` entrypoint works end to end: seed a few
# memories into an ISOLATED throwaway DB, then feed real Devin hook events into
# the hook on stdin and capture the `additionalContext` it injects.
#
#   bash tests/try_hook.sh
#
# Nothing here touches your real ~/.memento/memory.db — it runs against a temp
# DB that is deleted on exit. All output is also written to tests/hook_test.log
# so you can scroll back / paste it to me.

set -euo pipefail
cd "$(dirname "$0")/.."

LOG="tests/hook_test.log"
WORK="$(mktemp -d)"
export MEMENTO_MEMORY_DB="$WORK/memory.db"      # isolated store
trap 'rm -rf "$WORK"' EXIT

# pick an interpreter
PY="${PYTHON:-python3}"; command -v "$PY" >/dev/null || PY=python

# how we invoke the hook: prefer the installed console script, else the module
if command -v devin-memento-hook >/dev/null 2>&1; then
  HOOK=(devin-memento-hook)
else
  HOOK=("$PY" mcp_server.py --hook)
fi

say() { printf '%s\n' "$*"; }

{
  say "=================================================================="
  say " memento Layer 2 hook test — $(date)"
  say " isolated DB: $MEMENTO_MEMORY_DB"
  say " hook cmd   : ${HOOK[*]}"
  say "=================================================================="

  # ── 1. seed the throwaway store ────────────────────────────────────────────
  say ""
  say "## 1. Seeding memories + a standing lesson into the temp DB"
  "$PY" - <<'PYEOF'
import memento_memory
s = memento_memory.open_store()
s.save("Postgres connection pooling",
       "Use PgBouncer in transaction mode; app pool size 5 per worker.",
       tier="semantic", tags="db,postgres")
s.save("Flaky checkout test",
       "test_checkout_total is time-dependent; freeze the clock with freezegun.",
       tier="episodic", tags="tests,checkout")
s.add_lesson("Never log PII",
             "Do not log raw email/card numbers; redact before logging.")
print("   seeded:", s.stats()["total"], "memories")
PYEOF

  # ── 2. UserPromptSubmit → should return matching memory + lessons ──────────
  say ""
  say "## 2. UserPromptSubmit event (prompt mentions the checkout test)"
  say "   expect: 'Flaky checkout test' memory + the 'Never log PII' lesson"
  say "   --- hook stdout: ---"
  printf '{"hook_event_name":"UserPromptSubmit","prompt":"fix the flaky checkout total test"}' \
    | "${HOOK[@]}" | "$PY" -m json.tool || say "   (no output)"

  # ── 3. SessionStart → lessons only ─────────────────────────────────────────
  say ""
  say "## 3. SessionStart event (no prompt)"
  say "   expect: standing lessons only, no per-prompt hits"
  say "   --- hook stdout: ---"
  printf '{"hook_event_name":"SessionStart"}' \
    | "${HOOK[@]}" | "$PY" -m json.tool || say "   (no output)"

  # ── 4. fail-safe: garbage stdin must emit nothing and exit 0 ───────────────
  say ""
  say "## 4. Fail-safe: malformed JSON on stdin"
  say "   expect: empty output, exit code 0 (a memory problem never blocks Devin)"
  set +e
  OUT=$(printf 'not json at all' | "${HOOK[@]}"); RC=$?
  set -e
  say "   stdout : '${OUT}'"
  say "   exit   : ${RC}  $( [ "$RC" -eq 0 ] && echo OK || echo 'FAIL — should be 0')"

  # ── 5. unrelated event type → ignored ──────────────────────────────────────
  say ""
  say "## 5. Unrelated event (PreToolUse) is ignored"
  say "   expect: empty output"
  OUT=$(printf '{"hook_event_name":"PreToolUse"}' | "${HOOK[@]}")
  say "   stdout : '${OUT}'  $( [ -z "$OUT" ] && echo OK || echo 'FAIL — should be empty')"

  say ""
  say "## done. If sections 2 & 3 show JSON with 'additionalContext' containing"
  say "   the seeded memories/lessons, automatic recall works."
} 2>&1 | tee "$LOG"
