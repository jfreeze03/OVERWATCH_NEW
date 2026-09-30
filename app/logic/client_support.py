"""Client driver support status for Security > Clients (Next-Fifty #34). Pure module.

Two reads meet here, joined in pandas:

* ``security_sql.client_drivers``: what connected in the window (ACCOUNT_USAGE.SESSIONS, one row per
  DRIVER x VERSION x PROGRAM, with the in-account STATUS: BEHIND / CURRENT / NO VERSION / SNOWFLAKE-RUN);
* ``security_sql.client_version_info``: Snowflake's own per-driver floors from
  SYSTEM$CLIENT_VERSION_INFO(): the minimum supported version, the version it lists as nearing end of
  support, and the recommended version.

The floors are read from the function every time and are never written into this module: they move
whenever Snowflake updates its support policy. The function states a version floor only, so nothing here
says an unsupported driver "will stop working".

Who upgrades: a short allow-list (``SNOWFLAKE_RUN_*``) switches a row to Snowflake-run, meaning Snowflake's
own web app / Snowsight backend and SnowServices ingress; Snowflake upgrades those and there is nothing to
install on the customer side. Everything else is "Yours", which errs toward showing a row as actionable.
``security_sql.client_drivers`` builds its SQL twin of the rule from the same constants, so the in-account
STATUS and WHO_UPGRADES cannot drift apart.

No app.data import and no clock read: every input is passed in.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

import pandas as pd

# ---------------------------------------------------------------------------
# vocabulary
# ---------------------------------------------------------------------------

UNSUPPORTED = "UNSUPPORTED"
NEARING_EOS = "NEARING_EOS"
BELOW_RECOMMENDED = "BELOW_RECOMMENDED"
OK = "OK"
NO_VERSION = "NO_VERSION"
NOT_LISTED = "NOT_LISTED"
UNAVAILABLE = "UNAVAILABLE"

# What the SUPPORT_STATUS column shows (the panel help defines each of these words).
STATUS_LABELS = {
    UNSUPPORTED: "UNSUPPORTED",
    NEARING_EOS: "NEARING END OF SUPPORT",
    BELOW_RECOMMENDED: "BELOW RECOMMENDED",
    OK: "OK",
    NO_VERSION: "NO VERSION",
    NOT_LISTED: "NOT LISTED",
    UNAVAILABLE: "unavailable",
}
# Worst first; the table sorts on this, then yours-before-Snowflake-run, then sessions.
STATUS_RANK = {UNSUPPORTED: 0, NEARING_EOS: 1, BELOW_RECOMMENDED: 2, OK: 3, NO_VERSION: 4,
               NOT_LISTED: 5, UNAVAILABLE: 6}

WHO_YOURS = "Yours"
WHO_SNOWFLAKE = "Snowflake-run"
SNOWFLAKE_RUN_SUFFIX = f" ({WHO_SNOWFLAKE})"

# The Snowflake-run allow-list. A PROGRAM prefix (case-insensitive; no LIKE wildcards allowed, because the
# SQL twin uses ILIKE '<prefix>%') or an exact DRIVER (case-insensitive).
SNOWFLAKE_RUN_PROGRAM_PREFIXES: tuple[str, ...] = ("Snowflake Web App",)
SNOWFLAKE_RUN_DRIVERS: tuple[str, ...] = ("Snowsight", "SnowServices Ingress")

# The DRIVER label client_drivers gives a NULL or blank CLIENT_APPLICATION_ID.
NO_CLIENT_ID = "(no client id)"

# Friendly client names by self-reported PROGRAM prefix (case-insensitive). The rest are named by
# SAMPLE_USERS already (e.g. the Trexis AWS Glue jobs run program 'PythonConnector').
CLIENT_LABELS: tuple[tuple[str, str], ...] = (
    ("INFA_DI_Cloud", "Informatica"),
    ("INFA_IICS", "Informatica"),
    ("MashupEngineGateway", "Power BI gateway"),
    ("[ADBC]MashupEngine", "Power BI (ADBC)"),
    ("com.amazonaws.services.glue", "AWS Glue"),
    ("terraform-provider-snowflake", "Terraform"),
    ("posit_workbench_rstudio", "Posit"),
    # a Snowflake tool that bundles the JavaScript driver: the fix is a CLI update on the user's machine
    ("cortex_code_", "Cortex Code CLI"),
)

# Display names for a driver id where the raw id reads badly in a sentence.
_DRIVER_DISPLAY = {"PYTHONCONNECTOR": "Python connector"}

SUPPORT_SORT_LABEL = "support status, yours before Snowflake-run, then sessions"
INVENTORY_SORT_LABEL = "driver, newest version first"   # the builder's own ORDER BY

# Columns the Clients table shows, in order (internal helpers such as UPGRADED_BY / SUPPORT_CODE stay out).
DISPLAY_COLUMNS = (
    "DRIVER", "VERSION", "CLIENT", "PROGRAM", "SUPPORT_STATUS", "WHO_UPGRADES", "MIN_SUPPORTED",
    "RECOMMENDED", "STATUS", "NEWEST_IN_ACCOUNT", "USERS", "SESSIONS", "FIRST_SEEN", "LAST_SEEN",
    "SAMPLE_USERS",
)


# ---------------------------------------------------------------------------
# small parsers
# ---------------------------------------------------------------------------

def _is_blank(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    try:
        if pd.isna(value):          # pd.NA / NaT
            return True
    except (TypeError, ValueError):  # array-likes are never a scalar blank
        return False
    return not str(value).strip()


def _text(value: object) -> str:
    return "" if _is_blank(value) else str(value).strip()


_VERSION_RE = re.compile(r"[vV]?(\d[\d.]*)")


def version_key(version: object) -> tuple[int, ...] | None:
    """'3.13.22' -> (3, 13, 22, 0). None for '?', blank, None/NaN, or text that does not start with a digit.

    Pads to four segments like the SQL VKEY (so 2.0 == 2.0.0) and keeps any fifth segment; an empty
    segment ('3..1') reads 0 as it does in the SQL. Trailing text ('3.0.0-beta') is ignored."""
    text = _text(version)
    if not text or text == "?":
        return None
    m = _VERSION_RE.match(text)
    if not m:
        return None
    segs = [int(p) if p else 0 for p in m.group(1).rstrip(".").split(".")]
    while len(segs) < 4:
        segs.append(0)
    return tuple(segs)


def _lt(a: tuple[int, ...], b: tuple[int, ...]) -> bool:
    width = max(len(a), len(b))
    return a + (0,) * (width - len(a)) < b + (0,) * (width - len(b))


def support_status(version: object, min_supported: object, nearing_eos: object,
                   recommended: object) -> str:
    """One driver version against Snowflake's floors for that driver.

    NOT_LISTED when the function gave no usable floor at all (no entry, or an entry missing every version
    key); NO_VERSION when the client reported no version (never compared, never newest); UNSUPPORTED below
    the minimum supported version; NEARING_EOS at or above it but below the version listed as nearing end
    of support; BELOW_RECOMMENDED supported but older than the recommended version; else OK."""
    mk, nk, rk = version_key(min_supported), version_key(nearing_eos), version_key(recommended)
    if mk is None and nk is None and rk is None:
        return NOT_LISTED
    vk = version_key(version)
    if vk is None:
        return NO_VERSION
    if mk is not None and _lt(vk, mk):
        return UNSUPPORTED
    if nk is not None and _lt(vk, nk):
        return NEARING_EOS
    if rk is not None and _lt(vk, rk):
        return BELOW_RECOMMENDED
    return OK


def who_upgrades(driver: object, program: object) -> str:
    """'Snowflake-run' for Snowflake's own services (PROGRAM starting 'Snowflake Web App', DRIVER
    'Snowsight' or 'SnowServices Ingress'); 'Yours' for everything else."""
    prog = str(program or "").upper()
    if any(prog.startswith(p.upper()) for p in SNOWFLAKE_RUN_PROGRAM_PREFIXES):
        return WHO_SNOWFLAKE
    drv = str(driver or "").strip().upper()
    if drv in {d.upper() for d in SNOWFLAKE_RUN_DRIVERS}:
        return WHO_SNOWFLAKE
    return WHO_YOURS


def client_label(program: object) -> str:
    """Friendly client name from the self-reported PROGRAM prefix, '' when there is none."""
    prog = str(program or "").strip().upper()
    for prefix, label in CLIENT_LABELS:
        if prog.startswith(prefix.upper()):
            return label
    return ""


def driver_display(driver: object) -> str:
    text = _text(driver)
    return _DRIVER_DISPLAY.get(text.upper(), text)


# ---------------------------------------------------------------------------
# SYSTEM$CLIENT_VERSION_INFO() entries
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ClientFloor:
    """One SYSTEM$CLIENT_VERSION_INFO() entry, normalised. Blank strings for missing values."""

    client_id: str
    client_app_id: str
    min_supported: str
    nearing_eos: str
    recommended: str


def _norm_key(key: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


# Accepted spellings per field, compared case- and punctuation-insensitively, so the documented JSON keys
# (clientAppId, minimumSupportedVersion, ...) and the builder's column names (CLIENT_APP_ID,
# MIN_SUPPORTED_VERSION, ...) both land in the same field.
_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "client_app_id": ("clientappid", "clientapplicationid"),
    "client_id": ("clientid",),
    "min_supported": ("minimumsupportedversion", "minsupportedversion"),
    "nearing_eos": ("minimumnearingendofsupportversion", "nearingendofsupportversion", "nearingeosversion"),
    "recommended": ("recommendedversion",),
}


def _normalize_pairs(pairs: Iterable[tuple[object, object]]) -> ClientFloor | None:
    found: dict[str, str] = {}
    for key, value in pairs:
        nk = _norm_key(key)
        for field, aliases in _FIELD_ALIASES.items():
            if field not in found and nk in aliases and not _is_blank(value):
                found[field] = _text(value)
    if not found.get("client_app_id") and not found.get("client_id"):
        return None
    return ClientFloor(client_id=found.get("client_id", ""), client_app_id=found.get("client_app_id", ""),
                       min_supported=found.get("min_supported", ""), nearing_eos=found.get("nearing_eos", ""),
                       recommended=found.get("recommended", ""))


def normalize_entry(entry: Mapping) -> ClientFloor | None:
    """A raw JSON entry or a builder row -> ClientFloor. None when it names no client at all.

    Tolerates key-spelling variants (clientAppId / clientId / CLIENT_APP_ID ...) and missing keys: a
    missing version key is blank, and an entry with no version key at all reads NOT_LISTED downstream.
    The first non-blank value in the mapping's own order wins for each field."""
    return _normalize_pairs(entry.items())


