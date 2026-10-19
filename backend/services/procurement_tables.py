"""
采购表格价格提取 - 第一性原理优化版本

核心改进:
1. 配置与逻辑分离
2. 单一职责函数
3. 类型安全的数据结构
4. 预编译正则表达式
5. 明确的错误处理
"""
import re
import unicodedata
from dataclasses import dataclass
from typing import Optional, List, Dict, Any


# ============================================================================
# 阶段1: 配置常量
# ============================================================================

class ColumnPatterns:
    """表头列名模式"""
    MODEL = {'型号', '货物型号', '规格型号'}
    # Formal contracts often call the model-bearing column "服务内容",
    # "采购内容", or "规格" instead of repeating "型号". These aliases are
    # accepted only when the target model is explicitly present in a data row;
    # they do not turn narrative text into a price record.
    #:
    #: ``型号规格`` is the same word order as ``规格型号``, reversed -- 广东省教育部门
    #: "小零易购"（``gdegp.gds.edu.cn:8093``）的成交明细表就是这么写列名的
    #: （``商品名称 | 产品名称 | 品牌 | 型号规格 | 金额（元） | 协议价格（元） |
    #: 数量 | …``）。列名必须照原样认，不能在整形时改写：报告里的引文要跟页面原文
    #: 一致，改写表头等于伪造引文。
    MODEL_CONTEXT = MODEL | {'服务内容', '采购内容', '规格', '规格/型号', '型号规格'}
    BRAND = {'品牌', '货物品牌'}
    NAME = {'名称', '货物名称', '采购标的', '货物品牌'}
    RANKING = {'排名'}  # 排名表不是确定的中标价格
    #: 供应商列。明细表逐行写供应商，同一张表里可以给不同供应商，所以只能按行取。
    #: 实测 636 份来源里 82 个价格表表头带这一列，包括 CCGP 的「主要标的信息」
    #: （``序号 | 供应商名称 | 货物名称 | 货物品牌 | 货物型号 | 货物数量 | 货物单价(元)``）
    #: ——正是那条取到 150000 元成交价的公告。原先这条通道产出 ``purchaser``/``supplier``
    #: 的只有转载记录一路，最权威的一手表格反倒是字段最少的。
    SUPPLIER = {'供应商', '供应商名称', '成交供应商', '中标供应商', '中标人', '成交人',
                '中标单位', '成交单位'}
    #: 采购人 / 采购单位列。明细表里少见（实测只在一张聚合站的表上出现过），
    #: 但表头写了就直接读，比只靠正文标签多一条路。
    PURCHASER = {'采购人', '采购单位', '采购人名称', '采购单位名称', '招标人',
                 '招标人名称', '建设单位'}
    #: 项目编号列。明细表里几乎没有，表头写了才读。
    PROJECT = {'项目编号', '采购项目编号', '项目编码'}
    #: 上面三类列名放在一起：数据格里出现这些词说明那一格是列名本身
    #: （合并单元格被展开时会重复写上列名），不是值。
    IDENTITY_HEADS = SUPPLIER | PURCHASER | PROJECT


class RegexPatterns:
    """预编译的正则表达式"""
    # 价格表头: "单价(元)" / "货物单价(万元)" / "单价（千元）"
    #: 括号里除了单位还可以有别的东西：别人的模板会写 ``单价（元/台）``、
    #: ``单价（元/年）`` 这种"单位后面还带着计量口径"的表头。**单位仍然是必需的**
    #: （货币字必须紧跟左括号），``UNIT_EXTRACTOR`` 取不到时
    #: ``extract_unit_from_heading`` 照旧默认 ``元``——放宽的只是"括号里还能有字"，
    #: 不是"没有单位也算价格列"。
    #:
    #: 保留它的理由：认不出价格列头会让**整张表**在 ``parse_table_header`` 第一步就
    #: 被判成"这不是价格表"，一行都读不出来；而多认一种表头的代价有界——单元格里的
    #: 金额仍旧要过 ``parse_amount``（只认数字，``95%`` 或 ``不含税`` 都读不出金额），
    #: 整行也仍旧要过 ``validate_price``。
    PRICE_HEADING = re.compile(r'(?:货物)?单价\s*[（(](元|万元|千元)[^）)]*[）)]')

    # 数量表头: "数量" / "货物数量" / "数量(台)"
    QUANTITY_HEADING = re.compile(r'(?:货物)?数量(?:[（(][^）)]*[）)])?')

    # 单位提取: 从表头中提取"元/万元/千元"
    UNIT_EXTRACTOR = re.compile(r'[（(](元|万元|千元)[）)]')

    # 单元格自带单位: 明细表的单价单元格常把表头已声明的单位再写一遍
    # （``43,300.00元``）。只在它与表头声明一致时才接受，见 ``parse_amount``。
    CELL_UNIT = re.compile(r'(万元|千元|人民币|元)\s*$')

    # 货币上下文: "金额单位：元" / "元(人民币)"
    CURRENCY_CONTEXT = re.compile(r'(?:金额单位\s*[:：]?\s*元|元\s*[（(]人民币[）)])')

    # 数字格式: 整数或小数，可带逗号分隔符
    NUMBER_FORMAT = re.compile(r'\d+(?:\.\d+)?')

    # 数量格式: 数字 + 可选单位后缀 "1.00(套)" / "30台" / "5"
    QUANTITY_FORMAT = re.compile(r'(\d+(?:\.\d+)?)(?:[（(][^）)]*[）)]|(?:台|套|件|个|组|部|批))?')

    # 单元格分隔符
    CELL_SEPARATOR = re.compile(r'[;；]')


