"""教育／科研部门中小型交易平台：一个骨架，站点差异只在这里描述一次。

覆盖的是「买一两件」那一类小额采购：广东省级教育部门自己的竞价/反拍平台。
它们和政府采购网（CCGP、省级政采）不是一类站点——公告条数不少，但**每条只
对应一台仪器**，所以额度口径也不同（见 ``EDUCATION_PLATFORM_LIMIT``）。

设计只有一条：**列表 → 详情 → 品牌/型号/单价** 的骨架在本模块里只写一遍；
每加一个站，只加一条 ``EducationPlatform`` 配置。站点差异全部集中在配置里：

* ``first_request``  列表第一条请求怎么发（GET 还是 ASP.NET 回发 POST）；
* ``list_items``     怎么从一个列表响应里取出「候选公告」；
* ``next_request``   怎么翻到下一页（回发字段名写在这里）；
* ``detail_url``     怎么由候选拼出详情页地址；
* ``shape_text``     （可选）详情页正文要不要整形。

本模块**不做采信判断**：抽出来的价格仍然走既有的 ``validate_price`` 那一整套，
没有一条规则为它放开。这里只负责把官方平台的公告正文取回来、把页面自己印着的
表格列关系恢复出来。

限速：同一站点内所有请求串行，间隔不小于 ``MIN_INTERVAL``。这不是反爬对抗，
是低频访问的自我约束；撞到验证码或封禁就停手，不绕过。
"""
from __future__ import annotations

import re
import threading
import time
import urllib.parse
import urllib.request
from html import unescape

#: 同一站点两次请求之间的最小间隔（秒）。任务口径是「不超过每秒 1 次」，
#: 这里取 1.0 并靠锁保证串行。
MIN_INTERVAL = 1.0

#: 这个通道自己的小额度（请求数）。理由写在 ``procurement_research`` 里：
#: 这些平台公告条数多，但每条只对应一台仪器，所以按"请求数"给量而不按来源条数。
EDUCATION_PLATFORM_LIMIT = 24
#: 一次发现最多交给价格通道几条来源。
#:
#: 12 而不是 6，理由有两条，都是实测出来的：
#:
#: 1. **按平台轮转**（见 ``_balanced_selection``）时，额度要够每个站轮上几轮。每个站
#:    只轮两轮时，一个"只能按品类拉列表"的站（订单名都是"生物安全柜"这类品类词）
#:    要靠逐条读详情才碰得到目标型号；实测 10 条生物安全柜订单里只有 3 条含某型号。
#: 2. 这些来源的正文读取走的是共享 ``fetch`` 预算，不是这条通道自己的额度，所以这里
#:    的上限同时也是"这条通道最多挤占多少正文读取"的上限。12 对一轮 72 的 fetch 来说
#:    是小额。
EDUCATION_PLATFORM_CANDIDATES = 12

DEFAULT_TIMEOUT = 20.0
_USER_AGENT = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
               '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36')


class QuotaReached(Exception):
    """额度用完：安静收手，不是错误。"""


#: 逐站点串行 + 限速。锁按 host 建，多个站之间不互相等待。
_pace_locks: dict = {}
_request_locks: dict = {}
_lock_guard = threading.Lock()
_last_request: dict = {}


def _lock_for(table, host):
    with _lock_guard:
        return table.setdefault(host, threading.Lock())


def platform_host(url) -> str:
    return (urllib.parse.urlsplit(url).hostname or '').lower()


def _default_port(parts) -> int:
    return parts.port or (443 if parts.scheme == 'https' else 80)


def platform_address(url):
    """``(host, port)``。端口要参与识别：**同一台服务器的不同端口是两套数据**。

    ``gdegp.gds.edu.cn``（默认端口）是电子反拍结果公告，而
    ``gdegp.gds.edu.cn:8093`` 是"小零易购"完整系统（商品价格目录 + 结果公示），
    两者的列表、字段、额度口径都不同。只按主机名认站会把它们混成一个。
    """
    parts = urllib.parse.urlsplit(url)
    return (parts.hostname or '').lower(), _default_port(parts)


def pace(host, minimum=MIN_INTERVAL):
    """Wait until this host's own floor has elapsed (caller owns no lock)."""
    with _lock_for(_pace_locks, host):
        wait = _last_request.get(host, 0.0) + minimum - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_request[host] = time.monotonic()


#: ``discover(transport=...)`` 的哨兵：没传 = 用本模块自带的 HTTP 客户端
#: （工具与探测脚本直接调它）；显式传 ``None`` = 本轮**不发任何请求**。
_UNSET = object()


def request(url, *, method='GET', data=None, headers=None, on_request=None,
            timeout=DEFAULT_TIMEOUT, transport=None):
    """One paced public request. Raises on failure; never retries behind a block.

    ``transport`` 是**产品自己的公开网页读取器**（见 ``discover``）。给了它就走
    它，本模块不再自己开连接——这样 ``network_url``、重定向逐跳校验、正文上限
    与测试注入的传输层都照旧生效。为 ``None`` 时用本模块自带的 ``urllib``，那是
    探测脚本的路径，不是产品的路径。
    """
    if on_request is not None:
        on_request()  # quota is spent before the wire, exactly like ``consume``
    host = platform_host(url)
    if transport is not None:
        with _lock_for(_request_locks, host):
            pace(host)
            payload = transport(url, method=method, data=data, headers=headers,
                                timeout=timeout)
        return payload if isinstance(payload, bytes) else str(payload).encode('utf-8')
    merged = {'User-Agent': _USER_AGENT, 'Accept-Language': 'zh-CN,zh;q=0.9'}
    merged.update(headers or {})
    body = None
    if data is not None:
        body = urllib.parse.urlencode(data).encode('utf-8')
        merged.setdefault('Content-Type', 'application/x-www-form-urlencoded')
    with _lock_for(_request_locks, host):
        pace(host)
        req = urllib.request.Request(url, data=body, headers=merged, method=method)
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.read()


# ---------------------------------------------------------------------------
# HTML 小工具（只做文本整形，不做采信）
# ---------------------------------------------------------------------------

#: HTML 标签的通用形状：**属性值里的 ``>`` 不是标签结束**。
#: 不能用 ``<[^>]*>`` 那种写法：表单控件的 ``value`` 里可以带着字面 ``</td>`` 这样的
#: 串，``[^>]*`` 会在那一处提前收尾，后面真正的标签边界就全错位了。这里把带引号的
#: 值当成一个整体。
_TAG_SHAPE = r'<(?:[^>"\']|"[^"]*"|\'[^\']*\')*>'
_TAG = re.compile(r'(?is)' + _TAG_SHAPE)
_HIDDEN_INPUT = re.compile(r'(?is)<input\b(?:[^>"\']|"[^"]*"|\'[^\']*\')*type\s*=\s*"hidden"(?:[^>"\']|"[^"]*"|\'[^\']*\')*>')
_NAME = re.compile(r'(?i)\bname\s*=\s*"([^"]*)"')
_VALUE = re.compile(r'(?i)\bvalue\s*=\s*"([^"]*)"')


