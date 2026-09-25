"""First-response playbooks per alert rule. Pure module.

Three moves, not an essay: what it means, what to check first, when to
escalate. Shown in the alert drawer and available to the digest.
"""

from __future__ import annotations

PLAYBOOKS: dict[str, str] = {
    "COST_CLOUD_SVC_RATIO": (
        "**Means:** this warehouse burns an outsized share on cloud services — many tiny "
        "queries, metadata-heavy patterns, or compile-heavy SQL.\n\n"
        "1. Cost > Spend → *Cloud-services health*: confirm the warehouse and open the "
        "compile-heavy families table under it.\n"
        "2. Look for chatty automation (single-row queries in loops, aggressive polling).\n"
        "3. Fix = batch the small queries, cache lookups, or move the workload; re-check in a week."
    ),
    "COST_STORAGE_SURGE": (
        "**Means:** a database grew more than the threshold in one day.\n\n"
        "1. Cost Intelligence > Optimization & Savings → *Storage growth movers* for the table-level movers.\n"
        "2. Check for runaway CTAS/backup copies and missing retention on staging.\n"
        "3. If intentional (backfill, new feed), note it on the event and resolve."
    ),
    "COST_SERVERLESS_CREEP": (
        "**Means:** a serverless service doubled week-over-week.\n\n"
        "1. Cost Intelligence > Spend & Attribution: switch the category view to the service in the title.\n"
        "2. Auto-clustering/MV/search-opt: confirm someone enabled it on purpose and the "
        "table churn justifies it.\n"
        "3. If not intentional, suspend the feature before month-end, then resolve."
    ),
    "COST_CLOUD_SVC_ANOMALY": (
        "**Means:** this warehouse's cloud-services credits stepped far outside its own 28-day "
        "baseline — a per-warehouse signal, so a warehouse that is chronically compile-heavy stays "
        "quiet and only a real change surfaces.\n\n"
        "1. Operations > Queries → *Cloud-services chatter by application*: which client/driver moved.\n"
        "2. Cost > Spend → *Cloud-services health*: the compile-heavy families under that warehouse.\n"
        "3. Fix = quiet the chatty tool / cache metadata / cut reconnects (credits shown are gross "
        "usage, before the account-level ~10% rebate); recurring on the same warehouse = raise the "
        "threshold on the rule."
    ),
    "COST_IDLE_OPPORTUNITY": (
        "**Means:** over the last 14 complete days this warehouse burned a large share of its credits "
        "in hours with zero queries; a tighter AUTO_SUSPEND recovers at least the rule threshold (USD "
        "per month, after the ~60s resume tail per active hour); and its current timer — read from the "
        "daily SHOW WAREHOUSES snapshot — is disabled or above 60s. Weekly per warehouse; a mid-week "
        "jump past 5x the threshold re-raises it as HIGH.\n\n"
        "1. Cost Intelligence > Optimization & Savings → *Idle & sizing* with a 14-day window: the same "
        "warehouse shows about the same actionable USD/month (the alert counts 14 complete days only).\n"
        "2. Apply the ALTER in the alert detail, the *Respond — closed loop* panel in this drawer, or "
        "*Remediation & ledger* (both re-read the live setting and never raise an already-tight timer). "
        "A latency-sensitive workload pays a cold resume on its first query — check that first.\n"
        "3. A timer decrease is picked up by the next daily change scan and auto-booked (a zero-dollar row "
        "the closed loop booked from this alert is adopted as that booking, not duplicated); "
        "SP_LEDGER_AUTOBOOK settles the measured saving once the 14-day tracking window closes. Enabling "
        "a timer on a never-suspend warehouse is not auto-booked — book it once (a closed-loop run already "
        "booked its zero-dollar row; otherwise use *Remediation & ledger*) and verify it on the Savings "
        "ledger."
    ),
    "COST_ANOMALY_SWEEP": (
        "**Means:** yesterday's credits for this series sit far outside its 28-day pattern.\n\n"
        "1. Investigate → lands on Cost Intelligence > Spend & Attribution scoped to the entity; check the day's attribution.\n"
        "2. Operations > Queries for that window: one heavy query family usually explains it.\n"
        "3. Recurring anomaly on the same series = raise the threshold or fix the workload."
    ),
    "PIPE_COPY_FAILURES": (
        "**Means:** files failed to load in the last 24h; the target table is behind.\n\n"
        "1. Operations > Pipeline SLA → *File-load failures* for the sample error.\n"
        "2. Bad-file errors: inspect the stage file; permission/format errors: check the "
        "pipe/file-format definition.\n"
        "3. After the fix, re-COPY or refresh the pipe, confirm freshness, resolve."
    ),
    "PIPE_DT_FAILURES": (
        "**Means:** a dynamic table's refresh failed — downstream reads are stale.\n\n"
        "1. Operations > Pipeline SLA → *Dynamic table refresh health*.\n"
        "2. `SELECT SYSTEM$GET_DT_REFRESH_HISTORY_ERRORS` / Snowsight DT page for the root error.\n"
        "3. Fix the upstream break, wait one refresh cycle, confirm SUCCEEDED, resolve."
    ),
    "SEC_CRED_EXPIRY": (
        "**Means:** a credential expires within the threshold (or already has).\n\n"
        "1. Security > Access → *Expiring credentials* for owner and days left.\n"
        "2. Rotate: create the new secret/key first, roll consumers, then retire the old one.\n"
        "3. Expired + job failures already happening = treat as an incident, not a chore.\n\n"
        "Auto-clear (V157, when the rule's auto-clear is on): once no credential with this user and "
        "name is still expiring inside the rule window — rotated to a later expiry, or removed — the "
        "hourly scan resolves the OPEN event as CONDITION_ENDED (after at least 1h; ACK'd and snoozed "
        "events stay yours to close). If an EXPIRED event was auto-declared into an incident, that "
        "incident moves to MITIGATED once all its member alerts resolve — close it with the root cause."
    ),
    "SEC_BREAK_GLASS_USE": (
        "**Means:** heavy statement volume under a break-glass admin role.\n\n"
        "1. Security > Changes → *Break-glass role activity* for who and how much.\n"
        "2. Expected admin work? Ask them to switch to SNOW_SYSADMINS day-to-day.\n"
        "3. Unexpected? Check LOGIN_HISTORY for that user and treat as a security event."
    ),
    "PERF_CHANGE_REGRESSION": (
        "**Means:** a procedure/task runs worse after a change, vs its frozen baseline.\n\n"
        "1. Operations > Change impact: open the object's run history around the change line.\n"
        "2. Diff the DDL (CHANGE_DDL column) against the prior version; check the new query "
        "profile for the regressed step.\n"
        "3. Fix forward or roll back; the tracker verdicts IMPROVED once p95/credits recover."
    ),
    "OPS_SCAN_DEGRADED": (
        "**Means:** one or more rule blocks inside the alert scan errored this run (hourly "
        "SP_ALERT_SCAN, or SP_ALERT_SCAN_DAILY when the event key carries `|DAILY|`). The other "
        "rules kept running, but the failed rule was skipped — its silence proves nothing until "
        "this is fixed.\n\n"
        "1. Admin > Errors & telemetry → *Persisted error log*: PAGE `AlertScan`, type "
        "`rule_block_failed` — CONTEXT names the rule, ERROR_MESSAGE has the SQL error. Snowsight: "
        "`SELECT LOGGED_AT, CONTEXT, ERROR_MESSAGE FROM DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG "
        "WHERE PAGE = 'AlertScan' AND ERROR_TYPE = 'rule_block_failed' ORDER BY LOGGED_AT DESC LIMIT 50;`\n"
        "2. Usual causes: a revoked grant, or an ACCOUNT_USAGE column change after a Snowflake "
        "release (Admin > Canary shows the same drift). For the task chain run "
        "`snowflake/alert_pipeline_check.sql` top to bottom.\n"
        "3. After the fix, `CALL DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN();` (or `SP_ALERT_SCAN_DAILY()`), "
        "confirm no new `rule_block_failed` row, then resolve."
    ),
    "OPS_PIPELINE_DEGRADED": (
        "**Means:** part of OVERWATCH's own pipeline stopped while its tasks still read SUCCEEDED: a "
        "telemetry source is past its load cadence (hourly sources 3h, `DAILY`/`METERING` sources "
        "30h), a loader logged a failure and carried on, or the alert notifier has not acquired its "
        "sender lease in 3h while a delivery route is enabled. `ALERT_SCAN_HOURLY` / "
        "`ALERT_SCAN_DAILY` are the alert scans' own heartbeats: that scan stopped, or its heartbeat "
        "stamp keeps failing (`scan_heartbeat_failed`).\n\n"
        "1. Admin > Migrations & freshness → *Diagnose stale sources* (maps each stale source to its "
        "latest loader error). Snowsight: `SELECT LOGGED_AT, PAGE, ERROR_TYPE, CONTEXT, ERROR_MESSAGE "
        "FROM DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG WHERE ERROR_TYPE LIKE '%_failed' ORDER BY LOGGED_AT "
        "DESC LIMIT 50;`\n"
        "2. Task state: run `snowflake/loader_chain_check.sql`; a root auto-suspends after 10 "
        "consecutive failures (V071) — fix the cause, then `ALTER TASK ... RESUME`.\n"
        "3. After the fix, wait one cycle, confirm the row is fresh, then resolve. A stale source raises "
        "at most once per last-load day, and a loader failure once per source per day (the optional "
        "tag-coverage / task-node / AI-usage arms only through their stale row) — for an intentionally "
        "unloaded source, resolve it once with a note."
    ),
    "OPS_CANARY_FAIL": (
        "**Means:** the weekly (Mondays 05:30 Central) source canary (SP_CANARY_SENTINEL) could "
        "not read one or more "
        "objects OVERWATCH depends on — an ACCOUNT_USAGE view or an OVERWATCH table. Pages and "
        "loaders that read it are failing or empty.\n\n"
        "1. Snowsight: `SELECT RUN_AT, CHECK_NAME, ERROR FROM DBA_MAINT_DB.OVERWATCH.CANARY_RESULTS "
        "WHERE STATUS = 'FAIL' AND RUN_AT >= DATEADD('day', -1, CURRENT_TIMESTAMP()) ORDER BY RUN_AT DESC;`\n"
        "2. Admin > Canary: run the per-builder canary to see which app queries break. 'does not "
        "exist or not authorized' = a revoked grant (re-grant to the app owner role); 'invalid "
        "identifier' = column drift after a Snowflake release (fix the builder).\n"
        "3. `CALL DBA_MAINT_DB.OVERWATCH.SP_CANARY_SENTINEL();`, confirm every check PASS, resolve."
    ),
    "OPS_SLOW_RENDER": (
        "**Means:** a page's 7-day p95 first paint (APP_USAGE.RENDER_MS, at least 20 visits) is "
        "over the rule threshold. People are waiting on that page; nothing is broken.\n\n"
        "1. Admin > Performance → *Performance SLO scorecard (7d)* and *Fleet telemetry by page "
        "(7d)*: confirm the page and its slow statement families.\n"
        "2. Snowsight: `SELECT PAGE, APPROX_PERCENTILE(RENDER_MS, 0.95) / 1000 AS P95_S, COUNT(*) AS N "
        "FROM DBA_MAINT_DB.OVERWATCH.APP_USAGE WHERE AT >= DATEADD('day', -7, CURRENT_TIMESTAMP()) "
        "AND RENDER_MS IS NOT NULL GROUP BY 1 ORDER BY 2 DESC;`\n"
        "3. Levers: move a live ACCOUNT_USAGE read onto its mart, lazy-load the section, or batch "
        "reads with run_batch. Week-keyed: it re-raises next week while still slow."
    ),
    "DQ_BREACH": (
        "**Means:** a registered data-product table's latest load added an outlier number of rows "
        "vs its own prior loads (robust z at or over the threshold) — a spike (double-run, "
        "replayed file) or a thin load (partial feed).\n\n"
        "1. Operations > Pipeline SLA → *Data checks* → *Row-volume anomalies (registered products, "
        "28d)*: direction, baseline median and the catalog owner.\n"
        "2. Snowsight: `SELECT START_TIME, ROWS_ADDED FROM SNOWFLAKE.ACCOUNT_USAGE.TABLE_DML_HISTORY "
        "WHERE DATABASE_NAME = '<DB>' AND SCHEMA_NAME = '<SCHEMA>' AND TABLE_NAME = '<TABLE>' "
        "AND START_TIME >= DATEADD('day', -14, CURRENT_TIMESTAMP()) ORDER BY START_TIME DESC;` — "
        "a drop: also check *Recurring failures* → *File-load failures*.\n"
        "3. Tell the owner named in the event and re-run or dedupe the load; a planned new normal "
        "= resolve as expected (the baseline absorbs it over the next loads)."
    ),
    "DQ_RECON_ERROR": (
        "**Means:** the nightly reconciliation scan found source-vs-target mismatches for at least "
        "the threshold number of metrics — downstream numbers for those metrics are not trustworthy "
        "yet.\n\n"
        "1. Operations > Pipeline SLA → *Data checks* → *Reconciliation errors (source vs target)*, "
        "then *Reconciliation recurrence* for whether the same metric keeps breaking.\n"
        "2. Snowsight: `SELECT MTRC, N, LATEST_LOAD FROM DBA_MAINT_DB.OVERWATCH.ETL_RECON_RESULTS "
        "ORDER BY N DESC;` — trace the worst metric to the load that feeds it.\n"
        "3. Fix and re-run that load, confirm the metric leaves the next scan, then resolve; hold "
        "downstream reporting on those metrics until it does."
    ),
    "DQ_SCHEMA_DRIFT": (
        "**Means:** a table registered as an OBJECT data product changed shape vs its prior daily "
        "column snapshot — columns added, removed or retyped (listed in the event detail).\n\n"
        "1. Find the DDL: Security > Changes → *Who changed what (DDL/DCL)*, or "
        "`SELECT START_TIME, USER_NAME, QUERY_TYPE, QUERY_TEXT FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY "
        "WHERE (QUERY_TYPE ILIKE 'ALTER_TABLE%' OR QUERY_TYPE ILIKE 'CREATE_TABLE%') "
        "AND QUERY_TEXT ILIKE '%<TABLE>%' AND START_TIME >= DATEADD('day', -2, CURRENT_TIMESTAMP()) "
        "ORDER BY START_TIME DESC;`\n"
        "2. Compare snapshots: `SELECT SNAPSHOT_DAY, COLUMN_NAME, DATA_TYPE FROM "
        "DBA_MAINT_DB.OVERWATCH.DQ_SCHEMA_SNAPSHOT WHERE FQN = '<DB.SCHEMA.TABLE>' "
        "ORDER BY COLUMN_NAME, SNAPSHOT_DAY DESC;`\n"
        "3. Intended: tell downstream consumers (views, loaders, extracts), then resolve. "
        "Unintended removal/retype: restore it (owning DDL or Time Travel) before the next load."
    ),
    "WH_CHANGE_REGRESSION": (
        "**Means:** a warehouse setting change (size, auto-suspend, min/max clusters, scaling "
        "policy) made things worse vs its frozen 14-day pre-change baseline — more USD/day, higher "
        "p95, queueing, spill or failures. CRITICAL when USD/day at least doubled.\n\n"
        "1. Operations > Change impact → *Warehouse setting changes*: the REGRESSED row's "
        "before/after numbers; CHANGE_SOURCE says MANAGED (deploy user) or MANUAL (a human).\n"
        "2. Rule out a new workload: Operations > Queries on that warehouse, after vs before the change.\n"
        "3. Unjustified → roll back, e.g. `ALTER WAREHOUSE <WAREHOUSE> SET WAREHOUSE_SIZE = '<OLD_VALUE>';` "
        "(or AUTO_SUSPEND / MIN_CLUSTER_COUNT / MAX_CLUSTER_COUNT / SCALING_POLICY), note it on the "
        "event and resolve."
    ),
    "SEC_FAILED_LOGINS": (
        "**Means:** a user crossed the failed-login threshold on one day — a stale secret in a "
        "job, a locked-out person, or password spraying.\n\n"
        "1. Security > Access → *Authentication*: *Failed logins*, *Failed-login reasons* (network "
        "policy vs bad credentials) and *Account-takeover candidates* (a failed burst then a success).\n"
        "2. Snowsight: `SELECT EVENT_TIMESTAMP, CLIENT_IP, REPORTED_CLIENT_TYPE, ERROR_MESSAGE, IS_SUCCESS "
        "FROM SNOWFLAKE.ACCOUNT_USAGE.LOGIN_HISTORY WHERE USER_NAME = '<USER>' "
        "AND EVENT_TIMESTAMP >= DATEADD('day', -2, CURRENT_TIMESTAMP()) ORDER BY EVENT_TIMESTAMP DESC;`\n"
        "3. Service user with a stale secret → rotate it and fix the job. Unknown IPs, or a success "
        "right after the burst → `ALTER USER <USER> SET DISABLED = TRUE;` and treat as a security incident."
    ),
    "SEC_NEW_ADMIN_NETWORK": (
        "**Means:** a user holding ACCOUNTADMIN / SNOW_ACCOUNTADMINS / SNOW_SYSADMINS logged in "
        "from a client IP not seen for them in the prior 90 days.\n\n"
        "1. Security > Access → *Authentication* → *New networks for privileged users (90-day "
        "baseline)*: user, IP, first seen, auth factor.\n"
        "2. Confirm with the user (travel, VPN egress change, new host). Snowsight: "
        "`SELECT EVENT_TIMESTAMP, CLIENT_IP, REPORTED_CLIENT_TYPE, FIRST_AUTHENTICATION_FACTOR, "
        "SECOND_AUTHENTICATION_FACTOR, IS_SUCCESS FROM SNOWFLAKE.ACCOUNT_USAGE.LOGIN_HISTORY "
        "WHERE USER_NAME = '<USER>' AND EVENT_TIMESTAMP >= DATEADD('day', -2, CURRENT_TIMESTAMP()) "
        "ORDER BY EVENT_TIMESTAMP DESC;` — a password-only login is the bigger concern.\n"
        "3. Unexplained → `ALTER USER <USER> SET DISABLED = TRUE;`, rotate their credentials, and "
        "review what ran from that IP (QUERY_HISTORY by USER_NAME)."
    ),
    "SEC_NEW_EXPOSURE": (
        "**Means:** a privilege was granted to PUBLIC in the last 24h. Every role inherits PUBLIC, "
        "so this is instant account-wide access to the object(s) in the title.\n\n"
        "1. Security > Changes → *Recent grant changes*: who granted it and when; confirm intent.\n"
        "2. Full set: `SELECT PRIVILEGE, GRANTED_ON, TABLE_CATALOG, TABLE_SCHEMA, NAME, GRANTED_BY, "
        "CREATED_ON FROM SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_ROLES WHERE GRANTEE_NAME = 'PUBLIC' "
        "AND DELETED_ON IS NULL AND CREATED_ON >= DATEADD('day', -2, CURRENT_TIMESTAMP()) "
        "ORDER BY CREATED_ON DESC;`\n"
        "3. Not intended → `REVOKE <PRIVILEGE> ON <GRANTED_ON> <DB.SCHEMA.NAME> FROM ROLE PUBLIC;` "
        "(a batch grant: `REVOKE <PRIVILEGE> ON ALL <OBJECT_TYPE>S IN SCHEMA <DB.SCHEMA> FROM ROLE "
        "PUBLIC;` and check `SHOW FUTURE GRANTS IN SCHEMA <DB.SCHEMA>;`). Grant a named role "
        "instead, then resolve.\n\n"
        "Auto-clear (V157, when the rule's auto-clear is on): once every grant of this batch shows a "
        "revoke (DELETED_ON) in GRANTS_TO_ROLES, the hourly scan resolves the OPEN event as "
        "CONDITION_ENDED (after at least 1h; ACCOUNT_USAGE can lag about 2h; ACK'd and snoozed events "
        "stay yours to close)."
    ),
    "PIPE_TASK_FAILURES": (
        "**Means:** a task failed at least the threshold number of times on one day (retries "
        "collapse to the final attempt). The event detail carries the last error text.\n\n"
        "1. Operations > Tasks → *SLA* → *Actively broken tasks (failure streaks)*, then *Runs* "
        "for the attempt history.\n"
        "2. Snowsight: `SELECT SCHEDULED_TIME, STATE, ERROR_CODE, ERROR_MESSAGE FROM "
        "SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY WHERE DATABASE_NAME = '<DB>' AND SCHEMA_NAME = '<SCHEMA>' "
        "AND NAME = '<TASK>' AND SCHEDULED_TIME >= DATEADD('day', -2, CURRENT_TIMESTAMP()) "
        "ORDER BY SCHEDULED_TIME DESC;`\n"
        "3. Fix the root error, `EXECUTE TASK <DB.SCHEMA.TASK>;` once to prove it, confirm "
        "SUCCEEDED, resolve. A failed root also skipped its children — check Tasks > Graph."
    ),
    # V156 (Next-Fifty rank 2): the nightly Informatica cycle, pushed by SP_SCAN_ETL_CYCLE (hourly via the
    # V157 alert-scan add-on arm). Same night key / retry collapse as Operations > Pipeline SLA > Tonight.
    "PIPE_ETL_TASK_FAILED": (
        "**Means:** at least the threshold number (never fewer than one) of one workflow's tasks in "
        "tonight's Informatica cycle ended FAILED on their final attempt (retries collapse to the last "
        "attempt, so a failure a retry fixed auto-clears). A failure in the cycle TERMINAL workflow is "
        "raised HIGH — the cycle cannot finish until it is re-run. Snoozing one night's event also "
        "snoozes that workflow's later nights until it wakes.\n\n"
        "1. Operations > Pipeline SLA → *Tonight* → *Tonight at a glance* (failed workflows), then "
        "*Recurring failures* → *Failure recurrence* for whether this task is chronic.\n"
        "2. Snowsight: `SELECT CYCLE_DATE, TASK_NAME, TERMINAL_STATUS, FIRST_START, TERMINAL_START, "
        "TERMINAL_END FROM DBA_MAINT_DB.OVERWATCH.ETL_CYCLE_TASKS WHERE WORKFLOW_NAME = '<workflow>' "
        "ORDER BY CYCLE_DATE DESC, FIRST_START;` then the Informatica session log for the error.\n"
        "3. Fix and re-run the failed session(s) before the SLA target; resolve ACTIONED (NOISE if "
        "Informatica already handled it)."
    ),
    "PIPE_ETL_CYCLE_NOT_STARTED": (
        "**Means:** the cycle STARTER workflow (ETL_CYCLE_START_WORKFLOW) has no run for a night it ran on "
        "the same weekday last week, and it is past last week's kickoff + the rule's grace minutes — the "
        "same test as the *Cycle start: Overdue* tile. Nothing downstream loads tonight. Snoozing it also "
        "snoozes the next missed nights until it wakes.\n\n"
        "1. Operations > Pipeline SLA → *Tonight* → *Tonight at a glance*: confirm Cycle start = Overdue and "
        "when it last ran.\n"
        "2. Check the Informatica scheduler / integration service and the starter's schedule: `SELECT "
        "MAX(TASK_START_DTTM) FROM <ETL_CONTROL_STATUS_FQN> WHERE WORKFLOW_NAME = '<starter>';`\n"
        "3. Start the cycle, confirm the starter lands in CONTROL_STATUS, resolve ACTIONED. A planned no-run "
        "night (holiday, change freeze) = resolve as EXPECTED."
    ),
    "PIPE_ETL_CYCLE_LATE": (
        "**Means:** tonight's cycle (starter → terminal workflow) is at risk of, or past, its SLA clock. "
        "WARN (the rule severity, HIGH by default) = the terminal is unfinished inside the rule's lead "
        "window (threshold minutes before ETL_SLA_TARGET_HHMM) or a late start projects past the hard "
        "deadline. Past the target or the hard deadline: a cycle that already FINISHED late keeps the rule "
        "severity (email, no incident); a cycle still unfinished is CRITICAL and auto-declares an incident "
        "(when incident auto-declare is on). Each crossing re-alerts and supersedes the lower band. "
        "Snoozing one night's WARN also snoozes later nights' WARN, never a CRIT/EXH.\n\n"
        "1. Operations > Pipeline SLA → *Tonight* → *Tonight at a glance* (what failed / did not run / is "
        "still running), then *SLA finish forecast* for how late it usually runs.\n"
        "2. Snowsight: `SELECT WORKFLOW_NAME, TASK_NAME, TERMINAL_STATUS, FIRST_START FROM "
        "DBA_MAINT_DB.OVERWATCH.ETL_CYCLE_TASKS WHERE CYCLE_DATE = (SELECT MAX(CYCLE_DATE) FROM "
        "DBA_MAINT_DB.OVERWATCH.ETL_CYCLE_TASKS) AND TERMINAL_END IS NULL AND FIRST_OK_END IS NULL ORDER BY "
        "FIRST_START;` — the hung step on the critical path (a task with a FIRST_OK_END already finished clean "
        "this cycle, so a later re-run of it does not hold the night open).\n"
        "3. Unblock / re-run it, tell report owners if the target will be missed, resolve ACTIONED; a known-"
        "long night (month-end) = EXPECTED. Only lead-window WARN tags tune THRESHOLD_NUM: recurring "
        "lead-window WARNs on nights that finish on time = lower the rule's lead threshold."
    ),
    "PIPE_VOLUME_DROP": (
        "**Means:** a PROD table that normally adds 1,000+ rows/day loaded far fewer yesterday "
        "than its prior-7-day average.\n\n"
        "1. Operations > Pipeline SLA → *Data checks* → *Volume drops (yesterday vs prior-7d "
        "average)*; then *Recurring failures* → *File-load failures* for a failed COPY behind it.\n"
        "2. Snowsight: `SELECT DATE(START_TIME) AS DAY, SUM(ROWS_ADDED) AS ROWS_ADDED FROM "
        "SNOWFLAKE.ACCOUNT_USAGE.TABLE_DML_HISTORY WHERE DATABASE_NAME = '<DB>' AND SCHEMA_NAME = "
        "'<SCHEMA>' AND TABLE_NAME = '<TABLE>' AND START_TIME >= DATEADD('day', -10, CURRENT_DATE()) "
        "GROUP BY 1 ORDER BY 1 DESC;`\n"
        "3. Short upstream feed or failed load → re-run it and confirm today's volume recovers; "
        "holiday or planned cutover → note it and resolve as expected."
    ),
    "PIPE_REF_GAP": (
        "**Means:** the nightly reference-gap scan found new source codes with no XLAT "
        "translation row. The next nightly load hard-fails on them — fix before the cycle.\n\n"
        "1. Operations > Pipeline SLA → *Tonight* → *Reference-data gaps (codes missing from XLAT)*.\n"
        "2. Snowsight: `SELECT CHECK_NAME, NEW_CODE, SCANNED_AT FROM "
        "DBA_MAINT_DB.OVERWATCH.ETL_REF_GAP_RESULTS ORDER BY CHECK_NAME, NEW_CODE;`\n"
        "3. Add the XLAT row(s) with the data owner (the mapping is a business call), "
        "`CALL DBA_MAINT_DB.OVERWATCH.SP_SCAN_REF_GAPS();` to confirm the list is empty, resolve."
    ),
}

