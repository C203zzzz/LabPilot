"""Conservative DeepSeek analysis of uploaded procurement documents."""
from __future__ import annotations

import json
import logging
import re
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Mapping

from backend.infrastructure.deepseek import DeepSeekClient, DeepSeekError
from backend.services import inventory as _inventory_module
from backend.services.analysis_evidence_payload import (
    candidate_evidence_payload as _candidate_evidence_payload,
    normalize_analysis_output as _normalize_analysis_output,
    normalize_source as _source,
    normalize_status as _status,
    unparsed_field_evidence as _unparsed_field_evidence,
)
from backend.services.analysis_messages import (
    brand_unresolved_failure as _brand_unresolved_failure,
    brand_unresolved_gap as _brand_unresolved_gap,
    no_owned_evidence_failure as _no_owned_evidence_failure,
    no_owned_evidence_gap as _no_owned_evidence_gap,
    sibling_merge_failure as _sibling_merge_failure,
    sibling_merge_gap as _sibling_merge_gap,
)
from backend.services.analysis_ownership import (
    PARAMETERS_STATUS_NOT_LOCATED,
    brands_conflict as _brands_conflict,
    candidate_document_ids as _candidate_document_ids,
    enterprise_brand_from_documents as _enterprise_brand_from_documents,
    evidence_excerpts as _evidence_excerpts,
    family_key as _family_key,
    family_models_in_excerpt as _family_models_in_excerpt,
    identity_key as _identity_key,
    is_series_named as _is_series_named,
    merge_configuration as _merge_configuration,
    merge_unowned_candidates as _merge_unowned_candidates,
    model_number_agrees,
    model_number_and_prefix,
    sibling_merge_source as _sibling_merge_source,
    tidy_candidate_identities as _tidy_candidate_identities,
)
from backend.services.analysis_prompts import (
    CANDIDATE_INVENTORY_PROMPT,
    CANONICAL_FIELD_NAMES,
    FIELD_GAP_PROMPT,
    PARAMETER_FALLBACK_PROMPT,
    SYSTEM_PROMPT,
    VISUAL_DETAIL_PROMPT,
    VISUAL_INVENTORY_PROMPT,
)
from backend.services.candidate_merging import (
    candidate_identity as _candidate_identity_new,
    compatible_candidate_identity as _compatible_candidate_identity_new,
    merge_inventory_candidates as _merge_inventory_candidates,
    merge_short_series_aliases as _merge_short_series_aliases_new,
    structured_mention_inventory as _structured_mention_inventory,
)
from backend.services.candidate_merging.inventory_merger import (
    _candidate_has_only_non_instrument_evidence,
)
from backend.services.evidence_selection import (
    field_retrieval_profile as _field_retrieval_profile,
)
from backend.services.evidence_selection.limits import (
    _CANDIDATE_EVIDENCE_PAGE_CHAR_LIMIT,
    _CANDIDATE_EVIDENCE_PAGE_LIMIT,
    _CANDIDATE_EVIDENCE_TABLE_CHAR_LIMIT,
    _CANDIDATE_EVIDENCE_TABLE_ROW_LIMIT,
)
from backend.services.evidence_validation import (
    canonical_number as _canonical_number,
    excerpt_matches_evidence as _excerpt_matches_evidence,
    label_declares_size_unit as _label_declares_size_unit,
    metric_excerpt_supports as _metric_excerpt_supports,
    missing_quantity_unit as _missing_quantity_unit,
    numeric_tokens as _numeric_tokens,
    source_evidence_index as _source_evidence_index,
    temperature_metric as _temperature_metric,
    validated_field as _validated_field,
    validated_sources as _validated_sources,
    value_identity as _value_identity,
)
from backend.services.evidence_validation.field_validator import _FIELD_SOURCE_LIMIT
from backend.services.inventory import (
    configuration_with_catalog_number as _configuration_with_catalog_number,
    compound_model_variants as _compound_model_variants,
    has_concrete_model as _has_concrete_model,
    inventory_batches as _inventory_batches,
    inventory_units as _inventory_units,
    is_compound_of_known_models as _is_compound_of_known_models,
    normalise_inventory_candidate as _normalise_inventory_candidate,
    normalized_model as _normalized_model,
    probable_catalog_number as _probable_catalog_number,
    split_inventory_unit as _split_inventory_unit,
)
from backend.services.inventory.batch_builder import (
    _INVENTORY_BATCH_CHAR_LIMIT as _INVENTORY_BATCH_CHAR_LIMIT,
)
from backend.services.parameter_extraction import (
    PARAMETERS_STATUS_EXTRACTED,
    PARAMETERS_STATUS_FALLBACK_EMPTY,
    PARAMETERS_STATUS_INCOMPLETE,
    PARAMETERS_STATUS_NOT_IN_SOURCE,
    deterministic_parameter_fields as _deterministic_parameter_fields,
    has_comparable_parameter as _has_comparable_parameter,
    has_usable_parameter as _has_usable_parameter,
    merge_repaired_fields as _merge_repaired_fields,
    parameter_extraction_incomplete_failure as _parameter_extraction_incomplete_failure,
    parameter_fallback_failure as _parameter_fallback_failure,
    parameter_fallback_gap as _parameter_fallback_gap,
    parameters_absent_gap as _parameters_absent_gap,
    repairable_field_names as _repairable_field_names,
)
from backend.services.procurement_rules import contains_model
from backend.services.table_model_discovery import table_model_inventory