def hidden_fields(html) -> dict:
    """ASP.NET 回发要原样带回的隐藏字段（``__VIEWSTATE`` 等）。"""
    fields = {}
    for tag in _HIDDEN_INPUT.findall(html or ''):
        name, value = _NAME.search(tag), _VALUE.search(tag)
        if name and value:
            fields[name.group(1)] = unescape(value.group(1))
    return fields


def _cell_text(raw):
    return re.sub(r'\s+', ' ', unescape(_TAG.sub(' ', raw or ''))).strip()


# ---------------------------------------------------------------------------
# 个人信息字段表（这条通道的硬边界，两个站共用一张底表）
# ---------------------------------------------------------------------------

#: 公开网页上属于个人信息、**一律不落盘**的字段名。
#:
#: 为什么放在模块级而不是某个平台的类里：这是产品边界，不是站点差异——"哪些字段
#: 算个人信息"只该有一个口径。站点差异是**这些字段被印成什么形状**，那在各自平台的
#: ``redact_payload`` 里处理（见 ``EducationPlatform.redact_payload``）。
_PERSONAL_FIELDS = ('联系人', '联系电话', '联系地址', '手机', '邮箱', '收货人', '开票')

#: 政府平台（``gdegp``，默认端口）在底表之外还会印的字段：成交公告的付款与开票信息。
#: **只在它自己那一层加宽**，不动 8093 的口径（``_XLYG_PERSONAL_FIELDS`` 仍旧是底表）。
_GDEGP_PERSONAL_FIELDS = _PERSONAL_FIELDS + (
    '纳税人识别号', '开户行', '银行账号', '账号', '收货地址')

#: ``<td>`` 单元格。``<td>`` 不嵌套，非贪婪到这里就是这一格的边界。
_GDEGP_CELL = re.compile(r'(?is)<td\b(?:[^>"\']|"[^"]*"|\'[^\']*\')*>(.*?)</td\s*>')
#: "字段名: 值"里的**值**，且值不跨标签、不跨行（覆盖 div／span／纯文本行）。
#: 字段名按长度降序排列：``银行账号`` 要先于 ``账号`` 匹配，不然前缀会留在正文里。
_GDEGP_LABEL_VALUE = re.compile(
    r'(?is)(' + '(?:' + '|'.join(sorted(_GDEGP_PERSONAL_FIELDS, key=len, reverse=True))
    + r')\s*[:：]\s*)([^<\n]{1,120})')
#: 18 位身份证／纳税人识别号：整串掩码（只留头尾各 4 位，便于人核对）。
_GDEGP_LONG_ID = re.compile(r'(?<![0-9A-Za-z])(\d{4})\d{10}(\d{4})(?![0-9A-Za-z])')


def _blank_personal_cells(text):
    """把"字段名格 + 值格"里的**值**清空。返回处理后的 HTML 文本。

    两种形状都处理，因为**政府平台详情页的 HTML 样本本地没有**（离线环境不再打
    站点），不能把"字段名一定跟值同格"或"一定分两格"当成事实：

    * ``<td>供应商联系人：</td><td>张三</td>``：字段名格后面**紧跟**的那一格是它的值；
    * ``<td>供应商联系电话：13800000000</td>``：值就在同一格，整格清空。

    只清值、不动表格结构（价格判据要读页面自己的表格列关系，删掉标签会有连带影响）。
    """
    matches = list(_GDEGP_CELL.finditer(text))
    if not matches:
        return text
    out, cursor, pending_value = [], 0, False
    for match in matches:
        inner = _cell_text(match.group(1))
        opening = match.group(0)[:match.group(0).find('>') + 1]
        if pending_value:
            # 上一格只是"字段名："，这一格就是它的值。
            out.append(text[cursor:match.start()])
            out.append(opening + ' </td>')
            cursor, pending_value = match.end(), False
            continue
        field = next((name for name in sorted(_GDEGP_PERSONAL_FIELDS, key=len, reverse=True)
                      if name in inner), '')
        if not field:
            continue
        if inner.split(field, 1)[1].strip(' \u3000:：'):
            # 值和字段名同格：整格清空。
            out.append(text[cursor:match.start()])
            out.append(opening + ' </td>')
            cursor = match.end()
        else:
            # 只有字段名：值在下一格。
            pending_value = True
    out.append(text[cursor:])
    return ''.join(out)


# ---------------------------------------------------------------------------
# 站点配置
# ---------------------------------------------------------------------------

class EducationPlatform:
    """一个省级教育/科研交易平台。子类只描述差异，骨架在 ``discover`` 里。"""

    key = ''
    label = ''
    list_url = ''
    #: 详情页地址模板（``{id}`` 占位）。
    detail_url = ''
    #: 关键词走**表单字段**（ASP.NET 回发筛选）时的字段名；不走表单就留 ``None``。
    #: 骨架会把 ``discover`` 的品类词注入这个字段，站点自己只描述字段名。
    form_keyword_param = None
    #: 不带关键词时，最多翻几页（按时间倒序，越靠前越新）。
    recent_pages = 3

    def first_request(self):
        """列表第一条请求：``(url, method, data)``。"""
        return self.list_url, 'GET', None

    def list_items(self, payload) -> list:
        """从一次列表响应里取出候选项 ``[{'id','title','url','date'}]``。"""
        raise NotImplementedError

    def next_request(self, payload, page):
        """翻到第 ``page`` 页：``(url, method, data)``；``None`` 表示没有下一页。"""
        return None

    def shape_text(self, body, payload=None):
        """详情页正文整形（默认不动）。"""
        return body

    def redact_payload(self, payload):
        """落盘之前的脱敏钩子，默认原样返回。

        只有公开了个人信息的站点需要覆盖它。为什么钩子在**平台配置**里而不是
        在产品主流程里：哪些字段是个人信息、页面把它们印成什么形状，是**站点
        事实**；而"绝不落个人信息"是这条通道的硬边界，所以它必须在
        ``fetch_source`` 存快照之前就跑掉——一旦落盘就来不及了。
        """
        return payload


