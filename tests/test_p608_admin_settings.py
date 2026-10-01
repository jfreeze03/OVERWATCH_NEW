"""v4.608 round-2 fixes in Admin > Settings (cluster e6): the editors write only what the readers use.

R2-105: CORTEX_MODEL is saved normalized (blank = the default), the one name both the app (normalize_model) and the
scheduled digest / anomaly-sweep procs (which read the raw VALUE, defaulting only on NULL) run.
R2-106: the four retention editors start at SP_PURGE_FACTS's GREATEST floors, and say a change applies at the next
monthly purge; COCO_DAILY_CAP_CREDITS says 0 means the default 15.
R2-107: CREDIT_PRICE_OVERRIDE (read only by validate.sql) is editable and never flagged "safe to delete"; the
compute rate cannot be 0, and a non-3.68 rate without the override warns before the save.
R2-110: DATA_TRANSFER_USD_PER_TB has a number editor (min 0), like every other numeric setting.
"""

from __future__ import annotations

import re

import pandas as pd

from app.config import DEFAULT_SETTINGS
from app.core.result import QueryResult
from tests._source import ROOT, read


class _St:
    """Records the widgets _setting_value_input / _settings_tab touch; text_input returns ``typed``."""

    def __init__(self, typed: str = "", key: str = "CREDIT_PRICE_USD", number: float | None = None):
        self.typed, self.key, self.number = typed, key, number
        self.calls: list[tuple[str, object, dict]] = []
        self.options: list = []

    def text_input(self, label, **k):
        self.calls.append(("text_input", label, k))
        return self.typed

    def number_input(self, label, **k):
        self.calls.append(("number_input", label, k))
        return k["value"] if self.number is None else self.number

    def selectbox(self, label, options, **k):
        self.calls.append(("selectbox", label, k))
        self.options.extend(options)
        return self.key if self.key in options else list(options)[k.get("index", 0)]

    def caption(self, text, *_a, **_k):
        self.calls.append(("caption", str(text), {}))

    def warning(self, text, *_a, **_k):
        self.calls.append(("warning", str(text), {}))

    def code(self, *_a, **_k):
        return None

    def texts(self, kind: str) -> list[str]:
        return [str(t) for k, t, _ in self.calls if k == kind]


def _value_input(monkeypatch, key: str, current: dict, **st_kw):
    from app.ui.pages import admin
    fake = _St(**st_kw)
    monkeypatch.setattr(admin, "st", fake)
    return admin._setting_value_input(key, current), fake


# ------------------------------------------------------------------------------ R2-105 ----

def test_cortex_model_blank_saves_the_default_never_an_empty_string(monkeypatch):
    for typed in ("", "   "):
        value, fake = _value_input(monkeypatch, "CORTEX_MODEL", {"CORTEX_MODEL": "llama3.1-70b"}, typed=typed)
        assert value == "llama3.1-8b"
        ((_k, _label, kw),) = [c for c in fake.calls if c[0] == "text_input"]
        assert kw["value"] == "llama3.1-70b"                 # opens on the stored model, not an empty box
        assert "blank clears" not in kw["help"]
        assert any("default" in c for c in fake.texts("caption"))


def test_cortex_model_is_saved_in_the_form_the_app_runs(monkeypatch):
    from app.core.ai import normalize_model
    for typed in (" Llama3.1-70B", "llama3.1 8b", "mistral-large2"):
        value, _fake = _value_input(monkeypatch, "CORTEX_MODEL", {}, typed=typed)
        assert value == normalize_model(typed)
        assert value == value.strip().lower() and " " not in value


def test_the_settings_writer_never_upserts_a_blank_cortex_model(monkeypatch):
    sql = _settings_tab(monkeypatch, {"CORTEX_MODEL": "llama3.1-8b"}, key="CORTEX_MODEL", typed="")
    assert "'llama3.1-8b' AS VALUE" in sql and "'' AS VALUE" not in sql


# ------------------------------------------------------------------------------ R2-106 ----

def _purge_floors() -> dict[str, int]:
    """GREATEST(<var>, N) floors of the CURRENT SP_PURGE_FACTS definer (the newest migration that creates it)."""
    marker = "PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_PURGE_FACTS("
    texts = [t for t in (p.read_text(encoding="utf-8")
                         for p in sorted((ROOT / "snowflake" / "migrations").glob("V[0-9]*.sql"))) if marker in t]
    body = texts[-1].split(marker, 1)[1].split("$$;", 1)[0]
    keys = re.findall(r"KEY = '([A-Z_]+_RETENTION_DAYS[A-Z_]*)'", body)
    var_floor = dict(re.findall(r"(\w+_days) := GREATEST\(\1, (\d+)\)", body))
    into = re.search(r"INTO :(\w+), :(\w+), :(\w+), :(\w+)", body)
    assert into is not None
    return {k: int(var_floor[v]) for k, v in zip(keys, into.groups(), strict=True)}


