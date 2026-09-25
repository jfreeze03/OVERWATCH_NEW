"""Cloud-services driver intelligence — classify a compile-heavy / metadata query
family and decide whether a warehouse resize could plausibly help.

Phase 0 (app-only, no migration): works on the columns the existing compile-heavy
family builders already return (SAMPLE_TEXT, COMPILE_PCT, AVG_TOTAL_S, RUNS, and
QUERY_TYPE/WAREHOUSE_NAME when present). It turns "cloud services spiked" into
"WHICH behavior spiked, WHO owns the fix, and is resizing even relevant".

Two deliberate Phase-0 boundaries, both documented so a later phase can lift them:

* Classification is multi-signal but leans on the query-text SIGNATURE (SAMPLE_TEXT)
  plus the compile-vs-total shape. The stronger ``IS_CLIENT_GENERATED_STATEMENT``
  flag, per-session APP-INIT correlation, and connection-churn detection need columns
  that are not in the mart/extract today (Phase 1 adds them), so the SYSTEM GENERATED
  and HIGH FREQUENCY classes are approximated here, not fully realized.
* The resize verdict is two-state (RESIZE NOT INDICATED / INSUFFICIENT EVIDENCE). The
  third state, RESIZE MAY HELP, requires spill/queue evidence that this frame does not
  carry; asserting it without that evidence would be a false positive, so Phase 0 never
  claims it. RESIZE NOT INDICATED — the genuinely new, high-value verdict for
  compile/metadata-dominated chatter — is fully supported.

Every cloud-services credit this feature can attribute is GROSS USAGE, never billable:
the ~10% free-allotment rebate is an account+day (UTC) computation against a shared pool
and is not decomposable to a warehouse or a query family. The UI must say so; this module
never dollarizes a family.

Pure pandas — no Streamlit, no Snowflake. Reuses ``query_advisor.COMPILE_FRACTION`` so the
"compile-dominated" threshold matches the per-query advisor.
"""

from __future__ import annotations

from collections import Counter

import pandas as pd

from app.config import APP_SIS_QUERY_TAG_FRAGMENT
from app.logic.query_advisor import COMPILE_FRACTION

# --- driver classes (a Phase-0 subset of the 12-class taxonomy) --------------
SYSTEM_GENERATED = "System generated"
GOVERNANCE_DISCOVERY = "Governance / Cortex discovery"
JDBC_ODBC_DISCOVERY = "JDBC / ODBC discovery"
INFORMATION_SCHEMA = "INFORMATION_SCHEMA discovery"
STAGE_FILE = "Stage / file metadata"
COMPILE_HEAVY = "Compile heavy"
METADATA_CHATTER = "Metadata chatter"
NORMAL = "Normal workload"
UNKNOWN = "Unknown"

DRIVER_CLASSES = (
    SYSTEM_GENERATED, GOVERNANCE_DISCOVERY, JDBC_ODBC_DISCOVERY, INFORMATION_SCHEMA,
    STAGE_FILE, COMPILE_HEAVY, METADATA_CHATTER, NORMAL, UNKNOWN,
)

# --- resize verdicts ---------------------------------------------------------
RESIZE_NOT_INDICATED = "Resize not indicated"
RESIZE_INSUFFICIENT = "Insufficient evidence"

# --- remediation owner per class --------------------------------------------
_OWNER = {
    SYSTEM_GENERATED: "Platform / Snowsight (usually benign)",
    GOVERNANCE_DISCOVERY: "Security / Governance",
    JDBC_ODBC_DISCOVERY: "BI / IDE tool owner",
    INFORMATION_SCHEMA: "BI / IDE or Data Engineering",
    STAGE_FILE: "Data Engineering",
    COMPILE_HEAVY: "Application / SQL author",
    METADATA_CHATTER: "Application / BI tool owner",
    NORMAL: "",
    UNKNOWN: "Investigate (attribution needed)",
}

