# OVERWATCH_NEW — standing instructions (Claude Code auto-loads this file)

Streamlit-in-Snowflake cost/ops/security monitor for a shared ALFA+Trexis
account. Owner: Joe (jfreeze03). Everything lives in `DBA_MAINT_DB.OVERWATCH`
(schema SHARED with a previous app — never drop the schema/database).

Session state (HEAD, deploy status, pending work) is not kept in a doc: read
`git log` for HEAD, the top `CHANGELOG.md` entry and `APP_VERSION` in
`app/config.py` for the release, and the `V001..Vnnn applied` label in
`snowflake/validate.sql` for the migration tip. This file is the durable
stuff: laws, history, owner decisions.

## Gates (run before every commit)

```
python -m ruff check .
python -m mypy
python -m pytest -q          # needs Python 3.11+ (tests use datetime.UTC)
```
All three green or it doesn't ship. Migrations are run by JOE in Snowsight,
deliberately, off-peak — never by the agent. Agent Snowflake access, if
configured (snowsql `overwatch_ro`), is READ-ONLY: DESCRIBE/SHOW/SELECT to
validate assumptions before Joe deploys; never CREATE/ALTER/DROP/CALL/MERGE.

## House laws (each exists because something broke)

1. **Derivation law.** A migration that re-derives a proc/UDF re-emits the
   CURRENT definition byte-identically plus enumerated edits, via a
   forward-generation script in `outputs/gen_vNNN.py`;
   `tests/migrations/test_generators_regenerate.py` re-runs EVERY generator
   and byte-compares its migration (a new one is covered the day it lands),
   and most also have their own regen in `tests/migrations/test_vNNN_*.py`.
   Derive from the LATEST definition (V047 broke by deriving from V036
   instead of V037).
   Enforced by `tests/test_proc_lineage.py`: every re-derived proc, view or
   UDF's declared base must be its immediately previous definer (the V123
   class; views/UDFs tracked since wave 2a, one V088 waiver); from V151
   every re-definition must carry `-- >>> derived:<PROC>  (from Vnnn; <what
   changed>)` above its CREATE, or a `-- LINEAGE-WAIVER: <PROC> <reason>` line.
2. **V030 shape law.** `COMPANY_FOR_*` UDFs apply to plain columns only,
   never inside aggregation. Since V044, classification is evidence-based
   BOTH ways: Trexis by mapping/prefix/role, ALFA by WH_ALFA_* names /
   ALFA%+ADMIN dbs / %ALFA%-or-DBA roles, residual = 'UNKNOWN' (never NULL —
   V048 lesson: additivity). COMPANY_SCOPE rows are the explicit override.
3. **Live-scan budgets** (`tests/test_perf_budgets.py`) count "ACCOUNT_USAGE"
   literals per hot page file. Lowering is welcome; raising needs a
   justification comment in the dict. Keep SQL in `app/data/*_sql.py`;
   `unit_costs.py` budget is 0. The literal count is a lint proxy (v4.51):
   the builder-level gate in `tests/history_locks/test_v451_trust.py` renders each page's
   referenced builders and pins the true reachable ACCOUNT_USAGE table set —
   growing that set is the honest version of raising a budget.
4. **Every SQL builder gets a canary** (`app/data/canary.py`, default args,
   sqlglot-parses) and **every created object a teardown mention**
   (`tests/test_teardown_coverage.py`); a SHOW builder (EXPLAIN cannot compile
   one, e.g. `app/data/access_sql.py`) is exempted BY NAME with its reason in
   `tests/test_canary_coverage.py`. Teardown keeps ALL destructive lines
   commented; operator data survives; never DROP SCHEMA/DATABASE. The
   account-level delivery objects (OVERWATCH_* notification integrations,
   webhook secrets, the four NATIVE_ALERT_* email alerts) drop only inside its
   DELIVERY GATE (`drop_delivery_objects` DEFAULT FALSE, owner decision
   2026-10-02: the email default must never be overwritten again); the same
   test locks it.
5. **Migration guard + floor lockstep:** each V0XX opens with the not_ready
   guard and ends with the idempotent SCHEMA_VERSION insert; bump
   `snowflake/validate.sql` (both the `V001..V0XX applied` label and the
   `BETWEEN 1 AND N) = N` count) and admin's `_EXPECTED_MIGRATIONS` together;
   `tests/test_release_lockstep.py` and `tests/test_perf_budgets.py` derive
   both from the migrations directory. Per-migration tests lock their own
   content and run-doc line and **must not pin the tip or APP_VERSION** (the
   guard test fails if they do).
