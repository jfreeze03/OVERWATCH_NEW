"""Guard rules FG001-FG020: the checks Flyway does not do (or does too late) for this EDW.

Run by every PR build and again by every deploy (``fw.py guard``). Each rule exists because of a specific Flyway or
Snowflake behaviour; docs/SHORTCOMINGS.md maps them. Rules that are about authoring discipline apply to files under
``migrations/`` and ``repeatable/``; the callbacks are platform code and are only checked by FG005/FG015/FG017.

Waiver: a line ``-- FLYWAY-WAIVER: FGnnn <ticket> <reason>`` anywhere in a file waives that rule for that file,
except the findings marked unwaivable below (DROP DATABASE, CREATE OR REPLACE DATABASE/SCHEMA, USE DATABASE/SCHEMA/
ROLE). A waiver without a ticket and a reason is itself an error (FG001).
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

from . import sqlscan
from .manifest import Kit, config_findings

RULES: dict[str, str] = {
    "FG001": "file naming, location and waiver syntax",
    "FG002": "duplicate version / undo without its versioned migration",
    "FG003": "applied migrations are immutable (byte-identical to the base branch)",
    "FG004": "version timestamp in the future / applies out of order",
    "FG005": "undeclared placeholder",
    "FG006": "destructive DDL (data loss)",
    "FG007": "account-level DDL or grant in a migration",
    "FG008": "USE / ALTER SESSION statement (session state leaks across migrations)",
    "FG009": "hard-coded environment database name / ${edw_db} inside an object body",
    "FG010": "unqualified object name (would land in the FLYWAY schema)",
    "FG011": "CREATE OR REPLACE without COPY GRANTS",
    "FG012": "non-idempotent statement in a repeatable migration",
    "FG013": "repeatable ordering: dependency sorts after its dependant",
    "FG014": "task change without the tasks_enabled gate and a resume",
    "FG015": "Flyway Snowflake parser hazard (bare DECLARE/BEGIN block, unbalanced $$)",
    "FG016": "pipe COPY INTO target is qualified (a cloned pipe would load the source table)",
    "FG017": "secret material in a kit file",
    "FG018": "flyway.toml safety settings",
    "FG019": "runways.toml consistency",
    "FG020": "script configuration (.sql.conf) keys and values",
}

V_RE = re.compile(
    r"^(?P<prefix>[VU])(?P<y>\d{4})\.(?P<mo>\d{2})\.(?P<d>\d{2})\.(?P<hm>\d{4})(?:\.(?P<seq>\d{1,3}))?"
    r"__(?P<desc>[a-z0-9]+(?:_[a-z0-9]+)*)\.sql$"
)
B_RE = re.compile(r"^B(?P<ver>\d+(?:\.\d+)*)__(?P<desc>[a-z0-9]+(?:_[a-z0-9]+)*)\.sql$")
R_RE = re.compile(r"^R__(?P<layer>\d{2})_(?P<schema>[A-Z][A-Z0-9_]*)__(?P<obj>[a-z0-9]+(?:_[a-z0-9]+)*)\.sql$")
SCHEMA_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
WAIVER_RE = re.compile(r"--\s*FLYWAY-WAIVER:\s*(?P<rule>FG\d{3})(?P<rest>[^\n]*)")
PLACEHOLDER_RE = re.compile(r"\$\{([^}]*)\}")
CONF_KEYS = {"shouldexecute", "placeholderreplacement", "executeintransaction", "encoding"}
CALLBACK_EVENTS = {
    "beforeMigrate", "beforeEachMigrate", "beforeEachMigrateStatement", "afterEachMigrateStatement",
    "afterEachMigrateStatementError", "afterEachMigrate", "afterEachMigrateError", "beforeRepeatables",
    "afterVersioned", "afterMigrateApplied", "afterMigrate", "afterMigrateError", "beforeUndo", "beforeEachUndo",
    "beforeEachUndoStatement", "afterEachUndoStatement", "afterEachUndoStatementError", "afterEachUndo",
    "afterEachUndoError", "afterUndo", "afterUndoError", "beforeValidate", "afterValidate", "afterValidateError",
    "beforeBaseline", "afterBaseline", "afterBaselineError", "beforeRepair", "afterRepair", "afterRepairError",
    "beforeInfo", "afterInfo", "afterInfoError", "beforeConnect", "afterConnect",
}
LAYERS = {
    10: "sequences, file formats, stages", 20: "functions", 30: "views", 40: "materialized views / dynamic tables",
    50: "procedures", 70: "streams", 75: "pipes", 80: "tasks", 85: "alerts", 90: "grants",
}
SECRET_PATTERNS = (
    re.compile(r"-----BEGIN (?:ENCRYPTED |RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"(?i)\bPASSWORD\s*=\s*'[^'\s][^']*'"),
)
SECRET_SCAN_SUFFIXES = {".sql", ".toml", ".conf", ".json", ".yaml", ".yml", ".sh", ".groovy", ".cfg", ".properties"}
FLYWAY_HISTORY_DESCRIPTION_MAX = 200  # "description" VARCHAR(200) in the Snowflake history table
FLYWAY_HISTORY_SCRIPT_MAX = 1000  # "script" VARCHAR(1000)

# object kinds that are schema-level objects (targets of CREATE/ALTER/DROP that must be SCHEMA.OBJECT)
_KIND = (
    r"(?:TABLE|VIEW|MATERIALIZED\s+VIEW|DYNAMIC\s+TABLE|EXTERNAL\s+TABLE|EVENT\s+TABLE|ICEBERG\s+TABLE|HYBRID\s+TABLE|"
    r"FUNCTION|PROCEDURE|TASK|STREAM|PIPE|SEQUENCE|STAGE|FILE\s+FORMAT|ALERT|TAG|MASKING\s+POLICY|"
    r"ROW\s+ACCESS\s+POLICY|AGGREGATION\s+POLICY|PROJECTION\s+POLICY|SECRET|SEMANTIC\s+VIEW|NOTEBOOK|STREAMLIT|"
    r"MODEL)"
)
_CREATE_MODS = r"(?:(?:OR\s+REPLACE|OR\s+ALTER|SECURE|TEMPORARY|TEMP|TRANSIENT|VOLATILE|LOCAL|GLOBAL|RECURSIVE|"
_CREATE_MODS += r"AGGREGATE|EXTERNAL|HYBRID|ICEBERG)\s+)*"
TARGET_RE = re.compile(
    rf"^(?:CREATE\s+{_CREATE_MODS}(?P<ck>{_KIND})(?:\s+IF\s+NOT\s+EXISTS)?"
    rf"|ALTER\s+(?P<ak>{_KIND})(?:\s+IF\s+EXISTS)?"
    rf"|DROP\s+(?P<dk>{_KIND})(?:\s+IF\s+EXISTS)?"
    rf"|UNDROP\s+(?P<uk>{_KIND})"
    rf"|COMMENT\s+(?:IF\s+EXISTS\s+)?ON\s+(?P<mk>{_KIND}|COLUMN))\s+(?P<name>{sqlscan.NAME_RE.pattern})",
    re.IGNORECASE,
)
BODY_KINDS = re.compile(
    r"^CREATE\s+" + _CREATE_MODS + r"(?:VIEW|MATERIALIZED\s+VIEW|DYNAMIC\s+TABLE|FUNCTION|PROCEDURE|TASK|PIPE|ALERT|"
    r"SEMANTIC\s+VIEW)\b",
    re.IGNORECASE,
)
EDW_DB_RE = re.compile(r"\bALFA_EDW_[A-Z0-9_]+", re.IGNORECASE)


@dataclass(frozen=True)
class Finding:
    rule: str
    severity: str  # "error" | "warning"
    path: str
    line: int
    message: str

    def render(self) -> str:
        return f"{self.path}:{self.line}: {self.rule} {self.severity}: {self.message}"


@dataclass(frozen=True)
class SqlFile:
    path: Path
    rel: str  # path relative to the kit root, forward slashes
    area: str  # "migrations" | "repeatable" | "callbacks"
    text: str
    script: sqlscan.Script
    waivers: frozenset[str]
    placeholder_replacement: bool

    def line_of(self, offset: int) -> int:
        return self.script.line_of(offset)


def version_key(version: str) -> tuple[int, ...]:
    """Flyway compares version parts numerically and ignores trailing zero parts (1.0 == 1)."""
    parts = [int(p) for p in version.split(".")]
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def flyway_description(filename: str) -> str:
    """The description Flyway stores and (for repeatables) sorts on: text after the separator, `_` -> space."""
    stem = filename[:-4] if filename.endswith(".sql") else filename
    _, _, desc = stem.partition("__")
    return desc.replace("_", " ")


def _parse_conf(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        s = line.strip()
        if not s or s[0] in "#!":
            continue
        key, sep, val = s.partition("=")
        if sep:
            out[key.strip()] = val.strip()
    return out


class Guard:
    def __init__(self, kit: Kit, *, base: str | None = None, now: datetime | None = None) -> None:
        self.kit = kit
        self.root = kit.root
        self.base = base
        self.now = now or datetime.now(UTC)
        self.findings: list[Finding] = []
        self.files: list[SqlFile] = []
        self._versions: dict[tuple[int, ...], str] = {}

    # ------------------------------------------------------------------ plumbing
    def add(self, rule: str, severity: str, rel: str, line: int, msg: str, *, f: SqlFile | None = None,
            waivable: bool = True) -> None:
        if f is not None and waivable and rule in f.waivers and severity == "error":
            return
        self.findings.append(Finding(rule, severity, rel, line, msg))

    def _load(self) -> None:
        for area in ("migrations", "repeatable", "callbacks"):
            base = self.root / area
            if not base.is_dir():
                continue
            for path in sorted(p for p in base.rglob("*") if p.is_file()):
                rel = path.relative_to(self.root).as_posix()
                if path.suffix == ".sql":
                    text = path.read_text(encoding="utf-8")
                    conf_path = path.with_name(path.name + ".conf")
                    conf = _parse_conf(conf_path.read_text(encoding="utf-8")) if conf_path.exists() else {}
                    pr = {k.lower(): v for k, v in conf.items()}.get("placeholderreplacement", "true").lower()
                    waivers = set()
                    for m in WAIVER_RE.finditer(text):
                        rest = m.group("rest").split()
                        if len(rest) < 2:
                            self.add("FG001", "error", rel, text.count("\n", 0, m.start()) + 1,
                                     f"waiver for {m.group('rule')} needs a ticket and a reason")
                        else:
                            waivers.add(m.group("rule"))
                    self.files.append(SqlFile(path, rel, area, text, sqlscan.scan(text), frozenset(waivers),
                                              pr != "false"))
                elif path.name.endswith(".sql.conf"):
                    self._check_conf(path, rel)
                elif path.name in (".gitkeep", "README.md"):
                    continue
                else:
                    self.add("FG001", "error", rel, 1, "only .sql migrations (and their .sql.conf) belong here")

    # ------------------------------------------------------------------ entry point
    def run(self) -> list[Finding]:
        for cf in config_findings(self.kit):
            self.findings.append(Finding(cf.rule, cf.severity, cf.path, 1, cf.message))
        self._load()
        self._naming_and_versions()
        if self.base:
            self._immutability()
        for f in self.files:
            self._placeholders(f)
            self._parser_hazards(f)
            if f.area == "callbacks":
                continue
            self._statements(f)
        self._repeatable_order()
        self._secrets()
        self.findings.sort(key=lambda x: (x.path, x.line, x.rule))
        return self.findings

    # ------------------------------------------------------------------ FG001 / FG002 / FG004
    def _naming_and_versions(self) -> None:
        versions: dict[tuple[int, ...], str] = {}
        undo: list[tuple[tuple[int, ...], str, SqlFile]] = []
        future = self.now + timedelta(days=1)
        for f in self.files:
            parts = Path(f.rel).parts
            name = parts[-1]
            if f.area == "callbacks":
                event = name[:-4].split("__", 1)[0]
                if len(parts) != 2 or event not in CALLBACK_EVENTS:
                    self.add("FG001", "error", f.rel, 1,
                             f"callback must be callbacks/<event>[__<desc>].sql with a Flyway event (got {event!r})")
                continue
            if len(parts) != 3:
                self.add("FG001", "error", f.rel, 1, f"expected {f.area}/<SCHEMA>/<file>.sql")
                continue
            folder = parts[1]
            if f.area == "migrations" and folder == "_baseline":
                if not B_RE.match(name):
                    self.add("FG001", "error", f.rel, 1, "migrations/_baseline holds only B<version>__<desc>.sql")
                continue
            if not SCHEMA_RE.match(folder):
                self.add("FG001", "error", f.rel, 1, f"schema folder {folder!r} must be an upper-case schema name")
                continue
            if len(f.rel) > FLYWAY_HISTORY_SCRIPT_MAX:
                self.add("FG001", "error", f.rel, 1, "path longer than the history table's script column (1000)")
            if len(flyway_description(name)) > FLYWAY_HISTORY_DESCRIPTION_MAX:
                self.add("FG001", "error", f.rel, 1, "description longer than the history table allows (200)")
            if f.area == "repeatable":
                m = R_RE.match(name)
                if not m:
                    self.add("FG001", "error", f.rel, 1,
                             "repeatable must be named R__<LL>_<SCHEMA>__<object_lower>.sql (e.g. R__30_SALES__v_orders.sql)")
                elif m.group("schema") != folder:
                    self.add("FG001", "error", f.rel, 1, f"file names schema {m.group('schema')} but sits in {folder}/")
                elif int(m.group("layer")) // 10 * 10 not in LAYERS:
                    self.add("FG001", "warning", f.rel, 1,
                             f"layer {m.group('layer')} is outside the documented bands {sorted(LAYERS)}")
                continue
            m = V_RE.match(name)
            if not m:
                self.add("FG001", "error", f.rel, 1,
                         "versioned migration must be V<YYYY>.<MM>.<DD>.<HHMM>[.<N>]__<schema>_<desc>.sql "
                         "(run: python3 scripts/fw.py new)")
                continue
            if not m.group("desc").startswith(folder.lower() + "_"):
                self.add("FG001", "error", f.rel, 1, f"description must start with '{folder.lower()}_'")
            try:
                stamp = datetime(int(m.group("y")), int(m.group("mo")), int(m.group("d")),
                                 int(m.group("hm")[:2]), int(m.group("hm")[2:]), tzinfo=UTC)
            except ValueError:
                self.add("FG001", "error", f.rel, 1, "version is not a real date/time (YYYY.MM.DD.HHMM)")
                continue
            if stamp > future:
                self.add("FG004", "error", f.rel, 1,
                         "version timestamp is in the future: every later migration would apply out of order")
            version = ".".join(x for x in (m.group("y"), m.group("mo"), m.group("d"), m.group("hm"), m.group("seq"))
                               if x is not None)
            key = version_key(version)
            if m.group("prefix") == "U":
                undo.append((key, m.group("desc"), f))
                continue
            if key in versions:
                self.add("FG002", "error", f.rel, 1, f"version {version} duplicates {versions[key]}")
            else:
                versions[key] = f.rel
        for key, desc, f in undo:
            twin = versions.get(key)
            if twin is None:
                self.add("FG002", "error", f.rel, 1, "undo script has no versioned migration with the same version")
            elif flyway_description(Path(twin).name) != desc.replace("_", " "):
                self.add("FG002", "error", f.rel, 1, f"undo description must match {twin}")
        self._versions = versions

    # ------------------------------------------------------------------ FG003 / FG004 (git)
    def _git(self, *args: str) -> str:
        return subprocess.run(["git", "-C", str(self.root), *args], check=True, capture_output=True,
                              text=False).stdout.decode("utf-8", "replace")

    def _immutability(self) -> None:
        assert self.base is not None
        try:
            top = Path(self._git("rev-parse", "--show-toplevel").strip())
            self._git("rev-parse", "--verify", f"{self.base}^{{commit}}")
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            raise GuardUsageError(f"cannot resolve git base {self.base!r}: {exc}") from exc
        prefix = self.root.relative_to(top).as_posix()
        prefix = "" if prefix == "." else prefix + "/"
        listing = self._git("ls-tree", "-r", "--name-only", self.base, "--", f"{prefix}migrations")
        base_max: tuple[int, ...] = ()
        for repo_path in filter(None, listing.splitlines()):
            rel = repo_path[len(prefix):]
            name = Path(rel).name
            if not (name.endswith((".sql", ".sql.conf"))):
                continue
            if not (V_RE.match(name.removesuffix(".conf")) or B_RE.match(name.removesuffix(".conf"))):
                continue
            here = self.root / rel
            if not here.exists():
                self.add("FG003", "error", rel, 1,
                         f"applied migration deleted or renamed (present on {self.base}); fix forward instead")
                continue
            old = subprocess.run(["git", "-C", str(top), "show", f"{self.base}:{repo_path}"], check=True,
                                 capture_output=True).stdout
            if old != here.read_bytes():
                self.add("FG003", "error", rel, 1,
                         f"changed since {self.base}: Flyway checksums would fail on every database it ran on; "
                         "write a new migration instead")
            m = V_RE.match(name)
            if m and m.group("prefix") == "V":
                v = ".".join(x for x in (m.group("y"), m.group("mo"), m.group("d"), m.group("hm"), m.group("seq"))
                             if x is not None)
                base_max = max(base_max, version_key(v))
        if base_max:
            for key, rel in sorted(self._versions.items()):
                path = self.root / rel
                try:
                    tracked = subprocess.run(
                        ["git", "-C", str(top), "cat-file", "-e", f"{self.base}:{prefix}{rel}"],
                        capture_output=True).returncode == 0
                except FileNotFoundError:
                    tracked = False
                if not tracked and key < base_max and path.exists():
                    self.add("FG004", "warning", rel, 1,
                             "older than the newest migration on the base branch: it will apply OUT OF ORDER "
                             "(outOfOrder=true); make sure it does not depend on ordering")

    # ------------------------------------------------------------------ FG005
    def _placeholders(self, f: SqlFile) -> None:
        if not f.placeholder_replacement:
            return
        declared = self.kit.declared_placeholders()
        for m in PLACEHOLDER_RE.finditer(f.script.code):
            name = m.group(1).strip().lower()
            if name.startswith("flyway:") or name in declared:
                continue
            self.add("FG005", "error", f.rel, f.line_of(m.start()),
                     f"placeholder ${{{m.group(1)}}} is not defined for every environment in flyway.toml "
                     "(a JavaScript template literal? set placeholderReplacement=false in <file>.sql.conf)", f=f)

    # ------------------------------------------------------------------ FG015
    def _parser_hazards(self, f: SqlFile) -> None:
        if f.script.unterminated:
            self.add("FG015", "error", f.rel, 1,
                     f"file ends inside an unterminated {f.script.unterminated}: Flyway would mis-split it", f=f)
        for st in f.script.statements:
            head = st.head
            if re.match(r"^DECLARE\b", head):
                self.add("FG015", "error", f.rel, st.line,
                         "bare DECLARE block: wrap Snowflake Scripting as EXECUTE IMMEDIATE $$ ... $$;", f=f)
            elif re.match(r"^BEGIN\b", head):
                raw_head = st.raw.lstrip()
                if not re.match(r"^BEGIN (?:TRANSACTION|WORK|NAME)\b|^BEGIN\s*$", raw_head, re.IGNORECASE):
                    self.add("FG015", "error", f.rel, st.line,
                             "bare BEGIN block (Flyway's Snowflake parser only exempts 'BEGIN TRANSACTION' with one "
                             "space): wrap it as EXECUTE IMMEDIATE $$ ... $$;", f=f)

    # ------------------------------------------------------------------ statement rules
    def _statements(self, f: SqlFile) -> None:
        file_code = f.script.code
        is_rep = f.area == "repeatable"
        touches_task = False
        for st in f.script.statements:
            head = st.head
            line = st.line
            # FG006 destructive DDL
            if re.match(r"^DROP DATABASE\b|^CREATE OR REPLACE (?:TRANSIENT )?(?:DATABASE|SCHEMA)\b", head):
                self.add("FG006", "error", f.rel, line,
                         "DROP DATABASE / CREATE OR REPLACE DATABASE|SCHEMA is never allowed in a migration",
                         f=f, waivable=False)
            elif re.match(r"^DROP SCHEMA\b", head):
                self.add("FG006", "error", f.rel, line, "DROP SCHEMA destroys every object in it", f=f)
            elif re.match(r"^DROP (?:TRANSIENT |HYBRID |ICEBERG |EXTERNAL )?TABLE\b", head):
                self.add("FG006", "error", f.rel, line, "DROP TABLE loses data (waive with a ticket if intended)", f=f)
            elif re.match(r"^TRUNCATE\b", head):
                self.add("FG006", "error", f.rel, line, "TRUNCATE loses data", f=f)
            elif re.match(r"^CREATE OR REPLACE (?:TRANSIENT |HYBRID |ICEBERG )?TABLE\b", head):
                self.add("FG006", "error", f.rel, line,
                         "CREATE OR REPLACE TABLE drops the data, its Time Travel history and staleness-breaks "
                         "streams; use ALTER TABLE (or CREATE TABLE IF NOT EXISTS for new tables)", f=f)
            elif re.match(r"^ALTER TABLE (?:IF EXISTS )?\S+ (?:.*\s)?DROP (?:COLUMN\b|(?!CONSTRAINT|PRIMARY|UNIQUE|"
                          r"FOREIGN|CLUSTERING|ROW|SEARCH|ALL|AGGREGATION|JOIN|MASKING|PROJECTION|TAG|STORAGE|"
                          r"CONTACT|DATA METRIC)\w)", head):
                self.add("FG006", "error", f.rel, line, "ALTER TABLE ... DROP COLUMN loses data", f=f)
            elif re.match(r"^DELETE FROM \S+\s*$", head):
                self.add("FG006", "error", f.rel, line, "DELETE without WHERE empties the table", f=f)
            # FG007 account-level
            if re.match(r"^(?:CREATE|ALTER|DROP)(?: OR REPLACE| OR ALTER)? (?:WAREHOUSE|ROLE|USER|DATABASE(?! ROLE)|"
                        r"(?:API |STORAGE |NOTIFICATION |SECURITY |EXTERNAL ACCESS |CATALOG )?INTEGRATION|"
                        r"NETWORK POLICY|NETWORK RULE|RESOURCE MONITOR|SHARE|AUTHENTICATION POLICY|PASSWORD POLICY|"
                        r"SESSION POLICY|FAILOVER GROUP|REPLICATION GROUP|CONNECTION|COMPUTE POOL|ACCOUNT)\b", head) \
                    or re.match(r"^ALTER ACCOUNT\b", head) \
                    or re.match(r"^(?:GRANT|REVOKE)\b.*\bON (?:ACCOUNT|DATABASE|WAREHOUSE|INTEGRATION|USER)\b", head) \
                    or re.match(r"^GRANT ROLE \S+ TO USER\b", head) \
                    or re.match(r"^GRANT OWNERSHIP\b", head):
                self.add("FG007", "error", f.rel, line,
                         "account-level object or grant: belongs in bootstrap/ (run by a Snowflake admin), "
                         "not in a migration that every runway replays", f=f)
            # FG008 USE
            if re.match(r"^USE WAREHOUSE\b", head):
                self.add("FG008", "error", f.rel, line,
                         "USE WAREHOUSE persists for the rest of the Flyway run (waive for a heavy backfill)", f=f)
            elif re.match(r"^ALTER SESSION\b", head):
                self.add("FG008", "error", f.rel, line,
                         "ALTER SESSION persists for every later migration in the same Flyway run (Flyway resets only "
                         "ROLE and SCHEMA); waive it only with a matching ALTER SESSION UNSET at the end", f=f)
            elif re.match(r"^USE\b", head):
                self.add("FG008", "error", f.rel, line,
                         "USE DATABASE/SCHEMA/ROLE: Flyway resets ROLE and SCHEMA before each migration but never the "
                         "DATABASE, so this leaks into later migrations; schema-qualify instead", f=f, waivable=False)
            # FG009 hard-coded environment database / ${edw_db} in bodies
            for m in EDW_DB_RE.finditer(st.code):
                self.add("FG009", "error", f.rel, f.line_of(st.offset + m.start()),
                         f"hard-coded {m.group(0)}: code must not name its database (clone safety, one artifact "
                         "for every runway); use SCHEMA.OBJECT, or a placeholder for another database", f=f)
            if BODY_KINDS.match(head):
                # offsets must be taken on `masked` (same length as `code`), not on the collapsed `head`
                tm = TARGET_RE.match(st.masked)
                cut = tm.end("name") if tm else 0
                for m in re.finditer(r"\$\{\s*(?:edw_db|home_db)\s*\}", st.code, re.IGNORECASE):
                    if m.start() >= cut:
                        self.add("FG009", "error", f.rel, f.line_of(st.offset + m.start()),
                                 "${edw_db} inside an object body becomes a literal database name: a clone of this "
                                 "database would still point at the original. Use SCHEMA.OBJECT.", f=f)
            # FG010 unqualified target
            tm = TARGET_RE.match(head)
            if tm:
                kind = next(tm.group(g) for g in ("ck", "ak", "dk", "uk", "mk") if tm.group(g))
                name = tm.group("name")
                visible = [p for p in sqlscan.name_parts(name) if not p.startswith("${")]
                if kind.upper() != "COLUMN" and len(visible) < 2:
                    self.add("FG010", "error", f.rel, line,
                             f"{kind.upper()} {name} is not schema-qualified: Flyway runs USE SCHEMA FLYWAY before "
                             "every migration, so it would land in FLYWAY", f=f)
            # FG011 COPY GRANTS
            if re.match(r"^CREATE OR REPLACE (?:SECURE )?(?:RECURSIVE )?(?:MATERIALIZED VIEW|VIEW|FUNCTION|"
                        r"AGGREGATE FUNCTION|PROCEDURE|DYNAMIC TABLE|STREAM)\b", head) \
                    and not re.search(r"\bCOPY GRANTS\b", head):
                self.add("FG011", "error", f.rel, line,
                         "CREATE OR REPLACE without COPY GRANTS drops every explicit grant on the object "
                         "(add COPY GRANTS, or use CREATE OR ALTER)", f=f)
            # FG012 repeatable idempotency
            if is_rep:
                ok = (
                    re.match(r"^CREATE OR (?:REPLACE|ALTER)\b", head)
                    or re.match(r"^CREATE\b.*?\bIF NOT EXISTS\b", head)
                    or re.match(r"^(?:GRANT|REVOKE|COMMENT|EXECUTE IMMEDIATE|SELECT|SHOW|DESC|DESCRIBE)\b", head)
                    or re.match(r"^ALTER (?:TASK|ALERT|PIPE|DYNAMIC TABLE)\b.*\b(?:SUSPEND|RESUME|SET|UNSET|"
                                r"REFRESH)\b", head)
                    or re.match(r"^ALTER (?:VIEW|FUNCTION|PROCEDURE|STREAM|MATERIALIZED VIEW) .*\b(?:SET|UNSET)\b",
                                head)
                )
                if not ok:
                    self.add("FG012", "error", f.rel, line,
                             "repeatable migrations re-run whenever they change: only CREATE OR REPLACE / CREATE OR "
                             "ALTER / IF NOT EXISTS, grants and task/alert state changes belong here; data and "
                             "table changes go in a V migration", f=f)
            # FG014 (collect) / FG016
            if re.match(r"^CREATE(?: OR (?:REPLACE|ALTER))? TASK\b", head) or re.match(
                    r"^ALTER TASK\b.*\b(?:MODIFY|SET|UNSET|ADD|REMOVE)\b", head):
                touches_task = True
            pm = re.match(r"^CREATE(?: OR (?:REPLACE|ALTER))? PIPE\b.*?\bAS COPY INTO (?P<t>\S+)", head)
            if pm and "." in pm.group("t"):
                self.add("FG016", "error", f.rel, line,
                         f"pipe COPY INTO {pm.group('t')}: Snowflake treats schema.table as fully qualified, so a "
                         "cloned runway's pipe would load the SOURCE table; use the bare table name", f=f)
        if touches_task:
            gated = re.search(r"\$\{\s*tasks_enabled\s*\}", file_code, re.IGNORECASE)
            resumes = re.search(r"SYSTEM\$TASK_DEPENDENTS_ENABLE|\bALTER\s+TASK\b[^;]*\bRESUME\b", file_code,
                                re.IGNORECASE)
            if not (gated and resumes):
                self.add("FG014", "error", f.rel, 1,
                         "a created/modified task is left SUSPENDED: resume it with SYSTEM$TASK_DEPENDENTS_ENABLE "
                         "gated on '${tasks_enabled}' = 'true' (see docs/CONVENTIONS.md, Tasks)", f=f)

    # ------------------------------------------------------------------ FG013
    def _repeatable_order(self) -> None:
        reps = [f for f in self.files if f.area == "repeatable" and R_RE.match(Path(f.rel).name)]
        defined: dict[str, tuple[str, SqlFile]] = {}
        for f in reps:
            key = flyway_description(Path(f.rel).name)
            for st in f.script.statements:
                tm = TARGET_RE.match(st.head)
                if tm and tm.group("ck"):
                    defined.setdefault(sqlscan.normalize_name(tm.group("name")), (key, f))
        if not defined:
            return
        for f in reps:
            mine = flyway_description(Path(f.rel).name)
            own = {n for n, (_, g) in defined.items() if g is f}
            text = f.script.code
            for name, (theirs, g) in defined.items():
                if g is f or name in own or "." not in name:
                    continue
                schema, obj = name.split(".", 1)
                pat = re.compile(rf'(?<![\w$."]){re.escape(schema)}\s*\.\s*"?{re.escape(obj)}"?(?![\w$])',
                                 re.IGNORECASE)
                m = pat.search(text)
                if m and theirs > mine:
                    self.add("FG013", "error", f.rel, f.line_of(m.start()),
                             f"uses {name}, defined by {g.rel}, which Flyway runs AFTER this file (repeatables run "
                             f"in description order: '{theirs}' > '{mine}'); raise this file's layer number", f=f)

    # ------------------------------------------------------------------ FG017
    def _secrets(self) -> None:
        skip = {".git", "__pycache__", "dist", "reports", "node_modules"}
        for path in sorted(self.root.rglob("*")):
            if not path.is_file() or skip & set(path.relative_to(self.root).parts):
                continue
            if path.suffix not in SECRET_SCAN_SUFFIXES and not path.name.startswith("Jenkinsfile"):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            rel = path.relative_to(self.root).as_posix()
            for pat in SECRET_PATTERNS:
                for m in pat.finditer(text):
                    self.add("FG017", "error", rel, text.count("\n", 0, m.start()) + 1,
                             "secret material in a repository file: use Jenkins credentials / ${env.*} resolvers")

    # ------------------------------------------------------------------ FG020
    def _check_conf(self, path: Path, rel: str) -> None:
        sql = path.with_name(path.name[: -len(".conf")])
        if not sql.exists():
            self.add("FG001", "error", rel, 1, f"script configuration without its script {sql.name}")
        conf = _parse_conf(path.read_text(encoding="utf-8"))
        for key, val in conf.items():
            k = key.lower()
            if k not in CONF_KEYS:
                self.add("FG020", "error", rel, 1,
                         f"unknown key {key!r}: Flyway rejects it and EVERY command that resolves migrations fails")
            elif k in ("placeholderreplacement", "executeintransaction") and val.lower() not in ("true", "false"):
                self.add("FG020", "error", rel, 1, f"{key} must be true or false")
            elif k == "shouldexecute" and "flyway:environment" in val.replace(" ", ""):
                self.add("FG020", "warning", rel, 1,
                         "shouldExecute on ${flyway:environment} is RH_<ID> during a rehearsal; use ${env}")


class GuardUsageError(Exception):
    """Bad invocation (unknown base ref etc.): exit 2."""


def junit_xml(findings: Iterable[Finding], files_checked: int) -> str:
    """One JUnit testcase per rule (pass/fail), so Jenkins shows which rules failed and the PR build turns red."""
    by_rule: dict[str, list[Finding]] = {r: [] for r in RULES}
    for x in findings:
        by_rule.setdefault(x.rule, []).append(x)
    errors = [x for x in findings if x.severity == "error"]
    out = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<testsuite name="flyway-guard" tests="{len(by_rule)}" failures="{len({x.rule for x in errors})}">',
        f'  <properties><property name="files_checked" value="{files_checked}"/></properties>',
    ]
    for rule, items in by_rule.items():
        out.append(f"  <testcase classname=\"flyway-guard\" name={quoteattr(rule + ' ' + RULES.get(rule, ''))}>")
        errs = [x for x in items if x.severity == "error"]
        if errs:
            body = "\n".join(x.render() for x in errs)
            out.append(f"    <failure message={quoteattr(f'{len(errs)} finding(s)')}>{escape(body)}</failure>")
        warns = [x for x in items if x.severity == "warning"]
        if warns:
            out.append(f"    <system-out>{escape(chr(10).join(x.render() for x in warns))}</system-out>")
        out.append("  </testcase>")
    out.append("</testsuite>")
    return "\n".join(out) + "\n"


def findings_json(findings: Iterable[Finding]) -> str:
    return json.dumps([asdict(x) for x in findings], indent=2) + "\n"
