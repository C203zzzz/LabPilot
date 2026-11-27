from pathlib import Path
import sqlite3
import pytest


# Measured >= 2.0s per-file integration / multi-lane simulation suites
# (measured with fast_test_sqlite enabled; ~210s combined). Marked ``slow``
# centrally so daily ``pytest -m "not slow and not e2e"`` finishes ~152 files
# (~2,200 unit & light API tests) in ~1 minute without touching 44 test files.
SLOW_TEST_FILES = frozenset({
    'test_adaptive_pdf_analysis_api.py',
    'test_aggregator_discovery.py',
    'test_alternative_price_lookup.py',
    'test_alternative_price_on_demand.py',
    'test_api_runtime_contracts.py',
    'test_attachment_backfill.py',
    'test_attachment_download_render.py',
    'test_attachment_read_attempts.py',
    'test_body_fetch_parallel.py',
    'test_c_drift_fixes.py',
    'test_candidate_source_pages.py',
    'test_ccgp_body_pace.py',
    'test_columnar_price_gate.py',
    'test_delivery_fixes.py',
    'test_end_to_end_adversarial.py',
    'test_four_module_boundaries.py',
    'test_lane_crash_resilience.py',
    'test_lanes_together.py',
    'test_m03_candidates.py',
    'test_m04_official_search.py',
    'test_m6_report_diff.py',
    'test_metric_optional_downstream.py',
    'test_no_comparable_candidates_gap.py',
    'test_pdf_escalation.py',
    'test_price_lane_late_sources.py',
    'test_procurement_multisearch.py',
    'test_product_configuration.py',
    'test_product_configuration_reading.py',
    'test_product_document_attachments.py',
    'test_provider_health.py',
    'test_province_award_sources.py',
    'test_public_page_reader.py',
    'test_query_candidate_window.py',
    'test_reliability_development.py',
    'test_report_claims_and_frontend.py',
    'test_report_summary_overview.py',
    'test_research_network_errors.py',
    'test_research_repair.py',
    'test_search_access_recovery.py',
    'test_search_silent_failure.py',
    'test_supplier_update_price_retention.py',
})


def pytest_collection_modifyitems(config, items):
    slow_marker = pytest.mark.slow
    for item in items:
        if Path(str(item.fspath)).name in SLOW_TEST_FILES:
            item.add_marker(slow_marker)


_EXTERNAL_API_ENV_VARS = (
    'DEEPSEEK_API_KEY',
    'DEEPSEEK_ENDPOINT',
    'DEEPSEEK_MODEL',
    'DEEPSEEK_WEB_TOOL_TYPE',
    'TAVILY_API_KEY',
    'EXA_API_KEY',
    'EXA_MCP_URL',
    'JINA_API_KEY',
    'ENABLE_JINA_READER',
    'OPENAI_API_KEY',
    'LAB_BROWSER_READING',
)


@pytest.fixture(autouse=True)
def isolated_external_api_keys(monkeypatch, request):
    """Offline tests must never inherit real developer API keys from the shell.

    Inheriting real keys causes two bugs: tests asserting "unconfigured -> 503"
    hit live APIs and fail, and ordinary unit tests accidentally spend real quota.
    Tests marked ``@pytest.mark.e2e`` keep the caller's environment intact.
    """
    if request.node.get_closest_marker('e2e') is not None:
        return
    for var in _EXTERNAL_API_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True)
def fast_test_sqlite(monkeypatch):
    """Skip hardware fsync on throwaway test SQLite databases.

    Production code keeps SQLite's default durability settings; only temporary
    test databases skip disk sync so multi-write research suites run ~3x faster
    on Windows NTFS without changing SQL semantics.
    """
    real_connect = sqlite3.connect

    def _connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        try:
            conn.execute('PRAGMA synchronous=OFF')
        except sqlite3.Error:
            pass
        return conn

    monkeypatch.setattr(sqlite3, 'connect', _connect)


@pytest.fixture(autouse=True)
def isolated_provider_ledger(tmp_path, monkeypatch):
    """Offline tests must never consume or reset the user's real usage ledger."""
    monkeypatch.setenv('LAB_USAGE_DATABASE', str(tmp_path / 'provider-usage.sqlite3'))


@pytest.fixture(autouse=True)
def isolated_price_memory(tmp_path, monkeypatch):
    """Same reason as the ledger above: the price lane writes remembered prices
    to ``data/price_memory.sqlite3`` by default, and a test run must not touch
    the user's real memory (nor let one test's rows leak into the next)."""
    monkeypatch.setenv('LAB_PRICE_MEMORY_DB', str(tmp_path / 'price-memory.sqlite3'))


@pytest.fixture(autouse=True)
def isolated_knowledge_memory(tmp_path, monkeypatch):
    """同一个理由：每轮研究收尾会把成果写进 ``data/knowledge_memory.sqlite3``
    （K1~K5 沉淀）。测试必须各写各的——否则用例之间会互相看到对方的沉淀，
    而且会污染用户真实的库。想验证"库里已有沉淀"的用例自己传 ``database_path=``。"""
    monkeypatch.setenv('LAB_KNOWLEDGE_MEMORY_DB', str(tmp_path / 'knowledge-memory.sqlite3'))


@pytest.fixture(autouse=True)
def isolated_xlyg_library(tmp_path, monkeypatch):
    """The offline deal-detail library (``data/xlyg_prices.sqlite3``, 17 MB,
    12675 rows) is a *machine-local* artifact: it exists on the crawler's box and
    nowhere else.

    Two lanes read it by default -- the price lane (``local_price_rows``) and
    candidate discovery (``local_candidate_leads``) -- so a suite that let them
    reach the real file would answer differently on a machine that has it:
    candidates acquired from real trades would appear in tests whose baselines
    happen to share a category, and the "library missing" gap would disappear.
    Point both at a path that does not exist, so every test states its own input;
    tests that exercise the library pass ``database_path=`` (or set this variable)
    explicitly."""
    monkeypatch.setenv('LAB_XLYG_DB', str(tmp_path / 'no-xlyg-library.sqlite3'))


@pytest.fixture(autouse=True)
def isolated_ccgp_pacing(tmp_path, monkeypatch):
    """CCGP 的节流状态现在落在一个本地 sqlite 里（跨进程共享），默认
    ``data/ccgp_pacing.sqlite3``。测试必须各用各的：否则一个用例记下的"刚请求过"
    会让下一个用例凭空多等一段，而且会污染用户真实那一份。"""
    monkeypatch.setenv('LAB_CCGP_PACING_DB', str(tmp_path / 'ccgp-pacing.sqlite3'))


@pytest.fixture(autouse=True)
def isolated_channel_quarantine():
    """Channel quarantines are process-wide by design, so a test that trips one
    would otherwise silence that channel for every test after it."""
    from backend.infrastructure.search_providers import clear_channel_quarantine
    clear_channel_quarantine()
    yield
    clear_channel_quarantine()
