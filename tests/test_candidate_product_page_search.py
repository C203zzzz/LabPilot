"""候选发现页里没有产品页链接时，主动去找一次参数页（2026-10-03 批3 实测后加）。

背景：批2 批3 合计 64 个候选里 58 个的 ``detail_page`` 停在 ``url_missing``——旧逻辑
只顺着候选原文行里给出的网址读，"不额外检索也不猜网址"，而候选常常是从公告页或聚合页
里冒出来的，那上面本来就没有产品页链接。于是"配置比配置"的 351 项里 350 项是"未知"。
用户 2026-10-03 的口径：「候选品的配置可以在第三方网站和厂家网站中取」。

判据不另写：找到的那一页必须能过 ``product_configuration``（这页自己写着这个型号、
写进配置的是参数段），这里用 ``tests/test_product_configuration.py`` 里那张真实形态的
厂家参数页做样本。
"""
import re
from threading import Lock

from backend.services.procurement_research import (CANDIDATE_DETAIL_SEARCH_LIMIT,
                                                   CANDIDATE_DETAIL_SEARCH_RESERVE,
                                                   ProcurementResearch)
from tests.test_product_configuration import BRAND, MANUFACTURER_BODY, MODEL

FOUND_URL = 'https://www.xiangyilxj.com/product/info_itemid_952.html'
OTHER_BODY = '某某大学实验室仪器中标公告\n采购人：某某大学\n中标金额：120000 元'
#: 写着这个型号、但读不出参数段的一页（中文页面里型号紧贴汉字，是常态）。
MODEL_ONLY_BODY = '湘仪 XKA-2200 高速离心机\n本机采用无刷电机，运行平稳，噪音低。'


class FakeResearch:
    """只带这个方法要用的两件东西：一次检索，以及检索会把哪些页读进来。

    ``query`` 的形状与 ``ProcurementResearch.query`` 一致（检索句 + 用途），并把结果按
    真实行为追加进 ``run['sources']``（``purpose`` 记成调用方给的那个）。默认每次检索
    读进来一页**新地址**，这样一页只能接上一个候选。
    """

    def __init__(self, body=MANUFACTURER_BODY, error=None, pages=None):
        self.body = body
        self.error = error
        self.pages = pages
        self.queries = []
        self.stage_lock = Lock()

    record_operation = ProcurementResearch.record_operation

    def query(self, run, text, purpose):
        self.queries.append((text, purpose))
        if self.error is not None:
            raise self.error
        items = self.pages
        if items is None:
            items = [('https://example.com/product/%d.html' % len(self.queries), self.body)]
        for url, body in items:
            run['sources'].append({'url': url, 'body_status': 'fetched', 'body_text': body,
                                   'source_type': 'product_detail', 'purpose': purpose})


class PerModelResearch(FakeResearch):
    """每次检索只回**这一句问的那台机器**的页。

    ``FakeResearch`` 每次回同一页（含同一个型号），真实里一页一般只讲一台，所以
    "限额管的是检索次数"这条要用这个类才测得准。
    """

    def query(self, run, text, purpose):
        self.queries.append((text, purpose))
        model = re.search(r'[A-Za-z]+-\d+', text).group(0)
        run['sources'].append({'url': 'https://example.com/product/%s.html' % model,
                               'body_status': 'fetched',
                               'body_text': MANUFACTURER_BODY.replace(MODEL, model),
                               'source_type': 'product_detail', 'purpose': purpose})


def _run(used=0, limit=80):
    return {'limits': {'search': limit, 'fetch': 160},
            'usage': {'search': used, 'fetch': 0}, 'sources': [], 'gaps': []}


def _candidate(url=''):
    return {'model': MODEL, 'brand': BRAND,
            'detail_page': {'url': url, 'status': 'url_missing'}}


def _search(research, run, candidates):
    return ProcurementResearch.search_candidate_product_pages(research, run, candidates)


