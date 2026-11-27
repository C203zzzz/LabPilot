# -*- coding: utf-8 -*-
"""列式表（单位在表头、数据行裸数字）必须能进入记录型模型抽取。

背景是两份真实正文：

* 公告 https://ggzyjyw.hlj.gov.cn/jyfwdt/003002/003002002/003002002004/20251121/2c90835b9a71baac019aa4e3d4b3224d.html
  （黑龙江公共资源交易网《中国疾病预防控制中心地方病控制中心地病中心2025新楼设备中标（成交）结果公告》，正文 3857 字符）
* 附件 https://hljcg.hlj.gov.cn/gpx-bid-file/ZF_JGBM_000002/230001/2025/11/11/2c9080129a29a243019a33f3b9191cfa/gpx-bidconfirm/2c908ab79a32eb3f019aa4e3d2ee2d5e.pdf?accessCode=f939cf8519fb908bd68cccdde59e229c
  （同一公告的《报价明细附件》，正文 2715 字符）

两份都是**列式表**：货币单位只写在表头 ``单价(元)``/``单价（元）``，数据行是裸的
``43,300.00``。``prices()`` 原来在调模型前用一条「金额和单位必须在同一段文本里」
的正则做预筛，真实 region 全部不满足，两个源都 ``continue``：

* 表格解析器认不出附件的版式（PDF 文本层把加粗字画了两遍，``单价`` 读成 ``单单价价``），
  于是 ``price_table_rows`` 返回 0 行；
* 预筛又不让记录型模型去看。

两个通道同时失效，价格明细直接归零——而且是在最需要兜底的时候。

下面每一段正文都**逐字**抄自那两份真实文件。``has_amount_evidence`` 保留原判据，
只新增「单位由同一张表的表头声明」这一种情形；预算/最高限价表头不会因此被放行。
本次改动没有动 ``validate_price``。

后来又多了一类实测形态（``设计说明`` §5.3）：表头是**裸的价格列名**
（``… | 数量 | 单价``，单位写在表外），数据行是**独占单元格的裸整数**（``… | 1 | 1239000``）。
原判据的三条都不认它（第 2 类要表头里有货币单位，``_MONEY_SHAPED`` 又不认裸整数），
于是这个来源连"被问一次"的机会都没有。本文件末尾的 (c) 组钉住这一类的进出口。

判定读的始终是**原文**：这一层不折叠、不改写任何字节，引用仍然逐字落在原文上。
附件的重影表头 ``单单价价（（元元））`` 因此不需要在这里"修"——``AABB`` 的交叉处
本来就含 ``AB``。还原重影是 ``search_providers`` 那一层的事。

**这一层是成本门，不是采信门。** 通过它只说明"值得花一次模型调用"；价格最终得不
得进 ``prices`` 由 ``validate_price`` 决定，(c) 组有对应的反例测试。
"""
import re

import pytest

from tests.test_reliability_development import research  # noqa: F401 - pytest fixture

MODEL = 'H1750R'
BRAND = '湘仪'
NOTICE_URL = ('https://ggzyjyw.hlj.gov.cn/jyfwdt/003002/003002002/003002002004/20251121/'
              '2c90835b9a71baac019aa4e3d4b3224d.html')
ATTACHMENT_URL = ('https://hljcg.hlj.gov.cn/gpx-bid-file/ZF_JGBM_000002/230001/2025/11/11/'
                  '2c9080129a29a243019a33f3b9191cfa/gpx-bidconfirm/'
                  '2c908ab79a32eb3f019aa4e3d2ee2d5e.pdf?accessCode=f939cf8519fb908bd68cccdde59e229c')

#: 改之前 ``procurement_research.py:924`` 的原文（逐字），用来钉住"当年为什么被跳过"。
#: 留着它，是因为"现在能过"必须能和"当时不能过"对照，否则无从判断这是修好了还是
#: 这段代码本来就没挡住什么。
OLD_GATE = re.compile(
    r'(?:\d[\d,.]*\s*(?:万元|千元|元|人民币|CNY|RMB|USD|EUR))'
    r'|(?:[¥￥$€]|单价|成交价|中标价)\s*[:：]?\s*\d', re.I)

