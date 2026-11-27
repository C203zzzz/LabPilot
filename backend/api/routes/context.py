"""Shared runtime context and pure helper utilities for workflow API routes."""
from __future__ import annotations

import os
import re
import shutil
from datetime import datetime, timezone
from hashlib import sha1
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from backend.api.routes.schemas import ProviderHealthRequest
from backend.domain.candidates import CandidateSource, CandidateStatus, ProductCandidate
from backend.domain.facts import FactEvidence, FactEvidenceLevel, FactStatus, FactValueType
from backend.infrastructure.deepseek import DeepSeekClient, DeepSeekError
from backend.infrastructure.search_providers import (
    BaiduSearchProvider,
    CCGPSearchProvider,
    DeepSeekGroundedSearchProvider,
    PublicWebSearchProvider,
)
from backend.repositories.sqlite import SQLiteRepository
from backend.schemas.documents import (
    DocumentImage,
    DocumentPage,
    DocumentRecord,
    DocumentTable,
    ModelExtraction,
    ParseWarning,
)
from backend.services.document_ingestion import DocumentIngestionService
from backend.services.fact_service import confirm_fact, make_fact
from backend.services.history_service import TaskHistoryService
from backend.services.knowledge_index import SQLKnowledgeIndex
from backend.services.multimodal_ocr import MultimodalOCRAdapter
from backend.services.official_search import OfficialSearchService
from backend.services.requirement_service import RequirementService
from backend.services.vision import OpenAICompatibleVisionAdapter, vision_json_retries, vision_timeout


def remove_task_owned_files(storage_dir: Path, documents: list[dict]) -> list[str]:
    """Remove only the stored files that provably belong to the deleted tasks.

    Uploaded originals (``originals/<sha256>``) and captured source bytes
    (``research-snapshots/<sha256>.bin``) are content-addressed: the same bytes
    uploaded or fetched by another task resolve to the same file, so neither
    store is touched here.  What is removed is the per-document OCR/vision
    intermediate directory and the transient copy the upload route left under
    ``incoming/``, both of which the document record points at.
    """
    root = Path(storage_dir)
    incoming = (root / "incoming").resolve()
    removed: list[str] = []
    for document in documents:
        if not isinstance(document, dict):
            continue
        document_id = str(document.get("id") or "")
        if re.fullmatch(r"[A-Za-z0-9._-]{1,64}", document_id):
            directory = root / "visual_inputs" / document_id
            if directory.is_dir():
                shutil.rmtree(directory, ignore_errors=True)
                if not directory.exists():
                    removed.append(str(directory))
        source_uri = str(document.get("source_uri") or "")
        if not source_uri:
            continue
        try:
            candidate = Path(source_uri).resolve()
        except OSError:
            continue
        if candidate.parent == incoming and candidate.is_file():
            try:
                candidate.unlink()
            except OSError:
                continue
            removed.append(str(candidate))
    return removed


def document_record(raw: dict[str, Any]) -> DocumentRecord:
    return DocumentRecord(
        id=raw["id"],
        source_name=raw["source_name"],
        source_uri=raw["source_uri"],
        document_type=raw["document_type"],
        media_type=raw.get("media_type"),
        sha256=raw.get("sha256"),
        size_bytes=raw.get("size_bytes"),
        original_path=raw.get("original_path"),
        category=raw.get("category", "other"),
        pages=[DocumentPage(**page) for page in raw.get("pages", [])],
        tables=[DocumentTable(**table) for table in raw.get("tables", [])],
        images=[DocumentImage(**image) for image in raw.get("images", [])],
        ocr_status=raw.get("ocr_status", "not_requested"),
        warnings=[ParseWarning(**warning) for warning in raw.get("warnings", [])],
        extracted_text=raw.get("extracted_text", ""),
        operation_timings=raw.get("operation_timings", []),
        model_extraction=ModelExtraction(**raw.get("model_extraction", {})),
    )


