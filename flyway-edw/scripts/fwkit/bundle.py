"""Build once, promote the same bytes.

``build`` packs exactly what a deploy needs into a deterministic tar.gz with a manifest of SHA-256 per file and one
digest over all of them; Jenkins archives it with a fingerprint. ``verify`` re-hashes an extracted bundle before any
deploy and refuses a bundle that was altered, has extra or missing files, or (for MGM/PRD) was not built from main.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import re
import subprocess
import tarfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from .guard import B_RE, R_RE, V_RE, flyway_description

PAYLOAD = ("flyway.toml", "runways.toml", "sqlfluff.cfg", "rules", "migrations", "repeatable", "callbacks", "scripts")
TOP = "bundle"
MANIFEST = "bundle-manifest.json"
SKIP_PARTS = {"__pycache__", ".pytest_cache", ".mypy_cache"}


class BundleError(Exception):
    """The bundle is invalid, tampered with or not allowed for this runway."""


@dataclass(frozen=True)
class BuildResult:
    archive: Path
    manifest_path: Path
    manifest: dict[str, Any]


def _payload_files(root: Path) -> list[tuple[str, Path]]:
    out: list[tuple[str, Path]] = []
    for item in PAYLOAD:
        p = root / item
        if p.is_file():
            out.append((item, p))
        elif p.is_dir():
            for f in sorted(p.rglob("*")):
                rel = f.relative_to(root)
                if f.is_file() and not (SKIP_PARTS & set(rel.parts)) and f.suffix != ".pyc":
                    out.append((rel.as_posix(), f))
    if not any(r == "flyway.toml" for r, _ in out):
        raise BundleError(f"{root} is not a kit root (no flyway.toml)")
    return sorted(out)


def digest_of(files: dict[str, str]) -> str:
    h = hashlib.sha256()
    for rel in sorted(files):
        h.update(f"{rel}\t{files[rel]}\n".encode())
    return h.hexdigest()


def _should_execute(root: Path, rel: str) -> str:
    conf = root / (rel + ".conf")
    if not conf.is_file():
        return ""
    for line in conf.read_text(encoding="utf-8").splitlines():
        key, sep, val = line.strip().partition("=")
        if sep and key.strip() == "shouldExecute":
            return val.strip()
    return ""


def _inventory(root: Path, files: dict[str, str]) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Versioned + repeatable migrations in the bundle; `should_execute` records a .sql.conf shouldExecute expression
    so the promotion gate can tell a deliberate per-runway skip ("Ignored") from a migration that never ran."""
    versioned: list[dict[str, str]] = []
    repeatable: list[dict[str, str]] = []
    for rel in sorted(files):
        name = PurePosixPath(rel).name
        if rel.startswith("migrations/"):
            m = V_RE.match(name)
            b = B_RE.match(name)
            if m and m.group("prefix") == "V":
                version = ".".join(x for x in (m.group("y"), m.group("mo"), m.group("d"), m.group("hm"),
                                               m.group("seq")) if x is not None)
                item = {"version": version, "description": flyway_description(name), "path": rel}
                if expr := _should_execute(root, rel):
                    item["should_execute"] = expr
                versioned.append(item)
            elif b:
                versioned.append({"version": b.group("ver"), "description": flyway_description(name), "path": rel,
                                  "baseline": "true"})
        elif rel.startswith("repeatable/") and R_RE.match(name):
            item = {"description": flyway_description(name), "path": rel}
            if expr := _should_execute(root, rel):
                item["should_execute"] = expr
            repeatable.append(item)
    return versioned, repeatable


