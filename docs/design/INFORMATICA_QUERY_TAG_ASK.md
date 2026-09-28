# Ask to the ETL team: tag each Informatica session with a QUERY_TAG (Next-Fifty #14, 2026-09-28)

A one-page request. It changes nothing in Informatica's data flow: two SQL statements per Snowflake
session, set in the session properties.

## 1. Why

OVERWATCH already shows which Informatica task failed or ran slow (from `CONTROL_STATUS`), and
Operations ▸ Pipeline SLA ▸ Tonight ▸ *Explain a task* reads that task's Snowflake work from
`QUERY_HISTORY`. Today the only link between the two is **the procedure name plus the task's time
window**, because every `QUERY_TAG` on the nightly cycle is blank. That link:

- misses mapping (`M_*`) tasks, which send their SQL directly instead of calling a procedure;
- misses a task whose `CONTROL_STATUS.TASK_NAME` differs from the procedure it calls;
- can confuse two runs of the same procedure that overlap in time;
- caps the cost attribution on Pipeline SLA ▸ Performance, which keeps an "(unattributed)" remainder.

One session tag makes all of these exact.

## 2. The ask

In **each** Informatica Snowflake session, add to **Pre SQL** (the session-level pre-SQL property of the
Snowflake connector):

```sql
ALTER SESSION SET QUERY_TAG = '{"pipeline":"<workflow name>","task":"<session/task name>","run_id":"<CONTROL_STATUS.RUN_ID>","environment":"PROD","cost_center":"data-eng"}';
```

and to **Post SQL**:

```sql
ALTER SESSION UNSET QUERY_TAG;
```

| Key           | Value                                                                                   |
|---------------|-----------------------------------------------------------------------------------------|
| `pipeline`    | The workflow name — exactly `CONTROL_STATUS.WORKFLOW_NAME`.                               |
| `task`        | The session/task name — exactly `CONTROL_STATUS.TASK_NAME`. New, optional key.            |
| `run_id`      | Exactly the value the control framework writes to `CONTROL_STATUS.RUN_ID` (not Informatica's internal run id, unless that is what is stored). |
| `environment` | `PROD` / `SIT` / `DEV`, as in [ETL_COST_TAGS.md](ETL_COST_TAGS.md).                      |
| `cost_center` | The chargeback owner, as in [ETL_COST_TAGS.md](ETL_COST_TAGS.md).                        |

`pipeline`, `run_id`, `environment` and `cost_center` are the existing keys of the ETL cost-tag
convention ([ETL_COST_TAGS.md](ETL_COST_TAGS.md)); `target_object` stays optional. PowerCenter exposes the
workflow and session names as `$PMWorkflowName` and `$PMSessionName`; the ETL team to confirm the IDMC
equivalents and how the control framework's `RUN_ID` is reachable at session start.

## 3. Rules

- **Valid JSON:** double quotes around keys and values; escape any single quote as `''` inside the SQL
  literal. OVERWATCH reads the tag with `TRY_PARSE_JSON`, so a malformed tag is simply ignored.
- **At most 2000 characters** (Snowflake's `QUERY_TAG` limit).
- **Per session run**, not per connection object, and **unset in Post SQL**, so a pooled connection never
  carries a stale tag into the next session.
- **No secrets or personal data** in any value: `QUERY_TAG` is visible to anyone who can read query
  history.
- A procedure's own statements run in the caller's session, so the tag should reach them too; this is
  checked on the first tagged night (step 4).

## 4. How to verify (the morning after)

The DBA runs:

```sql
SELECT GET_PATH(TRY_PARSE_JSON(QUERY_TAG), 'pipeline')::VARCHAR AS PIPELINE,
       GET_PATH(TRY_PARSE_JSON(QUERY_TAG), 'task')::VARCHAR     AS TASK,
       GET_PATH(TRY_PARSE_JSON(QUERY_TAG), 'run_id')::VARCHAR   AS RUN_ID,
       COUNT(*) AS QUERIES
  FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
 WHERE START_TIME >= DATEADD('day', -1, CURRENT_TIMESTAMP())
   AND QUERY_TAG <> ''
 GROUP BY 1, 2, 3
 ORDER BY QUERIES DESC
 LIMIT 50;
```

Each tagged session should appear with its workflow, task and run id, and the procedure's statements
should be counted under the same tag. The **tag coverage** figure on Cost ▸ Unit costs ▸ *ETL unit
costs* should rise the same morning.

## 5. What OVERWATCH does with it

- **Immediately:** the ETL unit-cost KPIs ($/run, $/M rows, failed-run waste, tag coverage) light up,
  because `pipeline` and `run_id` are the keys they already read.
- **Phase 2 (deferred):** *Explain a task* and the run cost attribution match a task's statements by the
  exact `run_id` + `task` tag first, and fall back to procedure name + window only for untagged work.

## 6. Rollout and rollback

1. One workflow first — the nightly cycle's terminal workflow — for one night; run the check in step 4.
2. Then every workflow.

Rollback is removing the two Pre/Post SQL lines. OVERWATCH falls back to name + window on its own; no
OVERWATCH change or deploy is needed either way.

See also: [ETL_COST_TAGS.md](ETL_COST_TAGS.md) (the structured tag convention).
