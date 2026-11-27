"""Startup & runtime configuration self-check (eliminates silent 200 degradation)."""
from __future__ import annotations

import importlib.util
import ipaddress
import os
import socket
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.infrastructure.search_providers import channel_quarantine_reason


def _check_dns_target(host: str = "www.ccgp.gov.cn") -> dict[str, Any]:
    fake_ip_net = ipaddress.ip_network("198.18.0.0/15")
    try:
        rows = socket.getaddrinfo(host, None)
    except OSError as exc:
        return {
            "id": "network_dns_guard",
            "name": "外网域名解析与代理拦截检查",
            "category": "network",
            "status": "warning",
            "message": f"无法解析公网测试域名 {host}（{type(exc).__name__}），当前可能处于离线或断网状态。",
            "degraded_impact": "公网搜索与政府采购公告抓取会超时或失败，仅本地历史库可正常使用。",
            "remediation": "请确认本机网络连接正常；如在离线环境下仅需查历史资料库，可忽略此提示。",
            "details": {"host": host, "ips": []},
        }

    ips: list[str] = []
    private_ips: list[str] = []
    fake_ips: list[str] = []
    for row in rows:
        ip_str = str(row[4][0])
        if ip_str not in ips:
            ips.append(ip_str)
        try:
            addr = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        if addr in fake_ip_net:
            if ip_str not in fake_ips:
                fake_ips.append(ip_str)
        elif not addr.is_global:
            if ip_str not in private_ips:
                private_ips.append(ip_str)

    if fake_ips or private_ips:
        sample = ", ".join((fake_ips or private_ips)[:3])
        is_fake = bool(fake_ips)
        return {
            "id": "network_dns_guard",
            "name": "外网域名解析与代理拦截检查",
            "category": "network",
            "status": "error",
            "message": (
                f"检测到 {host} 解析到{'代理 TUN 假 IP（198.18.x.x）' if is_fake else '私有网络地址'}：{sample}，"
                "会被系统 SSRF 防护拦截。"
            ),
            "degraded_impact": "中国政府采购网、省本级直查及外部公告正文抓取会全部秒拒（私有网络拦截），导致查不到历史成交价。",
            "remediation": "请关闭代理软件的 TUN/虚拟网卡模式（改用系统 HTTP 代理），或将国内政务/厂商域名加入直连规则后重新自检。",
            "details": {"host": host, "ips": ips, "fake_ips": fake_ips, "private_ips": private_ips},
        }

    return {
        "id": "network_dns_guard",
        "name": "外网域名解析与代理拦截检查",
        "category": "network",
        "status": "ok",
        "message": f"公网域名解析正常（{host} -> {', '.join(ips[:2])}），不会被私有网络防护拦截。",
        "degraded_impact": "",
        "remediation": "",
        "details": {"host": host, "ips": ips[:4]},
    }


def _sqlite_row_count(path: Path | None, table: str) -> int | None:
    if path is None or not Path(path).is_file():
        return None
    import sqlite3
    try:
        conn = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)
        try:
            row = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()
            return int(row[0]) if row else 0
        finally:
            conn.close()
    except sqlite3.Error:
        return None


