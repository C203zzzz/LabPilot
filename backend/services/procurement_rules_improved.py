"""第一性原理优化：procurement_rules核心业务规则

修复P1.1：边界条件处理
修复P0.2：输入校验完整性
修复P1.3：类型安全
"""
from __future__ import annotations

import math
import re
import unicodedata
from datetime import date


# ============================================================================
# 阶段1：基础标准化与校验
# ============================================================================

def identity(value) -> str:
    """标准化标识符，保留数字和字母，移除空格和连字符

    关键规则：
    1. 空值抛出异常，不返回空字符串
    2. X100和X100S永远不会合并
    3. 大小写不敏感

    Raises:
        ValueError: 输入为空或标准化后为空
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ValueError("identity value cannot be empty")

    # Unicode标准化，将减号U+2212等转为连字符
    normalized_value = unicodedata.normalize("NFKC", str(value))
    # 移除所有连字符和空格（包括各种Unicode变体）
    # 不使用范围，逐个列出Unicode字符避免正则范围错误
    normalized = re.sub(
        r"[\s\-‐‑‒–—―−]+",
        "",
        normalized_value
    ).casefold()

    if not normalized:
        raise ValueError(f"value becomes empty after normalization: {value!r}")

    return normalized


#: 工商登记名的结尾词，用来把「品牌」和「公司全名」分开。
#:
#: 实测（``docs/待归档-价格没被采信的根因.md`` §1.4）：中标公告的
#: 「参考品牌及型号」列里写的是**供应商/生产商的登记名**——
#: ``上海知楚仪器设备1300A2``，抽取层于是把品牌读成 ``上海知楚仪器设备``，
#: 而比较基准写的品牌是 ``知楚``，``brand_identity`` 判成两个品牌，
#: 那条同型号成交价整条丢掉（``task-<run-8>``，
#: ``http://ztb.hubu.edu.cn/info/1203/10000005.htm``）。
#:
#: 只放行**带企业形态后缀**的长名，不按前缀匹配：``知楚`` 与 ``上海知楚仪器``
#: 仍然必须判成两个品牌（后者可能是经销商名），这样才不会把经销商自述
#: 当成厂商标称。结尾词表刻意写全长形态，不写裸的 ``仪器``/``设备``/``科技``。
_ENTERPRISE_SUFFIX = re.compile(
    r'(?:股份有限公司|有限责任公司|有限公司|仪器设备|科学仪器|仪器仪表|生物科技'
    r'|生物技术|医疗器械|实验仪器|实验设备|仪器公司|设备公司|商贸|贸易|科贸)$'
)


def _looks_like_enterprise_name(value: str) -> bool:
    return bool(_ENTERPRISE_SUFFIX.search(value))


#: 品牌别名表：``identity()`` 之后的写法 → 同一品牌的规范写法。
#:
#: 这是**唯一**的品牌别名来源：``procurement_rules.brand_identity`` 委托到这里，
#: ``brand_name_matches`` 也读它。表驱动、可增删；判据本身不做任何模糊匹配——
#: 不确定的写法一律判成两个品牌，品牌判错会把别家的价安到这台机器上。
#:
#: 每一条都要能写出依据：
#:
#: * ``天津泰斯特`` → ``泰斯特``、``上海一恒`` → ``一恒``：登记名去掉地名前缀的
#:   同义写法（既有条目，见 ``tests/test_procurement_rules_adversarial.py``）。
#: * ``徕卡`` → ``leica``：厂商的**中文官方名**与英文品牌名是同一个品牌。
#:   Leica Microsystems 中文站自称「徕卡显微系统」
#:   （https://www.leica-microsystems.com.cn/cn/company/about-us/ ），
#:   其中文站首页标题即「徕卡显微镜中国官网」（https://cn.leica-microsystems.com.cn/ ）。
#:   语料内的对应关系：``task-<run-4>`` 的公告把 ``品牌：徕卡`` 与
#:   ``规格型号：Dmi 1`` 写在同一行，而 ``DMi1`` 就是 Leica 的倒置显微镜型号。
#:   实测代价：目标品牌 ``Leica`` 的两条真实单台成交——a real university 47000、
#:   a real CDC body 55000——都被判 ``brand_mismatch``（``other_brand='徕卡'``），
#:   见 ``docs/待归档-改后效果评测.md`` §5.2/§5.4。
#: * ``苏州安泰`` → ``苏净安泰``：同一家公司**登记名**与**商标名**的两种写法。
#:   苏州安泰空气技术有限公司自己的官网（http://www.antai17.cn/ ）整站标题即
#:   「苏净安泰AIRTECH 苏州安泰空气技术有限公司」，其「关于我们」原文写
#:   「苏州安泰空气技术有限公司是江苏苏净集团有限公司与日本空气技术株式会社共同
#:   组建的合资企业……『苏净安泰』商标及品牌为业内所一致认可」——即登记名是
#:   「苏州安泰空气技术有限公司」，商标/品牌是「苏净安泰」。
#:   语料内的对应关系是同一行两列：CCGP《a real hospital…第十一包中标公告》
#:   （http://www.ccgp.gov.cn/cggg/dfgg/zbgg/202510/t20251001_10000003.htm ）
#:   的「分项报价表」附件里，品牌列写 ``苏州安泰``、制造商列写
#:   ``苏州安泰空气技术有限公司``，规格型号 ``BSC-1604 IIA2-e`` 与需求的
#:   ``苏净安泰 BSC-1604IIA2-e`` 是同一台机器。
#:   实测代价：这条同型号单台成交价 49500 元（5 台 247500，与公告总价
#:   5,648,500 对账）被判 ``brand_mismatch``（``other_brand='苏州安泰'``），
#:   见 ``docs/待归档-官方价格读取修复.md``。
#:   同一张表的**制造商列**写的是登记全名，所以全名一并收：抽取层经常把
#:   「参考品牌及型号」那一格里印的登记名读成品牌（见本文件 :data:`_ENTERPRISE_SUFFIX`
#:   的实测记录）。仓内既有语料也是这个形状：``tests/fixtures/template-regression/
#:   attachment-line-item-table.txt`` 的一行里品牌写 ``苏净安 泰``、制造商写
#:   ``苏州安泰空⽓技术有限公司``。
#: * ``申安`` → ``上海申安``、``shenan`` → ``上海申安``：同一个灭菌器品牌的
#:   **中文字号**与**英文商标**。厂商自己的官网（http://www.shenan17.cn/ ，
#:   「关于我们」页）原文即写「使《shenan》牌产品不断升级达到了一个全新的
#:   品牌平台」，整站标题与产品页一律自称「上海申安医疗器械厂」、站名 logo
#:   的 alt 是「申安」。行业名录里两种写法直接并列：苏州赛恩斯仪器有限公司的
#:   产品页把品牌栏写成「SHENAN 申安」、生产厂家栏写成「SHENAN 申安医疗器械」
#:   （某厂商的产品页）。
#:   语料内的对应关系：``task-<run-9>`` 的安徽那份来源，同一行里
#:   ``SHENAN 安全用仪器SHENAN上海申安LDZX-50L立式高压蒸汽灭菌器`` 两种写法
#:   并列（Z1 自证，全库唯一一条引文内自证的中英对）；同一份来源的另一笔
#:   （``zbcg.wit.edu.cn``，13000 元）只写 ``品牌(如有):申安``。
#:   实测代价与**双向对照**：目标品牌写 ``上海申安`` 时，安徽那笔 13900 元
#:   被判 ``brand_mismatch``（``other_brand='SHENAN'``）；把基准只写成
#:   ``SHENAN`` 再跑（``task-<run-10>``），浙江那笔 12000 元反过来被拒
#:   （``other_brand='上海申安'``）——同一台机器的两笔真实成交，一次运行里
#:   只能进来一笔。见 ``docs/190`` 第 75 行与 ``docs/DRAFT-brand-audit.md``。
#:   字号唯一性（2026-09 核对公开资料）：仪器/灭菌器领域查到的是同一家——
#:   上海申安医疗器械厂（企查查：1989-11-03 成立、注册资本 1000 万元、
#:   上海市嘉定区安亭镇和静路 2000 号），关联企业只有它自己的工会与一个已
#:   注销的经营部。另有一家同名「申安仪器有限公司」（浙江嘉兴经开区百墅路
#:   101 号，http://www.sterilizer.cn/ 与 https://www.shenan.net/ ），
#:   但它「公司简介」与上海申安官网的「关于我们」**逐字相同**（连注册于
#:   1989 年 11 月、注册资金 1000 万元、厂区 30000 平方米、员工 200 余人
#:   都在），产品型号也是同一套 ``DSX``/``LDZX``/``LDZM``/``LDZF``/``LDZH``，
#:   指向的是同一个 ``shenan`` 牌，不是第二个东家；它与上海申安医疗器械厂的
#:   确切工商关系没查到，这一条按"未完全验证"记。
#: * ``湖南湘仪`` → ``湘仪``：登记名去掉地名前缀的同义写法，与 ``天津泰斯特`` →
#:   ``泰斯特`` 同类。**简称是厂商自己写的**：官网「公司概况」原文即
#:   「湖南湘仪实验室仪器开发有限公司（简称湘仪）」
#:   （某厂商官网的公司简介页）。
#:   语料内的对应关系：离线重放从 acceptance 库抓到的 3 条，品牌列写
#:   ``湖南湘仪``、型号 ``XKA-2200``（2 条）与 ``H1850R``（1 条），
#:   所在任务的目标品牌写 ``湘仪``，三条都被判 ``brand_mismatch``。
#:   字号唯一性（2026-09 核对公开资料）：``湘仪`` 这个字号**不是长沙独一家**——
#:   另有「长沙高新开发区湘仪天平仪器设备有限公司」
#:   （某厂商官网的公司简介页 ，原机械工业部直属企业湖南仪器仪表
#:   总厂天平仪器厂改制而来，做天平，官网自称「湘仪产品」）。所以这条**只收
#:   完整的 ``湖南湘仪``**，不收裸的 ``湘仪``；重放碰到的 3 条型号都是湘仪
#:   离心机的型号。
#:
#: **刻意不收的写法**（"可能但不一定"；拒了只是少一条价，收了可能是错价）：
#: ``莱卡``（Lycra 的中文名同样是"莱卡"，且本仓语料里没有它指 Leica 的实例）、
#: ``徕卡等``（"等"说明这一格还并列着别的品牌，实测 ``t20260710_26914452``
#: 那一行是整包价 ``547,700.00``，现行判据拒得对）、以及任何靠"看着像"成立的对应。
#: ``苏净`` 单独一个词**不收**：江苏苏净集团旗下不止一家公司，只有
#: 「苏州安泰」与「苏州安泰空气技术有限公司」这两个具体写法有上面的证据。
#: ``湘仪`` 单独一个词**不收**：长沙另有「长沙高新开发区湘仪天平仪器设备
#: 有限公司」在用这个字号（做天平，官网自称「湘仪产品」），只有完整的
#: 「湖南湘仪」有上面的证据。
#: * ``上海卢湘仪`` → ``卢湘仪``：与上面三条同为"登记名去掉地名前缀"的同义写法。
#:   实测的代价（2026-09 实测）：CCGP a real college
#:   2025 年食品检验检测实训室建设项目中标结果公告
#:   （http://www.ccgp.gov.cn/cggg/dfgg/zbgg/202512/t20251201_10000004.htm ）
#:   正文写「品牌：上海卢湘仪 / 规格型号：H18.5B / 数量：1台 / 单价：12000元/台」，
#:   而候选 H18.5B 的目标品牌写 ``卢湘仪``——型号、数量、金额、成交措辞全部对得上，
#:   整条真中标单价只因这两个写法被判成两家而被拒（``brand_mismatch``，
#:   ``other_brand='上海卢湘仪'``，见该文件第 4164-4172 行）。
#:   同一个字号在语料里两处出现：``www.lxylxj.com``（上海卢湘仪离心机，正文自称
#:   「H18.5B 高速离心机-上海卢湘仪离心机…」）与 ``antinglxj.com`` 的
#:   ``gl-10000c`` 产品页。**未逐字核对工商登记**，按"厂商官网与公告自己这么写"记。
#:   这里**只收完整的 ``上海卢湘仪``**，不收裸的 ``卢湘仪`` 之外的地名变体。
#: ``(卢)湘仪/湘鑫`` 这类**一格并列两个品牌**的写法也**不收**：加了会把
#: 写不清是哪一家的行放进来（重放的 4 条实测因此保持被拒）。
_BRAND_ALIASES = {
    '天津泰斯特': '泰斯特',
    '上海一恒': '一恒',
    '湖南湘仪': '湘仪',
    '上海卢湘仪': '卢湘仪',
    '徕卡': 'leica',
    '申安': '上海申安',
    'shenan': '上海申安',
    '苏州安泰': '苏净安泰',
    '苏州安泰空气技术有限公司': '苏净安泰',
}


def brand_identity(value) -> str:
    """品牌标识符标准化

    只查这张显式别名表，不按前缀/后缀/读音截断或猜测。
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ValueError("brand value cannot be empty")

    token = identity(value)
    return _BRAND_ALIASES.get(token, token)


