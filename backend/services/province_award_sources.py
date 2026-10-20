"""省级政府采购平台：**按型号直查成交公告**（只查省本级），结果落跨任务缓存。

这个模块补的是产品里一直缺的那一环：**输入一个型号，直接拿到它的成交单价**。
此前只有 "品类词检索 → 公告 → 详情页 → 解析标的表" 这一条链——它要求先知道该搜
哪个品类词、再把公告逐条读回来。安徽 / 江西两站的成交公告接口**有全文检索参数**
（``keyword``，搜正文；``projectName`` 只搜项目名，两者不是一回事，见
/），而**正文 HTML 随列表响应一起返回**（字段 ``content``），
所以 "型号 → 公告正文（含品牌 / 规格型号 / 数量 / 单价）" 是一次请求就能完成的事，
不需要逐条进详情页。安徽详情页服务端只给 JS 空壳，进详情页反而读不到正文。

三个必须写在这里、不能靠记忆填的接口事实（2026-09-25 实站复核）：

* **必须显式传 ``district``**。不传时站点直接拒绝：
  ``{"success":false,"result":null,"error":"区划和实例白名单不能同时为空"}``。
  安徽省本级 ``349900``、江西省本级 ``369900``。
* ``keyword`` 才是搜正文的参数；``projectName`` 只搜项目名，型号写在正文的成交标的
  表里，用错参数会得到 "0 命中" 这种**假否定**。
* 江西站**只接受 legacy renegotiation** 的 TLS 握手，OpenSSL 3.x 默认拒绝（实测
  ``UNSAFE_LEGACY_RENEGOTIATION_DISABLED``）；姿态写在平台的 ``tls_profile`` 上，
  由产品读取器执行（见 ``search_providers.TLS_PROFILES``）。

## 只查省本级，不要遍历区划

``district`` 是**硬过滤**：``349900`` 是 "安徽省本级"，**不等于全省**。要覆盖全省得
逐区划遍历（安徽 183 个、江西 158 个区划码）。当前的做法
是"先用省本级按需查、把结果存下来"——所以 ``districts`` 是一个**配置项**，而现在
**默认只装省本级一格**。以后想扩全省是改配置，不是重写：每个 (平台, 区划) 各自一条
缓存键、各自一次请求。

## 两个平台家族：电子卖场的 ``queryPage`` 与政采云内核的 ``portal/all``

注册表分成两份，因为它们**不是同一套接口**：

* ``PROVINCES``（安徽 / 江西）：电子卖场（luban）
  ``POST <站>/announcement/lobby/queryPage``，``district`` 必传，
  **列表响应里的 ``content`` 就是完整正文**，一次请求同时拿到型号与单价。
* ``PORTAL_PROVINCES``（浙江 / 广西）：政采云内核 ``POST <站>/portal/all``，
  body 只有 ``keyword`` / ``pageNo`` / ``pageSize``，**没有区划参数**——检索是
  **站内全量**（返回行的 ``districtName`` 可以是市县，实测浙江 7/8、广西 1/6）；
  而且**列表行的 ``content`` 只是命中片段**（实测浙江 5 个
  型号 16 条行，片段都在 114–121 字符，`price_table_rows` **一条都读不出来**）。正文必须回读
  ``GET <站>/portal/detail?articleId=``——它回的是 JSON，正文在
  ``result.data.content`` 里（实测某型号那条：响应 25646 字节，
  ``content`` 原文 20586 字符，过一遍清洗链剩 1600 字符 / 110 行）。

``ALL_PROVINCES`` 是产品实际要查的全集（``province_award_platforms()`` 用它）。
``lookup()`` 的 ``platforms`` 默认值**仍旧是 ``PROVINCES``**：这是历史默认值，
逐字未改，直接调 ``lookup`` 的既有用例测的就是卖场家族；产品路径一律显式传
``ALL_PROVINCES``（见 ``procurement_research.province_award_platforms``）。

## 浙江 / 广西的正文形状：标项块，不是表格

政采云内核的合同公告把每个标项印成一组 ``标签：值`` 行（实测原文）：

    标项三
    主要标的名称： 立式压力蒸汽灭菌器
    数量： 1.00
    单价（元）： 12000.00
    规格型号（或服务要求）： 品牌:上海申安
    规格型号:LDZX-50L

**整份正文一个 ``<table>`` 都没有**（实测 ``<table`` 出现 0 次），所以既有的表格判据
（``price_table_rows`` 要求"表头行 + 列数完全相等的数据行"）在这份正文上读出 **0 行**
——单价 12000.00 就明明白白印在上面却读不出来。:func:`shape_lot_blocks` 因此把这组
**页面自己印着的标签**读成一张表（每个格子都是页面原文，不改一个字、不做任何算术），
**追加**在正文末尾；正文原件一字不动，引文与偏移照旧可回溯。

## 用缓存代替建库

同一个型号第二次查询（**包括别的任务、别的用户**）不再打网络：结果写进
``province_award_cache``（见 :func:`cache_key` / :func:`read_cache`）。缓存里存的是
**证据**（公告 URL + 平台自己给的正文 HTML + 元数据），不是 "价格结论"——每次运行
仍旧从正文重新过一遍既有的全部价格判据，所以缓存过的公告与新抓到的公告走的是
同一条采信链。

## 采信规则一条都没动

本模块只做两件事：把接口的响应解析成 "公告 + 正文"，以及把这两站成交标的表**页面
自己印着的列关系**恢复出来（:func:`shape_award_table`，带自校验）。金额仍旧要过
``price_table_rows`` / ``validate_price``、型号仍旧要逐字出现在数据行里、
``source_type`` 仍旧由 ``is_tender`` 判（为此只补了两个措辞：见
``source_authority.FORMAL_DECISION``）。**没有任何一条判据为这两个省放开。**

限速：请求按 host 串行，间隔不小于 ``MIN_INTERVAL``（复用既有省级平台通道那把按
host 的锁，两个通道对同一台服务器不会各自开一个 1 秒地板）。撞到验证码 / WAF 就
停下如实记失败，不绕过。
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

from backend.services.education_platform_sources import (
    DEFAULT_TIMEOUT, MIN_INTERVAL, QuotaReached, mask_landed_text, pace)

#: ``request(transport=...)`` 的哨兵：没传 = 用本模块自带的 ``urllib``
#: （工具与探测脚本直接调它）；显式传 ``None`` = 这一轮**一个请求都不发**。
_TRANSPORT_UNSET = object()

#: 成交公告那一组类型码（页面标签抓到的）：8013 电子卖场成交公告
#: （采购成功）、8020 / 8012 同族。``[8018,8023]`` 是**竞价公告**（还没成交），不能混。
ANNOUNCEMENT_TYPES = (8020, 8012, 8013)

#: ``pageSize`` 上限 100；一个型号一次取 20 条足够。实测命中数是个位数到十几
#: （安徽 ``TG16-WS`` 2、``DGG-9203AD`` 16；江西 ``DHG-9070A`` 1），20 覆盖到实测
#: 最大值。``total`` 有 10000 硬顶，但那是全量翻页才会碰到的事，按型号查碰不到。
PAGE_SIZE = 20
#: 一次请求拿到的公告里，最多并入几条来源。这是**展示与成本上限**（报告长度、
#: 快照数、模型调用），不是 "这个型号只有 8 条" 的结论。
MAX_SOURCES = 8

#: 省本级的区划码。**这一格不是全省**，见模块 docstring。
ANHUI_PROVINCE_LEVEL = '349900'
JIANGXI_PROVINCE_LEVEL = '369900'

#: 政采云内核那两站的 ``pageSize``。/实测用 15，
#: 也是 15。**没有测过上限**，所以不往上加：15 已经覆盖到实测的
#: 最大命中数（浙江 ``SW-CJ-1FD`` 7 条），翻页也没测过。
PORTAL_PAGE_SIZE = 15

#: 政采云内核站的"区划"格。这个家族**没有区划参数**，``keyword`` 检索是站内全量
#: （返回行可以是市县），所以这一格不是区划码，而是"全站"这个作用域标记。
#: 它进缓存键（``cache_key``），保证"全站"的结果不会被将来某个真区划查询复用；
#: 也进 ``queries`` 留痕，让"这一格查的是全站、不是省本级"在台账里看得出来。
PORTAL_SCOPE = 'site'

#: 一个型号最多回读几条详情页（每个政采云内核平台各一次上限）。
#:
#: 取值理由：① 列表片段里**带不带 ``单价（元）``** 是页面自己的证据，按它排序后
#: 命中的公告排在前面（实测 里"带单价的"就是能被读出来的那些）；
#: ② 实测一个型号在浙江最多 7 条行、带单价的 4 条（``DHG-9070A`` 6 条里 4 条带价），
#: 4 条能覆盖到实测的最大值；③ 它是**成本上限**，不是"这个型号只有 4 条"的结论——
#: 超出的行留在 ``queries.rejected`` 的计数里，看得见。
PORTAL_DETAIL_LIMIT = 4

#: ``shape_lot_blocks`` 铺出来的表头是**页面自己的标签**（``主要标的名称`` /
#: ``品牌`` / ``规格型号`` / ``数量`` / ``单价（元）``），所以这里没有常量——
#: 价格列名按每份正文自己印的那一种来写（几个块写法不一致就不铺）。


#: 缓存表与缓存版本。表结构 ``(id, task_id, payload)``，与既有表同形
#: （``backend/repositories/sqlite.py``）；读取只按 ``id``，所以**跨任务**生效。
CACHE_TABLE = 'province_award_cache'
CACHE_VERSION = 1
#: 缓存有效期。取值理由见 ：成交公告一旦发布就是
#: 历史事实、不会再变，但**同一个型号以后还会有新公告**，所以不能永不过期；
#: 而一次未命中的代价只有 1 个请求（每省），所以 7 天这种 "按周" 的窗口比按天更划算。
CACHE_TTL_SECONDS = 7 * 24 * 3600


@dataclass(frozen=True)
class ProvincePlatform:
    """一个省级平台的接口事实。站点差异只写在这里。"""

    key: str
    label: str
    endpoint: str
    list_page: str
    detail_page: str
    #: 要查的区划码。**默认只有省本级一格**；加区划 = 加请求，缓存的键跟着变。
    districts: tuple
    #: 站点的 TLS 握手姿态（``search_providers.TLS_PROFILES`` 里的名字）。
    tls_profile: str | None = None
    #: 正文里的成交标的表，尾列是不是 "非数据列"（页面印了、数据行不给值那种）。
    trailing_columns: tuple = ('报价明细',)
    #: 一个型号最多回读几条**详情页**。``0`` = 这个站不需要回读详情（列表响应里
    #: 就是完整正文）。卖场家族（安徽 / 江西）保持 0，行为一个字不变。
    detail_limit: int = 0

    def query_body(self, district: str, model: str, *, page_no: int = 1,
                   page_size: int = PAGE_SIZE) -> dict:
        """一次查询的 JSON body。``district`` **显式写进去**，绝不省略。

        ``{"pageSize":…,"pageNo":…,"announcementTypes":[…],"district":…,
        "keyword":…,"all":{"blackList":[],"whiteList":[]}}`` 是 
        逐字记下来的形状。
        """
        return {
            'pageSize': page_size, 'pageNo': page_no,
            'announcementTypes': list(ANNOUNCEMENT_TYPES),
            'district': district,
            'keyword': model,
            'all': {'blackList': [], 'whiteList': []},
        }

    def request_headers(self) -> dict:
        """页面自己发这个请求时带的头（``Origin``/``Referer`` 就是它自己的列表页）。"""
        origin = urllib.parse.urlsplit(self.endpoint)
        return {
            'Content-Type': 'application/json;charset=UTF-8',
            'Accept': 'application/json, text/plain, */*',
            'Origin': f'{origin.scheme}://{origin.netloc}',
            'Referer': self.list_page,
        }

    def detail_url(self, row: Mapping[str, Any], district: str) -> str:
        """由列表行拼出详情页地址（正文只是**引用出处**，不用于回读）。"""
        encrypt = str(row.get('encryptId') or '').strip()
        if not encrypt:
            return ''
        return f'{self.detail_page}?encryptId={encrypt}&district={district}'

    def detail_body(self, payload) -> str:
        """从一次详情页响应里取出正文 HTML（**不发请求**，只解析）。

        这个家族从列表响应里就已经拿到正文，所以基类实现是"没有详情页这一步"。
        需要回读详情的平台必须覆盖它——覆盖了才说明它自己知道正文在响应的哪一格。
        """
        raise NotImplementedError('这个平台不声明详情页正文的位置')

    def shape_text(self, body: str) -> str:
        """详情正文的工具专用整形（**默认原样返回**）。

        ``shape_award_table`` 已经在清洗链里跑过了（它只恢复"表头尾列"那种列关系）；
        这里留给"正文根本不是表格、要按站点自己的标签铺成表"的平台。默认恒等，
        所以卖场家族（安徽 / 江西）走的是原路径、一个字没变。
        """
        return body

    def parse_response(self, payload, model: str, district: str) -> dict:
        """把一次查询的响应解析成 ``{'total','announcements','rows','rejected'}``。

        **本地再验一遍型号**：接口是全文检索，返回的行理论上都含这个型号串，但不能靠
        "接口说含" 就落库——这里逐条在正文里复核，核不到的行丢掉（并记下丢了几条，
        这样 "接口给了 3 条、本地只认 2 条" 是可解释的，而不是静默缩水）。
        """
        if isinstance(payload, (bytes, bytearray)):
            payload = payload.decode('utf-8', 'replace')
        if isinstance(payload, str):
            data = json.loads(payload)
        else:
            data = payload or {}
        if not isinstance(data, dict):
            raise ValueError('省级平台响应不是对象')
        # ``success=false`` 是**拒绝**，不是 "0 命中"：实测（2026-09-25）不带 ``district``
        # 时两站都回 ``{"success":false,"result":null,"error":"区划和实例白名单不能同时为空"}``。
        # 把这种响应当空结果读，就等于把 "没查成" 说成 "没有"——的假否定
        # （"37 个型号全 0 命中"）就是这么来的。所以这里**抛错**，由调用方如实记一条失败。
        if data.get('success') is False or 'result' not in data:
            raise ValueError('省级平台拒绝这次查询：' + str(data.get('error') or '响应缺少 result')[:200])
        result = data.get('result') or {}
        return self._parse_rows(result.get('total'), result.get('data') or [], model, district)

    def _parse_rows(self, total, rows, model: str, district: str) -> dict:
        """两个家族共用的那一半：逐行复核型号、拼公告、数清楚丢了几条。"""
        announcements, rejected = [], 0
        needle = (model or '').strip().casefold()
        for row in rows if isinstance(rows, (list, tuple)) else []:
            if not isinstance(row, dict):
                rejected += 1
                continue
            url = self.detail_url(row, district)
            content = row.get('content') or ''
            if not url or not isinstance(content, str) or not content.strip():
                rejected += 1
                continue
            body = clean_content(content, url)
            if needle and needle not in body.casefold():
                rejected += 1
                continue
            announcements.append(self.announcement(row, url, content, district))
        announcements.sort(key=lambda item: item['published_at'], reverse=True)
        return {'total': total, 'rows': len(rows) if isinstance(rows, (list, tuple)) else 0,
                'announcements': announcements, 'rejected': rejected}

    def announcement(self, row: Mapping[str, Any], url: str, content: str,
                     district: str) -> dict:
        """一条列表行 → 一条来源公告（字段名按卖场家族的响应写的）。"""
        return {
            'platform': self.key,
            'platform_label': self.label,
            'url': url,
            'title': str(row.get('title') or row.get('projectName') or '').strip(),
            'project_name': str(row.get('projectName') or '').strip(),
            'project_code': str(row.get('projectCode') or '').strip(),
            'district': district,
            'district_name': str(row.get('districtName') or '').strip(),
            'announcement_type': row.get('announcementType'),
            'announcement_type_name': str(row.get('announcementTypeName') or '').strip(),
            'published_at': _released_at(row),
            'content': content,
        }


PROVINCES: tuple = (
    ProvincePlatform(
        key='anhui',
        label='安徽省政府采购云平台·电子卖场（省本级）',
        endpoint='https://mall.anhui.zcygov.cn/announcement/lobby/queryPage',
        list_page='https://mall.anhui.zcygov.cn/luban/announcement/list',
        detail_page='https://mall.anhui.zcygov.cn/luban/announcement/detail',
        districts=(ANHUI_PROVINCE_LEVEL,),
    ),
    ProvincePlatform(
        key='jiangxi',
        label='江西省政府采购电子卖场（省本级）',
        endpoint='https://www.jxemall.com/announcement/lobby/queryPage',
        list_page='https://www.jxemall.com/luban/announcement/list',
        detail_page='https://www.jxemall.com/luban/announcement/detail',
        districts=(JIANGXI_PROVINCE_LEVEL,),
        # 江西站只接受 legacy renegotiation（实测 OpenSSL 3.x 默认上下文直接
        # UNSAFE_LEGACY_RENEGOTIATION_DISABLED）。是站点事实，不是绕过访问控制。
        tls_profile='legacy-renegotiation',
    ),
)


@dataclass(frozen=True)
class PortalPlatform(ProvincePlatform):
    """政采云内核（``POST <站>/portal/all``）的省级站：浙江、广西。

    与卖场家族（:class:`ProvincePlatform`）的差异，每一条都是实测的站点事实：

    * **body 只有三个字段**（``keyword`` / ``pageNo`` / ``pageSize``）。这里**不写**
      ``district``：这个接口没有区划参数，检索是站内全量。也不写 ``announcementTypes``
      ——那是卖场家族的参数，照抄过来**没有证据说这个站认它**（见报告"公告类型"一节）；
      也不写 ``isTitleSearch``：记着"传布尔会 422"，而省略它时
      （默认就是搜正文）实测能召回只出现在正文里的词（"灭菌锅"在浙江站有结果）。
    * **响应形状不同**：``result.data`` 是对象（``{"total":…,"data":[…]}``），
      不是卖场家族那样的数组。
    * **列表行的 ``content`` 是命中片段**（~120 字符），正文要回读
      ``GET <站>/portal/detail?articleId=``（回 JSON，正文在 ``result.data.content``）。
    * **详情正文不是表格**：标项印成 ``标签：值`` 行，由 :func:`shape_lot_blocks` 铺成表。
    """

    #: 列表行的 ``content`` 只是片段，所以这个家族一律回读详情页。
    detail_limit: int = PORTAL_DETAIL_LIMIT
    #: 这个家族的表没有"印了但不给值"的尾列，所以整形钩子对它是空集——
    #: 这一格同时保证 ``shape_award_table`` 的循环不会因为新增平台而改变
    #: 卖场家族的结果（空元组永远匹配不上）。
    trailing_columns: tuple = ()

    def query_body(self, district: str, model: str, *, page_no: int = 1,
                   page_size: int = PORTAL_PAGE_SIZE) -> dict:
        """三个字段，逐字是 //实测可用的形状。"""
        return {'keyword': model, 'pageNo': page_no, 'pageSize': page_size}

    def request_headers(self) -> dict:
        """这个站吃的头（实测 `Content-Type: application/json` 即可，不需要 Origin）。"""
        return {
            'Content-Type': 'application/json',
            'Accept': 'application/json, text/plain, */*',
        }

    def detail_url(self, row: Mapping[str, Any], district: str) -> str:
        """详情页由 ``articleId`` 拼出。

        ``articleId`` 是 base64 形态、里面带 ``+`` / ``/`` / ``=``（实测
        ``F9vXJ+ZAylEIRdNeetiChQ==``），**必须整体百分号编码**：``+`` 在 query 里
        会被服务端读成空格，而 ``quote(safe='')`` 把它编成 ``%2B``。
        """
        article = str(row.get('articleId') or '').strip()
        if not article:
            return ''
        return f'{self.detail_page}?articleId={urllib.parse.quote(article, safe="")}'

    def detail_body(self, payload) -> str:
        """详情响应里的正文 HTML（``result.data.content``）。"""
        if isinstance(payload, (bytes, bytearray)):
            payload = payload.decode('utf-8', 'replace')
        data = json.loads(payload) if isinstance(payload, str) else (payload or {})
        if not isinstance(data, dict) or data.get('success') is False or 'result' not in data:
            raise ValueError('省级平台拒绝这次详情读取：'
                             + str((data or {}).get('error') if isinstance(data, dict) else '')[:200])
        content = ((data.get('result') or {}).get('data') or {}).get('content')
        if not isinstance(content, str) or not content.strip():
            raise ValueError('详情响应里没有正文（result.data.content 为空）')
        return content

    def announcement(self, row: Mapping[str, Any], url: str, content: str,
                     district: str) -> dict:
        """政采云内核的行字段（``publishDate`` 是毫秒、``districtName`` 是市县）。"""
        item = super().announcement(row, url, content, district)
        item['published_at'] = _released_at({'releasedAt': row.get('publishDate')})
        item['channel'] = str(row.get('pathName') or '').strip()
        return item

    def parse_response(self, payload, model: str, district: str) -> dict:
        """政采云内核的响应：``result.data.data`` 是行数组、``result.data.total`` 是总数。"""
        if isinstance(payload, (bytes, bytearray)):
            payload = payload.decode('utf-8', 'replace')
        data = json.loads(payload) if isinstance(payload, str) else (payload or {})
        if not isinstance(data, dict):
            raise ValueError('省级平台响应不是对象')
        if data.get('success') is False or 'result' not in data:
            raise ValueError('省级平台拒绝这次查询：' + str(data.get('error') or '响应缺少 result')[:200])
        result = (data.get('result') or {}).get('data') or {}
        if not isinstance(result, dict):
            raise ValueError('政采云内核的响应形状不对：result.data 不是对象')
        return self._parse_rows(result.get('total'), result.get('data') or [], model, district)

    def shape_text(self, body: str) -> str:
        return shape_lot_blocks(body)


