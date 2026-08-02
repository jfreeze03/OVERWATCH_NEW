"""One home for table and chart heights (rec 17).

Pages hardcoded ~15 distinct pixel heights (140-460) for the same content
classes, so density was inconsistent and a global density change meant hunting
literals across a dozen files. These tokens name the classes; the same content
class now gets the same height everywhere, and a density change is one edit.

Pure module: no imports. Values chosen to keep the current look (normalizing a
handful of near-duplicate heights to the nearest class, within ~20px).
"""

from __future__ import annotations

# Scrollable data tables.
TABLE_H_SM = 220   # compact / phone surfaces (Brief), short secondary lists
TABLE_H_MD = 260   # standard scrollable table (movers, triage, ledgers)
TABLE_H_LG = 460   # dense reference tables (Admin metrics)

# Charts.
CHART_H_SM = 220   # compact charts
CHART_H_MD = 264   # default chart height (the app-wide baseline)
CHART_H_LG = 300   # tall charts (heatmaps, stacked multi-series)