def _entries(doc: object) -> list:
    if isinstance(doc, list):
        return doc
    if isinstance(doc, Mapping):
        for value in doc.values():      # tolerate a wrapper object around the documented array
            if isinstance(value, list):
                return value
        return [doc]
    return []


def _json_object(raw: object) -> Mapping | None:
    if _is_blank(raw):
        return None
    try:
        parsed = json.loads(str(raw))
    except ValueError:
        return None
    return parsed if isinstance(parsed, Mapping) else None


def parse_client_version_info(raw: object) -> list[ClientFloor]:
    """The function's raw JSON text (or an already-decoded list) -> one ClientFloor per client entry.
    Unparseable text and non-object entries yield nothing, never an exception."""
    doc = raw
    if isinstance(raw, (str, bytes)):
        try:
            doc = json.loads(raw)
        except ValueError:
            return []
    out: list[ClientFloor] = []
    for item in _entries(doc):
        if isinstance(item, Mapping):
            floor = normalize_entry(item)
            if floor is not None:
                out.append(floor)
    return out


def floors_from_frame(df: pd.DataFrame | None) -> list[ClientFloor]:
    """The client_version_info builder's rows -> ClientFloors. The flattened columns win; RAW_ENTRY
    (the entry's own JSON) fills any field the flatten missed because Snowflake spelled a key differently."""
    if df is None or df.empty:
        return []
    out: list[ClientFloor] = []
    for row in df.to_dict("records"):
        pairs: list[tuple[object, object]] = [(k, v) for k, v in row.items() if str(k).upper() != "RAW_ENTRY"]
        raw = _json_object(row.get("RAW_ENTRY"))
        if raw is not None:
            pairs.extend(raw.items())
        floor = _normalize_pairs(pairs)
        if floor is not None:
            out.append(floor)
    return out