PORTAL_PROVINCES: tuple = (
    PortalPlatform(
        key='zhejiang',
        label='浙江政府采购网·政采云（全站检索）',
        endpoint='https://zfcg.czt.zj.gov.cn/portal/all',
        list_page='https://zfcg.czt.zj.gov.cn/luban/category',
        detail_page='https://zfcg.czt.zj.gov.cn/portal/detail',
        # 这一格不是区划码：这个接口没有区划参数，见 PORTAL_SCOPE。
        districts=(PORTAL_SCOPE,),
    ),
    PortalPlatform(
        key='guangxi',
        label='广西政府采购网·政采云（全站检索）',
        endpoint='https://zfcg.gxzf.gov.cn/portal/all',
        list_page='https://zfcg.gxzf.gov.cn/luban/category',
        detail_page='https://zfcg.gxzf.gov.cn/portal/detail',
        districts=(PORTAL_SCOPE,),
    ),
)

#: 产品实际要查的平台全集（``province_award_platforms`` 读它）。
#: 两份注册表分开，是因为两个家族连请求 body 与响应形状都不一样；
#: 合并成一份会让"卖场家族的默认值"这个既有事实消失。
ALL_PROVINCES: tuple = PROVINCES + PORTAL_PROVINCES

_BY_KEY = {platform.key: platform for platform in ALL_PROVINCES}


