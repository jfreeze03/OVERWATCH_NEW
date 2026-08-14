"""Single source of truth for OVERWATCH's semantic colors (rec 50).

Before this module the severity/status hues were typed as raw hex literals in
four places — main.py `_STRIP_COLORS`, components.py `_SEV_HEX`, charts.py
`SEV_COLORS`, status_colors.py `delta_css` — plus a *fifth*, unaligned copy in
`.streamlit/config.toml`. A1 aligned them by hand; nothing stopped the next
divergence. Now every consumer imports these constants, `config.toml` mirrors
the chrome tokens, and `tests/test_palette_drift.py` fails CI if any of them
drifts from the `--ow-*` design tokens declared in `app/theme.py`.

Pure module: no imports, no Streamlit.

v4.155 "graphite & iris" (owner ask 2026-08-13): the cold navy neutrals and
the everywhere-sky accent are retired. Neutrals are de-blued graphite, the
accent is iris (interactive elements only — INFO no longer doubles as the
accent), and the severity hues are richer and less pastel-neon. The muted-ink
WCAG AA floor is preserved (see the a11y test in test_codex_r2_wave.py).
"""

from __future__ import annotations

# --- Semantic hues (foreground / chart series) ------------------------------
# These four mirror the --ow-ok/warn/bad/info tokens in theme.py (asserted by
# the drift test). BAD doubles as CRITICAL. INFO is sky — informational only;
# since v4.155 the accent is its own (iris) hue, so information and interaction
# no longer share a color.
OK = "#3ecf8e"        # healthy / better / success            (--ow-ok)
WARN = "#f0b429"      # watch / MEDIUM / amber                 (--ow-warn)
BAD = "#f0566d"       # act-now / CRITICAL / worse / red       (--ow-bad)
INFO = "#4cc3f0"      # informational / sky                    (--ow-info)

# Accents (mirror --ow-accent / --ow-accent2). Iris — reserved for interactive
# chrome (buttons, active nav, links, brand); never a severity.
ACCENT = "#8e8ffa"
ACCENT2 = "#b8b4ff"

# Chart-only severity extras — not design tokens (no --ow-* equivalent), but
# still centralized here so they have one home. HIGH is a distinct orange so a
# HIGH series never reads as CRITICAL red; LOW/LABEL is the muted chart slate.
HIGH = "#f5883d"      # HIGH severity — orange, distinct from CRITICAL red (r4)
LOW = "#8d93a4"       # LOW severity / chart axis labels
MUTED = "#9aa1b2"     # neutral slate (sidebar strip / status cells)

# --- Chrome tokens (mirror theme.py; config.toml is aligned to these) --------
BG = "#0f1016"        # app background       (--ow-bg)
SURFACE = "#15161f"   # cards / secondary bg (--ow-surface)
RAISED = "#1c1d29"    # raised surfaces      (--ow-raised)
INK = "#edeef4"       # primary text         (--ow-ink)
INK_SOFT = "#b4b8c6"  # secondary text       (--ow-ink-soft)
INK_MUTE = "#8f94a6"  # muted labels         (--ow-ink-mute)

# Convenience maps for the consumers that key by state / severity name.
# State-keyed (sidebar health strip): OK/WARN/BAD/INFO/MUTED.
STATE_HUES = {"OK": OK, "WARN": WARN, "BAD": BAD, "INFO": INFO, "MUTED": MUTED}
# Severity-keyed (chart series): CRITICAL/HIGH/MEDIUM/LOW/INFO/OK.
SEVERITY_HUES = {"CRITICAL": BAD, "HIGH": HIGH, "MEDIUM": WARN,
                 "LOW": LOW, "INFO": INFO, "OK": OK}
