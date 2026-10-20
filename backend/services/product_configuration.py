"""厂家产品参数页：与「采购需求」并列、但性质完全不同的第二条证据。

为什么需要它
------------
政府采购链路里有三种信息，混起来就会造出一台不存在的机器：

1. **招标文件**（采购方发布）只写**采购需求**：「第4包 血型血清学离心机」+ 13 条
   技术要求（专用红血球清洗/SERO 转子、微机控制液晶显示、符合 ISO13485……）。
   这时候还没有型号，这些条款是**采购方对投标产品开出的门槛**。
2. **中标公告**（中标后发布）只写**型号、品牌、数量、单价**，从来不写技术参数。
3. **厂家／产品页**写**这个型号自己的标称参数**（XKA-2200：最高转速 4500r/min、
   转子 HLA/SERO、最大相对离心力 2000×g、外形尺寸 456×330×266mm、重量 12kg）。

所以价格记录里的 ``configuration`` 原本总是空的：它只能来自中标公告和公告自己的
附件，而公告从不写参数。用户看到的就是「配置那一格空着」，旁边却并排着一块采购
要求。这里补上第 3 种信息，并且**只补第 3 种**：采购需求一律不进 ``configuration``
（那会把采购方的门槛印成这台机器的标称参数，是凭空捏造；采购需求的去处是
``backend.services.tender_requirements`` 里那个独立字段）。

判据（全是结构信号，缺一即拒）
------------------------------
下面每一条都在真实运行上核过（冷跑与热跑各一轮）：

1. 来源已取得正文；
2. 正文里出现目标型号，用的是 ``procurement_rules.contains_model``（完整型号
   token，不是模糊相似、更不是子串）；
3. **不是政府采购公告或招标文件**：来源类型不是 ``tender_document``，
   ``source_authority.is_tender`` 判定它的正文不是正式公告，而且
   ``research_sources.attachment_document_type`` 没有把它自己认成招标文件；
4. **（2026-09-26 已撤销）主机不是聚合站／卖场**——这一条原本写成
   ``is_marketplace_or_aggregator``：``120shop.cn``、``bidcenter.com.cn``、
   ``amazon.`` 这类域名一律跳过。它是**价格通道**的门，不是配置的门，撤销的理由
   见下面"第 4 条为什么撤销"。编号留空不重排：正文与代码里有九处引用「第 5 条」
   「第 6 条」，重排一次就要同步改九处，收益为零、漏一处就自相矛盾；
5. 正文里有**独占一行的产品参数标题**（``PRODUCT_SPEC_HEADINGS``，取法是
   ``_spec_headings``）；**或者**正文／窗口自己就是一张「字段名 | 值」参数表
   （``_spec_field_table``，见下面"第 5 条的第二种形态"）；
6. ``configuration_regions`` 取到的窗口里，至少有一个窗口**自己就写着目标型号**。

第 5 条为什么用子集：``CONFIGURATION_HEADINGS`` 是给**附件**用的，它同时收着
采购方口径的标题——「采购需求」「技术要求」「货物需求」「设备清单」「主要标的」
「配置清单」。实测那条 ccgp 中标公告（4456 字）里就有「主要标的」（表头
「主要标的信息」），只按共享词表取窗口，公告正文会被当成一台机器的参数页。产品
参数的标题只有「主要技术参数／技术参数／技术规格／规格参数」这一族，其余全部
剔掉。窗口怎么取仍然只有一份实现（``configuration_regions``），这里只收窄
「什么算产品参数标题」。

第 5 条为什么还要「独占一行」：招标文件的参数章节标题是「1包 技术参数要求」，
正文里还有「按照合同中的技术参数要求逐条验收合格」。按子串匹配这两种都会命中。
实测两个真实运行的全部 108 个来源：子串匹配命中 25 个，独占一行只剩 17 个，
四个某型号产品页一个不少。

第 5 条的第二种形态（「字段名 | 值」参数表）：有一类厂家页的参数区标题写的是
「产品参数」，而它在正文里是**页面内的标签导航**，不是标题——那一行长这样：
``产品参数 [某厂商的产品页]``，与 ``产品介绍``／
``转子附表``／``应用案例``／``合作客户`` 排在一起、指向同一个 URL。
所以这里**不**去剥行尾的 ``[http://…]`` 尾巴、也**不**把「产品参数」收进白名单：
实测全库 1137 个来源、36 万行，剥掉链接尾巴后再比对词表，新命中的 15 行
（去重后 6 个来源）**全部**是这种标签导航条（``技术参数`` 9 处／``产品参数`` 6 处），
没有一行是真标题——认它等于把链接当标题，与上一段"独占一行"的取舍正好相反。
改看**结构**：一段文本里成片出现「字段名 | 值」两格行，字段名是短的（≤
``SPEC_FIELD_MAX``），值是短的（≤ ``SPEC_VALUE_MAX``）**并且是一个量**——以数字／
``±``／``≤``／负号／``×`` 开头，或者这一格就是这个型号。导航条、页脚、目录页
没有这种行；公告的成交标的表是多格表（``序号 | 标的名称 | 品牌 | 规格型号 | …``），
第 1 格是序号不是字段名，也落不进来。"值必须是量"那一半是拿真实反例换来的：
高校共享仪器平台的设备页写成 ``型号： | Lambda 750S``／``功能： | 190nm~2500nm
波长范围内薄膜样品吸光度(A)/反射率(R%)/透过率(T%)测试``／``工程师： | 付老师 /
(021)34206126-6010 / …``／``设备地点：``／``设备编号：``，只有"型号"一行是量值，
按"成片两格行"会把它当参数表，按"成片量值行 ≥ ``SPEC_FIELD_ROWS``"就挡住了。
判据落**两处**：整页先预筛一次（省掉取窗口的开销），真正写进 ``configuration``
的那段窗口上再确认一次——否则会出现"整页有参数表、留下的却是没有参数的那一段"。

第 6 条为什么必要：一个分类页会并列好几个型号的参数表，取窗口时只要标题附近
有型号就够了，拿错一张就是把**别的机器**的参数印到这台机器上。第 3 条与第 5 条
也不是冗余：实测安徽那份 47590 字的招标文件里「主要技术参数」独占一行，靠第 5 条
挡不住，是第 3 条把它拦下的。第 3 条里那三种写法各管一段：来源类型由抓取阶段
判定，正式公告由发布者等级判定，而「这份正文自己说它是招标文件」用的是
``attachment_document_type``（只读开头 8 个非空行，实测两个真实运行的 108 个
来源里它只认出 4 份，全部是真正的招标文件，没有一个产品页被误认）。

第 4 条为什么撤销（用户 2026-09-26 口径）
----------------------------------------
``is_marketplace_or_aggregator`` 是**价格通道**的门，不是配置的门。它自己的定义处
（``backend/services/source_authority.py:75-79``）逐字写着：这些域名"只当**发现渠道**
（拿线索），采信一律回到官方原文……也就没资格进价格通道——「报告里不得出现聚合站
转载作为价格来源」这条由 ``is_marketplace_or_aggregator`` 一处兜住"。通篇问的是
**「这笔成交价是不是真的」**。

配置要问的是另一件事：**「这段技术参数是谁写的、准不准」**。同一个域名，作为价格
来源不可信，不等于它挂的参数页上没有参数。用户 2026-09-26 的口径是「**如果是取配置
的来源 可以算 如果是取价格的来源 不能算**」。

名单本身也印证两边性质不同：``AGGREGATOR_HOST_MARKERS``
（``source_authority.py:126-136``）里 ``shihuantong.com``（世环通，环保／仪器行业
信息平台）、``120shop.cn``（八方资源网）、``caizhaowang.com``（中国采招网）这类是
**"把别处公告汇总起来"的招标信息站**，当价格来源不可信；其中一些**同时也挂产品
参数页**。整站挡在 ``configuration`` 之外，挡掉的是参数本身，不是价格的可靠性。

撤销的只有域名性质这一条。**红线一条没动**：``source_type == 'tender_document'``、
``is_tender(source)``、``attachment_document_type(body) == 'tender'`` 三条原样保留
（docstring 第 3 条），采购需求／招标文件绝不许变成配置。价格那一侧的 ``prices()``／
``republished_prices()``／``_unusable_non_local``／``PRICE_SOURCE_TYPES`` 一个字
不动，``is_marketplace_or_aggregator`` 函数本身也不动（价格还在用它）。

来源标签照旧分两档，本单没碰它的判定：能不能把一页认成"品牌方自己发的"，靠的是
下面的版权行＋公司名判据（``_COPYRIGHT``／``_COMPANY``）。第三方平台页只会记成
``PRODUCT_SOURCE``，不会被印成「厂家资料」。


找不到就返回 ``None``，调用方保持 ``configuration`` 为空——宁可 0 条，不用采购
需求或别的型号兜底。
"""
from __future__ import annotations

import re
import unicodedata

from backend.services.procurement_rules import brand_identity, contains_model, identity
from backend.services.research_sources import attachment_document_type
from backend.services.source_authority import host_of, is_tender

#: ``configuration_source`` 的取值：这段配置来自**产品资料**，不是中标公告写的。
#: 下游（前端、报告）据此把它与「成交公告里写的配置」分开，也据此在页面上注明
#: 出处。两档分开是因为它们的分量不同：前者是品牌方自己的页面，后者是产品库／
#: 第三方产品页——不能把第三方页面印成「厂家资料」。
MANUFACTURER_SOURCE = 'manufacturer_spec'
PRODUCT_SOURCE = 'product_page_spec'
SPEC_SOURCES = frozenset({MANUFACTURER_SOURCE, PRODUCT_SOURCE})

#: 产品参数类标题：``procurement_research.CONFIGURATION_HEADINGS`` 的子集。
#: 收窄的理由见模块 docstring 第 5 条。``tests/test_product_configuration.py``
#: 钉着「必须是子集」这条，共享词表改了这里会红。
PRODUCT_SPEC_HEADINGS = ('主要技术参数', '技术参数', '技术规格', '规格参数')

#: 与 ``procurement_research`` 的发布者核实用的是同一个形状：版权行 + 公司名。
#: 这一页的版权方公司名里带着品牌，说明这一页是品牌方自己发的，而不是经销商、
#: 门户或转载站。真实数据：``xiangyilxj.com``「Copyright © 湖南湘仪实验室仪器
#: 开发有限公司」= 厂家自有页；``xiangyiyt.com``「Copyright © 2012 - 上海仪天
#: 科学仪器有限公司」= 第三方展示平台（品牌是湘仪，公司名里没有湘仪）。
_COPYRIGHT = re.compile(r'(?:Copyright|版权所有|©)[^\n]{0,160}', re.I)
_COMPANY = re.compile(r'[\u4e00-\u9fff]{2,35}(?:有限公司|研究所)')

