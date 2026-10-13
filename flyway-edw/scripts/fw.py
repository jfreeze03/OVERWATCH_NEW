#!/usr/bin/env python3
"""EDW Flyway kit command line (stdlib only, Python 3.11+). Run from the kit root, or pass --root.

  guard          static + git-aware rules FG001-FG020          (PR builds, every deploy, developers locally)
  new            scaffold a correctly named migration file
  bundle         build the immutable deploy bundle (main/branch builds)
  verify-bundle  extract + re-hash a bundle before deploying it
  env            print a runway's pipeline settings (shell / properties / json)
  promote-check  has this bundle run cleanly on a predecessor runway?
  drift-sql      the INFORMATION_SCHEMA drift query for a runway
  drift-eval     apply the runway's drift policy to that query's rows
  rehearsal-sql  CREATE / DROP SQL for the rehearsal clone ALFA_EDW_RH_<ID>

Exit codes: 0 ok, 1 a check failed, 2 usage or configuration error.
"""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fwkit import bundle, drift, promote, scaffold
from fwkit.guard import Guard, GuardUsageError, findings_json, junit_xml
from fwkit.manifest import ConfigError, Kit, Runway, load_kit

KIT_ROOT = Path(__file__).resolve().parent.parent


def _kit(args: argparse.Namespace) -> Kit:
    return load_kit(Path(args.root))


def cmd_guard(args: argparse.Namespace) -> int:
    kit = _kit(args)
    g = Guard(kit, base=args.base)
    findings = g.run()
    for f in findings:
        print(f.render())
    errors = [f for f in findings if f.severity == "error" or (args.strict and f.severity == "warning")]
    if args.junit:
        Path(args.junit).parent.mkdir(parents=True, exist_ok=True)
        Path(args.junit).write_text(junit_xml(findings, len(g.files)), encoding="utf-8")
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(findings_json(findings), encoding="utf-8")
    warnings = sum(1 for f in findings if f.severity == "warning")
    print(f"guard: {len(g.files)} SQL files, {len(errors)} error(s), {warnings} warning(s)"
          + (f" (base {args.base})" if args.base else " (no --base: FG003 immutability not checked)"))
    return 1 if errors else 0


def cmd_new(args: argparse.Namespace) -> int:
    root = Path(args.root)
    if args.repeatable:
        if args.layer is None or not args.object:
            raise scaffold.ScaffoldError("--repeatable needs --layer and --object")
        paths = [scaffold.new_repeatable(root, args.schema, args.layer, args.object)]
    else:
        if not args.desc:
            raise scaffold.ScaffoldError("--desc is required for a versioned migration")
        at = datetime.fromisoformat(args.at) if args.at else None
        if at is not None and at.tzinfo is None:
            at = at.replace(tzinfo=UTC)  # a naive --at is UTC, like the versions themselves
        paths = scaffold.new_versioned(root, args.schema, args.desc, at=at, undo=args.undo)
    for p in paths:
        print(p.relative_to(root.resolve()) if p.is_absolute() and p.is_relative_to(root.resolve()) else p)
    return 0


def cmd_bundle(args: argparse.Namespace) -> int:
    res = bundle.build(Path(args.root), Path(args.out), build_id=args.build_id, git_sha=args.git_sha,
                       branch=args.branch, build_url=args.build_url or "")
    m = res.manifest
    print(f"bundle {res.archive} digest {m['digest']} ({len(m['files'])} files, {len(m['versioned'])} versioned, "
          f"{len(m['repeatable'])} repeatable) branch {m['branch']} sha {m['git_sha']}")
    return 0


