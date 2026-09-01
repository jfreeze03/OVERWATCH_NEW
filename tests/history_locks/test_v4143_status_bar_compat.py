"""v4.143.1 lock for the deployed Snowflake Streamlit button signature."""

from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core import state
from app.ui import components

_ROOT = Path(__file__).resolve().parents[2]


def test_status_bar_uses_deployed_button_signature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    button_calls: list[str] = []
    navigations: list[tuple[str, str]] = []

    def legacy_button(
        label: str,
        *,
        key: str,
        help: str,
        type: str,
        icon: str,
        width: str,
    ) -> bool:
        button_calls.append(label)
        return True

    fake_st = SimpleNamespace(
        markdown=lambda *_args, **_kwargs: None,
        container=lambda **_kwargs: nullcontext(),
        columns=lambda count, **_kwargs: [nullcontext() for _ in range(count)],
        button=legacy_button,
    )
    monkeypatch.setattr(components, "st", fake_st)
    monkeypatch.setattr(
        state,
        "request_navigation",
        lambda page, section: navigations.append((page, section)),
    )

    components.status_bar([
        {
            "k": "Open criticals",
            "v": "3",
            "sev": "bad",
            "target": ("Alerts", "Open events"),
        }
    ])

    assert button_calls == ["Open open criticals"]
    assert navigations == [("Alerts", "Open events")]


def test_status_bar_has_no_newer_icon_position_keyword() -> None:
    source = (_ROOT / "app" / "ui" / "components.py").read_text(encoding="utf-8")
    body = source.split("def status_bar", 1)[1].split("\ndef ", 1)[0]
    assert "icon_position" not in body
    assert 'icon=":material/arrow_forward:"' in body
    assert 'APP_VERSION = "4.395.0"' in (
        _ROOT / "app" / "config.py"
    ).read_text(encoding="utf-8")