def platform_for_url(url: str) -> ProvincePlatform | None:
    """按主机名认站（详情页 URL 也认）。

    ``zcygov.cn`` 下有多个省的电子卖场（``mall.<省>.zcygov.cn``），所以这里的
    **子域也要匹配**：只认注册表里登记过的那几个。
    """
    try:
        host = (urllib.parse.urlsplit(url or '').hostname or '').lower()
    except ValueError:
        return None
    for platform in ALL_PROVINCES:
        known = (urllib.parse.urlsplit(platform.endpoint).hostname or '').lower()
        if host == known or host.endswith('.' + known):
            return platform
    return None


def registered_hosts() -> set:
    return {(urllib.parse.urlsplit(platform.endpoint).hostname or '').lower()
            for platform in ALL_PROVINCES}


# ---------------------------------------------------------------------------
# 网络出口：**产品自己的公开网页读取器**，本模块不开第二个 HTTP 客户端
# ---------------------------------------------------------------------------

def request(url, *, body, headers, tls_profile=None, on_request=None,
            timeout=DEFAULT_TIMEOUT, transport=_TRANSPORT_UNSET, method='POST'):
    """一次低频公开读取。失败就抛，**不在被挡之后重试**。

    ``transport`` 是产品读取器给的出口（见
    ``procurement_research.province_award_transport``）。为 ``_TRANSPORT_UNSET`` 时用
    本模块自带的 ``urllib``——那是探测脚本的路径，**不是产品的路径**。传 ``None`` 表示
    "这一轮没有能发 POST 的读取器"，此时**一个请求都不发**。

    ``method='GET'`` 是给政采云内核那两站用的：列表查询是 POST，而
    ``GET /portal/detail?articleId=`` 回的是详情 JSON。**两条路都走同一个读取器**，
    不给这个模块开第二个 HTTP 客户端。

    为什么必须走产品读取器：自己开连接会绕过 ``network_url``、重定向逐跳校验、16MB
    上限，也会绕过测试注入的离线读取器——上一单就是这么让 8 个集成
    用例真去连站点并超时的。
    """
    if on_request is not None:
        on_request()
    host = (urllib.parse.urlsplit(url).hostname or '').lower()
    if transport is None:
        raise RuntimeError('本轮没有可用的公开网页读取器')
    if isinstance(body, (dict, list)):
        # 接口吃 JSON 对象。编码只做一次，两条路径（产品读取器 / 自带 urllib）拿到同一份字节。
        body = json.dumps(body, ensure_ascii=False).encode('utf-8')
    if transport is not _TRANSPORT_UNSET:
        with _request_lock(host):
            pace(host, MIN_INTERVAL)
            return transport(url, method=method, body=body, headers=headers,
                             tls_profile=tls_profile, timeout=timeout)
    payload = (None if method.upper() == 'GET'
               else body if isinstance(body, bytes) else str(body).encode('utf-8'))
    with _request_lock(host):
        pace(host, MIN_INTERVAL)
        from backend.infrastructure.search_providers import _tls_handlers
        req = urllib.request.Request(url, data=payload, headers=dict(headers or {}),
                                     method=method.upper())
        opener = urllib.request.build_opener(*_tls_handlers(tls_profile))
        with opener.open(req, timeout=timeout) as response:
            return response.read()