def brand_name_matches(left, right) -> bool:
    """两个品牌写法是不是同一家。判据窄，且必须双方都非空。

    放行的只有两种形状，都在**这一处**裁决：

    * 一侧是另一侧的**企业登记名**——``上海知楚仪器设备`` 剥掉 ``仪器设备``
      之后以 ``知楚`` 结尾（``docs/待归档-价格没被采信的根因.md`` §1.4）；
    * 两侧在 :data:`_BRAND_ALIASES` 里归到同一个规范写法——中英对照
      （``徕卡`` ↔ ``Leica``）与登记名去地名（``天津泰斯特`` ↔ ``泰斯特``）
      用的是同一张表、同一个分支，不为某一条品牌单开判据。

    刻意**不**放行的（都有反例）：
    * ``知楚`` 与 ``上海知楚仪器``：剥不出登记名后缀，判成两个品牌；
    * ``贝克曼`` 与 ``贝克曼库尔特``：不同实体（实测 ``task-<run-6>``
      的 2 条 ``brand_mismatch`` 靠这条成立）；
    * 没有别名条目的跨语言对：``贝克曼`` 与 ``Beckman Coulter`` 这里不猜。
    """
    left_token = brand_identity(left) if left and str(left).strip() else ''
    right_token = brand_identity(right) if right and str(right).strip() else ''
    if not left_token or not right_token:
        return False
    if left_token == right_token:
        return True
    for long_name, short_name in ((left_token, right_token), (right_token, left_token)):
        if len(short_name) < 2 or short_name not in long_name:
            continue
        stripped = long_name[:len(long_name) - len(short_name)]
        if _looks_like_enterprise_name(long_name) and stripped:
            return True
    return False


