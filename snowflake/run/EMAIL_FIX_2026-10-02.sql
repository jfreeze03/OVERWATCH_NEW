-- #####################################################################
--  OVERWATCH -- EMAIL_FIX_2026-10-02.sql
--  Make JDees@alfains.com the DEFAULT recipient of the OVERWATCH_EMAIL notification integration.
--
--  WHY: V164 (applied 2026-09-30) added a one-time email escalation to SP_NOTIFY_WEBHOOK. It sends through
--  SYSTEM$SEND_SNOWFLAKE_NOTIFICATION(..., SNOWFLAKE.NOTIFICATION.INTEGRATION('OVERWATCH_EMAIL')) with NO
--  address of its own, so it relies on the integration's DEFAULT_RECIPIENTS, which was never set. Every
--  escalation since then logged APP_ERROR_LOG escalation_email_failed: "No recipients specified and the
--  notification integration does not specify default recipients". The Teams posts were not affected by
--  that error (the email leg is isolated and logged).
--
--  WHAT THIS CHANGES: only DEFAULT_RECIPIENTS on OVERWATCH_EMAIL. It does NOT touch ALLOWED_RECIPIENTS,
--  ENABLED, the native email ALERT bodies, any OVERWATCH table or any task. Owner decision 2026-10-02:
--  JDees@alfains.com is the default and must never be overwritten.
--
--  RUN AS: a role that owns OVERWATCH_EMAIL (or ACCOUNTADMIN). Step 0 shows the owner. Run top to bottom;
--  stop at the first error and read the note under that step.
-- #####################################################################

-- 0. Who owns the integration (run the ALTER in step 2 as that role or ACCOUNTADMIN).
SHOW INTEGRATIONS LIKE 'OVERWATCH_EMAIL';

-- 1. Look first (read-only). Note ENABLED, ALLOWED_RECIPIENTS and DEFAULT_RECIPIENTS (expected: empty).
DESC NOTIFICATION INTEGRATION OVERWATCH_EMAIL;

-- 2. Set the default recipient. Only DEFAULT_RECIPIENTS changes.
ALTER NOTIFICATION INTEGRATION OVERWATCH_EMAIL SET DEFAULT_RECIPIENTS = ('JDees@alfains.com');

--    If step 2 fails because the address is not in ALLOWED_RECIPIENTS: add it to the list, KEEPING every
--    address step 1 showed (SET replaces the whole list), then re-run step 2. Fill in the existing ones:
-- ALTER NOTIFICATION INTEGRATION OVERWATCH_EMAIL
--     SET ALLOWED_RECIPIENTS = (<every address step 1 listed, each in quotes>, 'JDees@alfains.com');
--    If it fails because the address is not verified: JDees@alfains.com must be the VERIFIED email of a
--    user in this account (the user verifies it from the Snowsight profile), then re-run step 2.

-- 3. Verify: DEFAULT_RECIPIENTS now reads JDees@alfains.com, ENABLED is true.
DESC NOTIFICATION INTEGRATION OVERWATCH_EMAIL;

-- 4. Confirm the escalation leg points at this integration and is on (read-only).
--    Expect ESCALATE_EMAIL_INTEGRATION = OVERWATCH_EMAIL (blank = email leg off) and ESCALATE_AFTER_MIN = 120
--    (0 = escalation off). A missing row means the default (OVERWATCH_EMAIL / 120) applies.
SELECT KEY, VALUE, UPDATED_AT
  FROM DBA_MAINT_DB.OVERWATCH.SETTINGS
 WHERE KEY IN ('ESCALATE_AFTER_MIN', 'ESCALATE_EMAIL_INTEGRATION')
 ORDER BY KEY;

-- 5. One test email through the exact call the escalation leg makes (no address in the call: it goes to
--    DEFAULT_RECIPIENTS). Expect a SUCCESS-style result and an email at JDees@alfains.com within minutes.
CALL SYSTEM$SEND_SNOWFLAKE_NOTIFICATION(
    SNOWFLAKE.NOTIFICATION.TEXT_PLAIN('OVERWATCH escalation email test: OVERWATCH_EMAIL now has a default recipient. No action needed.'),
    SNOWFLAKE.NOTIFICATION.INTEGRATION('OVERWATCH_EMAIL'));

-- 6. What the failed emails were about (read-only): the escalations since V164 went in. Their Teams re-post
--    already happened; the email for these is not retried, so read them here.
SELECT EVENT_ID, RULE_ID, SEVERITY, COMPANY, TITLE, RAISED_AT, ESCALATED_AT, STATUS
  FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
 WHERE ESCALATED_AT >= '2026-09-30'
 ORDER BY ESCALATED_AT DESC;

-- 7. Proof over the next day (read-only): no new escalation_email_failed rows after the step-2 time.
SELECT LOGGED_AT, PAGE, ERROR_TYPE, LEFT(ERROR_MESSAGE, 200) AS ERROR_MESSAGE, LEFT(CONTEXT, 200) AS CONTEXT
  FROM DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
 WHERE ERROR_TYPE = 'escalation_email_failed'
 ORDER BY LOGGED_AT DESC
 LIMIT 20;
