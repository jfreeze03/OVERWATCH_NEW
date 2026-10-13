"""Load and validate the two configuration files the pipeline runs on.

* ``flyway.toml``  -- read by Flyway itself (environments, placeholders, safety settings).
* ``runways.toml`` -- read only by the pipeline and ``fw.py`` (promotion order, approvals, locks, gates).

``config_findings`` implements guard rules FG018 (flyway.toml safety) and FG019 (runways.toml consistency and its
agreement with flyway.toml). Everything else imports :class:`Kit` to find the runway policy.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

RUNWAY_IDS = ("DEV", "SAN", "SEA", "PHX", "MGM", "PRD")
DB_PREFIX = "ALFA_EDW_"
REHEARSAL_PREFIX = "ALFA_EDW_RH_"
TIERS = ("nonprod", "prod")
LANES = ("main", "branch")
DRIFT_GATES = ("warn", "fail")
NONPROD_PATTERNS = ["*:missing", "*:future"]
PROD_PATTERNS = ["*:future", "repeatable:missing"]
PER_DEPLOY_PLACEHOLDERS = ("deploy_id", "git_sha", "build_url", "approver")
SECRET_KEYS = ("password", "token", "email", "licensekey", "offlinepermitpath")
SECRET_KEY_PARTS = ("pwd", "passphrase", "password", "privatekey", "secret", "token")
# The only keys an [environments.<ID>.flyway] table may set: anything else would silently override a [flyway] safety
# setting for that runway (ClassicConfiguration prefers getEnvironmentOverrides()).
ENV_FLYWAY_KEYS = {"outOfOrder", "cleanDisabled", "baselineOnMigrate", "ignoreMigrationPatterns", "placeholders"}


class ConfigError(Exception):
    """A configuration file is missing or unreadable (usage error, exit 2)."""


@dataclass(frozen=True)
class Runway:
    id: str
    database: str
    tier: str
    order: int
    lanes: tuple[str, ...]
    promote_from: tuple[str, ...]
    approval_group: str
    four_eyes: bool
    ticket_required: bool
    rehearsal: bool
    drift_gate: str
    save_snapshot: bool
    preview_checks: bool
    lock: str
    credentials_id: str
    service_user: str
    deploy_role: str
    deploy_warehouse: str
    jenkins_folder: str
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @property
    def rehearsal_db(self) -> str:
        return REHEARSAL_PREFIX + self.id

    @property
    def rehearsal_env(self) -> str:
        return "RH_" + self.id


@dataclass(frozen=True)
class Kit:
    root: Path
    flyway: dict[str, Any]
    runways_doc: dict[str, Any]
    runways: dict[str, Runway]

    @property
    def kit(self) -> dict[str, Any]:
        return dict(self.runways_doc.get("kit", {}))

    @property
    def drift(self) -> dict[str, Any]:
        return dict(self.runways_doc.get("drift", {}))

    @property
    def history_schema(self) -> str:
        return str(self.kit.get("history_schema", "FLYWAY"))

    def runway(self, rid: str) -> Runway:
        key = rid.upper()
        if key not in self.runways:
            raise ConfigError(f"unknown runway {rid!r}; expected one of {', '.join(self.runways)}")
        return self.runways[key]

    def env_placeholders(self, env_id: str) -> dict[str, str]:
        """Effective placeholders for a Flyway environment. An environment's own placeholders table REPLACES the root
        [flyway.placeholders] table, it is not merged (ClassicConfiguration.getPlaceholders in Flyway 13.10); the
        root table applies only to an environment without one. Names are lower-cased (Flyway matches them
        case-insensitively)."""
        env = self.flyway.get("environments", {}).get(env_id, {})
        table = env.get("flyway", {}).get("placeholders")
        if table is None:
            table = self.flyway.get("flyway", {}).get("placeholders", {})
        return {str(k).lower(): str(v) for k, v in table.items()}

    def declared_placeholders(self) -> set[str]:
        """Placeholders every environment resolves (the intersection of their effective tables)."""
        envs = list(self.flyway.get("environments", {}))
        if not envs:
            return set(self.env_placeholders(""))
        sets = [set(self.env_placeholders(e)) for e in envs]
        return set.intersection(*sets)


def _load_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except FileNotFoundError as exc:
        raise ConfigError(f"missing {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: invalid TOML: {exc}") from exc


def _runway(rid: str, d: dict[str, Any]) -> Runway:
    def s(key: str, default: str = "") -> str:
        return str(d.get(key, default))

    def b(key: str) -> bool:
        return bool(d.get(key, False))

    return Runway(
        id=rid,
        database=s("database"),
        tier=s("tier"),
        order=int(d.get("order", 0)),
        lanes=tuple(str(x) for x in d.get("lanes", [])),
        promote_from=tuple(str(x) for x in d.get("promote_from", [])),
        approval_group=s("approval_group"),
        four_eyes=b("four_eyes"),
        ticket_required=b("ticket_required"),
        rehearsal=b("rehearsal"),
        drift_gate=s("drift_gate", "warn"),
        save_snapshot=b("save_snapshot"),
        preview_checks=b("preview_checks"),
        lock=s("lock"),
        credentials_id=s("credentials_id"),
        service_user=s("service_user"),
        deploy_role=s("deploy_role"),
        deploy_warehouse=s("deploy_warehouse"),
        jenkins_folder=s("jenkins_folder"),
        raw=dict(d),
    )


def load_kit(root: Path) -> Kit:
    root = root.resolve()
    flyway = _load_toml(root / "flyway.toml")
    doc = _load_toml(root / "runways.toml")
    runways = {rid.upper(): _runway(rid.upper(), d) for rid, d in doc.get("runways", {}).items()}
    return Kit(root=root, flyway=flyway, runways_doc=doc, runways=runways)


# --------------------------------------------------------------------------- FG018 / FG019

@dataclass(frozen=True)
class ConfigFinding:
    rule: str
    severity: str
    path: str
    message: str


def _url_param(url: str, key: str) -> str | None:
    m = re.search(rf"[?&]{re.escape(key)}=([^&]*)", url)
    return m.group(1) if m else None


def _walk_secret_keys(node: Any, trail: str) -> list[str]:
    hits: list[str] = []
    if isinstance(node, dict):
        for k, v in node.items():
            here = f"{trail}.{k}" if trail else str(k)
            norm = str(k).lower().replace("_", "")
            secretish = norm in SECRET_KEYS or any(part in norm for part in SECRET_KEY_PARTS)
            if secretish and isinstance(v, str) and not v.startswith("${env."):
                hits.append(here)
            hits.extend(_walk_secret_keys(v, here))
    return hits


def config_findings(kit: Kit) -> list[ConfigFinding]:
    out: list[ConfigFinding] = []

    def err(rule: str, path: str, msg: str) -> None:
        out.append(ConfigFinding(rule, "error", path, msg))

    def warn(rule: str, path: str, msg: str) -> None:
        out.append(ConfigFinding(rule, "warning", path, msg))

    fw = kit.flyway.get("flyway", {})
    envs = kit.flyway.get("environments", {})
    hs = kit.history_schema

    # ---- FG018: flyway.toml safety
    expect = {
        "cleanDisabled": True,
        "validateOnMigrate": True,
        "baselineOnMigrate": False,
        "createSchemas": False,
        "outOfOrder": True,
        "defaultSchema": hs,
    }
    for key, want in expect.items():
        if fw.get(key) != want:
            err("FG018", "flyway.toml", f"[flyway] {key} must be {want!r} (found {fw.get(key)!r})")
    if "schemas" in fw:
        err("FG018", "flyway.toml", "[flyway] schemas is not a Flyway-namespace setting (Flyway 13 rejects it): set "
                                    "schemas in each [environments.<ID>] table")
    if "table" in fw:
        err("FG018", "flyway.toml", '[flyway] table must stay the default "flyway_schema_history" (drift.py, the auditor '
                                    "grants and the runbooks read it)")
    if fw.get("skipExecutingMigrations") is True:
        err("FG018", "flyway.toml", "[flyway] skipExecutingMigrations would record migrations as applied without running")
    if fw.get("placeholderReplacement") is False:
        err("FG018", "flyway.toml", "[flyway] placeholderReplacement=false would deploy ${...} literally everywhere")
    locs = set(fw.get("locations", []))
    for loc in ("filesystem:migrations", "filesystem:repeatable", "filesystem:callbacks"):
        if loc not in locs:
            err("FG018", "flyway.toml", f"[flyway] locations must include {loc!r}")
    for hit in _walk_secret_keys(kit.flyway, ""):
        err("FG018", "flyway.toml", f"{hit}: credentials/licence values must come from ${{env.*}}, never the file")

    for rid, rw in kit.runways.items():
        env = envs.get(rid)
        if env is None:
            err("FG018", "flyway.toml", f"[environments.{rid}] missing (runways.toml defines runway {rid})")
            continue
        _check_env(env, rid, rw, rehearsal=False, err=err, history_schema=hs)
        if rw.rehearsal:
            renv = envs.get(rw.rehearsal_env)
            if renv is None:
                err("FG018", "flyway.toml", f"[environments.{rw.rehearsal_env}] missing (runway {rid} rehearses)")
            else:
                _check_env(renv, rw.rehearsal_env, rw, rehearsal=True, err=err, history_schema=hs)
    known = set(kit.runways) | {r.rehearsal_env for r in kit.runways.values() if r.rehearsal}
    for name in envs:
        if name not in known:
            warn("FG018", "flyway.toml", f"[environments.{name}] is not a runway or rehearsal environment")

    # ---- FG019: runways.toml
    ids = set(kit.runways)
    if ids != set(RUNWAY_IDS):
        err("FG019", "runways.toml", f"runways must be exactly {', '.join(RUNWAY_IDS)} (found {', '.join(sorted(ids))})")
    locks: dict[str, str] = {}
    for rid, rw in kit.runways.items():
        p = f"runways.toml [runways.{rid}]"
        if rw.database != DB_PREFIX + rid:
            err("FG019", p, f"database must be {DB_PREFIX + rid} (found {rw.database!r})")
        if rw.tier not in TIERS:
            err("FG019", p, f"tier must be one of {TIERS}")
        if not rw.lanes or any(lane not in LANES for lane in rw.lanes):
            err("FG019", p, f"lanes must be a non-empty subset of {LANES}")
        if rw.drift_gate not in DRIFT_GATES:
            err("FG019", p, f"drift_gate must be one of {DRIFT_GATES}")
        for src in rw.promote_from:
            if src not in kit.runways:
                err("FG019", p, f"promote_from names unknown runway {src!r}")
            elif kit.runways[src].order >= rw.order:
                err("FG019", p, f"promote_from {src} must come earlier (order {kit.runways[src].order} >= {rw.order})")
        if rw.tier == "prod":
            if list(rw.lanes) != ["main"]:
                err("FG019", p, 'prod-tier runways accept main bundles only: lanes = ["main"]')
            if not rw.approval_group:
                err("FG019", p, "prod-tier runways need an approval_group")
            if not rw.rehearsal:
                err("FG019", p, "prod-tier runways must rehearse on a clone (rehearsal = true)")
            if not rw.promote_from:
                err("FG019", p, "prod-tier runways need promote_from")
        if rid == "PRD" and not (rw.four_eyes and rw.ticket_required):
            err("FG019", p, "PRD requires four_eyes = true and ticket_required = true")
        if rw.lock in locks:
            err("FG019", p, f"lock {rw.lock!r} is also used by {locks[rw.lock]}")
        locks[rw.lock] = rid
        if rw.lock != rw.database:
            warn("FG019", p, f"lock {rw.lock!r} should equal the database name so every job serializes on it")
        for key in ("credentials_id", "service_user", "deploy_role", "deploy_warehouse", "jenkins_folder"):
            if not str(rw.raw.get(key, "")).strip():
                err("FG019", p, f"{key} is required")
        if rw.credentials_id and not rw.credentials_id.endswith(rid):
            warn("FG019", p, f"credentials_id {rw.credentials_id!r} should end with the runway id ({rid})")
    for key in ("flyway_version", "flyway_image", "python_image", "snowflake_cli", "auditor_role",
                "auditor_warehouse", "auditor_user", "auditor_credentials_id", "baseline_version"):
        if not str(kit.kit.get(key, "")).strip():
            err("FG019", "runways.toml [kit]", f"{key} is required")
    image = str(kit.kit.get("flyway_image", ""))
    if image and not image.endswith(":" + str(kit.kit.get("flyway_version", ""))):
        err("FG019", "runways.toml [kit]", "flyway_image tag must equal flyway_version (pin one exact version)")
    if image.endswith(":latest") or re.search(r":\d+(\.\d+)?$", image):
        err("FG019", "runways.toml [kit]", "flyway_image must pin an exact x.y.z tag, never latest or x / x.y")
    if str(kit.kit.get("history_schema", "FLYWAY")) != str(fw.get("defaultSchema", "")):
        err("FG019", "runways.toml [kit]", "history_schema must equal flyway.toml [flyway] defaultSchema")
    return out


def _check_env(env: dict[str, Any], name: str, rw: Runway, *, rehearsal: bool, err: Any,
               history_schema: str = "FLYWAY") -> None:
    p = f"flyway.toml [environments.{name}]"
    url = str(env.get("url", ""))
    want_db = "${env.FLYWAY_RH_DB}" if rehearsal else rw.database
    if _url_param(url, "db") != want_db:
        err("FG018", p, f"url must select db={want_db}")
    if "${env.SNOWFLAKE_ACCOUNT}" not in url:
        err("FG018", p, "url must take the account from ${env.SNOWFLAKE_ACCOUNT}")
    if _url_param(url, "JDBC_QUERY_RESULT_FORMAT") != "JSON":
        err("FG018", p, "url must set JDBC_QUERY_RESULT_FORMAT=JSON (Snowflake driver on Java 16+)")
    for key in ("role", "warehouse"):
        val = _url_param(url, key) or ""
        if not val.startswith("${env."):
            err("FG018", p, f"url {key}= must come from an ${{env.*}} resolver (deploy vs auditor logins differ)")
    if not str(env.get("user", "")).startswith("${env."):
        err("FG018", p, "user must come from an ${env.*} resolver")
    if "password" in {k.lower() for k in env}:
        err("FG018", p, "no password: key-pair authentication only")
    props = env.get("jdbcProperties", {})
    if str(props.get("authenticator", "")).lower() != "snowflake_jwt":
        err("FG018", p, "jdbcProperties.authenticator must be snowflake_jwt (key-pair)")
    if "private_key_file" not in props:
        err("FG018", p, "jdbcProperties.private_key_file is required (key-pair authentication)")
    for key, val in props.items():
        if key != "authenticator" and not str(val).startswith("${env."):
            err("FG018", p, f"jdbcProperties.{key} must come from an ${{env.*}} resolver, never a literal")
    if env.get("schemas", [history_schema]) != [history_schema]:
        err("FG018", p, f'schemas must be ["{history_schema}"] (the history schema only)')
    fwe = env.get("flyway", {})
    for key in fwe:
        if key not in ENV_FLYWAY_KEYS:
            err("FG018", p, f"flyway.{key} may not be overridden per environment (allowed: "
                            f"{', '.join(sorted(ENV_FLYWAY_KEYS))}); it would silently change this runway's safety")
    if fwe.get("cleanDisabled", True) is not True:
        err("FG018", p, "cleanDisabled must not be overridden to false on a runway")
    if fwe.get("baselineOnMigrate", False) is not False:
        err("FG018", p, "baselineOnMigrate must stay false")
    if fwe.get("outOfOrder", True) is not True:
        err("FG018", p, "outOfOrder must be true (timestamp versions merge out of author order)")
    want_pat = NONPROD_PATTERNS if (rw.tier == "nonprod" and not rehearsal) else PROD_PATTERNS
    if list(fwe.get("ignoreMigrationPatterns", [])) != want_pat:
        err("FG018", p, f"ignoreMigrationPatterns must be {want_pat}")
    ph = {k.lower(): str(v) for k, v in fwe.get("placeholders", {}).items()}
    want = {
        "env": rw.id,
        "edw_db": rw.rehearsal_db if rehearsal else rw.database,
        "home_db": rw.database,
    }
    for key, val in want.items():
        if ph.get(key) != val:
            err("FG018", p, f"placeholder {key} must be {val!r} (found {ph.get(key)!r})")
    if ph.get("tasks_enabled") not in ("true", "false"):
        err("FG018", p, "placeholder tasks_enabled must be 'true' or 'false'")
    for key in PER_DEPLOY_PLACEHOLDERS:
        if key not in ph:
            err("FG018", p, f"placeholder {key} needs a default here: an environment's placeholders table replaces the "
                            "root one, and the callbacks use it")
    if rehearsal and ph.get("tasks_enabled") != "false":
        err("FG018", p, "a rehearsal environment must set tasks_enabled = 'false'")