def test_a_candidate_without_a_link_gets_the_page_that_carries_its_parameters():
    research = FakeResearch(pages=[(FOUND_URL, MANUFACTURER_BODY)])
    run = _run()
    candidate = _candidate()
    assert _search(research, run, [candidate]) == 1
    assert candidate['detail_page']['url'] == FOUND_URL
    assert candidate['detail_page']['status'] == 'fetched'
    assert candidate['detail_page']['origin'] == 'candidate_model_search'
    # 配置比对照这个用途找证据：接上的这页必须被标成它。
    assert run['sources'][0]['purpose'] == 'alternatives_detail'
    # 检索句是人话：品牌 + 型号 + 参数。
    assert research.queries == [('%s %s 参数' % (BRAND, MODEL), 'alternatives_detail')]
    assert any('接上了写着该型号的参数页' in gap for gap in run['gaps'])


def test_the_candidates_own_configuration_is_written_with_its_provenance():
    # 候选的配置用与成交价同一套字段写（configuration ＋ 来源 ＋ 出处），界面与报告
    # 两处显示的才是同一件事；前端按 configuration_source 认它、不按"有值就算"。
    page = {'url': FOUND_URL, 'body_status': 'fetched', 'body_text': MANUFACTURER_BODY,
            'source_type': 'product_detail', 'purpose': 'discovery'}
    run = _run()
    run['sources'].append(page)
    research = FakeResearch()
    candidate = _candidate()
    assert _search(research, run, [candidate]) == 1
    assert candidate['configuration']
    assert candidate['configuration_source'] in ('manufacturer_spec', 'product_page_spec')
    assert candidate['configuration_url'] == FOUND_URL


def test_a_page_that_only_names_the_model_is_attached_and_marked_as_such():
    # 挑页与读值是两件事：只要正文里写着这个型号就先接上（下游按同一批行去读值，
    # 读不出来它会照实标"未知"），但要告诉读者这一页只到"写着型号"这一步。
    #
    # 2026-10-05 收紧的是**计数**：只写着型号、压不出参数段的页不再算"接上了参数页"，
    # 否则对外那句"为 N 台接上了写着该型号的参数页"比内部事实粗（实测 289 个候选详情页
    # 里 53 页是这种）。页面照接，返回值不含它。
    research = FakeResearch(body=MODEL_ONLY_BODY)
    candidate = _candidate()
    run = _run()
    assert _search(research, run, [candidate]) == 0
    assert candidate['detail_page']['status'] == 'fetched'
    assert candidate['detail_page']['coverage'] == 'model_only'
    assert not any('接上了写着该型号的参数页' in gap for gap in run['gaps'])
    assert any('只找到写着型号、没有产品参数段' in gap for gap in run['gaps']), run['gaps']


# ------------------------------------------------- 公告页／聚合站转载页不接做详情页
#
# 2026-10-05：全部已跑 run 的 289 个候选详情页里，有 12 页落在招标公告或聚合站转载页上，
# 而它们**一个参数段都没有**（coverage 全是 model_only）。批3 点名的三个案例就在其中：
# MAST-A／HSA-1500M → yiliaozhaobiao.com（已登记的聚合站），BCV-3S1 → jiancaizhaobiao.cn
# （正文本身就是中标公告）。那页本来就没有产品参数，接上它等于说"这台机器的详情页是那份公告"。
NOTICE_BODY = ('中国采招网 中标结果公告\n'
               '项目编号：ZC-2026-0001\n采购人：某某大学\n中标供应商：某某公司\n'
               '中标金额：120000 元\n' + MODEL + ' 高速离心机 1 台')
AGGREGATOR_URL = 'https://www.yiliaozhaobiao.com/news/2026/0001.html'
TENDER_SITE_URL = 'https://www.jiancaizhaobiao.cn/detail/0002.html'


