"""配置对比这条线上"两条路必须给出同一个结论"的固化用例。

背景（真跑证据 ``08-run-off.json``，两轮同输入、各 3 个候选）：正式候选的
配置对比矩阵 ``matrix`` 与候选卡片的 ``config_match`` 是**两条不同的路**——
``matrix`` 走"官方/未核身份来源 → 模型逐项抽取 → ``grounded_parameter`` 闸门 →
``evaluate_parameter``"，``config_match`` 走"候选自己那一行的正文 → 逐项正则读"。
同一个候选 ``GL-10000C`` 上，正文原文是同一行

    产品摘要： 安亭GL-10000C：最高转速：10000rpm (转/分)；最大相对离心力：15860(×g)；…

两条路却给出了两个结论：

  * ``最高转速``：矩阵行 ``observed`` 拿到了 ``10000``，但单位写作 ``rpm (转/分)``，
    查不到 ``UNITS`` 里的键 → ``status='unknown'``；卡片那条路归一成 ``r/min``
    → ``violated``。**同源同文，两个结论。**
  * ``最大相对离心力``：矩阵行的型号锚定闸门把上一个参数的值 ``10000rpm`` 当成
    "另一个型号"，整行后半段的参数全被拒 → ``observed=None``；卡片那条路读到了
    ``15860`` 并判 ``satisfied``。**同源同文，一个有一个没有。**

这里把这两件事钉住，同时钉住反向的那一面：来源里**确实没有**这个参数时
（``G21000`` 那一行只有型号和价格），两条路都必须是诚实的 ``unknown``，
不许因为要"消除未知"就编一个值出来。
"""
from backend.services.procurement_rules import (
    canonical_unit, evaluate_parameter, grounded_parameter, parameter_row,
)
from backend.services.research_planning import configuration_alignment

# ---------------------------------------------------------------------------
# 夹具：全部是证据文件里落盘的**原样正文**，不是手写编出来的。
# ---------------------------------------------------------------------------

#: 该型号自己那一页的整机参数行（候选 url 的正文，某次真跑落盘）。
GL_LINE = ('产品摘要： 安亭GL-10000C：最高转速：10000rpm (转/分)；最大相对离心力：15860(×g)；'
           '最大容量1200ml×6 (7000rpm)；温度范围：20℃～30℃；温度精度：±2℃；'
           '定时范围 ：0min～99min')
GL_URL = 'https://antinglxj.com/cp/example/gl-10000c.html'
GL_DETAIL_URL = 'http://www.antinglxj.com/'

#: 那一页读到的**详情页**正文：里面连型号都没有（``contains_model`` 为假）。
GL_DETAIL_LINES = '\n'.join([
    '上海安亭科学仪器厂_离心机价格优！',
    '上海安亭科学仪器厂官网 [http://www.antinglxj.com/]',
    '服务热线： 021-58201756 ***',
    '首页 [http://www.antinglxj.com/] | 产品中心 [http://www.antinglxj.com/cp/]',
    '台式低速离心机 [http://www.antinglxj.com/cp/]',
])

#: ``G21000`` 那一页的**全部**内容：目录行只有型号、台数和价格，一个参数都没有。
G21000_LINE = '台式常温高速离心机 | 湘智 | G21000 | 2台 | 12,400.00元'
G21000_URL = 'https://www.yiliaozhaobiao.com/news-00000000000000000000000000000001'

#: ``H18.5B`` 在聚合页上的那一行：型号与三项参数写在同一行里（原样）。
H185B_LINE = ('1 | 离心机 | 台式高速离心机 | 1 | 台 | 14,500 | H18.5B | 1. 5寸高清真彩触摸屏控制，'
              '操作简便，显示直观，设置参数和实时运行参数可同屏显示。 2. 具有转子自动识别功能，'
              '防止用户设置错误的转子号参数。 3. 铝合金转子，持久耐用且可高温高压灭菌。 '
              '4. 配备吸附式门锁，轻松关闭门盖，安全锁定离心。 5. 设有门盖保护、超速保护等多种保护功能，'
              '确保仪器安全运行。 6. 最高转速18500r/min 7. 最大相对离心力23797×g 8. 最大容量100ml×4 '
              '9. 转速精度±10r/min 10. 定时范围1s-99h59min59s 11. 整机噪音＜65dB(A) '
              '12. 电源AC220V 50Hz 10A 13. 整机功率700W 14. 外形尺寸（L×W×H）340mm×460mm×330mm '
              '15. 净重32kg 16. 配置：50mlx6角转子 最高转速：11000rpm 最大离心力：13000xg '
              '100mlx4角转子 最高转速：12000rpm 最大离心力：13000xg | 否 | 卢湘仪 | ** | **卢湘****点击查看公司')