def _has_minimum(floor: ClientFloor) -> bool:
    return version_key(floor.min_supported) is not None


def floor_index(floors: Iterable[ClientFloor]) -> dict[str, ClientFloor]:
    """Upper-cased driver id -> floor. A clientAppId match beats a clientId match (as in the W4c probe).

    Two entries for the same id: the one that lists a minimum supported version wins, else the first. A
    second, minimum-less entry for a driver (the W4c probe hints the live function has one for Go) must
    never erase that driver's minimum and turn an UNSUPPORTED version into a milder verdict."""
    by_id: dict[str, ClientFloor] = {}
    by_app: dict[str, ClientFloor] = {}

    def _keep(index: dict[str, ClientFloor], key: str, floor: ClientFloor) -> None:
        current = index.get(key)
        if current is None or (not _has_minimum(current) and _has_minimum(floor)):
            index[key] = floor

    for f in floors:
        if f.client_id:
            _keep(by_id, f.client_id.upper(), f)
        if f.client_app_id:
            _keep(by_app, f.client_app_id.upper(), f)
    return {**by_id, **by_app}


def unavailable_reason(error: object, error_kind: object) -> str:
    """One short line for the 'could not be read' caption: the error's first line, else its kind."""
    first = _text(error).splitlines()[0] if _text(error) else ""
    if first:
        return first[:160]
    kind = _text(error_kind)
    return {"unknown_function": "the function is not available to the app's owner role",
            "timeout": "the read timed out"}.get(kind, kind or "unknown error")