# ============================================================================
# 阶段2: 数据结构
# ============================================================================

@dataclass
class TableHeader:
    """表格表头信息"""
    cells: List[str]
    model_index: int
    price_index: int
    quantity_index: Optional[int]
    brand_index: Optional[int]
    name_indices: List[int]
    unit: str  # 价格单位: 元/万元/千元
    #: 可选的身份列。一手明细表里供应商列很常见（实测 82 个表头带），采购人与
    #: 项目编号列少见；没有这一列就是 ``None``，取值时按空格处理。
    supplier_index: Optional[int] = None
    purchaser_index: Optional[int] = None
    project_index: Optional[int] = None

    def has_ranking_column(self) -> bool:
        """是否包含排名列（排名表不是确定的中标价格）"""
        return any(cell in ColumnPatterns.RANKING for cell in self.cells)


#: ``第N中标候选人`` 小节标记。用于判断一行价格落在哪个候选人的清单里。
CANDIDATE_SECTION = re.compile(r'第\s*([一二三四五六七八九十\d]+)\s*中标候选人')
_ORDINALS = {'一': 1, '二': 2, '三': 3, '四': 4, '五': 5,
             '六': 6, '七': 7, '八': 8, '九': 9, '十': 10}


def _ordinal(token: str):
    if token.isdigit():
        return int(token)
    return _ORDINALS.get(token)


def candidate_section_ordinal(body_text, position):
    """``position`` 落在第几中标候选人的小节里？没有就返回 ``None``。

    为什么要这个：**落选者的投标报价不是成交价**。实测平顶山那份公告里
    目标型号全文只出现 1 次，就在「第二中标候选人」小节；
    而中标的第一中标候选人同一行项投的是艾本德 5810R（187,500），根本没投
    这个型号。把落选报价当成成交价，正是「价格区间」这个假象的来源。

    只认 ``position`` **之前**最近的一个标记：小节标题一定写在其内容之前。
    """
    text = body_text or ''
    if not isinstance(text, str):
        return None
    if position is None or position < 0:
        position = len(text)
    found = None
    for match in CANDIDATE_SECTION.finditer(text):
        if match.start() > position:
            break
        found = match
    return _ordinal(found.group(1)) if found else None