#: 广东省教育部门政府采购管理平台（"小零易购"电子反拍结果公告）。
#:
#: 列表是 ASP.NET WebForms **回发分页**：页号只能通过 ``__doPostBack`` 送出去，
#: 所以翻页要把隐藏字段原样带回。列表页**不吃关键词**，只能翻最近几页——这个
#: 平台的公告不按型号排序。
#:
#: 详情页把字段印成 ``字段: 值`` 的成对行：采购项目编号／商品名称／品目／品牌／
#: 型号规格／数量／是否进口／最高限价／中标价／制造商／产地／售后服务，用现成的
#: 清洗链就能读成文本，不需要整形。
#:
#: ⚠️ 它的列表页 HTML 里带着自己 ``:8090`` / ``:8093`` 的 href。以前这种地址会让
#: ``public_url`` 把端口当成登录凭据抛 ``ValueError``（已修），所以这里既不需要、也不允许再为它过滤 href。
class GuangdongEduGovernmentPlatform(EducationPlatform):
    key = 'gdegp'
    label = '广东省教育部门政府采购管理平台'
    list_url = 'https://gdegp.gds.edu.cn/fpjgList.aspx'
    detail_url = 'https://gdegp.gds.edu.cn/fpjgContent.aspx?BillGuid={id}'
    recent_pages = 3
    #: 回发控件名，站点自己的分页控件印在页面上（``ANP_checkInput(...,78)``）。
    pagination_target = 'ctl00$ContentPlaceHolder1$UcPagination'
    _ITEM = re.compile(
        r"<li><a\s+title='([^']*)'\s+target=\"_blank\"\s+href=\"(fpjgContent\.aspx\?BillGuid=[^\"]+)\"")
    _TOTAL_PAGES = re.compile(r"ANP_checkInput\(&#39;[^&]*&#39;,(\d+)\)")

    def _text(self, payload):
        return payload.decode('utf-8', 'replace') if isinstance(payload, bytes) else payload

    def list_items(self, payload):
        items = []
        for title, href in self._ITEM.findall(self._text(payload)):
            guid = href.split('BillGuid=', 1)[-1]
            items.append({'id': guid, 'title': unescape(title).strip(),
                          'url': self.detail_url.format(id=guid), 'date': ''})
        return items

    def total_pages(self, payload):
        found = self._TOTAL_PAGES.search(self._text(payload))
        return int(found.group(1)) if found else 1

    def next_request(self, payload, page):
        if page > self.total_pages(payload):
            return None
        fields = hidden_fields(self._text(payload))
        fields['__EVENTTARGET'] = self.pagination_target
        fields['__EVENTARGUMENT'] = str(page)
        return self.list_url, 'POST', fields

    # -- 隐私：落盘之前的硬边界 ---------------------------------------------

    def redact_payload(self, payload):
        """落盘之前的脱敏。**必须在存快照之前调用**（``fetch_source`` 里的位置）。

        这一站的结果公告公开了供应商联系人姓名、手机号、联系地址，以及采购人的
        收货人／开票信息（含纳税人识别号）。它的详情页正文与快照里都出现过真手机号，
        所以这道钩子必须有。

        为什么不能照抄 8093 那一套（整 ``<tr>`` 行删除 + 区块删除）：8093 的页面样本
        在手，它那几条正则（``_XLYG_ROW_TAG`` / ``_XLYG_PERSONAL_SECTION``）是照着
        它的 DOM 写的；**这一站的详情页 HTML 样本本地没有**（离线环境不该为了脱敏再打
        一次站点），所以这里不能把"字段一定在 ``<tr>`` 里、收货信息一定在一个 id 叫
        ``_three`` 的区块里"当成事实。做法改成：8093 那道整行删除留着当第一道，
        再补两道**不依赖行形状**的，最后用号码掩码兜底。

        四道，顺序执行：

        1. 含个人字段名的整 ``<tr>`` 行 → 整块删（与 8093 同一形状、同一取舍）；
        2. ``<td>字段名：</td><td>值</td>`` 与 ``<td>字段名：值</td>`` → 清掉值
           （表格形状认不出来时的兜底，见 ``_blank_personal_cells``）；
        3. ``字段名：值`` 整段（值不跨标签、不跨行）→ 删除，覆盖 div／span／纯文本行；
        4. 漏网的手机号与 18 位身份证／税号 → 掩码。

        取舍与 8093 一致：**宁可少一条候选，不肯泄漏**。所以商品名里带"手机"这种词
        （例如采购"手机壳"）会让那一行一起被删——极端情况下少一条价格，换的是
        "个人信息不落盘"这条硬边界。

        一条例外也照 8093 办：被删的行里如果出现"采购人"，这三个字留下。理由是
        正文权威性判据要读它（``source_authority._LOCAL_FIELDS`` 的"采购人"），
        而它是**机构级的采购单位标记、不是个人信息**（8093 那边留着"三、采购人的
        收货人信息和开票信息"这个标题，是同一条理由）。

        没做的事（如实记在这里，别当成已经做了）：座机号码（带区号的固话）不掩码
        ——8093 也没做，两个站保持同一口径；依据是这两站的个人联系号码以手机号为主，
        而"号码形状"的判据一旦放宽就会误伤价格与订单号。
        """
        text = self._text(payload)

        def drop_personal_row(match):
            block = match.group(0)
            if not any(field in block for field in _GDEGP_PERSONAL_FIELDS):
                return block
            return '采购人' if '采购人' in block else ''

        text = _XLYG_ROW_TAG.sub(drop_personal_row, text)
        text = _blank_personal_cells(text)
        text = _GDEGP_LABEL_VALUE.sub('', text)
        text = _XLYG_PHONE.sub(lambda m: m.group(1) + '****' + m.group(2), text)
        text = _GDEGP_LONG_ID.sub(lambda m: m.group(1) + '**********' + m.group(2), text)
        return text


# ---------------------------------------------------------------------------
# 广东省教育部门"小零易购"电子采购系统（gdegp.gds.edu.cn:8093）
# ---------------------------------------------------------------------------

#: 结果公示列表行。每行是一个 ``<div class="list-content">``，六个等宽 ``<div>``
#: 是订单编号／项目名称／采购单位／成交单位／成交时间／状态，``code`` 三个属性
#: 是详情页需要的参数。
_XLYG_ROW = re.compile(
    r"<div class=\"list-content\"[^>]*?\scode='([^']*)'[^>]*?\scodeType='([^']*)'"
    r"[^>]*?\scode1='([^']*)'[^>]*?\scode2\s*=\s*'([^']*)'[^>]*?>(.*?)</div>\s*</div>", re.S)
_XLYG_CELL = re.compile(r'<div[^>]*>(.*?)</div>', re.S)
_XLYG_LABEL = re.compile(r'<div[^>]*width:18%;"[^>]*title="\s*([^"]*)"')

#: 个人信息所在的行／区块。**这是硬边界**：这些内容不进正文、不进快照、不落盘。
#: 该站的结果公示详情页公开了供应商联系人姓名、手机号、联系地址，以及采购人的
#: 收货人与开票信息。接入只取商品与价格字段，所以这些整块在落盘前就删掉。
#: 字段表本身来自模块级的共用底表 ``_PERSONAL_FIELDS``（口径只有一份）。
_XLYG_PERSONAL_FIELDS = _PERSONAL_FIELDS
_XLYG_ROW_TAG = re.compile(r'(?is)<tr\b(?:[^>"\']|"[^"]*"|\'[^\']*\')*>.*?</tr\s*>')
#: "三、采购人的收货人信息和开票信息"区块：保留标题（它只是标题，且正文的权威性
#: 判据要看"采购人"这个词），删掉它后面到文档尾的所有表格内容。
_XLYG_PERSONAL_SECTION = re.compile(
    r'(?is)(<div[^>]*id="[^"]*_three"[^>]*>.*?<div[^>]*class="title"[^>]*>.*?</div>)(.*)$')