def read_floors(ok: bool, df: pd.DataFrame | None, error: object = "",
                error_kind: object = "") -> tuple[list[ClientFloor] | None, str]:
    """The SYSTEM$ read's outcome -> (floors, reason). floors is None whenever no support verdict can be
    given: the read failed (any error_kind), it returned nothing parseable, or no entry lists a usable
    minimum supported version. A failed read is never rendered as clean.

    The last case is key drift: GET_PATH on a key Snowflake has renamed returns NULL, not an error, so the
    read still succeeds with one row per client and every version would read NOT LISTED (or, with only the
    minimum key lost, a milder verdict) under a green '0 unsupported'. Keyed on the minimum because the
    minimum is what the UNSUPPORTED verdict needs."""
    if not ok:
        return None, unavailable_reason(error, error_kind)
    floors = floors_from_frame(df)
    if not floors:
        return None, "the function returned no client entries that could be parsed"
    if not any(_has_minimum(f) for f in floors):
        return None, ("the function's entries list no minimum supported version; its key names may have "
                      "changed")
    return floors, ""


# ---------------------------------------------------------------------------
# the join, counts and wording
# ---------------------------------------------------------------------------

def annotate_support(drivers: pd.DataFrame, floors: list[ClientFloor] | None) -> pd.DataFrame:
    """client_drivers' frame + CLIENT, WHO_UPGRADES, SUPPORT_CODE, SUPPORT_STATUS, MIN_SUPPORTED, RECOMMENDED.

    floors=None (the SYSTEM$ read failed) marks every row UNAVAILABLE ('unavailable'), with no floors.
    A Snowflake-run row keeps its true verdict but reads e.g. 'UNSUPPORTED (Snowflake-run)', which the
    table leaves neutral: there is nothing for the customer to install."""
    df = drivers.copy()
    drv = df["DRIVER"] if "DRIVER" in df.columns else pd.Series([""] * len(df), index=df.index)
    ver = df["VERSION"] if "VERSION" in df.columns else pd.Series([""] * len(df), index=df.index)
    prog = df["PROGRAM"] if "PROGRAM" in df.columns else pd.Series([""] * len(df), index=df.index)
    df["CLIENT"] = [client_label(p) or None for p in prog]
    df["WHO_UPGRADES"] = [who_upgrades(d, p) for d, p in zip(drv, prog, strict=True)]
    index = floor_index(floors) if floors is not None else {}
    codes: list[str] = []
    mins: list[str | None] = []
    recs: list[str | None] = []
    for d, v in zip(drv, ver, strict=True):
        if floors is None:
            codes.append(UNAVAILABLE)
            mins.append(None)
            recs.append(None)
            continue
        f = index.get(_text(d).upper())
        if f is None:
            codes.append(NOT_LISTED)
            mins.append(None)
            recs.append(None)
            continue
        codes.append(support_status(v, f.min_supported, f.nearing_eos, f.recommended))
        mins.append(f.min_supported or None)
        recs.append(f.recommended or None)
    df["SUPPORT_CODE"] = codes
    df["SUPPORT_STATUS"] = [
        STATUS_LABELS[c] + (SNOWFLAKE_RUN_SUFFIX if w == WHO_SNOWFLAKE and c != UNAVAILABLE else "")
        for c, w in zip(codes, df["WHO_UPGRADES"], strict=True)]
    df["MIN_SUPPORTED"] = mins
    df["RECOMMENDED"] = recs
    return df


