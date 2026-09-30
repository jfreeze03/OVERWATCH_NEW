"""Next-Fifty #43 Phase 1: masking / row-access / projection / aggregation policy coverage.

Shapes ``security_sql.data_policy_coverage`` and ``security_sql.masking_environment_parity`` into the Security >
Exposure panel's frames and sentences, and ``ACCOUNT_POLICY_REFS`` (from ``admin_network_policy_coverage``) into
the Access network-policy caption.

- Every number comes from SQL, keyed on fully qualified names. Nothing here sums a per-database column into an
  account total: distinct policies are not additive across databases, so the totals are read from the SQL's own
  account-level columns (repeated on every row).
- Nothing here feeds ``app.logic.security.domain_posture`` or the Decision-queue domain scores (owner default #43):
  the panel is information only, and the environment grouping is not a gap worklist.
- Pure: no Streamlit, no app.data, no clock.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from app.logic.formulas import safe_float

INVENTORY_COLUMNS: tuple[str, ...] = (
    "DATABASE_NAME", "ENVIRONMENT", "MASKED_OBJECTS", "MASKED_COLUMNS", "MASKING_POLICIES", "REFS_NOT_ACTIVE",
)
TOTAL_COLUMNS: tuple[str, ...] = (
    "TOTAL_MASKED_COLUMNS", "TOTAL_MASKED_OBJECTS", "TOTAL_MASKED_DATABASES", "TOTAL_MASKING_POLICIES",
    "MASKING_TAGS", "MASKING_TAG_DATABASES", "TAG_MASKING_POLICIES", "MASKING_REFS_NOT_ACTIVE",
    "ROW_ACCESS_OBJECTS", "ROW_ACCESS_POLICIES", "PROJECTION_OBJECTS", "PROJECTION_POLICIES",
    "AGGREGATION_OBJECTS", "AGGREGATION_POLICIES",
)
TEXT_TOTAL_COLUMNS: tuple[str, ...] = ("NOT_ACTIVE_STATUSES", "OTHER_POLICY_KINDS")
PARITY_COLUMNS: tuple[str, ...] = (
    "DATABASE_FAMILY", "SCHEMA_NAME", "OBJECT_NAME", "PARITY", "DATABASES_MASKED", "FAMILY_DATABASES",
    "COLUMN_SETS", "MASKED_IN", "NO_MASKING_REF_IN",
)
PARITY_TOTAL_COLUMNS: tuple[str, ...] = ("TOTAL_NAMES", "DIFFERING_NAMES")

POLICY_VIEW_UNREADABLE = (
    "Snowflake's policy-reference view (POLICY_REFERENCES) is not readable by this app: it is missing in this "
    "account, or the app's role cannot see it. Masking and row-access coverage cannot be shown here."
)
NO_MASKING = (
    "The policy-reference view lists no masking policy on any column or tag in this account, so there is no "
    "masking inventory. A policy attached in the last 2 hours may not be listed yet."
)
TAG_ONLY_MASKING = (
    "The policy-reference view lists no column-level masking reference; masking here is attached through tags "
    "only (see the tag-based line below)."
)
INVENTORY_NOTE = (
    "One row per database with a masked column. Counts are distinct fully qualified names; a policy used in "
    "several databases counts once in each row, so MASKING_POLICIES does not add up to the account total. "
    "ENVIRONMENT is the part of the database name after its last underscore, shown when two or more databases "
    "with masked columns share the rest of the name."
)
PARITY_LEGEND = (
    "SAME: masked in every database of its family that has masked columns, on the same column names. DIFFERS: "
    "masked in fewer of them, or on different columns. MASKED_IN gives each database with its masked-column "
    "count; NO_MASKING_REF_IN lists family databases that have masked columns but none on this name, and the "
    "table may not exist there. A family is the databases whose names match up to the last underscore (for "
    "example X_PRD and X_DEV). Information only, not a gap list: it does not assume every environment should be "
    "masked like production."
)
ACCOUNT_POLICY_HINT = (
    "The same view lists an account-level network policy, which applies to an admin without a user-level policy "
    "unless they sign in through a security integration that has its own."
)

# Snowflake's documented network-policy precedence (docs "Network policy precedence", re-read 2026-09-30): the most
# specific wins -- a security integration's policy overrides a user's and the account's, and a user's overrides the
# account's. So the user-level policy "pins" an admin except when they sign in through an integration that has its
# own policy; every caption below that credits the user-level policy carries that qualifier.
_INTEGRATION_PRECEDENCE = (
    "A security integration's own network policy takes precedence over both when an admin signs in through that "
    "integration."
)
# The Access caption before #43 (its first sentence verbatim) for the state where the account-level count is not
# known, plus the integration qualifier.
_NETWORK_CAPTION_UNKNOWN = (
    "A user-level policy overrides the account policy and pins an admin to known networks; an admin without one "
    "still falls under the account-level policy if one is set. " + _INTEGRATION_PRECEDENCE
)
_NETWORK_CAPTION_SET = (
    "An account-level network policy is set (the policy-reference view lists it). A user-level policy takes "
    "precedence over it and pins an admin to known networks; an admin without one falls under the account-level "
    "policy. " + _INTEGRATION_PRECEDENCE
)
_NETWORK_CAPTION_NONE = (
    "The policy-reference view lists no account-level network policy (it can lag up to 2 hours); confirm with "
    "SHOW PARAMETERS LIKE 'NETWORK_POLICY' IN ACCOUNT. Without one, an admin with no user-level policy is limited "
    "only by a security integration's own network policy, when signing in through it."
)


@dataclass(frozen=True)
class PolicyCoverage:
    """Account totals from data_policy_coverage (one read of row 0: the totals repeat on every row)."""

    masked_columns: int
    masked_objects: int
    masked_databases: int
    masking_policies: int
    masking_tags: int
    masking_tag_databases: int
    tag_masking_policies: int
    not_active_refs: int
    row_access_objects: int
    row_access_policies: int
    projection_objects: int
    projection_policies: int
    aggregation_objects: int
    aggregation_policies: int
    not_active_statuses: str
    other_policy_kinds: str


def _count(v: object) -> int:
    return int(safe_float(v))


def _text(v: object) -> str:
    if v is None:
        return ""
    try:
        if pd.isna(v):
            return ""
    except (TypeError, ValueError):
        pass
    return str(v).strip()


def _plural(n: int, one: str, many: str) -> str:
    return f"{n:,} {one if n == 1 else many}"


def _or_join(words: list[str]) -> str:
    if len(words) <= 1:
        return "".join(words)
    if len(words) == 2:
        return f"{words[0]} or {words[1]}"
    return f"{', '.join(words[:-1])} or {words[-1]}"


def summarize_policy_coverage(frame: pd.DataFrame | None) -> PolicyCoverage | None:
    """The account totals, or None when the frame is missing, empty, or lacks a total column (never zeros)."""
    if frame is None or frame.empty:
        return None
    if any(c not in frame.columns for c in (*TOTAL_COLUMNS, *TEXT_TOTAL_COLUMNS)):
        return None
    row = frame.iloc[0]
    return PolicyCoverage(
        masked_columns=_count(row["TOTAL_MASKED_COLUMNS"]),
        masked_objects=_count(row["TOTAL_MASKED_OBJECTS"]),
        masked_databases=_count(row["TOTAL_MASKED_DATABASES"]),
        masking_policies=_count(row["TOTAL_MASKING_POLICIES"]),
        masking_tags=_count(row["MASKING_TAGS"]),
        masking_tag_databases=_count(row["MASKING_TAG_DATABASES"]),
        tag_masking_policies=_count(row["TAG_MASKING_POLICIES"]),
        not_active_refs=_count(row["MASKING_REFS_NOT_ACTIVE"]),
        row_access_objects=_count(row["ROW_ACCESS_OBJECTS"]),
        row_access_policies=_count(row["ROW_ACCESS_POLICIES"]),
        projection_objects=_count(row["PROJECTION_OBJECTS"]),
        projection_policies=_count(row["PROJECTION_POLICIES"]),
        aggregation_objects=_count(row["AGGREGATION_OBJECTS"]),
        aggregation_policies=_count(row["AGGREGATION_POLICIES"]),
        not_active_statuses=_text(row["NOT_ACTIVE_STATUSES"]),
        other_policy_kinds=_text(row["OTHER_POLICY_KINDS"]),
    )


def database_inventory(frame: pd.DataFrame | None) -> pd.DataFrame:
    """The per-database rows (the NULL-database totals sentinel dropped), INVENTORY_COLUMNS only, in SQL order."""
    if frame is None or frame.empty or "DATABASE_NAME" not in frame.columns:
        return pd.DataFrame(columns=list(INVENTORY_COLUMNS))
    rows = frame[frame["DATABASE_NAME"].notna()]
    cols = [c for c in INVENTORY_COLUMNS if c in frame.columns]
    return rows[cols].reset_index(drop=True)


def tag_masking_sentence(c: PolicyCoverage) -> str:
    tags = c.masking_tags
    if tags <= 0:
        return "Tag-based masking: none. The policy-reference view lists no masking policy attached to a tag."
    return (f"Tag-based masking: {_plural(tags, 'tag', 'tags')} in "
            f"{_plural(c.masking_tag_databases, 'database', 'databases')} "
            f"{'carries' if tags == 1 else 'carry'} a masking policy "
            f"({_plural(c.tag_masking_policies, 'distinct policy', 'distinct policies')}). "
            "This counts the tag assignments; the columns each tag reaches are not listed here.")


def row_policy_sentences(c: PolicyCoverage) -> tuple[str, ...]:
    """One sentence per row-level policy kind in use, then ONE sentence naming every kind not in use (the explicit
    'no row-access policy' statement; it always renders when any kind is unused)."""
    out: list[str] = []
    unused: list[str] = []
    for label, attr in (("row-access", "row_access"), ("projection", "projection"), ("aggregation", "aggregation")):
        objs = int(getattr(c, f"{attr}_objects"))
        pols = int(getattr(c, f"{attr}_policies"))
        if objs > 0:
            out.append(f"{label.capitalize()} policies are attached to "
                       f"{_plural(objs, 'table or view', 'tables or views')} "
                       f"({_plural(pols, 'distinct policy', 'distinct policies')}).")
        else:
            unused.append(label)
    if unused:
        out.append(f"No {_or_join(unused)} policy is in use: the policy-reference view lists none attached to any "
                   "table or view.")
    return tuple(out)


def not_active_sentence(c: PolicyCoverage) -> str:
    n = c.not_active_refs
    if n <= 0:
        return ""
    return (f"{_plural(n, 'masking reference reports', 'masking references report')} a status other than ACTIVE: "
            f"{c.not_active_statuses or 'status not reported'}.")


def other_kinds_sentence(c: PolicyCoverage) -> str:
    if not c.other_policy_kinds:
        return ""
    return f"Other policy kinds in the view, not covered here: {c.other_policy_kinds}."


def parity_view(frame: pd.DataFrame | None) -> pd.DataFrame:
    """The displayed environment-grouping frame (and so the CSV): PARITY_COLUMNS only, totals dropped."""
    if frame is None:
        return pd.DataFrame(columns=list(PARITY_COLUMNS))
    cols = [c for c in PARITY_COLUMNS if c in frame.columns]
    return frame[cols].reset_index(drop=True)


def parity_counts(frame: pd.DataFrame | None) -> tuple[int, int]:
    """(TOTAL_NAMES, DIFFERING_NAMES): window totals taken in SQL before the row cap, read from row 0."""
    if frame is None or frame.empty:
        return 0, 0
    row = frame.iloc[0]
    return (_count(row["TOTAL_NAMES"]) if "TOTAL_NAMES" in frame.columns else 0,
            _count(row["DIFFERING_NAMES"]) if "DIFFERING_NAMES" in frame.columns else 0)


def parity_summary_sentence(total: int, differing: int) -> str:
    return (f"{_plural(total, 'masked table or view name belongs', 'masked table or view names belong')} to a "
            f"database family with two or more masked databases; {differing:,} "
            f"{'is' if differing == 1 else 'are'} not masked the same way in every one of them.")


def account_network_policy_refs(frame: pd.DataFrame | None) -> int | None:
    """ACCOUNT_POLICY_REFS from admin_network_policy_coverage, or None when it is not known (no frame, no column,
    or every value NULL)."""
    if frame is None or frame.empty or "ACCOUNT_POLICY_REFS" not in frame.columns:
        return None
    values = pd.to_numeric(frame["ACCOUNT_POLICY_REFS"], errors="coerce")
    if values.isna().all():
        return None
    return int(values.max())


def network_policy_caption(refs: int | None) -> str:
    """The Access caption: a fact line about the account-level network policy, never a coverage verdict."""
    if refs is None:
        return _NETWORK_CAPTION_UNKNOWN
    if refs > 0:
        return _NETWORK_CAPTION_SET
    return _NETWORK_CAPTION_NONE