#: 兜底：任何漏网的手机号／身份证号一律掩码（不是删除再补，是直接替换）。
_XLYG_PHONE = re.compile(r'(?<!\d)(1[3-9]\d)\d{4}(\d{4})(?!\d)')


class GuangdongXiaolingyigouPlatform(EducationPlatform):
    """广东省教育部门"小零易购"电子采购系统（8093 端口）：结果公示。

    ⚠️ 与同主机的默认端口（``GuangdongEduGovernmentPlatform``，电子反拍公告）**是两套
    不同的数据**，所以识别按 ``(host, port)`` 而不是主机名（见 ``platform_address``）。

    **结果公示**（``ResultPublicity.aspx``）：一条订单 = 一次成交。列表吃 ``orderName``
    项目名筛选（**裸 POST 即可，站点不校验 viewstate**），翻页走
    ``__doPostBack('…UcPagination','N')``；详情页印着商品信息表
    与成交总额。``publication_tier`` 天然成立（``.edu.cn`` 本地权威主机），
    所以它按普通一手来源进 ``prices()``。

    **商品价格目录**（``Goods_List.aspx?searchStr=``，按型号直查挂牌价）**已撤掉**：
    已经撤掉。撤掉的是这条**在线检索**，不是数据
    本身——``price_leads`` 线索通道（``backend/services/price_leads.py``）是通用机制、
    与这个站无关，一行都没动；这个类的**结果公示解析能力也原样保留**，
    ``backend/services/xlyg_local_db.py`` 仍旧实例化它来建离线库。要看小零易购的
    成交价，走那个离线库（``data/xlyg_prices.sqlite3``）或本平台的结果公示。

    详情页有**四套模板**，选哪一套由列表行的 ``codeType`` 与 ``code2`` 决定
    （见 ``_detail_template``）。两类页面的列名与"成交价"的来源都不一样：

    * **议价／直购订单**（``DD`` 单）：列名「金额（元）」「协议价格（元）」，
      整单成交额印成 ``成交总金额(元)：``；
    * **电子反拍／竞价**（``CM`` 单）：列名「**最高限价**」「**中标价**」，
      整单成交额印成 ``中标价：``。``最高限价`` 是采购人自设的上限，**不是成交价**。

    隐私：结果公示详情页公开供应商联系人手机号与地址、采购人的收货与开票信息。
    ``redact_payload`` 在**落盘之前**把这些整块删掉——正文与快照都不含个人信息。
    """

    key = 'xlyg'
    label = '广东省教育部门“小零易购”电子采购系统'
    list_url = ('https://gdegp.gds.edu.cn:8093/Upgrade/Publicity/ResultPublicity/'
                'ResultPublicity.aspx')
    #: 详情页地址：``{template}`` 由 ``_detail_template`` 按单据类型填。
    detail_url = ('https://gdegp.gds.edu.cn:8093/Upgrade/Publicity/ResultPublicity/'
                  '{template}?BillGuid={id}&RequestCodeType={kind}&RequestGuid={code}')
    #: 列表行 ``codeType``：``0``/``1`` 是订单（议价、直购），``3``/``4`` 是电子反拍／竞价。
    #: 这两个集合是**实测**出来的（265 条抽样里 codeType 只出现这四种）。
    _ORDER_CODE_TYPES = frozenset({'0', '1'})
    _BILL_CODE_TYPES = frozenset({'3', '4'})
    #: 列表行 ``code2``：``1`` = 服务类，``0`` = 物品类。
    #: 判据抄自列表页自己的 ``DoView(this)`` JS：
    #: ``var isService = $(obj).attr("code2");//判断是什么类型；物品和服务``。
    #: **「服务类订单不需要」**（目标是仪器设备的成交价，
    #: 服务类采购不是目标）。所以下面跳过它是**有意的**，不是"还没来得及补"。
    _SERVICE_CODE2 = '1'
    #: 关键词走**表单字段**（结果公示的项目名筛选），不走查询串。
    form_keyword_param = 'ctl00$ContentPlaceHolder1$orderName'
    #: 这个站的关键词筛选是回发，所以列表地址本身不带参数。
    recent_pages = 2
    _FIND_TARGET = 'ctl00$ContentPlaceHolder1$btn_Find'
    _PAGE_TARGET = 'ctl00$ContentPlaceHolder1$UcPagination'
    #: 结果公示里除项目名外的筛选项：一律留空，等于不筛。
    _EMPTY_FILTERS = ('ctl00$ContentPlaceHolder1$orderNo',
                      'ctl00$ContentPlaceHolder1$purchaser',
                      'ctl00$ContentPlaceHolder1$supplier',
                      'ctl00$ContentPlaceHolder1$orderType',
                      'ctl00$ContentPlaceHolder1$orderTypeHid',
                      'ctl00$ContentPlaceHolder1$txtStateDate',
                      'ctl00$ContentPlaceHolder1$endStateDate')

    # -- 列表 ---------------------------------------------------------------

    def _form(self, target, argument):
        return {'__EVENTTARGET': target, '__EVENTARGUMENT': argument,
                **{name: '' for name in self._EMPTY_FILTERS}}

    def first_request(self):
        # 筛选本身就是第一条请求：站点不校验 viewstate，直接回发即可（实测生效）。
        return self.list_url, 'POST', self._form(self._FIND_TARGET, '')

    def _detail_template(self, code_type, code2):
        """按列表行的两个属性选详情页模板；**认不出来就返回 ``None``**。

        分支照抄列表页自己的 ``DoView(this)`` JS：

        ==================  =========  ===============================
        ``codeType``        ``code2``  模板
        ==================  =========  ===============================
        ``0`` / ``1``       ``0``      ``OrderResultPublicityView``
        ``0`` / ``1``       ``1``      ``FWOrderResultPublicityView``
        ``3`` / ``4``       ``0``      ``ResultPublicityView``
        ``3`` / ``4``       ``1``      ``FWBillResultView``
        ==================  =========  ===============================

        ⚠️ 服务类（``code2=1``）那两套模板**有意不接**——我们要的是仪器设备的成交价，
        服务类采购不是目标数据。
        技术上还有第二条同向理由：抽样里的 265 条 ``code2`` 全是 ``0``、一个服务类
        样本都没有，那两套模板的列名从未见过——而本模块靠**认表头列名**取价，猜错会把
        服务费当成设备成交价。所以这里返回 ``None``，调用方不生成这条候选
        （**静默跳过：不生成候选、不发请求、不算缺口、不报错**）。
        ``codeType`` 也没见过的同样返回 ``None``。
        **宁可少一条候选，也不要取回一个表头在、数据空的空壳页**——空壳页在
        下游看起来和"这单没有价格"一模一样，是最坏的一种错。
        """
        if code2 == self._SERVICE_CODE2:
            return None
        if code2 not in ('', '0'):
            return None
        if code_type in self._ORDER_CODE_TYPES:
            return 'OrderResultPublicityView.aspx'
        if code_type in self._BILL_CODE_TYPES:
            return 'ResultPublicityView.aspx'
        return None

    def list_items(self, payload):
        payload = payload.decode('utf-8', 'replace') if isinstance(payload, bytes) else (payload or '')
        items = []
        for code, code_type, code1, code2, inner in _XLYG_ROW.findall(payload or ''):
            template = self._detail_template(code_type, code2)
            if template is None:
                continue
            cells = [_cell_text(cell) for cell in _XLYG_CELL.findall(inner)]
            cells = [cell for cell in cells if cell]
            title = cells[1] if len(cells) > 1 else (cells[0] if cells else '')
            items.append({'id': code, 'title': title,
                          'url': self.detail_url.format(template=template, id=code,
                                                        kind=code_type, code=code1),
                          'date': cells[4] if len(cells) > 4 else ''})
        return items

    def next_request(self, payload, page):
        return self.list_url, 'POST', self._form(self._PAGE_TARGET, str(page))

    # -- 隐私：落盘之前的硬边界 ---------------------------------------------

    def redact_payload(self, payload):
        text = payload.decode('utf-8', 'replace') if isinstance(payload, bytes) else (payload or '')

        def drop_personal_row(match):
            return '' if any(field in match.group(0) for field in _XLYG_PERSONAL_FIELDS) else match.group(0)

        text = _XLYG_ROW_TAG.sub(drop_personal_row, text)

        def drop_personal_section(match):
            # 标题留着（正文权威性判据读"采购人"这个词），它下面的内容整块删掉。
            return match.group(1) + ' </div></div>'

        text = _XLYG_PERSONAL_SECTION.sub(drop_personal_section, text)
        text = _XLYG_PHONE.sub(lambda m: m.group(1) + '****' + m.group(2), text)
        return text

    # -- 正文整形 -----------------------------------------------------------

    def shape_text(self, body, payload=None):
        """把页面自己印着的成交单价恢复成产品认的表形状。

        既有的价格列判据只认「单价（元）」这类列名，而**那条判据不能为这个站放宽**
        （``金额`` 在别的表里常常是总价）。所以这里按站点自己的语义把页面那一列读成
        一列单价，两种模板各读各的：

        * **议价／直购订单**：``协议价格（元）`` 非零时它就是成交单价（成交总金额 =
          协议价格 × 数量）；否则用 ``金额（元）``（实测 ``DYCZ-30C`` 那一单协议价格
          为 0，成交总金额 48000 = 金额 2400 × 数量 20）。
        * **电子反拍／竞价**：成交单价是 ``中标价`` 列；``最高限价`` 是采购人自设的
          上限，**不参与**。整单金额取"二、确定成交供应商"里的 ``中标价：``。

        这个"哪一列是成交价"的判断**每一步都自校验**：算出来的单价 × 数量必须
        等于这一页印着的成交总额，对不上就整单不要。所以它不是"给价格补一个
        值"，而是"只在页面自己的数字自洽时才读它"。

        ``payload`` 是原始 HTML；整形只读清洗链产出的 ``body``，所以它**不再被使用**
        （签名保持与基类一致，``shape_detail`` 会照旧把第二个参数传进来）。商品价格
        目录页的那条分支随在线检索一起撤掉了，见类 docstring。
        """
        section = self._quote_section(body)
        return body + '\n' + '\n'.join(section) if section else body

    #: 议价／直购订单（``OrderResultPublicityView``）里"成交单价"可能落在哪一列，按优先级。
    _PRICE_COLUMNS = ('协议价格（元）', '金额（元）')
    #: 电子反拍／竞价（``ResultPublicityView``）里成交单价只有「中标价」一列。
    #: **「最高限价」不在候选里**：那是采购人自设的上限（不是市场价、更不是成交价），
    #: 拿它当成交价会系统性高估（实测 H1850 限价 28656 而中标价 15000）。
    _BILL_PRICE_COLUMNS = ('中标价',)
    #: 表头里认出哪一组列名，就按哪一组读。
    _ALL_PRICE_COLUMNS = _PRICE_COLUMNS + _BILL_PRICE_COLUMNS

    def _quote_section(self, body):
        """结果公示详情页 → ``型号规格 | 单价（元） | 数量 | 品牌``（订单级自校验）。

        只读清洗链已经切好的 ``|`` 行——那张表是产品自己的 HTML 清洗产出的，这里
        不另起一套解析。

        数据行**常常比表头短**：尾部为空的单元格会被清洗链省略（实测 10 列表头
        下面是 9 格）。所以按表头右补齐；缺的只会是尾部空列，中间列不会缺（页面
        是按列渲染的）。补齐带来的错位风险由下面那道订单级自校验兜住。
        """
        total = self._award_total(body)
        if total is None:
            return []
        header = None
        collected = []
        for line in (body or '').splitlines():
            if '|' not in line:
                # 章节标题（"二、确定成交供应商"）不是表结束：实测商品信息表的数据行
                # 后面紧跟着就是它。表结束只由下一个表头或文末决定。
                continue
            cells = [cell.strip() for cell in line.split('|')]
            if '型号规格' in cells and any(name in cells for name in self._ALL_PRICE_COLUMNS):
                header, collected = cells, []
                continue
            if header is None or len(cells) > len(header):
                # 比表头还宽的 ``|`` 行属于别的表（例如更长的报价明细），不是本表的行。
                continue
            collected.append(cells + [''] * (len(header) - len(cells)))
        section = []
        if header is not None:
            # ``orders`` 是"先试哪一组列"的顺序。反拍页只有一组；订单页先按站点语义
            # 试"协议价格优先"，不行再试"整单一律用金额"。每一次都要求整张表自洽，
            # 所以不存在"试到能过为止"的松动。
            orders = ((self._BILL_PRICE_COLUMNS,) if '中标价' in header
                      else (self._PRICE_COLUMNS, tuple(reversed(self._PRICE_COLUMNS))))
            for columns in orders:
                section = self._quote_rows(header, collected, total, columns)
                if section:
                    break
        return ['型号规格 | 单价（元） | 数量 | 品牌', *section] if section else []

    @staticmethod
    def _award_total(body):
        """这一单的成交总额。两个模板把它印成两种样子：

        * 订单页：``成交总金额(元)： 43400.00``；
        * 反拍页：``中标价： | 77000.00``（在"二、确定成交供应商"里，是**整单**金额）。

        ⚠️ 反拍页的「中标价」出现两次：商品表里的**列名**（单价）与章节里的**整单金额**。
        这里只认带冒号的写法——列名后面跟的是 ``|``，不是冒号，所以不会认错。
        """
        for pattern in (
                r'成交总金额\s*[（(]元[）)]\s*[:：]?\s*\|?\s*([\d,]+(?:\.\d+)?)',
                r'中标价\s*[:：]\s*\|?\s*([\d,]+(?:\.\d+)?)'):
            found = re.search(pattern, body or '')
            if not found:
                continue
            try:
                return float(found.group(1).replace(',', ''))
            except ValueError:
                return None
        return None

    def _quote_rows(self, header, rows, total, columns):
        """整单一次判断：**所有行的 单价 × 数量 之和**必须等于这一页的成交总金额。

        单行校验在多商品订单上必然失败（实测一单两件：39780 + 11475 = 51255，而
        每一行都不等于 51255），所以校验只能在订单级做。这里同时要求每一行的数量
        与单价都读得出来——有一行读不出就整单不要，因为那样求和本身就不完整。
        """
        def at(name):
            return header.index(name) if name in header else None

        model_at, brand_at, quantity_at = at('型号规格'), at('品牌'), at('数量')
        if model_at is None or quantity_at is None:
            return []
        entries, summed = [], 0.0
        for cells in rows:
            model = cells[model_at].strip()
            if not model:
                # 不是商品行：订单抬头、"供应商：" 这一行、被补齐出来的空行都走这里。
                continue
            try:
                count = float(cells[quantity_at].replace(',', ''))
            except ValueError:
                return []
            if count <= 0:
                return []
            chosen = None
            for column in columns:
                index = at(column)
                if index is None:
                    continue
                raw = cells[index].replace(',', '')
                if re.fullmatch(r'\d+(?:\.\d+)?', raw) and float(raw) > 0:
                    chosen = (raw, float(raw))
                    break
            if chosen is None:
                return []
            entries.append(' | '.join([model, chosen[0], cells[quantity_at].strip(),
                                       cells[brand_at].strip() if brand_at is not None else '']))
            summed += chosen[1] * count
        if not entries or abs(summed - total) > 0.01:
            return []
        return entries


