"""Session-wide test isolation (added 2026-09-29 after a leak incident).

Without this, any code path that reaches pricing.cache.store_candles or
db.transaction() writes to whatever settings.db_path resolves to — which,
in this developer's persistent shell, is the LIVE kolfi.db. A rescue test
writing its fixture candles into production is exactly how the 0xabababab
sentinel leaked (1,828 rows) on 2026-09-29.

Redirect DB_PATH to a throwaway file BEFORE `config` is first imported
(settings resolves it via default_factory at import time), so every
incidental write lands there instead. Must stay import-order-critical:
no module above this line may `import config`.
"""
import os
import tempfile
from pathlib import Path

_TMP_DB = Path(tempfile.mkdtemp(prefix="kolfi_test_")) / "test.db"
os.environ["DB_PATH"] = str(_TMP_DB)
os.environ["SCHEMA_FILE"] = "schema_unified.sql"
os.environ["PRICING_ENGINE"] = "7d"

import pytest  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _isolated_db():
    """Init the throwaway schema once per session."""
    from db import init_db
    init_db()
    yield