# ============================================================================
# 阶段1c：资料正文里的厂名抽取（只补抽取，不放宽任何判据）
# ============================================================================

#: 企业名的**组织形式后缀**，在正文里当厂名锚点用。
#:
#: 刻意只收长形态，不收裸的 ``公司``／``厂``：反例是清洗机说明书正文里的
#: ``切勿私自使用配件或使用厂家未曾建议的配件``（``sample-b`` 真实正文），
#: 裸字会把它当成厂名。
_ENTERPRISE_FORM_SUFFIX = (
    '股份有限公司', '有限责任公司', '有限公司',
    '仪器厂', '器械厂', '设备厂', '制造厂', '器材厂',
)

#: 厂名左侧必须落在分隔符上（行首、空白、标点）。PDF 文字层常在汉字之间插
#: 单空格（``上海天美科学 仪器 有限公司``），所以名字内部允许单空格，但连续
#: 空白与换行是边界——厂名页是独立成行的联系方式块，靠这条把句子切开。
_ENTERPRISE_NAME_RE = re.compile(
    r'(?:^|[\s\u3000,，。;；:：、!！?？()（）\[\]【】<>《》"“”\'’/|｜·•\-—～~])'
    r'((?:[\u4e00-\u9fff]{1,6}[ \t\u3000]?){1,4}'
    r'(?:' + '|'.join(_ENTERPRISE_FORM_SUFFIX) + r'))',
    re.MULTILINE,
)