def test_an_aggregator_or_notice_page_is_not_attached_as_the_detail_page():
    research = FakeResearch(pages=[(AGGREGATOR_URL, NOTICE_BODY)])
    run = _run()
    candidate = _candidate()
    assert _search(research, run, [candidate]) == 0
    assert candidate['detail_page']['status'] == 'url_missing'
    assert candidate['detail_page']['url'] == ''
    # 被拒的那一页要留痕（给排查），但**不进**候选的来源网页清单。
    assert candidate['detail_page']['refused_url'] == AGGREGATOR_URL
    assert '招标公告或聚合站的转载页' in candidate['detail_page']['reason']
    assert any('未接成候选详情页' in gap for gap in run['gaps']), run['gaps']


def test_a_tender_page_on_an_unlisted_domain_is_caught_by_its_own_text():
    """``jiancaizhaobiao.cn`` 没登记进聚合站词表，靠**正文自己就是公告**认出来。"""
    from backend.services.source_authority import is_notice_page
    source = {'url': TENDER_SITE_URL, 'body_status': 'fetched', 'body_text': NOTICE_BODY}
    assert is_notice_page(source) is True
    research = FakeResearch(pages=[(TENDER_SITE_URL, NOTICE_BODY)])
    candidate = _candidate()
    assert _search(research, _run(), [candidate]) == 0
    assert candidate['detail_page']['url'] == ''


def test_a_product_page_wins_over_a_notice_page_that_names_the_same_model():
    """两页都写着这个型号时先看产品页——公告页即使排在来源清单前面也不该被选中。"""
    from backend.services.source_authority import product_page_rank
    notice = {'url': AGGREGATOR_URL, 'body_status': 'fetched', 'body_text': NOTICE_BODY,
              'source_type': 'tender_document', 'purpose': 'discovery'}
    product = {'url': FOUND_URL, 'body_status': 'fetched', 'body_text': MANUFACTURER_BODY,
               'source_type': 'product_detail', 'purpose': 'discovery'}
    assert product_page_rank(notice, [BRAND]) > product_page_rank(product, [BRAND])
    run = _run()
    run['sources'].extend([notice, product])
    candidate = _candidate()
    assert _search(FakeResearch(), run, [candidate]) == 1
    assert candidate['detail_page']['url'] == FOUND_URL
    assert candidate['detail_page']['coverage'] == 'parameters'


# ------------------------------------------- 型号后面粘着中文说明时也要认得出来（T3）
#
# 台账里的型号写法比厂家页长：``B-260型``、``DK-98-ⅡA双列六孔``、``CJ-1DII单人单面``、
# ``LS-75HD(原LS-B75L-I数显）``（本地成交明细库 5648 个唯一型号里 964 个含中文）。
# 厂家产品页只写型号本身，而 ``contains_model`` 要求完整 token——不按去掉说明的写法试一次，
# 这些候选的配置**永远取不到**（只降到 model_only）。
ANNOTATED_MODEL = 'XKA-2200型'
#: 参数段照抄 ``MANUFACTURER_BODY`` 的形状，只把型号写成厂家自己的写法（没有「型」）。
ANNOTATED_PAGE_BODY = MANUFACTURER_BODY.replace(MODEL, 'XKA-2200')


def test_a_model_with_a_trailing_chinese_note_still_matches_the_manufacturer_page():
    from backend.services.procurement_rules import model_spellings
    assert model_spellings(ANNOTATED_MODEL) == (ANNOTATED_MODEL, 'XKA-2200')
    research = FakeResearch(pages=[(FOUND_URL, ANNOTATED_PAGE_BODY)])
    run = _run()
    candidate = {'model': ANNOTATED_MODEL, 'brand': BRAND,
                 'detail_page': {'url': '', 'status': 'url_missing'}}
    assert _search(research, run, [candidate]) == 1
    assert candidate['configuration']
    # 候选自己的型号写法一个字不改，只记下这一页是按哪种写法认出来的。
    assert candidate['model'] == ANNOTATED_MODEL
    assert candidate['detail_page']['model_spelling'] == 'XKA-2200'


