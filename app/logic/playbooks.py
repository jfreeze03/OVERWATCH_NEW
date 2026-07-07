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
        "1. Cost > Optimization → *Storage growth movers* for the table-level movers.\n"
        "2. Check for runaway CTAS/backup copies and missing retention on staging.\n"
        "3. If intentional (backfill, new feed), note it on the event and resolve."
    ),
    "COST_SERVERLESS_CREEP": (
        "**Means:** a serverless service doubled week-over-week.\n\n"
        "1. Cost > Spend: switch the category view to the service in the title.\n"
        "2. Auto-clustering/MV/search-opt: confirm someone enabled it on purpose and the "
        "table churn justifies it.\n"
        "3. If not intentional, suspend the feature before month-end, then resolve."
    ),
    "COST_ANOMALY_SWEEP": (
        "**Means:** yesterday's credits for this series sit far outside its 28-day pattern.\n\n"
        "1. Investigate → lands on Cost > Spend scoped to the entity; check the day's attribution.\n"
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
        "3. Expired + job failures already happening = treat as an incident, not a chore."
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
}

_FAMILY_FALLBACK = {
    "COST": "1. Cost > Spend for the window in the title.\n2. Attribution to find the owner.\n3. Note findings on the event; resolve with the fix.",
    "PERF": "1. Operations > Queries scoped to the entity.\n2. Compare p95 and queue vs the prior window.\n3. Right-size or fix the query family.",
    "PIPE": "1. Operations > Pipeline SLA.\n2. Find the failing loader/task and its error.\n3. Re-run after the fix and confirm freshness.",
    "SEC": "1. Security page for the matching panel.\n2. Verify with the named user/owner.\n3. Rotate/revoke as needed and document.",
    "TASK": "1. Operations > Tasks for the failure detail.\n2. Check the task's history and error.\n3. Fix and resume the task.",
    "BUDGET": "1. Cost > Contract for pacing.\n2. Identify the driver in Attribution.\n3. Adjust the budget or the workload.",
}


def playbook_for(rule_id: str) -> str:
    rid = str(rule_id or "").strip().upper()
    if rid in PLAYBOOKS:
        return PLAYBOOKS[rid]
    for prefix, text in _FAMILY_FALLBACK.items():
        if rid.startswith(prefix):
            return text
    return "No playbook yet for this rule — add one in app/logic/playbooks.py."
