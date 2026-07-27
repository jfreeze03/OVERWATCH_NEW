# Action layer — full round design (V053 / r28 continued)

**Status: OUTCOME — both action procs DROPPED after build + review; V053 ships
the safe residue only (typed savings link + verifier selection). See the
"Build outcome" note below. The design (D1–D5) is retained as the record of
what was attempted and why the procs were not shippable.**

## Build outcome (2026-07-27)

The remediation proc was built to this design, hardened across two more
adversarial-review rounds, and still failed:

- Round 1 (V051 draft): owner-privileged SQL **injection** (concatenated
  proof). Fixed.
- Round 2: **dead happy path** (trailing `;` blocked every call), **FAILED
  reported as success**, **audit rolled back on bookkeeping failure**, and an
  over-broad allow-list. Fixed.
- Round 3: the allow-list is a **substring** check, so a trailing
  `-- SET DATA_RETENTION_TIME_IN_DAYS` comment carries an arbitrary
  `ALTER TABLE … DROP COLUMN` past it and runs under `EXECUTE AS OWNER`.

The lesson across three rounds: **validating free-text SQL server-side under
owner rights is not robustly securable by string checks.** The verify proc had
the parallel problem (an operator-supplied proof runs owner-privileged).

**Owner decision (2026-07-27): drop both procs.** V053 ships only the part that
was always safe and was the actual goal (Codex P1-A): the typed `SAVINGS_LEDGER`
link + a re-derived monthly verifier that finds app-booked rows. The app stamps
`FINDING_TYPE`/`TARGET_OBJECT` on its **existing** guarded booking inserts — no
stored procedure, no owner-privileged free-text execution. Remediation and
verify stay on today's already-safe path (typed confirmation + operator gating
+ clean builders).

If atomic/idempotent remediation is ever wanted, the robust design is **typed
parameters** — the proc takes `(kind, identifier-validated target, validated
value)` and *constructs* the statement internally, so no free-text SQL crosses
the boundary. That's a fresh round, not a patch to this one.

---

_Original design (retained as the record of the attempt):_

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