def test_too_short_a_stripped_spelling_is_refused_rather_than_guessed():
    """``YP系列电子天平/电子台秤`` 去掉中文只剩 ``YP``：两个字、没有数字，不给额外写法。"""
    from backend.services.procurement_rules import model_spellings
    assert model_spellings('YP系列电子天平/电子台秤') == ('YP系列电子天平/电子台秤',)
    assert model_spellings('Explorer™ 分析天平') == ('Explorer™ 分析天平',)
    # 括号里是英文的品牌写法不是中文说明，不动。
    assert model_spellings('梅特勒-托利多(METTLER TOLEDO)') == ('梅特勒-托利多(METTLER TOLEDO)',)
    assert model_spellings(MODEL) == (MODEL,)
    assert model_spellings('') == ()


def test_a_stripped_spelling_must_still_look_like_a_model():
    """去掉说明之后必须**既含字母又含数字、且不含中文**，否则宁可不多给。

    这条是量出来的（``output/e2e-final/tmp-model-spellings-blast.py``，全库 5648 个唯一型号）：
    放宽带宽时 401 条（7.1%）会多出一个写法，其中 ``1000九运``→``1000``、
    ``100发一盒``→``100``、``2020年广东建设职业技术学院超星系列数据库采购``→``2020``
    一眼就是错的——``213型``→``213`` 那种会把任何印着 213 的页面当成"这一页在讲这台机器"，
    读出来的参数就记到这条候选头上。收紧后 176 条（3.1%），上面这类一条不剩。
    """
    from backend.services.procurement_rules import model_spellings
    for junk in ('213型', '1000九运', '100发一盒', '003助泳板', '1312(72个装)',
                 '2020年广东建设职业技术学院超星系列数据库采购',
                 '1分钟仰卧起坐测试仪外设TSN200-YW-01（考试版）', '175克飞盘',
                 '1000×1000×850/全钢结构'):
        assert model_spellings(junk) == (junk,), junk
    # 该救的仪器型号一条不少。
    for model, cleaned in (('B-260型', 'B-260'), ('DK-98-ⅡA双列六孔', 'DK-98-ⅡA'),
                           ('CJ-1DII单人单面', 'CJ-1DII'),
                           ('LS-75HD(原LS-B75L-I数显）', 'LS-75HD')):
        assert model_spellings(model) == (model, cleaned), model


def test_a_page_that_does_not_carry_this_model_is_not_used():
    research = FakeResearch(body=OTHER_BODY)
    candidate = _candidate()
    run = _run()
    assert _search(research, run, [candidate]) == 0
    assert candidate['detail_page']['status'] == 'url_missing'
    # 2026-10-05 起理由要说得更细：这一轮**任何一页**都没提到这个型号（连同族写法都没有），
    # 与"只有同族近似写法"是两件事（见下面那两条用例）。
    assert '没有任何一页提到' in candidate['detail_page']['reason']
    assert candidate['detail_page']['url'] == ''


def test_a_candidate_that_already_has_a_detail_page_is_left_alone():
    research = FakeResearch()
    candidate = _candidate(url=FOUND_URL)
    assert _search(research, _run(), [candidate]) == 0
    assert research.queries == []
    assert candidate['detail_page']['url'] == FOUND_URL


def test_a_page_already_fetched_for_another_purpose_is_used_without_searching():
    # 这一版真正省事的地方：含候选型号的产品页往往是发现或官方车道早就抓回来的，
    # 旧写法只看"这次检索新增的页"，于是白花检索还接不上（批3 实测：多花 20 次检索、
    # 70 次抓取，一台都没接上）。
    page = {'url': FOUND_URL, 'body_status': 'fetched', 'body_text': MANUFACTURER_BODY,
            'source_type': 'product_detail', 'purpose': 'discovery'}
    run = _run()
    run['sources'].append(page)
    research = FakeResearch()
    candidate = _candidate()
    assert _search(research, run, [candidate]) == 1
    assert research.queries == []
    assert candidate['detail_page']['coverage'] == 'parameters'
    # 原来那条记录一个字不动，也不另立一条：同址两条记录会把来源计数弄脏
    # （查价台账的 sources_scanned 当场 4 变 5）。
    assert page['purpose'] == 'discovery'
    assert len(run['sources']) == 1