#: 公告正文第 29-32 行，逐字（原文「四、主要标的信息」表格）。
NOTICE_TABLE = (
    '品目号 | 品目名称 | 采购标的 | 品牌 | 规格型号 | 数量（单位） | 单价(元) | 总价(元)\n'
    '1-1 | 其他仪器仪表 | 高速冷冻离心机（生物安全型） | 湘仪 | H1750R | 2.00(台) | 43,300.00 | 86,600.00\n'
    '1-2 | 其他仪器仪表 | 恒温培养箱 | 上海搏旅 | CT-150i | 2.00(台) | 23,500.00 | 47,000.00\n'
    '1-3 | 其他仪器仪表 | 试剂冷藏保存箱 | 中科美菱 | YC-315L | 2.00(台) | 8,000.00 | 16,000.00\n'
)

#: 附件正文第 1-3 行，逐字。注意表头的重影：``单单价价（（元元））``——这是 PDF
#: 文本层的缺陷（另一个任务在 ``search_providers.py`` 修），不是本判据要修的东西；
#: 判据只需不因为这个缺陷而误判。
ATTACHMENT_TABLE = (
    '品品 目目 号号 | 品品目目名名 称称 | 货货物物名名称称 | 规规格格型型号号 | 品品牌牌 | 制制造造商商名名称称 | '
    '产产 地地 | 单单价价（（元元）） | 数数量量 | 单单 位位 | 总总价价（（元元））\n'
    '1-1 | 其他仪 器仪表 | 高速冷冻离心机 （生物安全型） | H1750R | 湘仪 | 湖南湘仪实验室仪 器开发有限公司 | '
    '长 沙 | 43,300.00 | 2.0000 | 台 | 86,600.00\n'
    '1-2 | 其他仪 器仪表 | 恒温培养箱 | CT-150i | 上海搏旅 | 上海搏旅生物科技 有限公司 | '
    '上 海 | 23,500.00 | 2.0000 | 台 | 47,000.00\n'
)


def _region(body, model=MODEL):
    from backend.services.research_sources import price_regions
    regions = price_regions(body, model)
    assert regions, body
    return regions[0]['text']


def _counting_client(service):
    """装一个会数调用次数的客户端；判定"模型被调用了"靠它，不靠推断。"""
    calls = []

    class Client:
        endpoint = 'test://columnar-price-gate'
        model = 'counting'
        last_usage = None

        def analyze(self, **kwargs):
            calls.append(kwargs.get('user_prompt', ''))
            return {'prices': []}

    service.client_factory = Client
    return calls


def _source(url, body):
    return {'url': url, 'body_status': 'fetched', 'source_type': 'tender_document', 'body_text': body}


# ---------------------------------------------------------------------------
# 卡点本身：真实 region 里确实没有"数字+单位"
# ---------------------------------------------------------------------------

def test_the_real_notice_region_carries_no_unit_of_its_own():
    """反例检验：公告 region 里搜不到任何「数字+元」，所以原判据必然放过它。"""
    text = _region(NOTICE_TABLE)
    assert 'H1750R' in text and '43,300.00' in text
    assert '单价(元)' in text                      # 单位只在表头这一行
    assert not re.search(r'\d[\d,.]*\s*元', text)  # 数据行没有任何一处"数字+单位"
    assert OLD_GATE.search(text) is None           # 原判据确实不命中
    assert '单价' in text and not OLD_GATE.search(text)


def test_the_real_attachment_region_carries_no_unit_of_its_own():
    text = _region(ATTACHMENT_TABLE)
    assert 'H1750R' in text and '43,300.00' in text
    assert not re.search(r'\d[\d,.]*\s*元', text)
    assert OLD_GATE.search(text) is None


# ---------------------------------------------------------------------------
# (a) 现在会进入抽取路径
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('body', [NOTICE_TABLE, ATTACHMENT_TABLE])
def test_a_real_columnar_region_now_has_amount_evidence(body):
    from backend.services.research_sources import has_amount_evidence
    assert has_amount_evidence(_region(body))