#: 常见行政区划写法。只用于**裁掉厂名左侧被一起读进来的句子**——
#: ``请与我们联系上海天美科学仪器有限公司`` → ``上海天美科学仪器有限公司``；
#: 它不参与品牌判据，判据仍然只看 :data:`_BRAND_ALIASES` 与
#: :func:`brand_name_matches`，这里不做任何"看着像"的放宽。
_ENTERPRISE_PLACE_PREFIXES = (
    '内蒙古', '黑龙江', '新疆', '西藏', '宁夏', '广西', '香港', '澳门',
    '北京', '上海', '天津', '重庆',
    '石家庄', '唐山', '太原', '沈阳', '大连', '鞍山', '长春', '哈尔滨',
    '南京', '苏州', '无锡', '常州', '徐州', '南通', '连云港',
    '杭州', '宁波', '温州', '嘉兴', '绍兴', '金华', '台州',
    '合肥', '芜湖', '蚌埠', '福州', '厦门', '泉州', '南昌',
    '济南', '青岛', '烟台', '潍坊', '淄博', '威海', '郑州', '洛阳',
    '武汉', '宜昌', '襄阳', '长沙', '株洲', '湘潭',
    '广州', '深圳', '珠海', '东莞', '佛山', '中山', '惠州', '汕头',
    '南宁', '桂林', '海口', '三亚', '成都', '绵阳', '贵阳', '昆明', '拉萨',
    '西安', '宝鸡', '兰州', '西宁', '银川', '乌鲁木齐',
    '河北', '山西', '辽宁', '吉林', '江苏', '浙江', '安徽', '福建', '江西',
    '山东', '河南', '湖北', '湖南', '广东', '海南', '四川', '贵州', '云南',
    '陕西', '甘肃', '青海', '台湾',
)

#: 厂名长度上下限（去空格后的汉字数）。低于下限不采用；高于上限说明左侧
#: 吃进了句子、又没有地名可裁，同样不采用——宁漏不错。
_ENTERPRISE_NAME_MIN_CHARS = 5
_ENTERPRISE_NAME_MAX_CHARS = 20


def _trim_enterprise_name(name: str) -> str | None:
    """把被一起读进来的前缀裁掉；裁不出来就返回 None（不猜）。"""
    index = max((name.rfind(place) for place in _ENTERPRISE_PLACE_PREFIXES), default=-1)
    if index > 0:
        name = name[index:]
    if not _ENTERPRISE_NAME_MIN_CHARS <= len(name) <= _ENTERPRISE_NAME_MAX_CHARS:
        return None
    return name


def enterprise_names_in_text(text) -> list[str]:
    """按企业名后缀从正文里抽厂名，按出现顺序去重。

    确定性规则，不调用模型：资料正文里写明厂名时给出该厂名，没写就返回空
    列表——空的语义是"这份正文里没有厂名"，不是"没抽到"。

    实测来源（``sample-d``，紫外可见分光光度计 UV1100 的 111 页文字层 PDF，
    厂名在最后一页联系方式的独立行里）：

        ``上海天美科学 仪器 有限公司`` → ``上海天美科学仪器有限公司``
    """
    if not isinstance(text, str) or not text.strip():
        return []
    normalized = unicodedata.normalize('NFKC', text)
    names: list[str] = []
    for match in _ENTERPRISE_NAME_RE.finditer(normalized):
        name = _trim_enterprise_name(re.sub(r'\s+', '', match.group(1)))
        if name and name not in names:
            names.append(name)
    return names


def brand_from_enterprise_name(name) -> str:
    """把正文里抽到的厂名转成候选的 ``brand`` 值。

    别名表里有的写法返回规范写法（``苏州安泰空气技术有限公司`` →
    ``苏净安泰``），与候选发现用的 :func:`brand_identity` 是同一个 key；
    其余**原样保留正文里的登记名**——登记名不猜、不改写，判据那一侧由
    :func:`brand_name_matches` 既有的"登记名 vs 字号"分支负责对上
    （``上海天美科学仪器有限公司`` ↔ ``天美``）。
    """
    raw = re.sub(r'\s+', '', unicodedata.normalize('NFKC', str(name or '')))
    if not raw:
        raise ValueError("enterprise name cannot be empty")
    return _BRAND_ALIASES.get(identity(raw), raw)