def candidate_from_payload(raw: dict[str, Any]) -> ProductCandidate:
    sources = [CandidateSource(**source) for source in raw.get("sources", [])]
    return ProductCandidate(
        brand=raw.get("brand"),
        series=raw.get("series"),
        model=raw["model"],
        configuration=raw.get("configuration"),
        sources=sources,
        status=CandidateStatus(raw.get("status", CandidateStatus.PENDING)),
        id=raw["id"],
        note=raw.get("note"),
        created_at=raw.get("created_at"),
    )


def analysis_fact(
    candidate: dict[str, Any],
    field: dict[str, Any],
    *,
    reviewed_by: str,
    review_note: str | None = None,
):
    candidate_id = str(candidate["candidate_id"])
    evidences = []
    for source in field.get("sources", []) or candidate.get("sources", []):
        document_id = str(source.get("document_id") or "unknown")
        raw_location = source.get("page_or_location") or source.get("location") or source.get("page") or "unknown"
        location = str(raw_location)
        page = None
        page_match = re.search(r"(?:page|页)\s*[:：]?\s*(\d+)", location, flags=re.I)
        if page_match:
            page = int(page_match.group(1))
        evidence_key = sha1(f"{candidate_id}|{field.get('field_name')}|{document_id}|{location}".encode("utf-8")).hexdigest()[:16]
        evidences.append(FactEvidence(
            evidence_id=f"evidence-{evidence_key}",
            candidate_id=candidate_id,
            source_type="user_document",
            authority_score=80,
            evidence_level=FactEvidenceLevel.USER_PROVIDED,
            extracted_at=datetime.now(timezone.utc),
            document_id=document_id,
            page=page,
            location=location,
            quote=str(source.get("excerpt") or "")[:2000],
        ))
    if not evidences:
        return None
    value = field.get("value")
    name = str(field.get("field_name") or "").strip()
    if not name or value is None or (isinstance(value, str) and not value.strip()):
        return None
    if any(token in name.casefold() for token in ("price", "amount", "报价", "价格", "金额")):
        value_type = FactValueType.PRICE
    elif isinstance(value, bool):
        value_type = FactValueType.BOOLEAN
    elif isinstance(value, (int, float)):
        value_type = FactValueType.NUMBER
    elif isinstance(value, (dict, list)):
        value_type = FactValueType.JSON
    else:
        value_type = FactValueType.TEXT
    draft = make_fact(candidate_id, name, value, value_type, evidences, unit=field.get("unit"), status=FactStatus.PENDING_REVIEW)
    return confirm_fact(draft, reviewed_by, note=review_note)


def deepseek_client_cls() -> type[DeepSeekClient]:
    """Resolve ``DeepSeekClient`` via the package namespace so test monkeypatches apply."""
    import backend.api.routes as routes_pkg
    return routes_pkg.DeepSeekClient