#: 注册表。加一个站 = 在这里加一条。
PLATFORMS = (
    GuangdongEduGovernmentPlatform(),
    GuangdongXiaolingyigouPlatform(),
)

_BY_KEY = {platform.key: platform for platform in PLATFORMS}


def registered_hosts() -> frozenset:
    """注册平台自己的主机名（这条通道覆盖到的站点集合）。"""
    return frozenset(platform_host(platform.list_url) for platform in PLATFORMS)


def platform_for(url):
    """这个 URL 属于哪个注册平台；不认识就 ``None``。

    按 ``(host, port)`` 比，host 允许后缀匹配、两边都去掉 ``www.``。端口必须比：
    ``gdegp.gds.edu.cn`` 的默认端口与 ``:8093`` 是两套不同的数据（见
    ``platform_address``）。
    """
    host, port = platform_address(url)
    host = host.removeprefix('www.')
    if not host:
        return None
    for platform in PLATFORMS:
        registered_host, registered_port = platform_address(platform.list_url)
        if port != registered_port:
            continue
        registered_host = registered_host.removeprefix('www.')
        if host == registered_host or host.endswith('.' + registered_host):
            return platform
    return None


def is_registered_platform(url) -> bool:
    return platform_for(url) is not None


def shape_detail(platform_key, body, payload=None):
    platform = _BY_KEY.get(platform_key)
    return platform.shape_text(body, payload) if platform else body