def test_the_search_budget_is_kept_for_the_lanes_that_come_after():
    research = FakeResearch()
    run = _run(used=80 - CANDIDATE_DETAIL_SEARCH_RESERVE, limit=80)
    assert _search(research, run, [_candidate()]) == 0
    assert research.queries == []


def test_a_failed_search_is_recorded_and_the_rest_still_get_their_turn():
    research = FakeResearch(error=ValueError('boom'))
    run = _run()
    assert _search(research, run, [_candidate(), _candidate()]) == 0
    # 一次检索走不通不是整条车道的事（设计规范）：记下来，下一个候选照常试。
    assert [item['code'] for item in run['failures']] == ['candidate_product_page_search_failed'] * 2
    assert len(research.queries) == 2


def test_only_so_many_candidates_are_searched_in_one_round():
    """限额管的是"出去检索几次"——这一点没放宽。

    每次检索只回**这一句问的那台机器**的页（真实里一页一般只讲一台），所以后面那些候选
    没有现成页可用、各自都得出去查一次；额度用完就停，剩下的照实记 ``url_missing``。
    """
    research = PerModelResearch()
    models = ['XKA-%d' % (2200 + index) for index in range(CANDIDATE_DETAIL_SEARCH_LIMIT + 2)]
    candidates = [{'model': model, 'brand': BRAND,
                   'detail_page': {'url': '', 'status': 'url_missing'}} for model in models]
    run = _run()
    found = _search(research, run, candidates)
    assert len(research.queries) == CANDIDATE_DETAIL_SEARCH_LIMIT
    assert found == CANDIDATE_DETAIL_SEARCH_LIMIT
    assert sum(1 for c in candidates if c['detail_page']['status'] == 'url_missing') == 2


# ------------------------------------------- 本地库线索：已按设计规范决定撤出候选发现
#
# 2026-10-05 决定：那个库只有成交明细、没有配置，补进来的线索配置只能靠检索，真跑两轮
# 6 条全部拿不到产品页（命中率 0），且结构上进不了核验名额。库的用途收回价格。
# 这里钉住"撤掉了"，免得以后有人以为它还在跑。

def test_local_db_leads_are_not_merged_into_candidate_discovery():
    """``proposed_by='local_db'`` 的线索不再进候选列表，也不再有检索保底。

    直接断言函数签名层面的两件事：常量没了、``alternatives()`` 里不再调用库侧函数。
    行为侧的验证在 ``tests/test_xlyg_candidate_leads.py`` 的集成用例里（库线索不出现）。
    """
    from backend.services import procurement_research as module
    import inspect
    assert not hasattr(module, 'LOCAL_CANDIDATE_SEARCH_MINIMUM')
    assert not hasattr(module, 'LOCAL_CANDIDATE_LEAD_LIMIT')
    source = inspect.getsource(module.ProcurementResearch.alternatives)
    assert 'from backend.services.xlyg_local_db import local_candidate_leads' not in source


# ------------------------------------------- 没接上时要说清"为什么"（型号写法）
#
# 量测：305 条没配置的候选里，64 条（21%）是自己那一轮的页面里只有"同族不同后缀"的写法
# （候选 CC-1008EW 而页面写 CC-1008E、候选 H1650K 而页面写 H-1650、候选 VS-840U 而页面写
# VS-840-2），11 条是任何页面都没提到。"写法要核对"与"这一轮根本没抓到相关的页"是两件事。

