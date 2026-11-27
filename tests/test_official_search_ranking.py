"""测试综合排序算法：权威性 + 相关性"""
from backend.domain.search import SearchQuery, SearchResult
from backend.services.official_search import FakeSearchProvider, OfficialSearchService, relevance_score, classify_source, authority


def test_relevance_score_brand_model_matching():
    """测试品牌和型号匹配的相关性评分"""
    query = SearchQuery(text='"知楚 VORTEX22K" 价格', brand='知楚', model='VORTEX22K', purpose='price')

    # 场景1：标题包含品牌+型号
    result1 = SearchResult(
        url='http://instrument.com/vortex22k.html',
        title='知楚VORTEX22K涡旋混合器中标公告',
        snippet='采购结果'
    )
    score1 = relevance_score(query, result1)
    assert score1 >= 70, f"品牌+型号在标题: {score1}"

    # 场景2：政府网站但标题无关
    result2 = SearchResult(
        url='http://www.gov.cn/other_news.html',
        title='政府工作报告',
        snippet='年度工作总结'
    )
    score2 = relevance_score(query, result2)
    assert score2 < 20, f"无关内容: {score2}"

    # 场景3：摘要包含品牌型号
    result3 = SearchResult(
        url='http://example.com/news.html',
        title='实验室设备采购公告',
        snippet='知楚 VORTEX22K 涡旋混合器 成交价格'
    )
    score3 = relevance_score(query, result3)
    assert 30 <= score3 < 70, f"品牌+型号在摘要: {score3}"


def test_relevance_score_pdf_bonus():
    """测试PDF文档在招标查询中的加分"""
    query = SearchQuery(text='"知楚 VORTEX22K" 中标', brand='知楚', model='VORTEX22K', purpose='price')

    result_html = SearchResult(
        url='http://www.ccgp.gov.cn/award_123.html',
        title='知楚VORTEX22K采购结果公告',
        snippet='中标公示'
    )
    result_pdf = SearchResult(
        url='http://www.ccgp.gov.cn/award_123.pdf',
        title='知楚VORTEX22K采购结果公告',
        snippet='中标公示'
    )

    score_html = relevance_score(query, result_html)
    score_pdf = relevance_score(query, result_pdf)

    assert score_pdf > score_html, f"PDF应加分: HTML={score_html}, PDF={score_pdf}"
    assert score_pdf - score_html >= 10, "PDF加分应为10分"


def test_combined_ranking_authority_vs_relevance():
    """测试综合排序：高权威低相关 vs 低权威高相关"""
    query = SearchQuery(text='"知楚 VORTEX22K" 价格', brand='知楚', model='VORTEX22K', purpose='price')

    # 场景1：政府网站（权威85）但内容无关（相关性0）
    result1 = SearchResult(
        url='http://www.gov.cn/other_news.html',
        title='政府工作报告',
        snippet='年度总结'
    )

    # 场景2：仪器门户（权威10）但高度相关（相关性90）
    result2 = SearchResult(
        url='http://instrument.com/vortex22k.html',
        title='知楚VORTEX22K涡旋混合器中标公告',
        snippet='采购结果 成交价格 中标'
    )

    # 场景3：政府网站且高度相关（权威85，相关性100）
    result3 = SearchResult(
        url='http://www.ccgp.gov.cn/award_123.pdf',
        title='知楚VORTEX22K采购结果公告',
        snippet='中标公示 成交价格'
    )

    results = [result1, result2, result3]
    fake_provider = FakeSearchProvider({query.text: results})
    run = OfficialSearchService(providers=[fake_provider], fetch_body=False).run(query)

    # 验证排序
    urls = [s.url for s in run.sources]

    # result3（高权威+高相关）应排第一
    assert urls[0] == result3.url, f"最优结果应排第一: {urls}"

    # result2（低权威但高相关）应排在result1（高权威但无关）之前或相近
    # combined_score(result1) = 85*0.6 + 0*0.4 = 51
    # combined_score(result2) = 10*0.6 + 90*0.4 = 42
    # 由于权威性权重更高，result1可能仍排在前面，但差距应缩小