def test_the_overprinted_header_is_matched_without_rewriting_the_text():
    """附件的重影表头 ``单单价价（（元元））`` **不需要折叠**就能命中。

    伪粗体把一个字画两遍是 ``AABB``：``单[A]价[B]`` 里的 ``AB`` 本来就在第一个字的
    第二笔和第二个字的第一笔之间相邻，所以 ``单价`` 依然是一个子串。判定因此读原文
    即可——判据和最终引用落在同一份字节上，这一层不改写任何文本。

    表格通道在这份文本上仍然读不出（``price_table_rows`` 返回 0 行），模型兜底正是
    为这种情形存在的。
    """
    from backend.services.procurement_tables import price_table_rows
    from backend.services.research_sources import has_amount_evidence

    text = _region(ATTACHMENT_TABLE)
    assert '单单价价（（元元））' in text
    assert price_table_rows(ATTACHMENT_TABLE, MODEL) == []
    assert has_amount_evidence(text)


def test_both_real_sources_now_reach_the_extractor(research):
    """改之前这两个源都是 0 次调用；现在各调一次。"""
    service, run, _, _ = research
    service.verify_publishers = lambda *args, **kwargs: None
    calls = _counting_client(service)
    before = run['usage']['model']
    run['sources'] = [_source(NOTICE_URL, NOTICE_TABLE), _source(ATTACHMENT_URL, ATTACHMENT_TABLE)]

    service.prices(run, {'brand': BRAND, 'model': MODEL})

    assert len(calls) == 2
    assert run['usage']['model'] == before + 2
    assert NOTICE_URL in calls[0] and ATTACHMENT_URL in calls[1]


def test_the_attachment_source_reaches_the_extractor_with_the_model_as_its_only_reader(research):
    """附件是模型兜底真正吃紧的那一路：表格解析器在重影表头上返回 0 行。"""
    from backend.services.procurement_tables import price_table_rows
    service, run, _, _ = research
    service.verify_publishers = lambda *args, **kwargs: None
    calls = _counting_client(service)
    run['sources'] = [_source(ATTACHMENT_URL, ATTACHMENT_TABLE)]

    assert price_table_rows(ATTACHMENT_TABLE, MODEL) == []
    service.prices(run, {'brand': BRAND, 'model': MODEL})
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# (b) 预算 / 最高限价形态的文本仍然被拒
# ---------------------------------------------------------------------------

BUDGET_TABLE = (
    '包号 | 包名称 | 预算金额（元） | 最高限价（元）\n'
    '1 | 高速冷冻离心机 H1750R 采购 | 1,500,000.00 | 1,500,000.00\n'
)
PRICE_CAP_TABLE = (
    '序号 | 包名称 | 最高限价（元）\n'
    '1 | 高速冷冻离心机 H1750R | 1,500,000.00\n'
)
BUDGET_INLINE = '项目预算金额：1,500,000.00 元\n采购内容：高速冷冻离心机 H1750R\n'


@pytest.mark.parametrize('body', [BUDGET_TABLE, PRICE_CAP_TABLE])
def test_a_budget_or_price_cap_table_has_no_amount_evidence(body):
    from backend.services.research_sources import has_amount_evidence
    assert not has_amount_evidence(_region(body))


def test_a_budget_or_price_cap_source_never_reaches_the_extractor(research):
    service, run, _, _ = research
    service.verify_publishers = lambda *args, **kwargs: None
    calls = _counting_client(service)
    before = run['usage']['model']
    run['sources'] = [_source('https://www.ccgp.gov.cn/budget/demo.htm', BUDGET_TABLE)]

    service.prices(run, {'brand': BRAND, 'model': MODEL})

    assert calls == []
    assert run['usage']['model'] == before


def test_a_price_cap_header_is_not_a_price_column():
    """``最高限价（元）`` 不含任何价格列词根，所以它连"表头声明了金额列"都不算。"""
    from backend.services.research_sources import has_amount_evidence
    assert not has_amount_evidence('最高限价（元）\nH1750R | 1,500,000.00\n')


def test_a_budget_unit_price_column_is_not_a_deal_price_column():
    """``预算单价（元）`` 说的是上限，挖掉上限声明后这一行没有价格列词根。"""
    from backend.services.research_sources import has_amount_evidence
    assert not has_amount_evidence('规格型号 | 预算单价（元） | 数量\nH1750R | 1,500,000.00 | 2\n')


