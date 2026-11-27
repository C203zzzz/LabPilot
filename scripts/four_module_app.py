"""Isolated, real-provider acceptance. Never opens production task storage."""
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'output/four-module-implementation-20260920/runtime'
if not os.getenv('QIANWENAI_TEST_API_KEY'):
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, 'Environment') as registry:
        key = winreg.QueryValueEx(registry, 'QIANWENAI_TEST_API_KEY')[0]
else:
    key = os.environ['QIANWENAI_TEST_API_KEY']
os.environ['DEEPSEEK_API_KEY'] = key
os.environ['DEEPSEEK_ENDPOINT'] = 'https://maas.qianwenaiapi.com/compatible-mode/v1/chat/completions'
os.environ['DEEPSEEK_MODEL'] = 'deepseek-v4.1-flash'
os.environ['LAB_MODEL_THINKING'] = 'off'
os.environ['LAB_MODEL_CALLS_PER_HOUR'] = '60'
os.environ['LAB_MODEL_TIMEOUT'] = '90'
os.environ['LAB_USAGE_DATABASE'] = str(ROOT / 'data/provider-usage.sqlite3')
ledger = json.loads((ROOT / 'output/model-comparison-20260920/cost-ledger.json').read_text(encoding='utf-8-sig'))
known = sum(row.get('charged_ceiling_cny', row['reserved_cny']) for row in ledger['calls'])
os.environ['LAB_COST_LEDGER'] = str(DATA / 'cost.sqlite3')
# Keep an additional reserve for unknown prior attempts inside the 2.80 limit.
os.environ['LAB_COST_INITIAL_CNY'] = str(known + .20)
os.environ['LAB_COST_LIMIT_CNY'] = '2.80'
os.environ['LAB_BROWSER_READING'] = '1'
os.environ['TEMP'] = os.environ['TMP'] = str(DATA / 'tmp')
Path(os.environ['TEMP']).mkdir(parents=True, exist_ok=True)

from backend.main import create_app
from backend.services.official_search import OfficialSearchService
from backend.infrastructure.search_providers import PublicWebSearchProvider, SogouSearchProvider
from backend.infrastructure.page_reader import PublicPageReader

# Only keyless search, no native paid search or Firecrawl subscription.
search = OfficialSearchService(providers=[SogouSearchProvider(), PublicWebSearchProvider()],
                               reader=PublicPageReader(browser=True), timeout=15)
app = create_app(database_path=DATA / 'tasks.sqlite3', storage_dir=DATA / 'files',
                 cors_origins=['http://127.0.0.1:5177'], search_service=search)