def test_retention_editors_start_at_the_purge_floors():
    from app.ui.pages import admin
    floors = _purge_floors()
    assert floors == {"FACT_RETENTION_DAYS_HOURLY": 90, "FACT_RETENTION_DAYS_DAILY": 365,
                      "ERROR_LOG_RETENTION_DAYS": 30, "APP_USAGE_RETENTION_DAYS": 90}
    for key, floor in floors.items():
        kind, spec = admin._SETTING_EDITORS[key]
        assert kind == admin._NUM and spec["min_value"] == float(floor), key
        assert f"at least {floor} days" in spec["help"] and "monthly purge" in spec["help"]
    assert floors == admin._PURGE_FLOORS
    # the config comment lists all four floors
    assert "90/365/30/90" in read("app/config.py")


def test_a_below_floor_stored_retention_opens_at_the_floor(monkeypatch):
    value, _fake = _value_input(monkeypatch, "APP_USAGE_RETENTION_DAYS", {"APP_USAGE_RETENTION_DAYS": "30"})
    assert value == "90"                                      # what SP_PURGE_FACTS actually keeps


def test_retention_save_says_it_applies_at_the_monthly_purge(monkeypatch):
    fake = _St(key="FACT_RETENTION_DAYS_DAILY")
    _settings_tab(monkeypatch, {"FACT_RETENTION_DAYS_DAILY": "800"}, fake=fake, operator=True)
    caps = fake.texts("caption")
    assert any("next monthly purge" in c and "at least 365 days" in c for c in caps)
    assert not any("one cache cycle" in c for c in caps)


def test_coco_cap_editor_says_zero_is_the_default():
    from app.ui.pages import admin
    _kind, spec = admin._SETTING_EDITORS["COCO_DAILY_CAP_CREDITS"]
    assert "0 = not set" in spec["help"] and "15 credits" in spec["help"]


# ------------------------------------------------------------------------------ R2-107 ----

def _settings_tab(monkeypatch, current: dict, *, key: str = "CREDIT_PRICE_USD", typed: str = "",
                  fake: _St | None = None, operator: bool = False) -> str:
    """Render _settings_tab over a SETTINGS frame of ``current``; returns the upsert SQL shown."""
    from app.ui.pages import admin
    fake = fake or _St(typed=typed, key=key)
    frame = pd.DataFrame({"KEY": list(current), "VALUE": list(current.values()),
                          "UPDATED_AT": [None] * len(current), "UPDATED_BY": [""] * len(current)})
    shown: list[str] = []
    fake.code = lambda sql, *_a, **_k: shown.append(sql)          # type: ignore[method-assign]
    monkeypatch.setattr(admin, "run", lambda *_a, **_k: QueryResult(df=frame.copy(), ok=True, source="SETTINGS"))
    monkeypatch.setattr(admin, "load_settings", lambda _p: {"_source": "stub"})
    monkeypatch.setattr(admin, "guard", lambda res, *_a, **_k: res.usable())
    for name in ("panel_help", "styled_table", "result_caption", "section_header"):
        monkeypatch.setattr(admin, name, lambda *_a, **_k: None)
    monkeypatch.setattr(admin, "with_user_names", lambda df, *_a, **_k: df)
    monkeypatch.setattr(admin, "confirm_gate", lambda *_a, **_k: operator)
    monkeypatch.setattr(admin, "write_gate_open", lambda *_a, **_k: True)
    monkeypatch.setattr(admin, "execute_statement", lambda *_a, **_k: (True, "ok"))
    monkeypatch.setattr(admin, "stamp_write", lambda *_a, **_k: None)
    monkeypatch.setattr(admin, "notify", lambda *_a, **_k: None)
    monkeypatch.setattr(admin, "st", fake)
    admin._settings_tab(is_operator=operator)
    return shown[-1] if shown else ""