#: 标题行的前置编号（``一、``、``1.``、``（二）``、markdown 井号、项目符号）与
#: 尾部标点（``技术参数：``）。剥掉这两头之后，标题行必须**正好等于**参数标题。
_ENUMERATION = re.compile(r'^(?:[（(]?[一二三四五六七八九十\d]+[）)、.．]\s*)+')
_TRAILING = re.compile(r'[\s:：,，.。、;；]+$')

#: 第 5 条的第二种形态：「字段名 | 值」参数表。字段名这一格最多这么长。
#: 实测厂家页最长的一条是「最大相对离心力 Max RCF」（16 字符）、「转速精度
#: Speed Accuracy」（19 字符）——24 给中英文双写的字段名留余量。这一格一旦变长，
#: 就不再是字段名，而是正文句子（表格里也会出现长句子，见 ``_spec_field_rows``）。
SPEC_FIELD_MAX = 24
#: 「值」这一格最多这么长。参数值是量（``18600r/min``／``-40°C to -86°C``），
#: 不是说明句：实测高校共享仪器平台的设备页写成 ``功能： | 190nm~2500nm波长范围内
#: 薄膜样品吸光度(A)/反射率(R%)/透过率(T%)测试``（50+ 字），那是设备说明，不是参数。
SPEC_VALUE_MAX = 40
#: 至少这么多行**量值**才算一张参数表。孤零零一两行（``型号 | KH20A``）可能只是页面
#: 上的一个信息条目，成片才是表。这一条同时挡住共享仪器平台的设备台账页：那页的
#: 字段是「型号／功能／工程师／设备地点／设备编号」，量值只剩「型号」一行。
SPEC_FIELD_ROWS = 3
#: 值这一格以这些字符开头才算"量值"：数字、``±``（``±10r/min``）、``≤``（``≤60dB``）、
#: 负号（``-40°C``，放末尾才是字面量）、``×``（``4×750ml``）、直径号。管理信息
#: （人名、地点、编号、说明句）不以量值开头——这一条是把"设备台账"与"产品参数"
#: 分开的那把尺子。
_VALUE_HEAD = re.compile(r'^[\d±≤≥<>~×⌀Φφ-]')

#: 「字段名：值」形态的参数行里，字段名这一格最多这么长（与 ``SPEC_FIELD_MAX``
#: 同一把尺子，只是分隔符从 ``|`` 换成冒号）。有些厂家页的参数表不是网格，
#: 而是普通行——实测 ``bjpersee.com`` 的 TU-1901 写成 ``1、 波长范围： 190nm～900nm``。
SPEC_PAIR_MAX = SPEC_FIELD_MAX
#: 报告里至少要有这么多行参数才认为是"压缩成功"。只压出一行时信息量比原来
#: 直接截参数区还少（见 ``compressed_configuration`` 的保底），退回原来的截法。
SPEC_REPORT_MIN_ROWS = 2
#: 多格行的上限：格数与行长。超过就说明"这一行"其实是**整张表挤成一行**
#: （排版丢了换行），不是一行参数。实测库里已有配置的多格行：真参数表最多
#: 10 格、230 字符（``型 号 Model | 型 号 Model | 工作室尺寸 …``），
#: 而 ``某聚合站的产品页`` 那页把整张多型号表写在 1006 个
#: 字符、31 个格的一行里（``型号 | SPX-100B-ZII | … 控温范围 | 0-60℃ | …``），
#: 格与格之间夹着另一张表的行——收进报告就是一段没有标点的流水账。
SPEC_GRID_CELLS = 12
SPEC_GRID_WIDTH = 300
#: 联系方式类字段名：这些不是产品参数，是页脚的客服信息。实测 ``maiyivip.com``
#: 那页写 ``固话：021-80122208``／``传真：021-63450333``／``Q Q：800072208``，
#: ``lxjxy.com`` 那页写 ``营销中心电话：0731-82842826``。形状与"字段名：量"
#: 一模一样（短字段名 + 数字值），只能按字段名的词挡。这是**启发式**，不是判据
#: ——产品参数里也可能出现这些字（例如"工作环境温度：5-30℃"不含这些词，
#: 但"报警方式：电话"就会误伤），所以词表尽量窄。
_CONTACT = re.compile(
    r'(?:电话|固话|传真|热线|邮箱|邮件|邮编|地址|微信|客服|手机|来电|联系|对话'
    r'|\bWeb\b|\bwww\b|\bE-?mail\b|\bTel\b|\bFax\b)', re.I)
#: 页面元信息类字段名：``更新时间： 2026-08-03``（``ybzhan.cn``）、``发布日期``、
#: ``浏览量`` 这些是页面自己的元数据，不是产品参数，但形状与"字段名：量"一样
#: （短字段名 + 日期/数字）。同样按字段名的词挡。
_META = re.compile(
    r'(?:更新时间|发布日期|发布时间|浏览次数|浏览量|点击量|访问量'
    r'|\bmodi(?:fi|ﬁ)ed[\s-]*date\b|\bversion\b|\bdocument\s+code\b'
    r'|\bdoc(?:ument)?\s*no\b|\bdedicated\s+code\b'
    r'|\bcatalog(?:ue)?\s*(?:no|number)\b)', re.I)
#: 公告流程类字段名：``项目编号：``／``采购人：``／``成交供应商：``／``中标金额：``
#: 这些是**公告**的字段，不是产品参数，形状却与「字段名：值」完全一样。
#: 它们也是"反例"要挡的那一类：省级成交公告结构上不印参数表，但它的字段行
#: 能一条条通过参数行判据，报告里就会把公告的字段当成产品参数印出来。
#: 词表只收整词、只收公告特有的这几个，避免误伤真实的"项目"类参数名。
_DOCUMENT_FIELD = re.compile(r'(?:项目编号|采购人|采购单位|成交供应商|中标供应商|成交金额|中标金额|成交公告|招标公告|公告期限|评审专家)')

#: 自推"别的型号"时，候选写法与目标型号**归一化后**至少要共享这么长的前缀。
#:
#: 取 **3**，三条理由都对着真实记录：
#:
#: * ``SPX-70BⅢ``（归一化 ``spx70biii``）与 ``SPX-150BIII``（``spx150biii``）共享
#:   ``spx``——这正是厂商标号段；再短一位（``sp``）会把同厂别的系列也算进来；
#: * ``Master-S30``（``masters30``）与 ``Master-S15``（``masters15``）共享
#:   ``masters``，``BDW-86L390`` 与 ``BDW-86L490`` 共享 ``bdw86l``；
#: * **短型号直接推不出来**：``Q20``（``q20``）与 ``Q10``／``Q30`` 只共享 ``q``，
#:   一律不裁。``tests/test_reliability_development.py:21`` 那组
#:   ``Q20``／``Q10``／``Q30`` 用例正属于这一类；两字符型号在真实语料里只出现在
#:   单型号页（``KH20A``／``X100``），不裁没有代价。
#:
#: 这一档是"宁可漏裁"：漏裁的结果是报告里多几行别的型号，误裁的结果是目标型号的
#: 参数整行消失。真实记录上的效果见 。
MODEL_SIBLING_PREFIX = 3

#: 报告正文里那一行的上限。
#:
#: 为什么从 200 改成 400（实测）：主隔离库的 18 个 research run × 各自目标型号，
#: 拿**已有来源**复算 ``product_configuration`` 得到 27 条真实配置；其中 2 条压不出
#: 参数行（走保底），下表按 25 条算。
#: ------------------------------------------------------------------
#: 数的是"报告那一行里装进了几行**完整**参数行"（同行边界装，见
#: ``_fit_lines``），同一把判据（``_parameter_line``）数修前与修后：
#:
#: * 200 字：修前中位数 6 行／合计 141，修后中位数 7 行／合计 198
#:   （13 条变多、14 条持平、**0 条变少**）；
#: * **400 字：修前中位数 9 行／合计 254，修后中位数 10 行／合计 322**
#:   （15 条变多、12 条持平、0 条变少）；
#: * 600 字：修后中位数 14 行／合计 388——比 400 再多 66 行，代价是报告那一行
#:   从约 4 行屏幕文字涨到约 6 行。
#:
#: 取 400 的第二个理由是"整表装完"：25 条压缩版长度中位数 564 字，
#: ≤200 字能整表装完 3 条、≤300 字 4 条、**≤400 字 9 条**、≤600 字 13 条。
#: 400 是第一个把那张 14 行主参数表装完的档位（564 字的压缩版里
#: 装到第 17 行，主参数表全进）；300 字时它差 2 行。再往上到 600 多出来的
#: 都是**转子附表**这类二级表，而同一条报告行后面紧跟着单价、日期与缺口标签，
#: 再长就把价格本身淹了。
#:
#: 截断按**行**切（见 ``_fit_lines``），不按字符切：报告里的配置是证据，
#: 印出 ``整机功率 | 1.6`` 这种被削掉一半的数值比少印一行更糟。
REPORT_CHARACTER_LIMIT = 400


# ---------------------------------------------------------------------------
# 窗口正文清洗：去掉页面框架内容、去掉被投影重复了一遍的表格行
# ---------------------------------------------------------------------------
#: ``web_tables.render_table`` 追加的行（``web_tables.py:89``）。它是**同一张表的
#: 第二种写法**：第 1 格是型号的那些表会先被 ``output`` 原样输出（``row | row``），
#: 再把「型号列 × 数据格」逐格投影追加在后面。配置窗口取的是**抓下来整段正文里
#: 的 2400 字**，两次写法都在窗口里，于是同一份参数出现两遍。
_PROJECTION = re.compile(
    r'^\[表格按列展开\s*行(?P<row>\d+)列(?P<column>\d+)\]\s*(?P<body>[\s\S]*)$')
#: ``search_providers._clean_html`` 把 ``<a href>`` 展开成 ``label [href]``
#: （``search_providers.py:392-398``），href 与 label 不同行时就会单独占一行。
_LINK_ONLY = re.compile(r'^\[https?://[^\]\s]+\]$')


def _projection_parts(line):
    """一条 ``[表格按列展开 行r列c]`` 的 (行号, 列号, 正文段列表)；不是投影行返回 ``None``。

    只有表格**第 1 格是型号**时 ``render_table`` 才认得出型号表头（``web_tables.py:77``），
    认得出才投影。投影正文的写法固定是「型号 + 行标签 + 这一行前几格 + 那一格的值」
    （``web_tables.py:85-89`` 的 ``model / descriptor / row[c]``），但段数不定：
    2 列表是 ``型号 标签 值``（3 段），多列表把前几格拼进 descriptor。
    """
    match = _PROJECTION.match(line)
    if not match:
        return None
    parts = match.group('body').split()
    if len(parts) < 3:
        return None
    return int(match.group('row')), int(match.group('column')), parts


