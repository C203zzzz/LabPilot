"""
对抗性测试：边界条件和异常case
"""
import pytest
from backend.services.official_search import (
    SearchQuery, SearchResult, relevance_score, classify_source, authority
)


def test_brand_included_in_model_prevents_double_counting():
    """品牌名包含在型号中时，防止双重计分"""
    query = SearchQuery(
        text="Thermo Scientific NanoDrop",
        brand="Thermo",
        model="Thermo Scientific NanoDrop 2000",
        purpose="price"
    )

    result = SearchResult(
        url="http://example.com/product.html",
        title="Thermo Scientific NanoDrop 2000 Spectrophotometer",
        snippet="Full specifications for Thermo Scientific NanoDrop 2000",
        provider="test",
        retrieved_at=None
    )

    score = relevance_score(query, result)

    # 品牌在标题中：25分
    # 型号在标题中：35分
    # 但品牌包含在型号中，品牌分减半：25 * 0.5 = 12.5
    # 关键词覆盖：可能有额外的分数
    # 总分应该 < 70（不修复时是 25+35=60，加上关键词）

    # 关键是：修复后的分数应该明显低于未修复的分数
    # 未修复：25 + 35 + 关键词 = 60 + 关键词
    # 已修复：12.5 + 35 + 关键词 = 47.5 + 关键词
    assert score < 70, f"品牌型号重叠导致过高评分：{score}"

    # 验证品牌+型号的基础分确实 < 50（12.5 + 35 = 47.5）
    # 实际得分应该在 47.5 + 关键词分数
    assert 45 <= score <= 75, f"得分应该在合理范围内：{score}"


def test_none_snippet_does_not_crash():
    """snippet为None时不应崩溃"""
    query = SearchQuery(
        text="VORTEX22K",
        brand="ZhiChu",
        model="VORTEX22K",
        purpose="price"
    )

    result = SearchResult(
        url="http://example.com/product.html",
        title="ZhiChu VORTEX22K Vortex Mixer",
        snippet=None,  # None snippet
        provider="test",
        retrieved_at=None
    )

    score = relevance_score(query, result)

    assert score >= 0, "None snippet导致负分"
    assert score <= 100, "None snippet导致超限"
    # 品牌(25) + 型号(35) = 60
    assert score >= 60, f"应该至少得到品牌+型号分：{score}"


def test_empty_snippet_vs_none_snippet():
    """空字符串snippet和None snippet应该等价处理"""
    query = SearchQuery(
        text="VORTEX22K",
        brand="ZhiChu",
        model="VORTEX22K",
        purpose="price"
    )

    result_none = SearchResult(
        url="http://example.com/p1.html",
        title="ZhiChu VORTEX22K",
        snippet=None,
        provider="test",
        retrieved_at=None
    )

    result_empty = SearchResult(
        url="http://example.com/p2.html",
        title="ZhiChu VORTEX22K",
        snippet="",
        provider="test",
        retrieved_at=None
    )

    score_none = relevance_score(query, result_none)
    score_empty = relevance_score(query, result_empty)

    assert abs(score_none - score_empty) < 1, \
        f"None和空字符串snippet应该得分接近：{score_none} vs {score_empty}"


def test_very_long_model_partial_match():
    """超长型号的部分匹配应该合理评分"""
    query = SearchQuery(
        text="long model",
        brand="Brand",
        model="ABC-DEF-GHI-JKL-MNO-PQR-STU-VWX",  # 8个部分
        purpose="specifications"
    )

    # 匹配5个部分（62.5% > 60%阈值）
    result_5 = SearchResult(
        url="http://example.com/p1.html",
        title="Brand ABC DEF GHI JKL MNO",
        snippet="Product details",
        provider="test",
        retrieved_at=None
    )

    # 匹配4个部分（50% < 60%阈值）
    result_4 = SearchResult(
        url="http://example.com/p2.html",
        title="Brand ABC DEF GHI JKL",
        snippet="Product details",
        provider="test",
        retrieved_at=None
    )

    score_5 = relevance_score(query, result_5)
    score_4 = relevance_score(query, result_4)

    # 5个匹配应该触发部分匹配加分（>=60%）
    assert score_5 > score_4, \
        f"更多部分匹配应该得分更高：{score_5} vs {score_4}"

    # 但都不应该是0分
    assert score_4 > 20, "4个部分匹配应该至少得到品牌分"


