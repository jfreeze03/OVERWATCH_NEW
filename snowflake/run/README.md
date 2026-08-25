# Run box

`RUN_NEXT.sql` is the single rolling file of SQL to run in Snowsight, maintained by Claude on the `runbox` branch. Workflow: open the file on GitHub → **Copy raw contents** → paste into a Snowsight worksheet → run → paste the result back.

- Overwritten each time there is something new to run (git history keeps every past version).
- Every query is independent and fully qualified (`DBA_MAINT_DB.OVERWATCH.*`) — no `USE` needed.
- This branch is a transport lane only; it is **never merged** to `main`.
