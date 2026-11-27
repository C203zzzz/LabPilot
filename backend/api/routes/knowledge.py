"""Cross-task pure-SQL knowledge index and supplier price comparison endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Query

from backend.api.routes.context import RouteContext


def _ensure_synced(ctx: RouteContext) -> None:
    try:
        ctx.knowledge_index.ensure_initialized(ctx.repo)
    except Exception:
        pass


def register_knowledge_routes(router: APIRouter, ctx: RouteContext) -> None:
    @router.get("/knowledge/summary")
    def knowledge_summary():
        _ensure_synced(ctx)
        return ctx.knowledge_index.summary()

    @router.post("/knowledge/reindex")
    def knowledge_reindex():
        synced = ctx.knowledge_index.sync_from_repository(ctx.repo)
        return {"synced": synced, "summary": ctx.knowledge_index.summary()}

    @router.get("/knowledge/quotes")
    def knowledge_quotes(
        model: str = "",
        brand: str = "",
        category: str = "",
        supplier: str = "",
        keyword: str = "",
        date_from: str = "",
        date_to: str = "",
        exact_model: bool = False,
        include_xlyg: bool = False,
        limit: int = Query(200, ge=1, le=500),
    ):
        _ensure_synced(ctx)
        return ctx.knowledge_index.search_quotes(
            model=model,
            brand=brand,
            category=category,
            supplier=supplier,
            keyword=keyword,
            date_from=date_from,
            date_to=date_to,
            exact_model=exact_model,
            include_xlyg=include_xlyg,
            xlyg_path=ctx.xlyg_database_path,
            limit=limit,
        )

    @router.get("/knowledge/compare")
    def knowledge_compare(
        model: str = "",
        brand: str = "",
        category: str = "",
        supplier: str = "",
        keyword: str = "",
        date_from: str = "",
        date_to: str = "",
        exact_model: bool = False,
        include_xlyg: bool = False,
        limit: int = Query(200, ge=1, le=1000),
    ):
        _ensure_synced(ctx)
        return ctx.knowledge_index.compare_suppliers(
            model=model,
            brand=brand,
            category=category,
            supplier=supplier,
            keyword=keyword,
            date_from=date_from,
            date_to=date_to,
            exact_model=exact_model,
            include_xlyg=include_xlyg,
            xlyg_path=ctx.xlyg_database_path,
            limit=limit,
        )

    @router.get("/knowledge/documents")
    def knowledge_documents(
        model: str = "",
        brand: str = "",
        keyword: str = "",
        limit: int = Query(50, ge=1, le=300),
    ):
        _ensure_synced(ctx)
        return ctx.knowledge_index.search_documents(
            model=model,
            brand=brand,
            keyword=keyword,
            limit=limit,
        )
