"""Typed result for every Snowflake read the app performs."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import pandas as pd


@dataclass
class QueryResult:
    """What a page gets back from core.query.run().

    ``ok=False`` means the query failed and ``error`` says why — pages render
    a labeled error state, never a silent empty frame. ``truncated=True``
    means the row cap was hit and the UI must show a truncation banner.
    """

    df: pd.DataFrame = field(default_factory=pd.DataFrame)
    ok: bool = True
    error: str = ""
    # Classified from the RAW exception (Codex r10 #4): format_snowflake_error
    # rewrites messages for humans, which silently broke marker-string checks
    # downstream (canary GAP never matched). Kinds: absent | unknown_function
    # | timeout | other | "" (no error).
    error_kind: str = ""
    truncated: bool = False
    source: str = ""
    tier: str = "recent"
    fetched_at: datetime | None = None
    # R12: fetched_at is stamped at RETURN, so on a cache hit it is the "served now"
    # time, NOT when Snowflake was actually hit (which was up to the tier TTL earlier).
    # cache_hit lets the provenance caption say "served … (cached)" instead of claiming
    # a fresh "fetched …", so an old result never carries a fresh-looking fetch time.
    cache_hit: bool = False
    elapsed_ms: float = 0.0

    @property
    def empty(self) -> bool:
        return self.df is None or self.df.empty

    def usable(self) -> bool:
        """True when the page can render data from this result."""
        return self.ok and not self.empty
