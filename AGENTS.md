# AGENTS.md — instructions for AI coding agents (Codex, etc.)

This is a Streamlit-in-Snowflake cost/ops/security monitor for a shared
ALFA+Trexis account. Owner: Joe (jfreeze03).

**Read `CLAUDE.md` in the repo root and `docs/handoff/*.md` first, and follow
them — they are the source of truth.** Trust `git log` over any snapshot
numbers in docs. This file is the condensed version of the rules that break
things most often; `CLAUDE.md` has the full house laws.

**Baseline: the last shipped release** (see `APP_VERSION` in `app/config.py` and the
top of `CHANGELOG.md`). Work is reviewed as a diff against `origin/main`. Keep scopes
small and self-contained; don't restructure or "clean up" beyond the task you were given.

## Non-negotiable

1. **Gates must be green before you claim done or commit** — all three:
   ```
   python -m ruff check .
   python -m mypy
   python -m pytest -q          # needs Python 3.11+
   ```
   Green or it doesn't ship.
2. **Never run or author Snowflake migrations against the account.** Migrations
   are applied by the owner in Snowsight, off-peak — never by an agent. Agent
   Snowflake access is **READ-ONLY** (SELECT/SHOW/DESCRIBE only; never
   CREATE/ALTER/DROP/CALL/MERGE). New DB work is a **new forward migration file** —
   the next unused `VNNN` after the highest one in `snowflake/migrations/`; never edit
   an already-applied migration (every committed `VNNN` is live). Never drop the shared
   schema/database.
3. **Never commit secrets** (tokens, webhook URLs, emails) into tracked files —
   a test fails on it. Leave placeholders for the owner to paste in Snowsight.

## Structure — put code where it already lives

- **SQL builders** → `app/data/*_sql.py`. Return SQL strings only, no I/O. Day
  windows clamp via `bounded_days`; filters flow through `app/companies.py`
  clause builders.
- **Credits → dollars** → **only** `app/logic/formulas.py`. Never inline a rate.
  AI/Cortex credits price at the AI rate via `blended_billed_usd`, not the flat
  compute rate. Metric semantics → `app/logic/metric_registry.py`.
- **Charts** → `app/ui/charts.py`; **shared UI** → `app/ui/components.py`.
- **Absence renders through `components.empty_state(kind, ...)`** (C25): `clean`
  green ok-row = verified clean, `needs_setup` blue info, `no_data_yet` quiet
  caption, `unavailable` red lead + detail expander. `guard()` already routes
  both branches through it — pass `kind="clean"` when the empty IS the good
  outcome. Never a raw `st.info`/`st.success` for an absence; a workflow empty
  can carry its next best action (`action_label`/`on_action`, F56). The rule
  covers absence OUTCOMES of a read; action receipts, direct answers to a
  submitted question, and notes about data that structurally cannot exist
  stay raw `st.info`/`st.success`.
- **Every operator-write click block pairs the C48 latch**:
  `write_gate_open(<key>)` as the click gate's LAST condition +
  `stamp_write(<key>, ok)` after the block's last write, BEFORE any `st.rerun`.
  Scope the key by action/target when a fixed key could swallow a genuinely
  different action (fragments freeze the run seq). The in-flight spinner lives
  in the `execute_*` query layer — don't add per-site spinners.
- **Cross-page jumps** → `app/core/state.request_navigation(page, section,
  filters)`. Do **not** wire a filter-bearing nav to a page a profile can't see
  (`PAGES_BY_PROFILE` in `app/config.py`) — it clamps the page but still applies
  the filter, leaking scope. Gate the affordance to profiles that have the
  target page.

## Repo-specific traps

- **Shape-pinning tests grep source for exact code shape** (e.g. counting
  `on_click="ignore"`, asserting a builder is called). If you intentionally
  change that shape, **update the assertion to the new shape — strengthen it,
  never delete or weaken it.** A failing shape test usually means "update the
  contract," not "the code is wrong."
- **Honesty rules** (CLAUDE.md law 8): empty panels say "checked, clean" — never
  blank, never a fabricated 0; `pct=None` when the denominator is empty; source
  labels say which path served (mart vs live fallback); mart-first with live
  fallback via `run_mart_first`. No `") or {}"` after `run_batch`.
- **SiS's conda channel lags PyPI:** any newer-Streamlit widget feature needs a
  `hasattr(st, "...")` or `try/except` degrade whose fallback still delivers the
  core value (see `clickable_bar_usd`, `section_toc`, `lazy_sections`).
- **mypy is config-scoped to the pure layers only** (`app/logic`, `app/data`,
  `app/config`) — keep those clean and side-effect-free. `app/main.py` and
  `app/ui/*` are **not** mypy-checked, so don't rely on mypy for UI-layer types.

## Process per shippable change

- Bump `APP_VERSION` in `app/config.py` and add a `CHANGELOG.md` entry
  (date ≤ today).
- End every round with all gates green.
- When an external review claims a bug, verify it against the actual line before
  acting — ship or decline with evidence, not on assertion.
