"""Tests for Jina Reader integration."""
from __future__ import annotations

import pytest
from unittest.mock import Mock, patch
import urllib.error

from backend.infrastructure.jina_reader import fetch_with_jina


def test_fetch_with_jina_rejects_private_target_before_network(monkeypatch):
    called = []
    monkeypatch.setattr("urllib.request.urlopen", lambda *args, **kwargs: called.append(args))
    with pytest.raises(ValueError, match="私有网络"):
        fetch_with_jina("http://127.0.0.1/private")
    assert called == []


def test_fetch_with_jina_rejects_private_dns_target_before_network(monkeypatch):
    monkeypatch.setattr(
        "socket.getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", ("10.0.0.8", 443))],
    )
    with pytest.raises(ValueError, match="私有网络"):
        fetch_with_jina("https://attacker.example/private")


class TestJinaReader:
    """Test suite for Jina Reader web content extraction."""

    def test_fetch_with_jina_success(self):
        """Test successful content fetch through Jina Reader."""
        mock_response = Mock()
        mock_response.status = 200
        mock_response.read.return_value = b"# Test Content\n\nThis is markdown."
        mock_response.__enter__ = Mock(return_value=mock_response)
        mock_response.__exit__ = Mock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_response):
            text, metadata = fetch_with_jina("https://example.com/test")

        assert text == "# Test Content\n\nThis is markdown."
        assert metadata["status_code"] == 200
        assert metadata["jina_used"] is True
        assert metadata["content_length"] == len(text)
        assert metadata["original_url"] == "https://example.com/test"

    def test_fetch_with_jina_invalid_url_scheme(self):
        """Test that invalid URL schemes are rejected."""
        with pytest.raises(ValueError, match="Invalid URL scheme"):
            fetch_with_jina("ftp://example.com/file")

        with pytest.raises(ValueError, match="Invalid URL scheme"):
            fetch_with_jina("javascript:alert(1)")

    def test_fetch_with_jina_timeout(self):
        """Test that timeout is properly passed to urlopen."""
        mock_response = Mock()
        mock_response.status = 200
        mock_response.read.return_value = b"content"
        mock_response.__enter__ = Mock(return_value=mock_response)
        mock_response.__exit__ = Mock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_response) as mock_open:
            fetch_with_jina("https://example.com", timeout=5.0)

            # Verify timeout was passed
            call_args = mock_open.call_args
            assert call_args[1]["timeout"] == 5.0

    def test_fetch_with_jina_http_error(self):
        """Test handling of HTTP errors from Jina Reader."""
        with patch("urllib.request.urlopen", side_effect=urllib.error.HTTPError(
            "https://r.jina.ai/https://example.com",
            429,
            "Too Many Requests",
            {},
            None
        )):
            with pytest.raises(urllib.error.HTTPError):
                fetch_with_jina("https://example.com")

    def test_fetch_with_jina_network_error(self):
        """Test handling of network errors."""
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("Network unreachable")):
            with pytest.raises(urllib.error.URLError):
                fetch_with_jina("https://example.com")

    def test_fetch_with_jina_encoding_handling(self):
        """Test that non-UTF8 content is handled gracefully."""
        mock_response = Mock()
        mock_response.status = 200
        # Invalid UTF-8 sequence
        mock_response.read.return_value = b"\xff\xfe Invalid UTF-8 \x80"
        mock_response.__enter__ = Mock(return_value=mock_response)
        mock_response.__exit__ = Mock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_response):
            text, metadata = fetch_with_jina("https://example.com")

        # Should not raise; errors="replace" handles it
        assert metadata["jina_used"] is True
        assert len(text) > 0

    def test_fetch_with_jina_response_size_limit(self):
        """Test that responses larger than 10MB are rejected."""
        mock_response = Mock()
        mock_response.status = 200
        # Simulate 11MB response
        mock_response.read.return_value = b"x" * (10 * 1024 * 1024 + 1)
        mock_response.__enter__ = Mock(return_value=mock_response)
        mock_response.__exit__ = Mock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_response):
            with pytest.raises(ValueError, match="Response too large"):
                fetch_with_jina("https://example.com")

    def test_fetch_with_jina_empty_response(self):
        """Test that empty responses are rejected."""
        mock_response = Mock()
        mock_response.status = 200
        mock_response.read.return_value = b"   \n\n  "  # only whitespace
        mock_response.__enter__ = Mock(return_value=mock_response)
        mock_response.__exit__ = Mock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_response):
            with pytest.raises(ValueError, match="empty content"):
                fetch_with_jina("https://example.com")

    def test_fetch_with_jina_invalid_timeout_config(self):
        """Test that invalid JINA_READER_TIMEOUT is handled in search_providers."""
        # This is tested in test_jina_integration.py
        pass
def test_jina_reader_rejects_private_target_before_network(monkeypatch):
    called = []
    monkeypatch.setattr("urllib.request.urlopen", lambda *args, **kwargs: called.append(args))
    with pytest.raises(ValueError, match="私有网络"):
        fetch_with_jina("http://127.0.0.1/private")
    assert called == []


def test_jina_reader_rejects_private_dns_target_before_network(monkeypatch):
    monkeypatch.setattr(
        "socket.getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", ("10.0.0.8", 443))],
    )
    with pytest.raises(ValueError, match="私有网络"):
        fetch_with_jina("https://attacker.example/private")