# ============================================================================
# 阶段1d：正文表格行里的品牌列（只读那一行，读不到就留空）
# ============================================================================

#: 表格/详情页里"品牌"这一栏的列名写法（逐字，来自原文）。
#: ``research_planning._BRAND_FIELD``（详情页的「品牌 奥豪斯」那一栏）与本节的表格列
#: 定位读的是**同一张表**，由这里生成，不另写一套。``品牌商标`` 必须排在 ``品牌``
#: 前面：正则的"或"从左往右试，顺序反了长名永远匹配不上。
BRAND_LABELS = ('品牌商标', '品牌名称', '品牌')

#: "品牌"出现在这些地方时不是在说这台仪器的品牌（导航/推荐位），读到就是错的。
#: ``_BRAND_FIELD`` 与本节共用这一张表。
BRAND_NOISE = ('品牌库', '推荐品牌', '相关品牌', '热门品牌', '品牌中心', '品牌排行',
               '品牌会议', '品牌专区')

#: 表格行的分格符。正文里的表格已被压平成 ``格 | 格``（``web_tables`` 的投影行、
#: 政采公告的明细表都是这个形状）。
_ROW_CELL_SPLIT = re.compile(r'[|｜]')
#: 纯分隔格（``---``／``:--``／空白）不是值。
_SEPARATOR_CELL = re.compile(r'^[\s:：\-—–_]+$')
#: 数量格：``2台``／``19 台``。
_QUANTITY_CELL = re.compile(
    r'^\d+(?:\.\d+)?\s*(?:台|套|件|个|支|组|把|只|箱|盒|瓶|张|块|批|辆|部)$')
#: 金额格：``12,400.00元``／``¥12400``／``1.2万元``／``12400``。
_AMOUNT_CELL = re.compile(r'^[¥￥]?\s*\d[\d,]*(?:\.\d+)?\s*(?:万元|元|人民币)?$')
#: "字母型号"形状的格（``G21000``／``XZ21K-T``／``H1650``）。
#: 只在**候选自己那一行的型号 token 对不上整格**时用（``XZ21K-T`` 那一行直读出来的
#: token 是 ``XZ21K``：``contains_model`` 刻意把 ``-T`` 挡在外面，所以整格对位对不上），
#: 而且必须**恰好一格**是这个形状，多一格就不是"哪一格是型号"能定的了。
_MODEL_SHAPED_CELL = re.compile(r'^[A-Za-z][A-Za-z0-9]*\d[A-Za-z0-9\-‐‑]*$')
#: 这些整格内容不是品牌（占位与分类，不是厂商标的字号），读到就是错的。
_NOT_A_BRAND = frozenset({'无', '不详', '待定', '见附件', '详见附件', '国产', '进口',
                          '其他', '其它', '不适用', '同上', '略', '合计', '小计', '备注',
                          '-', '—', '/', '\\'})
#: 品牌值里出现这些写法说明读到的是别的列（单位/规格/表格术语），不是字号。
_NOT_A_BRAND_IN = ('元', '台', '套', '%', '规格', '型号', '附件', '详见', '单价',
                   '数量', '品牌', '商标', '合计', '小计')

#: 品牌值的长度上下限（去空格后）。低于下限是单字噪声；高于上限基本是把一句话读进来了
#: （企业登记名是另一条路：:func:`enterprise_names_in_text`）。
_BRAND_VALUE_MIN_CHARS = 2
_BRAND_VALUE_MAX_CHARS = 16

#: 行内"标签：值"形态（``品牌：湘智``／``品牌: 湘智``）。与 ``_BRAND_FIELD`` 同一张
#: 标签表；这里只认带冒号的窄形态——表格行里的品牌值本身就挨着 ``|``，不靠这个认。
_ROW_BRAND_LABEL = re.compile(
    r'(?:' + '|'.join(BRAND_LABELS) + r')\s*[:：]\s*([^|｜\s]{2,24})')


def row_cells(line) -> list[str]:
    """把一条压平的表格原文行切成**值格**（丢掉空格的纯分隔格）。

    行的形状是正文整形后的样子：``名称 | 品牌 | 规格型号 | 数量 | 单价``。
    没有分格符的行返回空列表——单独一格读不出"哪一列是品牌"，那不是这条路的活。
    """
    if not isinstance(line, str) or not _ROW_CELL_SPLIT.search(line):
        return []
    cells = [cell.strip() for cell in _ROW_CELL_SPLIT.split(line)]
    return [cell for cell in cells if cell and not _SEPARATOR_CELL.match(cell)]