def _grid_row_cells(line):
    """一行表网格（``a | b | c``）的单元格列表；不是网格行返回 ``None``。

    ``render_table`` 输出的是**原始网格行**（``web_tables.py:74``）：第 1 格是行标签
    （2 列表里就是参数名），后面每一格对应一个型号列。所以 2 列表的行只有 2 格
    （``最高转速 | 4500r/min``）——这是最常见的一种，判据不能要求 3 格以上。
    """
    if '|' not in line:
        return None
    cells = [cell.strip() for cell in line.split('|')]
    if len(cells) < 2 or not cells[0] or not cells[-1]:
        return None
    return cells


#: 投影行里 ``行N`` 与实际网格行之间的最大容差（行）。
#: ``render_table`` 是在整张 ``<table>`` 上数行的（``web_tables.py:53``），而"行"里
#: 混着页面自己的行——它只投影"有值、且第 1 格不是配件/转子"的行（``web_tables.py:81``），
#: 所以偏差不是常数：实测那个厂家页前面差 2 行、从 ``行8`` 起差 1 行，同一张表里漂移。
#: 容差只决定"跟哪一行比"，内容本身仍要逐字对齐才删，所以放宽一点不会变成误删。
PROJECTION_ROW_TOLERANCE = 3


def _projection_repeats_grid(parts, grid_lines):
    """这条投影的内容是不是已经由某一行网格承载了。

    对齐方式（不靠精确行号）：在投影声明的行附近（容差 ``PROJECTION_ROW_TOLERANCE`` 行内）
    找网格行，再逐格比内容（见 ``_cell_matches``）。``行N`` 的偏差不是常数（见上面
    ``PROJECTION_ROW_TOLERANCE`` 的注释），所以只能靠内容对齐；容差只决定"跟哪一行比"，
    内容仍要逐字相等才删，对不上的投影一律留着。

    ``grid_lines`` 是 ``{行号: 该行单元格}``。
    """
    for offset in range(-PROJECTION_ROW_TOLERANCE, PROJECTION_ROW_TOLERANCE + 1):
        row = grid_lines.get(parts[0] + offset)
        if row and _cell_matches(parts, row):
            return True
    return False


def _cell_matches(parts, row):
    """一条投影与一行网格是否是同一格内容的两种写法。

    投影正文是「型号 + 这一行前几格 + 那一格的值」（``web_tables.py:85-89`` 的
    ``model`` / ``descriptor`` / ``row[c]``），判据两条：

    ① **值**：网格第 ``c`` 格的内容是投影正文里连续的一段（``_run_position`` 按词比）；
    ② **位置**：这一段之前还有几个词，正好等于网格行里同一位置的格数（见下）。

    ② 不能省：多型号表同一行有两格值不同（实测 4500/min 与 4000/min），只比"值相同"
    会让两条投影都挂到同一格上。带位置之后两条各自只在自己那一列成立，都保留。

    ① 按连续一段而不是整格相等：单元格的值自己带空格（实测 ``AC220V ±22V 50/60Hz``、
    ``最低 ≤58dB(A)``），``render_table`` 把整格拼进字符串，切词之后跨好几个 token。

    **不能按 ``行N`` 的行号对齐**：那个数字数的是 ``<table>`` 里的原始行，而"行"里混着
    页面自己的行（``render_table`` 只投影有值、且第 1 格不是配件/转子的行，
    ``web_tables.py:81``），所以它会整段地错开——实测那个厂家页从 ``行8`` 起差 1 行、
    前面差 2 行，同一张表里漂移。只能靠内容对齐（见 ``PROJECTION_ROW_TOLERANCE``）。

    ``列c`` 与网格格号的基准还有一处错位，只在窗口把表格截断时出现：``render_table``
    按原始列号写 ``列c``（``web_tables.py:79``），而配置窗口可能只剩 ``型号 | X`` 两格
    （实测那页投影仍写 ``列2``，事实上是最后一格）。所以宽度够时按 ``row[c-1]``，
    宽度不够时按最后一格。
    """
    _, column, words = parts
    if len(words) < 3:
        return False
    cell = column - 1 if column <= len(row) else len(row) - 1
    if not 1 <= cell <= len(row) - 1:
        return False
    position = _run_position(words, row[cell].split())
    if position is None:
        return False
    # 正文的这一格前面只有型号 + 网格第 1..c-1 格；型号占一个词，所以位置必须是
    # （前面几格的总词数）。多一格少一格都说明这一格跟别的位置对上了，不能删。
    return position == len(words) - len(row[cell].split())


def _run_position(words, cell_words):
    """``cell_words`` 作为 ``words`` 的连续一段，其起始下标；没有这样的段返回 ``None``。

    按词比、要求连续，用来把"投影里写的那一格"和"网格里的那一格"对上。
    """
    if not cell_words:
        return None
    length = len(cell_words)
    for index in range(len(words) - length + 1):
        if words[index:index + length] == cell_words:
            return index
    return None


def without_repeated_table_projection(text):
    """去掉与上面那张表逐格重复的投影行；没重复的一律保留。

    判据是**内容重复**，不是格式：投影行说的是"第 r 行第 c 格是什么"，而 ``render_table``
    已经把那张表的每一行原样输出过了。逐格对齐（见 ``_projection_repeats_grid``）之后：

    * 多型号表（``xiangyiyt.com`` 那张 3 列表：同一行投影出 ``列2``/``列3`` 两条，
      值不同）两条都保留——各自对不上，删了就真丢了；
    * 型号列缺一格（``行5`` 是空值）时 ``render_table`` 少投影一条，也对不齐，
      那条同样保留；
    * 没有表网格的正文（整段没有 ``|``）**一条都不删**——没有第二份可对照，
      投影就是唯一一份。

    返回 ``(清洗后的正文, 删掉的行数)``。
    """
    lines = text.splitlines()
    grid_lines = {}
    for number, line in enumerate(lines):
        cells = _grid_row_cells(line.strip())
        if cells:
            grid_lines[number] = cells
    kept, dropped = [], 0
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        parts = _projection_parts(stripped)
        if parts is None or not _projection_repeats_grid(parts, grid_lines):
            kept.append(line)
            continue
        dropped += 1
    return '\n'.join(kept), dropped


def strip_page_chrome(text):
    """去掉产品页的导航／推荐位行，其余原样保留。

    这些内容不是这家公司的发明：``_clean_html`` 会把每个 ``<a>`` 展开成
    ``label [href]``，产品页的页头页脚和「相关推荐」轮播因此变成**一片裸链接行**
    加一串**产品名行**。两类都按结构删，不按词表：

    1. **纯链接行**——整行只有一个 ``[http://…]``，一个字的信息都没有，界面上的
       表现就是一串网址（用户截图里的那批）。参数行永远不会长这样：参数行的值是
       ``4500r/min`` 这种，链接即使作为某一格出现，也不会是**整行**。
    2. **推荐位成对行**——「产品名行 + 紧跟一条纯链接行」连续成片（≥3 对）。产品名
       行文字上确实像型号（实测就是 ``H300R大容量高速冷冻离心机``），但它在这份
       正文里的作用是**别处的链接文字**，而且成片出现正是轮播列表的形状；孤立的
       一行名+链接（例如「查看厂家资料原文 [url]」）不动。
    3. **短行块**——紧挨着那片推荐位的几条极短行（``留言板``／``发 送``／``相关推荐``）。
       判据是结构：一条或连续几条 2~4 个字的行（没有数字、没有分隔符号、不是参数标题），
       **紧接着**就是推荐位。整块去掉——块里全是模板文字，没有一行是参数；孤立的短行
       （例如 ``转子参数`` 这种下面接着参数表的组标题）不动，因为它后面不是推荐位。

       这一条是三条里说服力最弱的：它靠长度与相邻关系，不靠内容。``留言板``／``发 送``
       留在正文里的代价只是多两行噪声、不会误报成参数，所以宁可按保守的写法写。

    返回 ``(清洗后的正文, 删掉的行数)``。
    """
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return text, 0
    stripped = [line.strip() for line in lines]

    def short(body):
        """极短、且没有任何参数特征的一行：只可能是模板上的标题或按钮文字。"""
        return (2 <= len(body) <= 4 and not re.search(r'[\d|/×%℃]', body)
                and _heading_of(body) is None)

    paired = set()
    starts = []
    index = 0
    while index < len(stripped) - 1:
        if _LINK_ONLY.match(stripped[index + 1]) and '|' not in stripped[index] and stripped[index]:
            run = [index]
            cursor = index + 2
            while (cursor < len(stripped) - 1 and '|' not in stripped[cursor]
                   and stripped[cursor] and _LINK_ONLY.match(stripped[cursor + 1])):
                run.append(cursor)
                cursor += 2
            if len(run) >= 3:
                for position in run:
                    paired.add(position)
                    paired.add(position + 1)
                starts.append(index)
            index = cursor
        else:
            index += 1

    # 紧挨着推荐位的那几条短行（``留言板``／``发 送``／``相关推荐``）整块去掉。
    # 孤立的短行不动——``转子参数`` 这种组标题后面接的是参数表，不是推荐位。
    for start in starts:
        head = start - 1
        while head >= 0 and short(stripped[head]):
            paired.add(head)
            head -= 1

    kept, dropped = [], 0
    for number, line in enumerate(lines):
        body = stripped[number]
        if number in paired:
            # 推荐位的产品名行与它下面那条纯链接行，以及紧挨着推荐位的短行块
            # （见 docstring 第 2、3 条）。
            dropped += 1
        else:
            kept.append(line)
    return '\n'.join(kept), dropped


def clean_page_text(text):
    """配置窗口正文的清洗：页面框架内容 + 重复的表格投影。

    两个判据合起来只作用于**配置清单这一路**（本模块是它的唯一产出点），
    正文原文、检索线、价格判据都不经过这里。删掉的行数用 ``dropped`` 汇报，
    便于在真机上核对"删了什么"。
    """
    if not isinstance(text, str) or not text.strip():
        return text, 0
    text, chrome = strip_page_chrome(text)
    text, repeated = without_repeated_table_projection(text)
    return text, chrome + repeated


def _heading_of(line):
    """这一行是不是一个「独占一行的参数标题」；是就返回标题，不是返回 ``None``。"""
    stripped = _TRAILING.sub('', _ENUMERATION.sub('', line.strip().strip('#*->• \t')))
    return stripped if stripped in PRODUCT_SPEC_HEADINGS else None


