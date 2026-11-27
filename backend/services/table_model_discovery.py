"""Discover mechanically readable model numbers in specification tables.

职责边界（与设计规范逐条确认过的硬边界一致）：

* **只发现**：从表格结构里读出"型号位"上的标识，作为**新的一路**候选并入候选池。
  读不出时什么都不发生——本模块从不产生"不采用模型候选"的结论，也从不做否决。
* **只做并集**：接入方式是给 ``inventory_results`` 多追加一路，合并仍由既有的
  ``_merge_inventory_candidates`` 完成，本模块不碰合并、不碰归属、不碰任何闸门。
* **不做证据归属**：候选只带表格行原文，归属仍由 ``evidence_ownership`` 决定。
* **不做型号形态归一**：读到的型号字符串**原样**输出（全角、空格、点号都不动）。

只实现评估已证明零减少的三种形态（依据 ``output/table-model-rule-extract/report.md``
与 ``output/table-model-rule-extract/impl2-report.md``）：

* 形态 A「列头是型号」：某行首格为空、或首格是型号标签；该行其余格全是标识形态；
  且它下方至少有两条参数标签行（证明这一行是表头，不是别的东西）。
* 形态 B「型号在行」：某行首格是型号标签，取该行其余格里第一个标识形态的格。
* 形态 C「正文列举行」：一行正文写着 ``型号：080、150、250、350、450``（型号标签 +
  冒号 + 分隔符分隔的一组标识）。扫描件没有表格结构、正文就是渲染文本，形态 A/B
  读不到它——``sample-c`` 的 ``page:2/rendered`` 就长这样。

明确不做（评估列为"漏掉的形态"；要做必须重跑同一套验证并设计规范）：
型号标签带后缀（``Model Parameters``）、标签不在首格、型号挤在跨列长文本里。
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any, Iterable, Mapping

#: 型号标签：左列写着"这里说的是型号"。
MODEL_LABEL_RE = re.compile(
    r'^(?:型号|规格型号|产品型号|仪器型号|设备型号|订货型号|产品规格|规格|'
    r'model(?:\s*(?:no\.?|number|code))?|type|cat(?:alog)?\.?\s*(?:no\.?|number)?)$',
    re.IGNORECASE,
)

#: 明显是"值"而不是"型号"的结尾单位（220V / 600W / 50Hz）。
#: 这里**故意不含单字母 ``A``**（安培）——它的判据单独放在 :data:`_AMPERE_UNIT_RE`。
_VALUE_UNIT_RE = re.compile(
    r'\d+(?:\.\d+)?\s*(?:V|W|KW|MW|MA|AH|HZ|KHZ|MHZ|MM|CM|M|L|ML|UL|KG|G|MG|PA|KPA|MPA'
    r'|BAR|DB|RPM|MIN|MS|N|NM|UM|℃|°C|C|K|PCS|SET|PCS\.)\.?$',
    re.IGNORECASE,
)

#: 单字母安培写法 ``A`` 的单独判据。
#:
#: 为什么末尾的 ``A`` 不能一律当安培单位：型号的末尾字母是规格后缀，``080A`` 就是
#: 80 的 A 型，跟电流单位 ``5A``/``10A`` **完全同形**，只看"数字＋字母"分不开。
#: 能分开的只有数字形状——**量值不写前导零，编号常写**（``080``/``0150``）。所以：
#: 数字带前导零时（``080A``/``0150A``）末尾的 A 算型号后缀；其余（``5A``/``10A``/
#: ``2.5A``/``0.5A``/``100A``）仍算安培值。宁可少认，也不把 ``080A`` 整条丢掉。
_AMPERE_UNIT_RE = re.compile(r'(?<![0-9])(?:0|[1-9]\d*)(?:\.\d+)?\s*A\.?$', re.IGNORECASE)

#: 型号形态：字母开头含数字（``SHP-080``），或纯数字短编号（``080``）。
_ALPHA_MODEL_RE = re.compile(r'^[A-Za-z][A-Za-z0-9]*(?:[\s\-_/][A-Za-z0-9]+)*$')
_NUMERIC_MODEL_RE = re.compile(r'^\d{2,4}[A-Za-z]{0,2}$')

_PIPE_SPLIT_RE = re.compile(r'[|｜]')
_SEPARATOR_ROW_RE = re.compile(r'^[\s:\-—–=+]+$')

#: 形态 C：正文里的型号列举行。**只认这一种明确的列举形态**，不做贪心解析——
#: ``<标签><冒号><分隔符分隔的一组标识>`` 之外不许有别的内容（行尾多个字就整行不认）。
_ENUM_LABEL_LINE_RE = re.compile(r'^([^:：]{1,12})\s*[:：]\s*(.+)$')
_ENUM_SPLIT_RE = re.compile(r'[、,，;；]')
#: 一排机器不会有 20 个规格，超过就当它不是列举（防御）。
_MAX_ENUM_ITEMS = 20

#: 单次分析里规则最多贡献多少条候选。这是**防御性上限**：既有的
#: ``_merge_inventory_candidates`` 在合并结果超过 50 条时抛错并让整轮分析失败，
#: 规则候选不能成为压垮它的那根稻草（那等于整批拒收，是必须避免的坑）。
#: 历史语料里规则最多一份读出 3 条，这个上限不会被现实资料触发。
_MAX_RULE_CANDIDATES = 20


def _clean(value: Any) -> str:
    return unicodedata.normalize("NFKC", str(value if value is not None else "")).strip()


def identifier_like(cell: Any) -> bool:
    """这一格像不像"型号位"上的标识（只判位置形态，不判厂家、不查白名单）。"""
    text = _clean(cell)
    if not text or len(text) > 24:
        return False
    if re.search(r'[×*~±℃%()（）\[\]{}]', text):
        return False
    if re.search(r'[，,；;。：:、！？!"\'“”]', text):
        return False
    if not re.search(r'\d', text):
        return False                      # 型号必须含数字
    if _VALUE_UNIT_RE.search(text) or _AMPERE_UNIT_RE.search(text):
        return False                      # 220V / 600W / 50Hz / 5A 是值（080A 不是）
    if _NUMERIC_MODEL_RE.match(text):
        return True                       # 080 / 150 / 080A / 250A
    if _ALPHA_MODEL_RE.match(text):
        return True                       # SHP-080 / H1850R / ZGP-9080
    return False


def parameter_label_like(cell: Any) -> bool:
    """这一格像不像参数名（左列）——用来确认"上一行是表头"。"""
    text = _clean(cell)
    if not text or len(text) > 40:
        return False
    if re.search(r'[|｜]', text):
        return False
    return not re.search(r'\d', text)


def model_label(cell: Any) -> bool:
    """这一格是不是"型号"标签（形态 A 的首格 / 形态 B 的首格）。"""
    return bool(MODEL_LABEL_RE.match(_clean(cell)))


def split_line(line: Any) -> list[str] | None:
    """把一行 markdown 管道行切成单元格；不是表格行返回 ``None``。"""
    stripped = str(line or "").strip()
    if not stripped or '|' not in stripped and '｜' not in stripped:
        return None
    body = stripped.strip('|｜').strip()
    if not body or _SEPARATOR_ROW_RE.match(body):
        return None
    cells = [_clean(part) for part in _PIPE_SPLIT_RE.split(stripped)]
    if cells and cells[0] == '':
        cells = cells[1:]
    if cells and cells[-1] == '':
        cells = cells[:-1]
    return cells if len(cells) >= 2 else None


def discover_in_prose(line: Any) -> list[str]:
    """从**一行正文**里读出"型号列举行"（形态 C）的型号；不是这种行就返回空。

    判定一条都不放宽，四处都要过：

    1. 整行必须恰好是 ``标签 + 冒号 + 列举体``，行内没有别的内容；
    2. 标签必须**整格**等于型号标签（``型号``/``规格``/``Model``…），不是"包含"——
       ``消耗功率`` / ``工作室尺寸(mm)`` 这类参数标签一律不认；
    3. 列举体由顿号/逗号/分号分隔，**至少两项**；
    4. **每一项**都要过 :func:`identifier_like`，所以 ``600W``、``400x400x500``、
       ``220V 50Hz`` 这些同样是"标签＋顿号列举"的值一个都进不来；不合格的项**直接丢掉**，
       剩下的合格项不足两项就整行放弃（丢掉不合格项不引入新的误报面——不合格项本来就读不出来，
       但能让"一项被 OCR 弄脏"不至于把整行好型号一起拖没）。

    宁可少认：形态怪（行尾带括号说明、项里混着单位）就整行放弃，不猜。
    """
    text = str(line if line is not None else "").strip()
    if not text:
        return []
    match = _ENUM_LABEL_LINE_RE.match(text)
    if not match:
        return []
    label, body = match.group(1), match.group(2)
    if not model_label(label):
        return []
    items = [piece.strip() for piece in _ENUM_SPLIT_RE.split(body)]
    items = [item for item in items if item]
    if len(items) < 2 or len(items) > _MAX_ENUM_ITEMS:
        return []
    models = [_clean(item) for item in items if identifier_like(item)]
    if len(models) < 2:
        return []
    return models


def discover_in_rows(rows: Iterable[Any]) -> list[dict[str, Any]]:
    """从一个表格（行列表）里发现型号；只返回"位置角色是型号位"的格。

    ``row`` 是传入 ``rows`` 里的**原始行号**，调用方据此取该行原文当引文。
    """
    indexed: list[tuple[int, list[str]]] = []
    for index, row in enumerate(rows):
        if not isinstance(row, (list, tuple)):
            continue
        cells = [_clean(cell) for cell in row]
        if not cells or all(not cell for cell in cells):
            continue
        if all(_SEPARATOR_ROW_RE.match(cell) for cell in cells if cell):
            continue
        indexed.append((index, cells))
    found: list[dict[str, Any]] = []
    if not indexed:
        return found

    # ---- 形态 A：列头是型号 ----
    for position, (row_index, row) in enumerate(indexed):
        if len(row) < 3:
            continue
        head, rest = row[0], row[1:]
        if head and not model_label(head):
            continue
        if len(rest) < 2:
            continue
        if not all(identifier_like(cell) for cell in rest):
            continue
        labels = [cells[0] for _, cells in indexed[position + 1:]]
        if len([label for label in labels if parameter_label_like(label)]) < 2:
            continue
        for cell in rest:
            found.append({"model": cell, "kind": "column_header", "row": row_index})
        break

    # ---- 形态 B：型号写在行里 ----
    for row_index, row in indexed:
        if len(row) < 2 or not model_label(row[0]):
            continue
        for cell in row[1:]:
            if identifier_like(cell):
                found.append({"model": cell, "kind": "label_row", "row": row_index})
                break

    return found


def discover_in_text(text: Any) -> list[dict[str, Any]]:
    """从一段文本（markdown 管道表格）里发现型号，并保留所在行的原文。"""
    lines = str(text or "").splitlines()
    found: list[dict[str, Any]] = []
    block: list[tuple[int, list[str]]] = []

    def flush() -> None:
        if len(block) >= 2:
            for item in discover_in_rows([cells for _, cells in block]):
                item["line"] = lines[block[item["row"]][0]].strip()
                found.append(item)
        block.clear()

    for index, line in enumerate(lines):
        cells = split_line(line)
        if cells is None:
            flush()
            # 形态 C：正文里的列举行（没有表格结构的那类扫描件只有这一路可读）
            for model in discover_in_prose(line):
                found.append({"model": model, "kind": "prose_enum", "row": index,
                              "line": line.strip()})
            continue
        block.append((index, cells))
    flush()

    # 形态 B 也允许出现在非连续的单行里（如 "| 型号 | H1850R |" 独自成段）
    for line in lines:
        cells = split_line(line)
        if not cells or not model_label(cells[0]):
            continue
        if any(item.get("line", "").strip() == line.strip() for item in found):
            continue
        for cell in cells[1:]:
            if identifier_like(cell):
                found.append({"model": cell, "kind": "label_row", "row": 0,
                              "line": line.strip()})
                break

    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for item in found:
        key = unicodedata.normalize("NFKC", item["model"]).casefold()
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def _document_hits(doc: Mapping[str, Any]) -> list[dict[str, Any]]:
    document_id = str(doc.get("document_id") or doc.get("id") or "")
    hits: list[dict[str, Any]] = []
    pages = doc.get("pages")
    for page in pages if isinstance(pages, list) else []:
        if not isinstance(page, Mapping):
            continue
        location = str(page.get("location") or page.get("source_location") or "")
        for item in discover_in_text(page.get("text")):
            hits.append({**item, "document_id": document_id, "location": location})
    tables = doc.get("tables")
    for table in tables if isinstance(tables, list) else []:
        if not isinstance(table, Mapping):
            continue
        rows = table.get("rows")
        if not isinstance(rows, list):
            continue
        location = str(table.get("source_location") or f"table:{table.get('index')}")
        for item in discover_in_rows(rows):
            line = " | ".join(_clean(cell) for cell in rows[item["row"]] if _clean(cell))
            hits.append({**item, "document_id": document_id, "location": location,
                         "line": line})
    # 同一份资料里同一个型号只留第一次读到的位置：形态 A 与形态 B 会读到同一个型号
    # （首格写着"型号"的列头行同时满足两种形态），去重后候选池里不会出现两条一样的锚点。
    unique: list[dict[str, Any]] = []
    seen_models: set[str] = set()
    for hit in hits:
        key = unicodedata.normalize("NFKC", str(hit["model"])).casefold()
        if key in seen_models:
            continue
        seen_models.add(key)
        unique.append(hit)
    return unique


def table_model_candidates(prompt_docs: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """产出**候选**，结构与清单环节的模型输出一致，可直接并入 ``inventory_results``。

    只发现，不判定——没有任何一条逻辑会因为这里读不到型号而影响别的候选。
    """
    candidates: list[dict[str, Any]] = []
    for doc in prompt_docs:
        if not isinstance(doc, Mapping):
            continue
        for hit in _document_hits(doc):
            if len(candidates) >= _MAX_RULE_CANDIDATES:
                return candidates
            candidates.append({
                "product_role": "instrument",
                "brand": None,
                "series": None,
                "model": hit["model"],
                "configuration": None,
                "sources": [{
                    "document_id": hit["document_id"],
                    "page_or_location": hit["location"],
                    "excerpt": hit.get("line") or hit["model"],
                }],
            })
    return candidates


def table_model_inventory(prompt_docs: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """给 ``inventory_results`` 用的一路输入（与 ``_structured_mention_inventory`` 同形）。"""
    return {"candidates": table_model_candidates(prompt_docs)}
