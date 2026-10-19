"""线索对齐闸门：公告里的机器，是不是线索里要买的那一台？

为什么有这个模块
----------------
召回用的是**品类词**（"培养箱"、"生物安全柜"），所以命中的是"含该词的所有项目"。
`docs/76` §1 事实 #6 与 §9 的三个案例说明：全链路没有一步检查"这条公告里的机器
是不是用户要买的那台"，而这些公告又几乎全是 `.gov.cn` / `.edu.cn`、`authority_score=85`，
光靠 `source_authority.py` 的权威性判据**一条都筛不掉**。本模块就补这一步。

三条不变式（改这个文件之前先读这三条）
--------------------------------------
1. **权威性再高也不许掩盖硬冲突。** 硬冲突（型号不符 / 供应商不符）为 `rejected`，
   且与得分无关——`combined_score` 可以很高（高权威 + 同时效 = 0.5 打底）。
2. **只有可确定性判断的信号参与判定**：公告里出现目标型号（归一化后相等）、
   公告声明的供应商与线索一致、买方一致、金额在容差内。型号/品牌的**表格行抽取是启发式**，
   启发式只能用于 `rejected`（"公告里写的是别的型号"），**永远不能用于 `verified`**——
   所以启发式抽错只会让结论偏保守，不会凭空造出一个 `verified`。
3. **说不清就 `unresolved`，并且必须带上试过的查询式和时间窗**，让用户能判断
   "你到底试过什么"，不允许出现"搜到了但没核对"的中间态。

权威性来自 `backend/services/source_authority.py`，本模块不另写一套分级，只把它的
tier 映射成 0..1 的分量（见 `AUTHORITY_BY_TIER`）。

参考资料
--------
- `docs/76-聚合站线索对齐链.md` §1 事实 #6/#10、§2 对话 3、§9 三个回归案例
- `docs/80-线索对齐数据模型.md` §2.2（零填充 ISO）、§6（字段命名不对称）
- `docs/77-来源权威性与未结构化字段.md`（1300A2 与 1306A2 尺寸完全相同，是最容易被误判的一对）
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Iterable, Mapping, Sequence

from backend.services import source_authority

STATE_VERIFIED = "verified"
STATE_REJECTED = "rejected"
STATE_UNRESOLVED = "unresolved"

#: `source_authority` 的 tier → 0..1 权威分量。映射只有这一处，改只改这里。
AUTHORITY_BY_TIER = {
    source_authority.LOCAL_AUTHORITY: 1.0,
    source_authority.FORMAL_PUBLICATION: 0.75,
}
#: 非本地域名、也没有正式发布证据：不能算官方，但也未必是聚合站。
AUTHORITY_UNCLASSIFIED = 0.35
#: 聚合站 / 商户页面。
AUTHORITY_MARKETPLACE = 0.05

#: 字段吻合的固定权重；线索里没有的字段按 0 计（不重算分母，保持跨案例可比）。
WEIGHT_MODEL = 0.5
WEIGHT_SUPPLIER = 0.3
WEIGHT_BUYER = 0.1
WEIGHT_AMOUNT = 0.1

#: 金额容差：相对偏差 ≤ 满分，≤ 50% 减半，其余 0。
AMOUNT_TOLERANCE = 0.15
AMOUNT_TOLERANCE_WIDE = 0.5

_UNIT_MULTIPLIER = {"万元": 10000.0, "千元": 1000.0, "百元": 100.0, "元": 1.0}

_MODEL_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-._/+]{1,30}")
_LONG_DIGIT_RUN = re.compile(r"\d{6,}")
_URL_RE = re.compile(r"https?://\S+")
_MONEY_LABEL_RE = re.compile(
    r"(?:总中标金额|中标\s*[（(]成交[）)]\s*金额|成交金额|中标金额|中标价|成交价|合同金额)"
    r"[^\n0-9￥¥]{0,12}?[￥¥]?[ \t]*([\d,]+(?:\.\d+)?)[ \t]*[（(]?[ \t]*(万元|千元|百元|元)?"
)
_SUPPLIER_LABEL_RE = re.compile(
    r"(?:成交供应商|中标供应商|供应商名称|成交人|中标人|成交单位|中标单位|供应商)"
    r"(?:\s*[（(][^）)]{0,10}[）)]?)?\s*[：:]\s*\|?\s*([^\n|：:]{2,60})"
)
_SUPPLIER_TABLE_HEADER_RE = re.compile(r"供应商名称\s*\|[^\n]*(?:供应商地址|中标|成交)")
_COMPANY_SUFFIXES = ("公司", "中心", "工厂", "厂", "集团", "商行", "商店", "研究院", "研究所", "事务所", "经营部")
_INVALID_DATES = {"", "nan", "none", "null", "-"}
_AMOUNT_TEXT_RE = re.compile(r"(-?\d+(?:\.\d+)?)\s*(万元|千元|百元|万|千|百|元)?")


# ---------------------------------------------------------------------------
# 线索输入
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LeadProfile:
    """对齐用的一条线索（买方/卖方/型号/金额/日期/品类）。

    对齐层要用的是"可比较的值"，聚合站抓来的原文是纯文本（`docs/76` 对话 4 才补结构），
    所以这里统一用 `coerce_lead` 把两种形态收进本对象。金额一律折算成**元**。
    """

    model: str | None = None
    brand: str | None = None
    supplier: str | None = None
    buyer: str | None = None
    amount: float | None = None
    amount_text: str | None = None
    date: str | None = None
    category: str | None = None
    project_id: str | None = None
    candidate_id: str | None = None
    window_start: str | None = None
    window_end: str | None = None


_LEAD_ALIASES = {
    "model": ("model", "model_no", "model_number", "型号"),
    "brand": ("brand", "brand_name", "品牌"),
    "supplier": ("supplier", "supplier_name", "seller", "卖方", "供应商", "成交供应商", "中标供应商"),
    "buyer": ("buyer", "buyer_name", "purchaser", "purchaser_name", "采购人", "采购单位", "买方"),
    "amount": ("amount", "price", "supplier_price", "quote", "金额"),
    "amount_text": ("amount_text", "amount_raw", "price_text"),
    "date": ("date", "notice_date", "publication_date", "publish_date", "date_text", "日期"),
    "category": ("category", "item", "item_name", "品类", "标的名称"),
    "project_id": ("project_id", "project_no", "project_code", "项目编号"),
    "candidate_id": ("candidate_id", "candidate", "id"),
    "window_start": ("window_start",),
    "window_end": ("window_end",),
}


def _raw(source: Any, names: Sequence[str]) -> Any:
    for name in names:
        if isinstance(source, Mapping):
            if name in source:
                return source[name]
            continue
        if hasattr(source, name):
            return getattr(source, name)
    return None


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _clean_iso_date(value: Any) -> str | None:
    """零填充 ISO 日期才参与时效打分；其它写法原样保留但不解析（不猜）。"""
    text = _clean_text(value)
    if text is None or text.casefold() in _INVALID_DATES:
        return None
    return text


def parse_amount(value: Any) -> float | None:
    """把 ``3.25万`` / ``¥28,500`` / ``962000元`` 折算成元；认不出就 ``None``。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if value >= 0 else None
    text = _clean_text(value)
    if text is None:
        return None
    match = _AMOUNT_TEXT_RE.search(text.replace(",", "").replace("，", ""))
    if not match:
        return None
    amount = float(match.group(1))
    if amount < 0:
        return None
    unit = match.group(2)
    if unit in ("万元", "万"):
        amount *= 10000.0
    elif unit in ("千元", "千"):
        amount *= 1000.0
    elif unit in ("百元", "百"):
        amount *= 100.0
    return amount