#: 万一教育平台通道那份按 host 的锁表改了名字，这里仍然逐站点串行。
_own_request_locks: dict = {}
_own_lock_guard = threading.Lock()


def _request_lock(host):
    """逐站点串行的那把锁：**与教育平台通道共用同一份表**。

    共用是刻意的：两个通道可能对同一台服务器发请求（江西站两边都查），各自维护一份
    "1 秒地板" 等于地板失效。这里只**读**教育平台模块里那张表，不改它一行；万一那边
    换了名字，就退回本模块自己的锁表，行为仍然是 "每站串行"，只是少了一层跨通道共用。
    """
    from backend.services import education_platform_sources as shared
    helper = getattr(shared, '_lock_for', None)
    table = getattr(shared, '_request_locks', None)
    if helper is None or not isinstance(table, dict):
        with _own_lock_guard:
            return _own_request_locks.setdefault(host, threading.Lock())
    return helper(table, host)


# ---------------------------------------------------------------------------
# 正文整形：恢复页面自己印着的列关系（只动表头，数据行一字不改）
# ---------------------------------------------------------------------------

MODEL_COLUMNS = ('规格型号', '型号规格', '型号', '货物型号', '规格/型号')
QUANTITY_COLUMNS = ('数量', '货物数量')
AMOUNT_COLUMNS = ('成交金额（元）', '成交金额(元)', '成交金额', '金额（元）', '金额(元)')
PRICE_HEADING = re.compile(r'(?:货物)?单价\s*[（(]\s*(?:元|万元|千元)')
TOTAL_AMOUNT = re.compile(r'总成交金额')
TOTAL_UNIT = re.compile(r'(万元|千元|元)')
TOTAL_NUMBER = re.compile(r'(\d[\d,]*(?:\.\d+)?)')


def _total_yuan(body: str) -> float | None:
    """页面自己写的成交总金额（换算成元）。

    两个细节都不能省：

    * **数字要取标记之后的那个**。同一行里常有别的数字（``成交日期: 2026年9月24日
      总成交金额（元）: 10640``），从行首找第一个数字会读成 2026。
    * **单位要跟着数字一起读**。安徽写 ``总成交金额（元）: 54940``，江西写
      ``总成交金额： 12.09 （万元）``——同一份江西公告里明细是元、汇总是万元
      ，照抄数字会差 10000 倍。
    """
    for line in (body or '').splitlines():
        marker = TOTAL_AMOUNT.search(line)
        if not marker:
            continue
        tail = line[marker.end():]
        number = TOTAL_NUMBER.search(tail)
        if not number:
            continue
        try:
            value = float(number.group(1).replace(',', ''))
        except ValueError:
            continue
        unit = TOTAL_UNIT.search(tail)
        factor = {'万元': 10000.0, '千元': 1000.0, '元': 1.0}.get(unit.group(1) if unit else '', None)
        if factor is None:
            continue
        return value * factor
    return None


