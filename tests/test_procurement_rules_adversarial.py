"""对抗性测试套件：业务服务层

测试procurement_rules等关键业务逻辑在极端场景下的行为
"""
import pytest
from backend.services.procurement_rules_improved import (
    identity, brand_identity, contains_model, supported, amount, grounded_parameter,
    _BRAND_ALIASES,
)
from backend.services.procurement_rules import similarity_assessment


class TestIdentityAdversarial:
    """identity()函数对抗性测试"""

    def test_none_rejected(self):
        """None应被拒绝"""
        with pytest.raises(ValueError, match="cannot be empty"):
            identity(None)

    def test_empty_string_rejected(self):
        """空字符串应被拒绝"""
        with pytest.raises(ValueError, match="cannot be empty"):
            identity("")

    def test_whitespace_only_rejected(self):
        """仅空格应被拒绝"""
        with pytest.raises(ValueError, match="identity value cannot be empty"):
            identity("   ")

    def test_special_chars_only_rejected(self):
        """仅特殊字符应被拒绝"""
        with pytest.raises(ValueError, match="becomes empty after normalization"):
            identity("---")

    def test_unicode_normalization(self):
        """Unicode标准化"""
        # 全角字符应被标准化
        assert identity("Ａ１００") == identity("A100")

    def test_case_insensitive(self):
        """大小写不敏感"""
        assert identity("X100") == identity("x100")
        assert identity("X100") == identity("X100")

    def test_preserves_digits(self):
        """保留数字"""
        assert identity("X100S") == "x100s"
        assert identity("X-100-S") == "x100s"

    def test_no_merge_similar_models(self):
        """相似型号不应合并"""
        # X100和X100S是不同型号
        assert identity("X100") != identity("X100S")


class TestBrandIdentityAdversarial:
    """brand_identity()函数对抗性测试"""

    def test_none_rejected(self):
        """None应被拒绝"""
        with pytest.raises(ValueError, match="brand value cannot be empty"):
            brand_identity(None)

    def test_empty_string_rejected(self):
        """空字符串应被拒绝"""
        with pytest.raises(ValueError, match="brand value cannot be empty"):
            brand_identity("")

    def test_unknown_brand_passthrough(self):
        """未知品牌应原样返回"""
        result = brand_identity("UnknownBrand")
        assert result == identity("UnknownBrand")

    def test_alias_mapping(self):
        """别名映射"""
        assert brand_identity("天津泰斯特") == identity("泰斯特")
        assert brand_identity("上海一恒") == identity("一恒")
        # 中英对照：上海申安医疗器械厂官网「关于我们」原文写「使《shenan》牌产品……」。
        assert brand_identity("申安") == identity("上海申安")
        assert brand_identity("SHENAN") == identity("上海申安")
        assert brand_identity("Shenan") == identity("上海申安")
        # 登记名去地名：官网「公司概况」原文写「（简称湘仪）」。
        assert brand_identity("湖南湘仪") == identity("湘仪")

    def test_alias_table_keys_are_identity_formed(self):
        """表里的 key 与 value 都必须是 identity() 之后的写法。

        key 不是 ``identity()`` 之后的写法就永远查不到（``brand_identity``
        先做 ``identity()`` 再查表）；value 不是的话，另一侧无论怎么归一
        都等不上它，``left_token == right_token`` 那个分支会永远失效。
        """
        for key, value in _BRAND_ALIASES.items():
            assert key == identity(key), f'key {key!r} 不是 identity() 之后的写法'
            assert value == identity(value), f'value {value!r} 不是 identity() 之后的写法'

    def test_unaliased_brands_are_not_guessed(self):
        """没有依据的写法一律原样返回，不许被截断或猜测。

        ``湘仪`` 单独一个词不收（长沙另有湘仪天平仪器设备有限公司在用这个
        字号）；``苏净`` 单独一个词不收（江苏苏净集团旗下不止一家公司）；
        ``上海申安医疗器械厂`` 也不收——结尾是裸的「厂」，不在
        ``_ENTERPRISE_SUFFIX`` 的结尾词表里。
        """
        for value in ["湘仪", "苏净", "莱卡", "湘鑫", "上海三申", "博迅",
                      "申安医疗", "上海申安医疗器械厂", "湖南湘仪实验室仪器开发有限公司"]:
            assert brand_identity(value) == identity(value), f'{value!r} 不该有别名'


