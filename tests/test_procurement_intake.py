from fastapi.testclient import TestClient
from backend.main import create_app
from backend.services.official_search import OfficialSearchService, FakeSearchProvider
from backend.services.procurement_intake import read_intake, respond, next_question
from backend.services.procurement_rules import validate_price
from backend.repositories.sqlite import SQLiteRepository
import pytest


def test_intake_keeps_quote_and_service_separate_and_survives_reopen(tmp_path):
    repo = SQLiteRepository(tmp_path / 'case.db')
    repo.put('tasks', 't', {'id':'t', 'scenario':'生化培养箱 泰斯特 SPX-150BIII 1台 单价6800元'})
    class Extractor:
        def analyze(self, **kwargs):
            return {'facts': {
                'model': {'value':'SPX-150BIII','quote':'SPX-150BIII'},
                'supplier_price': {'value':6800,'quote':'单价6800元'},
                'quantity': {'value':1,'quote':'1台'}, 'currency': {'value':'CNY','quote':'6800元'},
                'warranty': {'value':'2年','quote':'保修2年'},
                'service_response': {'value':'4小时','quote':'4小时响应'},
                'service_onsite': {'value':'48小时','quote':'48小时上门'},
                'delivery': {'value':'10天','quote':'交货10天'},
                'tax': {'value':'含税','quote':'模型凭空编造的句子'},
            }, 'parameters':[{'name':'使用温度','value':[25,42],'unit':'℃','direction':'covers','quote':'25-42度'}]}
    state = respond(repo,'t',{'revision':0,'message':'25-42度，保修2年，4小时响应，48小时上门，交货10天'},Extractor)
    assert state['draft']['supplier_price'] == 6800
    assert state['draft']['service_response'] == '4小时'
    assert state['draft']['service_onsite'] == '48小时'
    assert state['draft']['delivery'] == '10天'
    assert 'tax' not in state['facts']
    with pytest.raises(ValueError, match='新回复'):
        respond(repo,'t',{'revision':0,'message':'重复'},Extractor)
    repo.close()
    repo = SQLiteRepository(tmp_path / 'case.db')
    assert read_intake(repo,'t')['draft'] == state['draft']
    repo.close()


def test_quote_and_after_sales_questions_only_ask_missing_fields():
    facts = {k:{'value':'已有'} for k in ['category','brand','model','scenario','requirements','supplier_price','quantity','currency','configuration','tax','shipping','installation','warranty']}
    q = next_question({'facts':facts})
    assert q['group'] == 'service'
    assert '报修响应时间' in q['text'] and '上门处理时间' in q['text']
    assert '保修期限' not in q['text'] and '销售单价' not in q['text']


def test_unknown_answer_skips_provider_and_survives_reload(tmp_path):
    repo = SQLiteRepository(tmp_path / 'skip.db')
    repo.put('tasks', 't', {'id': 't', 'scenario': '培养箱'})
    def unavailable():
        raise AssertionError('Unknown answer must not call the model')
    state = respond(repo, 't', {'revision': 0, 'message': '销售暂未说明，先保留缺项。', 'defer': True}, unavailable)
    assert 'use' in state['deferred']
    assert not state['facts']
    assert read_intake(repo, 't')['revision'] == 1
    repo.close()


def test_after_sales_terms_must_match_before_claiming_same_conditions():
    from datetime import date
    stamp = date.today().isoformat()
    body = f'成交公告 {stamp} 厂商 X1 单价6800元 数量1台 标准配置 含税 免费安装 保修一年 交货10天 运费包含 4小时响应 48小时上门'
    source = {'url': 'https://example.edu.cn/result', 'body_status': 'fetched', 'body_text': body, 'source_type': 'tender_document'}
    row = {'kind': 'award', 'brand': '厂商', 'model': 'X1', 'amount': 6800, 'amount_unit': '元', 'amount_scope': 'unit', 'quantity': 1, 'excerpt': body, 'date': stamp,
           'configuration': '标准配置', 'tax': '含税', 'installation': '免费安装', 'warranty': '保修一年', 'delivery': '交货10天'}
    base = {**row, 'supplier_price': 6800, 'currency': 'CNY', 'shipping': '运费包含', 'service_response': '4小时响应', 'service_onsite': '48小时上门'}
    quote = validate_price(row, source, base)
    assert quote['level'] == 'historical_configuration' and quote['delta'] == 0
    assert set(quote['gaps']) >= {'shipping', 'service_response', 'service_onsite'}
    row.update({k: base[k] for k in ('shipping', 'service_response', 'service_onsite')})
    assert validate_price(row, source, base)['level'] == 'historical_configuration'
    row['service_onsite'] = '24小时上门'  # Not in the source; cannot become a better promise.
    changed = validate_price(row, source, base)
    assert changed['level'] == 'historical_configuration'
    assert 'service_onsite' in changed['gaps']

    # A grounded but different commercial term remains visible as a same-model
    # historical price and is called out for the user's own comparison.
    base['configuration'] = '高配方案'
    different = validate_price({**row, 'service_onsite': '48小时上门'}, source, base)
    assert different['level'] == 'historical_configuration'
    assert 'configuration' in different['gaps']


def test_different_function_wording_does_not_prove_noncompliance():
    from backend.services.procurement_rules import evaluate_parameter
    assert evaluate_parameter({'value': '自动连续记录，并可导出 Excel/CSV 或打印', 'unit': ''},
                              {'value': '选配U盘数据存储', 'unit': '', 'evidence': True}) == 'unknown'