def coerce_lead(lead: Any) -> LeadProfile:
    """把任意形态的线索（`LeadProfile` / mapping / 任何带属性的对象）收成 `LeadProfile`。"""
    if isinstance(lead, LeadProfile):
        return lead
    if lead is None:
        return LeadProfile()
    values: dict[str, Any] = {}
    for field_name, aliases in _LEAD_ALIASES.items():
        values[field_name] = _raw(lead, aliases)
    amount = parse_amount(values["amount"])
    if amount is None:
        amount = parse_amount(values["amount_text"])
    return LeadProfile(
        model=_clean_text(values["model"]),
        brand=_clean_text(values["brand"]),
        supplier=_clean_text(values["supplier"]),
        buyer=_clean_text(values["buyer"]),
        amount=amount,
        amount_text=_clean_text(values["amount_text"]) or _clean_text(values["amount"]),
        date=_clean_iso_date(values["date"]),
        category=_clean_text(values["category"]),
        project_id=_clean_text(values["project_id"]),
        candidate_id=_clean_text(values["candidate_id"]),
        window_start=_clean_iso_date(values["window_start"]),
        window_end=_clean_iso_date(values["window_end"]),
    )


# ---------------------------------------------------------------------------
# 型号归一化
# ---------------------------------------------------------------------------


