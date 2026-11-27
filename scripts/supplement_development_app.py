"""Offline development page fixture; not a business acceptance environment."""
from pathlib import Path
from backend.main import create_app
from backend.services.official_search import OfficialSearchService


class OfflineProvider:
    name = 'offline-development'
    def search(self, query):
        return []
    def fetch(self, url, timeout):
        if url == 'https://development.example.edu.cn/award':
            return '2026-09-19 合成品牌 DEV100 数量1台 中标单价12000元'
        raise ValueError('仅支持开发快照，不访问网络')


class OfflineModel:
    def analyze(self, **kwargs):
        return {'candidates': []}


root = Path('output/reliability-development-20260919/browser-fixture')
app = create_app(database_path=root/'development.sqlite3', storage_dir=root/'files',
                 cors_origins=['http://127.0.0.1:5175'], search_service=OfficialSearchService(providers=[OfflineProvider()]))
app.state.research_service.client_factory = OfflineModel
task_id = 'supplement-development-only'
if not app.state.repository.get('tasks', task_id):
    app.state.repository.put('tasks', task_id, {'id': task_id, 'scenario': '合成开发检查：容量检测', 'title': '补证开发检查（合成）', 'status': 'draft'})
    run = app.state.research_service.confirm(task_id, {'brand': '合成品牌', 'model': 'DEV100', 'scenario': '合成开发检查：容量检测', 'parameters': [{'name': '容量', 'value': 100, 'unit': 'ml', 'direction': 'min'}]})
    app.state.research_service.futures[run['id']].result(timeout=10)