def test_credit_price_override_is_editable_and_never_safe_to_delete(monkeypatch):
    fake = _St(key="CREDIT_PRICE_OVERRIDE")
    rows = {k: str(v) for k, v in DEFAULT_SETTINGS.items()} | {"CREDIT_PRICE_USD": "3.5",
                                                                 "CREDIT_PRICE_OVERRIDE": "TRUE"}
    sql = _settings_tab(monkeypatch, rows, fake=fake)
    assert not [w for w in fake.texts("warning") if "CREDIT_PRICE_OVERRIDE" in w and "safe to delete" in w]
    assert "CREDIT_PRICE_OVERRIDE" in fake.options
    # the enum editor opens on the stored TRUE, and the MERGE upserts the key (inserting it when absent)
    assert "'CREDIT_PRICE_OVERRIDE' AS KEY" in sql and "'TRUE' AS VALUE" in sql


def test_a_genuinely_retired_key_is_still_flagged(monkeypatch):
    fake = _St()
    _settings_tab(monkeypatch, {"CREDIT_PRICE_USD": "3.68", "INCIDENT_REOPEN_DAYS": "14"}, fake=fake)
    assert "Settings rows the app no longer reads (safe to delete): INCIDENT_REOPEN_DAYS" in fake.texts("warning")


def test_non_default_rate_without_the_override_warns_before_the_save(monkeypatch):
    fake = _St(key="CREDIT_PRICE_USD", number=3.5)
    _settings_tab(monkeypatch, {"CREDIT_PRICE_USD": "3.68"}, fake=fake)
    assert any("-20013" in w and "CREDIT_PRICE_OVERRIDE" in w for w in fake.texts("warning"))
    quiet = _St(key="CREDIT_PRICE_USD", number=3.5)
    _settings_tab(monkeypatch, {"CREDIT_PRICE_USD": "3.68", "CREDIT_PRICE_OVERRIDE": "yes"}, fake=quiet)
    assert not any("-20013" in w for w in quiet.texts("warning"))
    default = _St(key="CREDIT_PRICE_USD", number=3.68)
    _settings_tab(monkeypatch, {"CREDIT_PRICE_USD": "3.68"}, fake=default)
    assert not any("-20013" in w for w in default.texts("warning"))


def test_compute_rate_editor_refuses_zero():
    from app.ui.pages import admin
    _kind, spec = admin._SETTING_EDITORS["CREDIT_PRICE_USD"]
    assert spec["min_value"] > 0                              # validate.sql RAISEs e_rate_pos on 0


def test_every_settings_key_validate_reads_is_known_to_admin():
    """Recurrence lock: a SETTINGS key the deploy gate reads must be a DEFAULT_SETTINGS key or a declared
    deploy-gate setting, or Admin flags its row 'safe to delete' and offers no editor (the DEPLOY_ACTORS class)."""
    from app.ui.pages import admin
    read_keys: set[str] = set()
    for rel in ("snowflake/validate.sql", "snowflake/rebuild/05_validate.sql"):
        read_keys |= set(re.findall(r"\bKEY\s*=\s*'([A-Z0-9_]+)'", read(rel)))
    assert "CREDIT_PRICE_OVERRIDE" in read_keys
    assert read_keys - set(DEFAULT_SETTINGS) - set(admin._DEPLOY_GATE_SETTINGS) == set()
    # and every declared deploy-gate key is really read there (no stale allow-list entry)
    assert set(admin._DEPLOY_GATE_SETTINGS) <= read_keys
    assert not set(admin._DEPLOY_GATE_SETTINGS) & set(DEFAULT_SETTINGS)


# ------------------------------------------------------------------------------ R2-110 ----

def test_every_numeric_default_setting_has_a_number_editor():
    from app.ui.pages import admin
    numeric = [k for k, v in DEFAULT_SETTINGS.items()
               if isinstance(v, int | float) and not isinstance(v, bool) and not k.startswith("_")]
    assert "DATA_TRANSFER_USD_PER_TB" in numeric
    missing = [k for k in numeric if admin._SETTING_EDITORS.get(k, ("",))[0] != admin._NUM]
    assert missing == []
    assert admin._SETTING_EDITORS["DATA_TRANSFER_USD_PER_TB"] == (admin._NUM, {"min_value": 0.0, "step": 0.01})


def test_egress_rate_editor_cannot_go_negative(monkeypatch):
    value, fake = _value_input(monkeypatch, "DATA_TRANSFER_USD_PER_TB", {"DATA_TRANSFER_USD_PER_TB": "-20"})
    ((_k, _label, kw),) = [c for c in fake.calls if c[0] == "number_input"]
    assert kw["min_value"] == 0.0 and value == "0"            # a stored negative opens clamped to the floor
    assert not [c for c in fake.calls if c[0] == "text_input"]
