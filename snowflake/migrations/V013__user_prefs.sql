-- V013__user_prefs.sql — per-user preferences: saved filter views and a
-- default landing view. Written by the app as CURRENT_USER(); this is a
-- convenience store, not a security boundary. Grants for viewer roles are
-- in roles.sql (re-run it after this migration). Idempotent.

CREATE TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.USER_PREFS (
    USER_NAME  VARCHAR(200)  NOT NULL DEFAULT CURRENT_USER(),
    PREF_KEY   VARCHAR(100)  NOT NULL,   -- 'DEFAULT_VIEW' | 'VIEW:<name>'
    PREF_VALUE VARCHAR(4000),            -- JSON: {"page", "section", "filters"}
    UPDATED_AT TIMESTAMP_NTZ  NOT NULL DEFAULT CURRENT_TIMESTAMP(),
    PRIMARY KEY (USER_NAME, PREF_KEY)
);

MERGE INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION t
USING (SELECT 13 AS VERSION, 'user prefs: saved views + default landing' AS DESCRIPTION) s
ON t.VERSION = s.VERSION
WHEN NOT MATCHED THEN INSERT (VERSION, DESCRIPTION) VALUES (s.VERSION, s.DESCRIPTION);
