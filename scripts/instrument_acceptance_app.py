"""Real-provider isolated server for browser acceptance; never opens production DB."""
import os
from pathlib import Path

from backend.main import create_app

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT/'output/bio-instrument-acceptance-20260920/browser-runtime'
os.environ['LAB_USAGE_DATABASE'] = str(ROOT/'data/provider-usage.sqlite3')
if os.getenv('LAB_MODEL_CALLS_PER_HOUR') != '60':
    raise RuntimeError('Browser acceptance requires the agreed process-local hourly cap')

app = create_app(database_path=DATA/'acceptance.sqlite3', storage_dir=DATA/'files',
                 cors_origins=['http://127.0.0.1:5177', 'http://localhost:5177'])


@app.get('/acceptance-diagnostics')
def acceptance_diagnostics():
    """Local isolated harness only: inspect worker failures without locals/secrets."""
    import traceback
    service = app.state.research_service
    items = []
    for run_id, future in list(service.futures.items()):
        error = future.exception() if future.done() and not future.cancelled() else None
        items.append({'run_id': run_id, 'done': future.done(),
                      'error_type': type(error).__name__ if error else None,
                      'stack': traceback.format_tb(error.__traceback__) if error else []})
    return {'workers': items}
