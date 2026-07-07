"""Single entry point for Streamlit-in-Snowflake and local dev."""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.main import main  # noqa: E402

main()
