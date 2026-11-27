"""Standard Word (.docx) and Excel (.xlsx) comparison report exporters."""
from __future__ import annotations

import re
from io import BytesIO
from typing import Any
from urllib.parse import quote

from docx import Document
from docx.shared import Pt
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill

from backend.services.report_library import (
    evidence_label,
    humanize_body_status,
    humanize_export_text,
    humanize_failure_kind,
    humanize_source_type,
)

_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def office_media_type(fmt: str) -> str:
    return _DOCX_MIME if fmt == "docx" else _XLSX_MIME


def office_download_headers(title: str, fmt: str) -> dict[str, str]:
    clean = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", str(title or "procurement-report")).strip() or "procurement-report"
    utf8_name = quote(f"{clean[:48]}.{fmt}")
    return {"Content-Disposition": f'attachment; filename="labpilot-report.{fmt}"; filename*=UTF-8\'\'{utf8_name}'}


def _extract_bundle(item: dict[str, Any], record: dict[str, Any]) -> dict[str, Any]:
    report = record.get("report") if isinstance(record.get("report"), dict) else {}
    snap = record.get("snapshot") if isinstance(record.get("snapshot"), dict) else {}
    baseline = snap.get("baseline") if isinstance(snap.get("baseline"), dict) else {}
    if not baseline and isinstance(snap.get("requirement"), dict):
        baseline = snap["requirement"]
    prices = list(snap.get("prices") or [])
    alternatives = list(snap.get("alternatives") or [])
    if not alternatives and isinstance(snap.get("candidates"), list):
        alternatives = list(snap["candidates"])
    existing_alt_keys = {
        (str(a.get("brand") or "").strip().casefold(), str(a.get("model") or "").strip().casefold())
        for a in alternatives
        if isinstance(a, dict)
    }
    alternative_leads = []
    seen_lead_keys = set()
    for cand in list(snap.get("discovered_candidates") or []):
        if not isinstance(cand, dict):
            continue
        model = str(cand.get("model") or "").strip()
        if not model:
            continue
        brand = str(cand.get("brand") or "").strip()
        pair = (brand.casefold(), model.casefold())
        if pair in existing_alt_keys or pair in seen_lead_keys:
            continue
        seen_lead_keys.add(pair)
        alternative_leads.append(cand)
    sources = list(snap.get("sources") or [])
    raw_gaps = list(snap.get("gaps") or [])
    gaps = [humanize_export_text(str(g)) for g in raw_gaps if g]
    sections = list(report.get("sections") or [])
    return {
        "title": str(item.get("title") or report.get("title") or "实验室仪器采购比选报告"),
        "version": item.get("version") or report.get("baseline_version") or 1,
        "created_at": str(item.get("created_at") or report.get("created_at") or ""),
        "scenario": str(item.get("scenario") or baseline.get("scenario") or ""),
        "baseline": baseline,
        "prices": prices,
        "alternatives": alternatives,
        "alternative_leads": alternative_leads,
        "sources": sources,
        "gaps": gaps,
        "sections": sections,
        "price_assessment": str(snap.get("price_assessment") or ""),
    }


def _add_table(doc: Document, headers: list[str], rows: list[list[str]]) -> None:
    table = doc.add_table(rows=1, cols=len(headers))
    table.style = "Table Grid"
    hdr_cells = table.rows[0].cells
    for idx, text in enumerate(headers):
        hdr_cells[idx].text = text
        for run in hdr_cells[idx].paragraphs[0].runs:
            run.bold = True
    for row_vals in rows:
        row_cells = table.add_row().cells
        for idx, val in enumerate(row_vals):
            row_cells[idx].text = str(val or "")