def sort_by_support(df: pd.DataFrame) -> pd.DataFrame:
    """Support-status rank (worst first), then yours before Snowflake-run, then sessions desc."""
    if df.empty:
        return df.reset_index(drop=True)
    keyed = df.assign(
        _rank=[STATUS_RANK.get(str(c), len(STATUS_RANK)) for c in df["SUPPORT_CODE"]],
        _who=[0 if w == WHO_YOURS else 1 for w in df["WHO_UPGRADES"]],
        _sess=pd.to_numeric(df["SESSIONS"], errors="coerce").fillna(0) if "SESSIONS" in df.columns else 0,
    )
    keyed = keyed.sort_values(["_rank", "_who", "_sess"], ascending=[True, True, False], kind="stable")
    return keyed.drop(columns=["_rank", "_who", "_sess"]).reset_index(drop=True)


def display_frame(df: pd.DataFrame) -> pd.DataFrame:
    return df[[c for c in DISPLAY_COLUMNS if c in df.columns]]


def _identified(df: pd.DataFrame) -> pd.DataFrame:
    """Rows with a real client id ('(no client id)' is not a driver family)."""
    return df[df["DRIVER"].astype(str) != NO_CLIENT_ID]


def driver_family_count(df: pd.DataFrame) -> int:
    return int(_identified(df)["DRIVER"].nunique())


def driver_version_count(df: pd.DataFrame) -> int:
    """Distinct DRIVER x VERSION (the builder's rows are DRIVER x VERSION x PROGRAM)."""
    return len(_identified(df)[["DRIVER", "VERSION"]].drop_duplicates())


def behind_count(df: pd.DataFrame) -> int:
    """Driver versions whose in-account STATUS is BEHIND, at DRIVER x VERSION grain. The builder gives
    Snowflake-run rows STATUS 'SNOWFLAKE-RUN', so they never count here."""
    behind = df[df["STATUS"].astype(str) == "BEHIND"]
    return len(behind[["DRIVER", "VERSION"]].drop_duplicates())


def no_client_id_sessions(df: pd.DataFrame) -> int:
    rows = df[df["DRIVER"].astype(str) == NO_CLIENT_ID]
    return int(pd.to_numeric(rows.get("SESSIONS"), errors="coerce").fillna(0).sum()) if not rows.empty else 0


def _driver_versions(df: pd.DataFrame) -> pd.DataFrame:
    """One row per DRIVER x VERSION: its support code (the same on every PROGRAM row), whether any of its
    rows is yours, its total sessions, and whether its floor lists no usable minimum (NO_MIN)."""
    rows = _identified(df)
    if rows.empty:
        return pd.DataFrame(columns=["DRIVER", "VERSION", "SUPPORT_CODE", "YOURS", "SESSIONS", "NO_MIN"])
    sess = pd.to_numeric(rows["SESSIONS"], errors="coerce").fillna(0) if "SESSIONS" in rows.columns else 0
    tmp = rows.assign(_yours=rows["WHO_UPGRADES"] == WHO_YOURS, _sess=sess,
                      _nomin=[version_key(m) is None for m in rows["MIN_SUPPORTED"]])
    return (tmp.groupby(["DRIVER", "VERSION"], sort=False, dropna=False)
            .agg(SUPPORT_CODE=("SUPPORT_CODE", "first"), YOURS=("_yours", "any"), SESSIONS=("_sess", "sum"),
                 NO_MIN=("_nomin", "all"))
            .reset_index())