def _spec_headings(body):
    """正文里独占一行的参数标题 ``(具体程度, 行号, 标题)``。

    为什么要求独占一行，而不是「正文里出现这四个词」：招标文件的技术要求章节
    标题就是「1包 技术参数要求」，正文里还到处写着「按照合同中的技术参数要求
    逐条验收合格」。按子串匹配，这两种写法都会命中，而它们一个字也不是这台机器
    的参数。实测两个真实运行的全部 108 个来源：子串匹配命中 25 个，独占一行只剩
    17 个——四个 XKA-2200 产品页全部留下，被剔掉的是导航链接（``技术参数及彩页
    下载``）、问答句（``有……详细技术参数吗？``）、招标文件的「技术参数要求」，
    以及一个不足页面的召回损失：``ccen.net`` 那个标题行写成
    「XZ21K-T 高速冷冻离心机技术参数表」，按这条判据取不到（它也不是本次型号）。
    """
    hits = []
    for number, line in enumerate(body.splitlines()):
        heading = _heading_of(line)
        if heading:
            hits.append((PRODUCT_SPEC_HEADINGS.index(heading), number, heading))
    return sorted(hits)


def _spec_field_rows(text):
    """这段文本里「字段名 | 值」形状的行 ``(字段名, 值)``。

    两格（``a | b``）、字段名这一格非空且不超过 ``SPEC_FIELD_MAX`` 字符、值这一格
    非空且不超过 ``SPEC_VALUE_MAX`` 字符。多格表一律不算——公告的成交标的表
    （``序号 | 标的名称 | 品牌 | 规格型号 | …``）、多型号并列的尺寸表都是多格，
    而且第 1 格是序号或型号，不是字段名。
    """
    rows = []
    for line in text.splitlines():
        cells = _grid_row_cells(line)
        if not cells or len(cells) != 2:
            continue
        left, right = cells[0], cells[1]
        if not left or not right:
            continue
        if len(left) <= SPEC_FIELD_MAX and len(right) <= SPEC_VALUE_MAX:
            rows.append((left, right))
    return rows


def _spec_measure_rows(text, model):
    """「字段名 | 值」里值是一个**量**的行：数字／``±``／``≤``／负号／``×`` 开头，
    或者这一格本身就是目标型号。"""
    return [(left, right) for left, right in _spec_field_rows(text)
            if _VALUE_HEAD.match(right) or contains_model(right, model)]


def _spec_field_table(text, model, minimum=SPEC_FIELD_ROWS):
    """这段文本自己就是一张产品参数表（第 5 条的第二种形态）。

    成片的「字段名 | 量值」，而且其中有一行的值**就是这个型号**（``型号 Model |
    KH20A``）。要求"值就是型号"而不是"这段文本里出现型号"：后者在分类页、导航、
    推荐语里到处都是，前者只在参数表里出现。写进 ``configuration`` 的正是这段文本，
    所以这一条同时管住了"别把参数页的导航段当配置"。
    """
    rows = _spec_measure_rows(text, model)
    if len(rows) < minimum:
        return False
    return any(contains_model(right, model) for _left, right in rows)


def _copyright_owner(body):
    """版权行里写的公司名；读不到返回空串。"""
    if not isinstance(body, str):
        return ''
    for match in _COPYRIGHT.finditer(body):
        found = _COMPANY.search(match.group())
        if found:
            return found.group()
    return ''


def _own_brand_page(source, brand):
    """这一页是不是品牌方自己发布的页面。"""
    if source.get('source_type') in {'manufacturer_website', 'manufacturer_download'}:
        return True
    relation = source.get('publisher_evidence')
    # 两侧都是品牌写法，走 ``brand_identity``（查别名表）；用 ``identity`` 时
    # ``天津泰斯特`` 与 ``泰斯特`` 判成两家，这条来源就掉一档（厂家自有页 → 产品页）。
    if brand and isinstance(relation, dict) and brand_identity(relation.get('brand')) == brand_identity(brand):
        return True
    owner = _copyright_owner(source.get('body_text'))
    # 子串包含**刻意保持 ``identity``**（2026-09-27 量测）：换成 ``brand_identity`` 后
    # ``湖南湘仪`` 缩成 ``湘仪``，实测把 ``某厂商官网的产品页``
    # （版权所有者「长沙高新开发区湘仪天平仪器设备有限公司」，做天平的另一家）判成
    # 「湖南湘仪」自己的页面——那是误放行；同时 ``申安``→``上海申安`` 会让两家
    # ``shenan.net`` 页面失去命中。分母 8088 个三元组里 4 条变化：1 条误放行、
    # 1 条正确放行、2 条失去命中，净收益不明，所以不动。
    return bool(brand and owner and identity(brand) in identity(owner))


def _rank(candidate):
    """厂家自有页优先；同一档里选「这段文本就是这个型号的参数」的那个。

    第二项是**证据强度**：独占一行的产品参数标题（``headings``）比「字段名 | 值」
    参数表硬，两者都在时选前者。少了这一维会出回归：新通路让一批没有标题的窗口
    也成了候选，其中型号密度高的（窗口短，密度自然高）会顶掉原来带标题的候选——
    实测 ``task-<run-11>``（``某型号``）就会从 ``one aggregator product page``
    （``技术参数``，4414 字）换成另一个没有标题的页面。

    第三项是**型号密度**（每千字出现几次），不是出现次数：整页带上导航栏的文本
    会把型号出现次数抬上去，同一段参数却被菜单、推荐位稀释。实测某型号：
    厂家官网那个 2400 字的「技术参数 + 转子参数」窗口密度 4.58，同一家公司另一个
    域名的整页（4464 字，含导航）密度 3.36，第三方展示平台整页 6.20 但不在厂家
    档。所以密度只在同一档内比较。
    """
    density = candidate['model_mentions'] / max(len(candidate['text']), 1) * 1000
    return (0 if candidate['manufacturer_owned'] else 1,
            0 if candidate['headings'] else 1,
            -density, len(candidate['text']))


def product_configuration(sources, model, brand=''):
    """这个型号的厂家产品参数；找不到返回 ``None``。

    返回的 dict 里 ``text`` 是参数正文，``url`` 是它的出处，``host``／
    ``source_type``／``manufacturer_owned`` 说明这一页的身份，
    ``headings`` 是命中的参数标题，``model_mentions`` 是这个型号在窗口里出现的
    次数。调用方必须把 ``url`` 一起存下来：离开出处的参数就是无源之谈。
    """
    if not isinstance(model, str) or not model.strip():
        return None
    from backend.services.procurement_research import configuration_regions
    best = None
    for source in sources or []:
        if not isinstance(source, dict) or source.get('body_status') != 'fetched':
            continue
        body = source.get('body_text')
        if not isinstance(body, str) or not body.strip():
            continue
        if source.get('source_type') == 'tender_document' or is_tender(source):
            continue
        if attachment_document_type(body) == 'tender':
            continue
        url = source.get('url')
        # 这里原本还有一条 `is_marketplace_or_aggregator(host_of(url))` 就跳过（第 4 条）。
        # 它是**价格通道**的门——``source_authority.py:75-79``：「只当发现渠道，采信一律
        # 回到官方原文……也就没资格进价格通道」——问的是"这笔成交价是不是真的"；配置
        # 问的是"这段参数是谁写的、准不准"。用户 2026-09-26 的口径：「如果是取配置的
        # 来源 可以算 如果是取价格的来源 不能算」，所以这一条撤销（详见模块 docstring
        # 「第 4 条为什么撤销」）。撤销的只有域名性质这一条：上面三条（来源类型、
        # `is_tender`、`attachment_document_type`）原样保留，招标文件仍然进不来。
        if not contains_model(body, model):
            continue
        headings = _spec_headings(body)
        # 第 5 条有两条通路（见模块 docstring）：独占一行的产品参数标题，或者窗口自己
        # 就是一张「字段名 | 值」参数表。后者先在这里做一次廉价预筛：整页里连成片的
        # 两格参数行都没有，就不必再取窗口了。
        if not headings and not _spec_field_table(body, model):
            continue
        # 只保留自己就写着这个型号的窗口：同页并列多个型号时，取错一张就是把
        # 别的机器的参数挂到这台机器上。
        regions = [region for region in configuration_regions(body, model)
                   if contains_model(region.get('text') or '', model)]
        if not regions:
            continue
        window = '\n'.join(region['text'] for region in regions).strip()
        # 预筛是在整页上做的，而写进 ``configuration`` 的是窗口这一段：万一张
        # 参数表里的一行型号字段落在窗口外，留下的就是一段没有参数的页面正文。
        # 所以第二通路要在窗口本身上再确认一次。
        if not headings and not _spec_field_table(window, model):
            continue
        text = window
        # 清洗只作用在这里：``configuration`` 由本函数产出，正文原文与价格判据都
        # 不经过这一段。``model_mentions`` 仍在**清洗前**的窗口上数（见下），
        # 否则"型号密度"会被清洗改变，``_rank`` 的档内比较就不是同一把尺子。
        text, _dropped = clean_page_text(text)
        text = text.strip()
        if not text:
            continue
        elif contains_model(text, model) is False:
            # 清洗不该把目标型号本身洗掉：窗口是靠"自己就写着这个型号"选出来的，
            # 洗掉它说明判据动了不该动的东西，宁可退回未清洗的原文。
            text = '\n'.join(region['text'] for region in regions).strip()
        candidate = {
            'text': text,
            'url': url,
            'host': host_of(url),
            'source_type': source.get('source_type', 'unknown'),
            'model': model,
            'headings': [heading for _, _, heading in headings],
            'model_mentions': sum(region['text'].count(model) for region in regions),
            'manufacturer_owned': _own_brand_page(source, brand),
        }
        candidate['source'] = MANUFACTURER_SOURCE if candidate['manufacturer_owned'] else PRODUCT_SOURCE
        if best is None or _rank(candidate) < _rank(best):
            best = candidate
    return best


def _fill(records, spec):
    """把 ``spec`` 写进这些价格记录；已有配置的记录不动。返回写入条数。"""
    written = 0
    for record in records or []:
        if not isinstance(record, dict) or record.get('configuration') not in (None, '', 'unknown'):
            continue
        record['configuration'] = spec['text']
        record['configuration_source'] = spec['source']
        record['configuration_url'] = spec['url']
        written += 1
    return written


