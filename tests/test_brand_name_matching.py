"""品牌写法：企业登记名与厂商中文官方名都归到品牌名下，但不同实体不许合并。

Q1 全量归类（``设计说明`` §1.2/§1.4）里，
101 条被拒记录中**唯一一条确认判错**的就是品牌：中标公告的
「参考品牌及型号」列写的是登记名 ``上海知楚仪器设备1300A2``，
抽取层把品牌读成 ``上海知楚仪器设备``，比较基准写的是 ``知楚``，
``brand_identity`` 判成两个品牌，``task-20358119eae4`` 那条同型号成交价整条丢掉。

改后效果评测又实测出方向相反的一类（``设计说明`` §5.2/§5.4）：
目标品牌 ``Leica``，公告写的是中文官方名 ``徕卡``，``task-a283e5adf675``
两条真实单台成交（47000、55000）同样整条丢掉。两者**走同一张别名表**
（``brand_model_rules._BRAND_ALIASES``）与同一处结构判据，
不为某一条品牌另开分支。

同一批数据里还有**必须保持不匹配**的反例：``湘仪`` 与 ``湘潭湘仪`` 是同字号
不同主体（湘潭做天平、湖南湘仪做离心机），``洁康`` 与 ``深圳捷康`` 只是读音近。

2026-10-02 补：``task-013dcdcff7e6`` 那 2 条 ``brand_mismatch`` 的写法
（``贝克曼``↔``贝克曼库尔特``、``Beckman Coulter``↔``贝克曼库尔特``）经核实
是**同一厂家的三种写法**，已作为别名条目录入 ``_BRAND_ALIASES``；同批还补了
``山东新华``/``新华医疗``→``新华``。这是**加登记**，不是放宽判据——
``brand_name_matches`` 的三步判据一个字没动。证据与准入标准写在
``brand_model_rules.py`` 的 ``_BRAND_ALIASES`` 注释里，
实测与全量回放见 ``output/brand-alias-fix/report.md``。
"""
import pytest

from backend.services.procurement_rules import brand_name_matches


@pytest.mark.parametrize('left,right', [
    ('知楚', '上海知楚仪器设备'),          # 实测：hubu 公告的「参考品牌及型号」列
    ('上海知楚仪器设备', '知楚'),
    ('知楚', '上海知楚仪器设备有限公司'),
    ('湘仪', '湖南湘仪实验室仪器开发有限公司'),
    ('知楚', '知楚'),
    ('', ''),
])
def test_a_registered_name_matches_its_brand(left, right):
    if not left and not right:
        assert brand_name_matches(left, right) is False   # 两边都空：不匹配
        return
    assert brand_name_matches(left, right) is True


@pytest.mark.parametrize('left,right', [
    ('知楚', '上海知楚仪器'),         # 只多一个"仪器"：可能是经销商名，不认
    ('知楚', '知楚仪器'),
    ('湘仪', '湘智'),                # 实测 task-271 的替代候选就是这家，不同厂
    ('湘仪', '湘潭湘仪'),             # 同字号不同主体：湘潭做天平，湖南湘仪做离心机
    ('知楚', ''),
    ('', '知楚'),
    (None, '知楚'),
])
def test_a_different_or_unregistered_name_does_not_match(left, right):
    assert brand_name_matches(left, right) is False


def test_non_strings_fail_closed():
    assert brand_name_matches(123, '知楚') is False
    assert brand_name_matches('知楚', {'brand': '知楚'}) is False
    assert brand_name_matches(None, None) is False


