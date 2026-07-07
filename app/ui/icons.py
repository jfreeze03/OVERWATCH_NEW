"""Inline SVG icon set — replaces emoji (which render inconsistently across
platforms). Feather-style 24x24 stroke paths using currentColor, so an icon
inherits the text color of wherever it's placed.

`icon(name)` returns an HTML string for st.markdown(unsafe_allow_html=True).
Unknown names fall back to a neutral dot, so a typo never breaks a render.
"""

from __future__ import annotations

# path bodies (inside <svg>), 24x24 viewBox, stroke=currentColor
_PATHS = {
    "brief": '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M2 12h2M20 12h2M5 5l1.5 1.5M17.5 17.5L19 19M19 5l-1.5 1.5M6.5 17.5L5 19"/>',
    "overview": '<rect x="3" y="3" width="8" height="8" rx="1"/><rect x="13" y="3" width="8" height="5" rx="1"/><rect x="13" y="10" width="8" height="11" rx="1"/><rect x="3" y="13" width="8" height="8" rx="1"/>',
    "control": '<path d="M3 12h4l2 6 4-16 2 10h6"/>',
    "cost": '<circle cx="12" cy="12" r="9"/><path d="M12 7v10M15 9.5c0-1.4-1.3-2.5-3-2.5s-3 1-3 2.3c0 3 6 1.7 6 4.7 0 1.3-1.3 2.5-3 2.5s-3-1.1-3-2.5"/>',
    "operations": '<rect x="4" y="4" width="16" height="16" rx="2"/><rect x="9" y="9" width="6" height="6"/><path d="M9 2v2M15 2v2M9 20v2M15 20v2M2 9h2M2 15h2M20 9h2M20 15h2"/>',
    "alerts": '<path d="M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.7 21a2 2 0 0 1-3.4 0"/>',
    "security": '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/><path d="M9 12l2 2 4-4"/>',
    "admin": '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.6 1.6 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.6 1.6 0 0 0-2.7 1.1V21a2 2 0 1 1-4 0v-.1A1.6 1.6 0 0 0 6.6 19l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1A1.6 1.6 0 0 0 3 13.6H3a2 2 0 1 1 0-4h.1A1.6 1.6 0 0 0 4.6 7l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.6 1.6 0 0 0 1.8.3H9a1.6 1.6 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.6 1.6 0 0 0 2.7 1.1l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.6 1.6 0 0 0-.3 1.8V9a1.6 1.6 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.6 1.6 0 0 0-1.5 1z"/>',
    # section / semantic
    "spend": '<path d="M3 3v18h18"/><path d="M7 14l3-4 3 3 5-7"/>',
    "contract": '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6M8 13h8M8 17h5"/>',
    "chargeback": '<rect x="3" y="6" width="18" height="13" rx="2"/><path d="M3 10h18M7 15h4"/>',
    "optimize": '<path d="M12 2v4M12 18v4M2 12h4M18 12h4"/><circle cx="12" cy="12" r="4"/>',
    "pipeline": '<path d="M4 7h10a3 3 0 0 1 0 6H8a3 3 0 0 0 0 6h12"/><circle cx="4" cy="7" r="1.5"/><circle cx="20" cy="19" r="1.5"/>',
    "warehouse": '<path d="M3 21V9l9-5 9 5v12"/><path d="M3 21h18M9 21v-6h6v6"/>',
    "clock": '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    "bolt": '<path d="M13 2L3 14h7l-1 8 10-12h-7z"/>',
    "search": '<circle cx="11" cy="11" r="7"/><path d="M21 21l-4-4"/>',
    "refresh": '<path d="M21 12a9 9 0 1 1-3-6.7L21 8"/><path d="M21 3v5h-5"/>',
    "up": '<path d="M12 19V5M5 12l7-7 7 7"/>',
    "down": '<path d="M12 5v14M5 12l7 7 7-7"/>',
    "flat": '<path d="M5 12h14"/>',
    "dot": '<circle cx="12" cy="12" r="4"/>',
}

_PAGE_ICON = {
    "Brief": "brief", "Overview": "overview", "Control Room": "control",
    "Cost & Contract": "cost", "Operations": "operations", "Alerts": "alerts",
    "Security & Governance": "security", "Security": "security", "Admin": "admin",
}


def icon(name: str, size: int = 16, cls: str = "", stroke: float = 1.9) -> str:
    body = _PATHS.get(name, _PATHS["dot"])
    klass = f' class="{cls}"' if cls else ""
    return (f'<svg{klass} width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" '
            f'stroke="currentColor" stroke-width="{stroke}" stroke-linecap="round" '
            f'stroke-linejoin="round" style="vertical-align:-2px">{body}</svg>')


def page_icon(page: str, size: int = 16) -> str:
    return icon(_PAGE_ICON.get(page, "dot"), size=size)
