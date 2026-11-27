"""Phase 4.4 tests: Word (.docx) & Excel (.xlsx) comparison report export."""
from __future__ import annotations

from io import BytesIO
from pathlib import Path

from docx import Document
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from backend.main import create_app


def test_export_single_report_docx_and_xlsx_humanize_gaps_and_include_all_tables(tmp_path):
    app = create_app(database_path=tmp_path / "app.sqlite3", storage_dir=tmp_path / "files")
    with TestClient(app) as client:
        repo = app.state.repository
        task_id = "task-exp1"
        report_id = "rep-exp1"
        repo.put("tasks", task_id, {"id": task_id, "scenario": "采购高温灭菌锅", "category": "灭菌锅"})
        repo.put(
            "reports",
            report_id,
            {
                "task_id": task_id,
                "history_version": {"version_number": 2, "created_at": "2026-10-05T08:00:00+00:00"},
                "report": {
                    "report_id": report_id,
                    "title": "灭菌锅采购比选报告",
                    "baseline_version": 2,
                    "created_at": "2026-10-05T08:00:00+00:00",
                    "sections": [
                        {
                            "title": "价格核查结论",
                            "summary": "发现 1 条可比历史成交价；no_official_source 提醒已转换",
                            "items": [{"label": "单价对比", "text": "历史成交 12000 元", "evidence": []}],
                        }
                    ],
                },
                "snapshot": {
                    "baseline": {
                        "category": "立式压力蒸汽灭菌器",
                        "brand": "上海申安",
                        "model": "LDZX-50L",
                        "supplier_price": 13500,
                        "currency": "CNY",
                        "quantity": 1,
                        "configuration": "50L 全自动",
                        "warranty": "2年",
                    },
                    "price_assessment": "报价略高于历史成交参考",
                    "prices": [
                        {
                            "brand": "上海申安",
                            "model": "LDZX-50L",
                            "unit_price": 12000.0,
                            "currency": "CNY",
                            "quantity": 1,
                            "date": "2026-06-15",
                            "source_type": "tender_document",
                            "url": "https://zfcg.czt.zj.gov.cn/1",
                            "excerpt": "单价（元）：12000.00",
                        }
                    ],
                    "alternatives": [
                        {
                            "brand": "致微",
                            "model": "GI54DW",
                            "status": "eligible",
                            "unit_price": 11500.0,
                            "configuration": "54L",
                            "reason": "参数满足",
                        }
                    ],
                    "gaps": ["no_official_source", "tender_body_unverified: https://example.com/a"],
                    "sources": [
                        {
                            "url": "https://zfcg.czt.zj.gov.cn/1",
                            "source_type": "tender_document",
                            "body_status": "fetched",
                            "error_reason": "",
                        }
                    ],
                },
            },
            task_id=task_id,
        )

        # Missing report returns 404
        assert client.get("/api/v1/reports/not-exists/export?format=docx").status_code == 404

        # DOCX export
        docx_res = client.get(f"/api/v1/reports/{report_id}/export", params={"format": "docx"})
        assert docx_res.status_code == 200
        assert "wordprocessingml.document" in docx_res.headers["content-type"]
        assert "filename*=UTF-8''" in docx_res.headers["content-disposition"]

        doc = Document(BytesIO(docx_res.content))
        full_text = "\n".join(p.text for p in doc.paragraphs)
        for tbl in doc.tables:
            for row in tbl.rows:
                full_text += "\n" + " ".join(cell.text for cell in row.cells)
        assert "LDZX-50L" in full_text
        assert "12000.0" in full_text
        assert "GI54DW" in full_text
        assert "no_official_source" not in full_text
        assert "tender_body_unverified" not in full_text

        # XLSX export
        xlsx_res = client.get(f"/api/v1/reports/{report_id}/export", params={"format": "xlsx"})
        assert xlsx_res.status_code == 200
        assert "spreadsheetml.sheet" in xlsx_res.headers["content-type"]
        wb = load_workbook(BytesIO(xlsx_res.content))
        assert wb.sheetnames == ["01-采购基准与结论", "02-历史成交价明细", "03-候选与替代对比", "04-核查缺口", "05-来源清单"]
        prices_sheet_vals = [row for row in wb["02-历史成交价明细"].iter_rows(values_only=True)]
        assert any(12000.0 in row for row in prices_sheet_vals)
        gaps_text = "\n".join(str(cell) for row in wb["04-核查缺口"].iter_rows(values_only=True) for cell in row if cell)
        assert "no_official_source" not in gaps_text
        assert "tender_body_unverified" not in gaps_text