def apply_product_configuration(records, sources, model, brand=''):
    """把产品参数写进这些价格记录并标明出处；已有配置的记录不动。

    ``configuration`` 原本为空（``None``／空串／``'unknown'``）才写：中标公告或
    合同附件里真的写了配置时，那是这笔成交记录自己的事实，不能被产品参数顶掉。

    返回真正写入的条数；``0`` 表示这一批来源里没有可用作参数页的产品页。
    """
    spec = product_configuration(sources, model, brand)
    return _fill(records, spec) if spec else 0


def hydrate_run(run):
    """把一个**已经存下来**的 run 里该有而没有的厂家参数补齐（只改内存，不落库）。

    为什么读的时候还要再算一遍
    --------------------------
    ``prices()`` 末尾那次 ``apply_product_configuration`` 跑在**价格通道**里，
    而来源并不是价格通道抓完的。实测 ``task-<run-6>`` 的 run
    ``research-<id>``（66 个来源）四段
    ``stage_timings`` 的 ``fetch`` 次数是 0／2／23／12：价格通道只抓了 2 个来源，
    之后「替代产品核查」又抓 23 个、「来源复核与结果汇总」再抓 12 个。产品参数页
    完全可能是那 35 个里的一个——算价格的时候它还没进 ``run['sources']``，那次就
    没算上，而价格通道已经标成 ``completed``，不会再跑第二遍。于是「已经取到的
    厂家参数」永远到不了页面上。

    推导本身是 ``(sources, model, brand)`` 的纯函数（不调模型、不花额度），所以
    读的时候重算一次与写的时候算出来的**逐字相同**，不存在版本不一致。这也是库里
    已有的 run 唯一能拿到这一格的途径：它们不会再执行一次价格通道。

    返回值是真正填上的条数，调用方据此决定要不要做别的事（例如刷新已存的报告）。
    """
    if not isinstance(run, dict):
        return 0
    sources = run.get('sources')
    specs = {}

    def fill(records, model, brand):
        if not records:
            # 空列表直接跳过：定价本来就是每个型号一份，没有记录就没有要补的东西，
            # 没必要为此再把全部来源扫一遍（真实 run 上实测单次约 96 ms）。
            return 0
        key = (model or '', brand or '')
        if key not in specs:
            specs[key] = product_configuration(sources, model, brand)
        return _fill(records, specs[key]) if specs[key] else 0

    baseline = run.get('baseline') or {}
    written = fill(run.get('prices'), baseline.get('model'), baseline.get('brand'))
    for candidate in run.get('alternatives') or []:
        if isinstance(candidate, dict):
            written += fill(candidate.get('prices'), candidate.get('model'), candidate.get('brand'))
    return written


def _parameter_region_start(lines, model=''):
    """参数区从第几行开始；找不到返回 ``None``。

    配置文本是**配置窗口的原文**（实测那一页 8525 字），开头就是页面导航。报告从
    开头截 200 字，印出来的正好是「网站首页 [http://…] 产品中心 [http://…]」。
    所以先定位参数区：独占一行的参数标题优先，其次是第一行「字段名 | 值」，值的
    那一格要么是一个量、要么就是这个型号（``型号 Model | KH20A`` 那一行也要带进
    报告，它是"这段参数就是这个型号"的证据）。
    """
    for number, line in enumerate(lines):
        if _heading_of(line) is not None:
            return number
    for number, line in enumerate(lines):
        cells = _grid_row_cells(line)
        if not cells or len(cells) != 2:
            continue
        if _VALUE_HEAD.match(cells[1]) or (model and contains_model(cells[1], model)):
            return number
    return None


def _spec_pair_line(line):
    """这一行是不是「字段名：值」形态的参数行 ``(字段名, 值)``；不是返回 ``None``。

    判据与 ``_spec_field_rows`` 同一把尺子，只把分隔符从 ``|`` 换成冒号：
    字段名这一格剥掉行首编号后非空、不超过 ``SPEC_PAIR_MAX``、而且**像字段名**
    （``_looks_like_field``）；值这一格非空、不超过 ``SPEC_VALUE_MAX``、
    而且是一个**量**（``_VALUE_HEAD``）或就是这个型号。

    为什么必须有这一条：有一类厂家页的参数表根本不是网格。实测 ``bjpersee.com``
    的 TU-1901 写的是 ``1、 波长范围： 190nm～900nm``／``2、 波长准确度：±0.3nm(…)``
    共 14 行，一行 ``|`` 都没有——只认网格的话这 14 行真参数会被整段丢掉，
    压缩反而比不压缩信息更少。

    反例（都在真实窗口里，靠"值是量 + 字段名像字段名"两条挡住）：

    * ``如您的问题特别紧急，请拨打我们的客户热线：400-920-3909``（``bingosci.com``
      页脚）——值是电话、以数字开头，只有字段名那一条挡得住它；
    * ``在线客服 服务时间：09:00-18:00``（同页）；
    * 留言表单的 ``您的单位：``（冒号后没有值）；
    * ``周一至周五（09:00-18:00）``（字段名和值合起来是一句话）。
    """
    stripped = _ENUMERATION.sub('', line.strip().strip('#*->• \t'))
    if '://' in stripped and '：' not in stripped:
        # 链接行不是「字段名：值」。``[http://www.example.com/]`` 按半角冒号
        # 切出来是 ``[http`` 与 ``//www.example.com/]``，两半都像词/量；
        # 整行带 ``://`` 又只有半角冒号时直接不收（真参数不会长这样）。
        return None
    if '：' in stripped:
        field, _, value = stripped.partition('：')
    elif ':' in stripped:
        field, _, value = stripped.partition(':')
    else:
        # 没有冒号就不是「字段名：值」——说明句（``系统先进，离心效果好``）
        # 与组标题（``产品详情``）都会走到这里。
        return None
    field, value = field.strip(), value.strip()
    if not field or not value or len(field) > SPEC_PAIR_MAX:
        return None
    if not _looks_like_field(field):
        return None
    if len(value) > SPEC_VALUE_MAX:
        return None
    return field, value

def _looks_like_word(cell, limit=SPEC_FIELD_MAX):
    """这一格是不是一个**词**（参数名或短语值）：短、不含链接、不含句子标点与冒号。

    与 ``_looks_like_field`` 的差别只有标点那一档：允许顿号（``、``）——参数值
    常常是顿号并列（``透过率、吸光度、反射率、能量``）。逗号／分号／句号／问号
    仍然不许：``High/low temperature, power failure, sensor error`` 是句子。
    ``://`` 单独挡：链接按冒号切成两半之后，两半看上去都"短、没标点"。
    """
    if not cell or len(cell) > limit:
        return False
    if '://' in cell:
        # 链接片段不是词。``[http://www.example.com/]`` 会被冒号切成
        # ``[http`` 与 ``//www.example.com/]``，两半看上去都"短、没标点"；
        # 整行含 ``://`` 这一条把两半一起挡住。
        return False
    if re.search(r'[，。；！？,;!?:：]', cell):
        return False
    if not re.search(r'[\w\u4e00-\u9fff]', cell):
        # 纯符号（``—``、``/``、``·``）不是词。
        return False
    return True


def _looks_like_field(cell):
    """这一格是不是**参数名**的形状（用在「字段名 | 值」和「字段名：值」两处）。

    与 ``_spec_field_rows`` 的"字段名 ≤ ``SPEC_FIELD_MAX``"同源，再加三条
    这一轮从真实窗口里换来的结构判据，都是"反例驱动"的：

    * **不含句子标点**（``，。；！？,;!?``）与**冒号**（``:：``）——
      ``如您的问题特别紧急，请拨打我们的客户热线：400-920-3909``（``bingosci.com``
      页脚）值那一格是电话、以数字开头，只有字段名这一格挡得住它；两格行的左格是
      URL 时（``[https://…] | XZ21K-T高速冷冻离心机 […]``，``cnpowder.com.cn``
      的面包屑）也是靠冒号这一条挡住的。**顿号不算句子标点**：
      ``温度、湿度`` 是参数名。
    * **最多两个空格分隔的词**——参数名是词，不是句子。``在线客服 服务时间``
      （同页）是 2 个词、单靠这条挡不住，还有 ``_CONTACT`` 那条；``我们乐意为您
      提供帮助`` 这种整句是 6 个词，挡住了。
    * **至少一个汉字、字母或数字**——挡住 ``—``、``/`` 这类纯符号行。
    * **联系方式与页面元信息不是参数名**——``固话``／``传真``／``更新时间``
      （见 ``_CONTACT``／``_META``）。
    * **一两个字符、又没汉字的不是参数名**（``Q Q``、``No``）——
      纯中文的 ``型号``／``重量``／``电源``是正经参数名，要留（``XKA-2200``
      那张表的``型号 | XKA-2200``一旦被挡，压缩会把型号本身丢掉，比不压缩更糟）。

    双语双写的字段名（``型号 Model``、``Temperature Range``）是**两个词**，
    照收——不能因为"怕长句"把整张英文参数表挡在外面：实测
    ``easymanua.ls`` 那页 16 行参数表（``General Model | DW-86L578S``、
    ``Temperature Range | -40°C to -86°C``…）全是这种带空格的字段名。
    """
    if not _looks_like_word(cell):
        return False
    if len(cell.split()) > 3:
        return False
    if _CONTACT.search(cell):
        # 联系方式不是参数（见 ``_CONTACT``）：``固话 | 021-80122208``、
        # ``传真：021-63450333``、``营销中心电话：0731-82842826`` 都在真实
        # 窗口里出现过，形状与"字段名：量"完全一样，只能按字段名的词挡。
        return False
    if _META.search(cell):
        # 页面元信息不是参数（见 ``_META``）：``更新时间： 2026-08-03``。
        return False
    if _DOCUMENT_FIELD.search(cell):
        # 公告字段不是参数（见 ``_DOCUMENT_FIELD``）：``成交金额：人民币 174800 元``。
        return False
    if len(cell.replace(' ', '')) <= 2 and not re.search(r'[\u4e00-\u9fff]', cell):
        # 一两个字符、又没有汉字的"字段名"是图标文字或缩写（``Q Q：800072208``
        # 那种客服 QQ、``No | 1``）；纯中文的两个字不算（见 docstring）。
        return False
    return True