class TestContainsModelAdversarial:
    """contains_model()函数对抗性测试"""

    def test_non_string_text_rejected(self):
        """非字符串text应被拒绝"""
        with pytest.raises(ValueError, match="text must be string"):
            contains_model(123, "X100")

    def test_empty_model_rejected(self):
        """空model应被拒绝"""
        with pytest.raises(ValueError, match="model cannot be empty"):
            contains_model("some text", "")

    def test_none_model_rejected(self):
        """None model应被拒绝"""
        with pytest.raises(ValueError, match="model cannot be empty"):
            contains_model("some text", None)

    def test_recursion_depth_limit(self):
        """递归深度限制"""
        result = contains_model("text", "model", max_depth=0)
        assert result is False

    def test_word_boundary_enforcement(self):
        """单词边界强制执行"""
        # X100应匹配"X100"但不匹配"AX100"
        assert contains_model("型号 X100 参数", "X100")
        assert not contains_model("型号 AX100 参数", "X100")

    def test_hyphen_space_variants(self):
        """连字符和空格变体"""
        assert contains_model("X-100", "X100")
        assert contains_model("X 100", "X100")
        assert contains_model("X‐100", "X100")  # Unicode连字符

    def test_case_insensitive_matching(self):
        """大小写不敏感匹配"""
        assert contains_model("型号 x100", "X100")
        assert contains_model("型号 X100", "x100")

    def test_special_regex_chars_escaped(self):
        """特殊正则字符应被转义"""
        # 型号中包含特殊字符不应导致正则错误
        assert contains_model("型号 X+100", "X+100")
        assert contains_model("型号 X.100", "X.100")

    def test_invalid_regex_handled(self):
        """无效正则应被处理"""
        # 某些极端情况可能导致正则无效，应优雅处理
        try:
            contains_model("text", "(invalid")
        except ValueError:
            pass  # 应抛出ValueError而非re.error


class TestSupportedAdversarial:
    """supported()函数对抗性测试"""

    def test_non_string_inputs(self):
        """非字符串输入应返回False"""
        assert not supported(None, "body")
        assert not supported("excerpt", None)
        assert not supported(123, "body")
        assert not supported("excerpt", 456)

    def test_empty_excerpt_rejected(self):
        """空摘录应返回False"""
        assert not supported("", "body text")
        assert not supported("   ", "body text")

    def test_whitespace_normalized(self):
        """空格应被标准化"""
        assert supported("hello world", "hello  world")
        assert supported("hello\nworld", "hello world")
        assert supported("hello\tworld", "hello world")

    def test_excerpt_not_in_body(self):
        """摘录不在正文中应返回False"""
        assert not supported("not found", "some body text")

    def test_substring_match(self):
        """子串匹配"""
        assert supported("body", "some body text")
        assert supported("body text", "some body text here")


class TestAmountAdversarial:
    """amount()函数对抗性测试"""

    def test_none_returns_none(self):
        """None应返回None"""
        assert amount(None) is None

    def test_negative_returns_none(self):
        """负数应返回None"""
        assert amount(-1) is None
        assert amount(-0.01) is None

    def test_infinity_returns_none(self):
        """无穷大应返回None"""
        assert amount(float('inf')) is None
        assert amount(float('-inf')) is None

    def test_nan_returns_none(self):
        """NaN应返回None"""
        assert amount(float('nan')) is None

    def test_zero_valid(self):
        """0是有效的"""
        assert amount(0) == 0.0

    def test_positive_valid(self):
        """正数是有效的"""
        assert amount(100) == 100.0
        assert amount(99.99) == 99.99

    def test_string_invalid(self):
        """字符串应可转换"""
        assert amount("100") == 100.0

    def test_bool_invalid(self):
        """布尔值应返回None"""
        assert amount(True) is None
        assert amount(False) is None