def normalize_model(value: Any) -> str:
    """型号归一化：只留字母数字，小写。

    ``1300A2`` / ``1300 A2`` / ``1300A2 Ⅱ级`` → ``1300a2``；``SPX-150BIII`` → ``spx150biii``。
    **比较用相等，不用包含**：``spx150b`` 是 ``spx150biii`` 的子串，用包含判定会把
    绿博 ``SPX-150B`` 认成泰斯特 ``SPX-150BIII``；``1300A2`` 与 ``1306A2`` 尺寸数值
    完全相同（`docs/77`），也必须靠数字区分。
    """
    if not isinstance(value, str):
        return ""
    return re.sub(r"[^0-9a-z]+", "", value.casefold())


def is_same_model(left: Any, right: Any) -> bool:
    """两个型号写法是否指同一台机器；任一侧归一化后为空则一律 ``False``。"""
    a, b = normalize_model(left), normalize_model(right)
    return bool(a) and a == b


def model_tokens(text: Any) -> frozenset[str]:
    """正文里出现的"像型号"的 token（归一化后）。

    规则：同时含字母和数字、不含 6 位以上连续数字（排除 ``A02109900`` 这类品目编码）、
    不在 URL 里。用于"公告里写的是别的型号"这类判定，以及反例检查。
    """
    if not isinstance(text, str) or not text:
        return frozenset()
    stripped = _URL_RE.sub(" ", text)
    tokens = set()
    for raw in _MODEL_TOKEN_RE.findall(stripped):
        token = raw.strip(".-_/+")
        if len(token) < 3:
            continue
        if _LONG_DIGIT_RUN.search(token):
            continue
        if not re.search(r"[A-Za-z]", token) or not re.search(r"\d", token):
            continue
        normalized = normalize_model(token)
        if normalized:
            tokens.add(normalized)
    return frozenset(tokens)


def _target_pattern(normalized_target: str) -> re.Pattern:
    """目标型号的"允许内部分隔"正则。

    归一化后的型号要在**原文**里找，而原文可能写成 ``1300 A2`` / ``1300-A2`` / ``1300A2 Ⅱ级``
    （docs/76 明确要求视为同一型号）。逐个字符之间允许空格/连字符，两侧用字母数字**与连字符**
    边界卡住：``spx150b`` 不会匹配到 ``spx150biii``，``1300a2`` 也不会匹配到 ``1300a21``，
    ``spx250b`` 也不会匹配到 ``spx250b-zii`` —— ``-`` 落在边界上时后缀就不许被忽略
    （docs/185 §2.1）。边界口径与 ``procurement_rules_improved.contains_model`` 逐字一致，
    两处必须一起改，否则报告与 `verified` 判定会给出两种说法（docs/185 §5.1）。
    字符**之间**的分隔符仍只认空格与半角连字符，不在本次改动范围内。
    """
    body = r"[\s\-]*".join(re.escape(ch) for ch in normalized_target)
    return re.compile(r"(?<![0-9a-z\-‐‑‒–—―−])" + body + r"(?![0-9a-z\-‐‑‒–—―−])")