CAP_BESIDE_UNIT_PRICE = (
    '规格型号 | 最高限价（元） | 数量 | 单价（元）\n'
    'H1750R | 1,500,000.00 | 2 | 43,300.00\n'
)


def test_a_price_cap_column_beside_a_real_unit_price_column_still_qualifies():
    """上限列和成交单价列**并排在同一行**时，不能因为看到"最高限价"就作废整行。

    这是 ``_BUDGET_LABEL`` 只挖掉上限声明、不作废整行的原因：挖掉
    ``最高限价（元）`` 之后，这一行还留着 ``单价（元）``，那是真实的成交单价列。
    """
    from backend.services.research_sources import has_amount_evidence
    assert has_amount_evidence(_region(CAP_BESIDE_UNIT_PRICE))


def test_a_price_cap_beside_a_unit_price_column_still_reaches_the_extractor(research):
    """同上，端到端：这种公告仍然会被交给模型抽成交单价。"""
    service, run, _, _ = research
    service.verify_publishers = lambda *args, **kwargs: None
    calls = _counting_client(service)
    run['sources'] = [_source('https://www.ccgp.gov.cn/award/cap-and-price.htm', CAP_BESIDE_UNIT_PRICE)]

    service.prices(run, {'brand': BRAND, 'model': MODEL})

    assert len(calls) == 1


def test_a_same_sentence_budget_is_pre_existing_behaviour_not_a_new_route():
    """如实钉住：``项目预算金额：1,500,000.00 元`` 本来就命中**原判据**。

    同段写单位的预算是老问题，拦住它的是模型提示词和下游 ``validate_price``
    的预算/最高限价分支，不是这道预筛。这里把它标出来，免得把"预筛没拦同段预算"
    误读成本次放宽引入的新通道——本次只新增列式表分支，原判据一个字符没改。
    """
    from backend.services.research_sources import has_amount_evidence
    assert OLD_GATE.search(BUDGET_INLINE)
    assert has_amount_evidence(BUDGET_INLINE)


# ---------------------------------------------------------------------------
# 放宽的边界：什么数字不算金额
# ---------------------------------------------------------------------------

def test_a_four_decimal_quantity_is_not_money():
    """``2.0000`` 是数量精度（附件里就是这么写的），不是两位小数金额。"""
    from backend.services.research_sources import has_amount_evidence
    text = '规格型号 | 单价(元) | 数量 | 单 位\nH1750R |  | 2.0000 | 台\n'
    assert '2.0000' in text
    assert not has_amount_evidence(text)


def test_a_quantity_carrying_its_unit_is_not_money():
    """``2.00(台)``——两位小数，但紧跟计量单位，同样是数量不是金额。"""
    from backend.services.research_sources import has_amount_evidence
    text = '规格型号 | 单价(元) | 数量（单位）\nH1750R | 2.00(台)\n'
    assert not has_amount_evidence(text)


def test_a_quantity_only_table_never_opens_the_gate():
    from backend.services.research_sources import has_amount_evidence
    text = '规格型号 | 数量（台） | 备 注\nH1750R | 2.0000 | 台\n'
    assert not has_amount_evidence(text)


def test_the_original_same_sentence_judgement_is_unchanged():
    """原判据一个字符没改：同段金额照旧通过，没有金额的段落照旧被拒。"""
    from backend.services.research_sources import has_amount_evidence
    for text in ('成交价 43,300.00 元', '￥43,300.00', '中标价：43300',
                 '采购需求：H1750R 高速冷冻离心机 2 台', 'H1750R 技术参数 容量 10ml'):
        assert has_amount_evidence(text) == bool(OLD_GATE.search(text)), text


# ---------------------------------------------------------------------------
# 逐字段换行的中标公告：金额行不能被窗口切掉
# ---------------------------------------------------------------------------

#: 逐字抄自 CCGP「峨山彝族自治县医疗与健康服务集团 2026 年第四批医疗设备采购
#: 项目（1包、3包、4包）中标公告」的正文行结构：每个字段独占一行，金额在型号行
#: 之后第二行。
FIELD_PER_LINE_NOTICE = (
    '中标公告\n'
    '名称：血型血清学离心机\n'
    '品牌：湘仪\n'
    '规格型号：XKA-2200\n'
    '数量：1\n'
    '单价(元)：17480\n'
    '五、评审专家（单一来源采购人员）名单：\n'
)


