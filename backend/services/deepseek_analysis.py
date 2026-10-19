"""Conservative DeepSeek analysis of uploaded procurement documents."""
from __future__ import annotations

import json
import re
import unicodedata
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from backend.infrastructure.deepseek import DeepSeekClient, DeepSeekError
from backend.services.candidate_service import measurement_value_not_model


CANONICAL_FIELD_NAMES = (
    "product_identity_and_configuration",
    "core_function_and_applications",
    "detection_or_processing_modes",
    "capacity_and_throughput",
    "sample_and_format_compatibility",
    "performance_range",
    "accuracy_sensitivity_and_repeatability",
    "temperature_environment_and_safety",
    "software_data_and_compliance",
    "accessories_consumables_and_options",
    "physical_dimensions_power_and_installation",
    "price_warranty_delivery_and_service",
)


SYSTEM_PROMPT = """你是实验室仪器采购资料分析器。只根据用户提供的资料和采购场景输出 json，不补写资料外事实。
只返回一个完整 JSON 对象，不要 Markdown 或解释。结构必须遵守以下示例：
{"candidates":[{"brand":"品牌或null","series":"系列或null","model":"完整型号","sources":[{"document_id":"文档ID","page_or_location":"page:1","excerpt":"原文短摘录"}],"fields":[{"field_name":"参数名","value":"原文值或unknown","unit":"单位或null","status":"pending_review或unknown或conflicted","sources":[{"document_id":"文档ID","page_or_location":"page:1","excerpt":"原文短摘录"}]}]}]}
规则：
1. candidates 必须是数组；不同型号或配置不能合并。
2. 每个候选必须有非空 model；型号、品牌、系列不确定时不得猜测。
3. 每个候选的 fields 必须按以下 12 个 field_name 各输出一次，名称不得翻译或新增：
product_identity_and_configuration；core_function_and_applications；detection_or_processing_modes；capacity_and_throughput；sample_and_format_compatibility；performance_range；accuracy_sensitivity_and_repeatability；temperature_environment_and_safety；software_data_and_compliance；accessories_consumables_and_options；physical_dimensions_power_and_installation；price_warranty_delivery_and_service。
4. 同组多个具体参数可放入 value 对象；只收录资料明确提供且最影响选型的内容。每组最多 8 个键，每个值不超过 120 字；若使用字符串则不超过 500 字。不同量纲的参数必须各自携带单位和适用条件，不得输出脱离单位的裸数字，也不得把表头里的温度、配置条件省略。不要复制整段说明书或罗列营销描述。
5. 每个候选的 sources 最多 2 项；每个字段的 sources 为 0 到 8 项。有值时至少一项，每个子参数都须有对应摘录，所有数字（包括型号、条件）必须出现在摘录中。摘录不超过 120 字并给出准确页码；同一页同一原文不要重复。多个独立参数优先用 value 对象，不能把未获支持的值混成一个长字符串。
6. 没有证据时 value="unknown"、status="unknown"、sources=[]。同组原文冲突时 status=conflicted 并保留冲突来源；其余有证据字段为 pending_review。禁止输出 confirmed。
7. 整个 JSON 应保持精简，优先保证所有候选和 12 个固定分组完整闭合，不得因追求参数数量导致输出截断。"""

CANDIDATE_INVENTORY_PROMPT = """你是实验室仪器采购资料的候选清单整理器。输入是一个有字符预算的资料批次，可能来自 PDF 原生文本、逐页视觉识别或表格。
只返回完整 json 对象：{"candidates":[{"product_role":"instrument|instrument_configuration","brand":"品牌或null","series":null,"model":"产品型号","catalog_number":"货号、订货号、物料号或null","configuration":null,"sources":[{"document_id":"文档ID","page_or_location":"page:N/rendered","excerpt":"原文短摘录"}]}]}。
brand 只填资料正文里写明的厂名或商标（例：「上海天美科学仪器有限公司」「苏净安泰」）；正文没写就填 null，不得用品类常识、型号或目录名猜品牌。
只根据 evidence_units 中的 text 和 candidate_mentions 识别候选；原生文本与视觉结果同等处理。sources 必须沿用对应 unit 的 document_id 和 location，不得虚构页码。model 只能填写产品型号，不得用货号、订货号、物料号、SKU 或包装编号替代；这些编号只填 catalog_number。同一产品型号仅因电压、包装、是否含附件或订货号不同而形成的订购配置要合并为一个候选，并在 configuration 中精简汇总；只有资料明确给出不同产品型号时才拆分候选。product_role=instrument 仅用于可独立采购和运行的主仪器；product_role=instrument_configuration 仅用于主仪器的完整配置或明确变体，不包括注射器模块、微量板、转子、适配器等选配件。附件、试剂、耗材、配件、模块和软件不能列入 candidates，除非采购场景明确把它们作为采购对象。某个仪器系列若只在支架、转子、适配器等附件的兼容清单中被提及，且没有独立产品或规格证据，也不能列为候选。不得从品类常识猜测型号。每个候选最多 2 条来源，最多 50 个候选。"""

# Backwards-compatible name retained because the service originally only used
# visual pages. The prompt now deliberately covers both native and visual text.
VISUAL_INVENTORY_PROMPT = CANDIDATE_INVENTORY_PROMPT

VISUAL_DETAIL_PROMPT = SYSTEM_PROMPT + """
本次只分析用户给出的一个 candidate。返回 candidates 数组且恰好包含该候选一次；brand、series、model、configuration 必须沿用 candidate，不得新增、改名或返回其他候选。
用户提供的是后端筛选后的候选级有界证据，而不一定是整份文档。必须读取 evidence_selection：只能引用 documents 中实际提供的页、表格行和文字范围；omitted_page_count、omitted_selected_page_text_characters、omitted_selected_table_text_characters 或 text_truncated 表示还有内容未提供，不得把未提供内容推断为“不存在该参数”。证据不足的字段必须返回 unknown。"""

FIELD_GAP_PROMPT = """你是实验室仪器采购资料的字段补漏器。只根据用户提供的候选级有界证据，补充 requested_fields 指定的字段。
只返回完整 JSON 对象：{"fields":[{"field_name":"requested_fields 中的名称","value":"原文值或对象","unit":null,"status":"pending_review","sources":[{"document_id":"文档ID","page_or_location":"输入中的准确 location","excerpt":"不超过 120 字的原文短摘录"}]}]}。
字段语义：detection_or_processing_modes 是检测、测量或处理模式与工作原理；capacity_and_throughput 是容量、批量和通量；sample_and_format_compatibility 是样本、容器和格式兼容性；performance_range 是速度、作用力、量程、分辨率及其他性能上下限；accuracy_sensitivity_and_repeatability 是准确度、灵敏度、精度、检出限和重复性；temperature_environment_and_safety 是温湿度、噪声、安全和防护；software_data_and_compliance 是软件、数据、接口和合规认证；accessories_consumables_and_options 是标配、选配附件、耗材和试剂；physical_dimensions_power_and_installation 是尺寸、重量、电源、功率和安装条件；price_warranty_delivery_and_service 是价格、质保、交付、培训和售后。
只返回资料中有直接证据的字段；没有直接证据的字段不要输出。不得输出 requested_fields 之外的字段，不得猜测，不得输出 confirmed。每个字段最多 8 条来源；所有数字（包括型号、条件）必须出现在来源摘录中。多个独立参数使用 value 对象，每项附对应短摘录；无法引用的子参数不要填写。参考 current_fields 中已保留的参数和缺证项，优先补缺证项，沿用原键名，不重复改名输出已保留参数。每个值必须携带原文单位和适用条件，不同量纲不能共享一个 unit，也不能输出脱离单位的裸数字。"""


_CANDIDATE_KEYS = ("brand", "series", "model", "configuration")
_INVENTORY_BATCH_CHAR_LIMIT = 24_000
_INVENTORY_TEXT_CHUNK_CHARS = 5_000
_INVENTORY_TEXT_CHUNK_OVERLAP = 300
_INVENTORY_SOURCE_LIMIT = 8
_CANDIDATE_EVIDENCE_PAGE_LIMIT = 8
_CANDIDATE_EVIDENCE_FALLBACK_PAGES = 3
_CANDIDATE_EVIDENCE_PAGE_CHAR_LIMIT = 6_000
_CANDIDATE_IDENTITY_SOURCE_PAGE_LIMIT = 2
_CANDIDATE_EVIDENCE_TEXT_CHAR_LIMIT = (
    _CANDIDATE_EVIDENCE_PAGE_LIMIT * _CANDIDATE_EVIDENCE_PAGE_CHAR_LIMIT
)
_CANDIDATE_EVIDENCE_TABLE_CHAR_LIMIT = 6_000
_CANDIDATE_EVIDENCE_TABLE_TOTAL_CHAR_LIMIT = 12_000
_CANDIDATE_EVIDENCE_TABLE_ROW_LIMIT = 80
_FIELD_RETRIEVAL_TOP_K = 2
# Eight excerpts per model pass, plus two bounded repair passes. Merging must
# not discard a repair's evidence merely because the first pass filled its cap.
_FIELD_SOURCE_LIMIT = 24
_FIELD_RETRIEVAL_CHUNK_CHARS = 1_600
_FIELD_RETRIEVAL_CHUNK_OVERLAP = 200
_PAGE_LOCATION_RE = re.compile(r"(?:^|\b)page:(\d+)(?:\b|/)", re.IGNORECASE)
_NUMERIC_TOKEN_RE = re.compile(r"\d+(?:[.,]\d+)*(?:[eE][+-]?\d+)?")
_REFERENCE_FILLER_TOKENS = frozenset({
    "a", "an", "at", "from", "in", "located", "of", "on", "source", "the",
})
_REFERENCE_LOCATION_SUFFIX_RE = re.compile(
    r"(?:[\s,;，；\[(（]*)"
    r"(?:(?:in|at|on|from)\s+)?(?:page|p\.?)\s*[:#]?\s*\d+"
    r"(?:\s*/\s*rendered)?[\s\])）]*$",
    re.IGNORECASE,
)
_REFERENCE_CHINESE_LOCATION_SUFFIX_RE = re.compile(
    r"(?:[\s,;，；\[(（]*)(?:见|位于|来自)?第?\s*\d+\s*页[\s\])）]*$",
)
_CATALOG_HEADER_RE = re.compile(
    r"(?:catalog(?:ue)?\s*(?:number|no\.?|#)|order\s*(?:number|no\.?|#)|"
    r"part\s*(?:number|no\.?|#)|sku|货号|订货号|物料号|订购编号|产品编号)",
    re.IGNORECASE,
)
_ACCESSORY_CONTEXT_RE = re.compile(
    r"(?:accessor(?:y|ies)|adapter|rotor|rack|stand|holder|module|plate|"
    r"配件|附件|适配器|转子|支架|模块|微量板)",
    re.IGNORECASE,
)
_COMPATIBILITY_CONTEXT_RE = re.compile(
    r"(?:compatible\s+with|suitable\s+for|for\s+(?:the\s+)?(?:following|all)|"
    r"适用于|兼容|配套)",
    re.IGNORECASE,
)
_NON_INSTRUMENT_ROLE_RE = re.compile(
    r"(?:software|软件|consumable|耗材|reagent|试剂|accessor(?:y|ies)|配件|附件|"
    r"adapter|适配器|rotor|转子|rack|stand|holder|支架|module|模块|"
    r"assay(?:\s+or\s+setting)?|setting|preset|检测方法|检测设置|界面预设|"
    r"cross[ _-]?promotion|交叉推广)",
    re.IGNORECASE,
)
_PACKAGING_CONTEXT_RE = re.compile(
    r"(?:individually\s+packaged|disposable|pack(?:ed|aging)?|box\b|"
    r"单独包装|一次性|包装|\d+\s*(?:片|个|支|盒|包|枚)\s*/\s*(?:箱|盒|包))",
    re.IGNORECASE,
)
_SETTING_UI_CONTEXT_RE = re.compile(
    r"(?:measurement\s+wavelength|reference\s+well|bandwidth|input\s+data|"
    r"测量波长|参比孔|带宽|输入数据)",
    re.IGNORECASE,
)
_CROSS_PROMOTION_CONTEXT_RE = re.compile(
    r"(?:(?:besides|in addition to|除了).{0,180}(?:also|another|还有|另有).{0,180}"
    r"(?:more\s+information|learn\s+more|更多信息|详情)|"
    r"(?:more\s+information|learn\s+more|更多信息|详情).{0,180}"
    r"(?:besides|in addition to|除了).{0,180}(?:also|another|还有|另有))",
    re.IGNORECASE | re.DOTALL,
)

# Category-neutral procurement vocabulary. Terms describe the stable output
# contract rather than any instrument family, so new product categories use the
# same retrieval path without code changes.
_FIELD_RETRIEVAL_TERMS: dict[str, tuple[str, ...]] = {
    "product_identity_and_configuration": (
        "model", "catalog number", "order number", "configuration", "series",
        "型号", "货号", "订货号", "配置", "系列",
    ),
    "core_function_and_applications": (
        "function", "application", "applications", "intended use", "purpose",
        "功能", "应用", "用途", "适用范围",
    ),
    "detection_or_processing_modes": (
        "detection mode", "processing mode", "measurement mode", "method",
        "technology", "principle", "检测模式", "处理模式", "测量模式", "方法",
        "技术原理", "工作原理",
    ),
    "capacity_and_throughput": (
        "throughput", "capacity", "volume", "samples per hour", "tests per hour", "batch",
        "speed", "通量", "处理量", "容量", "容积", "每小时", "批处理", "速度",
    ),
    "sample_and_format_compatibility": (
        "sample type", "specimen", "format", "compatible", "compatibility",
        "sample volume", "样本类型", "样品类型", "兼容", "格式", "样本体积", "进样量",
    ),
    "performance_range": (
        "performance", "measurement range", "dynamic range", "range", "minimum",
        "maximum", "resolution", "性能", "量程", "动态范围", "范围", "最小", "最大", "分辨率",
    ),
    "accuracy_sensitivity_and_repeatability": (
        "accuracy", "sensitivity", "precision", "repeatability", "reproducibility",
        "coefficient of variation", "limit of detection", "lod", "准确度", "灵敏度",
        "精度", "重复性", "重现性", "变异系数", "检出限",
    ),
    "temperature_environment_and_safety": (
        "operating temperature", "temperature", "humidity", "environment", "safety",
        "noise", "温度", "湿度", "环境条件", "安全", "防护", "噪声",
    ),
    "software_data_and_compliance": (
        "software", "data export", "data management", "interface", "connectivity",
        "network", "compliance", "certification", "软件", "数据导出", "数据管理",
        "接口", "联网", "合规", "认证",
    ),
    "accessories_consumables_and_options": (
        "accessories", "consumables", "options", "included", "optional", "reagent",
        "配件", "耗材", "选件", "标配", "选配", "试剂",
    ),
    "physical_dimensions_power_and_installation": (
        "dimensions", "dimension", "weight", "power", "voltage", "installation",
        "footprint", "尺寸", "外形", "重量", "功率", "电源", "电压", "安装", "占地",
    ),
    "price_warranty_delivery_and_service": (
        "price", "quotation", "warranty", "delivery", "lead time", "service",
        "support", "training", "价格", "报价", "保修", "质保", "交付", "货期", "售后",
        "服务", "培训",
    ),
}


