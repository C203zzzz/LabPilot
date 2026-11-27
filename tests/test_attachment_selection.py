# -*- coding: utf-8 -*-
"""附件选择：按文件名分级，额度先花在带着关键信息的那几个上。

发布方一次挂十个附件是常态，而每个附件都是一次正文读取。实测那条 CCGP 中标
公告挂了 9 个附件，7 个是中小企业／符合本国产品标准的声明函，只有附件9
《招标文件》有配置——按正文顺序读等于把 8 次读取花在格式文本上。

分级只决定先读谁、值不值得读，不决定内容取舍：一个附件最后有没有用，仍然
由它自己的字节说了算。
"""
from backend.services.research_sources import (ATTACHMENT_FORM, ATTACHMENT_KEY,
                                               ATTACHMENT_PLAIN, PUBLICATION_ATTACHMENT_LIMIT,
                                               attachment_leads, attachment_rank, attachments)

NOTICE = 'http://www.ccgp.gov.cn/cggg/dfgg/zbgg/202603/t20260325_26313581.htm'
CCGP_ATTACHMENTS = [
    ('中小企业声明函（1包）.pdf', 'uuid=98759870-de5d-4c72-a3af-7afc4d'),
    ('关于符合本国产品标准的声明函.pdf', 'uuid=fdd94c55-8fb8-4d07-b280-2e278f'),
    ('中小企业声明函.pdf', 'uuid=7d1d6143-bb67-44ac-b8c1-c83a3e'),
    ('中小企业声明函（3包）.pdf', 'uuid=011527fc-5529-45da-82c3-347b49'),
    ('关于符合本国产品标准的声明函.pdf', 'uuid=2a755162-2a05-46d5-89e9-70fe36'),
    ('关于符合本国产品标准的声明函.pdf', 'uuid=56d0a4e2-6eb0-41be-a1cb-4a854e'),
    ('中标结果公告.pdf', 'uuid=7502a19e-7de9-47a9-98ad-6a39cf'),
    ('中小企业声明函.pdf', 'uuid=9f90e6f1-28fe-4319-8ee8-07d3a0'),
    ('招标文件（第四批）-定稿.docx', 'uuid=b695c131-8d60-4109-9735-a09d7f'),
]


def _url(identifier):
    return 'http://download.ccgp.gov.cn/oss/download?' + identifier


def _body(entries):
    """渲染后的正文形状：本机实测就是 ``附件N | 文件名 [url] | 文件名 [url]``。"""
    return '\n'.join('附件%d | %s [%s] | %s [%s]' % (number, name, _url(key), name, _url(key))
                     for number, (name, key) in enumerate(entries, 1))


def _notice(entries):
    return {'url': NOTICE, 'source_type': 'tender_document', 'body_status': 'fetched',
            'body_text': _body(entries)}


def test_the_file_name_decides_whether_an_attachment_is_worth_reading():
    assert attachment_rank('附件9 | 招标文件（第四批）-定稿.docx') == ATTACHMENT_KEY
    assert attachment_rank('报价明细附件.zip') == ATTACHMENT_KEY
    assert attachment_rank('中标结果公告.pdf') == ATTACHMENT_PLAIN
    assert attachment_rank('产品技术参数表.pdf') == ATTACHMENT_KEY
    assert attachment_rank('中小企业声明函.pdf') == ATTACHMENT_FORM
    assert attachment_rank('售后服务承诺函.pdf') == ATTACHMENT_FORM


def test_declaration_forms_are_not_read():
    source = _notice([('中小企业声明函.pdf', 'uuid=1'), ('关于符合本国产品标准的声明函.pdf', 'uuid=2')])
    assert attachments(source) == []
    assert [lead['rank'] for lead in attachment_leads(source)] == [ATTACHMENT_FORM] * 2


def test_the_tender_document_is_read_before_a_plain_attachment():
    """招标文件在正文里排在后面，仍要先读。"""
    tender, notice = _url('uuid=b695c131-8d60-4109-9735-a09d7f'), _url('uuid=7502a19e-7de9-47a9-98ad-6a39cf')
    source = _notice([('中标结果公告.pdf', 'uuid=7502a19e-7de9-47a9-98ad-6a39cf'),
                      ('招标文件（第四批）-定稿.docx', 'uuid=b695c131-8d60-4109-9735-a09d7f')])
    assert attachments(source) == [tender, notice]


def test_one_real_ccgp_notice_goes_from_nine_reads_to_two():
    source = _notice(CCGP_ATTACHMENTS)
    assert len(attachment_leads(source)) == 9
    assert attachments(source) == [_url('uuid=b695c131-8d60-4109-9735-a09d7f'),
                                   _url('uuid=7502a19e-7de9-47a9-98ad-6a39cf')]


def test_ranking_happens_before_the_attachment_cap():
    """先到先得的截断会把招标文件切掉；分级排序保证它留在上限之内。"""
    forms = [('中小企业声明函%d.pdf' % number, 'uuid=f%d' % number) for number in range(12)]
    source = _notice(forms + [('招标文件（第四批）-定稿.docx', 'uuid=tender')])
    assert len(forms) + 1 > PUBLICATION_ATTACHMENT_LIMIT
    assert attachments(source) == [_url('uuid=tender')]


def test_a_brand_manual_stays_readable_on_an_untrusted_page():
    source = {'url': 'https://vendor.example/product', 'source_type': 'manufacturer_website',
              'body_status': 'fetched',
              'body_text': '乙厂 Q20 [产品说明书.pdf](https://vendor.example/q20.pdf)'}
    assert attachments(source, model='Q20') == ['https://vendor.example/q20.pdf']