def test_the_two_real_records_now_land_the_same_way():
    """把「改前」的两条真实记录按现在的判据再走一遍。

    ``task-20358119eae4``（hubu）改前 ``BRAND_MISMATCH``，加别名表后放行；
    ``task-013dcdcff7e6``（ccgp-shaanxi）原先按"两个实体"拒，2026-10-02 核实
    ``贝克曼`` 与 ``贝克曼库尔特`` 是同一家（登记名/商标两列同行自证）后
    也放行——这两笔 110,000／台 的成交价本身就该采信。反例改由
    ``湘仪`` vs ``湘潭湘仪``、``洁康`` vs ``深圳捷康`` 那两组守着（见本文件
    与 ``test_brand_identity_call_sites.py``）。
    """
    from backend.services.price_rejections import explain_rejection
    from backend.services.procurement_rules import BRAND_MISMATCH

    hubu_row = {
        'kind': 'award', 'model': '1300A2', 'amount': 69200, 'amount_unit': '元',
        'amount_scope': 'unit', 'quantity': 2,
        'excerpt': '参考品牌及型号 | 上海知楚仪器设备1300A2',
    }
    hubu_source = {
        'url': 'http://ztb.hubu.edu.cn/info/1203/8269.htm', 'body_status': 'fetched',
        'source_type': 'tender_document',
        'body_text': '中标（成交）结果公告\n参考品牌及型号 | 上海知楚仪器设备1300A2\n'
                     '数量 | 2\n单价（元） | ￥69,200.00\n',
    }
    reason = explain_rejection(hubu_row, hubu_source, {'brand': '知楚', 'model': '1300A2'})
    assert reason.get('reason_code') != BRAND_MISMATCH, (
        f'登记名仍然被当成另一个品牌：{reason}')

    beckman_row = {
        'kind': 'award', 'model': 'Avanti J-15R', 'amount': 110000, 'amount_unit': '元',
        'amount_scope': 'unit', 'quantity': 2, 'brand': '贝克曼',
        'excerpt': '2-5 | 其他医疗设备 | 低速离心机 | 贝克曼 | Avanti J-15R | 2.00(台) | 110,000.00 | 220,000.00',
    }
    beckman_source = {
        'url': 'http://www.ccgp-shaanxi.gov.cn/freecms/site/shaanxi/ggxx/info/2024/x.html',
        'body_status': 'fetched', 'source_type': 'tender_document',
        'body_text': '中标（成交）结果公告\n采购人 | 某医院\n项目编号 | X-1\n'
                     + beckman_row['excerpt'] + '\n',
    }
    reason = explain_rejection(beckman_row, beckman_source,
                              {'brand': '贝克曼库尔特', 'model': 'Avanti J-15R'})
    assert reason.get('reason_code') != BRAND_MISMATCH, (
        f'同一家的两种写法仍被判成两家：{reason}')


@pytest.mark.parametrize('left,right', [
    ('Leica', '徕卡'),        # 厂商中文官方名：Leica Microsystems 中文站自称「徕卡显微系统」
    ('徕卡', 'Leica'),
    ('LEICA', '徕卡'),
    ('leica', '徕卡'),
    ('Leica', 'LEICA'),      # 同一别名表顺带覆盖大小写
    ('徕卡', '徕卡'),
])
def test_a_brand_and_its_official_chinese_name_are_one_brand(left, right):
    assert brand_name_matches(left, right) is True


@pytest.mark.parametrize('left,right', [
    # 别的显微镜品牌：绝不许和 Leica 合并
    ('Leica', 'Olympus'), ('Leica', 'Zeiss'), ('Leica', 'Nikon'), ('Leica', 'Motic'),
    ('徕卡', '奥林巴斯'), ('徕卡', '蔡司'), ('徕卡', '尼康'), ('徕卡', '麦克奥迪'),
    # 刻意没有依据、不收的写法
    ('徕卡', '莱卡'),          # Lycra 的中文名同样是"莱卡"；语料里没有它指 Leica 的实例
    ('徕卡等', 'Leica'),       # 实测 t20260710_26914452：这一格还并列着别的品牌（整包价 547,700.00）
    ('徕卡', '上海徕卡仪器有限公司'),   # 登记名不等于品牌别名，别把经销商当厂商
    # 无关公司名不许借"知楚"之名合并
    ('知楚', '上海一恒科学仪器有限公司'),
    ('知楚', '湖南湘仪实验室仪器开发有限公司'),
    ('知楚', '上海知楚仪器'),          # 只多一个"仪器"：可能是经销商名
])
def test_a_different_brand_is_never_merged(left, right):
    assert brand_name_matches(left, right) is False


def test_the_measured_leica_announcement_no_longer_trips_the_brand_gate():
    """a283 实测被拒的那条（47000）按现在的判据再走一遍。

    ``设计说明`` §5.2 表格第 1 行：大连医科大学，
    ``品牌：徕卡 规格型号：Dmi 1 数量：2 单价（元）：47000.0000``，
    ``other_brand='徕卡'``、``reason_code='brand_mismatch'``。
    """
    from backend.services.price_rejections import explain_rejection
    from backend.services.procurement_rules import BRAND_MISMATCH

    row = {
        'kind': 'award', 'brand': '徕卡', 'model': 'Dmi 1',
        'amount': 47000.0, 'amount_unit': '元', 'amount_scope': 'unit', 'quantity': 2,
        'date': '2025-10-22',
        'excerpt': '购倒置显微镜1：A02100301显微镜 品牌：徕卡 规格型号：Dmi 1 数量：2 单价（元）：47000.0000',
    }
    source = {
        'url': 'http://www.ccgp.gov.cn/cggg/dfgg/zbgg/202510/t20251022_25553858.htm',
        'body_status': 'fetched', 'source_type': 'tender_document',
        'publication_date': '2025-10-22',
        'body_text': '中标（成交）结果公告\n项目编号：JH25-210000-33918\n采购人：大连医科大学\n'
                     '发布日期：2025-10-22\n' + row['excerpt'] + '\n中标（成交）金额：人民币94,000元\n',
    }
    reason = explain_rejection(row, source, {'brand': 'Leica', 'model': 'DMi1'})
    assert reason.get('reason_code') != BRAND_MISMATCH, f'徕卡仍被当成另一个品牌：{reason}'


