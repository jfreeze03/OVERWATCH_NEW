# ETL cost tags — the structured QUERY_TAG convention (Phase 3, 2026-07-14)

Cost attribution for pipelines is only as good as the tags on the queries.
OVERWATCH's ETL unit-cost KPIs (Cost & Contract → Unit costs → *ETL unit costs*)
read a **JSON object** in Snowflake's `QUERY_TAG`. Set it once per session/run:

```sql
ALTER SESSION SET QUERY_TAG = '{"pipeline":"daily_edw_load","run_id":"2026-07-14T06:00Z","target_object":"ALFA_EDW_PRD.DW.FCT_SALES","environment":"PROD","cost_center":"data-eng"}';
```

| Key             | Purpose                                                        |
|-----------------|---------------------------------------------------------------|
| `pipeline`      | Stable pipeline name — the grain of $/run, $/M rows, $/TiB.    |
| `run_id`        | One execution (timestamp or orchestration id). Counts runs.   |
| `target_object` | The object the run builds (ties into FACT_OBJECT_COST_DAILY).  |
| `environment`   | PROD / SIT / DEV — filter prod cost from noise.                |
| `cost_center`   | Chargeback owner.                                             |

**How it's read.** `app/data/etl_sql.py` parses the tag with
`GET_PATH(TRY_PARSE_JSON(QUERY_TAG), 'pipeline')` and joins to MEASURED
`QUERY_ATTRIBUTION_HISTORY` credits. Only `pipeline`-tagged queries appear in
the per-pipeline table; `etl_tag_coverage` reports the **credit-weighted
coverage** so an untagged fleet reads as low coverage, not as "$0 pipelines".

**KPIs produced** (dollarized at the configured rate in the UI):
$/run, $/M rows, $/TiB scanned, failed-run (retry/abort) waste, and tag
coverage %. Method = MEASURED (see the metric registry, Admin → Metrics).

Adopt the tag in your orchestrator (dbt `query_comment`, Airflow, a stored
proc's `ALTER SESSION`, etc.) and the panel lights up on its own.
