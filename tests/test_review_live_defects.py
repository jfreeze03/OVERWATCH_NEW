"""Regression locks for the three live defects the Codex-review audit surfaced (v4.429.0).

CD-1  Cost ▸ Optimize storage-mover tiles byte-humanize like their sibling table
CD-2  validate.sql's migration-floor message is coherent (enforces 88, says 88)
CD-3  Admin verified-savings card renders through format_usd, like Brief / Decision Studio
"""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def _imports(src: str) -> str:
    return src.split("from app.logic.formulas import", 1)[1].split("\n", 1)[0]


# --- CD-1: storage-mover tiles format via unit= (no raw "%.2f TB") ----------------------
def test_storage_mover_tiles_byte_humanize():
    s = _src("app/ui/pages/cost_parts/optimize.py")
    # migrated to the canonical unit API (root-cause fix): raw TiB value + unit="tb"
    assert '"value": float(movers[\'CURRENT_TB\'].sum()), "unit": "tb"' in s
    assert '"value": float(movers[\'GROWTH_TB\'].sum()), "unit": "tb"' in s
    # the raw fixed-decimal-TB formatting that read "0.03 TB" is gone
    assert "CURRENT_TB'].sum()):,.2f}" not in s
    assert "GROWTH_TB'].sum()):,.2f}" not in s


# --- CD-3: admin verified-savings uses format_usd (matches Brief / Decision Studio) -----
def test_admin_verified_savings_uses_format_usd():
    a = _src("app/ui/pages/admin.py")
    assert "format_usd(float(a.get('VERIFIED_USD') or 0))" in a
    assert "${float(a.get('VERIFIED_USD')" not in a          # the raw ${:,.0f} is gone
    assert "format_usd" in _imports(a)


# --- CD-2: validate.sql teeth floor + message are coherent -----------------------------
def test_validate_teeth_floor_message_is_coherent():
    v = _src("snowflake/validate.sql")
    # the deliberate 88 floor (guardrail: "teeth-floor stays 88") is preserved
    assert "WHERE VERSION BETWEEN 1 AND 88" in v
    assert "IF (n_versions < 88) THEN RAISE e_migrations" in v
    # the raise message now references the enforced floor (88), not a stale V-number
    emsg = v.split("e_migrations", 1)[1].split("');", 1)[0]
    assert "88" in emsg
    assert "V117" not in emsg
    # the rotted "V001..V117 all applied" comment is gone
    assert "V001..V117 all applied" not in v