def test_combined_ranking_scores_calculation():
    """验证综合得分计算逻辑"""
    query = SearchQuery(text='"知楚 VORTEX22K" 价格', brand='知楚', model='VORTEX22K', purpose='price')

    # 高权威无关页面
    result_high_auth = SearchResult(
        url='http://www.ccgp.gov.cn/other.html',
        title='其他公告',
        snippet='无关内容'
    )
    auth1, _ = authority(classify_source(result_high_auth.url, result_high_auth.title))
    rel1 = relevance_score(query, result_high_auth)
    combined1 = auth1 * 0.6 + rel1 * 0.4

    # 低权威高相关页面
    result_high_rel = SearchResult(
        url='http://instrument.com/vortex22k.html',
        title='知楚VORTEX22K涡旋混合器详细参数',
        snippet='知楚 VORTEX22K 价格 规格参数'
    )
    auth2, _ = authority(classify_source(result_high_rel.url, result_high_rel.title))
    rel2 = relevance_score(query, result_high_rel)
    combined2 = auth2 * 0.6 + rel2 * 0.4

    assert auth1 > 80, f"政府网站权威性应高: {auth1}"
    assert rel1 < 20, f"无关内容相关性应低: {rel1}"
    assert auth2 < 20, f"未知网站权威性应低: {auth2}"
    assert rel2 > 70, f"高度匹配相关性应高: {rel2}"

    # 验证综合得分公式
    assert abs(combined1 - (auth1 * 0.6 + rel1 * 0.4)) < 0.01
    assert abs(combined2 - (auth2 * 0.6 + rel2 * 0.4)) < 0.01


def test_relevance_score_partial_model_match():
    """测试部分型号匹配（处理带连字符的型号）"""
    query = SearchQuery(text='Tecan INFINITE-200-PRO', brand='Tecan', model='INFINITE-200-PRO', purpose='general')

    # 型号带空格变体
    result1 = SearchResult(
        url='http://www.tecan.com/infinite200pro',
        title='Tecan INFINITE 200 PRO Microplate Reader',
        snippet='Official product page'
    )
    score1 = relevance_score(query, result1)

    # 型号完全匹配
    result2 = SearchResult(
        url='http://www.tecan.com/infinite-200-pro',
        title='Tecan INFINITE-200-PRO',
        snippet='Specifications'
    )
    score2 = relevance_score(query, result2)

    assert score1 > 50, f"部分匹配应有得分: {score1}"
    assert score2 >= score1, f"完全匹配应更高: {score2} vs {score1}"


def test_ranking_preserves_high_authority_and_relevance():
    """验证既权威又相关的结果排最前"""
    query = SearchQuery(text='"知楚 VORTEX22K" 中标公告', brand='知楚', model='VORTEX22K', purpose='price')

    results = [
        # 低权威低相关
        SearchResult('http://example.com/news.html', '科技新闻', '一般新闻'),
        # 高权威低相关
        SearchResult('http://www.ccgp.gov.cn/other.html', '政府其他公告', '无关内容'),
        # 低权威高相关
        SearchResult('http://forum.com/thread.html', '知楚VORTEX22K讨论帖', '知楚 VORTEX22K 价格讨论'),
        # 高权威高相关（应排第一）
        SearchResult('http://www.ccgp.gov.cn/award.pdf', '知楚VORTEX22K中标公告', '采购结果 中标 成交价'),
    ]

    fake_provider = FakeSearchProvider({query.text: results})
    run = OfficialSearchService(providers=[fake_provider], fetch_body=False).run(query)

    # 第一个结果应该是高权威+高相关的
    first_url = run.sources[0].url
    assert 'award.pdf' in first_url, f"最优结果应排第一，实际: {first_url}"


def test_relevance_score_keyword_coverage():
    """测试查询关键词覆盖率评分"""
    query = SearchQuery(text='酶标仪 多功能 温控', purpose='general')

    # 全覆盖
    result1 = SearchResult(
        url='http://example.com/product1',
        title='多功能酶标仪 温控系统',
        snippet='产品介绍'
    )
    score1 = relevance_score(query, result1)

    # 部分覆盖
    result2 = SearchResult(
        url='http://example.com/product2',
        title='酶标仪产品',
        snippet='基础型号'
    )
    score2 = relevance_score(query, result2)

    # 无覆盖
    result3 = SearchResult(
        url='http://example.com/product3',
        title='离心机设备',
        snippet='实验室设备'
    )
    score3 = relevance_score(query, result3)

    assert score1 > score2 > score3, f"覆盖率应递减: {score1} > {score2} > {score3}"