6. **Rebuild bundle is GENERATED** (`snowflake/rebuild/`) by
   `outputs/gen_rebuild_bundle.py`: 02 = a generated header (with the replay
   shim's `CREATE ROLE IF NOT EXISTS` lines for the retired roles V006-V008
   grant to, which 03 drops again; the only SQL allowed before the V001
   banner) + byte-concat of all migrations with `-- >>> name` banner
   sandwiches; 01/03/04/05 = generated comment-only banner + byte-copy of
   teardown/roles/backfill/validate; 00 = generated header + the hand-kept
   CLONE list; README = hand-kept runbook with a generated notes block (the
   generator also rewrites its 02 file name, migration count and heading).
   `tests/test_rebuild_replay.py` re-renders every file and byte-compares.
   Regenerate after ANY edit to a source; never hand-edit a generated part.
   The lock reads two hand-kept parts back from disk, so an edit there is
   compared with itself: 00's CLONE list and the README outside its
   generated notes block.
7. **Task-graph ordering (V041 incident):** in migrations, task RESUMEs +
   SYSTEM$TASK_DEPENDENTS_ENABLE go BEFORE first-fill CALLs AND again at the
   end — a halted worksheet must never strand the tree suspended. Procs swap
   under the running graph (CREATE OR REPLACE needs no suspends).
8. **Honesty rules:** no fabricated zeros (coverage gates / pct=None when the
   denominator is empty); empty panels say "checked, clean" not blank; source
   labels say which path served (mart vs live fallback); mart-first with
   live fallback via `run_mart_first` (empty mart → fallback, never lying
   zeros). No `") or {}"` after run_batch (r8 lock). Qualify columns
   (alias-shadow rule). Declared-exception guards only. Absence renders ONLY
   through `components.empty_state` (C25): `clean` green ok-row = verified
   clean, `needs_setup` blue info, `no_data_yet` quiet caption, `unavailable`
   red lead + detail expander; `guard()` routes both its branches through it
   (pass `kind="clean"` when the empty IS the good outcome), and a workflow
   empty can carry its next best action (`action_label`/`on_action`, F56).
   Never a raw `st.info`/`st.success` for an absence — this covers absence
   OUTCOMES of a read (zero rows / not configured / unavailable); action
   receipts, direct answers to a submitted question, and context notes
   about data that structurally cannot exist stay raw. **Presentation mode
   (C19):** `components.present_mode()` = `operator` (default, lean) or `audit`
   (full evidence chain), a per-viewer `PRESENT_MODE` pref hydrated like density.
   `result_caption` always shows the SOURCE but trims fetched-at + note in
   operator mode; gate methodology/how-computed/reconciliation blocks behind
   `audit_mode()` / `methodology_note()`. The pref-write MERGE
   (`prefs_sql.upsert_pref_sql`) is back for it.
9. **Identity:** owner's-rights SiS — `CURRENT_USER()` = app owner. Viewer
   identity via `app/core/identity.py` (`st.user`, CURRENT_USER() fallback)
   for prefs/usage/audit. Executor allow-list: one statement, DML/CALL on
   DBA_MAINT_DB.OVERWATCH objects or an Emergency lever (ALTER WAREHOUSE /
   PIPE / TASK / USER, ALTER ACCOUNT SET) only. Since v4.610.0 the executor
   itself requires admin entitlement for every lever AND every
   INSERT/UPDATE/DELETE/MERGE/CALL on OVERWATCH except the viewer's own
   self-service rows (USER_PREFS, USER_WATCHLIST, APP_USAGE,
   APP_QUERY_TELEMETRY, matched as the exact object token after the prefix).
   Admin = a direct USER member of SNOW_PRI_GFR_PRD_ALFA_DSA
   (`config.ADMIN_ACCESS_ROLE`) and nothing else; no username is hard-coded
   (owner 2026-10-07). Resolved by `session.viewer_access` (live
   `SHOW GRANTS OF ROLE`; once V175 is applied, `CALL SP_ADMIN_ROLE_MEMBERS()`,
   a read outside the executor); fail closed (a failed or empty lookup leaves
   NO in-app admin), re-verified at write time in `query._entitlement_refusal`;
   keep calling `_session.is_operator()` there, it is the tests' monkeypatch
   seam. Cache invalidation is domain-scoped.