def _looks_like_value(cell, model=''):
    """这一格是不是**参数值**的形状（两格行的右格、「字段名：值」的值）。

    四条路，都是本模块已有的口径：

    * 是一个量（``_VALUE_HEAD``：数字／``±``／``≤``／负号／``×`` 开头）；
    * 就是这个型号（``型号 Model | KH20A``）；
    * 或者是个**词**（``_looks_like_word``：短、无句子标点）——
      ``Cooling System | Direct cooling``（``easymanua.ls``）、
      ``光度方式： 透过率、吸光度、反射率、能量``（``bjpersee.com``）这种
      非数值参数要留住；页脚那种整句值
      （``如您的问题特别紧急，请拨打我们的客户热线``）是 6 个词且含逗号，挡住；
    * 或者本身是**「子标签：量」**（``_looks_like_sub_value``）——
      ``4、 光谱带宽： TU-1900：2nm``（同页）这种一个字段下按型号分行写值的行，
      不认它整条参数就丢了。
    """
    if not cell or len(cell) > SPEC_VALUE_MAX:
        return False
    if re.search(r'\d{1,2}:\d{2}', cell) and re.search(r'[-~～]', cell):
        # 时间区间，不是参数值：``周一至周五（09:00-18:00）``（``bingosci.com``
        # 客服时间）整行形状就是「字段名：数字起点」；值 ``09:00-18:00``
        # 靠这一条挡住。参数值里的时间点（``1s~99min59s``）没有 ``09:00``
        # 这种"时:分"，不会误伤。
        return False
    if _VALUE_HEAD.match(cell):
        return True
    if model and contains_model(cell, model):
        return True
    if _looks_like_word(cell):
        return len(cell.split()) <= 3
    return _looks_like_sub_value(cell, model)


def _looks_like_sub_value(cell, model=''):
    """``子标签：量`` 形态的值：``TU-1900：2nm``、``SPX-150BⅢ：3/5块``。

    参数值里出现全角冒号时，把冒号前后拆开看：前面像个短标签（``_looks_like_word``，
    且不含空格）、后面本身是个量值。段数超过 2 的（``9:00-18:00`` 这种时间、
    或整句话）直接不要。
    """
    parts = [part.strip() for part in re.split(r'[：:]', cell)]
    if len(parts) != 2 or not all(parts):
        return False
    label, value = parts
    if not _looks_like_word(label) or ' ' in label:
        return False
    return _looks_like_value(value, model) if value else False


def _cells_carry_measure(cells, model):
    """这一行网格里有没有**自己带量或型号**的格（判断 ``value`` 档用）。

    与 ``_grid_row_kind`` 里判 ``anchor`` 的条件同一句，只是拆出来验收。
    """
    return any(_VALUE_HEAD.match(cell) or (model and contains_model(cell, model))
               for cell in cells if cell)


def _grid_row_kind(cells, model):
    """一行网格是不是参数行：``'anchor'``（带量值或型号）／``'basic'``（形状像参数行但这一行没有量）／``None``。

    * 两格行用 ``_spec_field_rows`` 那把尺子：左格是参数名（``_looks_like_field``，
      含字段名长度上限）、右格是参数值（``_looks_like_value``，含量值上限）。
      带量（``_VALUE_HEAD``）或带型号的是 ``anchor``；其余形状合格的是 ``basic``
      ——``Defrosting | Manual`` 这种非数值参数要留住，但它自己不能证明
      "这里有一张参数表"，得靠同块的锚（``_parameter_lines``）。
    * 多格行是**型号对照表**（``型号 | 产品名称 | 最高转速 | 最大离心力 | 容量``）：
      每一格都要短（``_looks_like_field`` 或 ≤ ``SPEC_VALUE_MAX``），
      其中至少一格是量或型号才算 ``anchor``，否则 ``basic``。
      ``BDW-86L490`` 那页 33 行参数表就是这么保住的：表头行有型号、
      后面 ``挂锁孔 | 有 | 有 | 有 | 有`` 这类没有量格，
      按"每行自己必须带量"会整片丢掉。
    """
    for cell in cells:
        if not cell:
            continue
        if '://' in cell:
            # 链接不是参数名也不是参数值。``_spec_field_rows`` 那把尺子靠
            # "字段名 ≤ 24 字"挡住它，但 ``_VALUE_HEAD`` 认方括号
            # （``[http://www.kaidalab.com/]`` 以 ``[`` 开头），
            # 于是"值那一格是链接"的行会漏进来。
            return None
        if not (_looks_like_field(cell) or len(cell) <= SPEC_VALUE_MAX):
            return None
    if len(cells) > 2 and (len(cells) > SPEC_GRID_CELLS or len(' | '.join(cells)) > SPEC_GRID_WIDTH):
        # 一张表被挤进一行：实测 ``yiqi.com`` 那页把整张多型号参数表写在**一行**里
        # （234 个字符、10 个格），格与格之间还是另一张表的行。这种"行"收进来
        # 等于把报告那一行写成一段没有标点的流水账，宁可不要——它本来也不是
        # 一行参数，而是排版丢了换行。
        return None
    if len(cells) == 2:
        left, right = cells
        if not _looks_like_field(left) or not _looks_like_value(right, model):
            return None
        if _VALUE_HEAD.match(right) or (model and contains_model(right, model)):
            return 'anchor'
        return 'basic'
    if any(_VALUE_HEAD.match(cell) or (model and contains_model(cell, model))
           for cell in cells):
        return 'anchor'
    return 'basic'


def _parameter_line(line, model=''):
    """这一行算不算「参数行」；算就返回它的档次（``anchor``／``basic``），不算返回 ``None``。

    三类判定全部复用本模块已有的口径，没有第三套规则：

    * ``heading``——独占一行的参数标题（``_heading_of``／``PRODUCT_SPEC_HEADINGS``），
      一定是 ``anchor``；
    * **``value``——这一行自己带量或型号**（两格行右格是量／是这个型号、
      「字段名：值」的值是量／是这个型号、多格行里有量格），是**最强**的锚；
    * ``anchor``——带量值或型号的行（``value`` 也算 ``anchor``）；这里只用于
      ``_parameter_lines`` 的块收口；
    * 网格行——``_grid_row_kind``（``_grid_row_cells`` + ``_spec_field_rows`` 的
      字段名／值长度尺子 + ``_VALUE_HEAD``）；
    * ``pair``——「字段名：值」且值是量、是这个型号、或是一个词
      （``_spec_pair_line`` + ``_looks_like_value``）。

    空行不是参数行：它是排版留白，留着只会让报告里的参数被空格顶开。
    """
    if not line.strip():
        return None
    if _heading_of(line) is not None:
        return 'anchor'
    cells = _grid_row_cells(line)
    if cells is not None:
        kind = _grid_row_kind(cells, model)
        if kind is None:
            return None
        if kind == 'anchor':
            return 'value' if _cells_carry_measure(cells, model) else 'anchor'
        return kind
    pair = _spec_pair_line(line)
    if pair is not None:
        field, value = pair
        # 字段名与值都要像参数才行。只查值不够：``[http://www.kaidalab.com/]``
        # 会被冒号切成 ``[http`` 与 ``//www.kaidalab.com/]``，而后者以 ``/``
        # 开头——``_VALUE_HEAD`` 认斜杠，于是整行混进参数里；字段名 ``[http``
        # 这一关挡得住它。
        if _looks_like_field(field) and _looks_like_value(value, model):
            if _VALUE_HEAD.match(value) or (model and contains_model(value, model)):
                return 'value'
            return 'basic'
    return None


def _projection_of_this_table(line, grid_at):
    """这一行是不是与上文某一行网格**逐格重复**的投影行。

    复用 ``_projection_parts``／``_cell_matches`` 的判据，但**不设行号容差**：
    ``_projection_repeats_grid`` 只在自己声明行号的 ±``PROJECTION_ROW_TOLERANCE``
    （3 行）内找对照行，而实测偏差不是常数——``xiangyilxj.com`` 那页差 1~2 行，
    ``m.cnpowder.com.cn`` 那页却差 13 行（``行2`` 对的是网格第 16 行），于是
    18 条投影一条都没被认出来，压缩结果里同一份参数印两遍（一份网格、一份投影）。
    投影的判据本来就是**内容**（``_cell_matches`` 逐格比），行号只决定"跟哪一行比"，
    所以这里改成在整段网格里找——反正要对上才删，放宽不会误删。
    """
    parts = _projection_parts(line.strip())
    if parts is None:
        return False
    return any(_cell_matches(parts, row) for row in grid_at.values())


def _parameter_lines(lines, model=''):
    """压缩的一份明细：``(参数行号列表, 每行的档次)``。

    "参数行"按 ``_parameter_line`` 逐行判，再按**块**收口：

    * 块 = 相邻的一串参数行；中间只要夹了一行不是参数行的内容（说明句、
      导航条、版权行），块就断开。
    * 块里**一个锚都没有**（标题、带量值的网格行、「字段名：值」参数行）时，
      整块不要。锚是"这里确实有一张参数表"的证据；``basic`` 行（多格表里
      ``挂锁孔 | 有 | 有 | 有 | 有`` 这种没有量格的行）要靠同块里的锚才进得来，
      孤立的 ``型号 | X`` 一行也不会被当成配置。实测公告的成交标的表
      （``序号 | 标的名称 | 品牌 | 规格型号 | 数量 | 单价``）就是靠这条挡住的。
    * 与上文逐格重复的 ``[表格按列展开 …]`` 投影行（``_projection_of_this_table``）
      直接删掉，而且**不算块断开**——它是同一张表的第二种写法。这一条是必需的：
      有一页的主参数表后面紧跟着 18 行投影，另一页把投影夹在
      参数表中间；不认它，压缩结果要么印两遍，要么把投影当成"非参数行"
      而截断后面的转子表。

    被测模块之外没人调用它，所以返回值给的是**行号**（报告要按行说话）。
    """
    grid_at = {}
    for number, line in enumerate(lines):
        cells = _grid_row_cells(line.strip())
        if cells:
            grid_at[number] = cells

    def continuation(line):
        """上一行参数行没写完、折到这一行的那半截。

        真实例子：``bjpersee.com`` 的 TU-1901 第 4 项写成
        ``4、 光谱带宽： TU-1900：2nm`` 与下一行 ``TU-1901：0.1nm、0.2nm、…``；
        光度准确度那条也折成 ``±0.3%T(0～T)``。折行本身以**量值**开头
        （``_VALUE_HEAD``）、很短、不含冒号、**不含汉字**，靠这四条认它。
        后两条是必须的：``周一至周五（09:00-18:00）``（``bingosci.com``
        客服时间，以数字开头）和 ``应用：`` 这种标签行都挡在外面。
        """
        body = line.strip()
        if not body or len(body) > SPEC_VALUE_MAX:
            return False
        if '：' in body or ':' in body:
            return False
        if re.search(r'[\u4e00-\u9fff]', body):
            return False
        return _VALUE_HEAD.match(body) is not None

    def blocks():
        block = []
        for number, line in enumerate(lines):
            if _projection_of_this_table(line, grid_at):
                continue
            kind = _parameter_line(line, model)
            if kind is not None:
                block.append((number, kind))
                continue
            # 折行（上一行参数行没写完的那半截）也能**起块**：实测 TU-1901 的
            # ``8、 光度准确度：±0.002Abs(0～0.5Abs)；…`` 因为参数名带顿号、
            # 值又超长而不算参数行，它的续行 ``±0.3%T(0～T)`` 就成了孤立折行。
            if continuation(line):
                block.append((number, 'basic'))
                continue
            if block:
                yield block
                block = []
        if block:
            yield block

    kept, kinds = [], []
    for block in blocks():
        # 锚 = ``value``（自己带量/型号）或 ``anchor``（标题、多格行里带量）。
        # 一个锚都没有的块不要：``basic`` 行（多格表里 ``挂锁孔 | 有 | 有 | 有``
        # 这种没有量格的行）要靠同块里的锚才进得来。
        if not any(kind in ('value', 'anchor') for _number, kind in block):
            continue
        # 格内逐字重复的行只留一次。实测两个真实例子：``某型号`` 那页的
        # ``技术参数`` 与 ``技术参数：`` 两颗标题（去冒号后同一行），
        # ``Master-S15`` 那页的 ``进水要求*``／``系统流程**`` 两行在表里
        # 各写了两遍。重复行留着只是把同一份参数印两遍，删掉不丢信息——
        # **不重复的一行都不动**（``No.2 | 角转子`` 与 ``No.2 | 适配器``
        # 只是前两格相同，整行不同，两条都留）。
        seen = set()
        for number, kind in block:
            body = lines[number].strip()
            if _grid_row_cells(body) is not None:
                if body in seen:
                    continue
                seen.add(body)
            kept.append(number)
            kinds.append(kind)
    return kept, kinds