def test_a_field_per_line_notice_keeps_the_amount_inside_the_region():
    """region 必须覆盖到金额那一行，否则这条来源会被金额门整体丢掉。

    窗口原来只往型号行下面取一行，于是 ``规格型号`` 的下一行 ``数量：1`` 就是
    终点，``单价(元)：17480`` 落在窗口外。region 里只剩型号没有钱，``prices()``
    的金额门直接 ``continue``——抽取模型一次都没被调用，``price_rejections`` 里
    连一条该型号的记录都没有，报告于是说"还没有取得同型号的成交单价"，
    而正文里就写着这个单价。
    """
    from backend.services.research_sources import price_regions, has_amount_evidence
    regions = price_regions(FIELD_PER_LINE_NOTICE, 'XKA-2200')
    assert len(regions) == 1
    assert '17480' in regions[0]['text']
    assert has_amount_evidence(regions[0]['text'])


def test_a_label_with_a_parenthesised_unit_carries_its_number():
    """``单价(元)：17480``——单位写在列名里、数值在冒号后。

    这种写法原先三条判据全不认：``数字+单位`` 那条要求单位在后，
    ``单价`` 标签那条要求标签直接接数字（中间隔着 ``(元)``），
    列式表那条又要求金额带千分位或两位小数（裸整数 ``17480`` 不算）。
    """
    from backend.services.research_sources import has_amount_evidence
    assert has_amount_evidence('单价(元)：17480')
    assert has_amount_evidence('成交价（万元）：12.5')
    assert has_amount_evidence('中标价：43300')


def test_a_price_heading_without_a_number_is_still_not_evidence():
    """放宽的是"标签 + 数值"，不是"标签本身"：空着的单价列不通过。"""
    from backend.services.research_sources import has_amount_evidence
    assert not has_amount_evidence('规格型号 | 单价(元) | 数量\nXKA-2200 |  | 1\n')


# ---------------------------------------------------------------------------
# (c) 表头是裸的价格列名、数据行是独占单元格的裸整数
# ---------------------------------------------------------------------------

#: 逐字抄自 ``task-f15f699a947b`` 的
#: ``http://www.ccgp.gov.cn/cggg/dfgg/zbgg/202512/t20251209_25889616.htm``
#: （正文第 44-45 行，``price_regions`` 切出的 386 字符窗口；
#: ``设计说明`` §5.3）。表头是裸的「单价」，整表没有货币单位，
#: 数据行的金额是裸整数 ``1239000``。
F15_URL = 'http://www.ccgp.gov.cn/cggg/dfgg/zbgg/202512/t20251209_25889616.htm'
F15_HEADER = '序号 | 名称 | 品牌（如有） | 规格型号 | 数量 | 单价'
F15_ROW = (
    '包别1 | 药理评价系统 | 一、东胜龙 二、海尔 三、弗洛雷斯 四、福立 五、瑞沃德 六、上海力康 七、净信 八、岛津 | '
    '一、药理评价核酸扩增功能：ETC821 二、药理评价-80℃超低温功能：DW-86L578s 三、药理评价定向排风功能：FLLS-01 '
    '四、药理评价高效分离、定性鉴别与定量分析功 能：F80 五、药理评价定位与精准微量输送功能： HOYI-R+R-480 '
    '六、药理评价无菌洁净环境功能： HFsafe-1200LC（A2） 七、药理评价样品颗粒细化、乳化、分散或细胞 破碎功能：GSL-15 '
    '八、药理评价系统液体分离、定性及定量测定功 能：Essentia LC-16 | 1 | 1239000'
)
F15_REGION = '\n'.join([F15_HEADER, F15_ROW, '五、 评审专家 ：',
                        '张兴星（第1包别采购人代表），陈俊东，王友爱，饶红，程向阳'])
#: 真实公告正文里窗口以外的部分（标题与栏目名），``validate_price`` 要用它
#: 确认这是一份成交公告。逐字抄自同一份正文。
F15_BODY = '中标（成交）结果公告\n四、主要标的信息\n' + F15_REGION