10. **Formulas:** `app/logic/formulas.py` is the only place credits become
    dollars; `app/logic/metric_registry.py` is the semantic contract
    (BILLED/METERED/MEASURED/ALLOCATED/ESTIMATED + grain + lag). SQL builders
    return SQL strings only, day windows clamp via `bounded_days`, filters
    flow through `app/companies.py` clause builders exclusively.
11. **Write-friction policy (rec14):** friction matches CONSEQUENCE, not the
    table. ONE CLICK (an operator-gated `st.button`, SQL preview still shown)
    for a REVERSIBLE upsert to OVERWATCH's OWN tables — alert ACK, action /
    work-item create+save, ownership + watchlist edits, budget / SLO /
    experiment saves. TYPE-TO-CONFIRM (`confirm_gate`) for a CLASSIFYING or
    account-touching write — alert RESOLVE (feeds per-rule precision),
    incident declare/close, warehouse levers, alert rule threshold /
    Enabled edits (type the RULE_ID; a compare-and-set UPDATE plus an
    ALERT_AUDIT RULE_EDIT row, v4.610.0). `st.form` stays declined (it
    hides the preview). `notify()` is the receipt: toast on success,
    persistent inline error on failure (rec48). EVERY write click block also
    pairs the C48 latch: `write_gate_open(<key>)` as the click gate's LAST
    condition + `stamp_write(<key>, ok)` after the block's last write, BEFORE
    any `st.rerun` (which raises). The latch arms on gate-open (a duplicate
    click preempts the script after the write commits), is run-seq aware and
    success-only; scope the key by action/target when a fixed key could
    swallow a genuinely different action (fragments freeze the run seq —
    emergency surfaces use scoped keys + short backstops). The query-layer
    spinners in `execute_*` are the in-flight state; don't add per-site ones.
12. **Schema gate (wave 4, v4.602):** gate every read of a column a migration
    adds, and every caption that claims a migration's behaviour, with
    `app.ui.schema_gate.has_migration(n, page)` — never a private
    SCHEMA_VERSION read. It answers from the startup gate's own read (no
    query; first-paint budgets stay put) and is False on an unreadable
    table, so a deploy before the apply keeps the pre-apply behaviour. A new
    `app/ui` module that imports `run` (or another read entry point) at
    module level must be registered in `tests/usage_sim.py`,
    `tests/test_pages_shaped.py` and `tests/test_pages_apptest.py`;
    `tests/test_schema_gate.py` enforces the first two in full (and that the
    apptest harness stubs schema_gate and attention).
13. **Snowflake-only failures (V173, 2026-10-02).** The executed sqlite harnesses
    evaluate any correlated subquery row by row and return NULL for x / 0, so a
    green harness proved neither V168's OR-correlated guard (Snowflake: "Unsupported
    subquery type cannot be evaluated") nor V163's filter-guarded division
    ("Division by zero"). Two static locks cover every current definer:
    `tests/test_snowflake_supported_subqueries.py` (each correlated subquery needs a
    top-level `inner column = outer expression` key; any other link to the outer
    row only beside one, as a production-proven `_PROVEN` shape) and
    `tests/test_sql_division_guards.py` (each divisor guards itself: NULLIF, a
    non-zero constant, a positive GREATEST floor or an IFF / CASE test of the same
    value; a WHERE / HAVING never counts).

## Owner decisions (do not relitigate)