H185B_AGGREGATOR_URL = 'https://www.yiliaozhaobiao.com/news-00000000000000000000000000000002'
H185B_MANUFACTURER_URL = 'http://www.lxylxj.com/lixinji/product.html'
H185B_MANUFACTURER_BODY = ('型号 | H18.5B\n订货号 | 2021015310\n最高转速 | 18500r/min\n'
                           '最大相对离心力 | 23797×g\n最大容量 | 100ml×4')

#: 用户确认的三项要求（``08-run-off.json`` 的 ``baseline.parameters`` 原样）。
REQUIREMENTS = [
    {'name': '最高转速', 'value': 12000, 'unit': 'r/min', 'direction': 'min', 'conditions': ''},
    {'name': '最大相对离心力', 'value': 13500, 'unit': 'xg', 'direction': 'min', 'conditions': ''},
    {'name': '最大容量', 'value': 24, 'unit': '根毛细管', 'direction': 'min', 'conditions': ''},
]


def _source(url, body, *, source_type='unknown', purpose='discovery'):
    return {'url': url, 'title': '夹具', 'source_type': source_type, 'authority_score': 30,
            'authority_reason': '夹具', 'snippet': '', 'body_text': body,
            'body_status': 'fetched', 'crawled_at': None, 'provider': 'fixture',
            'purpose': purpose}


def _observations(source, model, specs):
    """按"模型逐项抽取"的输出形状造 ``detail['parameters']``。"""
    return [{'name': name, 'value': value, 'unit': unit, 'conditions': conditions,
             'url': source['url'], 'excerpt': source['body_text']}
            for name, value, unit, conditions in specs]


def _parameter_sources(candidate, sources):
    """复刻 ``verify_and_price_candidate`` 挑参数来源的那一段（口径逐字照抄）。

    只做**判断**用，不参与生产：官方来源（厂商官网／厂商下载）与未核身份的来源
    可以给参数，聚合页（``aggregator``）这类不进这一集合。
    """
    from backend.services.procurement_rules import contains_model
    official = [s for s in sources if s.get('body_status') == 'fetched'
                and s.get('source_type') in {'manufacturer_website', 'manufacturer_download'}
                and contains_model(s.get('body_text'), candidate['model'])]
    provisional = [s for s in sources if s.get('body_status') == 'fetched'
                   and s.get('source_type') == 'unknown' and s.get('purpose') != 'tender'
                   and contains_model(s.get('body_text'), candidate['model'])]
    known = {s['url'] for s in official}
    return official + [s for s in provisional if s['url'] not in known]


def _matrix_status(candidate, source, observations):
    rows = [parameter_row(req, observations, [source], candidate['model'],
                          allow_unverified=True) for req in REQUIREMENTS]
    return rows, [row['status'] for row in rows]


def _card_status(candidate, source, detail_sources=None):
    match = configuration_alignment(candidate, [source], REQUIREMENTS,
                                    detail_sources=detail_sources)
    return match, [item['status'] for item in match['items']]


# ---------------------------------------------------------------------------
# 一、来源里**有**这个参数：必须落到 observed，两条路结论一致。
# ---------------------------------------------------------------------------

def test_a_source_that_has_the_parameter_lands_in_observed_on_both_paths():
    """``GL-10000C``：同一行正文里的三个参数，矩阵与卡片必须给出同一组结论。

    修前实测：矩阵 ``['unknown', 'unknown', 'unknown']``（且后两项 ``observed`` 是
    ``None``）、卡片 ``['violated', 'satisfied', 'unknown']``。
    """
    source = _source(GL_URL, GL_LINE)
    candidate = {'model': 'GL-10000C', 'url': GL_URL}
    observations = _observations(source, 'GL-10000C', [
        ('最高转速', '10000', 'rpm (转/分)', ''),
        ('最大相对离心力', '15860', '×g', ''),
    ])
    rows, matrix = _matrix_status(candidate, source, observations)
    match, card = _card_status(candidate, source)

    assert matrix == ['violated', 'satisfied', 'unknown']
    assert card == ['violated', 'satisfied', 'unknown']
    assert matrix == card
    # 读到值的那两项必须真的落在 ``observed`` 上，不能只活在 raw_evidence 里。
    assert rows[0]['observed'] is not None and rows[0]['observed']['value'] == '10000'
    assert rows[1]['observed'] is not None and rows[1]['observed']['value'] == '15860'
    # 引用必须与来源对得上（同一条正文、同一个网址）。
    assert rows[0]['observed']['url'] == GL_URL
    assert rows[1]['observed']['url'] == GL_URL