def _number(cell: str) -> float | None:
    text = (cell or '').replace(',', '').strip()
    if not re.fullmatch(r'\d+(?:\.\d+)?', text):
        return None
    return float(text)


def _table_lines(lines: Sequence[str], index: int) -> list:
    """表头之后、下一张表之前的连续数据行（``(行号, 单元格)``）。"""
    rows = []
    for position in range(index + 1, len(lines)):
        line = lines[position]
        if '|' not in line:
            break
        cells = [cell.strip() for cell in line.split('|')]
        if any(cell in MODEL_COLUMNS for cell in cells) and any(
                PRICE_HEADING.search(cell) for cell in cells):
            break  # 下一张表的表头
        rows.append((position, cells))
    return rows


def shape_award_table(body: str) -> str:
    """把成交标的表的**尾列**按页面自己的样子收齐，让既有表格判据读得出来。

    这两站的表头比数据行多**一列**：安徽印
    ``序号 | 标的名称 | 品牌 | 规格型号 | 数量 | 单价(元) | 成交金额（元） | 报价明细``，
    而数据行只有 7 格。根因不在站点，是清洗链里 ``web_tables.render_table`` 拼行时对
    **每一行**做 ``rstrip(' |')``：数据行的 ``报价明细`` 格是空的，尾格被吃掉；表头那一格
    有文字、留了下来——于是 "表头 8 列、数据 7 列"。``price_table_rows`` 要求表头与数据行
    **列数完全相等**，整张表就一行都读不出来（实测同一批正文：江西读得出、安徽读不出；
    整形后安徽从 4 行变 77 行，见 ``_probe/p8_pipeline.py``）。

    为什么不去改 ``render_table`` / ``price_table_rows``（治本但面太大）：那两个是共享的、
    被对抗性用例钉住的模块，动 ``rstrip`` 会让**全局**所有含尾空列的表格重新匹配，
    采信结果跟着变。这里走 里 8093 那条同款路子：**只恢复页面自己印着的
    列关系，不改一个数**。门槛不是 "看着像"：

    * 只认表头**尾列**正好是配置里那种非数据列名（``报价明细``）的情况；
    * 该表头之后的**每一行**都必须正好少一列，一行不对就不动；
    * 还要**自校验**：按收齐后的列位置，把每一行的 ``单价 × 数量`` 加总，
      必须等于**页面自己写的** ``总成交金额``（同一页上的另一个数，独立于这些格子）；
      对不上就原样返回——宁可这一条读不出来，也不能把整包价当单价报出去。

    只改表头那一行；数据行逐字保留，所以报告里的引文仍旧是页面原文。
    """
    lines = (body or '').splitlines()
    for index, line in enumerate(lines):
        if '|' not in line:
            continue
        cells = [cell.strip() for cell in line.split('|')]
        if len(cells) < 3:
            continue
        if not any(cell in MODEL_COLUMNS for cell in cells):
            continue
        if not any(PRICE_HEADING.search(cell) for cell in cells):
            continue
        # 遍历**全集**而不是卖场家族：政采云内核那两站的 ``trailing_columns`` 是空元组
        # （它们的表没有"印了但不给值"的尾列），空元组永远匹配不上，所以这一改对
        # 安徽 / 江西的结果**没有影响**——加平台不该悄悄改既有两省的采信结果。
        for platform in ALL_PROVINCES:
            if not cells[-1] or cells[-1] not in platform.trailing_columns:
                continue
            rows = _table_lines(lines, index)
            if not rows or any(len(row) != len(cells) - 1 for _position, row in rows):
                continue
            shaped = cells[:-1]
            if not _totals_agree(shaped, [row for _position, row in rows], body):
                continue
            updated = list(lines)
            updated[index] = ' | '.join(shaped)
            return '\n'.join(updated)
    return body


def _totals_agree(header: Sequence[str], rows: Sequence[Sequence[str]], body: str) -> bool:
    """收齐后的列位置上，``Σ(单价 × 数量)`` 是否等于页面自己写的总成交金额。"""
    total = _total_yuan(body)
    if total is None:
        return False
    try:
        model_at = next(i for i, cell in enumerate(header) if cell in MODEL_COLUMNS)
        price_at = next(i for i, cell in enumerate(header) if PRICE_HEADING.search(cell))
        quantity_at = next(i for i, cell in enumerate(header) if cell in QUANTITY_COLUMNS)
        amount_at = next(i for i, cell in enumerate(header) if cell in AMOUNT_COLUMNS)
    except StopIteration:
        return False
    if len({model_at, price_at, quantity_at, amount_at}) != 4:
        return False
    running = 0.0
    for row in rows:
        price = _number(row[price_at])
        amount = _number(row[amount_at])
        if price is None:
            # 【运费】那一行单价是空的、成交金额是 0；照页面读就是 "这一行没有单价"。
            if amount in (None, 0.0):
                continue
            return False
        quantity = _number(row[quantity_at])
        if quantity is None or amount is None:
            return False
        running += price * quantity
    return abs(running - total) < 0.01


# 清洗链用产品自己那一条（``page_reader`` 也是这么调的），不另写一个 HTML 剥壳器。
def clean_content(content: str, url: str) -> str:
    from backend.infrastructure.search_providers import PublicWebSearchProvider
    text = PublicWebSearchProvider._clean_html(content or '', url)
    return shape_award_table(text)


def body_text(announcement: Mapping[str, Any]) -> str:
    """一条公告的正文文本。

    缓存里**只存平台给的 ``content`` 原文**（证据），正文文本每次由它重新推出来：
    这样清洗 / 整形规则的任何改动都会立刻作用到缓存过的公告上，不会有一份
    "用旧规则洗过的文本" 在库里悄悄留着。

    平台自己的整形（``ProvincePlatform.shape_text``）接在清洗链**之后**：
    卖场家族的默认实现是恒等，所以安徽 / 江西走的是原路径；政采云内核那两站
    多一道"把标项块铺成表"（:func:`shape_lot_blocks`），追加在正文末尾、原件不动。
    """
    text = clean_content(str(announcement.get('content') or ''),
                        str(announcement.get('url') or ''))
    platform = _BY_KEY.get(str(announcement.get('platform') or ''))
    return platform.shape_text(text) if platform is not None else text


# ---------------------------------------------------------------------------
# 政采云内核的正文整形：把"标项块"铺成表（**只搬运页面自己的字，不做算术**）
# ---------------------------------------------------------------------------