logger = logging.getLogger(__name__)

_UNKNOWN_VALUES = {"", "unknown", "none", "null", "未知", "未提供", "n/a", "na"}


class AnalysisValidationError(ValueError):
    pass


def _normalize_price_value(value: Any) -> Any:
    if isinstance(value, dict) and "price" in value:
        price_str = str(value["price"])
        return price_str.split()[0] if price_str else price_str
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return value.split()[0] if value.split() else value
    return value


def _merge_validated_fallback_fields(
    raw_fields: list[Any],
    validated_fallback_fields: list[Any],
) -> None:
    existing_by_name: dict[str, Mapping[str, Any]] = {}
    for field in raw_fields:
        if isinstance(field, Mapping) and field.get("field_name"):
            existing_by_name.setdefault(field["field_name"], field)

    for fallback_field in validated_fallback_fields:
        if not isinstance(fallback_field, Mapping):
            raw_fields.append(fallback_field)
            continue
        field_name = fallback_field.get("field_name")
        if field_name not in existing_by_name:
            raw_fields.append(fallback_field)
            existing_by_name[field_name] = fallback_field
            continue
        existing = existing_by_name[field_name]
        existing_value = existing.get("value")
        fallback_value = fallback_field.get("value")
        if field_name == "price_warranty_delivery_and_service":
            values_match = _normalize_price_value(existing_value) == _normalize_price_value(fallback_value)
        else:
            values_match = existing_value == fallback_value
        if values_match:
            existing_sources = existing.get("sources", [])
            fallback_sources = fallback_field.get("sources", [])
            if isinstance(existing_sources, list) and isinstance(fallback_sources, list):
                for i, current in enumerate(raw_fields):
                    if current is existing:
                        combined = existing_sources + [s for s in fallback_sources if s not in existing_sources]
                        raw_fields[i] = {**existing, "sources": combined}
                        break
        else:
            raw_fields.append(fallback_field)


def _merge_deterministic_baseline(
    candidate: Mapping[str, Any],
    raw_fields: list[Any],
    deterministic_baseline: list[dict[str, Any]],
    gaps: list[str],
) -> list[Any]:
    has_model_comparable = _has_comparable_parameter(None, raw_fields)
    logger.warning(
        "[DEBUG] Merge logic for %s: deterministic_baseline=%d fields, has_model_comparable=%s",
        candidate.get("model"),
        len(deterministic_baseline),
        has_model_comparable,
    )
    gaps.append(
        f"[DEBUG] {candidate.get('model')}：确定性baseline={len(deterministic_baseline)}个字段，"
        f"模型可比参数={'有' if has_model_comparable else '无'}"
    )
    if not has_model_comparable:
        gaps.append(
            f"{candidate.get('model')}：模型未提取可比较参数，"
            f"使用确定性规则提取到{len(deterministic_baseline)}个参数。"
        )
        return deterministic_baseline

    det_by_name = {
        f.get("field_name"): f
        for f in deterministic_baseline
        if isinstance(f, dict) and f.get("field_name")
    }
    replaced_count = 0
    for i, model_field in enumerate(raw_fields):
        if not isinstance(model_field, dict):
            continue
        field_name = model_field.get("field_name")
        if field_name not in det_by_name:
            continue
        if not _has_comparable_parameter(None, [model_field]):
            raw_fields[i] = det_by_name.pop(field_name)
            replaced_count += 1
    raw_fields.extend(det_by_name.values())
    if replaced_count > 0:
        gaps.append(
            f"{candidate.get('model')}：模型字段包含配件列表，"
            f"用确定性规则替换了{replaced_count}个字段，追加了{len(det_by_name)}个字段。"
        )
    return raw_fields