def looks_like_brand_value(value) -> bool:
    """这一格像不像一个品牌字号（**只做形状判断，不查任何品牌名册**）。

    刻意严，宁可留空：带数字的写法一律不收（``H1650``／``3M``／``G21000`` 都是型号），
    所以 ``3M`` 这类字号会被漏掉——漏一条只是少一个候选，收错了是把别家的机器当成
    这一家的（品牌判据一个字没放宽，错值会直接进报价比对）。
    """
    text = re.sub(r'\s+', '', unicodedata.normalize('NFKC', str(value or '')))
    if not _BRAND_VALUE_MIN_CHARS <= len(text) <= _BRAND_VALUE_MAX_CHARS:
        return False
    if any(ch.isdigit() for ch in text):
        return False
    if text in _NOT_A_BRAND or any(noise in text for noise in BRAND_NOISE):
        return False
    if any(noise in text for noise in _NOT_A_BRAND_IN):
        return False
    if _QUANTITY_CELL.match(text) or _AMOUNT_CELL.match(text):
        return False
    return True


def _brand_cell_from_header(cells) -> str:
    """表头列名与数据格写在**同一行**时，按列名读出品牌列的值；否则空串。

    这是 ``sample-d`` 真实的形状（``yiliaozhaobiao`` 那篇公告的搜索摘要，正文整形把
    ``<thead>`` 丢了，表头只剩下摘要里这一段）::

        … 名称 品牌 规格型号 数量 单价 | 小型台式离心机3 | 湘智 | TGL-20MC | 19台 | 14,500.00元 | | --- | …

    列名自己说了"品牌"是第几列，所以不是猜：要求表头那一格里的列名序列末尾恰好与
    数据格数对齐，且其中"品牌"列名恰好出现一次。
    """
    header_at = None
    for index, cell in enumerate(cells):
        if any(token in BRAND_LABELS for token in cell.split()):
            if header_at is not None:
                return ''
            header_at = index
    if header_at is None:
        return ''
    header = cells[header_at].split()
    values = [cell for position, cell in enumerate(cells) if position != header_at]
    if not values or len(values) > len(header):
        return ''
    columns = header[-len(values):]
    positions = [position for position, name in enumerate(columns) if name in BRAND_LABELS]
    if len(positions) != 1:
        return ''
    value = values[positions[0]]
    return value if looks_like_brand_value(value) else ''


def _brand_from_inline_label(line) -> str:
    """行里就地写着 ``品牌：湘智`` 时取那个值；没有就空串。"""
    for match in _ROW_BRAND_LABEL.finditer(line or ''):
        value = match.group(1).strip()
        if looks_like_brand_value(value):
            return value
    return ''


def _brand_cell_by_shape(cells, model, category) -> str:
    """按"每个格子的语义都对得上"定位品牌列：认领之后**恰好只剩一格**才算数。

    这一行是 ``名称 | 品牌 | 规格型号 | 数量 | 单价`` 这类表格行时，型号格（等于这一条
    候选的型号）、名称格（含这一行的品类词）、数量格、单价格都能从格子的形状上认出来；
    剩下那一格是品牌列。多于一格或没有剩余都返回空串——那时候"哪一格是品牌"就成了猜。
    """
    if not cells:
        return ''
    roles: list[str | None] = [None] * len(cells)
    try:
        model_key = identity(model) if model else ''
    except (TypeError, ValueError):
        model_key = ''
    if not model_key:
        return ''
    model_at = [index for index, cell in enumerate(cells) if identity(cell) == model_key]
    if len(model_at) != 1:
        # 整格对不上（``XZ21K-T`` 那一行的 token 是 ``XZ21K``）时退到"型号形状的格"，
        # 但同样要求恰好一格，不然型号位就不确定了。
        model_at = [index for index, cell in enumerate(cells)
                    if _MODEL_SHAPED_CELL.match(cell)]
    if len(model_at) != 1:
        return ''
    roles[model_at[0]] = 'model'
    category_text = str(category or '').strip()
    if category_text:
        named = [index for index, cell in enumerate(cells)
                 if roles[index] is None and category_text in cell]
        if len(named) != 1:
            return ''
        roles[named[0]] = 'name'
    for role, pattern in (('quantity', _QUANTITY_CELL), ('amount', _AMOUNT_CELL)):
        hit = [index for index, cell in enumerate(cells)
               if roles[index] is None and pattern.match(cell)]
        if not hit:
            return ''
        roles[hit[0]] = role
    rest = [index for index, cell in enumerate(cells) if roles[index] is None]
    if len(rest) != 1:
        return ''
    value = cells[rest[0]]
    return value if looks_like_brand_value(value) else ''