#: 实测的 label，逐条抄自 ``output/pipe-supervisor-20260923/site-probe/attachment-rank-reality2.txt``
#: （拿真实公告正文跑 ``attachment_leads()`` 的输出，label 末尾那个空格也是原文）。
def test_the_real_haikou_labels_put_the_noise_out_and_the_dossier_first():
    """海口那条公告三个附件原先全是 PLAIN，谁排在前面纯靠顺序。

    现在两个噪声降到不读，唯一含配置的那份（55722 字，正文「2.1 主要配置清单」在
    第 28457 字处）升到优先——它的名字里没有「招标文件」，只有项目名加「-定稿」。
    """
    assert attachment_rank('新版操作说明 ') == ATTACHMENT_FORM
    assert attachment_rank('综合评分表.pdf ') == ATTACHMENT_FORM
    assert attachment_rank('《检验科全自动化学发光测定仪等一批医疗小设备项目》-定稿.pdf ') == ATTACHMENT_KEY


def test_the_other_real_labels_that_were_measured():
    assert attachment_rank(
        '附件【20210706稿招标文件--绍兴文理学院台式离心机、PCR仪等2个标段采购项目.docx ') == ATTACHMENT_KEY
    assert attachment_rank('附件【标项2-开标一览表.pdf ') == ATTACHMENT_FORM
    assert attachment_rank('中医药传承创新示范试点第二批科研设备项目报价明细附件.pdf ') == ATTACHMENT_KEY
    assert attachment_rank('附件【附件--响应价格明细表.pdf ') == ATTACHMENT_KEY
    # 「主要中标标的承诺函.pdf」在真实 run 里被旧词表判成 FORM 而没抓
    # （output/template-regression/run-hot/hot-run.json 的 attachment_skipped）。
    # 合肥大学那份同类文书正文里带着真单价 1239000.00 元，所以成交类的要读，
    # 投标人的格式承诺函仍然不读。
    assert attachment_rank('主要中标标的承诺函.pdf ') == ATTACHMENT_KEY
    assert attachment_rank('中小企业声明函.pdf') == ATTACHMENT_FORM


def test_the_real_haikou_notice_reads_only_the_dossier():
    """端到端：那条公告的三个附件里，只有定稿那份该被读。"""
    dossier = _url('uuid=dossier')
    source = _notice([('新版操作说明 ', 'uuid=manual'),
                      ('综合评分表.pdf ', 'uuid=score'),
                      ('《检验科全自动化学发光测定仪等一批医疗小设备项目》-定稿.pdf ', 'uuid=dossier')])
    assert attachments(source) == [dossier]
    assert [lead['rank'] for lead in attachment_leads(source)] == [
        ATTACHMENT_KEY, ATTACHMENT_FORM, ATTACHMENT_FORM]


def test_an_archive_is_not_downgraded_by_its_container_name():
    """``开标记录表.zip`` 里装的是分项报价明细表——容器名说明不了容器里的东西。

    黑龙江那条真实公告（中国疾控中心地方病控制中心，2025-11-21）只挂两个附件：
    ``开标记录表.zip`` 与 ``地病中心2025新楼设备报价明细附件.pdf``，而分项报价只在
    zip 里（解开是 ``报价明细/明细表.xlsx``，见
    ``tests/test_attachment_archive_landing.py``）。按名字降级就丢了唯一的价格表。
    同一个名字不带归档后缀时仍然降级——PDF 版的开标记录表就是那张表。
    """
    assert attachment_rank('开标记录表.zip') == ATTACHMENT_PLAIN
    assert attachment_rank(
        '开标记录表.zip',
        'https://hljcg.hlj.gov.cn/gpx-bid-file/zip/a.zip?accessCode=1') == ATTACHMENT_PLAIN
    # label 不写扩展名、只有地址是归档时同样不降级
    assert attachment_rank('开标记录表', 'https://files.example/a.zip') == ATTACHMENT_PLAIN
    assert attachment_rank('开标记录表') == ATTACHMENT_FORM
    assert attachment_rank('开标记录表.pdf') == ATTACHMENT_FORM
    # 关键判据先于归档例外：带关键词的名字照样优先读
    assert attachment_rank('采购文件.zip') == ATTACHMENT_KEY


def test_a_name_that_says_nothing_is_graded_plain_not_dropped():
    """名字什么也没说明的附件留在 PLAIN，不许因为「没命中关键词」就降级。

    名字带「示范文本」的安徽招标文件（47590 字，含配置清单／采购需求）就是这一类：
    按名字它什么都不是，靠正文才认出来（见 ``tests/test_attachment_document_type.py``）。
    """
    assert attachment_rank('4baf8799-f6bb-464c-af58-8fc395965013.pdf') == ATTACHMENT_PLAIN
    assert attachment_rank('') == ATTACHMENT_PLAIN
    assert attachment_rank(None) == ATTACHMENT_PLAIN


def test_the_labels_that_cannot_be_graded_before_the_fetch():
    """已知盲区，钉在这里免得被当成回归：这些名字抓取前判不出来。

    ``附件 1`` 是政采云生成的开标记录表／资格审查记录表（地址里也只有 UUID），
    ``xqyesmh.pdf`` 是「中小企业声明函」的拼音首字母。前者的正文判据见
    ``tests/test_attachment_document_type.py``；后者在 602 条真实公告正文的实测里
    一条都没出现（label 为空的有 8 条，全是厂家英文资料的父页）。
    """
    for label in ('附件 1', '附件1', 'dbedcecc-b3b6-4d82-aff0-f28d6cb0ab36.pdf',
                  'xqyesmh.pdf', '《检验科全自动化学发光测定仪等一批医疗小设备项目》.pdf'):
        assert attachment_rank(label) == ATTACHMENT_PLAIN, label