def model_present(text: Any, model: Any) -> bool:
    """原文里是否出现目标型号（容忍空格/连字符写法，但仍要求是完整型号）。

    这是**唯一**能把一条公告判成 `verified` 的型号判据，所以宁可保守：
    宁可漏判成 `unresolved`，也不许把 ``SPX-150B`` 认成 ``SPX-150BIII``。
    """
    normalized = normalize_model(model) if isinstance(model, str) else ""
    if not normalized or not isinstance(text, str) or not text:
        return False
    probe = _URL_RE.sub(" ", text).casefold()
    return bool(_target_pattern(normalized).search(probe))


def _normalize_name(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"[\s\u3000·,，。.、（）()\-]+", "", value.casefold())


def _names_agree(left: Any, right: Any) -> bool:
    """单位名一致判定：归一化后相等，或长名包含短名且短名占比 ≥0.6。"""
    a, b = _normalize_name(left), _normalize_name(right)
    if not a or not b:
        return False
    if a == b:
        return True
    if a in b:
        return len(a) / len(b) >= 0.6
    if b in a:
        return len(b) / len(a) >= 0.6
    return False


def _name_in(name: Any, haystack: str) -> bool:
    normalized = _normalize_name(name)
    if not normalized or not haystack:
        return False
    if normalized in haystack:
        return True
    # 公告常把公司名简称（去掉"有限公司"等尾巴），取前缀再比一次。
    for length in (len(normalized) - 2, len(normalized) - 4):
        if length >= 4 and normalized[:length] in haystack:
            return True
    return False


# ---------------------------------------------------------------------------
# 公告侧解析（启发式，只用于 rejected）
# ---------------------------------------------------------------------------


def _haystack(source: Any) -> str:
    parts = []
    for name in ("title", "snippet", "body_text", "tender_title"):
        value = _raw(source, (name,))
        if isinstance(value, str) and value:
            parts.append(value)
    return "\n".join(parts)


def _body(source: Any) -> str:
    value = _raw(source, ("body_text",))
    return value if isinstance(value, str) else ""


def _url(source: Any) -> str | None:
    value = _raw(source, ("url",))
    return value if isinstance(value, str) and value else None


def _publication_date(source: Any) -> str | None:
    return _clean_iso_date(_raw(source, ("publication_date", "notice_date", "date")))


def _declared_supplier(body: str) -> str | None:
    """从公告正文里读"公告声明的成交供应商"。读不出就 ``None``（不猜）。"""
    if not body:
        return None
    for match in _SUPPLIER_LABEL_RE.finditer(body):
        candidate = match.group(1).strip()
        candidate = candidate.split("|")[0].strip()
        if _looks_like_company(candidate):
            return candidate
    lines = body.splitlines()
    for index, line in enumerate(lines):
        if _SUPPLIER_TABLE_HEADER_RE.search(line) and index + 1 < len(lines):
            # 表头里的「供应商名称」不一定在第一列（库车那条公告第一列是序号），
            # 所以按列号取值，不能默认取首格。
            header_cells = [cell.strip() for cell in line.split("|")]
            column = next((i for i, cell in enumerate(header_cells) if cell.startswith("供应商名称")), 0)
            data_cells = [cell.strip() for cell in lines[index + 1].split("|")]
            cell = data_cells[column] if column < len(data_cells) else ""
            if _looks_like_company(cell):
                return cell
    # "成交供应商： | 公司名" 这类写法：标签后面紧跟一个空单元格。
    match = re.search(r"(?:成交供应商|中标供应商|供应商名称)\s*[：:]\s*\|?\s*\n\s*([^\n|]{2,60})", body)
    if match and _looks_like_company(match.group(1).strip()):
        return match.group(1).strip()
    return None


def _looks_like_company(value: str) -> bool:
    if not value or len(value) < 4 or len(value) > 60:
        return False
    if any(ch.isdigit() for ch in value):
        return False
    return any(value.endswith(suffix) for suffix in _COMPANY_SUFFIXES)


