"""小零易购（``gdegp.gds.edu.cn:8093``）成交明细本地库：**纯规则**解析与建库。

这个模块只做一件事：把结果公示的列表页与详情页，按页面自己印着的表格列关系
读成「一行商品 = 一条成交明细」，落进一个本地 SQLite。**全程正则/规则，不调用
任何模型。**

设计上不复用 ``education_platform_sources`` 里那条"喂给采信判据"的路径来做解析，
而是**独立**地按 HTML 单元格解析同一张表；两条路径的结果在 ``cross_check`` 里
逐单比对（``型号``/``单价``/``数量`` 三元组必须一致），所以"两套彼此独立的解析
给出同一个数"这件事在这个库里是**每次建库都跑一遍**的，不是一次性结论。

三件事必须说清楚，写在代码里也写在文档里：

* **「最高限价」永远不是成交价**。反拍页的成交单价只有「中标价」一列；
  实测同一行限价 28656 / 中标价 15000，取错列会系统性高估。
* **直购单（协议价格 = 0）的成交价就是商品目录挂牌价**，性质与议价折扣不同，
  单独标成 ``price_kind='list_price_direct'``，不与议价单混。
* **个人信息一格都不落盘**。落盘前先跑产品自己的 ``redact_payload``，再跑本模块
  的 ``pii_residue`` 闸门；闸门有命中就**不写这一页**并记一条缺口。
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import date, datetime
from html import unescape
from pathlib import Path

from backend.services.education_platform_sources import (
    GuangdongXiaolingyigouPlatform, redact_detail)

PLATFORM = GuangdongXiaolingyigouPlatform()
PLATFORM_KEY = 'xlyg'

SCHEMA_PATH = Path(__file__).with_name('xlyg_schema.sql')

#: 探测／抓取用的品类词。词表从最初 10 个入选品类出发，再加同义/上位词。
PROBE_KEYWORDS = (
    '离心机', '台式离心机', '高速离心机', '冷冻离心机',
    '纯水机', '超纯水机',
    '磁力搅拌器',
    '电子天平',
    '马弗炉', '箱式电阻炉',
    'pH计', '酸度计',
    '电热鼓风干燥箱', '干燥箱',
    '超声波清洗机',
    '紫外可见分光光度计', '分光光度计',
    '旋转蒸发仪',
    '灭菌器', '灭菌锅',
    '生物安全柜',
    '培养箱',
    '水浴锅',
    '振荡器',
    '匀浆机',
    '色谱',
    '移液器',
    '显微镜',
    '超低温冰箱',
)
#: 第二轮补的词：第一轮实抓 1481 行、离 1,500 差 19 行，补这几个词既补行数也补
#: 类别覆盖。先按 84 个月窗口探过量（见 ），
#: 粘度计 0 条、浊度计 3 条且全为终止单，没进词表。
PROBE_KEYWORDS = PROBE_KEYWORDS + (
    '酶标仪', '超净工作台', '通风柜', '冷冻干燥机', '真空泵',
    '水分测定仪', '电导率仪', '消解仪', '熔点仪', '氮吹仪',
)

#: 品类词 → 类别名。多个词落在一个类别里，是为了让"类别数"按**仪器类别**数
#: 而不是按**检索词**数。
CATEGORY_OF = {
    '离心机': '离心机', '台式离心机': '离心机', '高速离心机': '离心机', '冷冻离心机': '离心机',
    '纯水机': '纯水机/超纯水机', '超纯水机': '纯水机/超纯水机',
    '磁力搅拌器': '磁力搅拌器',
    '电子天平': '电子天平',
    '马弗炉': '马弗炉/箱式电阻炉', '箱式电阻炉': '马弗炉/箱式电阻炉',
    'pH计': 'pH计/酸度计', '酸度计': 'pH计/酸度计',
    '电热鼓风干燥箱': '电热鼓风干燥箱', '干燥箱': '电热鼓风干燥箱',
    '超声波清洗机': '超声波清洗机',
    '紫外可见分光光度计': '紫外可见分光光度计', '分光光度计': '紫外可见分光光度计',
    '旋转蒸发仪': '旋转蒸发仪',
    '灭菌器': '灭菌器/灭菌锅', '灭菌锅': '灭菌器/灭菌锅',
    '生物安全柜': '生物安全柜',
    '培养箱': '培养箱',
    '水浴锅': '水浴锅',
    '振荡器': '振荡器/摇床',
    '匀浆机': '匀浆机',
    '色谱': '色谱仪',
    '移液器': '移液器',
    '显微镜': '显微镜',
    '超低温冰箱': '超低温冰箱',
    '酶标仪': '酶标仪',
    '超净工作台': '超净工作台',
    '通风柜': '通风柜',
    '冷冻干燥机': '冷冻干燥机',
    '真空泵': '真空泵',
    '水分测定仪': '样品前处理',
    '消解仪': '样品前处理',
    '氮吹仪': '样品前处理',
    '电导率仪': '电导率仪',
    '熔点仪': '熔点仪',
}

#: 列表页「状态」列里说明**这单没有真实成交**的词。这些单一行价格都不产出。
TERMINAL_STATE_WORDS = ('终止', '取消', '重新竞价', '废标', '流标', '失败', '作废', '未成交')

#: 型号列里这些值不是型号。
PLACEHOLDER_MODELS = frozenset(
    value.lower() for value in
    ('', '-', '—', '/', '无', '其它', '其他', '见附件', '详见附件', '见清单', '详见清单',
     '无型号', '不适用', 'N/A', 'none', 'null', '待定', '标配'))

_ORDER_TEMPLATE = 'OrderResultPublicityView.aspx'
_BILL_TEMPLATE = 'ResultPublicityView.aspx'

# --- HTML 小工具（与产品同构：属性值里的 ``>`` 不是标签结束） --------------------
_TAG_SHAPE = r'(?:[^>"\']|"[^"]*"|\'[^\']*\')*'
_LIST_ROW = re.compile(
    r"<div class=\"list-content\"[^>]*?\scode='([^']*)'[^>]*?\scodeType='([^']*)'"
    r"[^>]*?\scode1='([^']*)'[^>]*?code2\s*=\s*'([^']*)'[^>]*?>(.*?)</div>\s*</div>", re.S)
_DIV_CELL = re.compile(r'<div[^>]*>(.*?)</div>', re.S)
_TABLE = re.compile(r'(?is)<table\b' + _TAG_SHAPE + r'>(.*?)</table\s*>')
_TR = re.compile(r'(?is)<tr\b' + _TAG_SHAPE + r'>(.*?)</tr\s*>')
_TD = re.compile(r'(?is)<t[dh]\b' + _TAG_SHAPE + r'>(.*?)</t[dh]\s*>')
_TAG = re.compile(r'(?is)<' + _TAG_SHAPE + r'>')
_PAGES = re.compile(r'ANP_checkInput\([^,]*,\s*(\d+)\s*\)')
#: 分页控件自己印的总数：「记录总数：<font…><b>640</b></font> 总页数：<font…><b>64</b></font>
#: 当前页：<font…><b>1</b></font>」。
#:
#: ⚠️ **别拿 ``ANP_checkInput(bid,mv)`` 里的 ``mv`` 当总页数**。实测：同一个查询的分页
#: 控件印着「总页数：64」，而 ``ANP_checkInput`` 的 ``mv`` 是 **6**，两个数对不上（``mv``
#: 到底代表什么我没验证到）。所以页数只认控件文字，``_PAGES`` 只在文字缺失时兜底
#: （实测那条分支从没走到过）。用 ``ANP_checkInput(...,78)`` 估过"78 页 ×
#: 15 条"，它自己标了"推断值"；要确切值就读控件文字。
_TOTALS = re.compile(
    r'记录总数：\s*(?:<[^>]*>\s*)*(\d+)(?:.*?总页数：\s*(?:<[^>]*>\s*)*(\d+))?', re.S)
_CURRENT = re.compile(r'当前页：\s*(?:<[^>]*>\s*)*(\d+)')
_MODES = ('直购', '议价', '竞价', '反拍', '电子反拍', '询价', '比选')
_BREADCRUMB = re.compile(r'(' + '|'.join(_MODES) + r')\s*(?:订单详情|结果详情)')
_SUPPLIER = re.compile(r'(?:中标供应商|供应商|成交供应商)\s*[:：]\s*\|\s*([^\n|]+)')
_ORDER_NO = re.compile(r'\b((?:DD|CM)\d{10,})\b')

#: 表头里认出的列名 → 落库字段名。
_COLUMN_FIELDS = {'商品名称': 'goods_name', '产品名称': 'product_name',
                  '品牌': 'brand', '型号规格': 'model'}


def _text(raw):
    return re.sub(r'\s+', ' ', unescape(_TAG.sub(' ', raw or ''))).strip()


def list_window(today, months=24):
    """时间窗 ``(start, end)``，``YYYY-MM-DD``。列表页吃 ``txtStateDate`` / ``endStateDate``。"""
    end = date(today.year, today.month, today.day)
    year, month = end.year, end.month - months
    while month <= 0:
        month += 12
        year -= 1
    return date(year, month, 1).isoformat(), end.isoformat()


# ---------------------------------------------------------------------------
# 列表页
# ---------------------------------------------------------------------------

def page_totals(html):
    """分页控件自己印的 ``{'records','pages','current'}``；印不全的项是 ``None``。"""
    found = _TOTALS.search(html or '')
    totals = {'records': int(found.group(1)) if found else None,
              'pages': int(found.group(2)) if (found and found.group(2)) else None,
              'current': None}
    current = _CURRENT.search(html or '')
    if current:
        totals['current'] = int(current.group(1))
    return totals


def parse_list_page(html):
    """列表页 → ``(rows, total_pages)``。

    每行六列：订单编号／项目名称／采购单位／成交单位／成交时间／状态。
    ``codeType`` 与 ``code2`` 就在行属性里。

    页数优先取分页控件印着的「总页数」；没有才退回 ``ANP_checkInput`` 的 ``mv``
    ——那个数**不是**总页数（见 ``_TOTALS`` 的注释），只是最后的兜底。
    """
    rows = []
    for code, code_type, code1, code2, inner in _LIST_ROW.findall(html or ''):
        # 行正则的 ``(.*?)</div>\s*</div>`` 会在**最后一格的闭合 ``</div>`` 之前**收尾，
        # 所以 ``inner`` 里的最后一个 ``<div>`` 是没闭合的。补一个闭合标签，六列才齐
        # （订单编号／项目名称／采购单位／成交单位／成交时间／状态）。
        cells = [cell for cell in (_text(part) for part in _DIV_CELL.findall(inner + '</div>'))
                 if cell]
        if len(cells) < 6:
            continue
        rows.append({
            'bill_guid': code, 'code_type': code_type, 'code1': code1, 'code2': code2,
            'order_no': cells[0], 'project_name': cells[1], 'purchaser': cells[2],
            'supplier': cells[3], 'deal_time': cells[4], 'state': cells[5],
        })
    totals = page_totals(html)
    if totals['pages'] is None:
        found = _PAGES.search(html or '')
        totals['pages'] = int(found.group(1)) if found else 1
    return rows, totals['pages']


def detail_template(code_type, code2):
    """按 ``codeType`` / ``code2`` 选详情页模板；**认不出来返回 ``None``**。

    判据与产品自己的 ``_detail_template`` 是同一个：服务类（``code2=1``）那两套
    FW 模板没有样本、没验证过，所以**不猜**——返回 ``None``、不发这条请求。
    """
    return PLATFORM._detail_template(code_type, code2)


def detail_url(row):
    template = detail_template(row['code_type'], row['code2'])
    if template is None:
        return None
    return PLATFORM.detail_url.format(template=template, id=row['bill_guid'],
                                      kind=row['code_type'], code=row['code1'])


def is_terminal_state(state):
    return any(word in (state or '') for word in TERMINAL_STATE_WORDS)


def normalize_deal_time(raw):
    """``2026/9/23 14:24:58`` → ``('2026-09-23', '2026-09-23 14:24:58')``。"""
    found = re.match(r'(\d{4})[/\-.](\d{1,2})[/\-.](\d{1,2})(?:\s+(\d{1,2}:\d{2}(?::\d{2})?))?',
                     (raw or '').strip())
    if not found:
        return '', ''
    stamp = f'{int(found.group(1)):04d}-{int(found.group(2)):02d}-{int(found.group(3)):02d}'
    return stamp, (f'{stamp} {found.group(4)}' if found.group(4) else stamp)


def order_mode(cleaned_body):
    """这一单是什么单据：直购／议价／竞价／反拍。页面面包屑自己印着。"""
    found = _BREADCRUMB.search(cleaned_body or '')
    return found.group(1) if found else ''


# ---------------------------------------------------------------------------
# 详情页
# ---------------------------------------------------------------------------

def goods_table(html):
    """详情页商品信息表 → ``(header, rows)``；认不出表返回 ``(None, [])``。

    判据与产品一致：表头里同时有「型号规格」和任一个价格列名
    （``金额（元）``/``协议价格（元）``/``中标价``）。
    """
    for table in _TABLE.findall(html or ''):
        header, rows = None, []
        for tr in _TR.findall(table):
            cells = [_text(cell) for cell in _TD.findall(tr)]
            if not any(cells):
                continue
            if header is None:
                if '型号规格' in cells and any(
                        name in cells for name in PLATFORM._ALL_PRICE_COLUMNS):
                    header = cells
                continue
            if len(cells) > len(header):
                # 比表头还宽的行属于别的表（例如更长的报价明细），不是本表的行。
                continue
            rows.append(cells + [''] * (len(header) - len(cells)))
        if header is not None:
            return header, rows
    return None, []


def select_prices(header, rows, total):
    """整单一次判断：选哪一列是成交单价。

    规则与产品的 ``_quote_rows`` 同一套（**订单级自校验**：Σ单价×数量 必须等于
    整单金额），只是这里保留"这一格是从哪一列读的"，因为直购单与议价单的性质不同。
    反拍页走 ``_BILL_PRICE_COLUMNS``（只有「中标价」）——**「最高限价」不在候选里**。

    返回 ``[(row_index, column, unit_price), …]``；认不出或对不上返回 ``None``。
    """
    def at(name):
        return header.index(name) if name in header else None

    model_at, quantity_at = at('型号规格'), at('数量')
    if model_at is None or quantity_at is None or total is None:
        return None
    orders = ((PLATFORM._BILL_PRICE_COLUMNS,) if '中标价' in header
              else (PLATFORM._PRICE_COLUMNS, tuple(reversed(PLATFORM._PRICE_COLUMNS))))
    for columns in orders:
        picked, summed = [], 0.0
        for index, cells in enumerate(rows):
            model = cells[model_at].strip()
            if not model:
                continue
            try:
                quantity = float(cells[quantity_at].replace(',', ''))
            except ValueError:
                picked = []
                break
            if quantity <= 0:
                picked = []
                break
            chosen = None
            for column in columns:
                where = at(column)
                if where is None:
                    continue
                raw = cells[where].replace(',', '')
                if re.fullmatch(r'\d+(?:\.\d+)?', raw) and float(raw) > 0:
                    chosen = (column, float(raw))
                    break
            if chosen is None:
                picked = []
                break
            picked.append((index, chosen[0], chosen[1]))
            summed += chosen[1] * quantity
        if picked and abs(summed - total) <= 0.01:
            return picked
    return None


def award_total(cleaned_body):
    """页面自己印着的整单成交金额；没有返回 ``None``。复用产品那一套判据。"""
    return PLATFORM._award_total(cleaned_body or '')


def shape_body(redacted_html, url=''):
    """产品自己的清洗链 + 8093 整形 → ``(清洗正文, 整形正文)``。"""
    from backend.infrastructure.search_providers import PublicWebSearchProvider
    payload = redacted_html.encode('utf-8') if isinstance(redacted_html, str) else redacted_html
    body = PublicWebSearchProvider.decode_body(payload, 'text/html', url)
    return body, PLATFORM.shape_text(body, payload)


def supplier_from_body(cleaned_body):
    found = _SUPPLIER.search(cleaned_body or '')
    return found.group(1).strip() if found else ''


def order_no_from_body(cleaned_body):
    found = _ORDER_NO.search(cleaned_body or '')
    return found.group(1) if found else ''


# --- 隐私闸门 -----------------------------------------------------------------

_PII_PATTERNS = (
    # 手机号要**左右设边界**：站点自己的订单号 CM20218010100000 里藏着 18010100000
    # 这种 11 位串，不设边界会把订单号本身判成手机号，整批页面被闸门拦掉。
    (re.compile(r'(?<![0-9A-Za-z])1[3-9]\d{9}(?![0-9A-Za-z])'), '手机号'),
    (re.compile(r'(?<![0-9A-Za-z])\d{17}[\dXx](?![0-9A-Za-z])'), '身份证/税号'),
    (re.compile(r'(联系人|联系电话|联系地址|收货人|开票信息|纳税人识别号|开户行|银行账号)'
                r'\s*[:：]\s*\S'), '个人信息字段带值'),
    (re.compile(r'[\w.+-]+@[\w-]+\.[\w.]{2,}'), '邮箱'),
)


def pii_residue(text):
    """落盘前最后一道闸门：返回命中的类别列表，空表示干净。"""
    return [label for pattern, label in _PII_PATTERNS if pattern.search(text or '')]


def redact(html_bytes):
    """产品自己的脱敏钩子 → ``(脱敏文本, 残留命中)``。"""
    redacted = redact_detail(PLATFORM_KEY, html_bytes)
    if isinstance(redacted, bytes):
        redacted = redacted.decode('utf-8', 'replace')
    return redacted, pii_residue(redacted)


# ---------------------------------------------------------------------------
# 类别
# ---------------------------------------------------------------------------

def assign_category(*texts):
    """按商品自己的文字（商品名称／产品名称／型号／品牌／项目名）定类别。

    取**命中的最长品类词**，所以「超纯水机」不会被更短的「纯水机」抢走；
    一个都没命中时返回 ``None``，由调用方回落到"发现它的检索词"。
    """
    haystack = ' '.join(text or '' for text in texts)
    best = ''
    for keyword in PROBE_KEYWORDS:
        if keyword in haystack and len(keyword) > len(best):
            best = keyword
    return CATEGORY_OF.get(best) if best else None


# ---------------------------------------------------------------------------
# 验证
# ---------------------------------------------------------------------------

def validate_price_row(item, shaped_body, publication_date):
    """把这一行送进**产品自己的** ``validate_price``，原样返回它的结论。

    送进去的都是页面自己的东西：``excerpt`` 是整形后的那一行
    （``型号规格 | 单价（元） | 数量 | 品牌``），``amount``/``quantity`` 是页面自己
    印的数字。``publication_date`` 用结果公示列表行的「成交时间」——所以它验的是
    "这个日期与列表页印的成交时间一致"，**不是**"详情页正文里有这个日期"。
    """
    from backend.services.procurement_rules import validate_price
    model, brand = item['model'], item['brand']
    raw = {'excerpt': item['price_excerpt'], 'amount': item['unit_price'],
           'amount_unit': '元', 'quantity': item['quantity'], 'model': model,
           'brand': brand, 'kind': 'award', 'amount_scope': 'unit'}
    source = {'body_status': 'fetched', 'body_text': shaped_body, 'url': item['detail_url'],
              'source_type': 'tender_document', 'publication_date': publication_date}
    refusals = {}
    try:
        quote = validate_price(raw, source, {'model': model, 'brand': brand}, refusals)
    except Exception as exc:  # 判据自己抛错也是"没通过"，如实记，不咽掉
        return None, f'{type(exc).__name__}: {exc}'
    return quote, ('' if quote else ','.join(sorted(refusals)) or 'refused')


def cross_check(shaped_section, picked, header, rows):
    """两套彼此独立的解析比对：``(型号, 单价, 数量)`` 三元组必须一致。

    一套是本模块**按 HTML 单元格**解析的 ``picked``，另一套是产品**按清洗后文本**
    解析的 ``shaped_section``。对不上就记缺口，不静默采信任何一边。
    """
    def num(text):
        try:
            return float(str(text).replace(',', ''))
        except (TypeError, ValueError):
            return None

    model_at, quantity_at = header.index('型号规格'), header.index('数量')
    mine = sorted((rows[index][model_at].strip(), price, num(rows[index][quantity_at]))
                  for index, _column, price in picked or [])
    theirs = []
    for line in shaped_section or []:
        cells = [cell.strip() for cell in line.split('|')]
        if cells[0] == '型号规格':
            continue
        if len(cells) < 3:
            return False, f'产品整形行不足三格: {line!r}'
        theirs.append((cells[0], num(cells[1]), num(cells[2])))
    return mine == sorted(theirs), f'mine={mine} theirs={sorted(theirs)}'


# ---------------------------------------------------------------------------
# 建库
# ---------------------------------------------------------------------------

def schema_sql():
    return SCHEMA_PATH.read_text(encoding='utf-8')


def connect(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(schema_sql())
    return conn


#: 漏斗各阶段的名字，顺序即处理顺序。
FUNNEL_STAGES = ('list_rows', 'unique_orders', 'skipped_no_template', 'skipped_terminal',
                 'detail_fetched', 'detail_ok', 'orders_with_price', 'goods_rows_raw',
                 'rows_kept', 'rows_validate_price')


def build(orders, details, *, window, built_at=None, keywords=PROBE_KEYWORDS):
    """把抓取结果读成订单表 + 成交明细行（内存结构；落盘由 ``write_database`` 做）。

    ``orders`` 是列表行（可重复：同一个单会被多个品类词搜到），
    ``details`` 是 ``{bill_guid: {'html': 脱敏 HTML, 'status': …, 'note': …}}``。
    """
    funnel = dict.fromkeys(FUNNEL_STAGES, 0)
    funnel['list_rows'] = len(orders)

    by_guid = {}
    for row in orders:
        entry = by_guid.setdefault(row['bill_guid'], {**row, 'keywords': set()})
        entry['keywords'].add(row.get('keyword') or '')
    funnel['unique_orders'] = len(by_guid)

    order_records, item_records, gaps = [], [], []

    for guid, row in sorted(by_guid.items(), key=lambda pair: pair[1]['deal_time'], reverse=True):
        keywords_of = sorted(k for k in row['keywords'] if k)
        template = detail_template(row['code_type'], row['code2'])
        day, stamp = normalize_deal_time(row['deal_time'])
        record = {
            'bill_guid': guid, 'order_no': row['order_no'], 'code_type': row['code_type'],
            'code1': row['code1'], 'code2': row['code2'], 'template': template or '',
            'order_mode': '', 'project_name': row['project_name'],
            'purchaser': row['purchaser'], 'supplier': row['supplier'],
            'supplier_detail': '', 'state': row['state'], 'deal_date': day,
            'deal_time': stamp, 'award_total': None, 'detail_url': detail_url(row) or '',
            'source_keywords': ','.join(keywords_of), 'fetch_status': '', 'skip_reason': '',
        }
        if template is None:
            funnel['skipped_no_template'] += 1
            record['fetch_status'] = 'skipped'
            record['skip_reason'] = (f'未验证的模板（codeType={row["code_type"]}, '
                                     f'code2={row["code2"]}）：不猜，记缺口跳过')
            gaps.append({'order_no': row['order_no'], 'kind': 'unknown_template',
                         'detail': record['skip_reason']})
            order_records.append(record)
            continue
        if is_terminal_state(row['state']):
            funnel['skipped_terminal'] += 1
            record['fetch_status'] = 'skipped'
            record['skip_reason'] = f'列表状态「{row["state"]}」：没有真实成交'
            order_records.append(record)
            continue

        detail = details.get(guid)
        if detail is None:
            record['fetch_status'] = 'missing'
            record['skip_reason'] = '没有抓取结果'
            order_records.append(record)
            continue
        funnel['detail_fetched'] += 1
        if detail.get('status') != 'ok':
            record['fetch_status'] = detail.get('status') or 'error'
            record['skip_reason'] = detail.get('note') or ''
            gaps.append({'order_no': row['order_no'], 'kind': record['fetch_status'],
                         'detail': record['skip_reason']})
            order_records.append(record)
            continue

        body, shaped = shape_body(detail['html'], record['detail_url'])
        header, rows = goods_table(detail['html'])
        total = award_total(body)
        record.update(order_mode=order_mode(body), supplier_detail=supplier_from_body(body),
                      award_total=total)
        printed = order_no_from_body(body)
        if printed and printed != record['order_no']:
            gaps.append({'order_no': record['order_no'], 'kind': 'order_no_mismatch',
                         'detail': f'详情页印的是 {printed}'})
        if header is None:
            record['fetch_status'] = 'no_table'
            record['skip_reason'] = '详情页没有商品信息表'
            gaps.append({'order_no': row['order_no'], 'kind': 'no_table',
                         'detail': record['skip_reason']})
            order_records.append(record)
            continue
        picked = select_prices(header, rows, total)
        if picked is None:
            record['fetch_status'] = 'no_price'
            record['skip_reason'] = '页面没有可用的成交单价（没有整单金额，或 Σ单价×数量 与整单金额对不上）'
            order_records.append(record)
            continue
        record['fetch_status'] = 'ok'
        funnel['detail_ok'] += 1
        funnel['orders_with_price'] += 1

        section = PLATFORM._quote_section(body)
        agree, why = cross_check(section, picked, header, rows)
        if not agree:
            gaps.append({'order_no': row['order_no'], 'kind': 'cross_check_mismatch',
                         'detail': why})

        keyword_category = CATEGORY_OF.get(row.get('keyword') or '')
        for line_no, (index, column, unit_price) in enumerate(picked, 1):
            cells = rows[index]
            goods = {attr: cells[header.index(name)].strip()
                     for name, attr in _COLUMN_FIELDS.items() if name in header}
            quantity = float(cells[header.index('数量')].replace(',', ''))
            model, brand = goods.get('model', ''), goods.get('brand', '')
            category = assign_category(goods.get('goods_name'), goods.get('product_name'),
                                       model, brand, row['project_name'])
            price_kind = ('auction_award' if column == '中标价'
                          else 'negotiated' if column == '协议价格（元）'
                          else 'list_price_direct')
            item = {
                'order_no': row['order_no'], 'line_no': line_no,
                'goods_name': goods.get('goods_name', ''),
                'product_name': goods.get('product_name', ''),
                'brand': brand, 'model': model, 'quantity': quantity,
                'unit_price': unit_price, 'amount': round(unit_price * quantity, 2),
                'price_column': column, 'price_kind': price_kind,
                'category': category or keyword_category or '',
                'category_source': 'item_text' if category else (
                    'probe_keyword' if keyword_category else ''),
                'deal_date': day, 'purchaser': row['purchaser'],
                'supplier': row['supplier'] or record['supplier_detail'],
                'detail_url': record['detail_url'],
                'excerpt': ' | '.join(cells), 'price_excerpt': '',
                'table_header': ' | '.join(header),
                'source_keywords': record['source_keywords'],
                'order_total_consistent': 1,
            }
            item['price_excerpt'] = ' | '.join([model, f'{unit_price:.2f}',
                                                f'{quantity:.2f}', brand])
            # 验证分三道闸门，逐行算，逐行记是哪一道没过。
            reasons = _structural_reasons(item)
            if reasons:
                item['validate_price'], item['validate_note'] = 0, 'skipped:structural'
            else:
                quote, note = validate_price_row(item, shaped, day)
                item['validate_price'] = 1 if quote else 0
                item['validate_note'] = note
                if not quote:
                    reasons.append(f'validate_price:{note}')
            item['verify_reasons'] = ';'.join(reasons)
            item['verified'] = 0 if reasons else 1
            if item['verified']:
                funnel['rows_kept'] += 1
            if item['validate_price']:
                funnel['rows_validate_price'] += 1
            item_records.append(item)
            funnel['goods_rows_raw'] += 1
        # 抓到价格的那条也要落订单表——否则 /orders/{no} 只查得到失败的单，
        # 成功的单反而"不存在"（测试里钉住）。
        order_records.append(record)

    return {'funnel': funnel, 'orders': order_records, 'items': item_records, 'gaps': gaps,
            'window': list(window), 'keywords': list(keywords),
            'built_at': built_at or datetime.now().isoformat(timespec='seconds')}


def _structural_reasons(item):
    """结构闸门：型号、单价、引文可追溯、隐私。返回没过的那几条。"""
    model = (item['model'] or '').strip()
    reasons = []
    if not model:
        reasons.append('model_empty')
    elif model.lower() in PLACEHOLDER_MODELS:
        reasons.append('model_placeholder')
    if model and item['excerpt'] and model not in item['excerpt']:
        reasons.append('model_not_in_row')
    if not item['unit_price'] > 0:
        reasons.append('price_not_positive')
    if pii_residue(item['excerpt']) or pii_residue(item['price_excerpt']):
        reasons.append('pii_residue')
    return reasons


ITEM_FIELDS = ('order_no', 'line_no', 'goods_name', 'product_name', 'brand', 'model',
               'quantity', 'unit_price', 'amount', 'price_column', 'price_kind',
               'category', 'category_source', 'deal_date', 'purchaser', 'supplier',
               'detail_url', 'excerpt', 'table_header', 'price_excerpt', 'verified',
               'verify_reasons', 'source_keywords', 'order_total_consistent',
               'validate_price')
ORDER_FIELDS = ('order_no', 'bill_guid', 'code_type', 'code2', 'template', 'order_mode',
                'project_name', 'purchaser', 'supplier', 'supplier_detail', 'state',
                'deal_date', 'deal_time', 'award_total', 'detail_url', 'source_keywords',
                'fetch_status', 'skip_reason')


def write_database(path, result):
    """把 ``build`` 的结果写进 SQLite（整库重建，先清空再写）。"""
    conn = connect(path)
    try:
        with conn:
            for table in ('xlyg_item', 'xlyg_order', 'xlyg_gap', 'xlyg_meta'):
                conn.execute(f'DELETE FROM {table}')
            conn.executemany(
                'INSERT OR REPLACE INTO xlyg_order (' + ','.join(ORDER_FIELDS) + ')'
                ' VALUES (' + ','.join('?' * len(ORDER_FIELDS)) + ')',
                [[row.get(field) for field in ORDER_FIELDS] for row in result['orders']])
            conn.executemany(
                'INSERT OR REPLACE INTO xlyg_item ('
                + ','.join(ITEM_FIELDS + ('validate_note',)) + ')'
                ' VALUES (' + ','.join('?' * (len(ITEM_FIELDS) + 1)) + ')',
                [[item.get(field) for field in ITEM_FIELDS] + [item.get('validate_note') or '']
                 for item in result['items']])
            conn.executemany('INSERT INTO xlyg_gap (order_no, kind, detail) VALUES (?,?,?)',
                             [(gap.get('order_no', ''), gap.get('kind', ''), gap.get('detail', ''))
                              for gap in result['gaps']])
            meta = {'window_start': result['window'][0], 'window_end': result['window'][1],
                    'built_at': result['built_at'],
                    'keywords': json.dumps(result['keywords'], ensure_ascii=False),
                    'funnel': json.dumps(result['funnel'], ensure_ascii=False)}
            conn.executemany('INSERT INTO xlyg_meta (key, value) VALUES (?,?)', list(meta.items()))
    finally:
        conn.close()
    return path