def test_a_value_in_front_of_the_next_label_is_not_read_as_another_model():
    """型号锚定闸门不许把"上一个参数的值"当成另一个型号。

    厂商页把整机参数写一行是常态：``…最高转速：10000rpm (转/分)；最大相对离心力：…``
    第二个标签前面的 token 是 ``10000rpm``。修前它被当成"另一个型号"，于是整行
    后半段的参数全被拒（``observed=None``），而同一条正文另一条路读得到值。
    """
    source = _source(GL_URL, GL_LINE, source_type='manufacturer_website')
    assert grounded_parameter({'value': 15860, 'unit': '×g', 'excerpt': GL_LINE},
                              source, 'GL-10000C', '最大相对离心力')
    assert grounded_parameter({'value': 10000, 'unit': 'rpm (转/分)', 'excerpt': GL_LINE},
                              source, 'GL-10000C', '最高转速')
    # 数量级不同的单位写法同样算"值"：``160ml``／``18500r``。
    body = 'AX10 容量160 ml；转速10000rpm'
    assert grounded_parameter({'value': 10000, 'unit': 'rpm', 'excerpt': body},
                              _source('https://vendor.example/a', body,
                                      source_type='manufacturer_website'), 'AX10', '转速')


def test_neighbour_models_are_still_rejected_after_that_relaxation():
    """放宽的是"值不是型号"，不是"型号必须一致"这条判据。"""
    cases = [('AX10', 'AX20'), ('3H24RI', '3H20RI'), ('Q10', 'Q10S')]
    for target, neighbour in cases:
        body = f'{target} 产品介绍\n{neighbour} 容量160 ml'
        source = _source('https://vendor.example/a', body, source_type='manufacturer_website')
        assert not grounded_parameter({'value': 160, 'unit': 'ml', 'excerpt': body},
                                      source, target, '容量'), (target, neighbour)
    # 同一行里紧跟着另一个型号时，也照旧不算本型号的参数。
    body = 'AX10 容量160 ml AX20 容量200 ml'
    assert not grounded_parameter({'value': 200, 'unit': 'ml', 'excerpt': body},
                                  _source('https://vendor.example/a', body,
                                          source_type='manufacturer_website'), 'AX10', '容量')


# ---------------------------------------------------------------------------
# 二、来源里**没有**这个参数：两条路都必须诚实 unknown。
# ---------------------------------------------------------------------------

def test_a_source_without_the_parameters_is_honestly_unknown_on_both_paths():
    """``G21000``：正文里只有型号、台数和价格，一个参数都没有。

    这里没有可修的东西——把"没查到"写成有值就是撒谎。两条路读的**来源集合本来就
    不同**（矩阵只吃官方/未核身份的来源，聚合页 ``source_type='aggregator'`` 进不去；
    卡片那条路读的就是候选自己那一行的页面），这一页上两条路都只能是 ``unknown``。
    """
    aggregator = _source(G21000_URL, G21000_LINE, source_type='aggregator')
    candidate = {'model': 'G21000', 'url': G21000_URL}
    # 矩阵那一条路拿到的来源集合：聚合页不进参数来源（与 ``verify_and_price_candidate``
    # 同一套过滤），所以这一轮一个参数来源都没有。
    assert _parameter_sources(candidate, [aggregator]) == []
    rows, matrix = _matrix_status(candidate, aggregator, [])
    match, card = _card_status(candidate, aggregator)

    assert matrix == ['unknown', 'unknown', 'unknown']
    assert card == ['unknown', 'unknown', 'unknown']
    assert all(row['observed'] is None for row in rows)
    assert all(item['observed'] is None for item in match['items'])


