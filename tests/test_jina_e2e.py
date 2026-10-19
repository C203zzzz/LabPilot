"""End-to-end test for Jina Reader integration (requires network)."""
from __future__ import annotations

import os
import pytest

from backend.infrastructure.jina_reader import fetch_with_jina
from backend.infrastructure.search_providers import PublicWebSearchProvider


@pytest.mark.skipif(
    os.environ.get("RUN_NETWORK_TESTS", "false").lower() != "true",
    reason="Set RUN_NETWORK_TESTS=true to run external network probes"
)
class TestJinaReaderE2E:
    """Opt-in real network tests for Jina Reader."""

    def test_jina_reader_real_fetch(self):
        """Test Jina Reader with a real network request."""
        text, metadata = fetch_with_jina("https://example.com", timeout=10.0)

        assert len(text) > 0
        assert metadata["status_code"] == 200
        assert metadata["jina_used"] is True
        assert "example" in text.lower()

    def test_provider_fetch_with_jina_enabled(self):
        """Test PublicWebSearchProvider.fetch() uses Jina Reader in real scenario."""
        provider = PublicWebSearchProvider()

        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("ENABLE_JINA_READER", "true")
            mp.setenv("JINA_READER_TIMEOUT", "10")

            result = provider.fetch("https://example.com", timeout=10.0)

        assert len(result) > 0
        assert "example" in result.lower()