#: 旧判据的全文（第 1 类 + 第 2 类），来自改动前的 ``has_amount_evidence``。
#: 留一份在这里，"现在能过"才有一个"当时确实不能过"的对照。
def _old_gate(text):
    from backend.services.research_sources import (
        _AMOUNT_WITH_UNIT, _BUDGET_LABEL, _MONEY_SHAPED, _PRICE_COLUMN_UNIT,
        _PRICE_COLUMN_WORD, _PRICE_LABEL_WITH_NUMBER)
    if _AMOUNT_WITH_UNIT.search(text) or _PRICE_LABEL_WITH_NUMBER.search(text):
        return True
    if not _MONEY_SHAPED.search(text):
        return False
    for line in text.splitlines():
        declared = _BUDGET_LABEL.sub(' ', line)
        if _PRICE_COLUMN_WORD.search(declared) and _PRICE_COLUMN_UNIT.search(line):
            return True
    return False


def test_the_bare_header_region_was_refused_by_every_old_predicate():
    """复现：旧判据对这一类**整类**不命中，逐条给出原因。"""
    from backend.services.research_sources import (
        _AMOUNT_WITH_UNIT, _MONEY_SHAPED, _PRICE_COLUMN_UNIT, _PRICE_COLUMN_WORD,
        _PRICE_LABEL_WITH_NUMBER)
    text = F15_REGION
    assert 'DW-86L578' in text and '1239000' in text and '元' not in text
    # ① 金额和单位同段：整表没有单位
    assert _AMOUNT_WITH_UNIT.search(text) is None
    # ② 价格标签紧接数字：表头的「单价」后面是行尾，数据行没有价格标签
    assert _PRICE_LABEL_WITH_NUMBER.search(text) is None
    # ② 的列式表变体：表头有价格列词根，但没有货币单位
    assert _PRICE_COLUMN_WORD.search(F15_HEADER) and not _PRICE_COLUMN_UNIT.search(F15_HEADER)
    # 金额形态：裸整数既没有千分位也没有两位小数
    assert _MONEY_SHAPED.search(text) is None
    assert _old_gate(text) is False


def test_the_bare_header_region_now_has_amount_evidence():
    from backend.services.research_sources import has_amount_evidence
    assert has_amount_evidence(F15_REGION) is True


def test_a_headed_column_with_a_bare_integer_also_opens():
    """表头写了单位、金额却是裸整数的表也要进（实测的另一个新打开来源）。

    逐字抄自 ``http://cgzx.hust.edu.cn/jgfscg/27085.chtml``
    （``task-4775bbb17607``，华中科技大学采购与招标中心）：表头 ``单价(元)``，
    数据行是裸整数 ``90000``；``_MONEY_SHAPED`` 不认裸整数，旧判据整条不命中。
    """
    from backend.services.research_sources import has_amount_evidence
    text = ('序号 | （货物）名称 | 型号 | 制造商和产地 | 数量 | 单价(元) | 小计(元)\n'
            '1 | 紫外可见分光光度计 | Cary 60 | 安捷伦/马来西亚 | 1 | 90000 | 90000\n')
    assert _old_gate(text) is False
    assert has_amount_evidence(text) is True


@pytest.mark.parametrize('text', [
    # 页码：没有价格列词根，数字也不是独占单元格的四位数
    '第 12 页 | 共 30 页\n高速冷冻离心机 H1750R\n',
    # 电话：11 位数字，且前面不是单元格边界
    '采购人联系电话：010-12345678\n高速冷冻离心机 H1750R\n',
    # 日期：四位数后面紧跟日期分隔符
    '公告日期 | 2025-12-09\n高速冷冻离心机 H1750R\n',
    # 项目编号：数字串在单元格中间
    '项目编号：JH25-210000-33918\n高速冷冻离心机 H1750R\n',
    # 金额单位缺失**且**没有表头：裸整数不成证据
    'H1750R | 1 | 1239000\n',
    # 表头是「单价」，但唯一的四位数是年份
    '序号 | 规格型号 | 单价 | 日期\n1 | H1750R | 2025-12-09\n',
    # 表头是「单价」，但数字只有三位（序号/包号量级）。
    # 注意 ``单价`` 后面直接跟数字的写法由**既有**的第 1 类判据放行（``\s*`` 能跨行），
    # 这里钉的是它不紧邻数字、也不构成独占单元格的情形。
    '序号 | 规格型号 | 数量 | 单价\n张兴星 | H1750R | 12 | 999\n',
    # 上限声明被挖掉之后表头里没有价格列词根
    '序号 | 规格型号 | 预算单价 | 数量\n1 | H1750R | 1500000 | 2\n',
])
def test_unrelated_numbers_still_carry_no_amount_evidence(text):
    """这条放宽不许变成"任何数字都算金额"。"""
    from backend.services.research_sources import has_amount_evidence
    assert has_amount_evidence(text) is False