#: 被删掉的行归成这几类，报告里各举一例。判据是**结构**，不是词表：
#: 导航与页脚靠"这一行是不是一整条链接""是不是纯链接行""有没有版权字样"。
#: "字段名：值"里冒号后没值的提示词（``您的单位：``）也归导航——它是表单标签。
_FOOTER = re.compile(r'(?:Copyright|版权所有|©|ICP备)', re.I)


def _dropped_category(line):
    """被删掉的一行属于哪一类：``nav``／``link``／``footer``／``blank``／``prose``。

    只用于**统计和报告举例**，不参与"删不删"的判断。所以 ``prose`` 是兜底：
    联系方式、页面元信息、公告字段这些按启发式词表删掉的行也落在这一类里。
    """
    body = line.strip()
    if not body:
        return 'blank'
    if _LINK_ONLY.match(body):
        return 'link'
    if body.endswith('：') or body.endswith(':'):
        # 冒号后面没有值的表单标签（``您的单位：``／``公司：``）。
        return 'nav'
    if _FOOTER.search(body):
        return 'footer'
    if '|' not in body and ('[' in body and ']' in body):
        # 带链接的一整行——页面导航条的形状（``产品参数 [http://…#cpcs]``）。
        return 'nav'
    return 'prose'


# ---------------------------------------------------------------------------
# 多型号宽表：报告那一行里只留**目标型号**的行／列
# ---------------------------------------------------------------------------
#: 这一组函数的判据与 ``backend.services.evidence_ownership.project_table`` 同源，
#: 全部复用本模块已有的尺子（``contains_model``／``identity``／``_grid_row_cells``）。
#: 差别只有"接口落差"那一处，见 ``model_siblings``。


def _bare_model_cell(value):
    """这一格是不是一个**裸型号写法**：NFKC 之后只剩字母／数字／空格／点／下划线／连字符。

    型号格与参数值格的区别是结构。裸写法这一条先把绝大部分参数值挡掉：

    * ``4500r/min``／``AC220V ±22V 50/60Hz``／``AC220 / 50``——带斜杠；
    * ``±0.5℃``／``0-65℃``／``-50 ～ -86``／``≤53``／``<10 ppb``——带符号或度号；
    * ``两块 （15kg/块）2pieces (15kg/piece)``／``18.2MΩ.cm@25℃``——带汉字或 ``@``。

    挡不住的是 ``300W``／``100W``／``0205202000``／``1-9999 min`` 这种纯字母数字量值，
    它们靠 ``model_siblings`` 的**同前缀**那一条收口（``300w`` 与 ``spx150biii``
    没有公共前缀）。NFKC 是必需的：原文写罗马数字 ``SPX-150BⅢ``，
    而 ``identity``／``contains_model`` 两边都按 ASCII 的 ``SPX-150BIII`` 判。
    """
    if not isinstance(value, str):
        return False
    normalized = unicodedata.normalize('NFKC', value).strip()
    if not normalized:
        return False
    return bool(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9 ._\-]*', normalized))


def _shared_prefix(left, right):
    """两个归一化写法从头起有多个少字符相同。"""
    length = 0
    while length < min(len(left), len(right)) and left[length] == right[length]:
        length += 1
    return length


def model_siblings(lines, model):
    """从**同一段文本**里自推"别的型号"清单；推不出来返回 ``[]``。

    为什么需要它
    ------------
    ``evidence_ownership.project_table`` 要一份 ``other_models``（研究里识别出的
    其他候选型号），那份清单在 PDF／结构化那条路上由 inventory 提供；
    压缩这条路是从**纯文本**提的，手上没有它（``product_configuration`` 的调用方
    只有 ``model``）。这里补的就是这份清单，判据三条，全部复用已有尺子：

    1. 这一格是**裸型号写法**（``_bare_model_cell``）；
    2. ``contains_model`` 说它**不是**目标型号——``SPX-150BⅢ`` 是目标，
       ``SPX-70BⅢ``／``SPX-250BX`` 不是；反过来的方向也挡住：``SPX-150B``
       与 ``SPX-150BIII`` 两个方向的包含关系闸门本来就都判 False
       （``tests/test_model_match_signal.py:96-97``、``:113-114``）；
    3. ``identity`` 之后与目标型号共享长度 ≥ ``MODEL_SIBLING_PREFIX`` 的**前缀**。

    第 3 条是"同系列"的结构判据，不是词表：``SPX-70BⅢ``（``spx70biii``）与
    ``SPX-150BIII``（``spx150biii``）共享 ``spx``。它同时挡住了恰好也是裸写法的
    参数值 ``0205202000``／``100W``／``12kg``／``1-9999 min``（与 ``spx…`` 没有
    公共前缀）。

    **不**用 ``lead_alignment.model_tokens``：那条尺子会把 ``220V``／``50HZ``／
    ``R134a`` 也当成型号 token（实测，见报告），当"别的型号"清单太脏。

    **推不出来就返回空**，调用方据此一行都不裁——这是有意的退化：这一页本来就是
    单型号页、型号归一化后太短（``Q20``）、或者同表里恰好没有同前缀的写法时，
    报告保持原样，绝不因为"认不出型号"而删行。真实记录上的退化情况见
    。
    """
    try:
        target = identity(model)
    except (TypeError, ValueError):
        return []
    if len(target) <= MODEL_SIBLING_PREFIX:
        # 前缀最多只有 len(target) 长；再要求"至少 3 位相同"就没有余量了。
        return []
    found = []
    for line in lines:
        cells = _grid_row_cells(line)
        if not cells:
            continue
        for cell in cells:
            if cell in found or not _bare_model_cell(cell):
                continue
            if contains_model(cell, model):
                continue
            try:
                token = identity(cell)
            except (TypeError, ValueError):
                continue
            if _shared_prefix(token, target) >= MODEL_SIBLING_PREFIX:
                found.append(cell)
    return found


def _own_model_columns(line, model, other_models):
    """这一行是不是"表头并列好几个型号"的横向宽表；是就返回**要保留的列索引**。

    判据与 ``evidence_ownership.project_table`` 的多型号列分支是同一个公式：
    ``list(range(first_model)) + model_columns``——**保留目标型号列之前的所有列**
    当行标签（``温度范围 〔℃〕`` 这类），丢掉其他型号的列。

    三处必须一致：

    * 型号列**重叠**（一格里同时写着目标与别的型号，合并单元格的歧义）时返回
      ``None``——歧义不继承，交给逐行判定，这与 ``project_table:33-34`` 的
      ``return []`` 是同一个取舍；
    * 这一行没有别的型号列（纵向长表，型号在**数据行**里）时返回 ``None``；
    * 这一行不含目标型号时返回 ``None``。
    """
    cells = _grid_row_cells(line)
    if not cells:
        return None
    model_columns = [i for i, cell in enumerate(cells) if contains_model(cell, model)]
    if not model_columns:
        return None
    foreign_columns = [i for i, cell in enumerate(cells)
                       if any(contains_model(cell, m) for m in other_models)]
    if set(model_columns) & set(foreign_columns):
        return None
    if not foreign_columns:
        return None
    return list(range(min(model_columns + foreign_columns))) + model_columns