def test_a_different_quantity_in_the_source_stays_unknown_on_both_paths():
    """``H18.5B`` 那一项：来源里写的是 ``最大容量 100ml×4``，要求是"24 根毛细管"。

    两个量纲不一样，换算不了 —— 这正是"来源里有'容量'但这一个量确实没有"的情形，
    两条路都必须是 ``unknown``，不许拿体积冒充毛细管根数。前两项两条路都必须读到
    同一个值（矩阵读厂商页，卡片读聚合页那一行——来源不同，结论必须相同）。
    """
    manufacturer = _source(H185B_MANUFACTURER_URL, H185B_MANUFACTURER_BODY, purpose='official')
    aggregator = _source(H185B_AGGREGATOR_URL, H185B_LINE, source_type='aggregator')
    candidate = {'model': 'H18.5B', 'url': H185B_AGGREGATOR_URL}
    observations = _observations(manufacturer, 'H18.5B', [
        ('最高转速', '18500', 'r/min', ''),
        ('最大相对离心力', '23797', '×g', ''),
        ('最大容量', '100ml×4', '', ''),
    ])
    rows, matrix = _matrix_status(candidate, manufacturer, observations)
    match, card = _card_status(candidate, aggregator)

    assert matrix == ['satisfied', 'satisfied', 'unknown']
    assert card == ['satisfied', 'satisfied', 'unknown']
    assert matrix == card
    assert rows[2]['observed'] is not None   # 读到的值留在 observed 上（不是"没读到"）
    assert rows[2]['status'] == 'unknown'    # 但量纲不同，判未知
    assert match['items'][2]['observed'] is not None
    assert evaluate_parameter(REQUIREMENTS[2], {'value': 100, 'unit': 'ml',
                                                'evidence': {'url': H185B_MANUFACTURER_URL,
                                                             'excerpt': H185B_MANUFACTURER_BODY}}) == 'unknown'


# ---------------------------------------------------------------------------
# 三、单位写法只有一份归一化实现：``rpm (转/分)`` 不许在一条路上算数、另一条路上不算。
# ---------------------------------------------------------------------------

def test_unit_spellings_are_normalized_by_one_shared_implementation():
    assert canonical_unit('rpm (转/分)') == 'r/min'
    assert canonical_unit('× g') == '×g'
    assert canonical_unit('xg') == '×g'
    assert canonical_unit('rcf') == '×g'
    # 认不出来的量纲原样返回，不猜。
    assert canonical_unit('根毛细管') == '根毛细管'
    # 卡片那条路读的就是同一个实现（真跑里它是自己一份，才会走出两个结论）。
    from backend.services.research_planning import canonical_unit as card_unit
    assert card_unit('rpm (转/分)') == canonical_unit('rpm (转/分)') == 'r/min'

    evidence = {'url': GL_URL, 'excerpt': GL_LINE}
    assert evaluate_parameter(REQUIREMENTS[0], {'value': 10000, 'unit': 'rpm (转/分)',
                                                'evidence': evidence}) == 'violated'
    assert evaluate_parameter(REQUIREMENTS[0], {'value': 12000, 'unit': '转/分',
                                                'evidence': evidence}) == 'satisfied'
    # 量纲对不上就是未知，归一化不会把它变成"可以比较"。
    assert evaluate_parameter(REQUIREMENTS[2], {'value': 1200, 'unit': 'ml',
                                                'evidence': evidence}) == 'unknown'


# ---------------------------------------------------------------------------
# 四、引用的网址必须是那一行自己的来源。
# ---------------------------------------------------------------------------

def test_fallback_line_keeps_its_own_source_url():
    """详情页里读不到时回落到目录页那一行，``evidence.url`` 必须是目录页。

    修前实测：``某型号`` 的卡片把目录页的原文引在详情页 ``http://www.antinglxj.com/``
    名下，而那一页正文里连型号都没有 —— 用户按这个网址去核对会核不到。
    """
    source = _source(GL_URL, GL_LINE)
    detail = _source(GL_DETAIL_URL, GL_DETAIL_LINES,
                     source_type='product_detail', purpose='alternatives_detail')
    candidate = {'model': 'GL-10000C', 'url': GL_URL, 'detail_page': {'url': GL_DETAIL_URL,
                                                                      'status': 'fetched'}}
    match, card = _card_status(candidate, source, detail_sources=[source, detail])

    assert card == ['violated', 'satisfied', 'unknown']
    for item in match['items']:
        if item['observed']:
            assert item['observed']['evidence']['url'] == GL_URL
            assert '安亭GL-10000C' in item['observed']['evidence']['excerpt']