class TestGroundedParameterAdversarial:
    """grounded_parameter()函数对抗性测试"""

    def test_non_dict_observed_rejected(self):
        """非dict的observed应被拒绝"""
        with pytest.raises(ValueError, match="observed must be dict"):
            grounded_parameter("not dict", {}, "X100", "temperature")

    def test_non_dict_source_rejected(self):
        """非dict的source应被拒绝"""
        with pytest.raises(ValueError, match="source must be dict"):
            grounded_parameter({}, "not dict", "X100", "temperature")

    def test_empty_model_rejected(self):
        """空model应被拒绝"""
        with pytest.raises(ValueError, match="model cannot be empty"):
            grounded_parameter({}, {}, "", "temperature")

    def test_empty_name_rejected(self):
        """空name应被拒绝"""
        with pytest.raises(ValueError, match="name cannot be empty"):
            grounded_parameter({}, {}, "X100", "")

    def test_recursion_depth_limit(self):
        """递归深度限制"""
        observed = {"excerpt": "test"}
        source = {
            "source_type": "manufacturer_website",
            "body_status": "fetched",
            "body_text": "test"
        }

        result = grounded_parameter(
            observed, source, "X100", "temperature",
            max_recursion=0
        )
        assert result is False

    def test_unverified_source_rejected_by_default(self):
        """未验证来源默认被拒绝"""
        observed = {"excerpt": "X100 temperature 25℃"}
        source = {
            "source_type": "dealer",
            "body_status": "fetched",
            "body_text": "X100 temperature 25℃"
        }

        result = grounded_parameter(observed, source, "X100", "temperature")
        assert result is False

    def test_body_not_fetched_rejected(self):
        """正文未获取应被拒绝"""
        observed = {"excerpt": "X100 temperature 25℃"}
        source = {
            "source_type": "manufacturer_website",
            "body_status": "not_requested",
            "body_text": ""
        }

        result = grounded_parameter(observed, source, "X100", "temperature")
        assert result is False

    def test_empty_excerpt_rejected(self):
        """空摘录应被拒绝"""
        observed = {"excerpt": ""}
        source = {
            "source_type": "manufacturer_website",
            "body_status": "fetched",
            "body_text": "some text"
        }

        result = grounded_parameter(observed, source, "X100", "temperature")
        assert result is False

    def test_excerpt_not_supported_rejected(self):
        """摘录不被正文支持应被拒绝"""
        observed = {"excerpt": "not in body"}
        source = {
            "source_type": "manufacturer_website",
            "body_status": "fetched",
            "body_text": "different text"
        }

        result = grounded_parameter(observed, source, "X100", "temperature")
        assert result is False

    def test_model_not_in_excerpt_rejected(self):
        """型号不在摘录中应被拒绝"""
        observed = {"excerpt": "Y200 temperature 25℃"}
        source = {
            "source_type": "manufacturer_website",
            "body_status": "fetched",
            "body_text": "Y200 temperature 25℃"
        }

        result = grounded_parameter(observed, source, "X100", "temperature")
        assert result is False

    def test_parameter_name_not_in_excerpt_rejected(self):
        """参数名不在摘录中应被拒绝"""
        observed = {"excerpt": "X100 humidity 60%"}
        source = {
            "source_type": "manufacturer_website",
            "body_status": "fetched",
            "body_text": "X100 humidity 60%"
        }

        result = grounded_parameter(observed, source, "X100", "temperature")
        assert result is False


class TestEdgeCasesIntegration:
    """集成边界情况测试"""

    def test_unicode_edge_cases(self):
        """Unicode边界情况"""
        # 全角数字
        assert identity("１２３") == identity("123")

        # 不同的连字符 - NFKC会将U+2212减号转为U+002D
        # 但我们的正则也匹配U+2212，所以两种方式都能移除
        assert identity("X‐100") == identity("X-100")
        # U+2212减号会被NFKC转为普通减号，然后被移除
        assert identity("X−100") == identity("X-100")

    def test_extreme_lengths(self):
        """极端长度"""
        # 超长字符串
        long_text = "x" * 10000
        result = contains_model(long_text, "X100")
        assert result is False  # 不应崩溃

    def test_special_characters_comprehensive(self):
        """全面的特殊字符测试"""
        special_chars = [
            "(", ")", "[", "]", "{", "}", ".", "*", "+", "?", "^", "$", "|", "\\"
        ]

        for char in special_chars:
            model = f"X{char}100"
            text = f"型号 {model} 参数"
            try:
                # 不应崩溃
                result = contains_model(text, model)
                # 应该能匹配（如果正则正确转义）
                assert result
            except ValueError:
                # 或者抛出清晰的错误
                pass


class TestSimilarityAssessmentAdversarial:
    """70%比较阈值不能把缺证或未知伪装成满足。"""

    @staticmethod
    def row(name, status, evidence_status='confirmed', conflict=False):
        return {
            'required': {'name': name, 'value': 1},
            'status': status,
            'evidence_status': evidence_status,
            'conflict': conflict,
        }

    def test_threshold_keeps_difference_visible(self):
        result = similarity_assessment([
            self.row('a', 'satisfied'), self.row('b', 'satisfied'),
            self.row('c', 'satisfied'), self.row('d', 'violated'),
        ])
        assert result['comparison_disposition'] == 'comparable_candidate'
        assert [item['name'] for item in result['difference_parameters']] == ['d']

    def test_unknown_and_conflict_are_gaps_not_matches(self):
        result = similarity_assessment([
            self.row('a', 'satisfied'), self.row('b', 'unknown'),
            self.row('c', 'satisfied', conflict=True),
        ])
        assert result['similarity_score'] == 1.0
        assert result['similarity_counts']['satisfied'] == 1
        assert result['similarity_counts']['missing'] == 2
        assert {item['name'] for item in result['missing_parameters']} == {'b', 'c'}

    def test_provisional_satisfaction_is_not_verified(self):
        result = similarity_assessment([
            self.row('a', 'satisfied', evidence_status='provisional'),
            self.row('b', 'satisfied'), self.row('c', 'violated'),
        ])
        assert result['similarity_score'] == 0.5
        assert result['comparison_disposition'] == 'needs_review'


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