class RouteContext:
    """Mutable per-router state and shared helpers injected into route groups."""

    def __init__(
        self,
        repo: SQLiteRepository,
        storage_dir: Path,
        search_service: OfficialSearchService | None = None,
        multimodal_adapter: MultimodalOCRAdapter | None = None,
        manage_search_service: bool = False,
        xlyg_database_path: str | Path | None = None,
    ) -> None:
        self.repo = repo
        self.storage_dir = storage_dir
        self.multimodal_adapter = multimodal_adapter
        self.requirements = RequirementService()
        self.ingestion = DocumentIngestionService(storage_dir, vision_adapter=multimodal_adapter)
        self.history_service = TaskHistoryService(repo)
        self.knowledge_index = SQLKnowledgeIndex(Path(repo.path).parent / "knowledge_index.sqlite3")
        from backend.api.xlyg_prices import default_database_path
        self.xlyg_database_path = Path(xlyg_database_path) if xlyg_database_path is not None else default_database_path()
        self.runtime_grounded = DeepSeekGroundedSearchProvider.from_env()
        self.runtime_provider_client = deepseek_client_cls().from_env() if os.getenv("DEEPSEEK_API_KEY") else None
        self.runtime_search_enabled = True
        self.runtime_search_modes: list[str] = ["ccgp", "baidu"]
        self.runtime_vision_verified = False
        self.runtime_provider_issue: dict[str, str] | None = None
        self.owns_search_service = search_service is None or manage_search_service
        default_providers = [
            provider
            for provider in (
                CCGPSearchProvider(),
                BaiduSearchProvider(),
                DeepSeekGroundedSearchProvider.from_env(),
            )
            if provider is not None
        ]
        self.search = search_service or OfficialSearchService(providers=default_providers, fetch_body=True)

    def runtime_vision_from_model_client(self, client) -> OpenAICompatibleVisionAdapter:
        """Reuse the browser-configured model API for image reading."""
        return OpenAICompatibleVisionAdapter(
            api_key=client.api_key,
            endpoint=client.endpoint,
            model=client.model,
            timeout=max(client.timeout, vision_timeout()),
            json_retries=vision_json_retries(),
            provider="runtime-reasoning-model-vision",
        )

    def runtime_vision_adapter(self):
        """Return the injected adapter when present, otherwise the runtime model API."""
        if self.multimodal_adapter is not None:
            return self.multimodal_adapter
        if self.runtime_provider_client is None:
            return None
        return self.runtime_vision_from_model_client(self.runtime_provider_client)

    def task_or_404(self, task_id: str) -> dict[str, Any]:
        task = self.repo.get("tasks", task_id)
        if not task:
            raise HTTPException(404, detail={"code": "task_not_found", "message": "任务不存在", "field": "task_id"})
        return task

    def scoped_item_or_404(self, table: str, task_id: str, item_id: str, code: str) -> dict[str, Any]:
        item = self.repo.get(table, item_id)
        if not item or item.get("task_id") != task_id:
            raise HTTPException(404, detail={"code": code, "message": "记录不存在", "field": "id"})
        return item

    def workflow_state(self, task_id: str) -> dict[str, Any]:
        return self.repo.get("workflow_state", task_id) or {
            "evaluations": [],
            "ranking": None,
            "quotes": [],
            "comparison": None,
            "generated_questions": None,
        }

    def persist_workflow_state(self, task_id: str, **changes: Any) -> dict[str, Any]:
        state = self.workflow_state(task_id)
        state.update(changes)
        state["updated_at"] = datetime.now(timezone.utc).isoformat()
        self.repo.put("workflow_state", task_id, state)
        return state

    def provider_client(self, body: ProviderHealthRequest) -> DeepSeekClient:
        key = body.api_key or os.getenv("DEEPSEEK_API_KEY")
        if not key:
            raise DeepSeekError("provider_not_configured", "未配置运行时凭据")
        endpoint = (body.base_url or os.getenv("DEEPSEEK_ENDPOINT", "https://api.deepseek.com/chat/completions")).rstrip("/")
        if not endpoint.endswith("/chat/completions"):
            endpoint = f"{endpoint}/chat/completions"
        client_cls = deepseek_client_cls()
        return client_cls(
            api_key=key,
            endpoint=endpoint,
            model=body.model or os.getenv("DEEPSEEK_MODEL", "deepseek-flash"),
            web_tool_type=os.getenv("DEEPSEEK_WEB_TOOL_TYPE", "web_search"),
        )

    def activate_runtime_search(self, client: DeepSeekClient) -> None:
        if not self.owns_search_service:
            return
        self.search.providers = [p for p in self.search.providers if getattr(p, "name", "") != "deepseek_web"]
        self.search.providers.insert(0, DeepSeekGroundedSearchProvider(client))

    def replace_runtime_search(self, providers: list[Any]) -> None:
        if self.owns_search_service:
            selected = list(providers)
            if not any(hasattr(provider, "fetch") for provider in selected):
                fetcher = PublicWebSearchProvider()
                fetcher.supports = lambda _query: False  # type: ignore[method-assign]
                selected.append(fetcher)
            self.search.providers = selected

    def reasoning_client(self):
        return self.runtime_provider_client or deepseek_client_cls().from_env()

    def sync_knowledge_task(self, task_id: str) -> None:
        try:
            self.knowledge_index.sync_task(self.repo, task_id)
        except Exception:
            pass

    def delete_knowledge_tasks(self, task_ids: list[str]) -> None:
        for task_id in task_ids:
            try:
                self.knowledge_index.delete_task(task_id)
            except Exception:
                pass