def test_export_humanizes_all_internal_codes_and_displays_alternative_leads(tmp_path):
    app = create_app(database_path=tmp_path / "app.sqlite3", storage_dir=tmp_path / "files")
    with TestClient(app) as client:
        repo = app.state.repository
        task_id = "task-exp-clean"
        report_id = "rep-exp-clean"
        repo.put("tasks", task_id, {"id": task_id, "scenario": "采购超净工作台", "category": "超净工作台"})
        repo.put(
            "reports",
            report_id,
            {
                "task_id": task_id,
                "history_version": {"version_number": 1, "created_at": "2026-11-26T10:00:00+00:00"},
                "report": {
                    "report_id": report_id,
                    "title": "超净工作台比选报告",
                    "baseline_version": 1,
                    "created_at": "2026-11-26T10:00:00+00:00",
                    "sections": [
                        {
                            "title": "核查过程",
                            "summary": "deepseek_web 召回来源；procurement_unit、agency 与 supplier 字段见正文",
                            "items": [
                                {
                                    "label": "configuration_refetch",
                                    "text": "product_detail 与 tender_document 已试读",
                                    "evidence": [],
                                }
                            ],
                        }
                    ],
                },
                "snapshot": {
                    "baseline": {
                        "category": "超净工作台",
                        "brand": "DRAWELL",
                        "model": "SW-CJ-1FD",
                        "supplier_price": 4500,
                        "currency": "CNY",
                    },
                    "prices": [
                        {
                            "brand": "DRAWELL",
                            "model": "SW-CJ-1FD",
                            "unit_price": 3400.0,
                            "currency": "CNY",
                            "quantity": 1,
                            "date": "2024-10-12",
                            "source_type": "tender_document",
                            "url": "https://zfcg.czt.zj.gov.cn/1",
                        }
                    ],
                    "alternatives": [],
                    "discovered_candidates": [
                        {
                            "brand": "苏净安泰",
                            "model": "SW-CJ-1F",
                            "source_url": "https://www.airtech.cn/1f",
                            "reason": "已识别同档型号，configuration_refetch 未读到完整配置",
                        }
                    ],
                    "gaps": [
                        "empty_shell：抖音页面",
                        "verification_required：采招网",
                        "empty_body：空正文",
                    ],
                    "sources": [
                        {
                            "url": "https://www.example.com/a",
                            "source_type": "product_detail",
                            "body_status": "unavailable",
                            "failure_kind": "empty_shell",
                            "error_reason": "empty_shell",
                        },
                        {
                            "url": "https://www.example.com/b",
                            "source_type": "tender_document",
                            "body_status": "failed",
                            "failure_kind": "verification_required",
                            "error_reason": "verification_required",
                        },
                        {
                            "url": "https://www.example.com/c",
                            "source_type": "tender_document",
                            "body_status": "unavailable",
                            "failure_kind": "empty_body",
                            "error_reason": "empty_body",
                        },
                    ],
                },
            },
            task_id=task_id,
        )

        docx_res = client.get(f"/api/v1/reports/{report_id}/export", params={"format": "docx"})
        xlsx_res = client.get(f"/api/v1/reports/{report_id}/export", params={"format": "xlsx"})
        assert docx_res.status_code == 200
        assert xlsx_res.status_code == 200

        doc = Document(BytesIO(docx_res.content))
        docx_text = "\n".join(p.text for p in doc.paragraphs)
        for tbl in doc.tables:
            for row in tbl.rows:
                docx_text += "\n" + " ".join(cell.text for cell in row.cells)

        wb = load_workbook(BytesIO(xlsx_res.content))
        xlsx_text = "\n".join(
            str(cell)
            for sheet in wb.sheetnames
            for row in wb[sheet].iter_rows(values_only=True)
            for cell in row
            if cell is not None
        )

        forbidden_tokens = (
            "tender_document",
            "product_detail",
            "empty_shell",
            "empty_body",
            "verification_required",
            "deepseek_web",
            "procurement_unit",
            "configuration_refetch",
            "agency",
            "supplier",
        )
        for token in forbidden_tokens:
            assert token not in docx_text, f"DOCX leaked internal token: {token}"
            assert token not in xlsx_text, f"XLSX leaked internal token: {token}"

        assert "SW-CJ-1F" in docx_text
        assert "待核实替代线索" in docx_text
        assert "SW-CJ-1F" in xlsx_text
        assert "待核实替代线索" in xlsx_text