@pytest.mark.parametrize('left,right', [
    # 苏州安泰空气技术有限公司自己的官网（http://www.antai17.cn/ ）整站标题即
    # 「苏净安泰AIRTECH 苏州安泰空气技术有限公司」，其「关于我们」写
    # 「……『苏净安泰』商标及品牌为业内所一致认可」：登记名与商标名是同一家。
    ('苏州安泰', '苏净安泰'),
    ('苏净安泰', '苏州安泰'),
    ('苏州安泰空气技术有限公司', '苏净安泰'),
    ('苏州 安泰', '苏净安泰'),          # 扫描件 OCR 会把词内空格留下
    ('苏州安泰空气技术有限公司', '苏州安泰'),
])
def test_a_trademark_and_its_registered_name_are_one_brand(left, right):
    assert brand_name_matches(left, right) is True


@pytest.mark.parametrize('left,right', [
    ('苏净', '苏净安泰'),          # 江苏苏净集团旗下不止一家公司：只收「苏州安泰」这个写法
    ('苏净安泰', '苏净'),
    ('苏州安泰', '苏州净化设备有限公司'),
    ('苏州安泰', '苏州安泰生物技术有限公司'),   # 只借了地名与两个字，是不同的登记名
    ('苏净安泰', '安泰'),
])
def test_an_unrelated_suzhou_name_is_never_merged(left, right):
    assert brand_name_matches(left, right) is False


def test_the_measured_biosafety_cabinet_row_no_longer_trips_the_brand_gate():
    """124 实测被拒的那条（49500）按现在的判据再走一遍。

    ``设计规范-生物安全柜额度基线.md`` §1：CCGP《雄安宣武医院…第十一包中标公告》
    的「分项报价表」附件里，品牌列 ``苏州安泰``、制造商列
    ``苏州安泰空气技术有限公司``、规格型号 ``BSC-1604 IIA2-e``、5 台、单价 49500、
    合计 247500（与公告总价 5,648,500 对账）。判据把它读成
    ``other_brand='苏州安泰'`` 的 ``brand_mismatch``。
    """
    from backend.services.price_rejections import explain_rejection
    from backend.services.procurement_rules import BRAND_MISMATCH

    row = {
        'kind': 'award', 'brand': '苏州安泰', 'model': 'BSC-1604 IIA2-e',
        'amount': 49500.0, 'amount_unit': '元', 'amount_scope': 'unit', 'quantity': 5,
        'date': '2025-10-27',
        'excerpt': '| 生物安全柜（ 双人）（注册 证名称： 生物安全柜） | 苏州 安泰 | '
                   'BSC-1604 IIA2-e | 苏州 | 苏州安泰 空气技术 有限公司 | 5台 | 49500 | 247500 |',
    }
    source = {
        'url': 'http://ccgp-hebei.gov.cn/BidWinAnncFiles/0063507b-3b43-4808-ac3a-2f45298ceac5.pdf',
        'body_status': 'fetched', 'source_type': 'tender_document',
        'body_text': row['excerpt'] + '\n单位：元（人民币）\n',
        'parent_url': 'http://www.ccgp.gov.cn/cggg/dfgg/zbgg/202510/t20251027_25581686.htm',
        'publication_context': {
            'url': 'http://www.ccgp.gov.cn/cggg/dfgg/zbgg/202510/t20251027_25581686.htm',
            'body_text': '雄安宣武医院运行建设相关的补充医疗设备项目第十一包中标公告\n'
                         '总中标金额 | ￥564.850000 万元（人民币）\n',
            'linked_url': 'http://ccgp-hebei.gov.cn/BidWinAnncFiles/'
                          '0063507b-3b43-4808-ac3a-2f45298ceac5.pdf'},
    }
    reason = explain_rejection(row, source, {'brand': '苏净安泰', 'model': 'BSC-1604IIA2-e'})
    assert reason.get('reason_code') != BRAND_MISMATCH, (
        f'苏州安泰仍被当成另一个品牌：{reason}')