# ---------------------------------------------------------------------------
# 落盘层的号码掩码（**所有来源**共用，不是某一站的钩子）
# ---------------------------------------------------------------------------
#
# 为什么要有这一层：``fetch_source`` 原来只对**注册平台**调 ``redact_detail``
# （按 host+port 认站），不在注册表里的主机（第三方校／院网站、附件所在的下载站、
# 品牌核验那一次普通搜索打到的页面）一律没有钩子——这是  第 3 行
# 记下的共同根因。修法是在**共享落盘路径**上加一道与站点无关的掩码：只认"号码
# 形状"，问的是"这段文本要落盘了吗"，不再问"这是哪一站"。
#
# 口径：**只掩号码，不删个人字段行**。理由是号码不会出现
# 在价格列里，掩码对判据几乎零影响；而"哪些行算个人信息行"是站点事实，继续留在
# 各站自己的 ``redact_payload`` 里（gdegp 那种钩子）。

#: 手机号：11 位，1 开头、第 2 位 3-9；允许 ``138-0000-0000`` / ``138 0000 0000``
#: 这类分隔写法（两个分隔符必须是同一个字符——真页面只会统一用一种，而放宽成"任意
#: 混用"会把 ``单价 1450-0000 元`` 这种金额区间误伤）。
#:
#: 前 4 位与后 4 位之外的六位数字是"填充位"，两种形状共用 ``\d{4}`` 那两段。
_MOBILE = re.compile(
    r'(?<!\d)(1[3-9]\d)(?:([-\u2010-\u2015\s\u3000])\d{4}\2|(\d{4}))(\d{4})(?!\d)')

#: 18 位统一社会信用代码／身份证号形状：**正好 18 位**纯数字，前后不接字母数字。
#: 前 3 位固定 ``[1-9]\d{2}``（统一社会信用代码登记管理部门码 1-9），这一条同时挡掉
#: 以 0 开头的长串（订单号／流水号常以 0 打头）。
#:
#: 不写 ``\b``／不写"十八位里可以夹字母"：``\b`` 在 ``[0-9A-Za-z]`` 之间不成立，
#: 而统一社会信用代码的字母只在末位可能出现的形状本地没有样本，宁可只认纯数字，
#: 也不放过一个真号码。
_LONG_ID_NUMBER = re.compile(r'(?<![0-9A-Za-z])([1-9]\d{2})\d{12}(\d{3})(?![0-9A-Za-z])')

#: 全角形状的同一组号码（只用来**认出**要处理的那一小段，掩码仍由半角那两条正则做）。
#: 全角数字是 ``U+FF10``-``U+FF19``；分隔符沿用半角那套字符集（真页面上全角号码的
#: 分隔符通常是全角空格，已在里面）。
_FULLWIDTH_MOBILE = re.compile(
    r'(?<![\uff10-\uff19])(\uff11[\uff13-\uff19][\uff10-\uff19]'
    r'(?:([-\u2010-\u2015\s\u3000])\uff10{4}\2|\uff10{4})\uff10{4})(?![\uff10-\uff19])')
_FULLWIDTH_LONG_ID = re.compile(
    r'(?<![0-9A-Za-z\uff10-\uff19])([\uff11-\uff19][\uff10-\uff19]{17})'
    r'(?![0-9A-Za-z\uff10-\uff19])')