class AnalysisValidationError(ValueError):
    pass


def _source(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        return {"document_id": None, "page_or_location": None, "excerpt": ""}
    return {"document_id": raw.get("document_id"), "page_or_location": raw.get("page_or_location", raw.get("location", raw.get("page"))),
            "excerpt": str(raw.get("excerpt", ""))[:2000]}


def _table_rows_text(table: Mapping[str, Any]) -> str:
    return "\n".join(
        "\t".join(str(cell) for cell in row)
        for row in table.get("rows", []) if isinstance(row, (list, tuple))
    )


def _enterprise_brand_from_documents(
    documents: list[Mapping[str, Any]],
) -> tuple[str | None, list[str]]:
    """Maker named in the document body; a brand value only when it is unique.

    Returns ``(brand_or_None, all_names)``. The brand is filled only when the
    owned documents name exactly one maker: two names mean the page carries a
    maker *and* a supplier (or two products), and picking one would put an
    unverified brand on the candidate. No name at all keeps ``brand`` null --
    a manual that never prints a company name must not acquire one.

    Why this exists at all (``sample-d``, 111-page text-layer UV1100 datasheet):
    the maker is printed on the last contact page
    (``上海天美科学 仪器 有限公司``), but that page names no model, so
    ``scoped_documents`` drops it from the candidate-level evidence and the
    inventory model never sees it -- ``brand`` came back null, candidate
    discovery had no brand to require agreement on, and the lane found nothing.
    """
    from backend.services.procurement_rules import (
        brand_from_enterprise_name, enterprise_names_in_text,
    )
    names: list[str] = []
    for doc in documents:
        texts: list[str] = []
        pages = doc.get("pages")
        if isinstance(pages, list):
            texts.extend(str(page.get("text", "")) for page in pages if isinstance(page, Mapping))
        tables = doc.get("tables")
        if isinstance(tables, list):
            texts.extend(_table_rows_text(table) for table in tables if isinstance(table, Mapping))
        for text in texts:
            for name in enterprise_names_in_text(text):
                if name not in names:
                    names.append(name)
    if len(names) != 1:
        return None, names
    return brand_from_enterprise_name(names[0]) or None, names


def _documents_text_excerpt(documents: list[Mapping[str, Any]], limit: int = 400) -> str:
    """资料正文的一小段，只为让留痕能定位到"就是这几页没写厂名"。"""
    parts: list[str] = []
    for doc in documents:
        pages = doc.get("pages")
        if isinstance(pages, list):
            parts.extend(str(page.get("text") or "") for page in pages if isinstance(page, Mapping))
        tables = doc.get("tables")
        if isinstance(tables, list):
            parts.extend(_table_rows_text(table) for table in tables if isinstance(table, Mapping))
    return "\n".join(part for part in parts if part).strip()[:limit]


def _brand_unresolved_gap(models: list[str]) -> str:
    """品牌没从资料里读出来时给用户看的那一句话（人话，不带异常类名）。

    资料里本来就没有厂名（扫描件、单页说明书常常只印型号），这不是抽取失败：
    品牌照旧留空、一个字不猜。但它决定了下游能不能判"跨品牌相似品"——候选发现
    拿原型号的品牌去认"其他品牌"，品牌为空时全部候选只能当参考候选。静默留空
    等于把这件事抹掉（``docs/209`` §3.4），所以如实写一条、不阻断流程。
    """
    listed = "、".join(dict.fromkeys(model for model in models if model)) or "本轮候选"
    return (f"{listed}：资料里没有厂名，未自动填入品牌，"
            "因此无法判定跨品牌相似品，请人工指定品牌。")


def _brand_unresolved_failure(models: list[str], names: list[str],
                              documents: list[Mapping[str, Any]]) -> dict[str, Any]:
    """同一条事实的排查版：出错位置、脱敏原文、原因；与 ``run['failures']`` 同形。

    ``type`` 是这条留痕的类别（与候选发现那一侧的 ``brand_unresolved`` 同名），
    **不是抛出来的异常类**——资料里本来没有厂名，编一个异常类名等于把"资料限制"
    说成"程序故障"（``api.routes._visual_failure_records`` 对旧记录同样"如实写明
    而不是编一个"）。``error`` 与 ``excerpt`` 都先过 ``redact_error_text``：留痕
    要能定位到具体那几页，但不能把资料里的号码一起落库。
    """
    import traceback

    from backend.services.procurement_research import redact_error_text
    frame = traceback.extract_stack()[-2]
    listed = "、".join(model for model in models if model) or "未记录"
    return {
        "stage": "brand_extraction",
        "type": "BrandUnresolved",
        "code": "brand_unresolved",
        "location": {"function": frame.name, "line": frame.lineno,
                     "file": frame.filename.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]},
        "models": [model for model in models if model],
        "error": redact_error_text(
            f"资料正文与表格里没有可用的厂名（企业名匹配到 {len(names)} 个），品牌留空；"
            f"候选型号：{listed}"),
        "excerpt": redact_error_text(_documents_text_excerpt(documents))[:240],
        "note": "资料里本来没有厂名，不是异常、也不阻断分析；type 是这条留痕的类别。",
    }


def _source_evidence_index(documents: list[Mapping[str, Any]]) -> dict[tuple[str, str], str]:
    """Index exactly the page/table evidence made available to a model call."""
    indexed: defaultdict[tuple[str, str], list[str]] = defaultdict(list)
    for doc in documents:
        document_id = str(doc.get("document_id", doc.get("id")) or "")
        if not document_id:
            continue
        for page in doc.get("pages", []) if isinstance(doc.get("pages", []), list) else []:
            if not isinstance(page, Mapping):
                continue
            location = page.get("location", page.get("source_location"))
            if location:
                evidence_parts = [str(page.get("text", ""))]
                visual_fields = page.get("visual_fields")
                if isinstance(visual_fields, Mapping):
                    evidence_parts.extend(_scalar_texts(visual_fields))
                indexed[(document_id, str(location))].append("\n".join(evidence_parts))
        for table in doc.get("tables", []) if isinstance(doc.get("tables", []), list) else []:
            if not isinstance(table, Mapping):
                continue
            location = table.get("source_location") or f"table:{table.get('index', 'unknown')}"
            indexed[(document_id, str(location))].append(_table_rows_text(table))
    return {key: "\n".join(parts) for key, parts in indexed.items()}


def _canonical_number(raw: str) -> str:
    value = raw
    if "," in value and "." not in value:
        if re.fullmatch(r"\d{1,3}(?:,\d{3})+", value):
            value = value.replace(",", "")
        else:
            value = value.replace(",", ".")
    else:
        value = value.replace(",", "")
    try:
        number = Decimal(value)
    except InvalidOperation:
        return value.casefold()
    if not number.is_finite():
        return value.casefold()
    normalized = format(number.normalize(), "f")
    return normalized.rstrip("0").rstrip(".") if "." in normalized else normalized


def _numeric_tokens(value: Any) -> set[str]:
    tokens: set[str] = set()
    for scalar in _scalar_texts(value):
        normalized = unicodedata.normalize("NFKC", scalar)
        tokens.update(_canonical_number(match.group(0)) for match in _NUMERIC_TOKEN_RE.finditer(normalized))
    return tokens


