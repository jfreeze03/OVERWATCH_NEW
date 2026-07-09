# V029 — the incident object (design)

Status: DESIGN, 2026-07-09 — build after owner sign-off (same doc-first flow
as V027). Consolidates Codex r6 #4 (incident object) and #6 (recommendation
lineage), the V027-era note on IS_RERUN scan hygiene, and the 2026-07-09
IaC decisions (Flyway for migrations, Terraform for account topology —
both probable, neither landed; every IaC hook below degrades to today's
behavior when the tools are absent).

## Why

Operators work INCIDENTS; the app stores fragments of them. An alert storm
of fifteen SEC/OPS events, one warehouse change, two task failures, and one
remediation is ONE story — today it is nineteen rows in five tables with no
shared key. Consequences we can measure: alert-grain MTTA/MTTR overstates
incident count and understates real recovery time; time-to-detect does not
exist (nothing records when the underlying thing STARTED); reopen/recurrence
is invisible; and remediation lineage lives in notes text (r6 #6) so
"which fix closed which alert" is a string search, not a join.

## The object model

Two small, permanent, operator-curated tables (teardown: PRESERVED — this
is operational history, not a rebuildable mart):

```
INCIDENTS (
    INCIDENT_ID    VARCHAR(80) DEFAULT UUID_STRING() PRIMARY KEY,
    TITLE          VARCHAR(300) NOT NULL,
    SEVERITY       VARCHAR(10) NOT NULL,            -- CRITICAL|HIGH|MEDIUM|LOW
    STATUS         VARCHAR(12) NOT NULL DEFAULT 'OPEN',  -- OPEN|MITIGATED|RESOLVED|CLOSED
    COMPANY        VARCHAR(40) NOT NULL DEFAULT 'ALL',
    DETECTED_AT    TIMESTAMP_NTZ NOT NULL,          -- first alert raised / manual declare time
    STARTED_AT     TIMESTAMP_NTZ,                   -- earliest member evidence (drives TTD)
    ACK_AT         TIMESTAMP_NTZ,
    MITIGATED_AT   TIMESTAMP_NTZ,
    RESOLVED_AT    TIMESTAMP_NTZ,
    ROOT_CAUSE_KIND VARCHAR(30),                    -- DEPLOY|CONFIG_CHANGE|DATA|CAPACITY|EXTERNAL|UNKNOWN
    ROOT_CAUSE_NOTE VARCHAR(2000),
    OWNER          VARCHAR(200),                    -- free text until V030 OBJECT_OWNERS
    REOPENED_FROM  VARCHAR(80),                     -- prior INCIDENT_ID; drives reopen rate
    DECLARED_BY    VARCHAR(200) DEFAULT CURRENT_USER(),
    UPDATED_AT     TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP()
)

INCIDENT_MEMBERS (
    INCIDENT_ID  VARCHAR(80) NOT NULL,
    MEMBER_KIND  VARCHAR(20) NOT NULL,   -- ALERT|TASK_FAIL|WH_CHANGE|DDL|DEPLOY|REMEDIATION
    REF_ID       VARCHAR(300) NOT NULL,  -- EVENT_ID / task name+day / CHANGE_ID / query id / flyway version / REMEDIATION_ID
    EVIDENCE_TS  TIMESTAMP_NTZ,
    AUTO_LINKED  BOOLEAN DEFAULT FALSE,
    LINKED_BY    VARCHAR(200) DEFAULT CURRENT_USER(),
    LINKED_AT    TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP()
)
```

Lifecycle is the alert pattern verbatim: generate-then-run SQL, operator
(DBA profile) gated, every transition audited, statuses only move forward
(reopen = NEW incident with REOPENED_FROM set — history is never rewritten).

## How incidents are born (no silent magic)

1. **Declared** — operator selects rows on the Control Room timeline / day
   replay and clicks "declare incident"; members attach with AUTO_LINKED
   FALSE. The ±30-minute drill that exists today becomes the pre-filled
   member list.
2. **Auto-declared for CRITICALs** — a CRITICAL alert with no open incident
   sharing its dedupe family opens one (SETTINGS toggle
   `INCIDENT_AUTO_DECLARE_CRITICAL`, default TRUE). One incident per dedupe
   family per 24h — storms compress instead of multiplying.
3. **Proposed, never assumed** — a view (`INCIDENT_PROPOSALS`) suggests
   groupings: open alerts + task failures + registry changes + deploys
   within ±30 minutes sharing a warehouse/database/dedupe family. The UI
   shows proposals; a human confirms. Nothing is grouped silently.

## Lineage (r6 #6) — additive, never retroactive

- `REMEDIATION_LOG` gains `EVENT_ID` and `INCIDENT_ID` (nullable ALTERs);
  the fix flows (alerts closed-loop, optimize resize, admin emergency)
  stamp them at execute time.
- `SAVINGS_LEDGER` gains `REMEDIATION_ID`, completing
  alert -> fix -> verified-$ as joins instead of notes text.
- Historical rows stay NULL — we do not invent lineage that was never
  recorded.

## Metrics (computed live — these tables are tiny)

| Metric | Definition |
|---|---|
| Incidents / month | count by SEVERITY and ROOT_CAUSE_KIND |
| Time-to-detect | STARTED_AT -> DETECTED_AT (only when evidence predates detection) |
| MTTA | DETECTED_AT -> ACK_AT |
| MTTR | DETECTED_AT -> RESOLVED_AT (alert-grain MTTA/MTTR panels stay — different question) |
| Reopen rate | incidents with REOPENED_FROM set within 14d of the prior close |
| Compression | ALERT members per incident — the fatigue panel's denominator done right |
| Change-correlated % | incidents with a DEPLOY or WH_CHANGE member — the IaC payoff number |

## IaC integration assumptions (decided 2026-07-09, tools not yet landed)

**Flyway** (probable, migrations transport):
- `flyway_schema_history` rows become DEPLOY members and a DEPLOY arm on
  MART_INCIDENT_TIMELINE (loader arm guarded per-mart like every V027 arm —
  absent table logs one `mart_load_failed` row, other arms unaffected).
- Admin -> Migrations reads `flyway_schema_history` when present (exact
  applied-vs-repo drift, installed_by, execution time), falling back to
  SCHEMA_VERSION + `_EXPECTED_MIGRATIONS`. Baseline at V028; Flyway tracks
  from V029. In-file guards stay (defense against Snowsight bypass).
- `success = FALSE` raises OPS_MIGRATION_FAILED — the rule SHIPS DISABLED
  and flips on when Flyway lands (V025 config-as-code pattern).

**Terraform** (probable, account topology + policy):
- WAREHOUSE_CHANGE_REGISTRY gains attribution: each change joined to the
  QUERY_HISTORY ALTER that made it -> CHANGED_BY. A `DEPLOY_ACTORS` setting
  (comma list: Flyway svc user, Terraform svc user) splits MANAGED from
  MANUAL changes. OPS_UNMANAGED_CHANGE alerts on MANUAL warehouse changes
  — ships DISABLED until Terraform owns the warehouses, because today every
  change is "manual" and the rule would be pure noise.
- The Terraform service user is an expected actor: whitelisted in the
  break-glass policy (V025 pattern), watched but not alarmed.
- **Decision gate for V030**: if Terraform enforces owner/team tags at
  provision time, OBJECT_OWNERS becomes a view over TAG_REFERENCES plus a
  manual-override table, not a hand-maintained mapping. Hold the V030
  design until the Terraform decision is final.

## Rider (small, ships in the same migration)

- OPS_SLOW_RENDER scan arm adds `COALESCE(IS_RERUN, FALSE) = FALSE` so the
  V027 rerun sampling can never pollute the first-paint p95 sentinel.

## App surfaces

- Control Room: an Incidents queue above the triage queue (open incidents
  first, triage stays for the not-yet-declared); declare-from-selection on
  the timeline; incident detail reuses `charts.event_timeline` over members.
- Alerts: event rows gain "link to incident" in the lifecycle flow;
  `incident_declare` / `incident_close` join the EVENT_KIND vocabulary.
- Day replay: incident spans render over the replayed day.

## Bookkeeping (the usual gates enforce most of this)

Guard -20029 (< 28), version row 29, validate -> 29, admin dict entry,
teardown (INCIDENTS/INCIDENT_MEMBERS join the PRESERVED list), roles.sql
operator INSERT/UPDATE grants, canaries for every new reader, sqlglot parse
gate, locks: lifecycle SQL builders, proposal-view shape, lineage ALTERs,
disabled-by-default rules, IS_RERUN scan filter. No live-scan budget moves
— everything here reads OVERWATCH-owned tables.

## Out of scope

Paging/on-call escalation (not a PagerDuty), automated root-cause analysis,
silent auto-grouping, cross-account correlation, impression tracking (still
no — the r5 #4 decision stands).

## Open questions for the owner

1. Auto-declare CRITICALs: default ON acceptable? (One incident per dedupe
   family per 24h.)
2. Reopen window: 14 days the right recurrence horizon?
3. Declare/close rights: DBA profile only (same as remediation), or should
   ANALYST/MANAGER profiles declare (not close)?