@pytest.mark.parametrize('left,right', [
    # 上海申安医疗器械厂自己的官网（http://www.shenan17.cn/ ）「关于我们」原文写
    # 「使《shenan》牌产品不断升级达到了一个全新的品牌平台」，整站标题与产品页
    # 一律自称「上海申安医疗器械厂」、站名 logo 的 alt 是「申安」；行业名录里
    # 两种写法直接并列——苏州赛恩斯仪器有限公司的产品页把品牌栏写成
    # 「SHENAN 申安」、生产厂家栏写成「SHENAN 申安医疗器械」
    # （https://sns17.com/product/shenan-ldzm-60kcs.html ）。
    ('申安', '上海申安'),
    ('上海申安', '申安'),
    ('SHENAN', '上海申安'),
    ('shenan', '上海申安'),
    ('Shenan', '上海申安'),
    ('SHENAN', '申安'),
    ('申安', 'SHENAN'),
    ('SHENAN', '上海 申安'),        # 扫描件 OCR 会把词内空格留下
    ('上海申安', 'ＳＨＥＮＡＮ'),      # 全角：identity 走 NFKC + casefold
])
def test_a_sterilizer_brand_and_its_english_mark_are_one_brand(left, right):
    assert brand_name_matches(left, right) is True


@pytest.mark.parametrize('left,right', [
    # 厂商官网「公司概况」原文即「湖南湘仪实验室仪器开发有限公司（简称湘仪）」
    # （https://www.xylxj.com/aboutus.html ）——简称是厂商自己写的，不是我们缩的。
    ('湖南湘仪', '湘仪'),
    ('湘仪', '湖南湘仪'),
    ('湖南 湘仪', '湘仪'),
    ('湖南湘仪', '湘 仪'),
    # 上海卢湘仪离心机仪器有限公司：公告写「品牌：上海卢湘仪」，目标品牌写「卢湘仪」。
    # 实测 CCGP 安徽职业技术大学 2025 年食品检验检测实训室建设项目中标结果公告
    # （http://www.ccgp.gov.cn/cggg/dfgg/zbgg/202512/t20251217_25946080.htm ）：
    # 型号 H18.5B、数量 1 台、单价 12000 元/台，整条真中标单价只因这两个写法被拒。
    ('上海卢湘仪', '卢湘仪'),
    ('卢湘仪', '上海卢湘仪'),
    ('上海 卢湘仪', '卢湘仪'),
])
def test_a_registered_name_without_its_place_is_one_brand(left, right):
    assert brand_name_matches(left, right) is True


@pytest.mark.parametrize('left,right', [
    # 同类灭菌器厂，各有自己的官网：上海三申医疗器械有限公司
    # （http://sh-sanshen.cn/ ）、上海博迅医疗生物仪器股份有限公司
    # （https://www.sh-boxun.cn/ ）。字号只共一个字，不许合并。
    ('申安', '上海三申'), ('上海三申', '申安'), ('SHENAN', '上海三申'),
    ('申安', '上海博迅'), ('申安', '博迅'),
    # 别的离心机厂
    ('湖南湘仪', '湘智'), ('湘仪', '湘智'), ('湖南湘仪', '湖南赫西'),
    ('湖南湘仪', '湖南凯达'),
    # 「湘仪」这个字号长沙不止一家在用：另有「长沙高新开发区湘仪天平仪器设备
    # 有限公司」（https://www.xytp.com/about/1.html ，做天平，官网自称「湘仪产品」）。
    # 所以别名只收完整的「湖南湘仪」，裸的「湘仪」和「湘仪天平」都不认。
    ('湖南湘仪', '湘仪天平'), ('湘仪', '湘仪天平'), ('申安', '湘仪天平'),
    # 两个新条目之间不许串
    ('湖南湘仪', '上海申安'), ('申安', '湘仪'), ('SHENAN', '湖南湘仪'),
    # 与别的品牌一概不认
    ('申安', '知楚'), ('申安', '徕卡'), ('申安', '苏净安泰'),
    ('申安', '贝克曼库尔特'), ('申安', '一恒'),
    ('湖南湘仪', '知楚'), ('湖南湘仪', '贝克曼库尔特'), ('湖南湘仪', '苏净安泰'),
    # 「卢湘仪」这条新条目同样不许把它与别的字号串起来：湘仪（湖南）、
    # 湘智、湘仪天平 都是另外的写法，只收完整的「上海卢湘仪」↔「卢湘仪」这一对。
    ('上海卢湘仪', '湘仪'), ('卢湘仪', '湘仪'), ('卢湘仪', '湘智'),
    ('上海卢湘仪', '湖南湘仪'), ('卢湘仪', '湘仪天平'), ('卢湘仪', '申安'),
])
def test_a_name_that_only_shares_a_word_is_never_merged(left, right):
    assert brand_name_matches(left, right) is False


