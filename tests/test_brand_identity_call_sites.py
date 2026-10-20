# -*- coding: utf-8 -*-
"""三个"品牌同一性"调用点是不是真的查了别名表。

量出：全仓还有几处做品牌同一性判断时用的是 ``identity``（只做 NFKC
归一／去空白连字符／小写），而 ``identity`` **不查别名表**，``brand_identity`` 才查。
后果是已经写进 ``_BRAND_ALIASES`` 的写法在那些地方全部判成两家。

只改**等值比较**那三处（``source_identity.verify_publisher`` 的发布者证据那一侧、
``procurement_research.verify_publishers`` 的 ``known_brand``、``product_configuration.
_own_brand_page`` 的发布者证据那一侧）。**子串包含**那几处按量测结论刻意不动，
本文件同时把它们钉住，防止后人顺手改松。

每个"改前"都用 ``monkeypatch`` 把该模块的 ``brand_identity`` 换回 ``identity``
模拟出来，所以一条测试里同时有调用前与调用后的结果。
"""
import pytest

from backend.services import procurement_research, product_configuration, source_identity
from backend.services.procurement_rules import brand_identity, brand_name_matches, identity
from backend.services.procurement_research import ProcurementResearch
from backend.services.research_planning import diverse_candidates

#: 已经写进别名表的 8 个写法 → 规范写法。key 侧才是 ``identity`` 不查表时会失配的那一侧。
ALIASES = [
    ('天津泰斯特', '泰斯特'),
    ('上海一恒', '一恒'),
    ('湖南湘仪', '湘仪'),
    ('徕卡', 'leica'),
    ('申安', '上海申安'),
    ('shenan', '上海申安'),
    ('苏州安泰', '苏净安泰'),
    ('苏州安泰空气技术有限公司', '苏净安泰'),
]


def _publisher_source(url, brand, body):
    return {'url': url, 'body_status': 'fetched', 'source_type': 'manufacturer_website',
            'publisher_evidence': {'brand': brand}, 'body_text': body}


# --------------------------------------------------------------------------
# 一、等值型三处：改前判两家，改后判同一家
# --------------------------------------------------------------------------

def test_source_identity_accepts_an_alias_spelling(monkeypatch):
    """``source_identity.py`` 里"发布者证据记的品牌 == 当前品牌"那一处。"""
    candidate = {'url': 'https://www.taisite.cn/download/a.pdf'}
    source = _publisher_source('https://www.taisite.cn/', '泰斯特', '资料见 %s' % candidate['url'])

    monkeypatch.setattr(source_identity, 'brand_identity', identity)   # 改前
    assert source_identity.verify_publisher(candidate, [source], '天津泰斯特') is None

    monkeypatch.undo()                                                 # 改后
    relation = source_identity.verify_publisher(candidate, [source], '天津泰斯特')
    assert relation is not None
    assert relation['domain'] == 'taisite.cn'


def test_own_brand_page_accepts_an_alias_spelling(monkeypatch):
    """``product_configuration._own_brand_page`` 里发布者证据那一处。"""
    source = {'source_type': 'unknown', 'publisher_evidence': {'brand': '泰斯特'}, 'body_text': ''}

    monkeypatch.setattr(product_configuration, 'brand_identity', identity)   # 改前
    assert product_configuration._own_brand_page(source, '天津泰斯特') is False

    monkeypatch.undo()                                                      # 改后
    assert product_configuration._own_brand_page(source, '天津泰斯特') is True


class _Repo:
    def __init__(self):
        self.records = {}

    def get(self, table, key):
        return self.records.get(key)


class _Stub:
    """只够 ``verify_publishers`` 跑起来的最小外壳：记下它发过哪些搜索。"""

    def __init__(self):
        self.repo = _Repo()
        self.queries = []

    def query(self, run, query, purpose, target=None):
        self.queries.append(query)

    def save(self, record):
        self.repo.records[record['id']] = record

    def persist(self, run):
        pass


def _run_with_evidence(brand):
    return {'task_id': 'task-brand-identity', 'publisher_queries': [], 'sources': [
        {'url': 'https://www.taisite.cn/', 'source_type': 'manufacturer_website',
         'body_status': 'fetched', 'body_text': '', 'publisher_evidence': {'brand': brand}}]}


def test_verify_publishers_does_not_search_when_the_alias_matches(monkeypatch):
    """``known_brand`` 那一处：认得出就省掉一次搜索，认不出就白花一次。"""
    monkeypatch.setattr(procurement_research, 'brand_identity', identity)   # 改前
    before = _Stub()
    ProcurementResearch.verify_publishers(before, _run_with_evidence('泰斯特'), '天津泰斯特')
    assert before.queries, '改前应当认不出同一个品牌的两种写法，于是多花一次搜索'

    monkeypatch.undo()                                                      # 改后
    after = _Stub()
    ProcurementResearch.verify_publishers(after, _run_with_evidence('泰斯特'), '天津泰斯特')
    assert after.queries == [], '改后应当认出，不再花这次搜索'


@pytest.mark.parametrize('written,canonical', ALIASES)
def test_every_alias_is_one_brand_at_the_equality_call_sites(written, canonical):
    """8 条别名在等值口径下都是同一家——这三处改的就是这个口径。"""
    assert brand_identity(written) == brand_identity(canonical)


