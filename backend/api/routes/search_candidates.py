"""Candidate management, web search, search plans, discovery, and source decisions."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from hashlib import sha1

from fastapi import APIRouter, HTTPException

from backend.api.routes.context import (
    RouteContext,
    candidate_from_payload,
    deepseek_client_cls,
    document_record,
)
from backend.api.routes.schemas import (
    CandidateBatchDecision,
    CandidateDecision,
    SearchPlanRequest,
    SearchRequest,
    SourceDecision,
    WebCandidateDiscoveryRequest,
)
from backend.domain.candidates import CandidateStatus, merge_candidates
from backend.domain.search import SearchQuery
from backend.infrastructure.deepseek import DeepSeekError
from backend.schemas.candidates import candidate_to_dict
from backend.schemas.search import result_to_dict, source_to_dict
from backend.services.candidate_service import (
    confirmed_candidates,
    decide_snapshot,
    extract_candidates,
    pages_from_document,
)
from backend.services.official_search import OfficialSearchService, queries_from_candidate
from backend.services.task_title import task_title
from backend.services.web_candidate_discovery import WebCandidateDiscoveryService


def register_search_candidate_routes(router: APIRouter, ctx: RouteContext) -> None:
    repo = ctx.repo

    @router.post("/tasks/{task_id}/candidates")
    def create_candidates(task_id: str):
        ctx.task_or_404(task_id)
        found = []
        for document in repo.list("documents", task_id):
            record = document_record(document)
            for candidate in extract_candidates(pages_from_document(record)):
                payload = candidate_to_dict(candidate)
                repo.put("candidates", candidate.id, payload, task_id=task_id)
                found.append(payload)
        return {"items": found}

    @router.post("/tasks/{task_id}/candidates/decisions")
    def decide_candidates_batch(task_id: str, body: list[CandidateBatchDecision]):
        ctx.task_or_404(task_id)
        existing = {item["id"]: item for item in repo.list("candidates", task_id)}
        missing = [item.candidate_id for item in body if item.candidate_id not in existing]
        if missing:
            raise HTTPException(404, detail={"code": "candidate_not_found", "message": "候选不存在", "field": "candidate_id"})
        output = []
        for item in body:
            candidate = candidate_from_payload(existing[item.candidate_id])
            try:
                candidate = decide_snapshot(candidate, CandidateStatus(item.status), item.note)
            except ValueError as exc:
                raise HTTPException(422, detail={"code": "invalid_candidate_decision", "message": str(exc), "field": "status"}) from exc
            payload = candidate_to_dict(candidate)
            repo.update_candidate(task_id, item.candidate_id, payload)
            output.append(payload)
        return {"items": output}

    @router.post("/tasks/{task_id}/candidates/{candidate_id}/decision")
    def decide_candidate(task_id: str, candidate_id: str, body: CandidateDecision):
        ctx.task_or_404(task_id)
        raw = repo.get("candidates", candidate_id)
        if not raw or not any(item.get("id") == candidate_id for item in repo.list("candidates", task_id)):
            raise HTTPException(404, detail={"code": "candidate_not_found", "message": "候选不存在", "field": "candidate_id"})
        candidate = candidate_from_payload(raw)
        try:
            candidate = decide_snapshot(candidate, CandidateStatus(body.status), body.note)
        except ValueError as exc:
            raise HTTPException(422, detail={"code": "invalid_candidate_decision", "message": str(exc), "field": "status"}) from exc
        payload = candidate_to_dict(candidate)
        repo.update_candidate(task_id, candidate_id, payload)
        return payload

    @router.get("/tasks/{task_id}/candidates/confirmed")
    def list_confirmed_candidates(task_id: str):
        ctx.task_or_404(task_id)
        items = [candidate_from_payload(item) for item in repo.list("candidates", task_id)]
        return {"items": [candidate_to_dict(item) for item in confirmed_candidates(items)]}

    @router.post("/tasks/{task_id}/search")
    def search_task(task_id: str, body: SearchRequest):
        ctx.task_or_404(task_id)
        requirement = repo.get("requirements", task_id) or {}
        if requirement.get("status") != "confirmed":
            raise HTTPException(409, detail={"code": "requirement_not_confirmed", "message": "请先确认需求", "field": "requirement"})
        all_candidates = repo.list("candidates", task_id)
        confirmed = [x for x in all_candidates if x.get("status") == CandidateStatus.CONFIRMED.value]
        if body.plan_id:
            plan = repo.get("reports", body.plan_id)
            task_plan_ids = {item.get("plan_id") for item in repo.list("reports", task_id) if item.get("kind") == "search_plan"}
            if not plan or plan.get("plan_id") not in task_plan_ids:
                raise HTTPException(404, detail={"code": "search_plan_not_found", "message": "检索计划不存在", "field": "plan_id"})
            if plan.get("status") != "confirmed":
                raise HTTPException(409, detail={"code": "search_plan_not_confirmed", "message": "请先确认检索计划", "field": "plan_id"})
            queries = [
                SearchQuery(
                    item["text"],
                    candidate_id=item.get("candidate_id"),
                    brand=item.get("brand"),
                    model=item.get("model"),
                    limit=item.get("limit", body.limit),
                    purpose=item.get("purpose", body.purpose),
                )
                for item in plan.get("queries", [])
                if item.get("text")
            ]
        else:
            if body.candidate_ids:
                selected = [x for x in confirmed if x.get("id") in set(body.candidate_ids)]
                if len(selected) != len(set(body.candidate_ids)):
                    raise HTTPException(409, detail={"code": "candidate_not_confirmed", "message": "只能检索已确认候选", "field": "candidate_ids"})
            elif body.query:
                selected = []
            else:
                selected = confirmed
                if not selected:
                    raise HTTPException(409, detail={"code": "candidate_not_confirmed", "message": "请先确认候选或提供明确检索词", "field": "candidate_ids"})
            queries = [SearchQuery(body.query, limit=body.limit, purpose=body.purpose)] if body.query else []
            for candidate in selected:
                queries.extend(
                    SearchQuery(
                        q.text,
                        candidate_id=q.candidate_id,
                        brand=q.brand,
                        model=q.model,
                        limit=body.limit,
                        purpose=body.purpose,
                    )
                    for q in queries_from_candidate(candidate_from_payload(candidate), purpose=body.purpose)
                )
        runs = []
        for query in queries:
            request_key = sha1(
                json.dumps(
                    {"query": query.__dict__, "fetch_body": body.fetch_body, "modes": ctx.runtime_search_modes},
                    sort_keys=True,
                    default=str,
                ).encode()
            ).hexdigest()

            def usable_cache(item):
                if item.get("request_key") != request_key or not item.get("sources"):
                    return False
                try:
                    fresh = (datetime.now(timezone.utc) - datetime.fromisoformat(item["created_at"])).total_seconds() < 300
                except (KeyError, TypeError, ValueError):
                    return False
                if not fresh or not body.fetch_body:
                    return fresh
                return all(
                    source.get("source_type") == "search_snippet"
                    or (
                        source.get("body_status") == "fetched"
                        and isinstance(source.get("body_text"), str)
                        and bool(source["body_text"].strip())
                    )
                    for source in item["sources"]
                )

            cached = next((item for item in repo.list("search_runs", task_id) if usable_cache(item)), None)
            if cached:
                runs.append(cached)
                continue
            runner = OfficialSearchService(
                providers=ctx.search.providers,
                fetch_body=body.fetch_body,
                timeout=ctx.search.timeout,
                reader=getattr(ctx.search, "reader", None),
            )
            try:
                run = runner.run(query)
            except ValueError as exc:
                raise HTTPException(422, detail={"code": "invalid_search_query", "message": str(exc), "field": "query"}) from exc
            payload = {
                "query": {"text": query.text, "candidate_id": query.candidate_id, "purpose": query.purpose},
                "results": [result_to_dict(x) for x in run.results],
                "sources": [source_to_dict(x) for x in run.sources],
                "warnings": run.warnings,
            }
            from backend.services.education_platform_sources import mask_landed_text
            for item in payload["sources"]:
                if isinstance(item.get("body_text"), str):
                    item["body_text"] = mask_landed_text(item["body_text"])
            for item in payload["results"]:
                if isinstance(item.get("snippet"), str):
                    item["snippet"] = mask_landed_text(item["snippet"])
            run_id = "search-" + uuid.uuid4().hex[:12]
            repo.put("search_runs", run_id, {"id": run_id, "request_key": request_key, "created_at": datetime.now(timezone.utc).isoformat(), **payload}, task_id=task_id)
            runs.append({"id": run_id, **payload})
        return {"items": runs}

    @router.post("/tasks/{task_id}/search/plan")
    def create_search_plan(task_id: str, body: SearchPlanRequest = SearchPlanRequest()):
        task = ctx.task_or_404(task_id)
        requirement = repo.get("requirements", task_id) or {}
        if requirement.get("status") != "confirmed":
            raise HTTPException(409, detail={"code": "requirement_not_confirmed", "message": "请先确认需求", "field": "requirement"})
        confirmed = [x for x in repo.list("candidates", task_id) if x.get("status") == CandidateStatus.CONFIRMED.value]
        queries = []
        scenario = (requirement.get("scenario") or task.get("scenario") or "").strip()
        subject = task_title({"scenario": scenario}, documents=repo.list("documents", task_id))
        targets = confirmed or [{"model": subject.removesuffix("选型") if subject != "实验室仪器选型" else scenario[:100]}]
        for candidate in targets:
            name = " ".join(str(candidate.get(k) or "") for k in ("brand", "model")).strip()
            for suffix, purpose in [
                ("official specifications", "official"),
                ("官网 公开价格", "price"),
                ("中标 成交公告 型号 单价", "tender"),
                ("同类产品 具体型号 技术参数", "discovery"),
            ]:
                prefix = f'"{name}"' if candidate.get("id") else name
                queries.append({
                    "text": f"{prefix} {suffix}",
                    "candidate_id": candidate.get("id"),
                    "brand": candidate.get("brand"),
                    "model": candidate.get("model") if candidate.get("id") else None,
                    "purpose": purpose,
                    "limit": min(body.limit, 5),
                })
        plan = {
            "plan_id": "plan-" + uuid.uuid4().hex[:12],
            "task_id": task_id,
            "status": "pending_confirmation",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "purpose": body.purpose,
            "queries": queries,
            "constraints": {
                "confirmed_requirement": True,
                "confirmed_candidates": [x.get("id") for x in confirmed],
                "price_rule": "formal tender/award documents are historical evidence; snippets are not prices",
            },
        }
        repo.put("reports", plan["plan_id"], {"kind": "search_plan", **plan}, task_id=task_id)
        return plan

    @router.get("/tasks/{task_id}/search/plan")
    def get_search_plan(task_id: str):
        ctx.task_or_404(task_id)
        plans = [item for item in repo.list("reports", task_id) if item.get("kind") == "search_plan"]
        if not plans:
            return None
        return sorted(plans, key=lambda item: (item.get("created_at", ""), item.get("plan_id", "")))[-1]

    @router.post("/tasks/{task_id}/search/plan/{plan_id}/confirm")
    def confirm_search_plan(task_id: str, plan_id: str):
        ctx.task_or_404(task_id)
        plan = repo.get("reports", plan_id)
        task_plan_ids = {item.get("plan_id") for item in repo.list("reports", task_id) if item.get("kind") == "search_plan"}
        if not plan or plan.get("plan_id") not in task_plan_ids:
            raise HTTPException(404, detail={"code": "search_plan_not_found", "message": "检索计划不存在", "field": "plan_id"})
        plan["status"] = "confirmed"
        plan["confirmed_at"] = datetime.now(timezone.utc).isoformat()
        repo.put("reports", plan_id, plan, task_id=task_id)
        return plan

    @router.get("/tasks/{task_id}/search")
    def list_search_runs(task_id: str):
        ctx.task_or_404(task_id)
        decisions = {item.get("url"): item for item in repo.list("source_decisions", task_id)}
        items = []
        for run in repo.list("search_runs", task_id):
            current = dict(run)
            current["sources"] = [
                {**source, "decision_status": decisions.get(source.get("url"), {}).get("status", "pending_review")}
                for source in run.get("sources", [])
            ]
            items.append(current)
        return {"items": items}

    @router.post("/tasks/{task_id}/search/discover-candidates")
    def discover_web_candidates(task_id: str, body: WebCandidateDiscoveryRequest = WebCandidateDiscoveryRequest()):
        task = ctx.task_or_404(task_id)
        requirement = repo.get("requirements", task_id) or {}
        if requirement.get("status") != "confirmed":
            raise HTTPException(409, detail={"code": "requirement_not_confirmed", "message": "请先确认需求", "field": "requirement"})
        decisions = {item.get("url"): item.get("status") for item in repo.list("source_decisions", task_id)}
        requested = set(body.source_urls)
        sources = []
        for run in repo.list("search_runs", task_id):
            for source in run.get("sources", []):
                url = source.get("url")
                if decisions.get(url) != "confirmed" or (requested and url not in requested):
                    continue
                if source.get("body_status") == "fetched" and source.get("source_type") != "search_snippet":
                    sources.append(source)
        if not sources:
            raise HTTPException(409, detail={"code": "confirmed_source_required", "message": "请先确认至少一个已抓取正文的来源", "field": "source_urls"})
        client_cls = deepseek_client_cls()
        try:
            client = ctx.runtime_provider_client
            if client is None:
                client = client_cls.from_env(model=body.model or "deepseek-flash")
            elif body.model:
                client = client_cls(
                    api_key=client.api_key,
                    endpoint=client.endpoint,
                    model=body.model,
                    timeout=client.timeout,
                    web_tool_type=client.web_tool_type,
                    transport=client.transport,
                )
            discovered = WebCandidateDiscoveryService(client).discover(scenario=task.get("scenario", ""), sources=sources)
        except DeepSeekError as exc:
            raise HTTPException(503, detail={"code": exc.code, "message": exc.message}) from exc
        existing = [candidate_from_payload(item) for item in repo.list("candidates", task_id)]
        merged = merge_candidates([*existing, *discovered])
        for candidate in merged:
            payload = candidate_to_dict(candidate)
            repo.put("candidates", candidate.id, payload, task_id=task_id)
        discovered_keys = {candidate.identity_key() for candidate in discovered}
        return {"items": [candidate_to_dict(candidate) for candidate in merged if candidate.identity_key() in discovered_keys]}

    @router.post("/tasks/{task_id}/sources/decisions")
    def decide_sources(task_id: str, body: list[SourceDecision]):
        ctx.task_or_404(task_id)
        output = []
        for item in body:
            decision = {"url": item.url, "status": item.status, "note": item.note}
            repo.put("source_decisions", item.url, decision, task_id=task_id)
            output.append(decision)
        return {"items": output}

    @router.get("/tasks/{task_id}/sources/decisions")
    def list_source_decisions(task_id: str):
        ctx.task_or_404(task_id)
        return {"items": repo.list("source_decisions", task_id)}