def test_the_bare_header_source_reaches_the_extractor(research):
    """门开了：这个来源现在会被问一次（改前 0 次）。"""
    service, run, _, _ = research
    service.verify_publishers = lambda *args, **kwargs: None
    calls = _counting_client(service)
    before = run['usage']['model']
    run['sources'] = [_source(F15_URL, F15_REGION)]

    service.prices(run, {'brand': '海尔', 'model': 'DW-86L578S'})

    assert len(calls) == 1
    assert run['usage']['model'] == before + 1


def test_a_bare_integer_without_a_header_never_reaches_the_extractor(research):
    service, run, _, _ = research
    service.verify_publishers = lambda *args, **kwargs: None
    calls = _counting_client(service)
    before = run['usage']['model']
    run['sources'] = [_source('https://www.ccgp.gov.cn/bare/demo.htm',
                              '序号 | 规格型号 | 数量\n1 | H1750R | 1239000\n')]

    service.prices(run, {'brand': BRAND, 'model': MODEL})

    assert calls == []
    assert run['usage']['model'] == before


#: 模型对上面那份 f15 正文给出的**真实**答案（改前 run ``research-54a31d59`` 里
#: 落库的那条 1,239,000 元记录；逐字段取自库，不是编的）。
F15_MODEL_PRICE = {
    'brand': '', 'model': 'DW-86L578S', 'amount': 1239000.0, 'amount_unit': '元',
    'amount_scope': 'unit', 'quantity': 1.0, 'currency': 'CNY', 'kind': 'award',
    'date': '2025-12-09', 'excerpt': F15_ROW,
}


def test_admitting_the_region_is_not_adopting_the_price():
    """放宽的是"要不要问一次"，不是"要不要采信"：1239000 仍被 ``validate_price`` 拒。

    模型给出的引文（数据行）里没有货币单位，``amount_unit='元'`` 因此绑不上；
    拒因是 ``unparsed_amount``。这条正是"整包价（8 种设备一个包别 123.9 万）
    没有被当成其中一台 -80℃ 冰箱的单价"的那道闸门。
    """
    from backend.services.procurement_rules import UNPARSEABLE_AMOUNT, validate_price_verdict
    from backend.services.research_sources import has_amount_evidence

    assert has_amount_evidence(F15_REGION) is True
    source = _source(F15_URL, F15_BODY)
    quote, refusal = validate_price_verdict(
        dict(F15_MODEL_PRICE), source, {'brand': '海尔', 'model': 'DW-86L578S'})
    assert quote is None
    assert refusal.get('reason_code') == UNPARSEABLE_AMOUNT, refusal


def test_the_gate_opening_does_not_reopen_the_package_price_hole(research):
    """反例自查：放宽金额门不许把这个洞重新打开。

    表格通道有 ``lists_sibling_models``（``7ca87eb``：整包行的总价不当单价）挡着，
    这条正文现在仍解析不出表格行；抽取通道即使拿到模型给的 1239000，
    也进不了 ``run['prices']``。两道都钉住。
    """
    from backend.services.procurement_tables import price_table_rows
    service, run, _, _ = research
    service.verify_publishers = lambda *args, **kwargs: None
    assert price_table_rows(F15_REGION, 'DW-86L578S') == []

    def client(service_):
        class Client:
            endpoint = 'test://package-price-hole'
            model = 'fixed'
            last_usage = None

            def analyze(self, **kwargs):
                return {'prices': [dict(F15_MODEL_PRICE)]}
        service_.client_factory = Client

    client(service)
    run['sources'] = [_source(F15_URL, F15_BODY)]
    service.prices(run, {'brand': '海尔', 'model': 'DW-86L578S'})

    assert run['prices'] == []