#: 标项块的分节标记。**浙江印**：每个标项前面一行 ``标项一`` … ``标项十五``。
LOT_MARKER = re.compile(r'^标项\s*([一二三四五六七八九十零〇百\d]+)\s*$')
#: **广西不印分节标记**：同一个 5 行组直接重复（实测正文见
#: ），所以没有 ``标项N`` 时
#: 用 ``主要标的名称`` 当分块起点。
LOT_NAME_START = re.compile(r'^主要标的名称\s*[:：]')
#: 块里的标签。**键名逐字是页面自己的措辞**，不是我们另起的列名。
#: 值一律捕到"下一个标签"或行尾为止，这样把两件事挤在一行的站点
#: （``规格型号（或服务要求）：品牌:佑科规格型号:FA2004``）也读得对。
LOT_LABELS = {
    'name': re.compile(r'^主要标的名称\s*[:：]\s*(.*?)\s*(?=(?:规格型号|品牌|数量|单价)\s*[:：]|$)'),
    'quantity': re.compile(r'^数量\s*[:：]\s*(.*?)\s*(?=(?:规格型号|品牌|数量|单价)\s*[:：]|$)'),
    'brand': re.compile(r'品牌\s*[:：]\s*(.*?)\s*(?=(?:规格型号|品牌|数量|单价)\s*[:：]|$)'),
    'model': re.compile(r'规格型号\s*[:：]\s*(.*?)\s*(?=(?:规格型号|品牌|数量|单价)\s*[:：]|$)'),
}
#: 单价那一格：**带单位的** ``单价（元）`` 才是价格列（列判据一条没放宽）。
#: 三段分别是：页面自己的列名原文 / 单位 / 值。
LOT_PRICE = re.compile(r'^(单价\s*[（(]\s*(元|万元|千元)\s*[）)])\s*[:：]\s*(.*)$')
#: 型号那一格的**退化写法**：不少公告不写 ``品牌:``、也不另起一行 ``规格型号:``，
#: 而是把型号写在 ``规格型号（或服务要求）： <值>`` 里（实测浙江 FA2004 两条正文就是
#: ``规格型号（或服务要求）： FA2004``，广西那条是 ``… 详见采购文件``）。
#: 值是 ``品牌:…`` 时不取（那是品牌，不是型号）——lookahead 会让它匹配成空串。
LOT_MODEL_FALLBACK = re.compile(
    r'规格型号\s*[（(]\s*或服务要求\s*[）)]\s*[:：]\s*'
    r'(.*?)\s*(?=(?:规格型号|品牌|数量|单价)\s*[:：]|$)')
#: 一个标项块**该有的**标签。``brand`` **不在**里面：实测有正文整块只印了
#: ``规格型号（或服务要求）： FA2004``（没有品牌行）——那一条的 4500.00 元是真的，
#: 不能因为"没印品牌"就丢掉；品牌格留空、由 ``validate_price`` 决定能不能采信。
LOT_REQUIRED = ('name', 'quantity', 'price', 'price_label', 'unit', 'model')


def _lot_line(line: str) -> dict:
    """读一行标项块里的内容，返回 ``{标签: 值}``；认不出来就是空字典。

    返回字典而不是单个键，是因为**站点会把品牌与型号印在同一行**
    （广西的片段就是 ``品牌:申安规格型号:LDZX``）。一行一个键的实现会把型号读丢，
    然后整块判成"不完整"、整条丢——那是我们自己造成的读不出来。
    """
    price = LOT_PRICE.match(line)
    if price:
        return {'price_label': price.group(1), 'unit': price.group(2),
                'price': price.group(3).strip()}
    found = {}
    for key, pattern in LOT_LABELS.items():
        match = pattern.search(line)
        if match and match.group(1).strip():
            found[key] = match.group(1).strip()
    fallback = LOT_MODEL_FALLBACK.search(line)
    if fallback and fallback.group(1).strip():
        found['model_fallback'] = fallback.group(1).strip()
    return found


def _lot_blocks(lines: Sequence[str]) -> list:
    """把标项块读成 ``[(起始行, 结束行, 块)]``；一条都读不出来就返回 ``[]``。

    **分块起点**：有 ``标项N`` 就用它（浙江），没有就用 ``主要标的名称``（广西）。
    两种形状都是实测的，不是猜的。

    **一个块读得完不完整，只影响那一条**（读不完整的块丢掉），不再"一条不完整就整份不铺"。
    这一条是实测逼出来的：广西那条 ``FA2004``（靶子 1750）的正文里第 6 个标项
    ``其他设备`` 只印了 ``规格型号（或服务要求）： 详见采购文件``，**既没有品牌也没有
    型号行**——按"整份不铺"的口径，这条真实公告连 1750 都读不出来。

    为什么放开是**安全**的：每个格子的值都只从**它自己那个块**里读，块与块之间不共享
    任何值，所以"块不完整"最坏的结果是**丢掉那一条**，不会把 A 标项的价配到 B 标项的
    型号上（那才是必须挡住的事）。仍然挡着的东西：

    * 同一块里同一种标签出现两次 → 丢掉这一条（说明块被读错了）；
    * 几个块的单价列名写法不一致 → 整份不铺（表头只能写一种）；
    * 印了 ``标项N`` 的页面里序号重复 → 整份不铺（说明分块读错了）。
    """
    marked = [index for index, line in enumerate(lines) if LOT_MARKER.match(line.strip())]
    if marked:
        starts, kind = marked, 'ordinal'
    else:
        starts = [index for index, line in enumerate(lines)
                  if LOT_NAME_START.match(line.strip())]
        kind = 'name'
    if not starts:
        return []
    found = []
    for position, start in enumerate(starts):
        stop = starts[position + 1] if position + 1 < len(starts) else len(lines)
        block, cursor = {'_kind': kind}, start
        marker = LOT_MARKER.match(lines[start].strip())
        if marker:
            block['ordinal'] = marker.group(1)
            cursor = start + 1
        while cursor < stop:
            fields = _lot_line(lines[cursor].strip())
            if not fields:
                break               # 这一块到此为止（后面的行不属于它）
            for key, value in fields.items():
                if key in block:
                    block[key + '_twice'] = True
                block.setdefault(key, value)
            cursor += 1
        # 退化的型号写法只在**没有** ``规格型号:`` 那一行时才用（优先级低，不能顶掉它）
        if not block.get('model') and block.get('model_fallback'):
            block['model'] = block['model_fallback']
        found.append((start, cursor, block))
    usable = [(start, stop, block) for start, stop, block in found
              if not any(block.get(key + '_twice') for key in LOT_REQUIRED)
              and all(block.get(key) for key in LOT_REQUIRED)]
    if not usable:
        return []
    if len({block['price_label'] for _s, _e, block in usable}) != 1:
        return []
    if kind == 'ordinal' and len({block['ordinal'] for _s, _e, block in found}) != len(found):
        return []
    return usable