def test_nearest_model_spellings_only_reports_same_family_different_spelling():
    from backend.services.procurement_research import nearest_model_spellings
    page = [{'url': 'https://x.example/p.html', 'body_status': 'fetched',
             'body_text': 'CC系列 型号 | 温度范围 | 加热功率 CC-1008E | -10～100 | 2 CC-2508E | -25～100 | 2'}]
    assert nearest_model_spellings(page, 'CC-1008EW') == ['CC-1008E']
    assert nearest_model_spellings(page, 'ZZZ-9999') == []
    assert nearest_model_spellings(page, 'CC-1008E') == []      # 写法一样就不算"近似"


def test_a_model_only_found_in_a_sibling_spelling_is_reported_as_a_writing_question():
    """页面只写着同族写法时，不许把"没接上参数页"说成"这页只有型号没有参数"。

    实测的形状：候选 `CC-1008EW`，页面表格里写的是 `CC-1008E`。宽松的同族判据
    （``_is_candidate_family``）认定"这页在讲它"，所以会接成 ``model_only``；
    但页面上**没有**这个型号本身，因此既要如实说清，又绝不能放行它的参数。
    """
    page = {'url': 'https://x.example/p.html', 'body_status': 'fetched',
            'body_text': 'CC系列 型号 | 温度范围 CC-1008E | -10～100',
            'source_type': 'product_detail', 'purpose': 'discovery'}
    run = _run(used=80 - CANDIDATE_DETAIL_SEARCH_RESERVE)   # 检索额度用完：不再出去查
    run['sources'].append(page)
    candidate = {'model': 'CC-1008EW', 'brand': BRAND,
                 'detail_page': {'url': '', 'status': 'url_missing'}}
    assert _search(PerModelResearch(), run, [candidate]) == 0
    assert candidate['detail_page']['coverage'] == 'model_only'
    assert candidate['detail_page']['similar_models_on_pages'] == ['CC-1008E']
    assert '型号写法可能需要核对' in candidate['detail_page']['reason']
    assert any('替代候选型号写法' in gap for gap in run['gaps']), run['gaps']
    # 判据一个字没放宽：页面上没有这个型号，就不给配置。
    assert not candidate.get('configuration')


def test_a_model_mentioned_nowhere_is_reported_as_having_no_source_at_all():
    run = _run()
    candidate = _candidate()
    assert _search(FakeResearch(body=OTHER_BODY), run, [candidate]) == 0
    assert '没有任何一页提到' in candidate['detail_page']['reason']
    assert any('替代候选型号来源' in gap for gap in run['gaps']), run['gaps']


def test_the_limit_does_not_stop_the_loop_from_using_pages_already_in_hand():
    """手上已有参数页时不受限额限制——那是零成本的本地扫描。

    为什么钉这一条（2026-10-05 离线重放，46 轮真实台账）：原来 ``found`` 还兼任"处理到第
    几个候选就停"，而"手上已抓到的页里有没有这一页"不花检索也不花抓取。拿一笔检索额度去卡
    一件不花钱的事，代价是白扔掉已经到手的参数页——实测接上参数页的候选 241 → 344（+43%），
    检索次数一次没多花。
    """
    models = ['XKA-%d' % (2200 + index) for index in range(CANDIDATE_DETAIL_SEARCH_LIMIT + 2)]
    run = _run()
    for index, model in enumerate(models):
        run['sources'].append(
            {'url': 'https://example.com/product/%d.html' % index, 'body_status': 'fetched',
             'body_text': MANUFACTURER_BODY.replace(MODEL, model),
             'source_type': 'product_detail', 'purpose': 'discovery'})
    candidates = [{'model': model, 'brand': BRAND,
                   'detail_page': {'url': '', 'status': 'url_missing'}} for model in models]
    research = FakeResearch()
    assert _search(research, run, candidates) == len(candidates)
    assert research.queries == []       # 一页都不用出去找
    scans = [entry for entry in run['operation_timings']
             if entry['operation'] == 'product_configuration_scan']
    assert len(scans) == len(candidates)
    assert all(entry['candidates'] == 1 and entry['pages_scanned'] >= 1
               for entry in scans)
    assert sum(entry['pages_scanned'] for entry in scans) >= len(candidates)