def build(root: Path, out_dir: Path, *, build_id: str, git_sha: str, branch: str, build_url: str = "",
          created: datetime | None = None) -> BuildResult:
    import tomllib

    files_list = _payload_files(root)
    files = {rel: hashlib.sha256(p.read_bytes()).hexdigest() for rel, p in files_list}
    versioned, repeatable = _inventory(root, files)
    with (root / "runways.toml").open("rb") as fh:
        kit = tomllib.load(fh).get("kit", {})
    manifest: dict[str, Any] = {
        "format": 1,
        "build_id": build_id,
        "git_sha": git_sha,
        "branch": branch,
        "build_url": build_url,
        "created_utc": (created or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "flyway_version": kit.get("flyway_version", ""),
        "digest": digest_of(files),
        "files": files,
        "versioned": versioned,
        "repeatable": repeatable,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    archive = out_dir / f"edw-flyway-bundle-{build_id}.tar.gz"
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for rel, p in files_list:
            _add(tar, f"{TOP}/{rel}", p.read_bytes(), 0o755 if p.suffix in (".py", ".sh") else 0o644)
        _add(tar, f"{TOP}/{MANIFEST}", (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode(), 0o644)
    with archive.open("wb") as out, gzip.GzipFile(filename="", mode="wb", fileobj=out, mtime=0) as gz:
        gz.write(raw.getvalue())
    manifest_path = out_dir / MANIFEST
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return BuildResult(archive, manifest_path, manifest)


def _add(tar: tarfile.TarFile, name: str, data: bytes, mode: int) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mtime = 0
    info.mode = mode
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    tar.addfile(info, io.BytesIO(data))


def extract(archive: Path, dest: Path) -> Path:
    """Extract safely: regular files only, no absolute paths, no '..', everything under bundle/."""
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:gz") as tar:
        for m in tar.getmembers():
            parts = PurePosixPath(m.name).parts
            if not m.isfile() or m.name.startswith("/") or ".." in parts or not parts or parts[0] != TOP:
                raise BundleError(f"refusing unsafe archive member {m.name!r}")
        tar.extractall(dest, filter="data")
    return dest / TOP


def verify(bundle_dir: Path, *, require_branch: str | None = None) -> dict[str, Any]:
    mpath = bundle_dir / MANIFEST
    if not mpath.exists():
        raise BundleError(f"{mpath} missing: not a bundle")
    manifest = json.loads(mpath.read_text(encoding="utf-8"))
    files: dict[str, str] = manifest.get("files", {})
    actual: dict[str, str] = {}
    for f in sorted(bundle_dir.rglob("*")):
        if not f.is_file():
            continue
        rel = f.relative_to(bundle_dir).as_posix()
        if rel == MANIFEST or SKIP_PARTS & set(PurePosixPath(rel).parts) or f.suffix == ".pyc":
            continue
        actual[rel] = hashlib.sha256(f.read_bytes()).hexdigest()
    missing = sorted(set(files) - set(actual))
    extra = sorted(set(actual) - set(files))
    changed = sorted(r for r in set(files) & set(actual) if files[r] != actual[r])
    problems = [f"missing {r}" for r in missing] + [f"unexpected {r}" for r in extra] + \
               [f"modified {r}" for r in changed]
    if digest_of(files) != manifest.get("digest"):
        problems.append("manifest digest does not match its own file list")
    if not problems:
        versioned, repeatable = _inventory(bundle_dir, actual)
        if versioned != manifest.get("versioned") or repeatable != manifest.get("repeatable"):
            problems.append("manifest migration inventory does not match the files (promote-check reads it)")
    if problems:
        raise BundleError("bundle verification failed: " + "; ".join(problems[:20]))
    if require_branch and manifest.get("branch") != require_branch:
        raise BundleError(f"bundle was built from {manifest.get('branch')!r}; this runway accepts only "
                          f"{require_branch!r} builds")
    return dict(manifest)


def verify_against_git(manifest: dict[str, Any], git_dir: Path, *, prefix: str = "",
                       require_ancestor: str | None = None) -> int:
    """Prove the bundle IS its commit: every file byte-identical to the git blob at manifest['git_sha'], and the same
    file set. A bundle built on a shared CI agent could be altered (with a self-consistent manifest) before it was
    hashed; the repository, read by the deploy job itself, cannot. With `require_ancestor` (MGM/PRD: origin/main) the
    commit must also be on that branch. Returns the number of files compared."""
    sha = str(manifest.get("git_sha", ""))
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise BundleError(f"manifest git_sha {sha!r} is not a full commit hash")

    def git(*args: str, data: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(["git", "-C", str(git_dir), *args], input=data, capture_output=True, check=False)

    if git("cat-file", "-e", f"{sha}^{{commit}}").returncode != 0:
        raise BundleError(f"commit {sha} is not in {git_dir} (fetch it first); the bundle cannot be traced to git")
    if require_ancestor and git("merge-base", "--is-ancestor", sha, require_ancestor).returncode != 0:
        raise BundleError(f"commit {sha} is not on {require_ancestor}: refused")
    pre = prefix.strip("/")
    pre = pre + "/" if pre else ""
    listing = git("ls-tree", "-r", "--name-only", sha, "--", *[pre + item for item in PAYLOAD])
    if listing.returncode != 0:
        raise BundleError(f"git ls-tree failed: {listing.stderr.decode('utf-8', 'replace').strip()}")
    in_git = set()
    for line in listing.stdout.decode("utf-8", "replace").splitlines():
        rel = line[len(pre):] if line.startswith(pre) else line
        if not (SKIP_PARTS & set(PurePosixPath(rel).parts)) and not rel.endswith(".pyc"):
            in_git.add(rel)
    files: dict[str, str] = manifest.get("files", {})
    problems = [f"not in commit: {r}" for r in sorted(set(files) - in_git)]
    problems += [f"in commit but not in bundle: {r}" for r in sorted(in_git - set(files))]
    names = sorted(set(files) & in_git)
    batch = git("cat-file", "--batch", data="".join(f"{sha}:{pre}{r}\n" for r in names).encode())
    out, pos = batch.stdout, 0
    for rel in names:
        header_end = out.index(b"\n", pos)
        header = out[pos:header_end].decode("utf-8", "replace").split()
        if len(header) != 3 or header[1] != "blob":
            problems.append(f"cannot read {rel} at {sha}")
            pos = header_end + 1
            continue
        size = int(header[2])
        blob = out[header_end + 1:header_end + 1 + size]
        pos = header_end + 1 + size + 1
        if hashlib.sha256(blob).hexdigest() != files[rel]:
            problems.append(f"differs from the commit: {rel}")
    if problems:
        raise BundleError("bundle does not match commit " + sha[:12] + ": " + "; ".join(problems[:20]))
    return len(names)


def verify_against_archive(manifest: dict[str, Any], archive: Path, *, prefix: str = "") -> int:
    """Same proof as verify_against_git, from `git archive --format=tar <git_sha>` made on the agent host (whose git
    the deploy job's checkout already uses), for containers that have no git. The ancestry check then runs on the
    host: `git merge-base --is-ancestor <git_sha> HEAD`."""
    pre = prefix.strip("/")
    pre = pre + "/" if pre else ""
    in_git: dict[str, str] = {}
    with tarfile.open(archive, "r:*") as tar:
        for member in tar.getmembers():
            if not member.isfile() or not member.name.startswith(pre):
                continue
            rel = member.name[len(pre):]
            if rel.split("/", 1)[0] not in PAYLOAD or SKIP_PARTS & set(PurePosixPath(rel).parts) or rel.endswith(".pyc"):
                continue
            fh = tar.extractfile(member)
            if fh is not None:
                in_git[rel] = hashlib.sha256(fh.read()).hexdigest()
    files: dict[str, str] = manifest.get("files", {})
    problems = [f"not in commit: {r}" for r in sorted(set(files) - set(in_git))]
    problems += [f"in commit but not in bundle: {r}" for r in sorted(set(in_git) - set(files))]
    problems += [f"differs from the commit: {r}" for r in sorted(set(files) & set(in_git)) if files[r] != in_git[r]]
    if problems:
        raise BundleError("bundle does not match commit " + str(manifest.get("git_sha", ""))[:12] + ": "
                          + "; ".join(problems[:20]))
    return len(files)
