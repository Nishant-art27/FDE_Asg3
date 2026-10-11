from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ["LLM_PROVIDER"] = "offline"  # tests never call a model


@pytest.fixture(autouse=True)
def in_process_vendor_api():
    """Serve the mock vendor-risk API in-process so tests need no running server."""
    from fastapi.testclient import TestClient
    from mock_api.app import app
    from src import tools

    client = TestClient(app)

    def fetch(name: str, timeout: float):
        r = client.get(f"/vendor-risk/{name}")
        return r.status_code, r.json()

    tools.set_vendor_fetcher(fetch)
    yield
    tools.set_vendor_fetcher(None)