class DeepSeekAnalysisService:
    def __init__(self, client: DeepSeekClient | None = None, checkpoint=None):
        self.client = client
        self.checkpoint = checkpoint

    def analyze(
        self,
        *,
        scenario: str,
        documents: list[Mapping[str, Any]],
        target_model: str | None = None,
    ) -> dict[str, Any]:
        if not scenario.strip():
            raise AnalysisValidationError("scenario is required")
        if not documents:
            raise AnalysisValidationError("至少需要一份已上传资料")
        prompt_docs = self._build_prompt_documents(documents)
        if self.client is None:
            self.client = DeepSeekClient.from_env()
        if any(doc.get("analysis_input") in {"rendered_page_vision", "hybrid"} for doc in prompt_docs):
            raw = self._analyze_visual(scenario, prompt_docs)
        else:
            raw = self._analyze_native_text(scenario, prompt_docs)
        result = self._normalize(raw)
        result["gaps"] = raw.get("gaps", [])
        result["failures"] = raw.get("failures", [])
        result["call_count"] = raw.get("call_count", 0)
        result["operation_timings"] = raw.get("operation_timings", [])
        _tidy_candidate_identities(result)
        self._scope_to_target_model(result, target_model)
        self._record_unresolved_brands(result, raw.get("brand_unresolved") or [], prompt_docs)
        if result["candidates"] and all(all(f["status"] == "unknown" for f in c["fields"]) for c in result["candidates"]):
            result["gaps"].append("已识别型号，但关键参数未可靠提取，请核对解析警告和原始资料")
        if result["gaps"]:
            result["status"] = "partial"
        return result

    @staticmethod
    def _build_prompt_documents(documents: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
        prompt_docs = []
        for doc in documents:
            visual_pages: dict[int, Mapping[str, Any]] = {}
            extraction = doc.get("model_extraction", {})
            extraction_fields = extraction.get("fields", {}) if isinstance(extraction, Mapping) else {}
            if isinstance(extraction_fields, Mapping):
                for location, value in extraction_fields.items():
                    if not isinstance(value, Mapping):
                        continue
                    match = re.fullmatch(r"page:(\d+)/rendered", str(location))
                    if match and str(value.get("text", "")).strip():
                        visual_pages[int(match.group(1))] = value
            pages = []
            for page in doc.get("pages", []):
                page_number = int(page.get("number") or len(pages) + 1)
                route = page.get("route")
                visual = visual_pages.get(page_number) if route in (None, "vision") else None
                page_input = "rendered_page_vision" if visual else "parser_text"
                pages.append({
                    "page": page_number,
                    "regions": page.get("regions", []),
                    "location": f"page:{page_number}/rendered" if visual else page.get("source_location"),
                    "text": str(visual.get("text", "")) if visual else str(page.get("text", "")),
                    "visual_fields": visual.get("fields", {}) if visual else {},
                    "analysis_input": page_input,
                    "route": route,
                    "source_provenance": page.get("provenance", []),
                })
            page_inputs = {page.get("analysis_input") for page in pages}
            document_input = "hybrid" if len(page_inputs) > 1 else next(iter(page_inputs), "parser_text")
            prompt_docs.append({
                "document_id": doc.get("id"),
                "source_name": doc.get("source_name"),
                "analysis_input": document_input,
                "pages": pages,
                "tables": doc.get("tables", []),
            })
        return prompt_docs

    @staticmethod
    def _record_unresolved_brands(
        result: dict[str, Any],
        unresolved: list[Mapping[str, Any]],
        documents: list[Mapping[str, Any]],
    ) -> None:
        names_by_model: defaultdict[str, list[str]] = defaultdict(list)
        for item in unresolved:
            if not isinstance(item, Mapping):
                continue
            model = str(item.get("model") or "")
            names_by_model[model].extend(str(name) for name in item.get("names") or [])
        models = [
            str(candidate.get("model") or "")
            for candidate in result.get("candidates") or []
            if not str(candidate.get("brand") or "").strip()
        ]
        if not models:
            return
        result["gaps"].append(_brand_unresolved_gap(models))
        result.setdefault("failures", []).append(
            _brand_unresolved_failure(
                models,
                [name for model in models for name in names_by_model.get(model, [])],
                documents,
            )
        )

    @staticmethod
    def _scope_to_target_model(result: dict[str, Any], target_model: str | None) -> None:
        target = str(target_model or "").strip()
        if not target:
            return
        matched = [c for c in result["candidates"] if contains_model(str(c.get("model") or ""), target)]
        if not matched:
            result["gaps"].append(f"已确认型号 {target} 未在资料中匹配到候选，已保留全部候选供人工确认。")
            return
        result["candidates"] = matched
        if len(matched) > 1:
            result["gaps"].append(
                f"已按已确认型号 {target} 筛选出 {len(matched)} 个候选（可能是同型号的不同配置），请人工确认。"
            )

    def _analyze_visual(self, scenario: str, prompt_docs: list[dict[str, Any]]) -> Mapping[str, Any]:
        return self._analyze_candidate_pipeline(scenario, prompt_docs)

    def _analyze_native_text(self, scenario: str, prompt_docs: list[dict[str, Any]]) -> Mapping[str, Any]:
        return self._analyze_candidate_pipeline(scenario, prompt_docs)

    def _run_inventory_stage(
        self,
        scenario: str,
        prompt_docs: list[dict[str, Any]],
        bounded_call,
        gaps: list[str],
    ) -> list[dict[str, Any]]:
        inventory_results = []

        def inventory_batch(batch, depth=0):
            if "inventory_batch" in batch:
                batch["inventory_batch"]["character_budget"] = _INVENTORY_BATCH_CHAR_LIMIT
            try:
                inventory_results.append(bounded_call(
                    _scope={
                        "stage": "inventory",
                        "documents": sorted({str(u.get("document_id", "")) for u in batch.get("evidence_units", [])}),
                        "depth": depth,
                    },
                    system_prompt=CANDIDATE_INVENTORY_PROMPT,
                    user_prompt=f"采购场景：{scenario}\n请根据以下有界资料批次整理 json 候选清单：\n"
                    + json.dumps(batch, ensure_ascii=False, separators=(",", ":")),
                ))
            except DeepSeekError as exc:
                units = batch.get("evidence_units", [])
                if exc.code != "model_output_truncated" or depth >= 2 or not units:
                    raise
                if len(units) > 1:
                    parts = [units[: len(units) // 2], units[len(units) // 2 :]]
                else:
                    unit = units[0]
                    text = unit.get("text", "")
                    if len(text) < 100:
                        raise
                    middle = len(text) // 2
                    parts = [[{**unit, "text": text[:middle]}], [{**unit, "text": text[middle:]}]]
                for part in parts:
                    inventory_batch({**batch, "evidence_units": part}, depth + 1)

        original_limit = _inventory_module.batch_builder._INVENTORY_BATCH_CHAR_LIMIT
        _inventory_module.batch_builder._INVENTORY_BATCH_CHAR_LIMIT = _INVENTORY_BATCH_CHAR_LIMIT
        try:
            for batch in [b for doc in prompt_docs for b in _inventory_batches([doc])]:
                try:
                    inventory_batch(batch)
                except DeepSeekError as exc:
                    if not inventory_results:
                        raise
                    gaps.append("目录识别尚未完成：" + exc.message)
                    break
        finally:
            _inventory_module.batch_builder._INVENTORY_BATCH_CHAR_LIMIT = original_limit

        inventory_results.append(_structured_mention_inventory(prompt_docs))
        inventory_results.append(table_model_inventory(prompt_docs))
        candidates, truncation_gap = _merge_inventory_candidates(
            inventory_results,
            _source_evidence_index(prompt_docs),
        )
        if truncation_gap:
            gaps.append(truncation_gap)
        return candidates

    def _analyze_candidate_pipeline(
        self,
        scenario: str,
        prompt_docs: list[dict[str, Any]],
    ) -> Mapping[str, Any]:
        calls = 0
        gaps: list[str] = []
        operation_timings: list[dict[str, Any]] = []
        brand_unresolved: list[dict[str, Any]] = []

        def record_operation(started, clock, outcome, cache, purpose, error_code=None):
            operation_timings.append({
                "operation": "document_model",
                "status": outcome,
                "started_at": datetime.fromtimestamp(started, timezone.utc).isoformat(),
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "duration_ms": max(0, int(round((time.perf_counter() - clock) * 1000))),
                "round": 1,
                "purpose": purpose,
                "cache": cache,
                "attempts": 0 if cache == "checkpoint_hit" else 1,
                "error_code": error_code,
            })
            del operation_timings[:-200]

        def bounded_call(_scope=None, **kwargs):
            nonlocal calls
            scope = _scope if isinstance(_scope, Mapping) else {}
            purpose = str(scope.get("stage") or "unspecified")
            started, clock = time.time(), time.perf_counter()
            checkpoint = self.checkpoint
            key = checkpoint.key(self.client, kwargs) if checkpoint else None
            if checkpoint:
                checkpoint.check()
                cached = checkpoint.get(key)
                if cached and cached["status"] == "completed":
                    record_operation(started, clock, "completed", "checkpoint_hit", purpose)
                    return cached["result"]
                if cached and cached.get("code") == "model_output_truncated":
                    record_operation(started, clock, "stopped", "checkpoint_hit", purpose, "model_output_truncated")
                    raise DeepSeekError("model_output_truncated", "此区域曾截断，继续其拆分区域")
            if calls >= 12:
                record_operation(started, clock, "stopped", "miss", purpose, "document_budget_exceeded")
                raise DeepSeekError(
                    "document_budget_exceeded",
                    "本轮资料分析最多调用十二次——这是单次请求的用量上限，不是故障。"
                    "已完成的识别结果都保留着；需要看更多页时，只选关键几页再分析一次即可。",
                )
            calls += 1
            if checkpoint:
                checkpoint.save(key, "running", scope=_scope)
            try:
                result = self.client.analyze(**kwargs)
            except DeepSeekError as exc:
                record_operation(started, clock, "failed", "miss", purpose, exc.code)
                if checkpoint:
                    checkpoint.save(key, "failed", code=exc.code, scope=_scope)
                raise
            if checkpoint:
                checkpoint.save(key, "completed", result=result, scope=_scope)
                checkpoint.check()
            record_operation(started, clock, "completed", "miss", purpose)
            return result

        candidates = self._run_inventory_stage(scenario, prompt_docs, bounded_call, gaps)
        if not candidates:
            return {
                "candidates": [],
                "gaps": gaps,
                "call_count": calls,
                "brand_unresolved": [],
                "operation_timings": operation_timings,
            }

        full_text_evidence = _source_evidence_index(prompt_docs)
        from backend.services.evidence_ownership import scoped_documents
        ownership = [scoped_documents(candidate, prompt_docs, candidates) for candidate in candidates]
        output: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []

        for index, candidate in enumerate(candidates):
            owned_docs = ownership[index]
            owned_ids = {str(doc.get("document_id") or "") for doc in owned_docs}
            owned_full_docs = [
                doc for doc in prompt_docs if str(doc.get("document_id") or "") in owned_ids
            ] or prompt_docs
            owned_locations = {
                (str(doc.get("document_id") or ""), str(page.get("location", page.get("source_location")) or ""))
                for doc in owned_docs
                for page in doc.get("pages", [])
                if isinstance(page, Mapping)
            }
            deterministic_docs = []
            for doc in owned_full_docs:
                document_id = str(doc.get("document_id") or "")
                pages = [
                    page
                    for page in doc.get("pages", [])
                    if isinstance(page, Mapping)
                    and (document_id, str(page.get("location", page.get("source_location")) or "")) in owned_locations
                ]
                if pages:
                    deterministic_docs.append({**doc, "pages": pages})

            self._resolve_candidate_brand(candidate, owned_full_docs, gaps, brand_unresolved)
            if not owned_docs:
                skipped = dict(candidate)
                skipped["fields"] = []
                skipped["parameters_status"] = PARAMETERS_STATUS_NOT_LOCATED
                skipped["sources"] = _validated_sources(candidate.get("sources", []), full_text_evidence)[:2]
                output.append(skipped)
                continue

            item, abort_remaining = self._analyze_single_candidate(
                scenario=scenario,
                candidate=candidate,
                candidates=candidates,
                index=index,
                owned_docs=owned_docs,
                deterministic_docs=deterministic_docs,
                prompt_docs=prompt_docs,
                full_text_evidence=full_text_evidence,
                bounded_call=bounded_call,
                gaps=gaps,
                failures=failures,
                output=output,
            )
            if abort_remaining:
                break
            if item is not None:
                output.append(item)

        _merge_unowned_candidates(output, candidates, ownership, gaps, failures)
        return {
            "candidates": output,
            "gaps": gaps,
            "call_count": calls,
            "brand_unresolved": brand_unresolved,
            "failures": failures,
            "operation_timings": operation_timings,
        }

    @staticmethod
    def _resolve_candidate_brand(
        candidate: dict[str, Any],
        owned_full_docs: list[Mapping[str, Any]],
        gaps: list[str],
        brand_unresolved: list[dict[str, Any]],
    ) -> None:
        body_brand, body_brand_names = _enterprise_brand_from_documents(owned_full_docs)
        if str(candidate.get("brand") or "").strip():
            return
        if body_brand:
            candidate["brand"] = body_brand
        elif len(body_brand_names) > 1:
            gaps.append(
                f"{candidate.get('model')}：资料正文里出现多个厂名（{'、'.join(body_brand_names[:3])}），"
                "未自动填入品牌，请人工指定。"
            )
        else:
            brand_unresolved.append({
                "model": str(candidate.get("model") or ""),
                "names": list(body_brand_names),
            })

    def _analyze_single_candidate(
        self,
        *,
        scenario: str,
        candidate: dict[str, Any],
        candidates: list[dict[str, Any]],
        index: int,
        owned_docs: list[dict[str, Any]],
        deterministic_docs: list[dict[str, Any]],
        prompt_docs: list[dict[str, Any]],
        full_text_evidence: Mapping[tuple[str, str], str],
        bounded_call,
        gaps: list[str],
        failures: list[dict[str, Any]],
        output: list[dict[str, Any]],
    ) -> tuple[dict[str, Any] | None, bool]:
        candidate_json = json.dumps(candidate, ensure_ascii=False, separators=(",", ":"))
        evidence_payload = _candidate_evidence_payload(candidate, owned_docs)
        evidence_json = json.dumps(evidence_payload, ensure_ascii=False, separators=(",", ":"))
        other_models = [str(other.get("model") or "") for other in candidates if other is not candidate]
        deterministic_baseline = _deterministic_parameter_fields(
            deterministic_docs,
            model=str(candidate.get("model") or ""),
            other_models=other_models,
        )

        def detail_region(payload, depth=0):
            try:
                return bounded_call(
                    _scope={
                        "stage": "parameters",
                        "model": candidate.get("model"),
                        "depth": depth,
                        "documents": [d.get("document_id") for d in payload.get("documents", [])],
                    },
                    system_prompt=VISUAL_DETAIL_PROMPT,
                    user_prompt=(
                        f"采购场景：{scenario}\ncandidate：{candidate_json}\n"
                        f"候选级有界证据 JSON：\n{json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}"
                    ),
                )
            except DeepSeekError as exc:
                if exc.code != "model_output_truncated" or depth >= 2:
                    raise
                from backend.services.analysis_regions import split_evidence
                parts = split_evidence(payload)
                if len(parts) < 2:
                    raise
                merged = {**candidate, "fields": [], "sources": []}
                for part in parts:
                    partial = detail_region(part, depth + 1)
                    for part_item in partial.get("candidates", []):
                        merged["fields"].extend(part_item.get("fields", part_item.get("parameters", [])))
                        merged["sources"].extend(part_item.get("sources", []))
                return {"candidates": [merged]}

        try:
            detail = detail_region(evidence_payload)
        except DeepSeekError as exc:
            skipped_candidates = candidates[index:]
            if not skipped_candidates:
                gaps.append(exc.message)
            for remaining in skipped_candidates:
                if remaining is candidate and deterministic_baseline:
                    fallback_fields = deterministic_baseline
                else:
                    fallback_fields = _deterministic_parameter_fields(
                        deterministic_docs,
                        model=str(remaining.get("model") or ""),
                        other_models=[str(other.get("model") or "") for other in candidates if other is not remaining],
                    )
                if fallback_fields and _has_comparable_parameter(None, fallback_fields):
                    gaps.append(
                        f"{remaining.get('model')}：模型调用失败（{exc.message}），"
                        f"使用确定性规则提取到{len(fallback_fields)}个参数。"
                    )
                    output.append({
                        **remaining,
                        "fields": fallback_fields,
                        "parameters_status": PARAMETERS_STATUS_EXTRACTED,
                    })
                else:
                    gaps.append(
                        f"{remaining.get('model')}：参数抽取未完成（{exc.message}），"
                        "该候选参数留空，请人工确认。"
                    )
                    failures.append(_parameter_extraction_incomplete_failure(remaining.get("model"), exc, prompt_docs))
                    output.append({
                        **remaining,
                        "fields": [],
                        "parameters_status": PARAMETERS_STATUS_INCOMPLETE,
                    })
            return None, True

        detail_candidates = detail.get("candidates", [])
        if not isinstance(detail_candidates, list) or not detail_candidates or not isinstance(detail_candidates[0], Mapping):
            raise DeepSeekError("candidate_detail_missing", f"候选 {candidate.get('model')} 未返回详情")
        item = dict(detail_candidates[0])
        detail_evidence = _source_evidence_index(evidence_payload["documents"])
        item["sources"] = _validated_sources(item.get("sources", []), detail_evidence)
        raw_fields = item.get("fields", item.get("parameters", []))
        if isinstance(raw_fields, list):
            raw_fields = [_validated_field(field, detail_evidence) if isinstance(field, Mapping) else field for field in raw_fields]
        else:
            raw_fields = []
        if deterministic_baseline:
            raw_fields = _merge_deterministic_baseline(candidate, raw_fields, deterministic_baseline, gaps)
        item["fields"] = raw_fields

        fallback_empty, deterministic_fallback_used, incomplete_item = self._run_parameter_fallback(
            scenario=scenario,
            candidate=candidate,
            candidate_json=candidate_json,
            item=item,
            raw_fields=raw_fields,
            evidence_json=evidence_json,
            detail_evidence=detail_evidence,
            full_text_evidence=full_text_evidence,
            deterministic_docs=deterministic_docs,
            other_models=other_models,
            prompt_docs=prompt_docs,
            bounded_call=bounded_call,
            gaps=gaps,
            failures=failures,
        )
        if incomplete_item is not None:
            return incomplete_item, False

        raw_fields = self._run_field_repairs(
            scenario=scenario,
            candidate=candidate,
            candidate_json=candidate_json,
            raw_fields=item["fields"],
            evidence_payload=evidence_payload,
            evidence_json=evidence_json,
            detail_evidence=detail_evidence,
            fallback_empty=fallback_empty,
            bounded_call=bounded_call,
            gaps=gaps,
        )
        item["fields"] = raw_fields

        for field in raw_fields:
            if isinstance(field, Mapping) and field.get("_unsupported_parts"):
                gaps.append(
                    f"{candidate.get('model')} · {field.get('field_name')}："
                    f"部分参数未通过原文校验（引文逐字对不上正文、数值没有单位，或指标与证据行对不上），"
                    f"未采用（{'、'.join(field['_unsupported_parts'])}）"
                )
        for key in ("brand", "series", "model", "configuration"):
            item[key] = candidate.get(key)
        if deterministic_fallback_used:
            item["parameters_origin"] = {
                "kind": "deterministic_text_fallback",
                "source": "已读候选正文中的明确参数行",
            }
        if not item.get("sources"):
            item["sources"] = _validated_sources(candidate.get("sources", []), full_text_evidence)[:2]
        if _has_comparable_parameter(item.get("configuration"), item.get("fields")):
            item["parameters_status"] = PARAMETERS_STATUS_EXTRACTED
        elif fallback_empty:
            item["parameters_status"] = PARAMETERS_STATUS_FALLBACK_EMPTY
        else:
            item["parameters_status"] = PARAMETERS_STATUS_NOT_IN_SOURCE
            gaps.append(_parameters_absent_gap(candidate.get("model")))
        return item, False

    def _run_parameter_fallback(
        self,
        *,
        scenario: str,
        candidate: dict[str, Any],
        candidate_json: str,
        item: dict[str, Any],
        raw_fields: list[Any],
        evidence_json: str,
        detail_evidence: Mapping[tuple[str, str], str],
        full_text_evidence: Mapping[tuple[str, str], str],
        deterministic_docs: list[dict[str, Any]],
        other_models: list[str],
        prompt_docs: list[dict[str, Any]],
        bounded_call,
        gaps: list[str],
        failures: list[dict[str, Any]],
    ) -> tuple[bool, bool, dict[str, Any] | None]:
        fallback_attempted = (
            not str(candidate.get("configuration") or "").strip()
            or not _has_usable_parameter(raw_fields)
            or not _has_comparable_parameter(candidate.get("configuration"), raw_fields)
        )
        if not fallback_attempted:
            return False, False, None

        fallback_empty = False
        deterministic_fallback_used = False
        try:
            fallback = bounded_call(
                _scope={"stage": "parameter_fallback", "model": candidate.get("model")},
                system_prompt=PARAMETER_FALLBACK_PROMPT,
                user_prompt=(
                    f"采购场景：{scenario}\ncandidate：{candidate_json}\n"
                    f"候选级有界证据 JSON：\n{evidence_json}"
                ),
            )
        except DeepSeekError as exc:
            deterministic_fields = []
            if not _has_comparable_parameter(candidate.get("configuration"), raw_fields):
                deterministic_fields = _deterministic_parameter_fields(
                    deterministic_docs,
                    model=str(candidate.get("model") or ""),
                    other_models=other_models,
                )
            if deterministic_fields:
                raw_fields.extend(deterministic_fields)
                item["fields"] = raw_fields
                deterministic_fallback_used = True
                gaps.append(_parameter_fallback_gap(candidate.get("model"), "deterministic_text_fallback"))
                failures.append(
                    _parameter_extraction_incomplete_failure(
                        candidate.get("model"), exc, prompt_docs, "deterministic_text_fallback"
                    )
                )
            elif not _has_usable_parameter(raw_fields):
                gaps.append(
                    f"{candidate.get('model')}：参数抽取未完成（{exc.message}），该候选参数留空，请人工确认。"
                )
                failures.append(_parameter_extraction_incomplete_failure(candidate.get("model"), exc, prompt_docs))
                item["parameters_status"] = PARAMETERS_STATUS_INCOMPLETE
                for key in ("brand", "series", "model", "configuration"):
                    item[key] = candidate.get(key)
                if not item.get("sources"):
                    item["sources"] = _validated_sources(candidate.get("sources", []), full_text_evidence)[:2]
                return False, False, item
            else:
                gaps.append(f"部分参数补提取未完成：{exc.message}")
            return fallback_empty, deterministic_fallback_used, None

        fallback_candidates = fallback.get("candidates", []) if isinstance(fallback, Mapping) else []
        fallback_item = (
            fallback_candidates[0]
            if isinstance(fallback_candidates, list) and fallback_candidates and isinstance(fallback_candidates[0], Mapping)
            else {}
        )
        fallback_fields = fallback_item.get("fields", fallback_item.get("parameters", [])) if isinstance(fallback_item, Mapping) else []
        validated_fallback_fields = []
        if isinstance(fallback_fields, list):
            validated_fallback_fields = [
                _validated_field(field, detail_evidence) if isinstance(field, Mapping) else field
                for field in fallback_fields
            ]
            _merge_validated_fallback_fields(raw_fields, validated_fallback_fields)
            item["fields"] = raw_fields

        fallback_configuration = fallback_item.get("configuration") if isinstance(fallback_item, Mapping) else None
        if (
            isinstance(fallback_configuration, str)
            and fallback_configuration.strip()
            and fallback_configuration.strip().casefold() not in _UNKNOWN_VALUES
            and _excerpt_matches_evidence(fallback_configuration, "\n".join(detail_evidence.values()))
        ):
            candidate["configuration"] = fallback_configuration.strip()
            from backend.services.product_configuration import PRODUCT_SOURCE
            if "configuration_source" not in candidate:
                candidate["configuration_source"] = PRODUCT_SOURCE

        fallback_empty = not _has_comparable_parameter(fallback_configuration, validated_fallback_fields)
        if fallback_empty:
            deterministic_fields = _deterministic_parameter_fields(
                deterministic_docs,
                model=str(candidate.get("model") or ""),
                other_models=other_models,
            )
            if deterministic_fields:
                raw_fields.extend(deterministic_fields)
                item["fields"] = raw_fields
                deterministic_fallback_used = True
                fallback_empty = not _has_comparable_parameter(candidate.get("configuration"), raw_fields)
                gaps.append(_parameter_fallback_gap(candidate.get("model"), "deterministic_text_fallback"))
                failures.append(_parameter_fallback_failure(candidate.get("model"), fallback, "deterministic_text_fallback"))
        if fallback_empty:
            gaps.append(_parameter_fallback_gap(candidate.get("model")))
            failures.append(_parameter_fallback_failure(candidate.get("model"), fallback))
        return fallback_empty, deterministic_fallback_used, None

    @staticmethod
    def _run_field_repairs(
        *,
        scenario: str,
        candidate: Mapping[str, Any],
        candidate_json: str,
        raw_fields: list[Any],
        evidence_payload: Mapping[str, Any],
        evidence_json: str,
        detail_evidence: Mapping[tuple[str, str], str],
        fallback_empty: bool,
        bounded_call,
        gaps: list[str],
    ) -> list[Any]:
        requested_fields = [] if fallback_empty else _repairable_field_names(raw_fields, evidence_payload)
        for _ in range(2):
            if not requested_fields:
                break
            try:
                repaired = bounded_call(
                    _scope={"stage": "field_repair", "model": candidate.get("model"), "fields": requested_fields},
                    system_prompt=FIELD_GAP_PROMPT,
                    user_prompt=(
                        f"采购场景：{scenario}\ncandidate：{candidate_json}\n"
                        f"requested_fields：{json.dumps(requested_fields, ensure_ascii=False)}\n"
                        f"current_fields：{json.dumps([f for f in raw_fields if isinstance(f, Mapping) and f.get('field_name') in requested_fields], ensure_ascii=False)}\n"
                        f"候选级有界证据 JSON：\n{evidence_json}"
                    ),
                )
            except DeepSeekError as exc:
                gaps.append("部分参数补提取未完成：" + exc.message)
                break
            updated_fields = _merge_repaired_fields(
                raw_fields,
                repaired.get("fields", []) if isinstance(repaired, Mapping) else [],
                requested_fields,
                detail_evidence,
            )
            remaining_fields = _repairable_field_names(updated_fields, evidence_payload)
            raw_fields = updated_fields
            if remaining_fields == requested_fields:
                break
            requested_fields = remaining_fields
        return raw_fields

    def _normalize(self, raw: Mapping[str, Any]) -> dict[str, Any]:
        return _normalize_analysis_output(raw)


__all__ = ["DeepSeekAnalysisService", "AnalysisValidationError", "CANONICAL_FIELD_NAMES"]