def export_report_docx(item: dict[str, Any], record: dict[str, Any]) -> bytes:
    """Render a structured procurement comparison document as .docx bytes."""
    data = _extract_bundle(item, record)
    doc = Document()
    style = doc.styles["Normal"]
    style.font.name = "Microsoft YaHei"
    style.font.size = Pt(10.5)

    doc.add_heading(f"{data['title']}（版本 {data['version']}）", level=0)
    doc.add_paragraph(f"生成时间：{data['created_at'] or '未记录'}")
    if data["scenario"]:
        doc.add_paragraph(f"采购场景：{data['scenario']}")
    if data["price_assessment"]:
        doc.add_paragraph(f"核价结论：{humanize_export_text(data['price_assessment'])}")

    # 1. 比较基准与商务条件
    base = data["baseline"]
    if base:
        doc.add_heading("一、比较基准与商务条件", level=1)
        terms_rows = [
            ["仪器品类", str(base.get("category") or "未填写")],
            ["品牌 / 型号", f"{base.get('brand') or ''} {base.get('model') or ''}".strip() or "未填写"],
            ["销售单价", f"{base['supplier_price']} {base.get('currency') or 'CNY'}" if base.get("supplier_price") not in (None, "") else "未提供"],
            ["采购数量", str(base.get("quantity") or "未填写")],
            ["配置说明", humanize_export_text(str(base.get("configuration") or "用户未填写"))],
            ["保修与售后", f"保修：{base.get('warranty') or '未填写'}；响应：{base.get('service_response') or '未填写'}；交期：{base.get('delivery') or '未填写'}"],
        ]
        _add_table(doc, ["项目", "确认内容"], terms_rows)

    # 2. 历史成交价格明细
    doc.add_heading("二、历史成交价格核查明细", level=1)
    if data["prices"]:
        price_rows = []
        for p in data["prices"]:
            price_rows.append([
                str(p.get("brand") or ""),
                str(p.get("model") or ""),
                f"{p.get('unit_price', '')} {p.get('currency') or 'CNY'}",
                str(p.get("quantity") or ""),
                str(p.get("date") or p.get("deal_date") or ""),
                humanize_source_type(p.get("source_type")),
                str(p.get("url") or ""),
            ])
        _add_table(doc, ["品牌", "型号", "成交单价", "数量", "日期", "来源类型", "出处链接"], price_rows)
    else:
        doc.add_paragraph("本轮未核实到可采信的同款历史成交单价记录。")

    # 3. 候选与其他品牌横向对比
    doc.add_heading("三、候选与替代品牌横向对比", level=1)
    if data["alternatives"]:
        alt_rows = []
        for alt in data["alternatives"]:
            alt_prices = alt.get("prices") or []
            price_text = " / ".join(str(ap.get("unit_price")) for ap in alt_prices if ap.get("unit_price")) or str(alt.get("unit_price") or "未查价")
            alt_rows.append([
                str(alt.get("brand") or ""),
                str(alt.get("model") or ""),
                humanize_export_text(str(alt.get("status") or "")),
                price_text,
                humanize_export_text(str(alt.get("configuration") or ""))[:120],
                humanize_export_text(str(alt.get("reason") or "")),
            ])
        _add_table(doc, ["品牌", "型号", "核查结论", "参考成交价", "配置摘要", "依据说明"], alt_rows)
    else:
        doc.add_paragraph("本轮暂无可供完整参数比对的其他品牌替代型号。")
    if data["alternative_leads"]:
        doc.add_heading("待核实替代线索（已发现型号，尚未读到完整参数配置）", level=2)
        lead_rows = []
        for lead in data["alternative_leads"][:30]:
            lead_url = str(lead.get("configuration_url") or lead.get("source_url") or lead.get("url") or "")
            lead_note = str(lead.get("reason") or (lead.get("price_lookup") or {}).get("note") or "已发现同档候选型号线索，待补充参数配置页")
            lead_rows.append([
                str(lead.get("brand") or "品牌待确认"),
                str(lead.get("model") or ""),
                "待补查配置（仅型号线索）",
                lead_url,
                humanize_export_text(lead_note),
            ])
        _add_table(doc, ["品牌", "型号", "线索状态", "发现来源", "说明"], lead_rows)

    # 4. 报告分节内容
    if data["sections"]:
        doc.add_heading("四、报告分节详情", level=1)
        for sec in data["sections"]:
            doc.add_heading(humanize_export_text(str(sec.get("title") or "分节")), level=2)
            if sec.get("summary"):
                doc.add_paragraph(humanize_export_text(str(sec["summary"])))
            for entry in sec.get("items") or []:
                label = humanize_export_text(str(entry.get("label") or ""))
                body = humanize_export_text(str(entry.get("text") or ""))
                doc.add_paragraph(f"{label}：{body}", style="List Bullet")
                for ev in entry.get("evidence") or []:
                    doc.add_paragraph(f"来源：{humanize_export_text(evidence_label(ev))}")
            if sec.get("missing_reason"):
                doc.add_paragraph(f"待补充：{humanize_export_text(str(sec['missing_reason']))}")

    # 5. 待核实缺口与来源清单
    if data["gaps"]:
        doc.add_heading("五、待补充缺口提醒", level=1)
        for gap in data["gaps"][:50]:
            doc.add_paragraph(gap, style="List Bullet")

    if data["sources"]:
        doc.add_heading("六、来源核查清单", level=1)
        src_rows = [
            [
                humanize_source_type(s.get("source_type")),
                humanize_body_status(s.get("body_status")),
                humanize_export_text(str(s.get("error_reason") or "已读取")),
                str(s.get("url") or ""),
            ]
            for s in data["sources"][:100]
        ]
        _add_table(doc, ["来源类型", "读取状态", "状态说明", "网址"], src_rows)

    buf = BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _style_sheet(ws, headers: list[str], rows: list[list[Any]]) -> None:
    header_fill = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")
    header_font = Font(bold=True, color="FFFFFF")
    ws.append(headers)
    for col_idx in range(1, len(headers) + 1):
        cell = ws.cell(row=1, column=col_idx)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center")
    for row in rows:
        ws.append(row)
    ws.freeze_panes = "A2"
    for col in ws.columns:
        max_len = max((len(str(c.value or "")) for c in col), default=8)
        ws.column_dimensions[col[0].column_letter].width = min(max(max_len + 4, 12), 52)