# The compile-dominated classes: their cost lives in the cloud-services/compile
# layer, so a virtual-warehouse resize (which scales only compute credits) cannot
# reduce them. Everything here => RESIZE NOT INDICATED.
_COMPILE_LAYER_CLASSES = frozenset({
    SYSTEM_GENERATED, GOVERNANCE_DISCOVERY, JDBC_ODBC_DISCOVERY,
    INFORMATION_SCHEMA, STAGE_FILE, METADATA_CHATTER, COMPILE_HEAVY,
})

# A family with fewer runs than this is a weak basis for a confident verdict
# (the builders already floor at 5/20; this only softens confidence, never excludes).
_THIN_RUNS = 20
# Compile share (0-100) at/above which the compile phase dominates. Matches
# query_advisor.COMPILE_FRACTION (a 0-1 fraction) so the two agree.
_COMPILE_DOMINANT_PCT = COMPILE_FRACTION * 100.0
# A compile-dominated family whose whole statement is this short is metadata-shaped
# (near-zero warehouse execution), not a heavy plan on a warehouse.
_METADATA_MAX_TOTAL_S = 2.0
# Output columns this module stamps onto a family frame.
DRIVER_COLS = ("DRIVER_CLASS", "DRIVER_CONFIDENCE", "RESIZE_VERDICT", "REMEDIATION_OWNER")


def _text(row: pd.Series | dict, col: str) -> str:
    val = row.get(col) if isinstance(row, dict) else row.get(col, None)
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return ""
    return str(val).upper()


def _num(row: pd.Series | dict, col: str) -> float:
    val = row.get(col) if isinstance(row, dict) else row.get(col, None)
    try:
        f = float(val)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if pd.isna(f) else f


def classify_row(row: pd.Series | dict) -> tuple[str, str]:
    """Return (driver_class, confidence) for one family row.

    Confidence is HIGH when an unambiguous text signature matched, MEDIUM for a
    shape-only call, LOW when the sample is thin or the text is missing/truncated.
    """
    text = _text(row, "SAMPLE_TEXT") or _text(row, "QUERY_TEXT")
    qtype = _text(row, "QUERY_TYPE")
    runs = _num(row, "RUNS")
    compile_pct = _num(row, "COMPILE_PCT")
    total_s = _num(row, "AVG_TOTAL_S")
    thin = 0 < runs < _THIN_RUNS

    def conf(base: str) -> str:
        # a text-signature HIGH decays to MEDIUM on a thin sample; a shape MEDIUM to LOW
        if thin:
            return {"HIGH": "MEDIUM", "MEDIUM": "LOW"}.get(base, base)
        return base

    # --- text-signature classes (most specific first) ------------------------
    _tag = _text(row, "QUERY_TAG")            # upper-cased; SiS stamps its app tag on every app statement
    if ("SYSTEM$FBE" in text or "EXECUTE STREAMLIT" in text or _tag.startswith("OVERWATCH")
            or APP_SIS_QUERY_TAG_FRAGMENT.upper() in _tag):
        return SYSTEM_GENERATED, conf("HIGH")
    if ("SYSTEM$GET_CLASSIFICATION" in text or "SYSTEM$CLASSIFY" in text
            or "SYSTEM$CORTEX_MODEL_ACCESSIBLE" in text or "SHOW CORTEX" in text):
        return GOVERNANCE_DISCOVERY, conf("HIGH")
    if ("JDBC:DATABASEMETADATA" in text or "/* JDBC" in text or "/* ODBC" in text
            or "GETPRIMARYKEYS" in text or "GETIMPORTEDKEYS" in text
            or "GETTABLES" in text or "GETCOLUMNS" in text):
        return JDBC_ODBC_DISCOVERY, conf("HIGH")
    if "INFORMATION_SCHEMA" in text:
        return INFORMATION_SCHEMA, conf("HIGH")
    if ("FROM @" in text or "LIST @" in text or text.startswith(("GET ", "PUT "))
            or qtype in ("GET_FILES", "LIST_FILES", "PUT_FILES")):
        return STAGE_FILE, conf("HIGH")
    # a system function we do not have a specific rule for: still platform-issued
    if "SYSTEM$" in text:
        return SYSTEM_GENERATED, conf("MEDIUM")

    # --- shape-based classes (no decisive text signature) --------------------
    if compile_pct >= _COMPILE_DOMINANT_PCT:
        # compile phase dominates. Sub-second total => metadata-only (no real
        # warehouse execution); otherwise a genuinely compile-heavy plan.
        if 0 < total_s <= _METADATA_MAX_TOTAL_S or _text(row, "WAREHOUSE_NAME") in ("", "NONE", "NULL"):
            return METADATA_CHATTER, conf("MEDIUM")
        return COMPILE_HEAVY, conf("MEDIUM")
    if compile_pct > 0 and total_s > 0:
        return NORMAL, conf("MEDIUM")
    return UNKNOWN, "LOW"