# Verdicts support_status can reach without a minimum (the entry lists only the other keys). A version
# with one of these and no minimum was never compared with a minimum: it is 'not checked', not clean.
_VERDICTS_WITHOUT_MINIMUM = (NEARING_EOS, BELOW_RECOMMENDED, OK)


def _versioned(dv: pd.DataFrame) -> pd.Series:
    return pd.Series([version_key(v) is not None for v in dv["VERSION"]], index=dv.index, dtype=bool)


def _not_checked(dv: pd.DataFrame) -> pd.Series:
    """Your driver versions that could not be compared with a minimum: NOT LISTED (no floor for the
    driver), or a verdict formed from an entry that lists no minimum. A row with no version ('?') is not
    counted here: nothing could compare it, and the table already says NO VERSION / NOT LISTED."""
    code = dv["SUPPORT_CODE"]
    no_min = dv["NO_MIN"].astype(bool) & code.isin(_VERDICTS_WITHOUT_MINIMUM)
    return ((code == NOT_LISTED) | no_min) & dv["YOURS"].astype(bool) & _versioned(dv)


def support_counts(df: pd.DataFrame) -> dict[str, int]:
    """KPI counts at DRIVER x VERSION grain from an annotate_support frame (read succeeded).

    A version seen under both a customer program and a Snowflake-run program counts as yours (actionable).
    Nearing end of support and below recommended count yours only; Snowflake-run versions are no action.

    checked_yours / not_checked_yours split your versions that have a version number into those compared
    with a minimum and those that could not be (see _not_checked); not_listed_yours is the NOT LISTED part
    of the latter (no floor at all, so no verdict of any kind). The page qualifies its KPIs and caption
    with these instead of letting an unchecked version read as a green 0."""
    dv = _driver_versions(df)
    code, yours = dv["SUPPORT_CODE"], dv["YOURS"].astype(bool)
    checked = (code == UNSUPPORTED) | (code.isin(_VERDICTS_WITHOUT_MINIMUM) & ~dv["NO_MIN"].astype(bool))
    return {
        "unsupported_yours": int(((code == UNSUPPORTED) & yours).sum()),
        "unsupported_snowflake": int(((code == UNSUPPORTED) & ~yours).sum()),
        "nearing_eos": int(((code == NEARING_EOS) & yours).sum()),
        "below_recommended": int(((code == BELOW_RECOMMENDED) & yours).sum()),
        "checked_yours": int((checked & yours).sum()),
        "not_checked_yours": int(_not_checked(dv).sum()),
        "not_listed_yours": int(((code == NOT_LISTED) & yours & _versioned(dv)).sum()),
    }


def _plural(n: int, one: str, many: str) -> str:
    return one if n == 1 else many


def _context(rows: pd.DataFrame) -> str:
    """Who runs this driver version: the friendly client names if any program has one, else the sample
    users of its busiest row."""
    if rows.empty:
        return ""
    labels = [c for c in dict.fromkeys(rows["CLIENT"].tolist()) if c]
    if labels:
        return ", ".join(labels)
    top = rows.sort_values("SESSIONS", ascending=False, kind="stable").iloc[0] if "SESSIONS" in rows else rows.iloc[0]
    names = [n.strip() for n in _text(top.get("SAMPLE_USERS")).split(",") if n.strip()]
    if not names:
        return ""
    shown = names[:2]
    users = pd.to_numeric(pd.Series([top.get("USERS")]), errors="coerce").fillna(0).iloc[0]
    more = max(int(users) - len(shown), len(names) - len(shown), 0)
    return "used by " + ", ".join(shown) + (f" +{more} more" if more else "")