@dataclass
class PriceRecord:
    """价格记录"""
    model: str
    brand: str
    amount: float
    amount_unit: str
    quantity: Optional[float]
    amount_scope: str
    currency: str
    excerpt: str
    money_excerpt: str
    table_evidence: Dict[str, Any]
    #: 一手表格自己的身份字段。供应商逐行写在表里，采购人/项目编号是整张表共用的。
    #: 空值不写进 ``to_dict``：写空串只会把噪声搬进报告，也让"这条价格本来就没有"
    #: 与"没读到"分不开。
    supplier: str = ''
    procurement_unit: str = ''
    project_id: str = ''

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典（向后兼容）"""
        record = {
            'model': self.model,
            'brand': self.brand,
            'amount': self.amount,
            'amount_unit': self.amount_unit,
            'quantity': self.quantity,
            'amount_scope': self.amount_scope,
            'currency': self.currency,
            'excerpt': self.excerpt,
            'money_excerpt': self.money_excerpt,
            'table_evidence': self.table_evidence,
        }
        for key, value in (('supplier', self.supplier),
                           ('procurement_unit', self.procurement_unit),
                           ('project_id', self.project_id)):
            if value:
                record[key] = value
        return record


# ============================================================================
# 阶段3: 辅助函数（纯函数）
# ============================================================================

def is_price_heading(value: str, has_currency_context: bool) -> bool:
    """判断是否为价格列表头"""
    return bool(RegexPatterns.PRICE_HEADING.fullmatch(value) or
                (value == '单价' and has_currency_context))


def extract_unit_from_heading(heading: str, has_currency_context: bool) -> str:
    """从表头提取价格单位"""
    match = RegexPatterns.UNIT_EXTRACTOR.search(heading)
    return match.group(1) if match else '元'


def split_cells(value: str) -> List[str]:
    """分割单元格内容（分号/中文分号分隔）"""
    return [v.strip() for v in RegexPatterns.CELL_SEPARATOR.split(value)]


#: 型号形态：以字母开头、带数字的字母数字串（``DW-86L578S``/``ETC821``/
#: ``FLLS-01``/``HFsafe-1200LC``）。只认这种形状，中文品名和纯字母名一律不算，
#: 所以 ``药理评价系统``、``BeneFusion zVP ex`` 不会把自己变成"型号"。
_MODEL_SHAPED = re.compile(r'[A-Za-z][A-Za-z0-9]*(?:[-+][A-Za-z0-9]+)*\d[A-Za-z0-9]*')


def lists_sibling_models(cell: str, model: str) -> bool:
    """单元格里是否并列着目标型号**以外**的型号。

    ``split_cells`` 只按分号切，所以"一、… 二、… 三、…"这种列举切不开，
    ``price_table_rows`` 会把整格当成一个型号（``target_count`` 算成 1），
    于是整行唯一那个金额——常常是"包别1"这样一整包的总价——就落到了
    目标型号头上。真实案例：一份"药理评价系统"整包（含 8 种设备，型号格里
    用"一、二、…"并列）的 1,239,000 元，被当成其中一台 -80℃ 冰箱的单价
    写进了成交清单并列给买家。

    同一型号的不同写法**不算**并列：``identity`` 去掉连字符和空格并折叠大小写，
    所以 ``XKA-2200``/``XKA2200``、``DW-86L578S``/``DW-86L578s`` 都归一成同一个。

    整格就是目标型号的另一种写法时直接判"没有并列"：空格与连字符是**型号内部**
    的分隔符（``contains_model`` 就是这么认的），却会被 :data:`_MODEL_SHAPED`
    当成两个标记的边界。实测一份纯扫描件的分项报价表逐行写着
    ``BSC-1604 IIA2-e | 5台 | 49500 | 247500``，型号格被切成 ``BSC-1604`` 与
    ``IIA2`` 两个标记，于是这一格被误判成"并列着别的型号"，**整行被丢掉**
    （``price_table_rows`` 对该表返回 0 行），单价 49500 因此进不了核价。

    必须真的数出**两个以上**型号标记才算并列。只有一处标记时，它可能只是型号的
    一部分（``知楚A2`` 里只能截出 ``A2``，``甲厂Q１０`` 里只能截出 ``Q１``），
    与完整型号的归一形不相等——那不算"并列着别的型号"，只是同一个型号被截短了。
    """
    from backend.services.procurement_rules import identity
    # NFKC 先行，全角字母数字（``Ｑ１０``）与 ``identity`` 的归一化保持同一形态，
    # 否则 ``\d`` 会吃下全角数字、把 ``Q10`` 截成 ``Q1`` 而误判成另一个型号。
    normalized = unicodedata.normalize('NFKC', cell or '')
    target = identity(model)
    try:
        if identity(normalized) == target:
            return False
    except ValueError:
        # 空格子：没有型号标记，也就没有并列。
        return False
    tokens = _MODEL_SHAPED.findall(normalized)
    if len(tokens) < 2:
        return False
    return any(identity(token) != target for token in tokens)


def expand_single_value(values: List[str], target_count: int) -> List[str]:
    """单值扩展：['A'] -> ['A', 'A', 'A']"""
    return values * target_count if len(values) == 1 and target_count > 1 else values


def parse_amount(raw_amount: str, declared_unit: Optional[str] = None) -> Optional[float]:
    """解析金额字符串

    明细表的单价单元格经常把表头已经声明的单位再写一遍（``43,300.00元``）。
    以前这种单元格一律解析失败，于是整行明细被丢掉——附件明明写了单价，系统
    却报"没提供"。这里容忍这种重复，但**只在单位与表头声明一致时**：
    ``单价（元）`` 表头配 ``43,300.00万元`` 单元格是两个互相矛盾的声明，
    照原样拒收，绝不拿其中一个去乘 10000。
    """
    # 去除逗号分隔符
    number_str = raw_amount.replace(',', '').strip()

    # 验证格式
    if not RegexPatterns.NUMBER_FORMAT.fullmatch(number_str):
        cell_unit = RegexPatterns.CELL_UNIT.search(number_str)
        normalized = {'人民币': '元'}.get(cell_unit.group(1), cell_unit.group(1)) if cell_unit else None
        if cell_unit is None or declared_unit is None or normalized != declared_unit:
            return None
        number_str = number_str[:cell_unit.start()].strip()
        if not RegexPatterns.NUMBER_FORMAT.fullmatch(number_str):
            return None

    try:
        return float(number_str)
    except ValueError:
        return None


def parse_quantity(raw_quantity: Optional[str]) -> Optional[float]:
    """解析数量字符串"""
    if raw_quantity is None:
        return None

    match = RegexPatterns.QUANTITY_FORMAT.fullmatch(raw_quantity)
    if not match:
        return None

    try:
        return float(match.group(1))
    except (ValueError, AttributeError):
        return None


def parse_table_header(cells: List[str], has_currency_context: bool) -> Optional[TableHeader]:
    """解析表头行"""
    # 查找必需列
    model_index = next((i for i, c in enumerate(cells) if c in ColumnPatterns.MODEL_CONTEXT), None)
    price_index = next((i for i, c in enumerate(cells) if is_price_heading(c, has_currency_context)), None)

    if model_index is None or price_index is None:
        return None

    # 查找可选列
    quantity_index = next(
        (i for i, c in enumerate(cells) if RegexPatterns.QUANTITY_HEADING.fullmatch(c)),
        None
    )
    brand_index = next((i for i, c in enumerate(cells) if c in ColumnPatterns.BRAND), None)
    name_indices = [i for i, c in enumerate(cells) if c in ColumnPatterns.NAME]
    supplier_index = next((i for i, c in enumerate(cells) if c in ColumnPatterns.SUPPLIER), None)
    purchaser_index = next((i for i, c in enumerate(cells) if c in ColumnPatterns.PURCHASER), None)
    project_index = next((i for i, c in enumerate(cells) if c in ColumnPatterns.PROJECT), None)

    # 提取单位
    unit = extract_unit_from_heading(cells[price_index], has_currency_context)

    return TableHeader(
        cells=cells,
        model_index=model_index,
        price_index=price_index,
        quantity_index=quantity_index,
        brand_index=brand_index,
        name_indices=name_indices,
        unit=unit,
        supplier_index=supplier_index,
        purchaser_index=purchaser_index,
        project_index=project_index,
    )


def looks_like_table_header(cells: List[str], has_currency_context: bool) -> bool:
    """Return whether a non-price row is another table's header.

    A document may place a second table immediately after the first one and
    use ``总价``/``合计`` instead of ``单价``.  Such a row must terminate the
    previous header context; otherwise its following data row can inherit the
    old price column and produce a false price for the requested model.
    """
    labels = {cell.strip() for cell in cells if cell.strip()}
    structural = 0
    structural += sum(cell in ColumnPatterns.MODEL_CONTEXT for cell in labels)
    structural += sum(bool(RegexPatterns.QUANTITY_HEADING.fullmatch(cell)) for cell in labels)
    structural += sum(cell in ColumnPatterns.BRAND for cell in labels)
    structural += sum(cell in ColumnPatterns.NAME for cell in labels)
    structural += sum(cell in ColumnPatterns.RANKING for cell in labels)
    structural += sum(is_price_heading(cell, has_currency_context) for cell in labels)
    # Total/amount headings are useful for detecting a new table but are not
    # accepted as unit-price headings by the extractor.
    structural += sum(bool(re.search(r'(?:总价|合计|金额|含税价|不含税价)', cell)) for cell in labels)
    return structural >= 2


def align_cells(
    cells: List[str],
    header: TableHeader,
    target_count: int,
) -> Optional[Dict[str, List[str]]]:
    """
    对齐单元格数据

    返回对齐后的数据，如果无法对齐则返回None
    """
    # 分割并提取基础字段
    models = split_cells(cells[header.model_index])
    amounts = split_cells(cells[header.price_index])

    # 数量
    quantities = (
        split_cells(cells[header.quantity_index])
        if header.quantity_index is not None
        else [None] * target_count
    )

    # 品牌（单值可扩展）
    brands = (
        expand_single_value(split_cells(cells[header.brand_index]), target_count)
        if header.brand_index is not None
        else [''] * target_count
    )

    # 名称（单值可扩展）
    names_list = [
        expand_single_value(split_cells(cells[i]), target_count)
        for i in header.name_indices
    ]

    # 对齐检查
    all_fields = [models, amounts, quantities, brands, *names_list]
    if any(len(field) != target_count for field in all_fields):
        return None

    return {
        'models': models,
        'amounts': amounts,
        'quantities': quantities,
        'brands': brands,
    }


def identity_cell(cells: List[str], index: Optional[int]) -> str:
    """按列索引取一格身份值（供应商 / 采购单位 / 项目编号）。

    没有这一列、索引越界、或者格子里写的还是列名本身（合并单元格被展开时会重复
    写上列名，实测有 ``中标供应商 | 中标供应商 | 中标供应商 | 单价 | …`` 这种表头）
    都返回空串——宁可这一行为空，也不把列名当供应商报出去。
    """
    if index is None or index < 0 or index >= len(cells):
        return ''
    value = cells[index].strip()
    if not value or value in ColumnPatterns.IDENTITY_HEADS:
        return ''
    return value


def create_price_record(
    model: str,
    found_model: str,
    brand: str,
    raw_amount: str,
    raw_quantity: Optional[str],
    unit: str,
    line: str,
    header_cells: List[str],
    item_index: int,
    item_count: int,
    currency_context_match: Optional[re.Match],
    price_heading: str,
    supplier: str = '',
    procurement_unit: str = '',
    project_id: str = '',
) -> Optional[PriceRecord]:
    """
    创建价格记录

    如果数据无效则返回None
    """
    # 解析金额
    amount = parse_amount(raw_amount, unit)
    if amount is None:
        return None

    # 解析数量
    quantity = parse_quantity(raw_quantity)
    if raw_quantity is not None and quantity is None:
        return None

    # 构建证据字段
    currency_context_text = (
        currency_context_match.group(0)
        if currency_context_match and not RegexPatterns.UNIT_EXTRACTOR.search(price_heading)
        else price_heading
    )

    # money_excerpt 中的金额要去除逗号（与原版本保持一致）；单元格重复写出的
    # 那个单位也要去掉，否则合成出来的证据会读成 ``43300.00元元``。
    amount_str_for_excerpt = raw_amount.replace(',', '').strip()
    cell_unit = RegexPatterns.CELL_UNIT.search(amount_str_for_excerpt)
    if cell_unit and {'人民币': '元'}.get(cell_unit.group(1), cell_unit.group(1)) == unit:
        amount_str_for_excerpt = amount_str_for_excerpt[:cell_unit.start()].strip()

    return PriceRecord(
        model=model,
        brand=brand,
        amount=amount,
        amount_unit=unit,
        quantity=quantity,
        amount_scope='unit',
        currency='CNY',
        excerpt=line,
        money_excerpt=f'{found_model} 单价 {amount_str_for_excerpt}{unit} 数量 {raw_quantity or "未知"}',
        table_evidence={
            'header': ' | '.join(header_cells),
            'row': line,
            'item_index': item_index,
            'item_count': item_count,
            'currency_context': currency_context_text,
        },
        supplier=supplier,
        procurement_unit=procurement_unit,
        project_id=project_id,
    )


# ============================================================================
# 阶段4: 主函数（编排逻辑）
# ============================================================================

def price_table_rows(body: str, model: str) -> List[Dict[str, Any]]:
    """
    从采购公告正文提取价格表格行

    Args:
        body: 公告正文
        model: 要匹配的型号

    Returns:
        价格记录列表（字典格式，向后兼容）
    """
    from backend.services.procurement_rules import contains_model

    # 检测全局货币上下文
    currency_context_match = RegexPatterns.CURRENCY_CONTEXT.search(body)
    has_currency_context = currency_context_match is not None

    header: Optional[TableHeader] = None
    results: List[PriceRecord] = []

    for line in body.splitlines():
        # 跳过非表格行
        if '|' not in line:
            if line.strip():
                header = None  # 非空行重置表头
            continue

        cells = [c.strip() for c in line.split('|')]

        # 尝试解析为表头
        potential_header = parse_table_header(cells, has_currency_context)
        if potential_header is not None:
            header = potential_header
            continue

        # A new table can begin immediately after the previous one without a
        # blank or narrative separator. If it has a model heading but no unit
        # price heading (for example, a total-price table), the old price
        # header must not leak into its rows.
        if looks_like_table_header(cells, has_currency_context):
            header = None
            continue

        # 没有有效表头时跳过数据行
        if header is None:
            continue

        # 数据行基本检查
        if len(header.cells) != len(cells):
            continue

        if not contains_model(line, model):
            continue

        if header.has_ranking_column():
            continue  # 排名表不是确定的中标价格

        # 对齐单元格数据
        models_in_row = split_cells(cells[header.model_index])
        target_count = len(models_in_row)

        # 型号格里并列着别的型号、而整行只有一个金额时，这个金额是这一行
        # （往往是一整包）的总价，不是其中任一型号的单价。见
        # ``lists_sibling_models``：宁可少一条，不能把整包价当单价报出去。
        if target_count == 1 and lists_sibling_models(cells[header.model_index], model):
            continue

        aligned = align_cells(cells, header, target_count)
        if aligned is None:
            continue

        # 提取每个型号的价格记录
        for index, found_model in enumerate(aligned['models']):
            if not contains_model(found_model, model):
                continue

            record = create_price_record(
                model=model,
                found_model=found_model,
                brand=aligned['brands'][index],
                raw_amount=aligned['amounts'][index],
                raw_quantity=aligned['quantities'][index],
                unit=header.unit,
                line=line,
                header_cells=header.cells,
                item_index=index,
                item_count=target_count,
                currency_context_match=currency_context_match,
                price_heading=header.cells[header.price_index],
                supplier=identity_cell(cells, header.supplier_index),
                procurement_unit=identity_cell(cells, header.purchaser_index),
                project_id=identity_cell(cells, header.project_index),
            )

            if record is not None:
                results.append(record)

    return [r.to_dict() for r in results]


# ============================================================================
# 阶段5: 转载成交记录行（无表头）
# ============================================================================

#: Aggregators republish award records as a headerless pipe table::
#:
#:     品目 | 品牌 | 型号 | 采购单位 | 供应商 | 日期 | 金额 | 查看
#:
#: ``price_table_rows`` cannot read these. It needs a recognised header row and
#: an exact column-count match, so every such line is skipped before the model
#: test ever runs. The row's own shape -- the target model, a dated amount, and
#: a named public purchaser -- is self-sufficient, so these rows are read on
#: their own terms.
#:
#: Such a row is a *republished* transaction: the host recorded a purchase it
#: did not conduct. Callers must therefore keep it distinguishable from a
#: first-hand announcement rather than merging it into the same evidence grade.
AWARD_ROW_DATE = re.compile(r'^(20\d{2})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})\s*日?$')
AWARD_ROW_AMOUNT = re.compile(r'^(?:[¥￥])?\s*(\d[\d,]*(?:\.\d+)?)\s*(万元|千元|元)$')

#: The buying unit must read as a public body. Seller-side names end in
#: 公司/商行/经营部 and are deliberately absent from this list, so a dealer's
#: name can never stand in for the purchaser.
AWARD_ROW_PURCHASER = re.compile(
    r'[\u4e00-\u9fff（）()]{2,}(?:医院|血站|血液中心|疾病预防控制中心|疾控中心|卫生院|保健院'
    r'|大学|学院|学校|研究院|研究所|科学院|检测中心|检验中心|测试中心|计量院|海关'
    r'|公安局|监狱|戒毒所|中学|小学|职业技术学院|委员会|管理局|管理站|服务中心)')

#: County-level health consortia buy under names like
#: 「峨山彝族自治县医疗与健康服务集团」, which carries no suffix from the list
#: above. A trading entity, by contrast, ends in 公司. So a 集团 that is not a
#: 公司 counts as a buyer. The complement is what keeps sellers out; the group
#: rule is not fitted to one sample.
AWARD_ROW_GROUP = re.compile(r'集团')
AWARD_ROW_COMPANY = re.compile(r'公司|商行|经营部|商店|门市|厂$')


def _is_public_purchaser(cell: str) -> bool:
    if AWARD_ROW_COMPANY.search(cell):
        return False
    return AWARD_ROW_PURCHASER.search(cell) is not None or AWARD_ROW_GROUP.search(cell) is not None

#: A column that states what the amount means. Used only to enrich the record,
#: never to admit or reject it: republished rows usually carry no such column.
_CONFIGURATION_HINT = re.compile(r'配置|型号规格|规格|主机|标准配置')


def republished_award_rows(body: str, model: str) -> List[Dict[str, Any]]:
    """Read headerless republished award rows pairing *model* with a dated amount.

    A row qualifies on its own shape, without any header:

    * exactly one standalone date cell and exactly one standalone amount cell,
      with the amount to the right of the date;
    * the target model appears in the line;
    * some cell to the left of the date names a public purchaser.

    Returns the same dict shape as :func:`price_table_rows` so callers can reuse
    the existing presentation, plus ``purchaser``, ``supplier`` and
    ``evidence_grade='secondary'``. The grade is not decoration: it is how the
    caller keeps a republished record out of the first-hand evidence slot.
    """
    from backend.services.procurement_rules import contains_model

    results: List[Dict[str, Any]] = []
    for line in body.splitlines():
        if '|' not in line or not contains_model(line, model):
            continue
        cells = [cell.strip() for cell in line.split('|')]
        dates = [(index, cell) for index, cell in enumerate(cells) if AWARD_ROW_DATE.match(cell)]
        amounts = [(index, cell) for index, cell in enumerate(cells) if AWARD_ROW_AMOUNT.match(cell)]
        if len(dates) != 1 or len(amounts) != 1:
            continue
        date_index, raw_date = dates[0]
        amount_index, raw_amount = amounts[0]
        if amount_index <= date_index:
            continue
        amount_match = AWARD_ROW_AMOUNT.match(raw_amount)
        value = parse_amount(amount_match.group(1))
        if value is None:
            continue
        date_match = AWARD_ROW_DATE.match(raw_date)
        date_value = f'{date_match.group(1)}-{int(date_match.group(2)):02d}-{int(date_match.group(3)):02d}'
        # The purchaser must sit to the left of the date; the supplier normally
        # follows the model. Neither may be confused with the other.
        purchaser_index = next(
            (index for index, cell in enumerate(cells[:date_index]) if _is_public_purchaser(cell)), None)
        if purchaser_index is None:
            continue
        model_index = next(
            (index for index, cell in enumerate(cells) if contains_model(cell, model)), None)
        if model_index is None:
            continue
        brand = cells[model_index - 1] if model_index >= 1 else ''
        if _is_public_purchaser(brand) or AWARD_ROW_DATE.match(brand) or AWARD_ROW_AMOUNT.match(brand):
            brand = ''
        supplier = next(
            (cell for cell in cells[purchaser_index + 1:date_index]
             if cell and not _is_public_purchaser(cell) and not contains_model(cell, model)),
            '')
        configuration = next((cell for cell in cells if _CONFIGURATION_HINT.search(cell)), '')
        results.append({
            'model': model,
            'brand': brand,
            'amount': value,
            'amount_unit': amount_match.group(2),
            'quantity': None,
            'amount_scope': 'unit',
            'currency': 'CNY',
            'date': date_value,
            'configuration': configuration or 'unknown',
            'purchaser': cells[purchaser_index],
            'supplier': supplier,
            'evidence_grade': 'secondary',
            'excerpt': line,
            'money_excerpt': line,
        })
    return results


# ============================================================================
# 阶段6: 标签连写行（同一行印完全部字段、字段之间没有分隔符）
# ============================================================================

#: 一行里印完一件设备的全部字段，标签之间**一个分隔符都没有**：:
#:
#:     第 2包\n货物类\n名称：▲台式高速离心机品牌：上海卢湘仪规格型号：H18.5B数量：1台单价：12000元/台
#:
#: 这是 CCGP 中标公告「四、主要标的信息」的原样排版：页面上是一件设备一段，
#: 抽成纯文本后各字段连写。这一行同时绕过本项目已有的两条记录型抽取：
#:
#: * :func:`price_table_rows` 要求"认得出的表头行 + 数据行列数完全相等"；
#: * ``tender_requirements._AWARD_FIELD`` 把正则锚在整行两端（``^…$``），
#:   只认"一行一对键值"。
#:
#: ``province_award_sources._lot_line`` 用的是同一个思路（每个值捕到下一个标签为止），
#: 但它服务政采云"标项块"（``主要标的名称`` 起块、``单价（元）`` 必带括号单位），
#: 与这里的"名称/品牌/规格型号/数量/单价"不是同一组标签，故各自成文、互不覆盖。
#:
#: **这里只做结构化，不做采信**：产出与 :func:`price_table_rows` 同形状的记录，
#: 照样要过 ``validate_price``——型号逐字一致、品牌同一家、金额口径、逐字引文，
#: 一条都不放宽。
_LABEL_FIELDS = (
    ('name', re.compile(r'名称\s*[:：]\s*(.*?)\s*(?=品牌|规格型号|型号|数量|单价|$)')),
    ('brand', re.compile(r'品牌\s*[:：]\s*(.*?)\s*(?=规格型号|型号|数量|单价|名称|$)')),
    ('model', re.compile(r'(?:规格型号|型号)\s*[:：]\s*(.*?)\s*(?=数量|单价|品牌|名称|规格型号|$)')),
    ('quantity', re.compile(r'数量\s*[:：]\s*(.*?)\s*(?=单价|品牌|规格型号|型号|名称|$)')),
)
#: 单价那一格。标签可以自带括号单位（``单价（元）：``），单位也可以写在值里
#: （``单价：12000元/台``）。两种写法都读，单位必须**出自这一行原文**。
_LABEL_PRICE = re.compile(
    r'单价\s*(?:[（(]\s*(万元|千元|元|人民币)\s*[）)])?\s*[:：]\s*'
    r'(.*?)\s*(?=名称|品牌|规格型号|型号|数量|$)')
#: 值里的金额与其单位：``12000元/台`` / ``12,400.00元``。
_LABEL_AMOUNT = re.compile(r'(\d[\d,]*(?:\.\d+)?)\s*(万元|千元|元|人民币)')
#: 标签本身。只为**数一数一行里印了几件设备**，不用来取值。
_LABEL_MODEL_TAG = re.compile(r'(?:规格型号|型号)\s*[:：]')
_LABEL_PRICE_TAG = re.compile(r'单价\s*(?:[（(][^）)]*[）)])?\s*[:：]')
#: 标签已经声明了单位时，值可以不带单位（``单价（元）：17480``）——这是
#: ``validate_price`` 明确支持的形状（CCGP 的「规格型号：XKA-2200 数量：1
#: 单价(元)：17480」）。只允许纯数字加一个计量后缀（``17480`` / ``12000/台``）：
#: 值里还有别的东西就说明这一格不是"一个金额"，整行跳过。
_LABEL_AMOUNT_WITHOUT_UNIT = re.compile(r'(\d[\d,]*(?:\.\d+)?)\s*(?:/\s*\S+)?')
#: ``人民币`` 与 ``元`` 是同一个刻度，其余各归各（与 ``parse_amount`` 同一张对照）。
_LABEL_UNITS = {'元': '元', '万元': '万元', '千元': '千元', '人民币': '元'}


def label_line_rows(body: str, model: str) -> List[Dict[str, Any]]:
    """把"标签连写在同一行"的设备行读成与 :func:`price_table_rows` 同形状的记录。

    只认**这一行**：型号、数量、单价必须印在同一行里——这正是
    :func:`price_table_rows` 与 ``_AWARD_FIELD`` 都读不到的形状。字段缺一个就跳过，
    **不跨行拼**：跨行拼出来的组合不是原文里那一件设备，型号与金额可能分属两件。

    金额口径按原文读，不从别处借用：

    * 单位只认这一行里写出来的（标签括号里的，或值里紧跟金额的）；
    * 值里没写单位（``单价：12000``）而标签也没声明单位 → 整个跳行，不猜；
    * 标签与值各自声明了不同刻度 → 跳行（两个互相矛盾的声明，与
      :func:`parse_amount` 同一条立场）。

    ``amount_scope`` 固定 ``unit``：这一格印的是"单价"，不是整包价。这一点与
    ``province_award_sources`` 读 ``单价（元）`` 那一格是同一条口径。

    **读取范围说不清时一律跳过**（宁可少一条，不能把 A 的价配到 B 的型号上）：

    * 行里没有型号标签（``规格型号：``/``型号：``）→ 不算标签行，跳过。
      ``生物安全柜 乙厂 Q20 数量1台 单价：56,000.00 元`` 这种散文里"哪一格是型号"
      没有标签指明，不该由这条通道认领（它放行过一次，见
      ``tests/test_procurement_tables.py::test_a_prose_line_without_a_model_label_is_not_a_label_line``）；
    * 一行里印了**两件**设备（两个型号标签或两个单价标签）→ 整行跳过；
    * 型号格里并列着多个型号（``H18.5B;H18.5BR``）而整行只有一个金额 →
      与 :func:`price_table_rows` 同一道 :func:`lists_sibling_models` 门，跳过。
    """
    from backend.services.procurement_rules import contains_model

    results: List[PriceRecord] = []
    for raw_line in body.splitlines():
        line = raw_line.strip()
        # 管道表行留给 :func:`price_table_rows` / :func:`republished_award_rows`：
        # 它们按表格列读，这里按标签读；同一行只许一条路径产出，免得同一笔价出两条。
        if not line or '|' in line or not contains_model(line, model):
            continue
        # 一行里印了两件设备（两个型号标签，或两个单价标签）：按标签取值只能取到
        # **第一件**，而 excerpt 是整行——交出去以后人看到的是两条设备一行，分不清
        # 哪一笔钱属于哪一件；更糟的形状是"名称/品牌"取第一件、"单价"取第二件，
        # 于是 B 的价被配到 A 的型号上。项目对并列型号的既有立场是"宁可少一条"
        # （见 :func:`lists_sibling_models` 在 :func:`price_table_rows` 里的调用），
        # 这里同理：这一行读不清就整行跳过；两件**分行**印的正常排版各读各的。
        if (sum(1 for _ in _LABEL_MODEL_TAG.finditer(line)) > 1
                or sum(1 for _ in _LABEL_PRICE_TAG.finditer(line)) > 1):
            continue
        fields: Dict[str, str] = {}
        for key, pattern in _LABEL_FIELDS:
            match = pattern.search(line)
            if match and match.group(1).strip():
                fields.setdefault(key, match.group(1).strip())
        # 这一行必须是**标签行**：型号那一格要真的写在行里（``规格型号：``/``型号：``）。
        # 没有型号标签的散文不属于这条通道——``生物安全柜 乙厂 Q20 数量1台
        # 单价：56,000.00 元`` 这种行里"哪一格是型号"根本没有标签指明，
        # ``price_table_rows`` 与抽取模型才是读它的地方。也**不许**拿目标型号回退：
        # 那等于把"这一行有个价"当成"这一行有目标型号的价"。
        if not fields.get('model'):
            continue
        # 型号格里并列着别的型号而整行只有一个金额时，那个金额是这一件的整包价，
        # 不是其中任一型号的单价——与 :func:`price_table_rows` 第 570 行同一道门，
        # 同一份判据，不为这条通道另写一套。
        if lists_sibling_models(fields['model'], model):
            continue
        price = _LABEL_PRICE.search(line)
        if price is None:
            continue
        declared = _LABEL_UNITS.get(price.group(1) or '')
        amount_in_value = _LABEL_AMOUNT.search(price.group(2).strip())
        if amount_in_value is not None:
            cell_unit = _LABEL_UNITS.get(amount_in_value.group(2))
            if declared and cell_unit and declared != cell_unit:
                continue
            unit = declared or cell_unit
            raw_amount = amount_in_value.group(1)
        else:
            # 值里没写单位：只有标签自己声明了单位才收（``单价（元）：17480``）。
            # 两个地方都没写单位就是口径不明，不从别处借一个。
            if not declared:
                continue
            plain = _LABEL_AMOUNT_WITHOUT_UNIT.fullmatch(price.group(2).strip())
            if plain is None:
                continue
            unit, raw_amount = declared, plain.group(1)
        if unit is None:
            continue
        record = create_price_record(
            model=model,
            found_model=fields['model'],
            brand=fields.get('brand', ''),
            raw_amount=raw_amount,
            raw_quantity=fields.get('quantity'),
            unit=unit,
            line=line,
            header_cells=['名称', '品牌', '规格型号', '数量', '单价'],
            item_index=0,
            item_count=1,
            currency_context_match=None,
            price_heading='单价',
        )
        if record is not None:
            # 这一行**没有**表头行，字段名是页面自己印的标签。标出来，免得下游把它
            # 当成一张真表去读"表头"。
            record.table_evidence['layout'] = 'label_line'
            results.append(record)
    return [r.to_dict() for r in results]
