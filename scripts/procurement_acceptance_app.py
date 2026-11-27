"""Isolated deterministic browser fixture. Never connects to paid providers.

Run: python -m uvicorn scripts.procurement_acceptance_app:app --port 8011
Use frontend VITE_API_BASE=http://127.0.0.1:8011/api/v1 on port 5174.
All brands, prices, pages are synthetic and must not become real reports.
"""
import json
from pathlib import Path
from backend.domain.search import SearchResult
from backend.main import create_app
from backend.services.official_search import OfficialSearchService

ROOT = Path('output/implementation-audit/isolated-browser-fixture')
BODY = '验收品牌官方网站 https://fixture-manufacturer.example/ 。验收品牌 X100 数量1台 中标单价10000元。验收品牌 X200 容量120 ml。'


class FixtureProvider:
    name = 'fixture'
    def search(self, query):
        return [SearchResult('https://fixture.edu.cn/award', '合成验收采购成交公告'),
                SearchResult('https://fixture-manufacturer.example/products', '合成验收产品目录')]
    def fetch(self, url, timeout):
        return BODY


class FixtureModel:
    def analyze(self, *, system_prompt, user_prompt):
        data = json.loads(user_prompt)
        if 'prices' in system_prompt:
            return {'prices': [{'brand':'验收品牌','model':'X100','kind':'award','amount':10000,'amount_unit':'元','amount_scope':'unit','quantity':1,'currency':'CNY','excerpt':'验收品牌 X100 数量1台 中标单价10000元'}]} if data['target']['model'] == 'X100' else {'prices':[]}
        if 'candidates' in system_prompt:
            return {'candidates':[{'brand':'验收品牌','model':'X200','configuration':'标准配置','url':'https://fixture-manufacturer.example/products','excerpt':'验收品牌 X200 容量120 ml'}]}
        return {'parameters':[{'name':'容量','value':120,'unit':'ml','conditions':'','url':'https://fixture-manufacturer.example/products','excerpt':'验收品牌 X200 容量120 ml'}]}


app = create_app(database_path=ROOT/'fixture.sqlite3', storage_dir=ROOT/'files',
                 cors_origins=['http://127.0.0.1:5174'], search_service=OfficialSearchService(providers=[FixtureProvider()]))
app.state.research_service.client_factory = FixtureModel
app.state.repository.put('tasks','fixture-browser',{'id':'fixture-browser','scenario':'【合成数据验收】样品容量检测；不是采购建议','title':'合成容量仪验收','status':'draft'})