def _notice_amount(body: str) -> float | None:
    """公告级的"中标（成交）金额 / 总中标金额"，口径是**整包价**。"""
    if not body:
        return None
    match = _MONEY_LABEL_RE.search(body)
    if not match:
        return None
    try:
        amount = float(match.group(1).replace(",", ""))
    except ValueError:
        return None
    unit = match.group(2) or "元"
    return amount * _UNIT_MULTIPLIER.get(unit, 1.0)


def _category_keys(category: str | None, body: str) -> tuple[str, ...]:
    """品类家族词：``生化培养箱`` → ``('生化培养箱', '培养箱')``。

    取尾 3 字是为了让公告里同族的其它行（``电热恒温培养箱``）也进来，
    这样理由能说清"公告里写的是哪几台"，而不是只报一行。
    """
    if not category:
        return ()
    keys = []
    for key in (category, category[-3:] if len(category) > 3 else category):
        if len(key) >= 2 and key in body and key not in keys:
            keys.append(key)
    return tuple(keys)


def _row_pairs(body: str, keys: Sequence[str]) -> list[tuple[str, str | None, str]]:
    """按品类家族词取表格行的 ``(所在行品类词, 品牌, 型号)``。

    列序在真实公告里不固定（库车把型号放最后一列，杭锦旗放倒数第四列），
    所以不按列号取，按"型号 token 前后最近的单元格"取。这是启发式，只喂 rejected。
    """
    if not body or not keys:
        return []
    pairs: list[tuple[str, str | None, str]] = []
    for line in body.splitlines():
        matched_key = next((key for key in keys if key in line), None)
        if matched_key is None or "|" not in line:
            continue
        cells = [cell.strip() for cell in line.split("|")]
        for index, cell in enumerate(cells):
            tokens = model_tokens(cell)
            if not tokens or len(cells) < 3:
                continue
            for raw_token in re.findall(r"[A-Za-z0-9][A-Za-z0-9\-._/+]{2,30}", cell):
                if normalize_model(raw_token) not in tokens:
                    continue
                brand = _brand_before(cells, index, matched_key)
                pairs.append((matched_key, brand, raw_token.strip()))
    return pairs


def _brand_before(cells: Sequence[str], index: int, category_key: str | None) -> str | None:
    for offset in range(index - 1, max(-1, index - 4), -1):
        cell = cells[offset]
        if not cell or (category_key and category_key in cell):
            continue
        if re.fullmatch(r"[\d,.\s()（）台个套张件批只支]+", cell):
            continue
        if model_tokens(cell):
            continue
        if len(cell) <= 24:
            return cell
    return None


def _row_amount(cells: Sequence[str], model_index: int) -> tuple[float | None, float | None]:
    """命中行里的 ``(单价, 数量)``；先看型号后面，再看型号前面。

    真实行里的顺序两种都有（``… | 数量 | 单价 | 型号`` 与 ``… | 型号 | 数量 | 单价 | 总价``），
    所以按"离型号最近的大额数字"取单价，紧邻它前面的小数字取数量。
    """
    def numeric(cell: str) -> float | None:
        cleaned = cell.replace(",", "").split("(")[0].strip()
        try:
            return float(cleaned)
        except ValueError:
            return None

    order = list(range(model_index + 1, len(cells))) + list(range(model_index - 1, -1, -1))
    price_index = None
    for index in order:
        value = numeric(cells[index])
        if value is not None and value > 100:
            price_index = index
            break
    if price_index is None:
        return None, None
    price = numeric(cells[price_index])
    quantity = None
    for index in range(price_index - 1, max(-1, price_index - 3), -1):
        value = numeric(cells[index])
        if value is not None and 0 < value <= 10000:
            quantity = value
            break
    return price, quantity