def resize_verdict(driver_class: str, compile_pct: float, total_s: float) -> str:
    """Two-state resize relevance (Phase 0). RESIZE NOT INDICATED for anything whose
    cost lives in the compile/cloud-services layer; INSUFFICIENT EVIDENCE otherwise
    (this frame has no spill/queue columns, so RESIZE MAY HELP is never asserted)."""
    if driver_class in _COMPILE_LAYER_CLASSES or compile_pct >= _COMPILE_DOMINANT_PCT:
        return RESIZE_NOT_INDICATED
    return RESIZE_INSUFFICIENT


def remediation_owner(driver_class: str) -> str:
    return _OWNER.get(driver_class, "")


def classify_families(df: pd.DataFrame) -> pd.DataFrame:
    """Return a copy of ``df`` with DRIVER_CLASS, DRIVER_CONFIDENCE, RESIZE_VERDICT
    and REMEDIATION_OWNER stamped on. Column-tolerant: a frame missing SAMPLE_TEXT /
    COMPILE_PCT / AVG_TOTAL_S still classifies (as UNKNOWN where it must). Never
    mutates the caller's frame; an empty frame comes back with the columns present so
    downstream renders/tests see a stable schema."""
    out = df.copy()
    if out.empty:
        for col in DRIVER_COLS:
            out[col] = pd.Series(dtype="object")
        return out

    classes, confs, verdicts, owners = [], [], [], []
    for _, row in out.iterrows():
        cls, confidence = classify_row(row)
        classes.append(cls)
        confs.append(confidence)
        verdicts.append(resize_verdict(cls, _num(row, "COMPILE_PCT"), _num(row, "AVG_TOTAL_S")))
        owners.append(remediation_owner(cls))
    out["DRIVER_CLASS"] = classes
    out["DRIVER_CONFIDENCE"] = confs
    out["RESIZE_VERDICT"] = verdicts
    out["REMEDIATION_OWNER"] = owners
    return out


def driver_summary(df: pd.DataFrame) -> dict:
    """One-line rollup for a caption: how many families landed in each class, how many
    are RESIZE NOT INDICATED, and the owner that recurs most. ``df`` is assumed to have
    already passed through ``classify_families``."""
    if df.empty or "DRIVER_CLASS" not in df.columns:
        return {"total": 0, "not_indicated": 0, "by_class": {}, "top_owner": ""}
    classes = [str(c) for c in df["DRIVER_CLASS"].tolist()]
    owners = [str(o) for o in df.get("REMEDIATION_OWNER", pd.Series(dtype="object")).tolist() if o]
    not_indicated = int((df["RESIZE_VERDICT"].astype(str) == RESIZE_NOT_INDICATED).sum())
    top_owner = Counter(owners).most_common(1)[0][0] if owners else ""
    return {
        "total": len(classes),
        "not_indicated": not_indicated,
        "by_class": dict(Counter(classes)),
        "top_owner": top_owner,
    }