#: 掩码后的样子。**统一替成 ``***``，不保留任何一位原号码**。
#:
#: 为什么不留后 4 位：留 4 位等于把"这个人是谁"的判别空间缩到 10^4，而它与前半段
#: 拼起来仍是一个软标识符；用户这一轮的要求是"只掩号码、不动别的"，那就把号码整个
#: 抹掉最省事也最彻底。另一条理由与判据有关：``***`` 不含数字，长度又固定 3 个字符，
#: 所以它既不会被价格／型号／日期的正则当成数字读走，也不会改变文本的行结构
#: （``validate_price`` 要求"摘录能在正文里逐字对上"，而摘录是从**同一份掩码后的
#: 正文**生成的，所以这条自洽性对新数据仍然成立）。
_NUMBER_MASK = '***'

#: 全角数字 → 半角，只映射 0-9 这一小段（全角标点、全角空格一律不动）。
#:
#: ⚠️ **只在命中的那一段号码上折，绝不对整段正文折**：正文另有一条既有的全角修法
#: （``unicode_text.fold_width``，只折金额里的全角千分位分隔符，理由是"７４，８００"
#: 会被金额读取器拆成两个数）。这里要是把整段正文的全角数字都折成半角，就顺手改掉了
#: 网页原文——那会连带改掉摘录与快照的逐字对应（``test_fullwidth_money.py`` 钉着这条
#: 行为，第一版就是这么写坏的，全量跑出来 2 个失败）。
_FULLWIDTH_DIGITS = str.maketrans(
    {code: code - 0xFF10 + ord('0') for code in range(0xFF10, 0xFF1A)})


def mask_landed_text(text):
    """落盘前把文本里的**号码**掩掉（手机号、18 位统一社会信用代码／身份证号）。

    ``None`` 原样返回，``bytes`` 进出保持 ``bytes``（调用方后面还要按字节判 PDF、
    按字节存快照）。这一函数**幂等**：掩码产物里没有 11 位／18 位数字串，再掩一次
    结果不变——这点很重要，因为同一个落盘点在补抓、缓存复用等路径上会被走两次。

    全角数字写法的号码（``１３８００００００００``）也掩：网页上的联系电话两种写法
    都有，只认半角等于漏一半。做法是**先按全角形状的号码试一次**，命中就把那一小段
    折成半角再掩（只动这一小段，见 ``_FULLWIDTH_DIGITS``）。

    不做的事（有意为之）：座机号（带区号的固话）
    不掩——放宽号码形状会误伤订单号与金额；姓名／地址不掩——"哪一行算个人信息行"
    是站点事实，仍旧由各站自己的 ``redact_payload`` 处理。
    """
    if text is None:
        return None
    if isinstance(text, (bytes, bytearray)):
        masked = mask_landed_text(bytes(text).decode('utf-8', 'replace'))
        return masked.encode('utf-8')
    if not isinstance(text, str) or not text:
        return text
    if _FULLWIDTH_MOBILE.search(text) or _FULLWIDTH_LONG_ID.search(text):
        text = _FULLWIDTH_MOBILE.sub(lambda m: mask_landed_text(m.group(0).translate(_FULLWIDTH_DIGITS)), text)
        text = _FULLWIDTH_LONG_ID.sub(lambda m: mask_landed_text(m.group(0).translate(_FULLWIDTH_DIGITS)), text)
    text = _MOBILE.sub(_NUMBER_MASK, text)
    return _LONG_ID_NUMBER.sub(_NUMBER_MASK, text)


def mask_landed_payload(payload):
    """文本载荷 → 掩码；二进制（PDF／Office）→ **原样返回**。

    这一句就是"落盘层"的入口：``fetch_source`` 在写快照与推正文**之前**对每一份
    载荷调它，不再看来源主机是不是注册平台。二进制不动，理由与 ``is_binary_payload``
    一字不差：改坏文件不等于删掉里面的数字（实测 379 → 635 字节）。
    **PDF／Office 文本层里的号码仍是缺口**，如实标注。
    """
    if isinstance(payload, (bytes, bytearray)) and not is_text_payload(bytes(payload)):
        return payload
    return mask_landed_text(payload)


#: 二进制载荷的魔数：PDF／OOXML（zip 容器：docx、xlsx）／OLE（旧版 doc、xls）。
_BINARY_MAGIC = (b'%PDF', b'PK\x03\x04', b'\xd0\xcf\x11\xe0')


def is_binary_payload(payload: bytes) -> bool:
    """这份字节是不是二进制文件（PDF／Office／图片）。

    **为什么入口要问这个问题**：平台的脱敏钩子内部是 ``decode('utf-8','replace')``
    → 正则 → 重新 ``encode('utf-8')``。对 HTML 没问题；对二进制会把文件本身改坏——
    离线实测一份最小 PDF：
    **379 字节 → 635 字节、128 个替换符**。而"把文件改坏"并不等于"删掉了里面的
    数字"：手机号是 ASCII，改坏之后照样读得出来。所以二进制在这里**原样返回**：
    不引入"用户拿到坏文件"这个新毛病，也不假装已经脱敏了（PDF 文本层里的个人信息
    是**已知未处理**的缺口）。
    """
    if payload.startswith(_BINARY_MAGIC):
        return True
    return b'\x00' in payload[:4096]


def is_text_payload(payload) -> bool:
    """这份载荷能不能按文本做正则：``str`` 一律算，``bytes`` 要过二进制那道问。

    "落盘层统一掩码"与"平台钩子"必须**共用同一个二进制判断**，否则会出现
    "共享层改了二进制、站点层没改"这种一半一半的坏结果。
    """
    if isinstance(payload, (bytes, bytearray)):
        return not is_binary_payload(bytes(payload))
    return isinstance(payload, str)


def redact_detail(platform_key, payload):
    """落盘之前的脱敏。**必须在存快照之前调用**：文件一写下去就来不及了。

    返回的类型跟着入参走（原始网页是 ``bytes``，测试替身常给 ``str``），因为
    调用方后面还要按字节判 PDF、按字节存快照。

    只对**文本载荷**做正则脱敏：二进制（PDF／Office）原样返回，理由见
    ``is_binary_payload``。这一步对两个注册平台一视同仁（钩子挂在共享入口上，
    不是某个平台的特例）。

    **必须与 ``mask_landed_payload`` 配套使用**：注册平台的钩子删的是"个人字段行"
    （站点事实），号码掩码在共享层、对所有来源生效。两者缺一都会留洞——只调这一个
    的话，没注册的主机一个钩子都没有。
    """
    platform = _BY_KEY.get(platform_key)
    if platform is None or payload is None:
        return payload
    if not is_text_payload(payload):
        return payload
    redacted = platform.redact_payload(payload)
    if isinstance(payload, bytes) and not isinstance(redacted, bytes):
        return redacted.encode('utf-8')
    return redacted


# ---------------------------------------------------------------------------
# 骨架：列表 → 详情
# ---------------------------------------------------------------------------