def support_caption(df: pd.DataFrame) -> str:
    """The per-version upgrade sentence, built from the MIN_SUPPORTED / RECOMMENDED columns (never from
    fixed numbers). Lists yours-to-upgrade UNSUPPORTED versions, busiest first, then the Snowflake-run tail.

    'None ... below the minimum' is said only of versions that were compared with one: when some of yours
    could not be (NOT LISTED, or an entry with no minimum) the sentence counts the checked ones and names
    the rest, and when none could be it never reads clean."""
    counts = support_counts(df)
    rows = _identified(df)
    unsupported = rows[(rows["SUPPORT_CODE"] == UNSUPPORTED)]
    dv = _driver_versions(df)
    mine = dv[(dv["SUPPORT_CODE"] == UNSUPPORTED) & dv["YOURS"].astype(bool)].sort_values(
        "SESSIONS", ascending=False, kind="stable")
    parts: list[str] = []
    for _, r in mine.iterrows():
        vrows = unsupported[(unsupported["DRIVER"] == r["DRIVER"]) & (unsupported["VERSION"] == r["VERSION"])
                            & (unsupported["WHO_UPGRADES"] == WHO_YOURS)]
        ctx = _context(vrows)
        bits = [ctx] if ctx else []
        floor_bits = []
        mn = _text(vrows["MIN_SUPPORTED"].iloc[0]) if not vrows.empty else ""
        rc = _text(vrows["RECOMMENDED"].iloc[0]) if not vrows.empty else ""
        if mn:
            floor_bits.append(f"minimum {mn}")
        if rc:
            floor_bits.append(f"recommended {rc}")
        if floor_bits:
            bits.append(", ".join(floor_bits))
        detail = f" ({'; '.join(bits)})" if bits else ""
        parts.append(f"{driver_display(r['DRIVER'])} {_text(r['VERSION'])}{detail}")
    n, n_sf = counts["unsupported_yours"], counts["unsupported_snowflake"]
    k, checked = counts["not_checked_yours"], counts["checked_yours"]
    sf_what = "Snowflake-run (Snowflake's own web app or services): no action."
    unchecked = dv[_not_checked(dv)].sort_values("SESSIONS", ascending=False, kind="stable")
    names = [f"{driver_display(d)} {_text(v)}" for d, v in zip(unchecked["DRIVER"], unchecked["VERSION"],
                                                                strict=True)]
    listed = ", ".join(names[:4]) + (f" +{len(names) - 4} more" if len(names) > 4 else "")
    n_drv = int(unchecked["DRIVER"].nunique())
    lists_none = f"Snowflake's function lists no minimum for {_plural(n_drv, 'that driver', 'those drivers')}"
    if n:
        text = (f"{n} driver {_plural(n, 'version', 'versions')} below Snowflake's supported minimum "
                f"{_plural(n, 'is', 'are')} yours to upgrade: {', '.join(parts)}.")
        if n_sf:
            text += f" {n_sf} more {_plural(n_sf, 'is', 'are')} {sf_what}"
    elif k and not checked:
        # nothing of yours could be compared: never the clean sentence
        text = (f"None of your driver versions could be checked against Snowflake's supported minimum "
                f"({listed}): {lists_none}.")
    elif k:
        text = f"None of your checked driver versions ({checked}) is below Snowflake's supported minimum."
    else:
        text = "None of your driver versions in this window is below Snowflake's supported minimum."
    if not n and n_sf:
        text += (f" {n_sf} driver {_plural(n_sf, 'version', 'versions')} below it "
                 f"{_plural(n_sf, 'is', 'are')} {sf_what}")
    if k and checked:
        text += (f" {k} of your driver versions could not be checked against a minimum ({listed}): "
                 f"{lists_none}.")
    return text


def capped_caption(rows: int) -> str:
    """The inventory feed hit run()'s row cap: every total over it would be partial, so none is given."""
    return (f"The driver inventory hit the {rows:,}-row cap, so the support KPIs, the upgrade list and the "
            "behind-version count are not shown: they would count only part of it. Narrow the window or "
            "the company scope.")


def unavailable_caption(reason: str) -> str:
    return (f"Snowflake's support floor could not be read ({reason}). STATUS below only compares each "
            "version with the newest one seen in this account. It is not a support verdict.")