# ---------------------------------------------------------------------------
# 判定
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AlignmentVerdict:
    """一条候选公告相对一条线索的判定结果。"""

    state: str
    reason: str
    combined_score: float
    field_match: float
    authority: float
    timeliness: float
    url: str | None = None
    matched_models: tuple[str, ...] = ()
    conflicting_models: tuple[str, ...] = ()
    declared_supplier: str | None = None
    buyer_matched: bool = False
    supplier_matched: bool = False
    model_matched: bool = False
    amount: float | None = None
    amount_basis: str | None = None
    notice_amount: float | None = None
    quantity: float | None = None
    publication_date: str | None = None
    attempted_queries: tuple[str, ...] = ()
    windows: tuple[tuple[str | None, str | None], ...] = ()

    def to_dict(self) -> dict:
        return {
            "state": self.state,
            "reason": self.reason,
            "combined_score": round(self.combined_score, 6),
            "field_match": round(self.field_match, 6),
            "authority": self.authority,
            "timeliness": self.timeliness,
            "url": self.url,
            "matched_models": list(self.matched_models),
            "conflicting_models": list(self.conflicting_models),
            "declared_supplier": self.declared_supplier,
            "buyer_matched": self.buyer_matched,
            "supplier_matched": self.supplier_matched,
            "model_matched": self.model_matched,
            "amount": self.amount,
            "amount_basis": self.amount_basis,
            "notice_amount": self.notice_amount,
            "quantity": self.quantity,
            "publication_date": self.publication_date,
            "attempted_queries": list(self.attempted_queries),
            "windows": [list(window) for window in self.windows],
        }


@dataclass
class AlignmentReport:
    """一条线索对一批候选公告的三堆结果。"""

    verified: list[AlignmentVerdict] = field(default_factory=list)
    rejected: list[AlignmentVerdict] = field(default_factory=list)
    unresolved: list[AlignmentVerdict] = field(default_factory=list)
    attempted_queries: tuple[str, ...] = ()
    windows: tuple[tuple[str | None, str | None], ...] = ()

    def to_dict(self) -> dict:
        return {
            "verified": [v.to_dict() for v in self.verified],
            "rejected": [v.to_dict() for v in self.rejected],
            "unresolved": [v.to_dict() for v in self.unresolved],
            "attempted_queries": list(self.attempted_queries),
            "windows": [list(window) for window in self.windows],
        }


def authority_component(source: Any) -> float:
    """来源权威性分量（0..1），判定完全交给 `source_authority`。"""
    url = _url(source)
    host = source_authority.host_of(url or "")
    body = _body(source)
    tier = source_authority.publisher_tier(host, body)
    if tier in AUTHORITY_BY_TIER:
        return AUTHORITY_BY_TIER[tier]
    if source_authority.is_marketplace_or_aggregator(host):
        return AUTHORITY_MARKETPLACE
    return AUTHORITY_UNCLASSIFIED


def _timeliness_component(lead_date: str | None, source_date: str | None) -> float:
    if not lead_date or not source_date:
        return 0.5
    try:
        lead_day = date.fromisoformat(lead_date)
        source_day = date.fromisoformat(source_date)
    except ValueError:
        return 0.5
    gap = abs((lead_day - source_day).days)
    if gap <= 90:
        return 1.0
    if gap <= 365:
        return 0.7
    if gap <= 730:
        return 0.4
    return 0.15


def lead_text_match(lead: Any, text: str) -> float:
    """只看标题/摘要/URL 时的线索吻合度（0..1），用于排序阶段（正文还没抓）。

    与 `verify_against_lead` 共用同一组权重，只少了金额一项（摘录里的金额不可结构化）。
    """
    profile = coerce_lead(lead)
    if not text:
        return 0.0
    haystack = _normalize_name(text)
    score = 0.0
    if profile.model and model_present(text, profile.model):
        score += WEIGHT_MODEL
    if profile.supplier and _name_in(profile.supplier, haystack):
        score += WEIGHT_SUPPLIER
    if profile.buyer and _name_in(profile.buyer, haystack):
        score += WEIGHT_BUYER
    return min(score, 1.0)