# --------------------------------------------------------------------------
# 二、反例：放宽之后仍然必须是两家
# --------------------------------------------------------------------------

@pytest.mark.parametrize('left,right', [
    ('上海申安', '上海三申'),                    # 同城另一家灭菌器厂
    ('上海申安', '上海三申医疗器械有限公司'),
    ('申安', '上海三申'),
    ('shenan', '上海三申'),
    ('徕卡', '莱卡'),
])
def test_counterexamples_are_still_two_brands(left, right):
    """这类反例两边连"登记名 vs 字号"的关系都没有，两条判据都必须判两家。"""
    assert brand_identity(left) != brand_identity(right)
    assert brand_name_matches(left, right) is False


@pytest.mark.parametrize('written,other', [
    ('湖南湘仪', '长沙高新开发区湘仪天平仪器设备有限公司'),   # 只收完整「湖南湘仪」
    ('天津泰斯特', '江苏泰斯特电子设备制造有限公司'),
    ('天津泰斯特', '海南泰斯特医学科技有限公司'),
    ('上海一恒', '苏州一恒科学仪器有限公司'),
])
def test_the_alias_table_does_not_suck_in_a_namesake(written, other):
    """字号同名但登记名不同的公司，别名口径下必须还是两家。

    注意这里**只**断言 ``brand_identity``：``brand_name_matches`` 另有一条
    "一侧是另一侧的企业登记名"的桥（短名是企业全名的子串、且全名以「有限公司」
    这类后缀结尾），上面这四组在那条桥下**今天就是 True**——那是既有行为，
    归「同行登记全名做桥」单独评估，这次一行没动它。
    要保证的是：别名表没有把这些同字号公司并进来（``identity`` 换成
    ``brand_identity`` 没有放宽到它们）。
    """
    assert brand_identity(written) != brand_identity(other)
    assert brand_identity(other) == identity(other), '别名表不该把这家同名公司的名字归一掉'


def test_shanghai_shenan_is_not_shanghai_sanshen_at_the_call_sites(monkeypatch):
    """``上海申安`` 与 ``上海三申`` 在三处等值口径下都必须还是两家。"""
    candidate = {'url': 'https://www.sanshen.com.cn/download/a.pdf'}
    source = _publisher_source('https://www.sanshen.com.cn/', '上海三申', '资料见 %s' % candidate['url'])
    assert source_identity.verify_publisher(candidate, [source], '上海申安') is None

    evidence = {'source_type': 'unknown', 'publisher_evidence': {'brand': '上海三申'}, 'body_text': ''}
    assert product_configuration._own_brand_page(evidence, '上海申安') is False

    stub = _Stub()
    ProcurementResearch.verify_publishers(stub, _run_with_evidence('上海三申'), '上海申安')
    assert stub.queries, '不是同一家，就必须照常去查'


# --------------------------------------------------------------------------
# 三、子串包含那几处刻意没改，钉住行为
# --------------------------------------------------------------------------

def test_the_substring_call_site_keeps_the_counterexample_out():
    """``_own_brand_page`` 的版权所有者子串那一处刻意保持 ``identity``。

    量测显示：换成 ``brand_identity``
    之后 ``湖南湘仪`` 缩成 ``湘仪``，``某厂商官网的产品页``
    （版权所有者「长沙高新开发区湘仪天平仪器设备有限公司」，做天平的另一家）会被判成
    「湖南湘仪」自己的页面——那是误放行；``申安``→``上海申安`` 又会让两家 ``shenan.net``
    页面失去命中。分母 8088 个三元组里 4 条变化，净收益不明，所以不动。
    """
    source = {'source_type': 'unknown',
              'body_text': '版权所有 © 长沙高新开发区湘仪天平仪器设备有限公司'}
    assert product_configuration._own_brand_page(source, '湖南湘仪') is False
    # 同一个页面在"目标品牌就写湘仪"时是既有行为（``湘仪`` 是那家公司登记名的子串），
    # 那不是这次引入的，这次也没有收窄它。
    assert product_configuration._own_brand_page(source, '湘仪') is True


def test_diverse_candidates_already_goes_through_the_alias_table():
    """``research_planning.diverse_candidates`` 两侧本来就是 ``brand_identity`` 的输出。

    所以它不属于"用 ``identity`` 不查别名表"那一类，不需要改；也刻意不换成
    ``brand_name_matches``——那会把企业登记全名那条桥也放进来，把本来能占一个
    "其他品牌"验证名额的候选当成同品牌剔掉。
    """
    selected, references = diverse_candidates([{'brand': '湖南湘仪', 'model': 'XKA-2200'}], '湘仪')
    assert selected == []
    assert references[0]['reference_reason'] == 'same_brand'


def test_known_bridge_behaviour_for_the_balance_company_is_not_touched_here():
    """「同行企业登记全名做桥」那条判据会认湘仪天平——既有行为，不是这次引入。

    ``brand_name_matches('湘仪', '长沙高新开发区湘仪天平仪器设备有限公司')`` 今天就是
    True（长名以「有限公司」结尾、短名是它的子串）。它单独评估，
    这次一行没动它，这里把它钉住，免得下一次有人把账记到别名表头上。
    """
    assert brand_name_matches('湘仪', '长沙高新开发区湘仪天平仪器设备有限公司') is True
    assert brand_identity('湘仪') != brand_identity('长沙高新开发区湘仪天平仪器设备有限公司')