def cmd_verify_bundle(args: argparse.Namespace) -> int:
    bdir = bundle.extract(Path(args.bundle), Path(args.extract))
    m = bundle.verify(bdir, require_branch=args.require_branch)
    print(f"verified {bdir} digest {m['digest']} build {m['build_id']} branch {m['branch']} sha {m['git_sha']}")
    if args.manifest_out:
        Path(args.manifest_out).write_text(json.dumps(m, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


def runway_settings(kit: Kit, rw: Runway, *, auditor: bool) -> dict[str, str]:
    """Pipeline settings for one runway. Pipeline keys are EDW_*: Flyway itself reads FLYWAY_<SETTING> environment
    variables (ConfigUtils.convertKey: FLYWAY_TARGET, FLYWAY_ENVIRONMENT, FLYWAY_BASELINE_VERSION, ...) as its own
    configuration, so exporting those names would silently retarget a run. FLYWAY_SF_* / FLYWAY_RH_DB are not Flyway
    settings; flyway.toml reads them through ${env.*} resolvers."""
    k = kit.kit
    s = {
        "EDW_TARGET": rw.id,
        "EDW_ENVIRONMENT": rw.id,
        "EDW_DATABASE": rw.database,
        "EDW_TIER": rw.tier,
        "EDW_LOCK": rw.lock,
        "EDW_LANES": ",".join(rw.lanes),
        "EDW_PROMOTE_FROM": ",".join(rw.promote_from),
        "EDW_APPROVAL_GROUP": rw.approval_group,
        "EDW_FOUR_EYES": str(rw.four_eyes).lower(),
        "EDW_TICKET_REQUIRED": str(rw.ticket_required).lower(),
        "EDW_REHEARSAL": str(rw.rehearsal).lower(),
        "EDW_RH_ENVIRONMENT": rw.rehearsal_env if rw.rehearsal else "",
        "FLYWAY_RH_DB": rw.rehearsal_db if rw.rehearsal else "",
        "EDW_DRIFT_GATE": rw.drift_gate,
        "EDW_SAVE_SNAPSHOT": str(rw.save_snapshot).lower(),
        "EDW_PREVIEW_CHECKS": str(rw.preview_checks).lower(),
        "EDW_CREDENTIALS_ID": str(k.get("auditor_credentials_id")) if auditor else rw.credentials_id,
        "FLYWAY_SF_USER": str(k.get("auditor_user")) if auditor else rw.service_user,
        "FLYWAY_SF_ROLE": str(k.get("auditor_role")) if auditor else rw.deploy_role,
        "FLYWAY_SF_WAREHOUSE": str(k.get("auditor_warehouse")) if auditor else rw.deploy_warehouse,
        "EDW_AUDITOR_CREDENTIALS_ID": str(k.get("auditor_credentials_id", "")),
        "EDW_AUDITOR_USER": str(k.get("auditor_user", "")),
        "EDW_AUDITOR_ROLE": str(k.get("auditor_role", "")),
        "EDW_AUDITOR_WAREHOUSE": str(k.get("auditor_warehouse", "")),
        "EDW_JENKINS_FOLDER": rw.jenkins_folder,
        "EDW_FLYWAY_VERSION": str(k.get("flyway_version", "")),
        "EDW_FLYWAY_IMAGE": str(k.get("flyway_image", "")),
        "EDW_PYTHON_IMAGE": str(k.get("python_image", "")),
        "EDW_SNOWFLAKE_CLI": str(k.get("snowflake_cli", "")),
        "EDW_MAIN_BRANCH": str(k.get("main_branch", "main")),
        "EDW_HISTORY_SCHEMA": kit.history_schema,
        "EDW_BASELINE_VERSION": str(k.get("baseline_version", "")),
    }
    return s


def cmd_env(args: argparse.Namespace) -> int:
    kit = _kit(args)
    s = runway_settings(kit, kit.runway(args.runway), auditor=args.auditor)
    if args.format == "json":
        print(json.dumps(s, indent=2, sort_keys=True))
    elif args.format == "properties":
        for key, val in s.items():
            print(f"{key}={val}")
    else:
        for key, val in s.items():
            print(f"export {key}={shlex.quote(val)}")
    return 0


def cmd_promote_check(args: argparse.Namespace) -> int:
    kit = _kit(args)
    rw = kit.runway(args.runway)
    infos: dict[str, Path] = {}
    for item in args.info or []:
        name, sep, path = item.partition("=")
        if not sep:
            raise ConfigError(f"--info expects RUNWAY=path, got {item!r}")
        infos[name.upper()] = Path(path)
    manifest = json.loads(Path(args.bundle_manifest).read_text(encoding="utf-8"))
    ok, verdicts = promote.check(rw.id, rw.promote_from, infos, manifest)
    if not rw.promote_from:
        print(f"promote-check {rw.id}: no predecessor required")
        return 0
    for v in verdicts:
        status = "PASS" if v.ok else "FAIL"
        print(f"{status} {v.runway}: {v.checked_versioned} versioned + {v.checked_repeatable} repeatable checked")
        for p in v.problems[:50]:
            print(f"    {p}")
    print(f"promote-check {rw.id}: {'PASS' if ok else 'FAIL'} (needs any one of {', '.join(rw.promote_from)})")
    return 0 if ok else 1


def cmd_drift_sql(args: argparse.Namespace) -> int:
    kit = _kit(args)
    print(drift.drift_sql(kit, kit.runway(args.runway)), end="")
    return 0


def cmd_drift_eval(args: argparse.Namespace) -> int:
    kit = _kit(args)
    rw = kit.runway(args.runway)
    rows = drift.evaluate(kit, rw, drift.load_rows(Path(args.rows)))
    if not rows:
        print(f"drift {rw.id}: checked, clean (no out-of-band DDL since the last successful migration)")
        return 0
    print(f"drift {rw.id}: {len(rows)} object(s) changed outside Flyway since the last deploy:")
    for r in rows:
        print(f"    {r.render()}")
    if rw.drift_gate == "fail" and not args.ack:
        print(f"drift gate is 'fail' for {rw.id}: reconcile (docs/RUNBOOKS.md R5) or re-run with ACK_DRIFT=true")
        return 1
    print(f"drift gate is '{rw.drift_gate}'" + (" (acknowledged)" if args.ack else "") + ": continuing")
    return 0


def cmd_rehearsal_sql(args: argparse.Namespace) -> int:
    kit = _kit(args)
    print(drift.rehearsal_sql(kit, kit.runway(args.runway), args.action), end="")
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="fw.py", description=__doc__.split("\n\n", 1)[0])
    p.add_argument("--root", default=str(KIT_ROOT), help="kit root (default: the folder above scripts/)")
    sub = p.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("guard", help="static + git-aware rules FG001-FG020")
    g.add_argument("--base", help="git ref to compare applied migrations against (e.g. origin/main)")
    g.add_argument("--junit", help="write JUnit XML here")
    g.add_argument("--json", help="write findings JSON here")
    g.add_argument("--strict", action="store_true", help="treat warnings as errors")
    g.set_defaults(fn=cmd_guard)

    n = sub.add_parser("new", help="scaffold a migration")
    n.add_argument("--schema", required=True)
    n.add_argument("--desc", help="versioned: lower_snake_case description (schema prefix added)")
    n.add_argument("--undo", action="store_true", help="also create the U undo script")
    n.add_argument("--at", help="ISO timestamp for the version, UTC unless it carries an offset (default: now)")
    n.add_argument("--repeatable", action="store_true")
    n.add_argument("--layer", type=int, help="repeatable layer 10-99 (see docs/CONVENTIONS.md)")
    n.add_argument("--object", help="repeatable: lower_snake_case object name")
    n.set_defaults(fn=cmd_new)

    b = sub.add_parser("bundle", help="build the immutable deploy bundle")
    b.add_argument("--out", required=True)
    b.add_argument("--build-id", required=True)
    b.add_argument("--git-sha", required=True)
    b.add_argument("--branch", required=True)
    b.add_argument("--build-url")
    b.set_defaults(fn=cmd_bundle)

    v = sub.add_parser("verify-bundle", help="extract and re-hash a bundle")
    v.add_argument("--bundle", required=True)
    v.add_argument("--extract", required=True)
    v.add_argument("--require-branch", help="refuse bundles not built from this branch (MGM/PRD: main)")
    v.add_argument("--manifest-out", help="also write the verified manifest here")
    v.set_defaults(fn=cmd_verify_bundle)

    e = sub.add_parser("env", help="print a runway's pipeline settings")
    e.add_argument("--runway", required=True)
    e.add_argument("--format", choices=("shell", "properties", "json"), default="shell")
    e.add_argument("--auditor", action="store_true", help="use the read-only auditor login (info/validate)")
    e.set_defaults(fn=cmd_env)

    pc = sub.add_parser("promote-check", help="has the bundle run cleanly on a predecessor?")
    pc.add_argument("--runway", required=True)
    pc.add_argument("--info", action="append", help="PREDECESSOR=path to its `flyway info -outputType=json`")
    pc.add_argument("--bundle-manifest", required=True)
    pc.set_defaults(fn=cmd_promote_check)

    ds = sub.add_parser("drift-sql", help="print the drift query for a runway")
    ds.add_argument("--runway", required=True)
    ds.set_defaults(fn=cmd_drift_sql)

    de = sub.add_parser("drift-eval", help="apply the drift policy to query rows")
    de.add_argument("--runway", required=True)
    de.add_argument("--rows", required=True, help="JSON rows from `snow sql --format json`")
    de.add_argument("--ack", action="store_true", help="an approver acknowledged the drift (ACK_DRIFT)")
    de.set_defaults(fn=cmd_drift_eval)

    rs = sub.add_parser("rehearsal-sql", help="SQL to create/drop the rehearsal clone")
    rs.add_argument("--runway", required=True)
    rs.add_argument("--action", choices=("create", "drop"), required=True)
    rs.set_defaults(fn=cmd_rehearsal_sql)
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return int(args.fn(args))
    except (ConfigError, GuardUsageError, scaffold.ScaffoldError) as exc:
        print(f"fw.py: {exc}", file=sys.stderr)
        return 2
    except (bundle.BundleError, drift.DriftError, ValueError) as exc:
        print(f"fw.py: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