def export_report_xlsx(item: dict[str, Any], record: dict[str, Any]) -> bytes:
    """Render a multi-sheet Excel procurement comparison workbook as .xlsx bytes."""
    data = _extract_bundle(item, record)
    wb = Workbook()

    # Sheet 1: 采购基准与结论
    ws1 = wb.active
    ws1.title = "01-采购基准与结论"
    base = data["baseline"]
    s1_rows = [
        ["报告标题", data["title"]],
        ["报告版本", data["version"]],
        ["生成时间", data["created_at"]],
        ["采购场景", data["scenario"]],
        ["核价结论", humanize_export_text(data["price_assessment"])],
        ["仪器品类", base.get("category") or ""],
        ["基准品牌", base.get("brand") or ""],
        ["基准型号", base.get("model") or ""],
        ["销售单价", base.get("supplier_price") if base.get("supplier_price") is not None else ""],
        ["币种", base.get("currency") or "CNY"],
        ["数量", base.get("quantity") if base.get("quantity") is not None else ""],
        ["配置说明", humanize_export_text(str(base.get("configuration") or ""))],
        ["保修", base.get("warranty") or ""],
        ["交期", base.get("delivery") or ""],
    ]
    for sec in data["sections"]:
        s1_rows.append([f"章节·{humanize_export_text(str(sec.get('title', '')))}", humanize_export_text(str(sec.get("summary") or ""))])
    _style_sheet(ws1, ["项目", "内容"], s1_rows)

    # Sheet 2: 历史成交价明细
    ws2 = wb.create_sheet("02-历史成交价明细")
    s2_rows = [
        [
            p.get("brand") or "",
            p.get("model") or "",
            p.get("unit_price"),
            p.get("currency") or "CNY",
            p.get("quantity"),
            p.get("date") or p.get("deal_date") or "",
            humanize_source_type(p.get("source_type")),
            "历史记忆" if p.get("from_memory") else "本轮检索",
            p.get("url") or "",
            humanize_export_text(str(p.get("excerpt") or ""))[:200],
        ]
        for p in data["prices"]
    ]
    _style_sheet(ws2, ["品牌", "型号", "成交单价", "币种", "数量", "成交日期", "来源类型", "数据渠道", "出处链接", "摘要原文"], s2_rows)

    # Sheet 3: 候选与替代对比
    ws3 = wb.create_sheet("03-候选与替代对比")
    s3_rows = []
    for alt in data["alternatives"]:
        alt_prices = alt.get("prices") or []
        price_val = alt_prices[0].get("unit_price") if alt_prices else alt.get("unit_price")
        s3_rows.append([
            alt.get("brand") or "",
            alt.get("model") or "",
            humanize_export_text(str(alt.get("status") or "")),
            price_val if price_val is not None else "",
            humanize_export_text(str(alt.get("configuration") or ""))[:180],
            humanize_export_text(str(alt.get("reason") or "")),
        ])
    for lead in data["alternative_leads"][:30]:
        lead_url = str(lead.get("configuration_url") or lead.get("source_url") or lead.get("url") or "")
        lead_note = str(lead.get("reason") or (lead.get("price_lookup") or {}).get("note") or "已发现候选型号，尚未读到完整参数配置")
        s3_rows.append([
            lead.get("brand") or "品牌待确认",
            lead.get("model") or "",
            "待核实替代线索（待补查配置）",
            "",
            lead_url,
            humanize_export_text(lead_note),
        ])
    _style_sheet(ws3, ["品牌", "型号", "结论状态", "参考成交价", "配置说明或发现来源", "依据说明"], s3_rows)

    # Sheet 4: 核查缺口
    ws4 = wb.create_sheet("04-核查缺口")
    _style_sheet(ws4, ["序号", "缺口说明（已转为可读中文）"], [[idx, g] for idx, g in enumerate(data["gaps"], 1)])

    # Sheet 5: 来源清单
    ws5 = wb.create_sheet("05-来源清单")
    s5_rows = [
        [
            humanize_source_type(s.get("source_type")),
            humanize_body_status(s.get("body_status")),
            humanize_failure_kind(s.get("failure_kind")),
            humanize_export_text(str(s.get("error_reason") or "已读取")),
            s.get("url") or "",
        ]
        for s in data["sources"]
    ]
    _style_sheet(ws5, ["来源类型", "读取状态", "失败类型", "说明", "网址"], s5_rows)

    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()