def shape_lot_blocks(body: str) -> str:
    """把政采云内核的"标项块"正文铺成产品认的表，**追加**在正文末尾。

    为什么需要它：浙江 / 广西的合同公告把每个标项印成
    ``标项三 / 主要标的名称：… / 数量：… / 单价（元）：… / 规格型号（或服务要求）：品牌:… /
    规格型号:…`` 若干行，**整份正文一个 ``<table>`` 都没有**。既有的
    ``price_table_rows`` 要求"表头行 + 行列数完全相等的数据行"，在这份正文上读出
    **0 行**——单价印在那里却读不出来（实测某型号那条：正文里就有
    ``单价（元）： 12000.00``，``price_table_rows`` 返回 0 行）。

    **两种实测形状都要认**（见 :data:`LOT_MARKER` / :data:`LOT_NAME_START`）：
    浙江每个标项前印一行 ``标项N``，广西**不印**、同一个 5 行组直接重复。

    **不改一个数、不做一次算术**：表头每一格都是页面自己印的标签（``主要标的名称``
    / ``品牌`` / ``规格型号`` / ``数量`` / ``单价（元）``），单元格是页面自己的值，
    逐字搬运（只去首尾空白）。铺出来的表**追加**在末尾，正文原件一字不动，所以
    引文与字符偏移照旧能回溯到原文。

    ⚠️ **为什么自校验是结构性的、不是算术的**：浙江那条正文印着自己的
    ``2.合同金额（元）： 506000.00``，而 15 个标项的 ``单价 × 数量`` 加总是
    **563600.00**，两者**对不上**；广西那条的合同金额与加总**对得上**
    （258418.00，两个都实测，见报告）。照 ``GuangdongEduGovernmentPlatform`` 那种
    "算出来必须等于页面印的总价"的闸门，浙江那份真实公告会一条都读不出来。
    所以这里的门槛是**结构性的**（分块读得对不对、每格的标签齐不齐），
    而不是替页面判断金额该怎么算。金额能不能当单价，仍旧由
    ``price_table_rows`` / ``validate_price`` 判。

    一个**已知的形状边界**：铺出来的行里，型号格与单价格来自同一块（页面的分组），
    所以"这个价属于这个型号"是页面的分组说了算；型号串本身仍要过
    ``contains_model``，单价仍要过 ``validate_price``。
    """
    lines = (body or '').splitlines()
    blocks = _lot_blocks(lines)
    if not blocks:
        return body
    label = blocks[0][2]['price_label']
    header = ' | '.join(['主要标的名称', '品牌', '规格型号', '数量', label])
    rows = [' | '.join([block.get('name', ''), block.get('brand', ''),
                        block.get('model', ''), block.get('quantity', ''),
                        block.get('price', '')]) for _s, _e, block in blocks]
    return body.rstrip('\n') + '\n' + header + '\n' + '\n'.join(rows) + '\n'


# ---------------------------------------------------------------------------
# 响应解析
# ---------------------------------------------------------------------------

