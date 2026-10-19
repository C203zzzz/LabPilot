import pytest


@pytest.fixture(autouse=True)
def isolated_provider_ledger(tmp_path, monkeypatch):
    """Offline tests must never consume or reset the user's real usage ledger."""
    monkeypatch.setenv('LAB_USAGE_DATABASE', str(tmp_path / 'provider-usage.sqlite3'))


@pytest.fixture(autouse=True)
def isolated_channel_quarantine():
    """Channel quarantines are process-wide by design, so a test that trips one
    would otherwise silence that channel for every test after it."""
    from backend.infrastructure.search_providers import clear_channel_quarantine
    clear_channel_quarantine()
    yield
    clear_channel_quarantine()
