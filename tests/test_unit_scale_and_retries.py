"""2026-10-05 三条批注的回归测试：单位/区间判定、通道重试、页面质量判据。

每一条都钉住"修好之后的行为"，并写清它对应哪个真实现象。
"""
import time

import pytest

from backend.infrastructure.page_reader import quality
from backend.infrastructure.search_providers import (
    CCGPSearchProvider,
    ProviderConfigurationError,
    PublicWebSearchProvider,
)
from backend.domain.search import SearchQuery
from backend.services.procurement_rules import (
    evaluate_parameter,
    split_requirement_range,
    unit_scale,
)


def observation(value, unit):
    return {'value': value, 'unit': unit, 'conditions': '', 'evidence': {'url': 'http://x'}}


# ---------------------------------------------------------------- 单位归一

def test_chinese_and_latin_volume_units_are_the_same_dimension():
    """批3 灭菌器：要求写"50 升"，页面写"50 L"，原先整串查表查不到 → unknown。"""
    assert unit_scale('升') == unit_scale('L') == ('volume', 1000)
    assert evaluate_parameter(
        {'name': '有效容积', 'value': 50, 'unit': '升', 'direction': 'equal', 'conditions': ''},
        observation(50.0, 'L')) == 'satisfied'
    assert evaluate_parameter(
        {'name': '有效容积', 'value': 50, 'unit': '升', 'direction': 'equal', 'conditions': ''},
        observation(200.0, 'L')) == 'violated'


def test_unit_with_a_trailing_note_still_yields_its_dimension():
    """模型会把说明写进单位字段：``直径*71深 cm``、``分钟-1周延迟``。"""
    assert unit_scale('直径*71深 cm') == ('length', 10)
    assert unit_scale('分钟-1周延迟') == ('time', 60)
    assert unit_scale('种方式（包括自然降温') is None, '没有单位词的串不许猜出一个单位'


# ---------------------------------------------------------------- 区间要求

def test_range_hidden_in_the_unit_becomes_a_range_requirement():
    """``value=105, unit='-135℃（可调'`` 表达的是 105~135℃。"""
    split = split_requirement_range(
        {'name': '灭菌温度', 'value': 105, 'unit': '-135℃（可调', 'direction': 'equal', 'conditions': ''})
    assert split['value'] == [105, 135]
    assert split['direction'] == 'covers'
    assert split['unit'] == '℃'


def test_a_single_observed_point_inside_the_range_is_satisfied():
    """观测只有一个数时：落在区间内记 satisfied、落在外面记 violated，不再一律 unknown。"""
    requirement = {'name': '灭菌温度', 'value': 105, 'unit': '-135℃（可调',
                   'direction': 'equal', 'conditions': ''}
    assert evaluate_parameter(requirement, observation(105.0, '℃')) == 'satisfied'
    assert evaluate_parameter(requirement, observation(120.0, '℃')) == 'satisfied'
    assert evaluate_parameter(requirement, observation(140.0, '℃')) == 'violated'
    # 保温温度 45~60℃：观测 60 落进区间，修复前会被等价判成"必须等于 45"。
    warm = {'name': '保温温度', 'value': 45, 'unit': '-60℃（可调', 'direction': 'equal', 'conditions': ''}
    assert evaluate_parameter(warm, observation(60.0, '℃')) == 'satisfied'


def test_a_plain_requirement_is_left_alone():
    plain = {'name': '水浴槽数', 'value': 4, 'unit': '个', 'direction': 'equal', 'conditions': ''}
    assert split_requirement_range(plain) == plain


# ---------------------------------------------------------------- 页面质量判据

def test_a_long_article_mentioning_verification_is_not_a_challenge_page():
    """antpedia 的产品页正文 9.2K、标题就是目标机器，却被判成人机验证页。"""
    body = '博迅BJ-1CD净化工作台 技术参数 安全验证说明 ' + '参数行 ' * 900
    assert len(body) > 3000
    assert quality(body) is None


def test_a_short_challenge_page_is_still_a_challenge_page():
    assert quality('请完成人机验证' + ' 页面载入' * 80) == 'verification_required'


def test_a_short_forbidden_page_counts_as_an_access_block():
    body = 'METTLER TOLEDO - 403 Forbidden We are sorry, but your current behavior is detected as abnormal'
    assert quality(body) == 'verification_required'


# ---------------------------------------------------------------- 通道重试

def test_ccgp_retries_once_after_a_timeout(monkeypatch):
    calls = []

    def flaky(_self, url, timeout, **form):
        calls.append(url)
        if len(calls) == 1:
            raise TimeoutError('timed out')
        return b'<html>ok</html>', 'text/html'

    monkeypatch.setattr(PublicWebSearchProvider, '_request', flaky)
    provider = CCGPSearchProvider(transient_backoff=0.0, min_interval=0.0)
    payload, _content_type = provider._request('http://www.ccgp.gov.cn/cggg/dfgg/zbgg/t1.htm', 5)
    assert payload == b'<html>ok</html>'
    assert len(calls) == 2


def test_a_paused_ccgp_is_never_retried(monkeypatch):
    """暂停期内的请求不许碰网——重试只针对"刚刚撞上的限流/超时"。"""
    def forbidden(*_args, **_kwargs):
        raise AssertionError('a paused channel must not send another request')

    monkeypatch.setattr(PublicWebSearchProvider, '_request', forbidden)
    provider = CCGPSearchProvider(paused_until=time.monotonic() + 60, transient_backoff=0.0)
    with pytest.raises(ProviderConfigurationError, match='paused'):
        provider._request('http://www.ccgp.gov.cn/cggg/dfgg/zbgg/t1.htm', 5)