def test_baseline_saves_without_any_required_parameter(tmp_path):
    """用户不一定有必须满足的实验要求：一项指标都没有也要能确认比较基准。

    保存必须成功，而且要如实带上"本轮没有确认可比指标"这个状态——
    不能只是一个空壳，也不能让下游把没有指标当成"满足"。
    """
    app = create_app(database_path=tmp_path/'app.db', storage_dir=tmp_path/'files', search_service=OfficialSearchService(providers=[FakeSearchProvider()]))
    service = app.state.research_service
    class Client:
        def analyze(self, **kwargs): return {'candidates': []}
    service.client_factory = Client
    with TestClient(app) as c:
        payload = {'brand': '测试', 'model': 'X1', 'category': '培养箱', 'scenario': '微生物培养',
                   'supplier_price': 6800, 'currency': 'CNY', 'quantity': 1, 'parameters': []}
        # 每个用例单独一个 task：确认一次比较基准会换掉这一轮的 run，同一个 task 里
        # 反复确认会把上一条的 lanes 状态带进来，判定就不干净了。
        def save(body):
            tid = c.post('/api/v1/tasks', json={'scenario': '微生物培养'}).json()['task']['id']
            return c.post(f'/api/v1/tasks/{tid}/baseline/save', json=body)

        r = save(payload)
        assert r.status_code == 200, r.text
        run = r.json()
        assert run['baseline']['parameters'] == []
        assert run['comparison_basis'] == 'no_confirmed_parameter'
        assert run['confirmed_parameter_count'] == 0
        # 价格链不受影响：报价照旧保存，核价通道该跑还跑。
        assert run['baseline']['supplier_price'] == 6800
        assert run['lanes'] == {'price': 'pending', 'alternatives': 'pending'}
        assert not service.futures
        # 缺一个键也不能被当成"有指标"（空列表和 None 是同一个意思：没有指标）。
        assert save({**payload, 'parameters': None}).json()['comparison_basis'] == 'no_confirmed_parameter'
        # 类型不对仍然拒绝：放宽的是"有没有"，不是"格式对不对"。
        assert save({**payload, 'parameters': 'not-a-list'}).status_code == 422
        # 有指标时行为不变（回归）。
        with_metric = save({**payload, 'parameters': [{'name': '温度', 'value': [25, 42], 'unit': '℃', 'direction': 'covers'}]}).json()
        assert with_metric['comparison_basis'] == 'confirmed_parameters'
        assert with_metric['confirmed_parameter_count'] == 1
        # 仍然拒绝残缺的指标行。
        assert save({**payload, 'parameters': [{'name': '', 'value': 1}]}).status_code == 422
        assert save({**payload, 'parameters': [{'name': '范围', 'value': [50, 0], 'unit': '℃', 'direction': 'covers'}]}).status_code == 422


def test_save_has_no_search_and_lanes_run_independently(tmp_path):
    app = create_app(database_path=tmp_path/'app.db', storage_dir=tmp_path/'files', search_service=OfficialSearchService(providers=[FakeSearchProvider()]))
    service = app.state.research_service
    class Client:
        def analyze(self, **kwargs): return {'candidates':[]}
    service.client_factory = Client
    with TestClient(app) as c:
        tid = c.post('/api/v1/tasks',json={'scenario':'微生物培养'}).json()['task']['id']
        prefix=f'/api/v1/tasks/{tid}'
        payload={'brand':'测试','model':'X1','category':'培养箱','scenario':'微生物培养','supplier_price':6800,'currency':'CNY','quantity':1,'warranty':'2年','service_response':'4小时','service_onsite':'48小时','delivery':'10天','parameters':[{'name':'温度','value':[25,42],'unit':'℃','direction':'covers'}]}
        r=c.post(prefix+'/baseline/save',json=payload)
        assert r.status_code == 200
        run=r.json(); assert run['status']=='ready' and not service.futures
        rid=run['id']
        initial_report = service.report(tid, rid)
        assert c.post(prefix+f'/research-runs/{rid}/start/price').status_code==200
        service.futures[rid].result(timeout=10)
        run=c.get(prefix+f'/research-runs/{rid}').json()
        assert run['lanes']=={'price':'completed','alternatives':'pending'}
        assert service.report(tid, rid)['report_id'] != initial_report['report_id']
        assert c.post(prefix+f'/research-runs/{rid}/start/alternatives').status_code==200
        service.futures[rid].result(timeout=10)
        run=c.get(prefix+f'/research-runs/{rid}').json()
        assert run['lanes']=={'price':'completed','alternatives':'completed'}
        report=service.report(tid,rid)
        assert '48小时' in str(report) and '4小时' in str(report) and '10天' in str(report)
        assert c.post(prefix+f'/research-runs/{rid}/report').content.startswith(b'PK')


@pytest.mark.parametrize('kind',['official_list','budget','ceiling'])
def test_website_prices_and_budgets_never_enter_procurement_comparison(kind):
    text='中标公告 X1 单价6800元 数量1台'
    source={'url':'https://example.gov.cn/result','body_status':'fetched','body_text':text,'source_type':'tender_document'}
    row={'kind':kind,'model':'X1','amount':6800,'amount_unit':'元','amount_scope':'unit','quantity':1,'excerpt':text}
    assert validate_price(row,source,{'model':'X1'}) is None