def brand_from_source_line(line, model='', category='') -> str:
    """从**一条原文行**里读出这一行自己写着的品牌；读不出来返回 ``''``。

    为什么需要它（``sample-d`` 真跑实测，``proposed_by='text_scan'`` 的 10 条候选品牌
    全空、整批被"品牌未识别"挡在正式候选之外）：这一轮的候选全部由正文直读产生，
    而直读当初按设计不抽品牌；可原文行里就写着品牌::

        台式常温高速离心机 | 湘智 | G21000 | 2台 | 12,400.00元

    同一行的表头列序是 ``名称 品牌 规格型号 数量 单价``，品牌就在第二格里。同一台机器
    在模型提出候选的那一轮（``sample-a-rerun2``）写的正是 ``brand='湘智'``。

    三条**只读这一行**的路径，依次尝试：

    1. 列名路径：行里同时有表头列名序列与数据格时，列名给出"品牌"的列号；
    2. 标签路径：``品牌：湘智`` 这种就地写明的标签；
    3. 形状路径：型号格、名称格（含品类词）、数量格、单价格都认领之后恰好只剩一格。

    三条路径的产出都要过 :func:`looks_like_brand_value`，并且是从这一行里**逐字切出来**
    的——不从别处借、不按常识补。行里没写品牌就返回空串；空的候选照旧进不了正式候选
    （品牌判据一个字没改，见 ``research_planning.diverse_candidates``）。

    别名归一不在这里做：那是 :func:`brand_identity` 的事，下游（``diverse_candidates``
    / 报告）都走它；这一层只负责"读到原文里写的那个写法"。
    """
    cells = row_cells(line)
    if len(cells) < 3:
        return ''
    if any(noise in line for noise in BRAND_NOISE):
        return ''
    for candidate in (_brand_cell_from_header(cells), _brand_from_inline_label(line)):
        if candidate:
            return candidate
    return _brand_cell_by_shape(cells, model, category)


def contains_model(text: str, model: str, max_depth: int = 3) -> bool:
    """检查文本中是否包含型号

    规则：
    1. 使用单词边界，不匹配子串
    2. 支持型号中的空格和连字符变体
    3. 大小写不敏感
    4. 递归深度限制防止栈溢出

    Args:
        text: 待搜索文本
        model: 目标型号
        max_depth: 递归深度限制

    Raises:
        ValueError: 输入无效
    """
    if max_depth <= 0:
        return False

    if not isinstance(text, str):
        raise ValueError(f"text must be string, got {type(text)}")

    if not isinstance(model, str) or not model.strip():
        raise ValueError("model cannot be empty")

    try:
        token = identity(model)
    except ValueError:
        return False

    normalized_text = unicodedata.normalize('NFKC', text)

    # 构建正则：左右边界卡字母数字**与连字符**，字符间可选空格/连字符。
    # 边界必须把 `-` 一起卡住：否则 ``SPX-250B-ZII`` 里的 ``SPX-250B`` 会被当成命中
    # ——`-` 落到"字符间可选分隔符"里，`-ZII` 那个后缀就看不见了，而 `-Z`／`-ZII`
    # 是博迅另外两个型号（docs/185 §2.1）。镜像方向 ``BSC-1300A2`` 里的 ``1300A2``
    # 同理（左边紧邻 `-`）。
    # 只卡**边界**：字符之间的分隔符一个不动，``Cary 60``／``Avanti J-15R``／
    # ``H-1850R``／``1300-A2`` 这些写法必须继续命中（docs/185 §3.1：311 对里 173 对
    # 的目标型号自己就含空格或连字符，那不是缺陷，是规范写法）。
    try:
        pattern = (
            r"(?<![A-Za-z0-9\-‐‑‒–—―−])" +
            r"[\s\-‐‑‒–—―−]*".join(map(re.escape, token)) +
            r"(?![A-Za-z0-9\-‐‑‒–—―−])"
        )
        return bool(re.search(pattern, normalized_text, re.I))
    except re.error as e:
        raise ValueError(f"invalid regex pattern for model {model!r}: {e}")


def supported(excerpt: str, body: str) -> bool:
    """检查摘录是否被正文支持

    规则：
    1. 标准化后的摘录必须是正文的子串
    2. 忽略空白差异
    """
    if not isinstance(excerpt, str) or not isinstance(body, str):
        return False

    norm = lambda value: re.sub(r"\s+", "", unicodedata.normalize("NFKC", value))

    normalized_excerpt = norm(excerpt)
    if not normalized_excerpt:
        return False

    return normalized_excerpt in norm(body)


# ============================================================================
# 阶段2：数值处理
# ============================================================================

def amount(value) -> float | None:
    """安全解析金额

    Returns:
        非负有限数值，或None表示无效

    注意：
        - 拒绝布尔值（True会被转为1.0）
        - 字符串会被尝试转换
    """
    # 拒绝布尔值
    if isinstance(value, bool):
        return None

    try:
        number = float(value)
        if not math.isfinite(number):
            return None
        if number < 0:
            return None
        return number
    except (TypeError, ValueError):
        return None


# ============================================================================
# 阶段3：参数验证（最复杂的业务逻辑）
# ============================================================================

