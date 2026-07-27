# Action layer — full round design (V053 / r28 continued)

**Status: decisions signed off 2026-07-27 (D1–D5 below). Ready to implement,
starting with V053a. Design-doc-first; each phase is its own Snowsight run.**

Continues Tranche C. V051 shipped the wired, verified slice — `SP_ALERT_LIFECYCLE`
+ `OW_ACTION_INTENTS` + the `execute_action()` seam — and your Snowsight smoke
test proved the Scripting mechanics (transactional UPDATE+INSERT, `SQLROWCOUNT`,
`SPLIT_TO_TABLE`, the OK/DUPLICATE/BLOCKED verdicts) work live. This round adds
the three procs that were deferred after adversarial review found an
owner-privileged SQL injection and an over-broad allow-list in the
auto-generated draft. Every defect that review found is addressed explicitly
below.

## Goals

1. **Remediation is atomic and audited** — one proc executes the lever, writes
   the audit row on both outcomes, and books the ledger, with idempotency.
2. **Savings verification is evidence-grade** — a read-only proof runs, its
   `QUERY_ID` + result snapshot are stored, and only then does ESTIMATED →
   VERIFIED. Closes Codex P1-A (the caption side already shipped in v4.51; this
   closes the *mechanism*).
3. **Action queue gains a lifecycle** — the missing UPDATE path (claim / assign
   / due / status), so an action can be worked, not just listed.

## What ships

### Migration V053 (hand-authored where new; derivation-law where re-derived)

- `OW_ACTION_INTENTS` already exists (V051) — reused, no change.
- `SAVINGS_LEDGER` gains the typed link + proof evidence (additive `ALTER`s):
  `FINDING_TYPE`, `TARGET_OBJECT`, `PROOF_QUERY_ID`, `PROOF_RESULT`,
  `PROOF_RUN_AT`. (`REMEDIATION_ID` already exists from V032.)
- **`SP_EXECUTE_REMEDIATION`** — idempotency guard → **identifier-validate the
  target** → **narrow allow-list re-check** → EXECUTE IMMEDIATE the single
  statement (its failure caught to a variable) → REMEDIATION_LOG row on both
  outcomes (`EXECUTED_BY` stamped) → typed ESTIMATED ledger row on success →
  intent row on success. **No stored PROOF_SQL built by concatenation** (the
  injection source — see D2).
- **`SP_VERIFY_SAVINGS`** — idempotency guard → read PROOF_SQL for an ESTIMATED
  item → **reject non-SELECT/WITH and any `;`** → run it → capture `QUERY_ID` +
  bounded snapshot → transactional UPDATE to VERIFIED **with a `SQLROWCOUNT`
  check** (0 rows ⇒ ROLLBACK + `BLOCKED`, no burned key) → intent row.
- **`SP_ACTION_UPDATE`** — idempotency guard → validate status enum + **reject a
  malformed due-date** (not silently NULL it) → transactional UPDATE with a
  `SQLROWCOUNT` check (unknown id ⇒ `BLOCKED`) → intent row.
- Re-derive `SP_VERIFY_IDLE_SAVINGS` from V007 under the derivation law:
  `WHERE FINDING_TYPE = 'AUTO_SUSPEND' OR <legacy LIKE>` so it finally selects
  app-booked rows (needs the app to stamp `FINDING_TYPE` — below).

### App wiring (proc-first with legacy fallback, the V051 pattern)

- Optimization's four booking paths (resize, retention, guarded remediation, and
  the idle copy-paste snippet) route their execute through `execute_action` →
  `SP_EXECUTE_REMEDIATION`, stamping `FINDING_TYPE`/`TARGET_OBJECT`.
- The verify expander routes through `SP_VERIFY_SAVINGS`.
- Overview's action list gains inline **status / owner / due** controls →
  `SP_ACTION_UPDATE`.

## How each review finding is closed

| Review finding (on the draft) | Fix in this design |
|---|---|
| Owner-privileged SQL **injection** (concatenated PROOF_SQL) | **D2** — no concatenated proof; identifier-validate the target; parameterize |
| **Over-broad allow-list** (ALTER USER/ACCOUNT/SESSION) | **D1** — narrow to warehouse/pipe/task/table + cancel |
| 0-row UPDATE reports success (TOCTOU) | `SQLROWCOUNT` check after every UPDATE ⇒ ROLLBACK + BLOCKED |
| `TRY_TO_DATE` silently NULLs a bad date | reject a non-empty unparseable date with BLOCKED |
| NULL/empty idempotency key slips the guard | `BLOCKED: missing idempotency key` up front (as V051) |
| `SPLIT_TO_TABLE` no trim | `TRIM(VALUE)` (as V051) |
| Proof gate stops writes but not owner reads/DoS | statement timeout applies; proof is operator-supplied and read-only; **D3** |
| PK-not-enforced race | sequential-dedup only, stated honestly (as V051) — **D5** |

## Decisions (settled 2026-07-27)

**D1 — Emergency lever allow-list → NARROW.** The proc allow-list is
warehouse / pipe / task / table / cancel only. The account-level Emergency
levers (`ALTER USER … SET DISABLED`, `ALTER ACCOUNT SET …`) **stay on their
existing guarded raw path** — typed-confirmed, "run as SNOW_ACCOUNTADMINS"
warned, rare break-glass. The owner-privileged proc surface stays minimal.

**D2 — auto-booked proof → NULL PROOF, operator supplies.** Remediation books
the ESTIMATED ledger row with `FINDING_TYPE`/`TARGET_OBJECT` and **NULL
PROOF_SQL**; the operator supplies/edits the proof on the ledger before
verifying. The injection surface is gone by construction. The target is
identifier-validated (`^[A-Za-z0-9_$.]+$`) wherever it reaches SQL.

**D3 — proof-run evidence → snapshot with QUERY_ID fallback.**
`SP_VERIFY_SAVINGS` stores `PROOF_QUERY_ID` + a 20-row JSON snapshot via
`RESULT_SCAN(LAST_QUERY_ID())`. This is the one Scripting construct V051 did not
exercise, so the V053a deploy runbook includes a proof-run smoke test (the
proven V051 pattern). If `RESULT_SCAN`-in-proc misbehaves live, fall back to
storing `QUERY_ID` only (the re-runnable id is the evidence).

**D4 — scope → TWO PHASES.**
  - **V053a** — remediation + verify: `SP_EXECUTE_REMEDIATION`,
    `SP_VERIFY_SAVINGS`, the typed `SAVINGS_LEDGER` columns, the re-derived
    `SP_VERIFY_IDLE_SAVINGS`, and wiring for the four optimize booking paths +
    the verify expander. Closes the savings loop end-to-end.
  - **V053b** — action-queue lifecycle: `SP_ACTION_UPDATE` + Overview's inline
    status/owner/due controls. Its own migration and Snowsight run.

**D5 — idempotency → sequential-dedup**, consistent with V051 (honest for the
~2-DBA writer population; transitions are idempotent). No concurrency-control
machinery.

## Build order

V053a first, as its own migration + app change + Snowsight verify (smoke test
per D3), then V053b. Each phase: derivation-law generator where a proc is
re-derived (`SP_VERIFY_IDLE_SAVINGS`), hand-authored where new; full lockstep;
a `test_v053*` lock; deploy runbook. No implementation begins until this doc is
committed.

## Out of scope (stays queued, r29/r30)

The full Action-Queue-as-operating-center product (entity-360 links,
alert→action creation everywhere, assignment notifications). This round adds the
*mechanism* (the UPDATE path); the *product* is a later round.