def verify_against_lead(
    source: Any,
    lead: Any,
    attempted_queries: Iterable[str] = (),
    windows: Iterable[Sequence[str | None]] = (),
) -> AlignmentVerdict:
    """一条候选公告 vs 一条线索 → `verified` / `rejected` / `unresolved`。

    `source` 可以是 `SourceRecord`（legacy 或 hardened）或任何带
    ``url`` / ``title`` / ``body_text`` / ``snippet`` / ``publication_date`` 的对象/mapping。
    `attempted_queries` / `windows` 会原样带进结论——`unresolved` 必须让用户看到试过什么。
    """
    profile = coerce_lead(lead)
    queries = tuple(str(q) for q in attempted_queries if str(q).strip())
    window_pairs = tuple((w[0], w[1]) for w in windows if len(tuple(w)) >= 2)

    body = _body(source)
    haystack = _haystack(source)
    haystack_normalized = _normalize_name(haystack)

    authority = authority_component(source)
    timeliness = _timeliness_component(profile.date, _publication_date(source))

    target_model = normalize_model(profile.model) if profile.model else ""
    model_matched = bool(target_model) and model_present(haystack, profile.model)
    matched_models = (target_model,) if model_matched else ()

    # 表格行抽取是启发式：只用来判定"公告里写的是别的型号"。
    keys = _category_keys(profile.category, body)
    pairs = _row_pairs(body, keys)
    conflicting = [pair for pair in pairs if normalize_model(pair[2]) != target_model] if target_model else []

    declared_supplier = _raw(source, ("supplier",)) or _declared_supplier(body)
    if not isinstance(declared_supplier, str) or not declared_supplier.strip():
        declared_supplier = None
    else:
        declared_supplier = declared_supplier.strip()

    supplier_conflict = bool(
        profile.supplier and declared_supplier
        and not _names_agree(profile.supplier, declared_supplier)
    )
    supplier_matched = bool(
        profile.supplier and declared_supplier and _names_agree(profile.supplier, declared_supplier)
    ) or bool(profile.supplier and not declared_supplier and _name_in(profile.supplier, haystack_normalized))

    model_conflict = bool(conflicting) and not model_matched

    buyer_matched = bool(profile.buyer) and (
        _name_in(profile.buyer, haystack_normalized)
    )

    row_price = row_quantity = None
    matched_label = None
    if model_matched:
        matched = _matched_row(body, target_model)
        if matched is not None:
            cells, index, raw_model, brand = matched
            matched_label = f"{brand} {raw_model}" if brand else raw_model
            row_price, row_quantity = _row_amount(cells, index)
        else:
            # 型号只在标题/摘要里出现（正文没抓到或只有附件），金额只能按整包口径记。
            row_price, row_quantity = None, None
    notice_amount = _notice_amount(body)
    if model_matched and row_price is not None:
        amount, amount_basis = row_price, "单台单价"
    elif notice_amount is not None:
        amount, amount_basis = notice_amount, "整包价"
    else:
        amount, amount_basis = row_price, None

    # ---- 字段吻合度：只算可确定性判断的信号 ----
    field_match = 0.0
    if model_matched:
        field_match += WEIGHT_MODEL
    if supplier_matched:
        field_match += WEIGHT_SUPPLIER
    if buyer_matched:
        field_match += WEIGHT_BUYER
    amount_signal = "n/a"
    if profile.amount and amount:
        relative = abs(profile.amount - amount) / max(profile.amount, amount)
        if relative <= AMOUNT_TOLERANCE:
            field_match += WEIGHT_AMOUNT
            amount_signal = "吻合"
        elif relative <= AMOUNT_TOLERANCE_WIDE:
            field_match += WEIGHT_AMOUNT / 2
            amount_signal = "接近"
        else:
            amount_signal = "不吻合"
    field_match = min(field_match, 1.0)

    combined = 0.5 * field_match + 0.3 * authority + 0.2 * timeliness

    notes: list[str] = []
    if supplier_conflict:
        notes.append(f"供应商不匹配：线索卖方「{profile.supplier}」，公告卖方「{declared_supplier}」")
    if model_conflict:
        rendered = "、".join(
            f"{key}/{brand} {model}".strip() if brand else f"{key}/{model}".strip()
            for key, brand, model in conflicting[:4]
        )
        notes.append(
            f"型号/品牌不匹配（公告内是{rendered}）：目标型号「{profile.model}」"
            "未在公告标题/摘要/正文中出现"
        )

    if model_conflict or supplier_conflict:
        state = STATE_REJECTED
        if amount_signal == "不吻合" and profile.amount and amount:
            notes.append(
                f"金额也不吻合（线索 {profile.amount:,.2f} 元 vs 公告 {amount_basis or '公告金额'} "
                f"{amount:,.2f} 元）"
            )
    elif model_matched or supplier_matched:
        state = STATE_VERIFIED
        evidence = []
        if model_matched:
            evidence.append(f"公告内「{matched_label or matched_models[0]}」与线索型号一致")
        if supplier_matched:
            evidence.append(f"公告卖方「{declared_supplier or profile.supplier}」与线索一致")
        if amount and amount_basis:
            evidence.append(f"{amount_basis} {amount:,.2f} 元")
        if buyer_matched:
            evidence.append(f"买方「{profile.buyer}」一致")
        notes.append("；".join(evidence) if evidence else "硬指标吻合")
        if row_quantity is not None:
            notes.append(f"数量 {row_quantity:g}")
    else:
        state = STATE_UNRESOLVED
        tried = "、".join(queries) if queries else "（调用方没有传查询式）"
        notes.append(f"证据不足：公告里没有出现目标型号「{profile.model or '未提供'}」，"
                     f"也没有可对照的同品类型号；已试查询式：{tried}")

    if state == STATE_UNRESOLVED and not queries:
        notes.append("提示：unresolved 必须带上已试查询式与时间窗，调用方需补齐")

    return AlignmentVerdict(
        state=state,
        reason="；".join(notes),
        combined_score=combined,
        field_match=field_match,
        authority=authority,
        timeliness=timeliness,
        url=_url(source),
        matched_models=matched_models,
        conflicting_models=tuple(normalize_model(pair[2]) for pair in conflicting[:4]),
        declared_supplier=declared_supplier,
        buyer_matched=buyer_matched,
        supplier_matched=supplier_matched,
        model_matched=model_matched,
        amount=amount,
        amount_basis=amount_basis,
        notice_amount=notice_amount,
        quantity=row_quantity,
        publication_date=_publication_date(source),
        attempted_queries=queries,
        windows=window_pairs,
    )