def _released_at(row: Mapping[str, Any]) -> str:
    """``releasedAt`` 是毫秒时间戳；拿不到就留空，不猜。"""
    value = row.get('releasedAt')
    if isinstance(value, (int, float)) and value > 0:
        seconds = value / 1000.0 if value > 10 ** 11 else float(value)
        try:
            return datetime.fromtimestamp(seconds, timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            return ''
    return ''


def parse_response(platform: ProvincePlatform, payload, model: str, district: str) -> dict:
    """把一次查询的响应解析成 ``{'total':…, 'announcements':[…], 'rows':…}``。

    解析规则**按平台自己走**（:meth:`ProvincePlatform.parse_response`）：卖场家族与
    政采云内核家族的响应形状不同（``result.data`` 是数组 vs 对象），
    这条函数保留原来的签名，是因为既有用例与调用方都按这个签名调它。
    """
    return platform.parse_response(payload, model, district)


# ---------------------------------------------------------------------------
# 跨任务缓存
# ---------------------------------------------------------------------------

def cache_key(platform_key: str, district: str, model: str) -> str:
    """缓存键 = ``(缓存版本, 平台, 区划, 型号)`` 的摘要。

    **键里必须有区划**：省本级与市县的公告集合互斥，
    拿省本级的结果回答 "全省" 就是错。以后加区划，各格各一条键，不会互相污染。
    型号按**去空白后的原串**入键——接口的匹配是完整 token，``TG16-WS`` 与
    ``TG16WS`` 不是一回事，大小写是否同义**没验过**，
    所以不合并、不归一化：宁可多打一次网络，不肯让两个不同的串共用一个答案。
    """
    raw = json.dumps([CACHE_VERSION, platform_key, district, model],
                     ensure_ascii=False, sort_keys=True)
    return 'province-award-' + hashlib.sha256(raw.encode('utf-8')).hexdigest()


def read_cache(repo, key: str, *, now=None, ttl: int = CACHE_TTL_SECONDS):
    """读一条未过期的缓存；没有 / 过期 / 版本不对都返回 ``None``。"""
    record = repo.get(CACHE_TABLE, key) if repo is not None else None
    if not isinstance(record, dict) or record.get('cache_version') != CACHE_VERSION:
        return None
    stamp = record.get('queried_at')
    if not isinstance(stamp, str):
        return None
    try:
        age = ((now or datetime.now(timezone.utc)) - datetime.fromisoformat(stamp)).total_seconds()
    except ValueError:
        return None
    if age < 0 or age > ttl:
        return None
    return record


def write_cache(repo, key: str, *, task_id, platform_key, district, model,
                announcements, total, rows, rejected, now=None) -> None:
    """把这一次查询的**证据**写进缓存（跨任务可读）。

    存的是公告 URL + 平台自己给的正文 HTML + 元数据，**不存价格、不存采信结论**：
    每次运行仍旧从正文重新过一遍既有判据，所以缓存过的公告不可能绕过任何校验。

    落盘前过一遍号码掩码（``mask_landed_text``）：缓存是**跨任务**可读的，写脏了
    就等于把真号散到后面每一个任务里。掩码放在这里而不是 ``parse_response`` 里，
    理由有两条：① 缓存与 ``run['sources']`` 看到的是同一份内容，掩在源头不会两边
    不一致；② 命中缓存的旧记录在 ``province_award_record`` 那一支还会再掩一次
    （掩码幂等），所以修复之前写进去的脏缓存也不会漏网。
    """
    if repo is None:
        return
    masked = []
    for item in announcements:
        item = dict(item)
        if isinstance(item.get('content'), str):
            item['content'] = mask_landed_text(item['content'])
        masked.append(item)
    repo.put(CACHE_TABLE, key, {
        'id': key, 'kind': 'province_award_cache', 'task_id': task_id,
        'cache_version': CACHE_VERSION,
        'queried_at': (now or datetime.now(timezone.utc)).isoformat(),
        'platform': platform_key, 'district': district, 'model': model,
        'total': total, 'rows': rows, 'rejected': rejected,
        'announcements': masked,
    }, task_id=task_id)


# ---------------------------------------------------------------------------
# 查询入口
# ---------------------------------------------------------------------------

def lookup(repo, target, *, transport, task_id, platforms: Iterable[ProvincePlatform] = PROVINCES,
           on_request=None, timeout=DEFAULT_TIMEOUT, now=None,
           ttl: int = CACHE_TTL_SECONDS) -> dict:
    """按型号在两站（各自的省本级）查一遍；命中缓存就不打网络。

    返回 ``{'model', 'districts', 'announcements', 'queries', 'failures'}``。

    * ``queries`` 里每一项都带 ``source``：``'cache'`` / ``'network'`` / ``'quota'``，
      以及 ``hits``。**"查过了、没有" 与 "根本没查成" 必须能分开**：前者在这里是一条
      ``hits=0`` 的正常记录，后者是 ``failures`` 里的一条。
    * ``failures`` 里是没查成的原因（网络、TLS、站点拒绝、解析）。调用方**必须**把
      它说出来——把 "没查" 说成 "没有" 才是真骗人。

    额度用完（``QuotaReached``）安静收手：这是额度，不是错误。
    """
    model = str((target or {}).get('model') or '').strip()
    result = {'model': model, 'announcements': [], 'queries': [], 'failures': [],
              'districts': {}}
    if not model:
        # 没有型号就没有可查的东西：这一条不算 "查过没有"，也不算失败。
        for platform in platforms:
            result['districts'].setdefault(platform.key, list(platform.districts))
        return result
    for platform in platforms:
        result['districts'][platform.key] = list(platform.districts)
        for district in platform.districts:
            key = cache_key(platform.key, district, model)
            cached = read_cache(repo, key, now=now, ttl=ttl)
            if cached is not None:
                announcements = [dict(item) for item in (cached.get('announcements') or [])]
                for item in announcements:
                    item['cache_hit'] = True
                result['queries'].append({
                    'platform': platform.key, 'district': district, 'source': 'cache',
                    'hits': len(announcements), 'total': cached.get('total'),
                    'queried_at': cached.get('queried_at'),
                })
                result['announcements'].extend(announcements)
                continue
            try:
                payload = request(platform.endpoint, body=platform.query_body(district, model),
                                  headers=platform.request_headers(),
                                  tls_profile=platform.tls_profile,
                                  on_request=on_request, timeout=timeout,
                                  transport=transport)
            except QuotaReached:
                result['queries'].append({'platform': platform.key, 'district': district,
                                          'source': 'quota', 'hits': 0})
                return result
            except Exception as exc:  # noqa: BLE001 — 网络/被挡/解析都在这里如实记一条
                result['failures'].append({
                    'platform': platform.key, 'district': district,
                    'code': type(exc).__name__, 'message': str(exc)[:300]})
                continue
            try:
                parsed = parse_response(platform, payload, model, district)
            except Exception as exc:  # noqa: BLE001
                result['failures'].append({
                    'platform': platform.key, 'district': district,
                    'code': 'parse_failed', 'message': f'{type(exc).__name__}: {str(exc)[:280]}'})
                continue
            written = [dict(item) for item in parsed['announcements']]
            failed_details = 0
            if platform.detail_limit > 0 and written:
                # 列表响应里只有"命中片段"，正文得逐条回读详情页。这一步的**三条规矩**：
                #
                # 1. **先便宜后贵**：按"片段里带不带价格列名"排序。那是页面自己的字，
                #    比"公告类型"更贴近"这一条有没有单价"（见报告"公告类型"一节），
                #    而且不花任何请求。
                # 2. **有上限**：``detail_limit`` 是成本上限，不是"只有这么几条"的结论；
                #    没读到的行数记进 ``queries.detail_skipped``，看得见。
                # 3. **读不到就如实记**：任意一条详情失败都进 ``failures``，并且
                #    **这一格不写缓存**——把"没读成"缓存 7 天，等于把一次网络抖动
                #    变成一周的"这个型号没有"。
                try:
                    written, failed_details = _hydrate_details(
                        platform, written, district=district, transport=transport,
                        on_request=on_request, timeout=timeout)
                except QuotaReached:
                    # 详情读了一半额度用完：与列表查询那一段**同一条规矩**——
                    # 安静收手、如实记一条 ``quota``、**这一格不写缓存**。
                    result['queries'].append({'platform': platform.key, 'district': district,
                                              'source': 'quota', 'hits': 0})
                    return result
                for _ in range(failed_details):
                    result['failures'].append({
                        'platform': platform.key, 'district': district,
                        'code': 'detail_failed',
                        'message': '详情页正文未读到（该条公告只有命中片段，不进来源）'})
            if failed_details == 0:
                write_cache(repo, key, task_id=task_id, platform_key=platform.key,
                            district=district, model=model, announcements=written,
                            total=parsed['total'], rows=parsed['rows'],
                            rejected=parsed['rejected'], now=now)
            record = {
                'platform': platform.key, 'district': district, 'source': 'network',
                'hits': len(written), 'total': parsed['total'],
                'rows': parsed['rows'], 'rejected': parsed['rejected'],
            }
            if platform.detail_limit > 0:
                # 详情回读的三个计数**只在这个家族出现**：卖场家族的记录因此逐字保持原样
                # （既有用例钉着那条记录的字段集合，而它们测的正是卖场家族）。
                record.update({
                    'detail_fetched': sum(1 for item in written if item.get('detail_fetched')),
                    'detail_failed': failed_details,
                    'detail_skipped': max(0, len(parsed['announcements'])
                                          - sum(1 for item in written if item.get('detail_fetched'))
                                          - failed_details),
                })
            result['queries'].append(record)
            result['announcements'].extend(written)
    return result


def _detail_priority(announcement: Mapping[str, Any]) -> int:
    """排序键：片段里印着价格列名的排前面。

    **不按公告类型筛**：类型只是"这条公告是什么"的名字，而片段里那个 ``单价（元）``
    是页面自己印的内容。实测浙江 6 条 ``DHG-9070A`` 里 4 条带价格列名，
    用内容排序一次就选中它们；用类型筛则要先赌"哪个类型码才有价"（见报告）。
    """
    excerpt = clean_content(str(announcement.get('content') or ''),
                            str(announcement.get('url') or ''))
    return 0 if PRICE_HEADING.search(excerpt) else 1


def _ordered_for_detail(announcements: list) -> list:
    """先按发布时间倒序，再**稳定**地按"片段里有没有价格列名"分组。"""
    newest = sorted(announcements, key=lambda item: item.get('published_at') or '',
                    reverse=True)
    return sorted(newest, key=_detail_priority)


def _hydrate_details(platform: ProvincePlatform, announcements: list, *, district,
                     transport, on_request, timeout) -> tuple:
    """回读详情页，把正文换进 ``announcement['content']``。

    返回 ``(留下来的公告, 失败条数)``。

    * **读不到正文的公告不进结果**：它只剩一段 120 字符的命中片段，既没有结论性措辞
      也没有表，留在 ``run['sources']`` 里只是噪声；但它**不是静默消失**——条数由
      调用方记成一条失败（``detail_failed``），并且**这一格不写缓存**。
    * **额度用完让它抛**（``QuotaReached``）：调用方照列表查询那一段的同一条规矩
      "安静收手 + 不写缓存"。写一份"一半是片段"的缓存等于把没读成当成读过了。
    """
    ordered = _ordered_for_detail(announcements)
    picked, spare = ordered[:platform.detail_limit], list(ordered[platform.detail_limit:])
    harvested, failed = [], 0
    for announcement in picked:
        try:
            payload = request(announcement['url'], body=None, headers=None,
                              tls_profile=platform.tls_profile, on_request=on_request,
                              timeout=timeout, transport=transport, method='GET')
            body = platform.detail_body(payload)
        except QuotaReached:
            raise
        except Exception:  # noqa: BLE001 — 任何一条读不到都只算一条失败，不重试
            failed += 1
            continue
        item = dict(announcement)
        item['content'] = body
        item['detail_fetched'] = True
        harvested.append(item)
    return harvested + spare, failed


def source_records(lookup_result: Mapping[str, Any], *, limit: int = MAX_SOURCES) -> list:
    """把查询结果变成 "可以直接进 ``run['sources']`` 的公告"（最多 ``limit`` 条）。

    按平台轮转取（与 ``education_platform_sources`` 的 ``_balanced_selection`` 同一条
    理由）：顺序取的话，第一个平台命中十几条时第二个平台一条都进不来，看起来就像
    "江西没数据"——那是名额分配造成的假象。站内已经按发布时间倒序。
    """
    grouped: dict = {}
    order: list = []
    for item in lookup_result.get('announcements') or []:
        key = item.get('platform')
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(item)
    chosen, index = [], 0
    while len(chosen) < limit:
        progressed = False
        for key in order:
            items = grouped[key]
            if index < len(items):
                chosen.append(items[index])
                progressed = True
                if len(chosen) >= limit:
                    break
        if not progressed:
            break
        index += 1
    return chosen