def grounded_parameter(
    observed: dict,
    source: dict,
    model: str,
    name: str,
    *,
    allow_unverified: bool = False,
    max_recursion: int = 5
) -> bool:
    """验证参数是否有可靠证据支持

    这是采购规则的核心函数，确保：
    1. 参数值来自官方来源
    2. 摘录包含型号和参数名
    3. 值在摘录中可定位
    4. 单位匹配

    Args:
        observed: 观察到的参数值
        source: 来源文档
        model: 产品型号
        name: 参数名称
        allow_unverified: 是否允许未验证来源
        max_recursion: 递归深度限制

    Returns:
        True表示证据可靠，False表示不可靠
    """
    if max_recursion <= 0:
        return False

    # 输入校验
    if not isinstance(observed, dict):
        raise ValueError("observed must be dict")
    if not isinstance(source, dict):
        raise ValueError("source must be dict")
    if not isinstance(model, str) or not model.strip():
        raise ValueError("model cannot be empty")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("name cannot be empty")

    # 来源类型校验
    verified_source = source.get('source_type') in {
        'manufacturer_website',
        'manufacturer_download'
    }
    provisional_source = (
        source.get('source_type') == 'unknown' and
        source.get('purpose') != 'tender'
    )

    if not (verified_source or (allow_unverified and provisional_source)):
        return False

    # 正文必须已获取
    if source.get('body_status') != 'fetched':
        return False

    body_text = source.get('body_text', '')
    if not body_text:
        return False

    # 摘录校验
    excerpt = str(observed.get('excerpt', ''))
    if not excerpt.strip():
        return False

    if not supported(excerpt, body_text):
        return False

    if not contains_model(excerpt, model):
        return False

    # 参数名校验（支持别名）
    condition = str(observed.get('conditions', ''))
    label = name.replace(condition, '').strip() if condition else name

    aliases = {
        '控温精度': ('温控精度', '温度精度'),
        '温度设置范围': ('温度范围', '控温范围'),
        '最高转速': ('最大转速',),
        '最大相对离心力': ('最大离心力',),
    }

    labels = list(dict.fromkeys([name, label, *aliases.get(label, ())]))
    matched = next((s for s in labels if s and s in excerpt), None)

    if not matched:
        return False

    # 表格列投影处理（递归）
    projections = [
        line for line in body_text.splitlines()
        if '[表格按列展开' in line
        and contains_model(line, model)
        and matched in line
    ]

    if projections and excerpt not in projections:
        # 递归检查每个投影
        for line in projections:
            if grounded_parameter(
                {**observed, 'excerpt': line},
                {**source, 'body_text': line},
                model,
                name,
                allow_unverified=allow_unverified,
                max_recursion=max_recursion - 1
            ):
                return True
        return False

    # 标准化摘录
    excerpt = unicodedata.normalize('NFKC', excerpt)

    # 多产品行校验
    parameter_line = next(
        (line for line in excerpt.splitlines() if matched in line),
        excerpt
    )

    if '[表格按列展开' in parameter_line:
        if not contains_model(parameter_line, model):
            return False
        excerpt = parameter_line

    # 型号位置校验
    model_tokens = re.findall(
        r'(?<![A-Za-z0-9])(?=[A-Za-z0-9-]*[A-Za-z])(?=[A-Za-z0-9-]*\d)[A-Za-z0-9][A-Za-z0-9-]*(?![A-Za-z0-9])',
        excerpt.split(matched, 1)[0]
    )

    if model_tokens:
        try:
            if identity(model_tokens[-1]) != identity(model):
                prefix = excerpt.split(matched, 1)[0]
                last_end = prefix.rfind(model_tokens[-1]) + len(model_tokens[-1])
                anchored = (
                    r'(?<![A-Za-z0-9])' +
                    r'[\s‐‑–—-]*'.join(map(re.escape, identity(model))) +
                    r'$'
                )
                if not re.search(anchored, prefix[:last_end], re.I):
                    return False
        except ValueError:
            return False

    # 值校验
    values = observed.get('value')
    if not isinstance(values, list):
        values = [values]

    tail = re.split(
        r'[，；;\n。]|,(?!\d{3}(?:\D|$))',
        excerpt.split(matched, 1)[-1],
        maxsplit=1
    )[0]

    # 条件处理
    if condition and condition not in matched:
        if condition in tail:
            tail = tail.split(condition, 1)[-1]
        elif not excerpt.split(matched, 1)[0].rstrip().endswith(condition):
            return False

    # 数值提取
    numbers = [
        float(n.replace(',', ''))
        for n in re.findall(r'(?<![A-Za-z0-9.])-?\d[\d,]*(?:\.\d+)?(?![\d.])', tail)
    ]

    # 值匹配校验
    for value in values:
        if isinstance(value, bool):
            continue

        if isinstance(value, (int, float, str)) and re.fullmatch(r'-?\d+(?:\.\d+)?', str(value)):
            try:
                number = float(value)
                if not math.isfinite(number):
                    return False
                if number not in numbers[:len(values)]:
                    return False
            except ValueError:
                return False
        elif value is None or not supported(str(value), tail):
            return False

    # 单位校验
    unit = unicodedata.normalize('NFKC', str(observed.get('unit', '')))
    if unit:
        pattern = r'(?<![A-Za-z])' + re.escape(unit) + r'(?![A-Za-z])'
        if not re.search(pattern, tail, re.I):
            return False

    # 条件完整性校验
    if observed.get('conditions') and not supported(observed['conditions'], excerpt):
        return False

    return True


__all__ = [
    'identity',
    'brand_identity',
    'brand_from_enterprise_name',
    'enterprise_names_in_text',
    'contains_model',
    'supported',
    'amount',
    'grounded_parameter',
]