def project_models(text, model):
    """报告那一行里**只留目标型号**的行／列；做不到时**原样返回**。

    两种真实形态，一个函数里都走一遍：

    * **横向宽表**——表头是 ``型号 | BDW-86L390 | BDW-86L490 | BDW-86L650 | …``，
      数据行每列一个型号。按表头给出的列索引裁：保留行标签列 + 目标型号列，
      丢掉其他型号的列（与 ``project_table`` 的公式一致）。裁完还要**再走一遍行**：
      ``Master-S15`` 那种表里有一行 ``产品型号 | 大流量型 | Master-S30 | …``，
      它自己不含目标型号，按列裁之后目标列上落的是**另一个型号**，必须整行删掉。
    * **纵向长表**——表头（``型 号 Model | 型 号 Model | 电源电压 …``）不含型号，
      型号在**数据行**的某一列里（``标 准 型 | SPX-70BⅢ | …``）。这一种没有列可裁，
      整行走行判定：**提到别的型号、又没有提到目标型号的行删掉**。

    与 ``project_table`` 有意不同的三处（都是为了"报告是给人看的证据"）：

    1. **表头这类没提型号的行保留**。``project_table`` 的纵向分支只输出"自己的行"，
       表头被丢掉；报告里丢了表头，``420×350×500`` 就没有说明。压缩路上表头一行
       221 字确实占掉了 400 字上限的一大半，这是**取舍**，写在
       里。
    2. 一行**同时**提到目标型号与别的型号时**保留**。``project_table`` 判"不继承"
       （喂模型时宁可少给）；报告里这一行是**对照行**，删掉就把目标型号的信息一起
       丢了。英文型号的对照表（``TU-1900``／``TU-1901``）就长这样。
    3. 不删第一格含 ``配件``／``选配``／``订货号``／``accessor``／``optional`` 的行
       （``project_table:27`` 那一条是 PDF 表格路径的取舍）：实测 ``某型号``
       那张表里 ``订货号 | 0205202000`` 是第一格就含"订货号"的真参数行，
       按那条会被误删。

    **退化路径（硬要求）**：自推不出别的型号、或裁完一行不剩，一律返回**原文**，
    报告那一行与改动前逐字相同——绝不允许因为"裁不了"而变空。
    """
    if not isinstance(text, str) or not text.strip():
        return text
    lines = text.splitlines()
    other_models = model_siblings(lines, model)
    if not other_models:
        return text

    # 表头 = **第一个**含目标型号的网格行（与 ``project_table:28`` 同一条取法），
    # 只看这一行决定要不要裁列。真实宽表里"第一个含目标型号的网格行"就是列名行；
    # 而数据行（``标 准 型 | SPX-150BⅢ | 220V/ 50HZ | …``）也可能在某几格里同时
    # 提到两个型号，那是**对照行**，不是表头——拿它当表头会把对照行切掉。
    header = next((line for line in lines
                   if _grid_row_cells(line) and contains_model(line, model)), None)
    columns = _own_model_columns(header, model, other_models) if header is not None else None
    width = len(_grid_row_cells(header)) if columns else None

    projected = []
    for line in lines:
        if columns is not None and _grid_row_cells(line) and len(line.split('|')) == width:
            parts = line.split('|')
            if max(columns) < len(parts):
                # ``line.split('|')`` 的片段原样保留（分隔符与格内字符一个字都不改），
                # 只把整行的首尾空白去掉——``compressed_configuration`` 的行本来就
                # 是 ``strip`` 过的，裁列之后要维持同一条约定。
                line = '|'.join(parts[i] for i in columns).strip()
        projected.append(line)

    kept = [line for line in projected
            if not (any(contains_model(line, m) for m in other_models)
                    and not contains_model(line, model))]
    if not kept:
        return text
    return '\n'.join(kept)


def compressed_configuration(text, model=''):
    """把配置窗口原文压成**只含参数行**的紧凑文本；压不出来返回 ``None``。

    为什么压缩而不是改小上限
    ------------------------
    配置在报告里是**证据**：每一个数值都要能在来源页原文里逐字对上。所以这里
    **只删行、不改字**——留下来的每一行都是原文里逐字存在的那一行，既不改写、
    也不合并、也不重排、更不补全。删掉的只有四类：

    * 页面导航（``产品参数 [http://…#cpcs] 产品介绍 […]`` 这种标签条）；
    * 裸链接行（整行只有一个 ``[http://…]``）；
    * 页脚（版权行、备案号、客服电话那一带）；
    * 说明性长句（``系统先进，离心效果好``、``◆7 英寸 高清触摸屏控制,操作简便``）；
    * 还有空行。

    四类之外还有三小类**按启发式词表**删掉（见 ``_CONTACT``／``_META``／
    ``_DOCUMENT_FIELD``）：页面联系方式（``固话：021-80122208``）、页面元信息
    （``更新时间： 2026-08-03``）、公告流程字段（``成交金额：人民币 174800 元``）。
    它们在 ``_dropped_category`` 的统计口径下都归入"说明性长句"。

    实测 KH20A 那一页（8525 字 / 197 个非空行）里 177 行是这些要删的，真正的参数
    是 9 行主参数 + 1 行转子表表头 + 10 行转子数据（压缩结果 20 行 / 951 字）；
    不压缩时报告从参数区起截 200 字，只够印到主参数表的第 6 行半，压缩后 400 字
    把主参数表印完还接上转子表表头。

    保底（宁可少压，不许丢内容）
    ----------------------------
    压出来的参数行里**带量或带型号的**（``value`` 档）少于 ``SPEC_REPORT_MIN_ROWS``
    行时返回 ``None``，调用方按"找不到参数区"的老办法处理（从参数区起截原文）。
    两个理由：

    * 只压出一两行而原文参数区还有别的写法时，压缩后信息反而比不压缩少；
    * 一段文字只要长得像一串「短字段：短值」就能被当成参数行——成交公告的
      ``项目编号：``／``采购人：``／``成交供应商：``／``成交金额：`` 正是这个形状，
      那种页面根本没有产品参数表，压缩只会把公告的字段当参数印进报告。
      要求"至少两行带量或带型号"就把这类页面挡住了（公告字段的值是人名、
      公司名、日期，没有量也不是型号）。

    误判成"参数行"的代价是把一句页面文字印进报告，比少印几行更糟。

    ``model`` 只用来判"这一行的值就是这个型号"（与 ``_parameter_region_start``
    同一个用法），不参与改写。
    """
    if not isinstance(text, str):
        return None
    lines = text.splitlines()
    kept, kinds = _parameter_lines(lines, model)
    if sum(1 for kind in kinds if kind == 'value') < SPEC_REPORT_MIN_ROWS:
        return None
    return '\n'.join(lines[number].strip() for number in kept)


def _fit_lines(excerpt, limit):
    """把这一段正文按**行**装进 ``limit`` 字，返回 ``(装入的文本, 是否还有没装的)``。

    按行装而不是按字符切：参数行是"字段名 + 值"，在字符上切断会把
    ``整机功率 | 1.65KW`` 印成 ``整机功率 | 1.6``——报告是**证据**，
    读到一个被削掉一半的数值比少印一行更糟。

    装不下第一行时（一整行本身就长过上限，实测 ``Master-S15`` 那张多型号表
    每行 100~300 字，``yiqi.com`` 那页甚至把整张表塞进一行 1000 字）按**字符**
    切到上限并标记"还有"：宁可把这一行切短，也不能让报告那一行没有上限。
    """
    pieces, used, more = [], 0, False
    for line in excerpt.splitlines():
        body = ' '.join(line.split())
        if not body:
            continue
        room = limit - used - (1 if pieces else 0)
        if pieces:
            if len(body) > room:
                more = True
                break
        elif len(body) > limit:
            pieces.append(body[:limit])
            return ' '.join(pieces), True
        pieces.append(body)
        used += len(body) + (1 if len(pieces) > 1 else 0)
    return ' '.join(pieces), more


def report_configuration(record, limit=REPORT_CHARACTER_LIMIT):
    """报告正文里「配置：」后面写什么。

    产品参数要注明它不是这笔成交公告写的（否则读报告的人会以为公告背了书），
    而且要截断——整段参数进一行会把报告读废。

    写进报告的是**压缩版**（``compressed_configuration``：只删行、不改字，见
    那里的说明）。压缩之前的老办法是"从参数区起截 200 字"，问题不是截错位置
    （那是上一版的修法，已经对），而是**截得短**：一份 10 行的参数表会被拦腰
    截断（实测 KH20A 只印到第 5 行参数）。压掉导航／链接／页脚／说明句之后，
    同样一行长度能装下表里更多的参数（见 ``REPORT_CHARACTER_LIMIT`` 上的实测数字）。

    **取材前先切一刀**：``_parameter_region_start`` 找得到参数区起点就先把参数区
    切出来，再在切出来的这一段上压缩。这一刀是必需的——"压缩得出东西"不等于
    "压缩对了"：``configuration_regions`` 对整页短于 ``CONFIGURATION_MAXIMUM``
    的页面直接返回**全文**，那种页面的左边栏推荐位每个自己就带两行带量的参数行，
    压缩照样成功，只是成功出来的全是**别台机器**（kaidasy
    那页：压缩版 133 行里目标型号到第 112 行才出现，报告那 400 字只装到第 44 行）。
    ``_parameter_region_start`` 只认参数标题行与"右格是量或本型号"的两格行，
    不被那些冒号推荐位带走。起点找不到时切出来就是原文，行为与不切完全一样。

    **多型号宽表**（``型 号 Model | 型 号 Model | 电源电压 …`` 下面一行一个型号，
    或 ``型号 | BDW-86L390 | BDW-86L490 | …`` 一列一个型号）另过一道
    ``project_models``：只留目标型号的行／列，别的型号的参数不再印进报告。
    自推不出别的型号、或裁完一行不剩时它原样返回——**这一行永远不会因为裁剪变空**。

    压缩版为空时用切出来的 ``body``（切不到参数区就是原文 ``raw``）；连参数区都
    找不到就从头截，不猜。"压缩版为空"包括**压出来的带量或带型号的参数不够两行**
    的情形（``SPEC_REPORT_MIN_ROWS``）：那种页面本来就没有参数表（省级成交公告的
    ``项目编号：``／``成交金额：``就是这种），压缩只是把公告的字段印成参数。

    报告里的正文按**行**装进 ``limit`` 字（``_fit_lines``），不按字符切。
    """
    from backend.services.procurement_research import price_term_text
    if not isinstance(record, dict):
        return '待核实'
    if record.get('configuration_source') not in SPEC_SOURCES:
        return price_term_text(record, 'configuration')
    label = '厂家资料' if record.get('configuration_source') == MANUFACTURER_SOURCE else '产品资料'
    raw = str(record.get('configuration') or '')
    model = str(record.get('model') or '')
    # 取材顺序：定位参数区 -> 切出来 -> 压缩 -> 裁多型号宽表 -> 装 400 字。
    # 定位**提到压缩之前**，压缩就不再是"整页原文压出什么都算数"。
    start = _parameter_region_start(raw.splitlines(), model)
    body = raw if start is None else '\n'.join(raw.splitlines()[start:])
    compact = compressed_configuration(body, model)
    if compact:
        # 多型号宽表：只留目标型号的行／列（``project_models``）。自推不出别的型号
        # 或裁完一行不剩时它**原样返回**，报告保持改动前的样子。
        compact = project_models(compact, model)
    excerpt = compact or body
    text, more = _fit_lines(excerpt, limit)
    return (f'{label}标称参数（不是这笔成交公告写的配置）：'
            + text + ('…' if more else ''))


__all__ = ['MANUFACTURER_SOURCE', 'PRODUCT_SOURCE', 'SPEC_SOURCES', 'PRODUCT_SPEC_HEADINGS',
           'SPEC_FIELD_MAX', 'SPEC_VALUE_MAX', 'SPEC_FIELD_ROWS',
           'SPEC_PAIR_MAX', 'SPEC_REPORT_MIN_ROWS',
           'REPORT_CHARACTER_LIMIT', 'product_configuration', 'apply_product_configuration',
           'hydrate_run', 'report_configuration', 'compressed_configuration',
           'project_models', 'model_siblings', 'MODEL_SIBLING_PREFIX',
           'clean_page_text', 'strip_page_chrome',
           'without_repeated_table_projection']