_FAMILY_FALLBACK = {
    "COST": "1. Cost Intelligence > Spend & Attribution for the window in the title.\n2. Attribution to find the owner.\n3. Note findings on the event; resolve with the fix.",
    "PERF": "1. Operations > Queries scoped to the entity.\n2. Compare p95 and queue vs the prior window.\n3. Right-size or fix the query family.",
    "PIPE": "1. Operations > Pipeline SLA.\n2. Find the failing loader/task and its error.\n3. Re-run after the fix and confirm freshness.",
    "SEC": "1. Security page for the matching panel.\n2. Verify with the named user/owner.\n3. Rotate/revoke as needed and document.",
    "TASK": "1. Operations > Tasks for the failure detail.\n2. Check the task's history and error.\n3. Fix and resume the task.",
    "BUDGET": "1. Cost Intelligence > Contract & Forecast for pacing.\n2. Identify the driver in Attribution.\n3. Adjust the budget or the workload.",
}


def playbook_for(rule_id: str) -> str:
    rid = str(rule_id or "").strip().upper()
    if rid in PLAYBOOKS:
        return PLAYBOOKS[rid]
    for prefix, text in _FAMILY_FALLBACK.items():
        if rid.startswith(prefix):
            return text
    return "No playbook yet for this rule — add one in app/logic/playbooks.py."
