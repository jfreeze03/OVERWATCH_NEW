"""Single source of truth for OVERWATCH's semantic colors (rec 50).

Before this module the severity/status hues were typed as raw hex literals in
four places — main.py `_STRIP_COLORS`, components.py `_SEV_HEX`, charts.py
`SEV_COLORS`, status_colors.py `delta_css` — plus a *fifth*, unaligned copy in
`.streamlit/config.toml`. A1 aligned them by hand; nothing stopped the next
divergence. Now every consumer imports these constants, `config.toml` mirrors
the chrome tokens, and `tests/test_palette_drift.py` fails CI if any of them
drifts from the `--ow-*` design tokens declared in `app/theme.py`.

Pure module: no imports, no Streamlit. Values are the exact current hues, so
adopting this changes nothing on screen except the intended config.toml
realignment (the native-widget dark now matches the card dark).
"""

from __future__ import annotations

# --- Semantic hues (foreground / chart series) ------------------------------
# These four mirror the --ow-ok/warn/bad/info tokens in theme.py (asserted by
# the drift test). BAD doubles as CRITICAL; INFO doubles as the sky accent.
OK = "#34d399"        # healthy / better / success            (--ow-ok)
WARN = "#fbbf24"      # watch / MEDIUM / amber                 (--ow-warn)
BAD = "#fb7185"       # act-now / CRITICAL / worse / red       (--ow-bad)
INFO = "#6c8cff"      # informational / indigo                 (--ow-info)

# Accents (mirror --ow-accent / --ow-accent2). v4.155 recolor: the brand
# identity moved off the stock sky-blue to an indigo->cyan pair for a cooler,
# more distinctive command-center look. INFO tracks ACCENT (one blue family).
ACCENT = "#6c8cff"
ACCENT2 = "#22d3ee"

# Chart-only severity extras — not design tokens (no --ow-* equivalent), but
# still centralized here so they have one home. HIGH is a distinct orange so a
# HIGH series never reads as CRITICAL red; LOW/LABEL is the muted chart slate.
HIGH = "#fb923c"      # HIGH severity — orange, distinct from CRITICAL red (r4)
LOW = "#8b98ad"       # LOW severity / chart axis labels
MUTED = "#94a3b8"     # neutral slate (sidebar strip / status cells)

# --- Chrome tokens (mirror theme.py; config.toml is aligned to these) --------
# v4.155 recolor: deeper, slightly cooler neutrals with clearer bg/surface/raised
# separation (all three still clear WCAG-AA for --ow-ink-mute, tested).
BG = "#090d17"        # app background       (--ow-bg)
SURFACE = "#0f1626"   # cards / secondary bg (--ow-surface)
RAISED = "#17203a"    # raised surfaces      (--ow-raised)
INK = "#e8eef7"       # primary text         (--ow-ink)
INK_SOFT = "#aab6c8"  # secondary text       (--ow-ink-soft)
INK_MUTE = "#8593a8"  # muted labels         (--ow-ink-mute)

# Convenience maps for the consumers that key by state / severity name.
# State-keyed (sidebar health strip): OK/WARN/BAD/INFO/MUTED.
STATE_HUES = {"OK": OK, "WARN": WARN, "BAD": BAD, "INFO": INFO, "MUTED": MUTED}
# Severity-keyed (chart series): CRITICAL/HIGH/MEDIUM/LOW/INFO/OK.
SEVERITY_HUES = {"CRITICAL": BAD, "HIGH": HIGH, "MEDIUM": WARN,
                 "LOW": LOW, "INFO": INFO, "OK": OK}
