"""Provider status and live health-check endpoints."""
from __future__ import annotations

import os
from dataclasses import replace
from typing import Any

from fastapi import APIRouter, HTTPException

from backend.api.routes.context import RouteContext
from backend.api.routes.schemas import ProviderHealthRequest
from backend.domain.search import SearchQuery
from backend.infrastructure.deepseek import DeepSeekClient, DeepSeekError
from backend.infrastructure.search_providers import (
    BaiduSearchProvider,
    CCGPSearchProvider,
    DeepSeekGroundedSearchProvider,
    ProviderConfigurationError,
)
from backend.services.runtime_diagnostics import collect_startup_diagnostics
from backend.services.vision import VisionAdapterError


def register_provider_routes(router: APIRouter, ctx: RouteContext) -> None:
    @router.get("/system/diagnostics")
    def system_diagnostics(strict: bool = False, check_dns: bool = True):
        try:
            ctx.knowledge_index.ensure_initialized(ctx.repo)
        except Exception:
            pass
        report = collect_startup_diagnostics(
            ctx.repo,
            storage_dir=ctx.storage_dir,
            search_service=ctx.search,
            runtime_provider_client=ctx.runtime_provider_client,
            runtime_provider_issue=ctx.runtime_provider_issue,
            runtime_vision_adapter=ctx.runtime_vision_adapter(),
            runtime_vision_verified=ctx.runtime_vision_verified,
            xlyg_path=ctx.xlyg_database_path,
            knowledge_index=ctx.knowledge_index,
            check_dns=check_dns,
        )
        if strict and report["overall_status"] == "broken":
            raise HTTPException(503, detail=report)
        return report

    @router.get("/providers")
    def list_providers():
        model_available = ctx.runtime_provider_client is not None or bool(os.getenv("DEEPSEEK_API_KEY"))
        payment_required = (
            ctx.runtime_provider_issue is not None
            and ctx.runtime_provider_issue.get("code") == "provider_payment_required"
        )
        model_ready = model_available and ctx.runtime_provider_issue is None
        vision_available = ctx.runtime_vision_adapter() is not None
        vision_ready = vision_available and ctx.runtime_vision_verified
        any_ready = model_ready or ctx.runtime_search_enabled or vision_available
        reasoning_status = "payment_required" if payment_required else ("configured" if model_ready else "unavailable")
        reasoning_label = "余额不足" if payment_required else "思考模型"
        vision_status = (
            "payment_required" if payment_required
            else "configured" if vision_ready
            else "ready_to_test" if vision_available
            else "unavailable"
        )
        vision_verified = bool(vision_ready)
        vision_label = (
            "余额不足" if payment_required
            else "视觉识别（已通过图片验证）" if vision_ready
            else "视觉识别（已配置，待验证）" if vision_available
            else "视觉识别（未配置）"
        )
        vision_detail = (
            "余额不足，视觉识别暂不可用" if payment_required
            else "已通过真实图片读取验证，可直接识别图片内容。" if vision_ready
            else "多模态接口已配置，但本进程尚未通过真实图片读取验证；按钮可用，首次识别会真正调用模型。" if vision_available
            else "未配置多模态接口，无法识别图片内容。"
        )
        return {
            "status": "payment_required" if payment_required else ("configured" if any_ready else "unavailable"),
            "label": "DeepSeek 余额不足" if payment_required else ("已连接" if model_ready and ctx.runtime_search_enabled else ("部分已连接" if any_ready else "服务未连接")),
            "issue": ctx.runtime_provider_issue,
            "items": [
                {"id": "reasoning", "status": reasoning_status, "label": reasoning_label},
                {"id": "search", "status": "configured" if ctx.runtime_search_enabled else "ready_to_test", "label": "联网能力", "modes": ctx.runtime_search_modes},
                {
                    "id": "vision",
                    "status": vision_status,
                    "label": vision_label,
                    "verified": vision_verified,
                    "adapter_available": vision_available,
                    "detail": vision_detail,
                },
            ],
        }

    @router.post("/providers/{provider_id}/health-check")
    def provider_health_check(provider_id: str, body: ProviderHealthRequest = ProviderHealthRequest()):
        if provider_id not in {"reasoning", "vision", "search"}:
            raise HTTPException(404, detail={"code": "provider_not_found", "message": "连接方式不存在", "field": "provider_id"})
        attempted_client: DeepSeekClient | None = None
        try:
            if provider_id == "reasoning":
                client = ctx.provider_client(body)
                attempted_client = client
                replace(client, max_tokens=32, json_retries=0).analyze(
                    system_prompt='只返回 JSON 对象 {"ok": true}。',
                    user_prompt="连接测试，不要执行采购分析。",
                )
                ctx.runtime_provider_client = client
                ctx.runtime_provider_issue = None
                ctx.runtime_vision_verified = False
                return {"status": "configured", "label": "已连接", "vision_status": "ready_to_test"}
            if provider_id == "vision":
                client = ctx.runtime_provider_client
                if client is None or any((body.api_key, body.base_url, body.model)):
                    client = ctx.provider_client(body)
                attempted_client = client
                adapter = ctx.multimodal_adapter
                if adapter is None:
                    adapter = ctx.runtime_vision_from_model_client(client)
                probe = getattr(adapter, "probe", None)
                if not callable(probe):
                    raise VisionAdapterError("vision_probe_unsupported", "当前视觉适配器不支持图片连通探针")
                probe()
                ctx.runtime_provider_client = client
                ctx.runtime_provider_issue = None
                ctx.runtime_vision_verified = True
                return {"status": "configured", "label": "已通过真实图片验证"}
            requested_modes = list(dict.fromkeys(body.modes or ["model"]))
            allowed_modes = {"model", "baidu", "ccgp", "cn"}
            invalid_modes = [mode for mode in requested_modes if mode not in allowed_modes]
            if invalid_modes:
                raise HTTPException(422, detail={"code": "invalid_search_mode", "message": "包含不支持的联网来源", "field": "modes"})
            query = SearchQuery("实验室仪器 官方资料", limit=1, purpose="official")
            active_providers: list[Any] = []
            active_modes: list[str] = []
            warnings: list[str] = []
            for mode in requested_modes:
                try:
                    if mode == "model":
                        client = ctx.provider_client(body)
                        rows = client.search_web(query=query.text, purpose=query.purpose, limit=1)
                        if not rows:
                            raise ProviderConfigurationError("模型联网未返回来源")
                        ctx.runtime_provider_client = client
                        active_providers.append(DeepSeekGroundedSearchProvider(client))
                    elif mode in {"baidu", "cn"}:
                        provider = BaiduSearchProvider()
                        if not provider.search(query):
                            raise ProviderConfigurationError("百度中文网页未返回结果")
                        active_providers.append(provider)
                    else:
                        provider = CCGPSearchProvider()
                        if not provider.search(SearchQuery("实验室仪器 中标", limit=1, purpose="tender")):
                            raise ProviderConfigurationError("中国政府采购网未返回结果")
                        active_providers.append(provider)
                    active_modes.append(mode)
                except (DeepSeekError, ProviderConfigurationError, OSError, ValueError) as exc:
                    if len(requested_modes) == 1 and isinstance(exc, DeepSeekError):
                        raise
                    warnings.append(f"{mode}: {exc}")
            if not active_providers:
                ctx.runtime_search_enabled = False
                ctx.runtime_search_modes = []
                ctx.replace_runtime_search([])
                return {"status": "unavailable", "label": "所选来源均未连接", "active_modes": [], "warnings": warnings}
            ctx.runtime_search_enabled = True
            ctx.runtime_search_modes = active_modes
            ctx.replace_runtime_search(active_providers)
            return {"status": "configured", "label": "已连接", "active_modes": active_modes, "warnings": warnings}
        except DeepSeekError as exc:
            if exc.code == "provider_payment_required" and attempted_client is not None:
                ctx.runtime_provider_client = attempted_client
                ctx.runtime_provider_issue = {"code": exc.code, "message": exc.message}
                ctx.runtime_vision_verified = False
            status = 402 if exc.code == "provider_payment_required" else (503 if exc.code in {"provider_not_configured", "provider_timeout", "provider_unavailable", "provider_http_error"} else 502)
            raise HTTPException(status, detail={"code": exc.code, "message": exc.message}) from exc
        except VisionAdapterError as exc:
            ctx.runtime_vision_verified = False
            status = 503 if exc.code in {"vision_provider_timeout", "vision_provider_unavailable"} else 502
            detail = {"code": exc.code, "message": exc.message}
            if exc.field:
                detail["field"] = exc.field
            raise HTTPException(status, detail=detail) from exc