@pytest.mark.parametrize('left,right', [
    # ``设计说明`` §四/§五点名"判据拒得对"的 5 条重放记录：
    # 4 条一格并列两个品牌（``(卢)湘仪/湘鑫``，1.75/3.00/3.00/3.98 万元），
    # 1 条只是型号里含目标型号（``上海上净`` 对 ``知楚``，SJ-LS-1300A2）。
    # 加别名之后必须仍然被拒——否则就是把歧义行或别家的价放进来。
    ('(卢)湘仪/湘鑫', '湘仪'),
    ('(卢)湘仪/湘鑫', '湖南湘仪'),
    ('(卢)湘仪/湘鑫', '湘鑫'),
    ('上海上净', '知楚'),
    ('上海上净', '上海知楚仪器设备'),
])
def test_the_five_correct_refusals_stay_refused(left, right):
    assert brand_name_matches(left, right) is False


def test_the_measured_liuxiangyi_row_still_trips_the_brand_gate():
    """``(卢)湘仪/湘鑫`` 那条重放记录走完整判据路径，加别名后仍要是 ``brand_mismatch``。

    引文逐字来自 ``output/brand-audit/12_pairs_and_fix_sim.txt`` 的第 [10] 条
    （来源 ``https://www.120shop.cn/i890850380``，1.75 万元）。
    """
    from backend.services.price_rejections import explain_rejection
    from backend.services.procurement_rules import BRAND_MISMATCH

    excerpt = ('血型专用离心机 | (卢)湘仪/湘鑫 | XKA - 2200 | 木垒哈萨克自治县人民医院 | '
               '新疆新丝路惠康医疗科技有限公司 | 2026-05-19 | 1.75万元 | 查看')
    row = {
        'kind': 'award', 'brand': '(卢)湘仪/湘鑫', 'model': 'XKA-2200',
        'amount': 17500.0, 'amount_unit': '元', 'amount_scope': 'unit', 'quantity': 1,
        'excerpt': excerpt,
    }
    source = {
        'url': 'https://www.120shop.cn/i890850380', 'body_status': 'fetched',
        'source_type': 'tender_document',
        'body_text': '中标（成交）结果公告\n' + excerpt + '\n',
    }
    reason = explain_rejection(row, source, {'brand': '湘仪', 'model': 'XKA-2200'})
    assert reason.get('reason_code') == BRAND_MISMATCH, (
        f'一格并列两个品牌的行被放行了：{reason}')


def test_the_measured_shanghai_shangjing_row_still_trips_the_brand_gate():
    """``上海上净`` 对 ``知楚`` 那条（型号 ``SJ-LS-1300A2``）也必须仍然被拒。

    目标型号取引文里那个完整型号，好把**品牌判据**单独隔离出来测；
    审计原场景（目标型号 ``1300A2``）下它先被型号判据拦下，同样不通过。
    """
    from backend.services.price_rejections import explain_rejection
    from backend.services.procurement_rules import BRAND_MISMATCH

    excerpt = ('20 | 实验室配套专用设备采购 | 生物安全柜 | 上海上净 | '
               'SJ-LS-1300A2 | 1 | 24000')
    row = {
        'kind': 'award', 'brand': '上海上净', 'model': 'SJ-LS-1300A2',
        'amount': 24000.0, 'amount_unit': '元', 'amount_scope': 'unit', 'quantity': 1,
        'excerpt': excerpt,
    }
    source = {
        'url': 'http://www.ccgp.gov.cn/cggg/dfgg/zbgg/x.htm', 'body_status': 'fetched',
        'source_type': 'tender_document',
        'body_text': '中标（成交）结果公告\n' + excerpt + '\n',
    }
    reason = explain_rejection(row, source,
                              {'brand': '知楚', 'model': 'SJ-LS-1300A2'})
    assert reason.get('reason_code') == BRAND_MISMATCH, (
        f'别家的生物安全柜被放行了：{reason}')

