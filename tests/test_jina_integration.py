"""Tests for Jina Reader integration in search providers."""
from __future__ import annotations

import os
import pytest
from unittest.mock import Mock, patch, MagicMock

from backend.infrastructure.search_providers import PublicWebSearchProvider


class TestJinaReaderIntegration:
    """Test Jina Reader integration in PublicWebSearchProvider.fetch()."""

    def test_fetch_uses_jina_when_enabled(self):
        """Test that fetch() uses Jina Reader when ENABLE_JINA_READER=true."""
        provider = PublicWebSearchProvider()

        mock_jina_result = ("# Jina Content\n\nClean markdown", {
            "status_code": 200,
            "jina_used": True,
            "content_length": 30,
            "original_url": "https://example.com"
        })

        with patch.dict(os.environ, {"ENABLE_JINA_READER": "true"}):
            with patch("backend.infrastructure.jina_reader.fetch_with_jina", return_value=mock_jina_result):
                result = provider.fetch("https://example.com", timeout=10.0)

        assert result == "# Jina Content\n\nClean markdown"

    def test_fetch_falls_back_to_urllib_on_jina_failure(self):
        """Test that fetch() falls back to urllib when Jina Reader fails."""
        provider = PublicWebSearchProvider()

        # Mock Jina to fail
        with patch.dict(os.environ, {"ENABLE_JINA_READER": "true"}):
            with patch("backend.infrastructure.jina_reader.fetch_with_jina", side_effect=Exception("Jina timeout")):
                # Mock urllib to succeed
                with patch.object(provider, "_request", return_value=(b"<html>fallback content</html>", "text/html")):
                    with patch.object(provider, "decode_body", return_value="fallback content"):
                        result = provider.fetch("https://example.com", timeout=10.0)

        assert result == "fallback content"

    def test_jina_validation_blocks_private_or_unresolvable_targets(self):
        """The hosted reader must not become an SSRF bypass for search leads."""
        provider = PublicWebSearchProvider()

        with patch.dict(os.environ, {"ENABLE_JINA_READER": "true"}):
            with patch("backend.services.research_sources.network_url", side_effect=ValueError("private target")):
                with patch("urllib.request.urlopen", side_effect=AssertionError("network must not be called")):
                    with patch.object(provider, "_request", return_value=(b"<html>fallback</html>", "text/html")):
                        with patch.object(provider, "decode_body", return_value="fallback"):
                            result = provider.fetch("http://169.254.169.254/latest/meta-data", timeout=10.0)

        assert result == "fallback"

    def test_fetch_skips_jina_when_disabled(self):
        """Test that fetch() skips Jina Reader when ENABLE_JINA_READER=false."""
        provider = PublicWebSearchProvider()

        with patch.dict(os.environ, {"ENABLE_JINA_READER": "false"}):
            with patch("backend.infrastructure.jina_reader.fetch_with_jina") as mock_jina:
                with patch.object(provider, "_request", return_value=(b"<html>direct</html>", "text/html")):
                    with patch.object(provider, "decode_body", return_value="direct content"):
                        result = provider.fetch("https://example.com", timeout=10.0)

        # Jina should not be called
        mock_jina.assert_not_called()
        assert result == "direct content"

    def test_fetch_respects_jina_timeout_config(self):
        """Test that JINA_READER_TIMEOUT config is respected."""
        provider = PublicWebSearchProvider()

        with patch.dict(os.environ, {"ENABLE_JINA_READER": "true", "JINA_READER_TIMEOUT": "5"}):
            with patch("backend.infrastructure.jina_reader.fetch_with_jina") as mock_jina:
                mock_jina.return_value = ("content", {"status_code": 200, "jina_used": True, "content_length": 7, "original_url": "https://example.com"})
                provider.fetch("https://example.com", timeout=10.0)

                # Should use min(5, 10 * 0.7) = min(5, 7) = 5
                mock_jina.assert_called_once()
                assert mock_jina.call_args[1]["timeout"] == 5.0

    def test_fetch_uses_request_timeout_when_smaller(self):
        """Test that request timeout budget (70%) is used when smaller than Jina config."""
        provider = PublicWebSearchProvider()

        with patch.dict(os.environ, {"ENABLE_JINA_READER": "true", "JINA_READER_TIMEOUT": "10"}):
            with patch("backend.infrastructure.jina_reader.fetch_with_jina") as mock_jina:
                mock_jina.return_value = ("content", {"status_code": 200, "jina_used": True, "content_length": 7, "original_url": "https://example.com"})
                provider.fetch("https://example.com", timeout=3.0)

                # Should use min(10, 3 * 0.7) = min(10, 2.1) = 2.1
                mock_jina.assert_called_once()
                actual_timeout = mock_jina.call_args[1]["timeout"]
                assert abs(actual_timeout - 2.1) < 0.01  # floating point tolerance

    def test_fetch_handles_invalid_timeout_config(self):
        """Test that invalid JINA_READER_TIMEOUT falls back to default."""
        provider = PublicWebSearchProvider()

        with patch.dict(os.environ, {"ENABLE_JINA_READER": "true", "JINA_READER_TIMEOUT": "invalid"}):
            with patch("backend.infrastructure.jina_reader.fetch_with_jina") as mock_jina:
                mock_jina.return_value = ("content", {"status_code": 200, "jina_used": True, "content_length": 7, "original_url": "https://example.com"})
                provider.fetch("https://example.com", timeout=20.0)

                # Should fall back to default 10.0, then min(10, 20 * 0.7) = min(10, 14) = 10
                mock_jina.assert_called_once()
                assert mock_jina.call_args[1]["timeout"] == 10.0

    def test_fetch_logs_jina_failures(self):
        """Test that Jina Reader failures are logged for observability."""
        provider = PublicWebSearchProvider()

        with patch.dict(os.environ, {"ENABLE_JINA_READER": "true"}):
            with patch("backend.infrastructure.jina_reader.fetch_with_jina", side_effect=ValueError("Empty content")):
                with patch("logging.debug") as mock_log:
                    with patch.object(provider, "_request", return_value=(b"<html>fallback</html>", "text/html")):
                        with patch.object(provider, "decode_body", return_value="fallback"):
                            provider.fetch("https://example.com", timeout=10.0)

                    # Verify logging was called
                    mock_log.assert_called_once()
                    log_message = mock_log.call_args[0][0]
                    assert "Jina Reader failed" in log_message
                    assert "ValueError" in log_message
