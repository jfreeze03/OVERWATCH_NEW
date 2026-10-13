"""Promotion gate: a bundle may deploy to a runway only after it ran cleanly on a predecessor runway.

Input is ``flyway info -outputType=json`` taken against each predecessor with the read-only auditor login, plus the
bundle manifest. Flyway's own ``validate`` (run by the pipeline against the same predecessor) proves the checksums of
VERSIONED migrations match; this check proves nothing is still pending, failed or outdated there.

Why repeatables are judged by state and not by checksum: Flyway checksums a repeatable AFTER placeholder replacement,
so the same file has a different checksum in every environment. "Success" (not "Outdated") in the predecessor means
the predecessor runs the same file revision with its own placeholder values.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .guard import version_key

# States (MigrationState.java display names) in which a versioned migration counts as delivered to a runway.
VERSIONED_OK = {"Success", "Out of Order", "Baseline", "Below Baseline", "Ignored (Baseline)"}
REPEATABLE_BAD = {"Outdated", "Pending", "Failed", "Failed (Missing)", "Failed (Future)"}


@dataclass
class Verdict:
    runway: str
    ok: bool
    problems: list[str] = field(default_factory=list)
    checked_versioned: int = 0
    checked_repeatable: int = 0


def load_info(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    # a composite result ({"individualResults": [...]}) when several verbs ran; pick the info result
    if isinstance(data, dict) and "migrations" not in data and isinstance(data.get("individualResults"), list):
        for item in data["individualResults"]:
            if isinstance(item, dict) and "migrations" in item:
                return item
    if not isinstance(data, dict) or "migrations" not in data:
        error = data.get("error") if isinstance(data, dict) else None
        raise ValueError(f"{path}: not a 'flyway info -outputType=json' result" + (f" (error: {error})" if error else ""))
    return data


def judge(runway: str, info: dict[str, Any], manifest: dict[str, Any]) -> Verdict:
    v = Verdict(runway, ok=True)
    rows = info.get("migrations") or []
    by_version: dict[tuple[int, ...], list[str]] = {}
    by_desc: dict[str, list[str]] = {}
    for r in rows:
        cat = str(r.get("category", ""))
        state = str(r.get("state", ""))
        if cat in ("Versioned", "Baseline") and r.get("version"):
            by_version.setdefault(version_key(str(r["version"])), []).append(state)
        elif cat == "Repeatable":
            by_desc.setdefault(str(r.get("description", "")), []).append(state)
    baseline_rows = [r for r in rows if str(r.get("type", "")).upper() == "BASELINE" or r.get("state") == "Baseline"]
    baseline_key = max((version_key(str(r["version"])) for r in baseline_rows if r.get("version")), default=())
    for item in manifest.get("versioned", []):
        if item.get("baseline"):
            continue
        v.checked_versioned += 1
        key = version_key(item["version"])
        states = by_version.get(key)
        if not states:
            if baseline_key and key <= baseline_key:
                continue  # older than the runway's adoption baseline: covered by the baseline
            v.problems.append(f"V{item['version']} {item['description']}: not known to {runway}")
            continue
        if item.get("should_execute") and states and all(s == "Ignored" for s in states):
            continue  # skipped on that runway by its own shouldExecute expression: delivered as designed
        if not any(s in VERSIONED_OK for s in states):
            v.problems.append(f"V{item['version']} {item['description']}: {'/'.join(states)} on {runway}")
    for item in manifest.get("repeatable", []):
        v.checked_repeatable += 1
        states = [s for s in by_desc.get(item["description"], []) if s != "Superseded"]
        if item.get("should_execute") and states and all(s == "Ignored" for s in states):
            continue
        if not states:
            v.problems.append(f"R {item['description']}: never applied on {runway}")
        elif bad := sorted({s for s in states if s in REPEATABLE_BAD}):
            v.problems.append(f"R {item['description']}: {'/'.join(bad)} on {runway} (a different revision ran there)")
        elif "Success" not in states:
            v.problems.append(f"R {item['description']}: {'/'.join(states)} on {runway}")
    v.ok = not v.problems
    return v


def check(target: str, promote_from: tuple[str, ...], infos: dict[str, Path], manifest: dict[str, Any]
          ) -> tuple[bool, list[Verdict]]:
    """Any-of semantics: the bundle must be fully delivered to at least one predecessor."""
    if not promote_from:
        return True, []
    verdicts = []
    for pred in promote_from:
        if pred in infos:
            verdicts.append(judge(pred, load_info(infos[pred]), manifest))
        else:
            verdicts.append(Verdict(pred, ok=False, problems=[f"no info supplied for predecessor {pred}"]))
    return any(x.ok for x in verdicts), verdicts