def _is_local_ordered_subsequence(needles: list[str], haystack: list[str]) -> bool:
    if not needles:
        return False
    position = 0
    first_match = None
    last_match = None
    for index, token in enumerate(haystack):
        if token == needles[position]:
            first_match = index if first_match is None else first_match
            last_match = index
            position += 1
            if position == len(needles):
                span = int(last_match) - int(first_match) + 1
                return span <= len(needles) + max(2, len(needles) // 2)
    return False


def _excerpt_matches_evidence(excerpt: Any, evidence: str) -> bool:
    excerpt_text = str(excerpt or "").strip()
    if not excerpt_text or not evidence.strip():
        return False
    excerpt_core = _REFERENCE_LOCATION_SUFFIX_RE.sub("", excerpt_text).strip()
    excerpt_core = _REFERENCE_CHINESE_LOCATION_SUFFIX_RE.sub("", excerpt_core).strip()
    normalized_excerpt = _search_text(excerpt_core)
    normalized_evidence = _search_text(evidence)
    compact_excerpt = normalized_excerpt.replace(" ", "")
    compact_evidence = normalized_evidence.replace(" ", "")
    if compact_excerpt and compact_excerpt in compact_evidence:
        return True
    clauses = [
        _search_text(clause).replace(" ", "")
        for clause in re.split(r"[;；\n]+", excerpt_core)
        if _search_text(clause).replace(" ", "")
    ]
    if len(clauses) > 1 and all(clause in compact_evidence for clause in clauses):
        return True

    # Permit only a tight ordered overlap after removing an explainable trailing
    # location phrase (for example "X-100 in page:1"). Wide, page-level token
    # combinations remain rejected as non-verbatim citations.
    excerpt_tokens = [
        token for token in normalized_excerpt.split()
        if token not in _REFERENCE_FILLER_TOKENS
    ]
    return len(excerpt_tokens) >= 2 and _is_local_ordered_subsequence(
        excerpt_tokens, normalized_evidence.split(),
    )


def _validated_sources(raw_sources: Any,
                       evidence_by_location: Mapping[tuple[str, str], str],
                       *, required_value: Any = None) -> list[dict[str, Any]]:
    validated = []
    for raw in raw_sources if isinstance(raw_sources, list) else []:
        source = _source(raw)
        key = (
            str(source.get("document_id") or ""),
            str(source.get("page_or_location") or ""),
        )
        evidence = evidence_by_location.get(key)
        if evidence is not None and _excerpt_matches_evidence(source.get("excerpt"), evidence):
            validated.append(source)
    required_numbers = _numeric_tokens(required_value)
    if required_numbers:
        cited_numbers: set[str] = set()
        for source in validated:
            cited_numbers.update(_numeric_tokens(source.get("excerpt")))
        if not required_numbers.issubset(cited_numbers):
            return []
    return validated


_TEMPERATURE_METRICS = {
    "spatial": re.compile(r"(?:温度|温场)\s*均匀(?:性|度)?|temperature\s+(?:(?:spatial|spatially)\s+)?(?:uniformity|variation)", re.I),
    "temporal": re.compile(r"温度\s*(?:波动|稳定)(?:性|度)?|temperature\s+(?:(?:temporal|over\s+time)\s+)?(?:fluctuation|stability)", re.I),
}


def _temperature_metric(label: Any) -> str | None:
    text = unicodedata.normalize('NFKC', str(label)).replace('_', ' ')
    matches = [kind for kind, pattern in _TEMPERATURE_METRICS.items() if pattern.search(text)]
    return matches[0] if len(matches) == 1 else None


def _metric_excerpt_supports(name: str, value: Any, sources: list[dict[str, Any]]) -> bool:
    """Keep easily confused chamber metrics attached to their own evidence row.

    This vocabulary applies to incubators, ovens, baths and other temperature
    chambers, independent of brand or model. Unknown labels retain the existing
    validation; this is not a general claim of semantic verification.
    """
    metric = _temperature_metric(name)
    if not metric:
        return True
    required = _numeric_tokens({'label': name, 'value': value})
    for source in sources:
        text = unicodedata.normalize('NFKC', source['excerpt']).replace('_', ' ')
        headings = sorted((m.start(), m.end(), kind) for kind, pattern in _TEMPERATURE_METRICS.items()
                          for m in pattern.finditer(text))
        for index, (start, end, kind) in enumerate(headings):
            if kind != metric:
                continue
            stop = headings[index + 1][0] if index + 1 < len(headings) else len(text)
            row = re.split(r'[;；\n]', text[start:stop], maxsplit=1)[0]
            if required and required.issubset(_numeric_tokens(row)):
                return True
    return False


#: 尺寸三连：``1340×850×2120``（分隔符有 ``×``/``x``/``X``/``*``）。
_DIMENSION_TRIPLE_RE = re.compile(r'^\s*\d[\d.,]*\s*[×xX*]\s*\d[\d.,]*\s*[×xX*]\s*\d[\d.,]*\s*$')
#: 长度单位只收无歧义的多字符写法。单个拉丁字母（``m``）在 ``(EN12469)``、
#: ``(ISO 14664.1 Class3)`` 这种括号里太容易误命中，宁可漏收也不误收。
_LENGTH_UNIT_RE = re.compile(r'(?<![A-Za-z0-9])(?:mm|cm|dm|km|μm|um|nm|英寸|毫米|厘米)(?![A-Za-z0-9])')
#: 尺寸行必须自己写了轴：只有 ``(mm)`` 而没有轴的三连数字与其他三连数字无法区分。
_DIMENSION_AXES = ('长', '宽', '高', '深')


def _label_declares_size_unit(name: str, value: Any) -> bool:
    """子项标签自己声明了「这是个带长度单位的尺寸三连」吗。

    真实数据（``output/bio-instrument-acceptance-20260920``，知楚 A2 生物安全柜
    参数表）里，模型把单位写在键名上：``外形尺寸(长×宽×高mm)(支架高度720)``
    对应 ``1340×850×2120``、``内部尺寸(长×深×高mm)`` 对应 ``1270×580×660``。
    这两种键名里单位（``mm``）与三个轴都写着，值因此**不是**「脱离单位的裸
    数字」——单位原样留在键名上，这里不往值里补任何字符，只决定要不要因为它
    没有字段级 ``unit`` 就把它降级成未结构化。

    三条同时成立才放行：值是三连数字、标签写了至少两个轴、标签里有长度单位。
    表头没有单位的行（``气体交换体积 (风速额定值)`` + ``485``）继续按裸数字拒收。
    """
    text = str(name or '')
    if not _DIMENSION_TRIPLE_RE.match(str(value or '')):
        return False
    if sum(axis in text for axis in _DIMENSION_AXES) < 2:
        return False
    return bool(_LENGTH_UNIT_RE.search(text))


def _missing_quantity_unit(name: str, value: Any, field: Mapping[str, Any]) -> bool:
    if field.get('field_name') not in {
        'capacity_and_throughput', 'performance_range', 'accuracy_sensitivity_and_repeatability',
        'temperature_environment_and_safety', 'physical_dimensions_power_and_installation',
    }:
        return False
    if isinstance(value, Mapping) or isinstance(value, bool):
        return False
    text = ' '.join(map(str, value)) if isinstance(value, (list, tuple)) else str(value)
    bare = bool(re.fullmatch(r'[\s\d.,…±+−\-/×xX~:至到—–]+', text))
    if not bare:
        return False
    # Counts and dimensionless measurements are legitimate without a unit.
    if re.search(r'数量|个数|数目|层数|孔数|相数|比率|(?<![A-Za-z])pH(?![A-Za-z])|\b(?:count|number|phases?|ratio)\b', name, re.I):
        return False
    unit = str(field.get('unit') or '').strip()
    # A comma-separated group of units cannot qualify each scalar separately.
    if unit and unit.casefold() not in {'unknown', 'null', 'none'} and not re.search(r'[;,；，]', unit):
        return False
    # 字段自己没有 unit 时，**尺寸三连**的键名可以自带单位，见
    # ``_label_declares_size_unit``。除此之外的裸数值照旧算缺单位。
    return not _label_declares_size_unit(name, value)


def _validated_field(raw: Mapping[str, Any],
                     evidence_by_location: Mapping[tuple[str, str], str]) -> dict[str, Any]:
    """Retain supported independent parameters; never split values from conditions.

    A mapping entry is atomic, including any nested value/unit/condition object.
    Unsupported string fragments cannot safely be separated and remain unknown.
    The source and numeric checks are identical to the whole-field guard.
    """
    field = dict(raw)
    # Keep a bounded, location-checked copy of the model's citations even when
    # strict value validation rejects the structured value.  The initial
    # delivery must remain inspectable: an unparsed value is not a confirmed
    # fact, but it is also not equivalent to having no source at all.
    raw_evidence = []
    for raw_source in raw.get("sources", []) if isinstance(raw.get("sources", []), list) else []:
        source = _source(raw_source)
        key = (str(source.get("document_id") or ""), str(source.get("page_or_location") or ""))
        evidence = evidence_by_location.get(key)
        if evidence is None:
            continue
        source["excerpt"] = str(evidence)[:2000]
        source["evidence_status"] = "unparsed"
        raw_evidence.append(source)
        if len(raw_evidence) >= _FIELD_SOURCE_LIMIT:
            break
    if raw_evidence:
        field["_raw_evidence"] = raw_evidence
    sources = _validated_sources(raw.get("sources", []), evidence_by_location)[:_FIELD_SOURCE_LIMIT]
    value = raw.get("value", "unknown")
    supported = _validated_sources(sources, evidence_by_location, required_value=value)
    missing = []
    if not supported:
        if isinstance(value, Mapping) and sources:
            kept = {}
            for name, part in value.items():
                # Without numbers, an unrelated string must not hitchhike on a
                # sibling's numeric citation when salvaging a rejected group.
                directly_cited = bool(_numeric_tokens(part)) or all(
                    any(_excerpt_matches_evidence(text, source["excerpt"]) for source in sources)
                    for text in _scalar_texts(part)
                ) and bool(_scalar_texts(part))
                if directly_cited and _validated_sources(sources, evidence_by_location, required_value=part):
                    kept[name] = part
                else:
                    missing.append(str(name))
            value = kept or "unknown"
            supported = sources if kept else []
        elif str(value).strip().casefold() not in {"", "unknown", "none", "null", "未知", "未提供"}:
            missing = ["整项"]
        if not supported:
            value = "unknown"
            field["unit"] = None
    if isinstance(value, Mapping):
        kept = {}
        for name, part in value.items():
            if _metric_excerpt_supports(str(name), part, sources) and not _missing_quantity_unit(str(name), part, raw):
                kept[name] = part
            else:
                missing.append(str(name))
        value = kept or 'unknown'
        if not kept:
            supported = []
            field['unit'] = None
    field.update(value=value, sources=supported)
    field["_unsupported_parts"] = missing
    field["_unsupported_values"] = {
        name: raw.get("value", {}).get(name) if isinstance(raw.get("value"), Mapping) else raw.get("value")
        for name in missing
    }
    if not supported:
        field["status"] = "unknown"
    return field


def _status(value: Any, *, has_source: bool) -> str:
    requested = str(value or "pending_review")
    if requested == "confirmed":
        requested = "pending_review"
    if requested not in {"pending_review", "unknown", "conflicted", "rejected"}:
        requested = "pending_review"
    return requested if has_source else "unknown"


def _value_identity(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return repr(value)


def _unparsed_field_evidence(field: Mapping[str, Any], fallback_sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Expose source text for a value that failed structural validation.

    These entries are deliberately separate from ``sources``: downstream
    confirmation and comparison code must not treat them as grounded facts.
    """
    raw = field.get("_raw_evidence")
    candidates = raw if isinstance(raw, list) else fallback_sources
    output = []
    seen = set()
    for source in candidates:
        if not isinstance(source, Mapping):
            continue
        excerpt = str(source.get("excerpt") or "").strip()
        if not excerpt:
            continue
        key = (source.get("document_id"), source.get("page_or_location"), excerpt)
        if key in seen:
            continue
        seen.add(key)
        output.append({
            "document_id": source.get("document_id"),
            "page_or_location": source.get("page_or_location"),
            "excerpt": excerpt[:2000],
            "evidence_status": "unparsed",
        })
        if len(output) >= _FIELD_SOURCE_LIMIT:
            break
    return output


def _page_number(location: Any) -> int | None:
    if isinstance(location, int):
        return location if location > 0 else None
    text = str(location or "").strip()
    if text.isdigit():
        number = int(text)
        return number if number > 0 else None
    match = _PAGE_LOCATION_RE.search(text)
    return int(match.group(1)) if match else None


def _search_text(value: Any) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return " ".join(re.sub(r"[\W_]+", " ", normalized).split())


def _scalar_texts(value: Any) -> list[str]:
    if isinstance(value, Mapping):
        output: list[str] = []
        for item in value.values():
            output.extend(_scalar_texts(item))
        return output
    if isinstance(value, (list, tuple, set)):
        output = []
        for item in value:
            output.extend(_scalar_texts(item))
        return output
    if value is None or isinstance(value, bool):
        return []
    text = str(value).strip()
    return [text] if text else []


def _candidate_terms(candidate: Mapping[str, Any]) -> list[tuple[str, str, str, int]]:
    """Return category-neutral identifiers ordered by evidential specificity."""
    weights = {"model": 400, "configuration": 240, "series": 180, "brand": 80}
    terms: dict[tuple[str, str], tuple[str, str, str, int]] = {}
    for key in _CANDIDATE_KEYS:
        for raw in _scalar_texts(candidate.get(key)):
            normalized = _search_text(raw)
            compact = normalized.replace(" ", "")
            if len(compact) < 2:
                continue
            terms[(key, compact)] = (key, normalized, compact, weights[key])
            for token in normalized.split():
                token_compact = token.replace(" ", "")
                # Short generic tokens (for example "96" or "pro") create
                # catalog-wide false hits. Mixed model identifiers remain useful.
                if len(token_compact) >= 4 or (
                    len(token_compact) >= 3
                    and any(char.isalpha() for char in token_compact)
                    and any(char.isdigit() for char in token_compact)
                ):
                    terms.setdefault(
                        (key, token_compact),
                        (key, token, token_compact, max(20, weights[key] // 2)),
                    )
    return sorted(terms.values(), key=lambda item: (-item[3], -len(item[2]), item[0], item[2]))


def _matched_candidate_terms(text: str, terms: list[tuple[str, str, str, int]]) -> list[tuple[str, int]]:
    normalized = _search_text(text)
    compact_text = normalized.replace(" ", "")
    matched: list[tuple[str, int]] = []
    for key, term, compact, score in terms:
        if term in normalized or (any(char.isdigit() for char in compact) and compact in compact_text):
            matched.append((key, score))
    return matched


def _retrieval_text_chunks(text: Any) -> list[tuple[int, int, str]]:
    value = str(text or "")
    if not value:
        return []
    chunks: list[tuple[int, int, str]] = []
    start = 0
    while start < len(value):
        end = min(len(value), start + _FIELD_RETRIEVAL_CHUNK_CHARS)
        chunks.append((start, end, value[start:end]))
        if end == len(value):
            break
        start = max(start + 1, end - _FIELD_RETRIEVAL_CHUNK_OVERLAP)
    return chunks


def _retrieval_term_matches(normalized_text: str, compact_text: str, raw_term: str) -> bool:
    term = _search_text(raw_term)
    if not term:
        return False
    compact = term.replace(" ", "")
    if any("\u4e00" <= char <= "\u9fff" for char in compact):
        return compact in compact_text
    return f" {term} " in f" {normalized_text} "


def _field_scores_for_text(text: Any) -> dict[str, dict[str, Any]]:
    normalized = _search_text(text)
    if not normalized:
        return {}
    compact = normalized.replace(" ", "")
    contains_number = bool(_NUMERIC_TOKEN_RE.search(unicodedata.normalize("NFKC", str(text))))
    output: dict[str, dict[str, Any]] = {}
    for field_name in CANONICAL_FIELD_NAMES:
        matched_terms = [
            term for term in _FIELD_RETRIEVAL_TERMS[field_name]
            if _retrieval_term_matches(normalized, compact, term)
        ]
        if not matched_terms:
            continue
        # Longer and multi-word terms are more discriminative. A small numeric
        # bonus favors specification blocks over prose without making numbers a
        # standalone retrieval signal.
        score = sum(
            10 + min(12, len(_search_text(term).replace(" ", "")))
            + (8 if " " in _search_text(term) else 0)
            for term in matched_terms
        )
        if contains_number:
            score += 6
        output[field_name] = {
            "score": score,
            "matched_terms": sorted(set(matched_terms)),
        }
    return output


def _field_retrieval_profile(text: Any) -> dict[str, Any]:
    field_scores: dict[str, int] = {}
    matched_terms: defaultdict[str, set[str]] = defaultdict(set)
    chunks: list[dict[str, Any]] = []
    for chunk_index, (start, end, chunk_text) in enumerate(_retrieval_text_chunks(text)):
        matches = _field_scores_for_text(chunk_text)
        if not matches:
            continue
        chunk_score = 0
        chunk_fields: dict[str, int] = {}
        chunk_terms: dict[str, list[str]] = {}
        for field_name, match in matches.items():
            score = int(match["score"])
            field_scores[field_name] = max(field_scores.get(field_name, 0), score)
            matched_terms[field_name].update(match["matched_terms"])
            chunk_score += score
            chunk_fields[field_name] = score
            chunk_terms[field_name] = list(match["matched_terms"])
        chunks.append({
            "chunk_index": chunk_index,
            "char_start": start,
            "char_end": end,
            "score": chunk_score,
            "field_scores": chunk_fields,
            "matched_terms": chunk_terms,
        })
    return {
        "score": sum(field_scores.values()),
        "field_scores": field_scores,
        "matched_terms": {
            field_name: sorted(terms) for field_name, terms in matched_terms.items()
        },
        "chunks": sorted(chunks, key=lambda chunk: (-chunk["score"], chunk["char_start"])),
    }


def _text_chunks(text: Any) -> list[tuple[int, int, str]]:
    value = str(text or "")
    if not value:
        return [(0, 0, "")]
    chunks: list[tuple[int, int, str]] = []
    start = 0
    while start < len(value):
        end = min(len(value), start + _INVENTORY_TEXT_CHUNK_CHARS)
        chunks.append((start, end, value[start:end]))
        if end == len(value):
            break
        start = max(start + 1, end - _INVENTORY_TEXT_CHUNK_OVERLAP)
    return chunks


def _inventory_units(prompt_docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    units: list[dict[str, Any]] = []
    for doc in prompt_docs:
        provenance = {
            "document_id": doc.get("document_id"),
            "source_name": doc.get("source_name"),
            "analysis_input": doc.get("analysis_input"),
        }
        for page in doc.get("pages", []):
            if not isinstance(page, Mapping):
                continue
            visual_fields = page.get("visual_fields", {})
            mentions = visual_fields.get("candidate_mentions", []) if isinstance(visual_fields, Mapping) else []
            if isinstance(mentions, Mapping):
                mentions = mentions.get("value", [])
            page_provenance = {
                **provenance,
                "analysis_input": page.get("analysis_input", provenance["analysis_input"]),
            }
            for chunk_index, (start, end, text) in enumerate(_text_chunks(page.get("text", ""))):
                if not text.strip() and not mentions:
                    continue
                units.append({
                    **page_provenance,
                    "kind": "page",
                    "page": page.get("page"),
                    "location": page.get("location"),
                    "chunk_index": chunk_index,
                    "chunk_char_start": start,
                    "chunk_char_end": end,
                    "text": text,
                    "candidate_mentions": mentions if chunk_index == 0 else [],
                    "provenance": {
                        **page_provenance,
                        "page": page.get("page"),
                        "location": page.get("location"),
                    },
                })
        for table in doc.get("tables", []) if isinstance(doc.get("tables", []), list) else []:
            if not isinstance(table, Mapping):
                continue
            location = table.get("source_location") or f"table:{table.get('index', 'unknown')}"
            table_text = "\n".join(
                "\t".join(str(cell) for cell in row)
                for row in table.get("rows", []) if isinstance(row, (list, tuple))
            )
            for chunk_index, (start, end, text) in enumerate(_text_chunks(table_text)):
                if not text.strip():
                    continue
                units.append({
                    **provenance,
                    "kind": "table",
                    "table_index": table.get("index"),
                    "location": location,
                    "chunk_index": chunk_index,
                    "chunk_char_start": start,
                    "chunk_char_end": end,
                    "text": text,
                    "candidate_mentions": [],
                    "provenance": {**provenance, "location": location},
                })
    return units


def _split_inventory_unit(unit: dict[str, Any], maximum_encoded_characters: int) -> list[dict[str, Any]]:
    encoded = json.dumps({"evidence_units": [unit]}, ensure_ascii=False, separators=(",", ":"))
    if len(encoded) <= maximum_encoded_characters:
        return [unit]
    text = str(unit.get("text", ""))
    if not text:
        raise DeepSeekError("inventory_batch_too_large", "候选清单单元元数据超过后端字符预算")
    metadata_characters = len(encoded) - len(text)
    text_limit = maximum_encoded_characters - metadata_characters - 16
    if text_limit <= 0:
        raise DeepSeekError("inventory_batch_too_large", "候选清单单元元数据超过后端字符预算")
    start_offset = int(unit.get("chunk_char_start") or 0)
    output = []
    for part_index, start in enumerate(range(0, len(text), text_limit)):
        part = text[start:start + text_limit]
        split = dict(unit)
        split["chunk_index"] = f"{unit.get('chunk_index', 0)}.{part_index}"
        split["chunk_char_start"] = start_offset + start
        split["chunk_char_end"] = start_offset + start + len(part)
        split["text"] = part
        split["candidate_mentions"] = unit.get("candidate_mentions", []) if part_index == 0 else []
        output.append(split)
    return output


def _inventory_batches(prompt_docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    # Reserve room for batch metadata; each unit itself is capped at 5,000 text
    # characters so this branch can always make progress.
    content_limit = _INVENTORY_BATCH_CHAR_LIMIT - 600
    for raw_unit in _inventory_units(prompt_docs):
        for unit in _split_inventory_unit(raw_unit, content_limit):
            candidate = current + [unit]
            encoded = json.dumps({"evidence_units": candidate}, ensure_ascii=False, separators=(",", ":"))
            if current and len(encoded) > content_limit:
                groups.append(current)
                current = [unit]
            else:
                current = candidate
    if current:
        groups.append(current)

    batches: list[dict[str, Any]] = []
    for index, units in enumerate(groups, start=1):
        payload = {
            "inventory_batch": {
                "index": index,
                "total": len(groups),
                "character_budget": _INVENTORY_BATCH_CHAR_LIMIT,
                "payload_characters": 0,
                "unit_count": len(units),
            },
            "evidence_units": units,
        }
        # The size field includes itself. Two passes are enough for its digit
        # count to stabilize, and the reserved metadata space keeps it bounded.
        for _ in range(2):
            payload["inventory_batch"]["payload_characters"] = len(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            )
        if payload["inventory_batch"]["payload_characters"] > _INVENTORY_BATCH_CHAR_LIMIT:
            raise DeepSeekError("inventory_batch_too_large", "候选清单批次超过后端字符预算")
        batches.append(payload)
    return batches


def _candidate_identity(candidate: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(_search_text(candidate.get(key)).replace(" ", "") for key in _CANDIDATE_KEYS)


def _has_concrete_model(value: Any) -> bool:
    normalized = _search_text(value).replace(" ", "")
    return bool(normalized) and normalized not in {
        "none", "null", "unknown", "na", "notprovided",
        "未知", "无", "未提供", "不详",
    }


def _normalized_model(value: Any) -> str:
    return re.sub(r"[^\w]+", "", unicodedata.normalize("NFKC", str(value or "")).casefold())


def _configuration_with_catalog_number(configuration: Any, catalog_number: Any) -> str | None:
    configuration_text = str(configuration or "").strip()
    catalog_text = str(catalog_number or "").strip()
    if not catalog_text:
        return configuration_text or None
    if _normalized_model(catalog_text) in _normalized_model(configuration_text):
        return configuration_text
    catalog_fragment = f"订货号 {catalog_text}"
    return f"{configuration_text}；{catalog_fragment}" if configuration_text else catalog_fragment


def _probable_catalog_number(candidate: Mapping[str, Any],
                             evidence_by_location: Mapping[tuple[str, str], str]) -> str | None:
    """Return a model value only when evidence proves it is an ordering code.

    The fallback is deliberately narrow: an explicit catalog_number from the
    model is accepted, otherwise the page needs an ordering-number header and
    the alleged model must be a longer, code-like extension of a concrete
    series value. This avoids category- or manufacturer-specific blacklists.
    """
    explicit = str(candidate.get("catalog_number") or "").strip()
    if explicit and _has_concrete_model(explicit):
        return explicit
    model = str(candidate.get("model") or "").strip()
    series = str(candidate.get("series") or "").strip()
    model_key = _normalized_model(model)
    series_key = _normalized_model(series)
    if not model_key or not series_key or model_key == series_key or not model_key.startswith(series_key):
        return None
    digit_count = sum(character.isdigit() for character in model)
    separated_numeric_groups = len(re.findall(r"\d+", model)) >= 3
    code_like = separated_numeric_groups or digit_count >= 7
    if not code_like:
        return None
    for source in candidate.get("sources", []) if isinstance(candidate.get("sources"), list) else []:
        if not isinstance(source, Mapping):
            continue
        key = (str(source.get("document_id") or ""), str(source.get("page_or_location") or ""))
        evidence = evidence_by_location.get(key, "")
        if model_key in _normalized_model(evidence) and _CATALOG_HEADER_RE.search(evidence):
            return model
    return None


def _normalise_inventory_candidate(candidate: Mapping[str, Any],
                                   evidence_by_location: Mapping[tuple[str, str], str]) -> dict[str, Any]:
    normalized = dict(candidate)
    catalog_number = _probable_catalog_number(normalized, evidence_by_location)
    if not catalog_number:
        normalized["_catalog_derived"] = False
        return normalized
    if _normalized_model(normalized.get("model")) == _normalized_model(catalog_number):
        series = normalized.get("series")
        if not _has_concrete_model(series):
            normalized["_catalog_derived"] = False
            return normalized
        normalized["model"] = series
    normalized["configuration"] = _configuration_with_catalog_number(
        normalized.get("configuration"), catalog_number,
    )
    normalized["_catalog_derived"] = True
    return normalized


def _merge_configuration(left: Any, right: Any) -> str | None:
    values = []
    for raw in (left, right):
        value = str(raw or "").strip()
        if value and value not in values:
            values.append(value)
    return "；".join(values) if values else None


def _accessory_compatibility_only_sources(sources: list[Mapping[str, Any]]) -> bool:
    if not sources:
        return False
    return all(
        _ACCESSORY_CONTEXT_RE.search(str(source.get("excerpt") or ""))
        and _COMPATIBILITY_CONTEXT_RE.search(str(source.get("excerpt") or ""))
        for source in sources
    )


def _candidate_has_only_non_instrument_evidence(
    candidate: Mapping[str, Any],
    sources: list[Mapping[str, Any]],
    evidence_by_location: Mapping[tuple[str, str], str],
) -> bool:
    """Reject role-confused seeds using their own evidence, not product names."""
    role = _search_text(candidate.get("product_role")).replace(" ", "_")
    if role and role not in {"instrument", "instrument_configuration"}:
        return bool(_NON_INSTRUMENT_ROLE_RE.search(role))
    if not sources:
        return False
    identifier = str(candidate.get("model") or candidate.get("series") or "").strip()
    identifier_key = _normalized_model(identifier)

    def source_is_non_instrument(source: Mapping[str, Any]) -> bool:
        excerpt = str(source.get("excerpt") or "")
        evidence = evidence_by_location.get((
            str(source.get("document_id") or ""),
            str(source.get("page_or_location") or ""),
        ), "")
        predecessor = re.escape(identifier) + r'\s*(?:的)?(?:升级款|升级版|后继型号|后继产品|换代产品)'
        if identifier and re.search(predecessor, excerpt, re.I):
            return True
        if re.search(r"(?:software|软件)", excerpt, re.IGNORECASE):
            return True
        if _PACKAGING_CONTEXT_RE.search(excerpt) and re.search(
            r"(?:chip|disc|patch|plate|cartridge|tip|芯片|圆盘|贴片|板|卡匣|吸头)",
            excerpt, re.IGNORECASE,
        ):
            return True
        if re.search(r"\bname\s+" + re.escape(identifier) + r"\b", excerpt, re.IGNORECASE):
            if _SETTING_UI_CONTEXT_RE.search(evidence):
                return True
        if len(sources) == 1 and identifier_key and identifier_key in _normalized_model(evidence):
            if _CROSS_PROMOTION_CONTEXT_RE.search(evidence):
                return True
        return False

    return all(source_is_non_instrument(source) for source in sources)


def _repairable_field_names(raw_fields: Any, evidence_payload: Mapping[str, Any]) -> list[str]:
    if not isinstance(raw_fields, list):
        return []
    fields_by_name = {
        str(field.get("field_name")): field
        for field in raw_fields if isinstance(field, Mapping)
    }
    if not set(CANONICAL_FIELD_NAMES).issubset(fields_by_name):
        return []
    supported_field_count = sum(
        isinstance(field.get("sources"), list) and bool(field.get("sources"))
        for field in fields_by_name.values()
    )
    if supported_field_count == 0:
        return []
    selection = evidence_payload.get("evidence_selection", {})
    coverage = selection.get("field_coverage", {}) if isinstance(selection, Mapping) else {}
    repairable = []
    for field_name in CANONICAL_FIELD_NAMES:
        field = fields_by_name[field_name]
        field_coverage = coverage.get(field_name, {}) if isinstance(coverage, Mapping) else {}
        if not isinstance(field_coverage, Mapping) or not field_coverage.get("covered"):
            continue
        value = str(field.get("value", "")).strip().casefold()
        has_sources = isinstance(field.get("sources"), list) and bool(field.get("sources"))
        if field.get("_unsupported_parts") or not has_sources or value in {"", "unknown", "none", "null", "未知", "未提供"}:
            repairable.append(field_name)
    return repairable


def _merge_repaired_fields(raw_fields: list[Any], repaired_fields: Any,
                           requested_fields: list[str],
                           evidence_by_location: Mapping[tuple[str, str], str]) -> list[Any]:
    replacements: dict[str, dict[str, Any]] = {}
    allowed = set(requested_fields)
    for raw in repaired_fields if isinstance(repaired_fields, list) else []:
        if not isinstance(raw, Mapping):
            continue
        field_name = str(raw.get("field_name", "")).strip()
        if field_name not in allowed:
            continue
        normalized = _validated_field(raw, evidence_by_location)
        if normalized["sources"]:
            normalized["status"] = "pending_review"
            replacements[field_name] = normalized
    if not replacements:
        return raw_fields
    merged = []
    for field in raw_fields:
        field_name = str(field.get("field_name", "")) if isinstance(field, Mapping) else ""
        replacement = replacements.get(field_name)
        if replacement and isinstance(field.get("value"), Mapping) and isinstance(replacement.get("value"), Mapping):
            # A repair cannot silently discard independently supported earlier values.
            combined = {**field["value"], **replacement["value"]}
            combined_sources = []
            for source in [*field.get("sources", []), *replacement.get("sources", [])]:
                if source not in combined_sources:
                    combined_sources.append(source)
            replacement = _validated_field({**replacement, "value": combined, "sources": combined_sources}, evidence_by_location)
            unresolved_values = {**field.get("_unsupported_values", {}),
                                 **replacements[field_name].get("_unsupported_values", {}),
                                 **replacement.get("_unsupported_values", {})}
            retained_values = replacement["value"] if isinstance(replacement["value"], Mapping) else {}
            # Keep signs, decimals and comparison symbols: -5 and +5 are not
            # the same repaired value, even when the labels have changed.
            def exact_value_key(value):
                return ''.join(unicodedata.normalize('NFKC', _value_identity(value)).casefold().split())
            resolved = {name for name, value in unresolved_values.items()
                        if any(exact_value_key(value) == exact_value_key(v)
                               and (not _temperature_metric(name) or _temperature_metric(name) == _temperature_metric(k))
                               for k, v in retained_values.items())}
            replacement["_unsupported_parts"] = sorted(set(
                field.get("_unsupported_parts", []) + replacements[field_name].get("_unsupported_parts", [])
                + replacement.get("_unsupported_parts", [])
            ) - set(retained_values) - resolved)
            replacement["_unsupported_values"] = {name: value for name, value in unresolved_values.items()
                                                  if name in replacement["_unsupported_parts"]}
        merged.append(replacement or field)
    return merged


def _compatible_candidate_identity(left: tuple[str, ...], right: tuple[str, ...]) -> bool:
    left_brand, left_series, left_model, left_configuration = left
    right_brand, right_series, right_model, right_configuration = right
    if not left_model or _normalized_model(left_model) != _normalized_model(right_model):
        return False
    for first, second in (
        (left_brand, right_brand),
        (left_series, right_series),
        (left_configuration, right_configuration),
    ):
        if first and second and first != second:
            return False
    return True


def _merge_inventory_candidates(results: list[Mapping[str, Any]],
                                 evidence_by_location: Mapping[tuple[str, str], str]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    identities: list[tuple[str, ...]] = []
    source_keys: list[set[tuple[str, str, str]]] = []
    catalog_derived: list[bool] = []
    for result in results:
        raw_candidates = result.get("candidates", [])
        if not isinstance(raw_candidates, list):
            raise DeepSeekError("invalid_candidate_inventory", "候选清单格式无效")
        for raw_item in raw_candidates:
            raw = _normalise_inventory_candidate(raw_item, evidence_by_location) if isinstance(raw_item, Mapping) else raw_item
            if not isinstance(raw, Mapping) or not _has_concrete_model(raw.get("model")):
                continue
            product_role = _search_text(raw.get("product_role")).replace(" ", "_")
            if product_role in {
                "accessory", "consumable", "reagent", "software", "service",
                "module", "adapter", "rotor", "plate",
            }:
                continue
            validated_raw_sources = _validated_sources(raw.get("sources", []), evidence_by_location)
            if _accessory_compatibility_only_sources(validated_raw_sources):
                continue
            if _candidate_has_only_non_instrument_evidence(
                raw, validated_raw_sources, evidence_by_location,
            ):
                continue
            # A specification row ("| 电源 | AC 220±22V 50/60Hz |") is a value,
            # not a product. The model may still quote one as a model, so the
            # same rule the local extractor uses is applied to its evidence.
            if measurement_value_not_model(
                str(raw.get("model") or ""),
                " ".join(str(source.get("excerpt") or "") for source in validated_raw_sources),
            ):
                continue
            identity = _candidate_identity(raw)
            exact_matches = [index for index, existing in enumerate(identities) if existing == identity]
            same_model_matches = [
                index for index, existing in enumerate(identities)
                if _normalized_model(existing[2]) == _normalized_model(identity[2])
                and (not existing[0] or not identity[0] or existing[0] == identity[0])
            ]
            compatible_matches = [
                index for index, existing in enumerate(identities)
                if _compatible_candidate_identity(existing, identity)
            ]
            same_instrument_matches = [
                index for index, existing in enumerate(identities)
                if (raw.get("_catalog_derived") or catalog_derived[index])
                and _normalized_model(existing[2]) == _normalized_model(identity[2])
                and (not existing[0] or not identity[0] or existing[0] == identity[0])
                and (not existing[1] or not identity[1] or existing[1] == identity[1])
            ]
            if exact_matches:
                candidate_index = exact_matches[0]
            elif len(same_model_matches) == 1:
                candidate_index = same_model_matches[0]
            elif len(compatible_matches) == 1:
                candidate_index = compatible_matches[0]
            elif len(same_instrument_matches) == 1:
                candidate_index = same_instrument_matches[0]
            else:
                candidate_index = len(merged)
                merged.append({
                    "brand": raw.get("brand"),
                    "series": raw.get("series"),
                    "model": raw.get("model"),
                    "configuration": raw.get("configuration"),
                    "sources": [],
                })
                identities.append(identity)
                source_keys.append(set())
                catalog_derived.append(bool(raw.get("_catalog_derived")))
            candidate = merged[candidate_index]
            for key in ("brand", "series", "model"):
                if candidate.get(key) in (None, "") and raw.get(key) not in (None, ""):
                    candidate[key] = raw.get(key)
            candidate["configuration"] = _merge_configuration(
                candidate.get("configuration"), raw.get("configuration"),
            )
            catalog_derived[candidate_index] = catalog_derived[candidate_index] or bool(raw.get("_catalog_derived"))
            identities[candidate_index] = _candidate_identity(candidate)
            for normalized in validated_raw_sources:
                source_key = (
                    str(normalized.get("document_id") or ""),
                    str(normalized.get("page_or_location") or ""),
                    _search_text(normalized.get("excerpt")),
                )
                if source_key in source_keys[candidate_index]:
                    continue
                source_keys[candidate_index].add(source_key)
                if len(candidate["sources"]) < _INVENTORY_SOURCE_LIMIT:
                    candidate["sources"].append(normalized)
    merged = _merge_short_series_aliases(merged)
    if len(merged) > 50:
        raise DeepSeekError("candidate_inventory_too_large", f"候选共 {len(merged)} 个，超过单任务 50 个上限，请按目录拆分资料")
    return merged


def _merge_short_series_aliases(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse an unambiguous short series label into its full candidate name.

    Catalogs sometimes use a short all-caps label in a specification-table
    heading after spelling out the full series name elsewhere. The rule is
    deliberately generic and conservative: the short label must be 3-4
    uppercase alphanumerics, be quoted in a series context, and have exactly
    one longer prefix match in the same document.
    """
    aliases: dict[int, int] = {}
    for index, candidate in enumerate(candidates):
        short = str(candidate.get("model") or "").strip()
        sources = candidate.get("sources", [])
        if not re.fullmatch(r"[A-Z0-9]{3,4}", short) or not isinstance(sources, list):
            continue
        series_sources = [
            source for source in sources
            if isinstance(source, Mapping)
            and re.search(r"(?:series|系列)", str(source.get("excerpt") or ""), re.IGNORECASE)
        ]
        if not series_sources:
            continue
        document_ids = {
            str(source.get("document_id") or "") for source in series_sources
            if str(source.get("document_id") or "")
        }
        short_key = _normalized_model(short)
        matches = []
        for other_index, other in enumerate(candidates):
            if other_index == index:
                continue
            other_key = _normalized_model(other.get("model"))
            if not other_key.startswith(short_key) or len(other_key) <= len(short_key):
                continue
            other_documents = {
                str(source.get("document_id") or "")
                for source in other.get("sources", [])
                if isinstance(source, Mapping) and str(source.get("document_id") or "")
            }
            if document_ids & other_documents:
                matches.append(other_index)
        if len(matches) == 1:
            aliases[index] = matches[0]

    removed: set[int] = set()
    for alias_index, target_index in aliases.items():
        if alias_index in removed or target_index in removed:
            continue
        alias = candidates[alias_index]
        target = candidates[target_index]
        for key in ("brand", "series"):
            if target.get(key) in (None, "") and alias.get(key) not in (None, ""):
                target[key] = alias.get(key)
        target["configuration"] = _merge_configuration(
            target.get("configuration"), alias.get("configuration"),
        )
        existing_sources = {
            (
                str(source.get("document_id") or ""),
                str(source.get("page_or_location") or ""),
                _search_text(source.get("excerpt")),
            )
            for source in target.get("sources", []) if isinstance(source, Mapping)
        }
        for source in alias.get("sources", []):
            if not isinstance(source, Mapping):
                continue
            source_key = (
                str(source.get("document_id") or ""),
                str(source.get("page_or_location") or ""),
                _search_text(source.get("excerpt")),
            )
            if source_key not in existing_sources and len(target["sources"]) < _INVENTORY_SOURCE_LIMIT:
                target["sources"].append(source)
                existing_sources.add(source_key)
        removed.add(alias_index)
    return [candidate for index, candidate in enumerate(candidates) if index not in removed]


def _structured_mention_inventory(prompt_docs: list[Mapping[str, Any]]) -> dict[str, Any]:
    """Promote page-level vision mentions into auditable inventory seeds.

    Vision extraction already paid the cost of reading the rendered page. A
    later batch-level inventory call must not be allowed to silently forget a
    concrete identifier that is quoted verbatim on that page.
    """
    candidates = []
    for doc in prompt_docs:
        document_id = str(doc.get("document_id") or "")
        for page in doc.get("pages", []) if isinstance(doc.get("pages"), list) else []:
            if not isinstance(page, Mapping):
                continue
            fields = page.get("visual_fields")
            if not isinstance(fields, Mapping):
                continue
            raw_mentions = fields.get("candidate_mentions", [])
            if isinstance(raw_mentions, Mapping):
                raw_mentions = raw_mentions.get("value", [])
            if not isinstance(raw_mentions, list):
                continue
            for mention in raw_mentions:
                if not isinstance(mention, Mapping):
                    continue
                quote = str(mention.get("evidence_quote") or "").strip()
                model = mention.get("model")
                series = mention.get("series")
                identifier = model if _has_concrete_model(model) else series
                if not _has_concrete_model(identifier):
                    continue
                if _normalized_model(identifier) not in _normalized_model(quote):
                    continue
                if measurement_value_not_model(str(identifier), quote):
                    continue
                brand = mention.get("brand")
                if _normalized_model(brand) in {
                    _normalized_model(identifier), _normalized_model(series),
                }:
                    brand = None
                candidates.append({
                    "product_role": mention.get("product_role") or "instrument",
                    "brand": brand,
                    "series": series,
                    "model": identifier,
                    "configuration": mention.get("configuration"),
                    "sources": [{
                        "document_id": document_id,
                        "page_or_location": str(page.get("location") or ""),
                        "excerpt": quote,
                    }],
                })
    for candidate in candidates:
        short = str(candidate.get("model") or "").strip()
        quote = str(candidate.get("sources", [{}])[0].get("excerpt") or "")
        if not re.fullmatch(r"[A-Z0-9]{3,4}", short) or not re.search(r"(?:series|系列)", quote, re.IGNORECASE):
            continue
        document_id = str(candidate.get("sources", [{}])[0].get("document_id") or "")
        matches = [
            other for other in candidates
            if other is not candidate
            and str(other.get("sources", [{}])[0].get("document_id") or "") == document_id
            and _normalized_model(other.get("model")).startswith(_normalized_model(short))
            and len(_normalized_model(other.get("model"))) > len(_normalized_model(short))
        ]
        if len(matches) == 1:
            candidate["model"] = matches[0].get("model")
            candidate["series"] = matches[0].get("series")
    return {"candidates": candidates}


def _bounded_candidate_page_text(text: Any,
                                 terms: list[tuple[str, str, str, int]],
                                 maximum_characters: int = _CANDIDATE_EVIDENCE_PAGE_CHAR_LIMIT,
                                 ) -> tuple[str, int, int, int]:
    value = str(text or "")
    maximum_characters = max(0, min(maximum_characters, _CANDIDATE_EVIDENCE_PAGE_CHAR_LIMIT))
    if len(value) <= maximum_characters:
        return value, 0, len(value), len(value)
    if maximum_characters == 0:
        return "", 0, 0, len(value)
    chunks = _text_chunks(value)
    ranked = sorted(
        chunks,
        key=lambda chunk: (
            -(
                sum(score for _, score in _matched_candidate_terms(chunk[2], terms))
                + sum(
                    int(match["score"])
                    for match in _field_scores_for_text(chunk[2]).values()
                )
            ),
            chunk[0],
        ),
    )
    best_start, best_end, _ = ranked[0]
    center = (best_start + best_end) // 2
    start = max(0, center - maximum_characters // 2)
    end = min(len(value), start + maximum_characters)
    start = max(0, end - maximum_characters)
    return value[start:end], start, end, len(value)


def _bounded_table_row(row: list[Any] | tuple[Any, ...], maximum_characters: int) -> list[str]:
    cells: list[str] = []
    used = 0
    for raw_cell in row:
        separator = 1 if cells else 0
        available = maximum_characters - used - separator
        if available <= 0:
            break
        cell = str(raw_cell)
        cells.append(cell[:available])
        used += separator + min(len(cell), available)
        if len(cell) > available:
            break
    return cells


def _bounded_candidate_table(table: Mapping[str, Any],
                             terms: list[tuple[str, str, str, int]],
                             maximum_characters: int) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    rows = [list(row) for row in table.get("rows", []) if isinstance(row, (list, tuple))]
    original_text = _table_rows_text({"rows": rows})
    original_characters = len(original_text)
    original_row_count = len(rows)
    maximum_characters = max(0, min(maximum_characters, _CANDIDATE_EVIDENCE_TABLE_CHAR_LIMIT))
    if not rows or maximum_characters == 0:
        return None, {
            "original_text_characters": original_characters,
            "selected_text_characters": 0,
            "original_row_count": original_row_count,
            "selected_row_count": 0,
            "truncated": bool(original_characters),
        }

    row_scores = {}
    for index, row in enumerate(rows):
        row_text = _table_rows_text({"rows": [row]})
        row_scores[index] = (
            sum(score for _, score in _matched_candidate_terms(row_text, terms))
            + sum(int(match["score"]) for match in _field_scores_for_text(row_text).values())
        )
    ranked_matches = sorted(
        (index for index, score in row_scores.items() if score > 0),
        key=lambda index: (-row_scores[index], index),
    )
    ranked_indices: list[int] = []
    for index in ([0] if rows else []) + ranked_matches:
        if index not in ranked_indices:
            ranked_indices.append(index)
        for neighbor in (index - 1, index + 1):
            if 0 <= neighbor < len(rows) and neighbor not in ranked_indices:
                ranked_indices.append(neighbor)
    ranked_indices.extend(index for index in range(len(rows)) if index not in ranked_indices)

    selected: dict[int, list[str]] = {}
    used = 0
    for index in ranked_indices:
        if len(selected) >= _CANDIDATE_EVIDENCE_TABLE_ROW_LIMIT or used >= maximum_characters:
            break
        separator = 1 if selected else 0
        bounded_row = _bounded_table_row(rows[index], maximum_characters - used - separator)
        if not bounded_row:
            continue
        selected[index] = bounded_row
        used += separator + len("\t".join(bounded_row))

    selected_indices = sorted(selected)
    selected_rows = [selected[index] for index in selected_indices]
    selected_characters = len(_table_rows_text({"rows": selected_rows}))
    payload = {
        **table,
        "source_location": table.get("source_location") or f"table:{table.get('index', 'unknown')}",
        "rows": selected_rows,
        "selected_row_indices": selected_indices,
        "original_row_count": original_row_count,
        "selected_row_count": len(selected_rows),
        "original_text_characters": original_characters,
        "selected_text_characters": selected_characters,
        "text_truncated": (
            len(selected_rows) < original_row_count or selected_characters < original_characters
        ),
    }
    return payload, {
        "original_text_characters": original_characters,
        "selected_text_characters": selected_characters,
        "original_row_count": original_row_count,
        "selected_row_count": len(selected_rows),
        "truncated": payload["text_truncated"],
    }


def _candidate_evidence_payload(candidate: Mapping[str, Any],
                                prompt_docs: list[dict[str, Any]]) -> dict[str, Any]:
    """Select a bounded, auditable page subset for one candidate detail call."""
    terms = _candidate_terms(candidate)
    all_keys: list[tuple[int, int]] = []
    page_by_key: dict[tuple[int, int], Mapping[str, Any]] = {}
    page_keys_by_doc: dict[int, list[tuple[int, int]]] = {}
    page_lookup: dict[tuple[int, int], list[tuple[int, int]]] = defaultdict(list)
    reasons: dict[tuple[int, int], set[str]] = defaultdict(set)
    scores: dict[tuple[int, int], int] = defaultdict(int)
    page_retrieval_profiles: dict[tuple[int, int], dict[str, Any]] = {}
    table_entries: list[dict[str, Any]] = []

    for doc_index, doc in enumerate(prompt_docs):
        doc_keys: list[tuple[int, int]] = []
        tables_by_page: defaultdict[int, list[dict[str, Any]]] = defaultdict(list)
        raw_tables = doc.get("tables", []) if isinstance(doc.get("tables", []), list) else []
        for table_position, table in enumerate(raw_tables):
            if not isinstance(table, Mapping):
                continue
            table_page = _page_number(table.get("source_location"))
            table_text = _table_rows_text(table)
            entry = {
                "key": (doc_index, table_position),
                "doc_index": doc_index,
                "table_position": table_position,
                "table": table,
                "page": table_page,
                "location": table.get("source_location") or f"table:{table.get('index', 'unknown')}",
                "profile": _field_retrieval_profile(table_text),
                "candidate_score": sum(score for _, score in _matched_candidate_terms(table_text, terms)),
            }
            table_entries.append(entry)
            if table_page is not None:
                tables_by_page[table_page].append(entry)
        for page_index, page in enumerate(doc.get("pages", [])):
            if not isinstance(page, Mapping):
                continue
            key = (doc_index, page_index)
            all_keys.append(key)
            doc_keys.append(key)
            page_by_key[key] = page
            number = int(page.get("page") or page_index + 1)
            page_lookup[(doc_index, number)].append(key)
            searchable = str(page.get("text", ""))
            retrieval_text = searchable
            if page.get("visual_fields"):
                searchable += "\n" + json.dumps(page.get("visual_fields"), ensure_ascii=False, default=str)
                retrieval_text += "\n" + "\n".join(_scalar_texts(page.get("visual_fields")))
            page_profile = _field_retrieval_profile(retrieval_text)
            combined_field_scores = dict(page_profile["field_scores"])
            combined_matched_terms: defaultdict[str, set[str]] = defaultdict(set)
            for field_name, matched in page_profile["matched_terms"].items():
                combined_matched_terms[field_name].update(matched)
            for table_entry in tables_by_page.get(number, []):
                table_profile = table_entry["profile"]
                searchable += "\n" + _table_rows_text(table_entry["table"])
                for field_name, score in table_profile["field_scores"].items():
                    combined_field_scores[field_name] = max(
                        combined_field_scores.get(field_name, 0), int(score),
                    )
                for field_name, matched in table_profile["matched_terms"].items():
                    combined_matched_terms[field_name].update(matched)
            page_retrieval_profiles[key] = {
                **page_profile,
                "page_score": page_profile["score"],
                "page_field_scores": page_profile["field_scores"],
                "page_matched_terms": page_profile["matched_terms"],
                "score": sum(combined_field_scores.values()),
                "field_scores": combined_field_scores,
                "matched_terms": {
                    field_name: sorted(matched)
                    for field_name, matched in combined_matched_terms.items()
                },
            }
            for field_name in combined_field_scores:
                reasons[key].add(f"canonical_field_match:{field_name}")
            for field, score in _matched_candidate_terms(searchable, terms):
                reasons[key].add(f"candidate_{field}_match")
                scores[key] += score
        page_keys_by_doc[doc_index] = doc_keys

    unresolved_sources: list[dict[str, Any]] = []
    source_keys: set[tuple[int, int]] = set()
    raw_sources = candidate.get("sources", [])
    for source in raw_sources if isinstance(raw_sources, list) else []:
        if not isinstance(source, Mapping):
            continue
        source_document_id = source.get("document_id")
        source_location = source.get("page_or_location", source.get("location", source.get("page")))
        source_page = _page_number(source_location)
        matching_docs = [
            index for index, doc in enumerate(prompt_docs)
            if source_document_id is not None and doc.get("document_id") == source_document_id
        ]
        if source_document_id is None and len(prompt_docs) == 1:
            matching_docs = [0]
        matching_keys: list[tuple[int, int]] = []
        if source_page is not None:
            for doc_index in matching_docs:
                matching_keys.extend(page_lookup.get((doc_index, source_page), []))
        if not matching_keys:
            unresolved_sources.append({
                "document_id": source_document_id,
                "page_or_location": source_location,
            })
            continue
        for key in matching_keys:
            source_keys.add(key)
            reasons[key].add("inventory_source")

    selected: set[tuple[int, int]] = set()

    def add_key(key: tuple[int, int], reason: str | None = None) -> None:
        if len(selected) >= _CANDIDATE_EVIDENCE_PAGE_LIMIT:
            return
        selected.add(key)
        if reason:
            reasons[key].add(reason)

    def add_neighbors(key: tuple[int, int]) -> None:
        doc_index, _ = key
        doc_keys = page_keys_by_doc.get(doc_index, [])
        try:
            position = doc_keys.index(key)
        except ValueError:
            return
        for neighbor_position in (position - 1, position + 1):
            if 0 <= neighbor_position < len(doc_keys):
                add_key(doc_keys[neighbor_position], "adjacent_context")

    ranked_sources = sorted(
        source_keys,
        key=lambda key: (-scores[key], key[0], key[1]),
    )[:_CANDIDATE_IDENTITY_SOURCE_PAGE_LIMIT]
    for key in ranked_sources:
        add_key(key)
    for key in ranked_sources:
        add_neighbors(key)

    field_page_rankings: dict[str, list[tuple[int, int]]] = {}
    field_top_k_pool: set[tuple[int, int]] = set()
    for field_name in CANONICAL_FIELD_NAMES:
        ranked = sorted(
            (
                key for key, profile in page_retrieval_profiles.items()
                if profile["field_scores"].get(field_name, 0) > 0
            ),
            key=lambda key: (
                -page_retrieval_profiles[key]["field_scores"][field_name],
                -page_retrieval_profiles[key]["score"],
                key[0], key[1],
            ),
        )[:_FIELD_RETRIEVAL_TOP_K]
        field_page_rankings[field_name] = ranked
        field_top_k_pool.update(ranked)

    covered_fields = {
        field_name
        for key in selected
        for field_name in page_retrieval_profiles.get(key, {}).get("field_scores", {})
    }
    field_selected_keys: list[tuple[int, int]] = []
    while field_top_k_pool and len(selected) < _CANDIDATE_EVIDENCE_PAGE_LIMIT:
        remaining = [key for key in field_top_k_pool if key not in selected]
        if not remaining:
            break
        best = max(
            remaining,
            key=lambda key: (
                len(set(page_retrieval_profiles[key]["field_scores"]) - covered_fields),
                sum(
                    score for field_name, score in page_retrieval_profiles[key]["field_scores"].items()
                    if field_name not in covered_fields
                ),
                page_retrieval_profiles[key]["score"],
                scores[key],
                -key[0], -key[1],
            ),
        )
        new_fields = set(page_retrieval_profiles[best]["field_scores"]) - covered_fields
        if not new_fields:
            break
        add_key(best, "canonical_field_top_k")
        field_selected_keys.append(best)
        for field_name in page_retrieval_profiles[best]["field_scores"]:
            if best in field_page_rankings[field_name]:
                reasons[best].add(f"field_top_k:{field_name}")
        covered_fields.update(page_retrieval_profiles[best]["field_scores"])
        field_top_k_pool.discard(best)

    # Coverage comes first, then use any remaining page slots for the strongest
    # second-source evidence from the per-field Top-K pool.  This keeps the
    # bounded payload useful for corroboration without letting one common field
    # crowd out fields that are documented only once.
    remaining_field_pages = sorted(
        (key for key in field_top_k_pool if key not in selected),
        key=lambda key: (
            -page_retrieval_profiles[key]["score"],
            -scores[key],
            key[0], key[1],
        ),
    )
    for key in remaining_field_pages:
        if len(selected) >= _CANDIDATE_EVIDENCE_PAGE_LIMIT:
            break
        add_key(key, "canonical_field_top_k")
        field_selected_keys.append(key)
        for field_name in page_retrieval_profiles[key]["field_scores"]:
            if key in field_page_rankings[field_name]:
                reasons[key].add(f"field_top_k:{field_name}")

    ranked_matches = sorted(
        (key for key, score in scores.items() if score > 0 and key not in source_keys),
        key=lambda key: (-scores[key], key[0], key[1]),
    )
    for key in ranked_matches:
        add_key(key)
    # PDF neighbours are not automatically relevant merely because a field
    # matched on the preceding page.  Expand only model-identity matches; field
    # pages already come from the bounded, scored Top-K pool above.
    for key in ranked_matches:
        if key in selected:
            add_neighbors(key)

    selected_before_fallback = len(selected)
    fallback_target = min(len(all_keys), _CANDIDATE_EVIDENCE_FALLBACK_PAGES)
    fallback_reason = None
    if len(selected) < fallback_target:
        fallback_reason = "no_candidate_page_match" if not selected else "insufficient_candidate_page_context"
        # Representative first/middle/last pages are deterministic and bounded.
        # The metadata below makes clear that this is incomplete evidence.
        representative: list[tuple[int, int]] = []
        for doc_index in range(len(prompt_docs)):
            doc_keys = page_keys_by_doc.get(doc_index, [])
            if doc_keys:
                representative.append(doc_keys[0])
        if all_keys:
            representative.extend([all_keys[len(all_keys) // 2], all_keys[-1]])
        representative.extend(all_keys)
        for key in representative:
            if len(selected) >= fallback_target:
                break
            add_key(key, "bounded_fallback")

    selected_doc_keys_by_index = {
        doc_index: [key for key in page_keys_by_doc.get(doc_index, []) if key in selected]
        for doc_index in range(len(prompt_docs))
    }
    selected_numbers_by_doc = {
        doc_index: {
            int(page_by_key[key].get("page") or key[1] + 1)
            for key in selected_doc_keys_by_index[doc_index]
        }
        for doc_index in range(len(prompt_docs))
    }
    inventory_source_locations = {
        (str(source.get("document_id") or ""), str(source.get("page_or_location") or ""))
        for source in raw_sources if isinstance(source, Mapping)
    }
    table_candidates = []
    for entry in table_entries:
        doc = prompt_docs[entry["doc_index"]]
        source_key = (str(doc.get("document_id") or ""), str(entry["location"]))
        on_selected_page = entry["page"] in selected_numbers_by_doc[entry["doc_index"]]
        has_retrieval_signal = entry["profile"]["score"] > 0 or entry["candidate_score"] > 0
        if (on_selected_page or entry["page"] is None) and (
            source_key in inventory_source_locations or has_retrieval_signal
        ):
            table_candidates.append(entry)
    table_candidates.sort(key=lambda entry: (
        -int((str(prompt_docs[entry["doc_index"]].get("document_id") or ""), str(entry["location"]))
             in inventory_source_locations),
        -entry["candidate_score"],
        -entry["profile"]["score"],
        entry["doc_index"], entry["table_position"],
    ))

    bounded_tables_by_doc: defaultdict[int, list[dict[str, Any]]] = defaultdict(list)
    selected_table_locations: list[dict[str, Any]] = []
    selected_table_keys: set[tuple[int, int]] = set()
    selected_table_text_char_count = 0
    selected_original_table_text_char_count = 0
    truncated_table_count = 0
    remaining_table_budget = min(
        _CANDIDATE_EVIDENCE_TABLE_TOTAL_CHAR_LIMIT,
        _CANDIDATE_EVIDENCE_TEXT_CHAR_LIMIT,
    )
    for position, entry in enumerate(table_candidates):
        doc_index = entry["doc_index"]
        table = entry["table"]
        tables_left = len(table_candidates) - position
        fair_share = remaining_table_budget // tables_left if tables_left else 0
        table_payload, stats = _bounded_candidate_table(
            table, terms, min(_CANDIDATE_EVIDENCE_TABLE_CHAR_LIMIT, fair_share),
        )
        selected_original_table_text_char_count += stats["original_text_characters"]
        if table_payload is None:
            continue
        bounded_tables_by_doc[doc_index].append(table_payload)
        selected_table_keys.add(entry["key"])
        selected_table_text_char_count += stats["selected_text_characters"]
        remaining_table_budget -= stats["selected_text_characters"]
        if stats["truncated"]:
            truncated_table_count += 1
        doc = prompt_docs[doc_index]
        selected_table_locations.append({
            "document_id": doc.get("document_id"),
            "source_name": doc.get("source_name"),
            "location": table_payload.get("source_location"),
            "retrieval_score": entry["profile"]["score"] + entry["candidate_score"],
            "candidate_match_score": entry["candidate_score"],
            "field_scores": entry["profile"]["field_scores"],
            "matched_terms": entry["profile"]["matched_terms"],
            "retrieval_reasons": sorted(
                (["inventory_source"] if (
                    str(doc.get("document_id") or ""), str(entry["location"])
                ) in inventory_source_locations else [])
                + (["candidate_match"] if entry["candidate_score"] else [])
                + (["canonical_field_match"] if entry["profile"]["score"] else [])
            ),
            "top_scored_chunks": entry["profile"]["chunks"][:3],
            **stats,
        })

    selected_page_payloads_by_doc: defaultdict[int, list[dict[str, Any]]] = defaultdict(list)
    selected_page_text_char_count = 0
    selected_original_page_text_char_count = 0
    truncated_page_count = 0
    remaining_page_budget = _CANDIDATE_EVIDENCE_TEXT_CHAR_LIMIT - selected_table_text_char_count
    ordered_selected_keys = [key for key in all_keys if key in selected]
    for position, key in enumerate(ordered_selected_keys):
        pages_left = len(ordered_selected_keys) - position
        fair_share = remaining_page_budget // pages_left if pages_left else 0
        doc_index, _ = key
        doc = prompt_docs[doc_index]
        page = page_by_key[key]
        bounded_text, text_start, text_end, original_text_length = _bounded_candidate_page_text(
            page.get("text", ""), terms,
            min(_CANDIDATE_EVIDENCE_PAGE_CHAR_LIMIT, fair_share),
        )
        selected_page_text_char_count += len(bounded_text)
        selected_original_page_text_char_count += original_text_length
        remaining_page_budget -= len(bounded_text)
        if original_text_length > len(bounded_text):
            truncated_page_count += 1
        selected_page_payloads_by_doc[doc_index].append({
            **page,
            "text": bounded_text,
            "text_truncated": original_text_length > len(bounded_text),
            "text_char_start": text_start,
            "text_char_end": text_end,
            "original_text_characters": original_text_length,
            "provenance": {
                "document_id": doc.get("document_id"),
                "source_name": doc.get("source_name"),
                "analysis_input": page.get("analysis_input", doc.get("analysis_input")),
                "page": page.get("page"),
                "location": page.get("location"),
            },
        })

    selected_documents: list[dict[str, Any]] = []
    selected_locations: list[dict[str, Any]] = []
    for doc_index, doc in enumerate(prompt_docs):
        selected_doc_keys = selected_doc_keys_by_index[doc_index]
        if not selected_doc_keys:
            continue
        selected_tables = bounded_tables_by_doc[doc_index]
        selected_page_payloads = selected_page_payloads_by_doc[doc_index]
        selected_documents.append({
            "document_id": doc.get("document_id"),
            "source_name": doc.get("source_name"),
            "analysis_input": doc.get("analysis_input"),
            "provenance": {
                "document_id": doc.get("document_id"),
                "source_name": doc.get("source_name"),
                "analysis_input": doc.get("analysis_input"),
            },
            "pages": selected_page_payloads,
            "tables": selected_tables,
        })
        for key in selected_doc_keys:
            page = page_by_key[key]
            retrieval_profile = page_retrieval_profiles.get(key, {
                "score": 0, "field_scores": {}, "matched_terms": {}, "chunks": [],
            })
            selected_locations.append({
                "document_id": doc.get("document_id"),
                "source_name": doc.get("source_name"),
                "analysis_input": page.get("analysis_input", doc.get("analysis_input")),
                "page": page.get("page"),
                "location": page.get("location"),
                "selection_reasons": sorted(reasons[key]),
                "retrieval_score": retrieval_profile["score"] + scores[key],
                "candidate_match_score": scores[key],
                "field_scores": retrieval_profile.get("page_field_scores", {}),
                "matched_terms": retrieval_profile.get("page_matched_terms", {}),
                "selection_field_scores": retrieval_profile["field_scores"],
                "selection_matched_terms": retrieval_profile["matched_terms"],
                "top_scored_chunks": retrieval_profile["chunks"][:3],
            })

    omitted = max(0, len(all_keys) - len(selected))
    selected_text_char_count = selected_page_text_char_count + selected_table_text_char_count
    selected_original_text_char_count = (
        selected_original_page_text_char_count + selected_original_table_text_char_count
    )
    retrieval_units: list[dict[str, Any]] = []
    for key, profile in page_retrieval_profiles.items():
        doc_index, _ = key
        doc = prompt_docs[doc_index]
        page = page_by_key[key]
        retrieval_units.append({
            "kind": "page",
            "key": key,
            "document_id": doc.get("document_id"),
            "location": page.get("location"),
            "score": profile.get("page_score", 0),
            "field_scores": profile.get("page_field_scores", {}),
            "matched_terms": profile.get("page_matched_terms", {}),
            "selected": key in selected,
        })
    for entry in table_entries:
        doc = prompt_docs[entry["doc_index"]]
        retrieval_units.append({
            "kind": "table",
            "key": entry["key"],
            "document_id": doc.get("document_id"),
            "location": entry["location"],
            "score": entry["profile"]["score"],
            "field_scores": entry["profile"]["field_scores"],
            "matched_terms": entry["profile"]["matched_terms"],
            "selected": entry["key"] in selected_table_keys,
        })
    field_coverage = {}
    for field_name in CANONICAL_FIELD_NAMES:
        ranked_units = sorted(
            (
                unit for unit in retrieval_units
                if unit["field_scores"].get(field_name, 0) > 0
            ),
            key=lambda unit: (
                -unit["field_scores"][field_name],
                -unit["score"],
                unit["kind"],
                str(unit["document_id"]),
                str(unit["location"]),
            ),
        )
        selected_units = [unit for unit in ranked_units if unit["selected"]]

        def audit_unit(unit: Mapping[str, Any]) -> dict[str, Any]:
            return {
                "kind": unit["kind"],
                "document_id": unit["document_id"],
                "location": unit["location"],
                "score": unit["field_scores"][field_name],
                "matched_terms": unit["matched_terms"].get(field_name, []),
                "selected": unit["selected"],
            }

        field_coverage[field_name] = {
            "covered": bool(selected_units),
            "selected_evidence_count": len(selected_units),
            "top_k": [audit_unit(unit) for unit in ranked_units[:_FIELD_RETRIEVAL_TOP_K]],
            "selected_evidence": [audit_unit(unit) for unit in selected_units],
        }
    covered_canonical_fields = [
        field_name for field_name in CANONICAL_FIELD_NAMES
        if field_coverage[field_name]["covered"]
    ]
    uncovered_canonical_fields = [
        field_name for field_name in CANONICAL_FIELD_NAMES
        if not field_coverage[field_name]["covered"]
    ]
    return {
        "evidence_selection": {
            "scope": "candidate_bounded_page_subset",
            "page_limit": _CANDIDATE_EVIDENCE_PAGE_LIMIT,
            "page_text_character_limit": _CANDIDATE_EVIDENCE_PAGE_CHAR_LIMIT,
            "text_character_budget": _CANDIDATE_EVIDENCE_TEXT_CHAR_LIMIT,
            "selected_text_character_count": selected_text_char_count,
            "selected_original_text_character_count": selected_original_text_char_count,
            "omitted_selected_page_text_characters": (
                selected_original_page_text_char_count - selected_page_text_char_count
            ),
            "selected_page_text_character_count": selected_page_text_char_count,
            "selected_original_page_text_character_count": selected_original_page_text_char_count,
            "selected_table_text_character_count": selected_table_text_char_count,
            "selected_original_table_text_character_count": selected_original_table_text_char_count,
            "omitted_selected_table_text_characters": (
                selected_original_table_text_char_count - selected_table_text_char_count
            ),
            "omitted_selected_text_characters": (
                selected_original_text_char_count - selected_text_char_count
            ),
            "truncated_selected_page_count": truncated_page_count,
            "candidate_table_count": len(table_candidates),
            "selected_table_count": len(selected_table_locations),
            "truncated_selected_table_count": truncated_table_count,
            "total_page_count": len(all_keys),
            "selected_page_count": len(selected),
            "omitted_page_count": omitted,
            "fallback_used": len(selected) > selected_before_fallback,
            "fallback_reason": fallback_reason,
            "unresolved_inventory_sources": unresolved_sources,
            "retrieval_strategy": "canonical_field_top_k_with_identity_anchors",
            "retrieval_top_k_per_field": _FIELD_RETRIEVAL_TOP_K,
            "covered_canonical_fields": covered_canonical_fields,
            "uncovered_canonical_fields": uncovered_canonical_fields,
            "field_coverage": field_coverage,
            "selected_pages": selected_locations,
            "selected_tables": selected_table_locations,
            "notice": (
                f"Only {len(selected)} of {len(all_keys)} source pages are supplied for this candidate; "
                f"{omitted} pages, "
                f"{selected_original_page_text_char_count - selected_page_text_char_count} page characters, "
                f"and {selected_original_table_text_char_count - selected_table_text_char_count} table "
                "characters are omitted. Missing facts must remain unknown."
            ),
        },
        "documents": selected_documents,
    }


class DeepSeekAnalysisService:
    def __init__(self, client: DeepSeekClient | None = None, checkpoint=None):
        self.client = client
        self.checkpoint = checkpoint

    def analyze(self, *, scenario: str, documents: list[Mapping[str, Any]],
                target_model: str | None = None) -> dict[str, Any]:
        if not scenario.strip():
            raise AnalysisValidationError("scenario is required")
        if not documents:
            raise AnalysisValidationError("至少需要一份已上传资料")
        prompt_docs = []
        for doc in documents:
            visual_pages: dict[int, Mapping[str, Any]] = {}
            extraction = doc.get("model_extraction", {})
            extraction_fields = extraction.get("fields", {}) if isinstance(extraction, Mapping) else {}
            if isinstance(extraction_fields, Mapping):
                for location, value in extraction_fields.items():
                    if not isinstance(value, Mapping):
                        continue
                    match = re.fullmatch(r"page:(\d+)/rendered", str(location))
                    if match and str(value.get("text", "")).strip():
                        visual_pages[int(match.group(1))] = value
            pages = []
            for page in doc.get("pages", []):
                page_number = int(page.get("number") or len(pages) + 1)
                route = page.get("route")
                # Explicit page routing is authoritative. Legacy records lack
                # route, so they retain the old rendered-page preference.
                visual = visual_pages.get(page_number) if route in (None, "vision") else None
                page_input = "rendered_page_vision" if visual else "parser_text"
                pages.append({
                    "page": page_number,
                    "regions": page.get("regions", []),
                    "location": f"page:{page_number}/rendered" if visual else page.get("source_location"),
                    "text": str(visual.get("text", "")) if visual else str(page.get("text", "")),
                    "visual_fields": visual.get("fields", {}) if visual else {},
                    "analysis_input": page_input,
                    "route": route,
                    "source_provenance": page.get("provenance", []),
                })
            page_inputs = {page.get("analysis_input") for page in pages}
            document_input = (
                "hybrid" if len(page_inputs) > 1
                else next(iter(page_inputs), "parser_text")
            )
            prompt_docs.append({"document_id": doc.get("id"), "source_name": doc.get("source_name"),
                                "analysis_input": document_input,
                                "pages": pages,
                                "tables": doc.get("tables", [])})
        if self.client is None:
            try:
                self.client = DeepSeekClient.from_env()
            except DeepSeekError:
                raise
        if any(doc.get("analysis_input") in {"rendered_page_vision", "hybrid"} for doc in prompt_docs):
            raw = self._analyze_visual(scenario, prompt_docs)
        else:
            raw = self._analyze_native_text(scenario, prompt_docs)
        result = self._normalize(raw)
        result['gaps'] = raw.get('gaps', [])
        result['failures'] = raw.get('failures', [])
        result['call_count'] = raw.get('call_count', 0)
        self._scope_to_target_model(result, target_model)
        self._record_unresolved_brands(result, raw.get('brand_unresolved') or [], prompt_docs)
        if result['candidates'] and all(all(f['status'] == 'unknown' for f in c['fields']) for c in result['candidates']):
            result['gaps'].append('已识别型号，但关键参数未可靠提取，请核对解析警告和原始资料')
        if result['gaps']:
            result['status'] = 'partial'
        return result

    @staticmethod
    def _record_unresolved_brands(result: dict[str, Any],
                                 unresolved: list[Mapping[str, Any]],
                                 documents: list[Mapping[str, Any]]) -> None:
        """品牌没从资料里读出来：用户侧一句人话（``gaps``）、排查侧一条技术留痕（``failures``）。

        放在 ``_scope_to_target_model`` **之后**：只对最终留下的候选说这件事，对外
        粒度与结果一致（``docs/209`` §2）。资料里没有厂名是资料限制，不是异常——
        品牌仍然留空、不猜、也不阻断后面的参数抽取与核验；这里只是不让它静默
        （§3.4：静默跳过等于把事实抹掉）。
        """
        names_by_model: defaultdict[str, list[str]] = defaultdict(list)
        for item in unresolved:
            if not isinstance(item, Mapping):
                continue
            model = str(item.get('model') or '')
            names_by_model[model].extend(str(name) for name in item.get('names') or [])
        models = [str(candidate.get('model') or '') for candidate in result.get('candidates') or []
                  if not str(candidate.get('brand') or '').strip()]
        if not models:
            return
        result['gaps'].append(_brand_unresolved_gap(models))
        result.setdefault('failures', []).append(_brand_unresolved_failure(
            models,
            [name for model in models for name in names_by_model.get(model, [])],
            documents,
        ))

    @staticmethod
    def _scope_to_target_model(result: dict[str, Any], target_model: str | None) -> None:
        """Keep the candidates that name the model the user actually asked about.

        A brochure listing several models used to arrive as one merged
        inventory, so the user could not pick the configuration matching the
        model they typed. This filters on the model field only -- it is not a
        judgement about which product is better. Several matches deliberately
        stay as separate ``pending_review`` candidates (different
        configurations of one model are exactly what the user must choose
        between), and when nothing matches, the full list stays and the
        mismatch is recorded as a gap instead of silently emptying the report.
        """
        from backend.services.procurement_rules import contains_model
        target = str(target_model or '').strip()
        if not target:
            return
        matched = [c for c in result['candidates'] if contains_model(str(c.get('model') or ''), target)]
        if not matched:
            result['gaps'].append(f'已确认型号 {target} 未在资料中匹配到候选，已保留全部候选供人工确认。')
            return
        result['candidates'] = matched
        if len(matched) > 1:
            result['gaps'].append(f'已按已确认型号 {target} 筛选出 {len(matched)} 个候选（可能是同型号的不同配置），请人工确认。')

    def _analyze_visual(self, scenario: str,
                        prompt_docs: list[dict[str, Any]]) -> Mapping[str, Any]:
        return self._analyze_candidate_pipeline(scenario, prompt_docs)

    def _analyze_native_text(self, scenario: str,
                             prompt_docs: list[dict[str, Any]]) -> Mapping[str, Any]:
        return self._analyze_candidate_pipeline(scenario, prompt_docs)

    def _analyze_candidate_pipeline(self, scenario: str,
                                    prompt_docs: list[dict[str, Any]]) -> Mapping[str, Any]:
        inventory_results = []
        calls = 0
        gaps = []
        #: 品牌没能从资料里读出来的候选（型号 + 资料里读到的厂名）。这一条事实的
        #: 两句话（用户侧 ``gaps``、排查侧 ``failures``）由 ``analyze`` 在型号筛选
        #: 之后一次写出，这里只收集事实。
        brand_unresolved: list[dict[str, Any]] = []
        # A conservative shared cap also bounds multi-document analysis. Each
        # explicit new analysis grants a fresh round; the hourly cap remains.
        def bounded_call(_scope=None, **kwargs):
            nonlocal calls
            checkpoint = self.checkpoint
            key = checkpoint.key(self.client, kwargs) if checkpoint else None
            if checkpoint:
                checkpoint.check()
                cached = checkpoint.get(key)
                if cached and cached['status'] == 'completed':
                    return cached['result']
                if cached and cached.get('code') == 'model_output_truncated':
                    raise DeepSeekError('model_output_truncated', '此区域曾截断，继续其拆分区域')
            if calls >= 12:
                raise DeepSeekError('document_budget_exceeded',
                                    '本轮资料分析最多调用十二次——这是单次请求的用量上限，不是故障。'
                                    '已完成的识别结果都保留着；需要看更多页时，只选关键几页再分析一次即可。')
            calls += 1
            if checkpoint:
                checkpoint.save(key, 'running', scope=_scope)
            try:
                result = self.client.analyze(**kwargs)
            except DeepSeekError as exc:
                if checkpoint:
                    checkpoint.save(key, 'failed', code=exc.code, scope=_scope)
                raise
            if checkpoint:
                checkpoint.save(key, 'completed', result=result, scope=_scope)
                checkpoint.check()
            return result
        def inventory_batch(batch, depth=0):
            try:
                inventory_results.append(bounded_call(
                    _scope={'stage': 'inventory', 'documents': sorted({str(u.get('document_id', '')) for u in batch.get('evidence_units', [])}), 'depth': depth},
                    system_prompt=CANDIDATE_INVENTORY_PROMPT,
                    user_prompt=f"采购场景：{scenario}\n请根据以下有界资料批次整理 json 候选清单：\n" + json.dumps(batch, ensure_ascii=False, separators=(",", ":")),
                ))
            except DeepSeekError as exc:
                units = batch.get('evidence_units', [])
                if exc.code != 'model_output_truncated' or depth >= 2 or not units:
                    raise
                if len(units) > 1:
                    parts = [units[:len(units)//2], units[len(units)//2:]]
                else:
                    unit = units[0]
                    text = unit.get('text', '')
                    if len(text) < 100:
                        raise
                    middle = len(text)//2
                    parts = [[{**unit, 'text': text[:middle]}], [{**unit, 'text': text[middle:]}]]
                for part in parts:
                    inventory_batch({**batch, 'evidence_units': part}, depth + 1)
        for batch in [b for doc in prompt_docs for b in _inventory_batches([doc])]:
            try:
                inventory_batch(batch)
            except DeepSeekError as exc:
                if not inventory_results:
                    raise
                gaps.append('目录识别尚未完成：' + exc.message)
                break
        inventory_results.append(_structured_mention_inventory(prompt_docs))
        candidates = _merge_inventory_candidates(
            inventory_results, _source_evidence_index(prompt_docs),
        )
        if not candidates:
            return {"candidates": [], "gaps": gaps, "call_count": calls,
                    "brand_unresolved": []}
        output = []
        # Sources arrive here already checked word-for-word against every page of
        # the supplied documents. A candidate whose model is written only as a
        # table column keeps that evidence even though no page "owns" it, so the
        # fallback check below must use the same full-text index, not the
        # narrower per-candidate detail scope.
        full_text_evidence = _source_evidence_index(prompt_docs)
        for candidate in candidates:
            from backend.services.evidence_ownership import scoped_documents
            owned_docs = scoped_documents(candidate, prompt_docs, candidates)
            # The maker's contact page names no model, so it survives in the
            # owned documents but not in the candidate-level evidence. Scan the
            # owned documents' full text for it (see
            # ``_enterprise_brand_from_documents``). Filled on the candidate
            # itself so a later detail-pass failure still keeps the brand.
            owned_ids = {str(doc.get("document_id") or "") for doc in owned_docs}
            owned_full_docs = [
                doc for doc in prompt_docs
                if str(doc.get("document_id") or "") in owned_ids
            ] or prompt_docs
            body_brand, body_brand_names = _enterprise_brand_from_documents(owned_full_docs)
            if not str(candidate.get("brand") or "").strip():
                if body_brand:
                    # Extraction only: the manufacturer's registered name is in
                    # the body, so the candidate carries it. Brand agreement for
                    # discovered prices is still decided by ``brand_identity`` /
                    # ``brand_name_matches`` -- that gate is untouched.
                    candidate["brand"] = body_brand
                elif len(body_brand_names) > 1:
                    gaps.append(
                        f"{candidate.get('model')}：资料正文里出现多个厂名（{'、'.join(body_brand_names[:3])}），"
                        "未自动填入品牌，请人工指定。"
                    )
                else:
                    # 资料里没有厂名（或只有一个厂名但归不到品牌）。这是资料的事实，
                    # 不是抽取失败，品牌照旧留空；但它决定了下游能不能判"跨品牌
                    # 相似品"，所以用户侧与排查侧各留一条痕（``docs/209`` §3.4）。
                    # 文案由 ``analyze`` 在 ``_scope_to_target_model`` 之后写：
                    # 只对**最终留下的候选**说这件事，对外粒度与结果一致（§2）。
                    brand_unresolved.append({
                        "model": str(candidate.get("model") or ""),
                        "names": list(body_brand_names),
                    })
            if not owned_docs:
                # No page owns this model (it appears only as a table column or
                # behind a series label), so the detail call would be sent with
                # empty evidence and could only answer "unknown". Skip the call
                # and record why, instead of paying for a blank result and then
                # reporting the candidate as if nothing had been looked at.
                gaps.append(
                    f"{candidate.get('model')}：型号在正文里没有独立成页出现（可能只出现在表格列或系列名里），"
                    "未做参数抽取，参数留空，请人工确认。"
                )
                skipped = dict(candidate)
                skipped["fields"] = []
                skipped["sources"] = _validated_sources(
                    candidate.get("sources", []), full_text_evidence,
                )[:2]
                output.append(skipped)
                continue
            candidate_json = json.dumps(candidate, ensure_ascii=False, separators=(",", ":"))
            evidence_payload = _candidate_evidence_payload(candidate, owned_docs)
            evidence_json = json.dumps(
                evidence_payload,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            try:
                def detail_region(payload, depth=0):
                    try:
                        return bounded_call(
                            _scope={'stage': 'parameters', 'model': candidate.get('model'), 'depth': depth,
                                    'documents': [d.get('document_id') for d in payload.get('documents', [])]},
                            system_prompt=VISUAL_DETAIL_PROMPT,
                            user_prompt=(
                                f"采购场景：{scenario}\ncandidate：{candidate_json}\n"
                                f"候选级有界证据 JSON：\n{json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}"
                            ),
                        )
                    except DeepSeekError as exc:
                        if exc.code != 'model_output_truncated' or depth >= 2:
                            raise
                        from backend.services.analysis_regions import split_evidence
                        parts = split_evidence(payload)
                        if len(parts) < 2:
                            raise
                        merged = {**candidate, 'fields': [], 'sources': []}
                        for part in parts:
                            partial = detail_region(part, depth + 1)
                            for item in partial.get('candidates', []):
                                merged['fields'].extend(item.get('fields', item.get('parameters', [])))
                                merged['sources'].extend(item.get('sources', []))
                        return {'candidates': [merged]}
                detail = detail_region(evidence_payload)
            except DeepSeekError as exc:
                skipped_candidates = candidates[len(output):]
                if not skipped_candidates:
                    gaps.append(exc.message)
                for remaining in skipped_candidates:
                    gaps.append(
                        f"{remaining.get('model')}：参数抽取未完成（{exc.message}），"
                        "该候选参数留空，请人工确认。"
                    )
                    output.append({**remaining, 'fields': []})
                break
            detail_candidates = detail.get("candidates", [])
            if not isinstance(detail_candidates, list) or not detail_candidates or not isinstance(detail_candidates[0], Mapping):
                raise DeepSeekError("candidate_detail_missing", f"候选 {candidate.get('model')} 未返回详情")
            item = dict(detail_candidates[0])
            detail_evidence = _source_evidence_index(evidence_payload["documents"])
            item["sources"] = _validated_sources(item.get("sources", []), detail_evidence)
            raw_fields = item.get("fields", item.get("parameters", []))
            if isinstance(raw_fields, list):
                raw_fields = [_validated_field(field, detail_evidence) if isinstance(field, Mapping) else field
                              for field in raw_fields]
                item["fields"] = raw_fields
                requested_fields = _repairable_field_names(raw_fields, evidence_payload)
                for repair_pass in range(2):
                    if not requested_fields:
                        break
                    try:
                        repaired = bounded_call(
                            _scope={'stage': 'field_repair', 'model': candidate.get('model'), 'fields': requested_fields},
                            system_prompt=FIELD_GAP_PROMPT,
                            user_prompt=(
                                f"采购场景：{scenario}\ncandidate：{candidate_json}\n"
                                f"requested_fields：{json.dumps(requested_fields, ensure_ascii=False)}\n"
                                f"current_fields：{json.dumps([f for f in raw_fields if isinstance(f, Mapping) and f.get('field_name') in requested_fields], ensure_ascii=False)}\n"
                                f"候选级有界证据 JSON：\n{evidence_json}"
                            ),
                        )
                    except DeepSeekError as exc:
                        gaps.append('部分参数补提取未完成：' + exc.message)
                        break
                    updated_fields = _merge_repaired_fields(
                        raw_fields, repaired.get("fields", []) if isinstance(repaired, Mapping) else [],
                        requested_fields, detail_evidence,
                    )
                    remaining_fields = _repairable_field_names(updated_fields, evidence_payload)
                    raw_fields = updated_fields
                    item["fields"] = raw_fields
                    if remaining_fields == requested_fields:
                        break
                    requested_fields = remaining_fields
                for field in raw_fields:
                    if isinstance(field, Mapping) and field.get("_unsupported_parts"):
                        gaps.append(f"{candidate.get('model')} · {field.get('field_name')}：部分参数未通过原文校验（引文逐字对不上正文、数值没有单位，或指标与证据行对不上），未采用（{'、'.join(field['_unsupported_parts'])}）")
            for key in ("brand", "series", "model", "configuration"):
                item[key] = candidate.get(key)
            if not item.get("sources"):
                item["sources"] = _validated_sources(
                    candidate.get("sources", []), full_text_evidence,
                )[:2]
            output.append(item)
        return {"candidates": output, "gaps": gaps, "call_count": calls,
                "brand_unresolved": brand_unresolved}

    def _normalize(self, raw: Mapping[str, Any]) -> dict[str, Any]:
        candidates = []
        for item in raw.get("candidates", []) if isinstance(raw.get("candidates", []), list) else []:
            if not isinstance(item, Mapping) or not str(item.get("model", "")).strip():
                continue
            sources = [_source(x) for x in item.get("sources", [])] if isinstance(item.get("sources", []), list) else []
            sources = [s for s in sources if s["document_id"] or s["excerpt"]][:2]
            fields_by_name: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
            raw_fields = item.get("fields", item.get("parameters", []))
            if isinstance(raw_fields, list):
                for field in raw_fields:
                    if not isinstance(field, Mapping):
                        continue
                    field_name = str(field.get("field_name", "")).strip()
                    if field_name not in CANONICAL_FIELD_NAMES:
                        continue
                    fs = [_source(x) for x in field.get("sources", sources)] if isinstance(field.get("sources", sources), list) else sources
                    fs = [s for s in fs if s["document_id"] or s["excerpt"]][:_FIELD_SOURCE_LIMIT]
                    value = field.get("value", "unknown")
                    normalized_field = {
                        "field_name": field_name,
                        "value": value,
                        "unit": field.get("unit"),
                        "status": _status(field.get("status"), has_source=bool(fs)),
                        "sources": fs,
                    }
                    unsupported = field.get("_unsupported_parts")
                    raw_value = field.get("_unsupported_values")
                    raw_evidence = _unparsed_field_evidence(field, fs)
                    if raw_evidence and (
                        str(value).strip().casefold() in {"", "unknown", "none", "null", "未知", "未提供"}
                        or unsupported
                    ):
                        normalized_field["raw_evidence"] = raw_evidence
                        normalized_field["evidence_status"] = "unparsed"
                        if isinstance(raw_value, Mapping) and raw_value:
                            normalized_field["unparsed_values"] = dict(raw_value)
                        if isinstance(unsupported, list) and unsupported:
                            normalized_field["unparsed_parts"] = [str(item) for item in unsupported]
                    fields_by_name[field_name].append(normalized_field)
            fields = []
            for field_name in CANONICAL_FIELD_NAMES:
                matches = fields_by_name[field_name]
                if not matches:
                    fields.append({
                        "field_name": field_name,
                        "value": "unknown",
                        "unit": None,
                        "status": "unknown",
                        "sources": [],
                    })
                    continue
                first = dict(matches[0])
                merged_sources = []
                source_keys = set()
                for match in matches:
                    for source in match["sources"]:
                        key = (
                            source.get("document_id"),
                            source.get("page_or_location"),
                            source.get("excerpt"),
                        )
                        if key not in source_keys:
                            source_keys.add(key)
                            merged_sources.append(source)
                first["sources"] = merged_sources[:_FIELD_SOURCE_LIMIT]
                raw_evidence = []
                unparsed_values = {}
                unparsed_parts = []
                for match in matches:
                    for evidence in match.get("raw_evidence", []):
                        if evidence not in raw_evidence:
                            raw_evidence.append(evidence)
                    if isinstance(match.get("unparsed_values"), Mapping):
                        unparsed_values.update(match["unparsed_values"])
                    unparsed_parts.extend(match.get("unparsed_parts", []))
                if raw_evidence:
                    first["raw_evidence"] = raw_evidence[:_FIELD_SOURCE_LIMIT]
                    first["evidence_status"] = "unparsed"
                if unparsed_values:
                    first["unparsed_values"] = unparsed_values
                if unparsed_parts:
                    first["unparsed_parts"] = list(dict.fromkeys(unparsed_parts))
                distinct_values = {_value_identity(match["value"]) for match in matches}
                if not first["sources"]:
                    first["value"] = "unknown"
                    first["unit"] = None
                    first["status"] = "unknown"
                elif len(distinct_values) > 1:
                    first["value"] = [match["value"] for match in matches]
                    first["unit"] = None if len({_value_identity(match["unit"]) for match in matches}) > 1 else first["unit"]
                    first["status"] = "conflicted"
                elif any(match["status"] == "conflicted" for match in matches):
                    first["status"] = "conflicted"
                else:
                    supported_statuses = [match["status"] for match in matches if match["sources"]]
                    first["status"] = "pending_review" if "pending_review" in supported_statuses else supported_statuses[0]
                fields.append(first)
            candidates.append({"candidate_id": "analysis-" + uuid.uuid4().hex[:12], "brand": item.get("brand"), "series": item.get("series"),
                               "model": str(item["model"]), "configuration": item.get("configuration"),
                               "status": "pending_review", "sources": sources, "fields": fields})
        return {"analysis_id": "analysis-" + uuid.uuid4().hex,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "status": "pending_review", "candidates": candidates,
                "disclaimer": "DeepSeek 输出仅为待复核草稿，未被确认前不得作为已核验事实或正式报价。"}


__all__ = ["DeepSeekAnalysisService", "AnalysisValidationError", "CANONICAL_FIELD_NAMES"]
