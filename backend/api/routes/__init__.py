"""Modular workflow API router entry point (keeps ``build_router`` compatible)."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter

from backend.api.routes.analysis import register_analysis_routes
from backend.api.routes.context import RouteContext, remove_task_owned_files
from backend.api.routes.documents import register_document_routes
from backend.api.routes.knowledge import register_knowledge_routes
from backend.api.routes.providers import register_provider_routes
from backend.api.routes.reports import register_report_routes
from backend.api.routes.search_candidates import register_search_candidate_routes
from backend.api.routes.tasks import register_task_routes
from backend.infrastructure.deepseek import DeepSeekClient
from backend.repositories.sqlite import SQLiteRepository
from backend.services.multimodal_ocr import MultimodalOCRAdapter
from backend.services.official_search import OfficialSearchService


def build_router(
    repo: SQLiteRepository,
    storage_dir: Path,
    search_service: OfficialSearchService | None = None,
    multimodal_adapter: MultimodalOCRAdapter | None = None,
    manage_search_service: bool = False,
    xlyg_database_path: Path | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["procurement"])
    ctx = RouteContext(
        repo=repo,
        storage_dir=storage_dir,
        search_service=search_service,
        multimodal_adapter=multimodal_adapter,
        manage_search_service=manage_search_service,
        xlyg_database_path=xlyg_database_path,
    )
    register_provider_routes(router, ctx)
    register_report_routes(router, ctx)
    register_task_routes(router, ctx)
    register_document_routes(router, ctx)
    register_analysis_routes(router, ctx)
    register_search_candidate_routes(router, ctx)
    register_knowledge_routes(router, ctx)
    router.reasoning_client = ctx.reasoning_client
    router.knowledge_index = ctx.knowledge_index
    router.route_context = ctx
    return router


__all__ = ["build_router", "DeepSeekClient", "remove_task_owned_files"]