def _matched_row(body: str, target_model: str) -> tuple[list[str], int, str, str | None] | None:
    """定位公告里写着目标型号的那一行 → ``(单元格, 型号列号, 原文型号, 品牌)``。"""
    if not body or not target_model:
        return None
    for line in body.splitlines():
        if "|" not in line:
            continue
        cells = [cell.strip() for cell in line.split("|")]
        for index, cell in enumerate(cells):
            raw = None
            if normalize_model(cell) == target_model:
                raw = cell
            else:
                for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9\-._/+]{2,30}", cell):
                    if normalize_model(token) == target_model:
                        raw = token.strip()
                        break
            if raw is None:
                continue
            return cells, index, raw, _brand_before(cells, index, None)
    return None


def _matched_row_amount(body: str, target_model: str) -> tuple[float | None, float | None]:
    """命中型号那一行的 ``(单价, 数量)``。型号所在单元格定位到行，再按邻格取数。"""
    matched = _matched_row(body, target_model)
    if matched is None:
        return None, None
    cells, index, _raw, _brand = matched
    return _row_amount(cells, index)


def align_sources(
    lead: Any,
    sources: Iterable[Any],
    attempted_queries: Iterable[str] = (),
    windows: Iterable[Sequence[str | None]] = (),
) -> AlignmentReport:
    """一条线索 vs 一批候选公告 → `verified` / `rejected`（带理由）/ `unresolved`（带已试查询式）。"""
    queries = tuple(str(q) for q in attempted_queries if str(q).strip())
    window_pairs = tuple((w[0], w[1]) for w in windows if len(tuple(w)) >= 2)
    report = AlignmentReport(attempted_queries=queries, windows=window_pairs)
    for source in sources or []:
        verdict = verify_against_lead(source, lead, attempted_queries=queries, windows=window_pairs)
        if verdict.state == STATE_VERIFIED:
            report.verified.append(verdict)
        elif verdict.state == STATE_REJECTED:
            report.rejected.append(verdict)
        else:
            report.unresolved.append(verdict)
    return report
