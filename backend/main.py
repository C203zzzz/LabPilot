"""Application entry point.

This module owns app construction only; business decisions remain in domain
objects and services so M01-M03 can continue to evolve independently.
"""
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from backend.domain.errors import ModuleValidationError
from backend.services.vision import VisionAdapterError

from backend.api.health import router as health_router
from backend.infrastructure.config import settings
from backend.repositories.sqlite import SQLiteRepository
from backend.api.routes import build_router


def create_app(*, database_path=None, storage_dir=None, cors_origins=None, search_service=None, multimodal_adapter=None, xlyg_database_path=None) -> FastAPI:
    # ``multimodal_adapter`` is a caller/test injection point only.  The product
    # reads images with the same thinking-model API the user configured in the
    # browser, and ``build_router`` resolves that per request; there is no
    # separate vision endpoint to fall back to.
    #
    # Normalise caller-provided strings as well as ``Path`` instances.  The
    # HTTP upload route performs path composition, so accepting a string here
    # must not turn a normal Uvicorn deployment into a 500 on first upload.
    database_path = Path(database_path or settings.database_path)
    storage_dir = Path(storage_dir or settings.storage_dir)
    repo = SQLiteRepository(database_path)

    from backend.api.procurement import procurement_router
    from backend.services.official_search import OfficialSearchService
    from backend.infrastructure.search_providers import CCGPSearchProvider, BochaSearchProvider, ExaMCPSearchProvider, TavilySearchProvider, PublicWebSearchProvider, DeepSeekGroundedSearchProvider
    # ``BochaSearchProvider.from_env()`` sits last and is ``stop_on_results=False``:
    # the five channels above keep their order, their failure vocabulary and the
    # number of warnings a round produces. Without ``BOCHA_API_KEY`` it returns
    # None, so an unconfigured deployment builds the exact same chain as before.
    research_search = search_service or OfficialSearchService(providers=[p for p in [CCGPSearchProvider(), ExaMCPSearchProvider.from_env(), TavilySearchProvider.from_env(), DeepSeekGroundedSearchProvider.from_env(), PublicWebSearchProvider(), BochaSearchProvider.from_env()] if p is not None])
    # Only the chain this module builds gets the switch: the reader *is* the
    # body-fetch transport, so attaching one to a caller-supplied service would
    # silently change that service's failure vocabulary (body_status) and its
    # tests. A caller who injects a service owns its transport.
    if search_service is None:
        import os
        from backend.infrastructure.page_reader import PublicPageReader
        research_search.reader = PublicPageReader(browser=os.getenv('LAB_BROWSER_READING') == '1')
    research_router, research_service = procurement_router(repo, research_search)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            yield
        finally:
            research_service.close()
            repo.close()

    app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins if cors_origins is None else cors_origins),
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "OPTIONS"],
        allow_headers=["Accept", "Content-Type"],
    )
    @app.exception_handler(ModuleValidationError)
    async def validation_error(_request: Request, exc: ModuleValidationError):
        return JSONResponse(status_code=422, content={"error": exc.to_dict()})

    @app.exception_handler(VisionAdapterError)
    async def vision_configuration_error(_request: Request, exc: VisionAdapterError):
        """配置类错误必须让浏览器读得到，而不是判成"网络失败"。

        ``LAB_VISION_JSON_RETRIES`` / ``LAB_VISION_TIMEOUT`` 写错时，异常从路由里
        抛出。不在这里接住的话，它会一路冒到 Starlette 最外层的
        ``ServerErrorMiddleware``：那一层生成的裸 500 不经过 ``CORSMiddleware``，
        缺 ``Access-Control-Allow-Origin``，浏览器因此读不到响应、只能显示
        "服务未连接"——把"配置写错了"讲成了"连不上"。走异常处理器得到的响应会
        穿过 CORS 中间件，前端才能拿到带 ``code`` 与中文原话的 502。状态码与
        ``POST /providers/{id}/health-check`` 对同一个配置错误的口径一致。
        """

        detail = {"code": exc.code, "message": exc.message}
        if exc.field:
            detail["field"] = exc.field
        return JSONResponse(status_code=502, content={"detail": detail})

    app.include_router(health_router)
    from backend.api.runtime_usage import router as runtime_usage_router
    # 只读的额度用量接口（``GET /api/v1/runtime/model-budget``）：用户在点"开始研究"
    # 或"分析资料"之前就能看到本小时还剩多少次，而不是跑到一半被挡。
    app.include_router(runtime_usage_router)
    app.include_router(research_router)
    from backend.api.xlyg_prices import xlyg_router
    # 只读的本地成交明细库（scripts/xlyg_build_db.py 生成）。库不在时端点是 503 而不是
    # 空列表——"没建库"和"这个型号没有成交"必须能分开。
    app.include_router(xlyg_router(xlyg_database_path))
    app.state.research_service = research_service
    app.state.repository = repo
    # Reuse the same provider chain for workflow and procurement research.
    # Building a second default chain here silently dropped runtime providers
    # such as Exa MCP and DeepSeek from the task search endpoint.
    workflow_router = build_router(repo, storage_dir, search_service=research_search, multimodal_adapter=multimodal_adapter, manage_search_service=search_service is None)
    research_service.client_factory = workflow_router.reasoning_client
    app.include_router(workflow_router)
    return app


def __getattr__(name):
    # Importing create_app for tests/isolated tools must not open the user's DB
    # or mark live research interrupted. Uvicorn resolves app on demand.
    if name == 'app':
        app = create_app()
        globals()['app'] = app
        return app
    raise AttributeError(name)


if __name__ == "__main__":  # pragma: no cover - exercised by uvicorn
    import uvicorn

    uvicorn.run("backend.main:app", host="127.0.0.1", port=8000, reload=False)