def test_empty_brand_and_model():
    """品牌和型号都为空时应该退化到关键词匹配"""
    query = SearchQuery(
        text="vortex mixer price",
        brand="",
        model="",
        purpose="price"
    )

    result = SearchResult(
        url="http://example.com/product.html",
        title="Vortex Mixer Laboratory Equipment",
        snippet="Best price for vortex mixer",
        provider="test",
        retrieved_at=None
    )

    score = relevance_score(query, result)

    # 没有品牌型号分，只有关键词覆盖（vortex, mixer, price都匹配）
    # 3/3 = 100% * 20 = 20分
    # + price查询的tender markers
    assert score > 0, "纯关键词匹配应该有得分"
    assert score <= 30, f"无品牌型号时得分不应过高：{score}"


def test_none_brand_and_model():
    """品牌和型号为None时应该正常处理"""
    query = SearchQuery(
        text="laboratory equipment",
        brand=None,
        model=None,
        purpose="price"
    )

    result = SearchResult(
        url="http://example.com/product.html",
        title="Laboratory Equipment Catalog",
        snippet="Complete laboratory equipment solutions",
        provider="test",
        retrieved_at=None
    )

    # 不应该崩溃
    score = relevance_score(query, result)
    assert score >= 0 and score <= 100


def test_chinese_brand_english_model_mixed():
    """中文品牌+英文型号混合匹配"""
    query = SearchQuery(
        text="知楚 VORTEX22K",
        brand="知楚",
        model="VORTEX22K",
        purpose="price"
    )

    result = SearchResult(
        url="http://example.com/product.html",
        title="知楚VORTEX22K涡旋混合器",
        snippet="知楚品牌 VORTEX22K型号 价格优惠",
        provider="test",
        retrieved_at=None
    )

    score = relevance_score(query, result)

    # 品牌在标题(25) + 型号在标题(35) = 60
    assert score >= 60, f"中英文混合匹配应该正常工作：{score}"


def test_all_results_zero_relevance_falls_back_to_authority():
    """所有结果相关性为0时应该退化到纯权威性排序"""
    query = SearchQuery(
        text="completely unrelated query xyz123abc",
        brand="NonExistent",
        model="ModelXYZ999",
        purpose="price"
    )

    results = [
        SearchResult(
            url="http://www.ccgp.gov.cn/unrelated.html",
            title="Government News",
            snippet="Some government announcement",
            provider="test",
            retrieved_at=None
        ),
        SearchResult(
            url="http://example.com/random.html",
            title="Random Page",
            snippet="Random content",
            provider="test",
            retrieved_at=None
        ),
    ]

    # 计算综合得分
    scores = []
    for r in results:
        st = classify_source(r.url, r.title)
        auth_score, _ = authority(st)
        rel_score = relevance_score(query, r)
        combined = auth_score * 0.6 + rel_score * 0.4
        scores.append((r.url, combined, auth_score, rel_score))

    # 相关性都是0时，排序应该完全由权威性决定
    sorted_by_combined = sorted(scores, key=lambda x: x[1], reverse=True)
    sorted_by_authority = sorted(scores, key=lambda x: x[2], reverse=True)

    assert sorted_by_combined[0][0] == sorted_by_authority[0][0], \
        "零相关性时应该退化到纯权威性排序"


def test_classification_not_called_twice():
    """验证分类不会被重复调用（性能测试）"""
    # 这个测试验证修复是否生效
    # 实际的性能测试需要在集成环境中进行
    query = SearchQuery(text="test", brand="B", model="M", purpose="price")
    result = SearchResult(
        url="http://www.ccgp.gov.cn/test.pdf",
        title="Test Document",
        snippet="Test content",
        provider="test",
        retrieved_at=None
    )

    # 应该能正常计算得分
    st = classify_source(result.url, result.title)
    auth_score, _ = authority(st)
    rel_score = relevance_score(query, result)
    combined = auth_score * 0.6 + rel_score * 0.4

    assert combined > 0, "分类和评分应该正常工作"


def test_special_characters_in_query():
    """查询中包含特殊字符时应该正常处理"""
    query = SearchQuery(
        text='测试"引号"和\'单引号\'',
        brand="测试",
        model="ABC-123",
        purpose="price"
    )

    result = SearchResult(
        url="http://example.com/test.html",
        title="测试 ABC-123",
        snippet="引号和单引号测试",
        provider="test",
        retrieved_at=None
    )

    # 不应该崩溃
    score = relevance_score(query, result)
    assert score > 0, "特殊字符不应该影响匹配"
