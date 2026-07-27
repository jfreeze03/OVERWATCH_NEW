# V051 action layer — design (Tranche C / r28, 2026-07-27)

Owner go: "tranche C" → after adversarial review, "ship the correct slice".

## What shipped (V051)

One transactional proc and one idempotency table — the wired, verified path:

- **`SP_ALERT_LIFECYCLE`** — alert ack/resolve as ONE set-based transaction
  (audit + update), replacing the app's split UPDATE-then-audit that could
  partially succeed. `EXECUTE AS OWNER`, `RETURNS VARCHAR` with the verdict
  vocabulary (`OK:` / `DUPLICATE:` / `BLOCKED:`) the app parses.
- **`OW_ACTION_INTENTS(IDEM_KEY PK, KIND, ACTOR, CREATED_AT, RESULT)`** —
  idempotency: a completed key returns `DUPLICATE` without re-running.
- App wiring: `execute_action(call, fallback)` in `app/core/query.py` calls
  the proc and reads its verdict; if the proc is not deployed yet, it runs the
  legacy statements exactly as v4.52. `idempotency_key()` in `identity.py` is
  sha1(kind, payload, viewer, minute-bucket) on the testable `account_now()`.
  Alert single + bulk lifecycle route through it.

### Corrections applied to the draft proc

Adversarial review of the first draft found real bugs; the shipped proc fixes:
- **Audit over-count** — the draft audited by POST-update status, so events
  already ACK/RESOLVED were re-audited. Fixed: audit BEFORE the update on the
  SAME pre-state filter, so audit rows match exactly the transitioned events.
- **No whitespace tolerance** — `SPLIT_TO_TABLE` values are now `TRIM`-ed.
- **Missing key guard** — an empty/NULL idempotency key returns `BLOCKED`
  instead of slipping past the EXISTS check into a PK violation.
- **Reserved keyword** — `AT` column renamed `CREATED_AT`.

### Idempotency, stated honestly

`IDEM_KEY` dedups **sequential** retries (a double-click returns `DUPLICATE`).
Snowflake `PRIMARY KEY` is informational — it does not lock — so two truly
concurrent calls sharing a key are not serialized. This is acceptable here: the
alert transitions are idempotent (re-ACK of an already-ACK event is a no-op via
the STATUS filter) and the writer population is ~2 DBAs. Full concurrency
control was judged over-engineering for that population.

## What was DEFERRED, and why

The first draft (auto-generated, never committed) also included
`SP_EXECUTE_REMEDIATION`, `SP_VERIFY_SAVINGS`, `SP_ACTION_UPDATE`, a typed
`SAVINGS_LEDGER` link, and a re-derived monthly verifier. Adversarial review
(3 independent verifiers) found, in **procs that no app path called**:

1. **Owner-privileged SQL injection** — `SP_EXECUTE_REMEDIATION` built a stored
   `PROOF_SQL` by concatenating the target name (quote-doubled but not
   backslash-safe); `SP_VERIFY_SAVINGS` later ran it under `EXECUTE AS OWNER`.
2. **Over-broad allow-list** — it permitted `ALTER USER` / `ALTER ACCOUNT` /
   `ALTER SESSION` (password / network-policy / default-role tampering) while
   *missing* the `ALTER TABLE` its own retention lever needs. Written
   speculatively, not matched to real call sites.
3. Correctness: 0-row updates reporting success (TOCTOU), `TRY_TO_DATE` silently
   NULLing a bad date, a proof-gate that stops writes but not owner-privileged
   reads/DoS, and the same PK-not-enforced race.

These are a genuine round: they need app wiring, an identifier-validated
(not string-concatenated) proof, an allow-list narrowed to
warehouse/pipe/task/cancel (+ a deliberate decision on the account-level
Emergency verbs), and per-proc row-affected checks. Queued.

## Standing (unchanged)

The v4.51 caption fixes already removed the false "the monthly verifier settles
this" promises, so nothing in the app currently over-claims settlement. The
typed savings-link that closes the P1-A *selection* gap rides with the deferred
remediation proc that would populate it.