def collect_startup_diagnostics(
    repo,
    *,
    storage_dir: Path,
    search_service: Any = None,
    runtime_provider_client: Any = None,
    runtime_provider_issue: dict[str, Any] | None = None,
    runtime_vision_adapter: Any = None,
    runtime_vision_verified: bool = False,
    xlyg_path: Path | None = None,
    price_memory_path: Path | None = None,
    knowledge_index: Any = None,
    check_dns: bool = True,
) -> dict[str, Any]:
    """Inspect runtime keys, network DNS, browser/PDF/Office dependencies, and SQL stores."""
    items: list[dict[str, Any]] = []

    # 1. Reasoning model
    has_key = runtime_provider_client is not None or bool(os.getenv("DEEPSEEK_API_KEY"))
    payment_issue = isinstance(runtime_provider_issue, dict) and runtime_provider_issue.get("code") == "provider_payment_required"
    if payment_issue:
        items.append({
            "id": "reasoning_model",
            "name": "思考模型（DeepSeek）",
            "category": "model",
            "status": "error",
            "message": "账号余额不足，模型调用已被接口拒绝（HTTP 402）。",
            "degraded_impact": "文档结构化解析、参数抽取、调研汇总与报告生成均无法完成。",
            "remediation": "请充值或更换有效的 API Key 后，在连接设置页测试思考模型连通性。",
        })
    elif not has_key:
        items.append({
            "id": "reasoning_model",
            "name": "思考模型（DeepSeek）",
            "category": "model",
            "status": "error",
            "message": "未检测到 DEEPSEEK_API_KEY 或运行时模型配置。",
            "degraded_impact": "上传资料后无法执行智能分析，采购研究只能跑空壳或停止。",
            "remediation": "在“连接设置”填入 API Key，或在 backend/.env 配置 DEEPSEEK_API_KEY。",
        })
    else:
        items.append({
            "id": "reasoning_model",
            "name": "思考模型（DeepSeek）",
            "category": "model",
            "status": "ok",
            "message": "思考模型凭据已配置，可用于文档分析与研究汇总。",
            "degraded_impact": "",
            "remediation": "",
        })

    # 2. Vision OCR
    if runtime_vision_adapter is None:
        items.append({
            "id": "vision_ocr",
            "name": "多模态视觉识别（OCR）",
            "category": "model",
            "status": "warning",
            "message": "当前未挂载视觉识别适配器（通常随思考模型 API 一并配置）。",
            "degraded_impact": "纯扫描版 PDF 或图片报价单将无法识图提取文字。",
            "remediation": "先完成思考模型配置，并在连接设置点击“测试视觉识别”。",
        })
    elif not runtime_vision_verified:
        items.append({
            "id": "vision_ocr",
            "name": "多模态视觉识别（OCR）",
            "category": "model",
            "status": "ok",
            "message": "视觉识别适配器已就绪（本进程尚未触发真实图片探针，首次识图将自动验证）。",
            "degraded_impact": "",
            "remediation": "",
        })
    else:
        items.append({
            "id": "vision_ocr",
            "name": "多模态视觉识别（OCR）",
            "category": "model",
            "status": "ok",
            "message": "已通过真实图片连通性探针验证。",
            "degraded_impact": "",
            "remediation": "",
        })

    # 3. Search channels
    providers = list(getattr(search_service, "providers", []) or [])
    active_names: list[str] = []
    quarantined_names: list[str] = []
    for p in providers:
        name = str(getattr(p, "name", "") or type(p).__name__)
        reason = channel_quarantine_reason(name)
        if reason:
            quarantined_names.append(f"{name}({reason})")
            continue
        active_names.append(name)
    if not active_names:
        items.append({
            "id": "search_channels",
            "name": "国内与政采检索通道",
            "category": "search",
            "status": "error",
            "message": "当前没有任何可用的检索通道（可能全部被隔离或未配置）。",
            "degraded_impact": "在线核价与替代品牌检索将完全找不到外部来源。",
            "remediation": "检查联网渠道设置或重启服务解除异常通道隔离，并确保至少启用 CCGP/百度/DeepSeek 之一。",
            "details": {"active": active_names, "quarantined": quarantined_names},
        })
    elif quarantined_names:
        items.append({
            "id": "search_channels",
            "name": "国内与政采检索通道",
            "category": "search",
            "status": "warning",
            "message": f"可用通道：{', '.join(active_names)}；已被熔断隔离：{', '.join(quarantined_names)}。",
            "degraded_impact": "被隔离通道在本进程后续任务中会自动跳过，可能减少检索召回来源。",
            "remediation": "确认相关通道配额或 Key 正常后重新检查连接。",
            "details": {"active": active_names, "quarantined": quarantined_names},
        })
    else:
        items.append({
            "id": "search_channels",
            "name": "国内与政采检索通道",
            "category": "search",
            "status": "ok",
            "message": f"已挂载 {len(active_names)} 个可用国内通道：{', '.join(active_names)}。",
            "degraded_impact": "",
            "remediation": "",
            "details": {"active": active_names, "quarantined": quarantined_names},
        })

    # 4. Network DNS / TUN guard
    if check_dns and os.getenv("LAB_DIAGNOSTICS_SKIP_DNS", "0") != "1":
        items.append(_check_dns_target("www.ccgp.gov.cn"))
    else:
        items.append({
            "id": "network_dns_guard",
            "name": "外网域名解析与代理拦截检查",
            "category": "network",
            "status": "ok",
            "message": "已按当前检查参数跳过实时外网 DNS 探测。",
            "degraded_impact": "",
            "remediation": "",
        })

    # 5. Browser driver (Playwright)
    has_pw = importlib.util.find_spec("playwright") is not None
    browser_flag = os.getenv("LAB_BROWSER_READING") == "1"
    if not has_pw:
        items.append({
            "id": "browser_driver",
            "name": "浏览器动态渲染组件（Playwright）",
            "category": "runtime",
            "status": "warning",
            "message": "未检测到 Playwright 包。",
            "degraded_impact": "依赖 JS 动态加载列表/附件下载链接的政采网站与厂家页只能退回静态 HTML 解析。",
            "remediation": "在虚拟环境安装 playwright 并执行 playwright install chromium。",
        })
    else:
        items.append({
            "id": "browser_driver",
            "name": "浏览器动态渲染组件（Playwright）",
            "category": "runtime",
            "status": "ok",
            "message": f"Playwright 已安装（LAB_BROWSER_READING={'已强制开启' if browser_flag else '按需触发'}）。",
            "degraded_impact": "",
            "remediation": "",
        })

    # 6. PDF parser
    has_fitz = importlib.util.find_spec("fitz") is not None
    has_plumber = importlib.util.find_spec("pdfplumber") is not None
    if not (has_fitz and has_plumber):
        missing_pdf = [n for n, ok in (("PyMuPDF/fitz", has_fitz), ("pdfplumber", has_plumber)) if not ok]
        items.append({
            "id": "pdf_parser",
            "name": "PDF 解析依赖",
            "category": "runtime",
            "status": "warning" if (has_fitz or has_plumber) else "error",
            "message": f"缺少 PDF 解析库：{', '.join(missing_pdf)}。",
            "degraded_impact": "PDF 页渲染预览、文字层抽取或表格解析将失败或降级。",
            "remediation": "安装缺失的 Python 包：pip install pymupdf pdfplumber。",
        })
    else:
        items.append({
            "id": "pdf_parser",
            "name": "PDF 解析依赖",
            "category": "runtime",
            "status": "ok",
            "message": "PyMuPDF (fitz) 与 pdfplumber 均已就绪。",
            "degraded_impact": "",
            "remediation": "",
        })

    # 7. Office Word/Excel export
    has_docx = importlib.util.find_spec("docx") is not None
    has_openpyxl = importlib.util.find_spec("openpyxl") is not None
    if not (has_docx and has_openpyxl):
        missing_off = [n for n, ok in (("python-docx", has_docx), ("openpyxl", has_openpyxl)) if not ok]
        items.append({
            "id": "office_export",
            "name": "Word / Excel 比选导出组件",
            "category": "runtime",
            "status": "warning",
            "message": f"缺少办公文档生成库：{', '.join(missing_off)}。",
            "degraded_impact": "标准 Word (.docx) 或 Excel (.xlsx) 导出接口将不可用。",
            "remediation": "安装缺失依赖：pip install python-docx openpyxl。",
        })
    else:
        items.append({
            "id": "office_export",
            "name": "Word / Excel 比选导出组件",
            "category": "runtime",
            "status": "ok",
            "message": "python-docx 与 openpyxl 均已安装，可直接导出标准 Word 与 Excel。",
            "degraded_impact": "",
            "remediation": "",
        })

    # 8. Local SQLite stores & storage dir
    db_path = Path(repo.path)
    storage_ok = False
    try:
        storage_dir.mkdir(parents=True, exist_ok=True)
        probe_file = storage_dir / ".diag_write_probe"
        probe_file.write_text("ok", encoding="utf-8")
        probe_file.unlink(missing_ok=True)
        storage_ok = True
    except OSError:
        storage_ok = False

    pm_path = price_memory_path or (db_path.parent / "price_memory.sqlite3")
    pm_count = _sqlite_row_count(pm_path, "price_memory")
    xlyg_count = _sqlite_row_count(xlyg_path, "xlyg_item") if xlyg_path else None
    k_summary = knowledge_index.summary() if knowledge_index is not None else {}

    if not storage_ok:
        items.append({
            "id": "local_storage",
            "name": "本地数据库与文件目录",
            "category": "storage",
            "status": "error",
            "message": f"附件存储目录不可写：{storage_dir}。",
            "degraded_impact": "上传产品资料与保存网页快照都会报错失败。",
            "remediation": "检查目录权限及磁盘可用空间。",
        })
    else:
        parts = [
            f"主库任务 {len(repo.list_tasks())} 个",
            f"知识库报价 {k_summary.get('quotes', 0)} 条 / 去重资料 {k_summary.get('documents', 0)} 份",
        ]
        if pm_count is not None:
            parts.append(f"价格记忆 {pm_count} 条")
        if xlyg_count is not None:
            parts.append(f"小零易购本地库 {xlyg_count} 条")
        items.append({
            "id": "local_storage",
            "name": "本地数据库与文件目录",
            "category": "storage",
            "status": "ok",
            "message": "本地 SQLite 与文件目录读写正常（" + "，".join(parts) + "）。",
            "degraded_impact": "",
            "remediation": "",
            "details": {
                "app_db": str(db_path),
                "storage_dir": str(storage_dir),
                "knowledge_quotes": k_summary.get("quotes", 0),
                "knowledge_documents": k_summary.get("documents", 0),
                "price_memory_rows": pm_count,
                "xlyg_rows": xlyg_count,
            },
        })

    errors = [item for item in items if item["status"] == "error"]
    warnings = [item for item in items if item["status"] == "warning"]
    overall = "broken" if errors else ("degraded" if warnings else "healthy")
    if overall == "healthy":
        summary = "所有核心组件、网络解析与本地库均正常，可直接运行完整采购研究。"
    elif overall == "broken":
        summary = f"发现 {len(errors)} 项关键阻断错误（另 {len(warnings)} 项提醒），直接开始研究会导致部分环节失败或拿不到报价。"
    else:
        summary = f"核心功能可运行，但存在 {len(warnings)} 项降级提醒，可能影响部分附件识图或特定站点。"

    return {
        "overall_status": overall,
        "summary": summary,
        "error_count": len(errors),
        "warning_count": len(warnings),
        "silent_degradation_risks": [
            item["degraded_impact"]
            for item in items
            if item["status"] in {"error", "warning"} and item.get("degraded_impact")
        ],
        "items": items,
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
