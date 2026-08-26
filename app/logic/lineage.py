"""Pure object-lineage blast-radius (Streamlit-free, unit-tested).

Pairs the DECLARED dependency graph (ACCOUNT_USAGE.OBJECT_DEPENDENCIES — mostly
view/matview/policy references, NOT stored-proc bodies or dynamic SQL) with the
OBSERVED consumers (ACCESS_HISTORY) to answer "if I ALTER this object — or it
breaks — what depends on it, and who actually touched those?".

Every number here is a COUNT of a recorded dependent or an observed consumer over
a window — never a prediction of what WILL break. A dependent never seen in
ACCESS_HISTORY is NOT-MEASURED, never a measured 0 (same discipline as
decision.prioritize_workloads). The graph walk is a pure BFS so it is unit-tested
and cycle-safe.
"""

from __future__ import annotations

import pandas as pd


def _norm(value: object) -> str:
    return str(value or "").strip().upper()


def downstream_dependents(edges: pd.DataFrame | None, root_fqn: str,
                          *, max_depth: int = 8) -> pd.DataFrame:
    """BFS the REFERENCED->REFERENCING edge frame from ``root_fqn`` to its transitive
    downstream dependents (the objects that REFERENCE it, directly or through a chain
    — the ones that break if it changes). Returns FQN / DEPTH (min hops from root) /
    DOMAIN, nearest first. Cycle-safe (visited set) and depth-capped. Empty in ->
    empty out; never raises."""
    root = _norm(root_fqn)
    cols = ["FQN", "DEPTH", "DOMAIN"]
    if edges is None or edges.empty or not root:
        return pd.DataFrame(columns=cols)
    ref = edges.get("REFERENCED_FQN")
    ing = edges.get("REFERENCING_FQN")
    if ref is None or ing is None:
        return pd.DataFrame(columns=cols)
    dom = edges.get("REFERENCING_DOMAIN", pd.Series("", index=edges.index))

    # adjacency: a REFERENCED object -> the objects that REFERENCE it (its dependents)
    adj: dict[str, list[tuple[str, str]]] = {}
    for r, g, d in zip(ref.map(_norm), ing.map(_norm), dom.astype(str), strict=False):
        if r and g:
            adj.setdefault(r, []).append((g, d))

    depth: dict[str, int] = {}
    domain: dict[str, str] = {}
    seen = {root}
    frontier = [root]
    level = 0
    while frontier and level < max_depth:
        level += 1
        nxt: list[str] = []
        for node in frontier:
            for child, dchild in adj.get(node, []):
                if child == root:            # a cycle back to the root is not a dependent
                    continue
                if child not in depth:       # first time reached = shortest depth
                    depth[child] = level
                    domain[child] = dchild
                if child not in seen:
                    seen.add(child)
                    nxt.append(child)
        frontier = nxt
    if not depth:
        return pd.DataFrame(columns=cols)
    out = pd.DataFrame({
        "FQN": list(depth),
        "DEPTH": [depth[k] for k in depth],
        "DOMAIN": [domain.get(k, "") for k in depth],
    })
    return out.sort_values(["DEPTH", "FQN"]).reset_index(drop=True)


def build_blast_radius(edges: pd.DataFrame | None, consumers: pd.DataFrame | None,
                       root_fqn: str, *, window_days: int) -> pd.DataFrame:
    """Downstream dependents joined to their OBSERVED consumer counts. A dependent
    never seen in ACCESS_HISTORY is MEASURED=False with QUERIES/USERS as NA — never a
    measured 0. Attention-first: measured (queried) dependents before un-queried, then
    by query volume, then by depth. Empty in -> empty out; never raises."""
    deps = downstream_dependents(edges, root_fqn)
    if deps.empty:
        return deps
    out = deps.copy()
    cons = consumers.copy() if (consumers is not None and not consumers.empty) else pd.DataFrame()
    if not cons.empty and "FQN" in cons.columns:
        cons["_K"] = cons["FQN"].map(_norm)
        # Default Series (like LAST_TOUCH) so a consumers frame missing QUERIES/USERS
        # degrades to NA rather than raising — pd.to_numeric(None) is a bare scalar NaN
        # whose zip() would blow up, breaking the "never raises" contract.
        q = dict(zip(cons["_K"], pd.to_numeric(cons.get("QUERIES", pd.Series(index=cons.index)),
                                               errors="coerce"), strict=False))
        u = dict(zip(cons["_K"], pd.to_numeric(cons.get("USERS", pd.Series(index=cons.index)),
                                               errors="coerce"), strict=False))
        lt = dict(zip(cons["_K"], cons.get("LAST_TOUCH", pd.Series(index=cons.index)), strict=False))
        out["QUERIES"] = out["FQN"].map(q)
        out["USERS"] = out["FQN"].map(u)
        out["LAST_TOUCH"] = out["FQN"].map(lt)
    else:
        out["QUERIES"] = pd.NA
        out["USERS"] = pd.NA
        out["LAST_TOUCH"] = pd.NA
    out["MEASURED"] = out["QUERIES"].notna()
    out["_q"] = pd.to_numeric(out["QUERIES"], errors="coerce").fillna(-1.0)
    return (out.sort_values(["MEASURED", "_q", "DEPTH"], ascending=[False, False, True])
            .drop(columns="_q").reset_index(drop=True))


def blast_summary(edges: pd.DataFrame | None, consumers: pd.DataFrame | None,
                  root_fqn: str, *, window_days: int) -> dict:
    """Headline counts for the blast radius — all counts of RECORDED/OBSERVED facts,
    never a prediction. ``measured`` = dependents seen in ACCESS_HISTORY;
    ``observed_queries`` = total observed queries across those dependents."""
    br = build_blast_radius(edges, consumers, root_fqn, window_days=window_days)
    if br.empty:
        return {"dependents": 0, "measured": 0, "unmeasured": 0,
                "observed_queries": 0, "deepest_level": 0}
    measured = br[br["MEASURED"]]
    return {
        "dependents": len(br),
        "measured": len(measured),
        "unmeasured": int((~br["MEASURED"]).sum()),
        "observed_queries": int(pd.to_numeric(measured.get("QUERIES"), errors="coerce")
                                .fillna(0).sum()) if not measured.empty else 0,
        "deepest_level": int(pd.to_numeric(br["DEPTH"], errors="coerce").fillna(0).max()),
    }