- **Access = four roles, decided by role only** (2026-10-05; amended
  2026-10-07: "...their roles should show who gets access to what"; supersedes
  2026-07-13's "Access = SNOW_ACCOUNTADMINS + SNOW_SYSADMINS, period"). The owner asked for
  SNOW_PRI_GFR_PRD_ALFA_DSA to have "admin and full rights" and for
  SNOW_PRI_GFR_PRD_ALFA_DTI to have "view access to all things that don't
  require admin", then approved the v4.610.0 spec (DTI = two pages). Direct
  USER grantees of DSA are OVERWATCH admins with FULL PARITY: the DBA page set
  (Admin and Ask), every in-app write and the account-level levers ALTER USER /
  ALTER ACCOUNT SET (every admin has them); a grant to a role is not expanded.
  There is no username allowlist, viewer pin or break-glass admin; do not
  re-add one (v4.611.0). Every other viewer who can open the app (DTI members,
  SNOW_* holders who are not direct DSA members, an unidentified SiS viewer)
  gets MONITOR = Cost Intelligence + Operations, read-only; DTI is never
  looked up. Membership is `SHOW GRANTS OF ROLE` run as the owner (once
  V175 is applied, `CALL SP_ADMIN_ROLE_MEMBERS()`: the same SHOW run as the
  procedure's owner, so it survives the SNOW_SYSADMINS owner switch), once
  per session (re-checked after 300 s; a failure, 'lookup_failed', or an
  empty USER set, 'unverified', is retried first after 60 s, the wait
  doubling with each further failure up to the 5-minute TTL) and FAILS
  CLOSED; every admin is re-verified at every privileged write (memo at
  most 15 s). If the DSA lookup is down (privilege gap, missing or unusable
  V175 procedure), nobody can change anything in-app until it recovers;
  urgent changes go through a Snowsight worksheet as SNOW_ACCOUNTADMINS. A
  SNOW_SYSADMINS cutover without V175 is worse: its SHOW can list SOME of
  the members with status ok, silently demoting the rest (no error row). Trust delegation
  accepted: whoever can GRANT the DSA role can mint an OVERWATCH admin with
  account-level levers. SNOW_ACCOUNTADMINS + SNOW_SYSADMINS keep roles.sql's
  object grants; DSA/DTI get no worksheet grants (USAGE on the database,
  schema and Streamlit only). The monitor/operator role layer stays retired;
  audit tables keep append-only REVOKEs (accident-proofing). **roles.sql
  (owner request 2026-10-06, v4.610.1)** grants DSA and DTI those six USAGE
  grants and nothing else, and its -20011/-20012 proof block accepts exactly
  the four roles, any grantee kind (`config.ROLES_SQL_APP_GRANTEES` =
  APP_ACCESS_ROLES, pinned to roles.sql by `tests/test_admin_access_tab.py`).
  Every `snow streamlit deploy --replace` re-creates the app object and drops
  its USAGE grants (database/schema USAGE survive): re-run roles.sql's
  Streamlit block after each deploy.
- **Task monitoring STAYS** (2026-07-13 correction: "i meant getting rid of
  resource monitor, not task monitoring"). V045 restored it end-to-end.
- **Resource monitors are GONE** (same correction). OVERWATCH_RM was
  suspending the app warehouse mid-use. No monitor levers/deductions in the
  app; auto-suspend tracking stays. No hard cap on WH_ALFA_ADMIN — COST
  alert rules are the guardrails.
- **UNKNOWN classification is law** (V044/V048): unmapped entities surface on
  Cost Intelligence → Spend & Attribution ("Unmapped entities" worklist,
  behind the "Load storage & unmapped-entity detail" toggle) instead of
  silently billing ALFA; KEBARR1 override → ALFA stands.
- **Cortex user attribution stays live-first, byte-exact v4.34.2 shape**
  (exact emails + timestamps; owner rejected the mart swap that lost them).
- **No monthly-budget KPI** on Overview; MTD-vs-prior-month pace instead.
- Deterministic prescriptive alert rules, dedupe-key pattern. Since V162 the hourly
  SP_ALERT_SCAN has 14 counting arms ([01]-[05], [10], [14], [17], [18], [20],
  [21] SEC_POSTURE_METRIC, [22] OPS_PIPELINE_DEGRADED, [26] SEC_LOGIN_TAKEOVER and [27] SEC_ADMIN_GRANT --
  both ungated, V162; the dead [15] break-glass arm is gone and [11] COST_CLOUD_SVC_RATIO is retired) + the
  [23] PIPE_ETL_CYCLE add-on; since V163 SP_ALERT_SCAN_DAILY has 14 ([06]-[09], [12], [13], [13b], [16],
  [19], [22], [24] COST_IDLE_OPPORTUNITY, [25] COST_SLEEP_POLLING -- a counting CALL arm;
  SP_SCAN_SLEEP_POLLING gates itself weekly, V160; [28] COST_AI_USER_RUNAWAY and [29]
  SEC_TRUST_REGRESSION, V163) + the [17]/[18] add-ons. Arm numbers are unique across BOTH scans from
  [26] on (the next free is [30]; [17], [18] and [22] already collide). Since V174 the [26]/[27] admin list is
  eight roles (`security_sql.ALERT_ADMIN_ROLES`: V162's seven + SNOW_PRI_GFR_PRD_ALFA_DSA, last) and [18] watches
  ACCOUNTADMIN + `security_sql.ADMIN_HOLDER_ROLES` (SNOW_ACCOUNTADMINS, SNOW_SYSADMINS, SNOW_PRI_GFR_PRD_ALFA_DSA);
  `tests/test_security_alert_parity.py` binds both lists to the latest SP_ALERT_SCAN. SP_INCIDENT_AUTODECLARE never
  declares for SEC_LOGIN_TAKEOVER / SEC_ADMIN_GRANT (a hard-coded crit-CTE exclusion, V162 owner
  decision), and its [attach] links them only to an incident that already holds the same user (V162
  review fix). Add-ons, sweeps and the [hb] heartbeats never increment `fails`
  (test_scan_denominators_match_counting_arms). Cadence gates (V157 compile diet): the hourly scan
  reads the Central hour ONCE (`ct_hour`); [10]/[20] + their condition-ended clears run only when
  MOD(ct_hour, 4) = 1, the hourly [22] only when MOD(ct_hour, 3) = 2; a gate wraps an UNCHANGED arm
  and a gated-off arm counts as ok. `app/logic/quotas.runaway_days` is the app twin of daily arm [28]:
  re-derive either side only with `tests/test_ai_runaway_parity.py` green (or change both together).
- Current definers (re-derive forward from THESE): SP_ALERT_SCAN = V174 (V174 changed only the admin-role
  lists of hourly [18], [26] and [27], appending SNOW_PRI_GFR_PRD_ALFA_DSA), SP_ALERT_SCAN_DAILY = V173
  (arm numbers unchanged; the next free is still [30]; V173 changed only hourly [18]'s dedupe guard and daily
  [24]'s two divisions), SP_INCIDENT_AUTODECLARE = V162, SP_NOTIFY_WEBHOOK =
  V164 (its escalation SETTINGS expressions are pinned to `mart_sql.ESCALATE_*` by
  `tests/test_escalation_delivery.py`, and `last_delivery_health` still keys on the
  `route <id> integration <name>` CONTEXT), SP_DAILY_DIGEST = V171 (its grounding literals still live in
  `app/logic/digest_grounding.py`, locked by `tests/test_digest_grounding_parity.py`), SP_CANARY_SENTINEL and
  SP_SCAN_REF_GAPS = V171, SP_INCIDENT_DECLARE = V170 with TWO overloads (the 4-arg kept until the gated app
  is deployed, the 5-arg with P_ACTOR: re-derive BOTH, or drop the 4-arg first) and INCIDENT_PROPOSALS = V170,
  SP_LOAD_MARTS_V27, SP_NIGHTLY_RECONCILE and SP_LOAD_PATTERN_COST = V167, SP_LOAD_SECURITY_FACTS,
  SP_LOAD_DAILY_FACTS, SP_LOAD_APP_COST and SP_LOAD_STORAGE_TRUTH = V166, SP_CHANGE_IMPACT_SCAN,
  SP_WAREHOUSE_CHANGE_SCAN, SP_SCAN_SCHEMA_DRIFT, SP_SCAN_CLOUD_SVC_ANOMALY and SP_ANOMALY_SWEEP = V172. The
  Hr/Min/Sec CASE template in V171's OPS_SLOW_RENDER title and V172's VERDICT_DETAIL is the SQL twin of
  `formulas.humanize_duration`; the digest (V171) and the sweep (V172) read CORTEX_MODEL with the same
  literal as `app.core.ai.normalize_model`. A proc-calling task's TASK_HISTORY RETURN_VALUE is NULL:
  verify a scheduled proc by its heartbeat / ledger rows, never by the return string or a hand CALL.
- Validate/loader worksheets are pasted by Joe; the app monitors the loader
  through APP_ERROR_LOG + SOURCE_FRESHNESS_STATE (loader-owned freshness).
- **Option C (2026-09-24, shipped v4.597):** Decision Studio → Proof (Proof ·
  Pipeline, EXECUTIVE-visible, read-only); Portfolio → Operations ▸ Optimize
  fix queue (Track = idempotent ACTION_QUEUE insert keyed on
  QUERY_FINGERPRINT); SLO editor, Experiments UI retired; Products hidden;
  Cost Truth → Spend ratio. Old links remap via navigate.LEGACY_TARGETS.

## History in one paragraph each

- **v4.36 (V041)** loader-efficiency pass (staged QH extract, watermarks,
  exec board v2, xdim alloc, posture riders) — shipped with two defects:
  resumes after first-fills (stranded the task tree when the worksheet
  halted) and a mart swap that dropped cortex emails/timestamps. Incident
  review: `docs/reviews/V041_INCIDENT_REVIEW_20260712.md`.
- **v4.37** full-rebuild bundle (`snowflake/rebuild/`, backup clones
  `*_BAK_20260712`, dropped by Joe 2026-09-28) + hardened chain; rebuild
  executed clean.
- **v4.38 (V042)** Codex r22 ship-half: FACT_QUERY_DAILY, atomic extract with
  gated watermark, purge coverage, AI usage stamps. r23: telemetry-picked
  perf (batching, predicate-first).
- **v4.39-4.40** triage filter chips + 20-rec review; r24: Snowsight profile
  links on every QUERY_ID table, pace KPI, systemic post-action refresh.
- **v4.41 (r25)** security metrics Joe picked: new-network logins (90d
  baseline) + Egress section (DATA_TRANSFER_HISTORY + UNLOAD watch).
- **v4.42 (r26)** roles collapsed to the two SNOW_* roles; task monitoring
  removed on a misread ask ("task monitor" meant resource monitor).
- **v4.43 (V043/r27)** Codex r27 adjudication ship-list: loader task
  retirement (later reversed), r25 alert teeth, viewer identity, executor
  allow-list, domain cache salts, set-based bulk ack, admin access
  self-check/grouped errors/settings hygiene, docs rewrite + drift locks.
  Adjudication: `docs/reviews/CODEX_R27_ADJUDICATION_20260713.md`.
- **v4.44 (V044)** UNKNOWN classification (#18).
- **v4.45 (V045)** the correction: task monitoring restored (app from git
  history + loader re-derivation, 120d refill, 19 scan arms), OVERWATCH_RM
  dropped.
- **2026-07-14 (Code session)** cost audit F1-F4, Codex cost items 1-8,
  metric registry, FACT_OBJECT_COST_DAILY (V046-V048), ETL cost tags,
  Phase 4 account-time/DST/reconciliation locks. See
  `docs/design/COSTDB_VS_OVERWATCH_2026-07-14.md` and
  `docs/reviews/CODEX_REVIEW_ASSESSMENT_2026-07-14.md`.

## Working style

Joe wants concise and direct. Verify claims in code before adjudicating
external review items (ship/route/decline with evidence — see the
adjudication docs for the format). Small honest scopes; every round ends
with all gates green, a CHANGELOG entry, an APP_VERSION bump, and locks that
pin what shipped. When an external reviewer (Codex/CoCo) is right, say so;
when the code disproves a claim, show the line. Owner corrections get
recorded in locks/comments so the story survives (grep "owner" in tests/).

## Standing open items

- r28+ queue: reconciliation v2 by dimension is the one item still open
  (SP_NIGHTLY_RECONCILE is V064's shape plus V167's R2-018 mark-and-sweep of the four wide-edge tables). Shipped since: the action
  layer (V051 OW_ACTION_INTENTS + SP_ALERT_LIFECYCLE; V074/V092
  SP_ACTION_LIFECYCLE with REQUEST_KEY idempotency; the remediation/verify
  procs were deliberately dropped in V053), evidence-grade savings
  verification for the mart-measurable finding types (V053
  PROOF_QUERY_ID/PROOF_RESULT, stamped by the measured verify in
  `app/ui/pages/cost_parts/optimize.py` from `app/logic/ledger_measure.py`;
  other rows still hand-verify), the V074 Action Queue foundation
  and V049 write-target attribution.
- Backups: V161 (owner decision 2026-09-28) retired TASK_BACKUP_OPERATOR (V158's
  OVERWATCH_BAK generations and V089's weekly `*_BAK_LAST` copies), and Joe
  dropped the July-12 `*_BAK_20260712` clones the same day. Recovery is Time
  Travel plus manual TRANSIENT clones taken before a risky change: teardown.sql
  B0, or `snowflake/rebuild/00_backup_operator_data.sql` after changing its
  hard-coded `_20260712` suffix to today's date.