#: 项目名与仪器名之间常有装饰词，匹配时只取有意义的片段。
_TOKEN = re.compile(r'[A-Za-z][A-Za-z0-9\-\.]{1,}|[\u4e00-\u9fff]{2,}')


def _tokens(text):
    return {token for token in _TOKEN.findall(text or '') if len(token) >= 2}


def relevance(item, model, brand, category) -> int:
    """这个候选跟目标有多相关。**只用来排序取舍，不是采信判据。**

    列表页只有标题，标题里出现型号是最强的信号。8093 结果公示的标题是**订单名**
    （通常只是"生物安全柜"这类品类词，不含型号），所以品类词与品牌词也参与排序。
    分数为 0 的候选照样返回：排序决定先看谁，采信与否仍旧只看正文。
    """
    haystack = ' '.join(str(item.get(k) or '') for k in ('title', 'dept'))
    lowered = haystack.lower()
    score = 0
    if model and model.lower() in lowered:
        score += 8
    elif model and any(token.lower() in lowered for token in _tokens(model)):
        score += 3
    if brand and brand in haystack:
        score += 4
    if category and category in haystack:
        score += 2
    return score


def _pages(platform, keyword):
    """没有关键词的列表只能翻最近几页：这些平台的公告不按型号排序。"""
    return platform.recent_pages


def discover(query, *, on_request=None, timeout=DEFAULT_TIMEOUT,
             limit=EDUCATION_PLATFORM_CANDIDATES, platforms=PLATFORMS,
             transport=_UNSET):
    """在注册平台上按 *query* 找候选结果公告，返回 ``[{'url','title',…}]``。

    ``query`` 是 ``{'model','brand','category'}``：型号用于在标题里认人，品类词
    用于向只吃项目名的列表接口提问。两条路都只产生**候选**——正文与价格仍旧由
    既有通道判。

    ``on_request`` 每发一次请求调用一次；它抛 ``QuotaReached``（或任何异常）时
    立即收手并返回已经拿到的候选。额度用完不该让整轮研究崩掉，也不该被当成
    "这个站没有东西"。

    额度用完时"已经拿到的候选"**包含当前这个平台已经翻过的页**（见下面
    ``stopped_for_quota`` 那一支）：额度是在上网之前扣的（``request`` 先调
    ``on_request``），所以额度耗尽的那一页本身没有内容，但它**前面**几页的候选
    是已经解析好的，必须一起返回——否则"额度在第 2 页用完"就等于把第 1 页白读了。

    返回值就是 ``_balanced_selection`` 排好序的候选 ``list``，**没有**"是否用完
    额度"的回传位（旧 docstring 提到的 ``reached_quota`` 从来没有真正返回过，已
    按实际改写）。调用方想知道额度耗尽与否，看它自己交给 ``on_request`` 的那个
    计数器：真正的调用点 ``procurement_research.education_platform_leads`` 传的是
    ``note_education_platform_request``，它按 ``run['usage']['education_platform']``
    记账，额度用尽后 ``education_platform_budget(run)`` 归零——现在返回的是候选本身
    而不是空，所以它不需要这个信号也照样能把"额度用尽"和"这个站没有东西"分开。

    ``transport`` 见 ``request``。产品必须显式传自己的公开网页读取器；显式传
    ``None``（这一轮没有可用读取器）时**一个请求都不发**，直接返回空——这条通道
    绝不自己另开一条绕过产品的网络出口。没传（``_UNSET``）才用模块自带的
    ``urllib``，那是探测脚本的用法。
    """
    if transport is None:
        return []
    send = None if transport is _UNSET else transport
    model = str((query or {}).get('model') or '').strip()
    brand = str((query or {}).get('brand') or '').strip()
    category = str((query or {}).get('category') or '').strip()
    keyword = category or model
    found, seen, stopped_for_quota = [], set(), False
    for platform in platforms:
        payload = None
        listed = []
        for page in range(1, _pages(platform, keyword) + 1):
            if page == 1:
                url, method, data = platform.first_request()
            else:
                if payload is None:
                    break
                # ``next_request`` 用裸 ``None`` 表示"没有下一页"（见基类约定），
                # 所以不能直接解包。
                step = platform.next_request(payload, page)
                if step is None:
                    break
                url, method, data = step
            if url is None:
                break
            if platform.form_keyword_param and keyword and data is not None:
                # 回发筛选：关键词是表单字段，不是查询串参数（站点自己只描述字段名）。
                data = {**data, platform.form_keyword_param: keyword}
            try:
                payload = request(url, method=method, data=data or None,
                                  on_request=on_request, timeout=timeout,
                                  transport=send)
            except QuotaReached:
                stopped_for_quota = True
                break
            except Exception:
                payload = None
                break
            try:
                items = platform.list_items(payload)
            except Exception:
                break
            if not items:
                break
            added = 0
            for item in items:
                if item['url'] in seen:
                    continue
                seen.add(item['url'])
                listed.append({**item, 'platform': platform.key,
                               'platform_label': platform.label})
                added += 1
            if not added:
                # 这一页一个没见过的候选都没带来：分页没有真的前进（回发参数没
                # 生效时站点会一直把第一页发回来）。再翻只是白耗额度。
                break
        # 额度用完时**这个平台已经解析出来的候选照收**——包括额度耗尽那一页的
        # **前几页**。这几页的请求已经发生过、候选已经解析好了，丢掉它们等于把
        # 花掉的额度白扔（8093 的 ``recent_pages = 2`` 正好会走到这里：翻第 2 页
        # 时额度用尽，第 1 页就成了白读）。``listed`` 只在这里 extend 一次，
        # 下面那支 ``break`` 之后就到不了本行，所以不存在重复 extend。
        found.extend(listed)
        if stopped_for_quota:
            # 额度用完：不再发任何请求，这一轮到此为止。
            break
    return _balanced_selection(found, platforms, model, brand, category, limit)


def _balanced_selection(found, platforms, model, brand, category, limit):
    """按平台轮转取候选，而不是全局排序取前 N。

    全局排序看起来更"择优"，实测却会让排在后面的站**一个名额都拿不到**：同一次运行
    里 6 个名额被一个站占满，于是"新接的站没有贡献"——那是名额分配造成的假象，不是
    那个站没有数据。轮转保证每个站至少轮到自己，站内再按相关度排序。
    """
    grouped = {}
    for item in found:
        grouped.setdefault(item['platform'], []).append(item)
    for items in grouped.values():
        items.sort(key=lambda item: -relevance(item, model, brand, category))
    order = [platform.key for platform in platforms if grouped.get(platform.key)]
    ranked, index = [], 0
    while len(ranked) < limit:
        progressed = False
        for key in order:
            items = grouped[key]
            if index < len(items):
                ranked.append(items[index])
                progressed = True
                if len(ranked) >= limit:
                    break
        if not progressed:
            break
        index += 1
    return ranked
