"""M19 task archive snapshots and safe task-state restoration."""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from typing import Any
import copy
import json
import uuid

ARCHIVE_SCHEMA = "m19.task-archive.v1"


def build_snapshot(repo, task_id: str) -> dict[str, Any]:
    task = repo.get("tasks", task_id)
    if not task:
        raise KeyError(task_id)
    # Keep the categories explicit so future tables cannot silently disappear.
    reports = copy.deepcopy(repo.list("reports", task_id))
    candidates = copy.deepcopy(repo.list("candidates", task_id))
    return {
        "task": copy.deepcopy(task),
        "requirement": copy.deepcopy(repo.get("requirements", task_id)),
        "documents": copy.deepcopy(repo.list("documents", task_id)),
        "candidates": candidates,
        "confirmed_products": [x for x in candidates if x.get("status") == "confirmed"],
        "reports": reports,
        "search_runs": copy.deepcopy(repo.list("search_runs", task_id)),
        "source_decisions": copy.deepcopy(repo.list("source_decisions", task_id)),
        "final_decision": copy.deepcopy(repo.list("final_decisions", task_id)),
        "supplier_inquiries": copy.deepcopy(repo.list("supplier_inquiries", task_id)),
        "supplier_replies": copy.deepcopy(repo.list("supplier_replies", task_id)),
        "facts": copy.deepcopy(repo.list("facts", task_id)),
        "workflow_state": copy.deepcopy(repo.get("workflow_state", task_id)),
        # Explicit M19 categories remain stable even when upstream modules
        # have not produced a value yet. Report payloads are preserved intact.
        "comparisons": [x.get("comparisons", []) for x in reports if isinstance(x, dict)],
        "questions": [x.get("questions") for x in reports if isinstance(x, dict) and x.get("questions") is not None],
        "final_confirmation": copy.deepcopy(repo.list("final_decisions", task_id)),
        "history": [v.to_dict() for v in repo.list_versions(task_id)],
        "rag_chunks": copy.deepcopy(repo.list_rag_chunks(task_id)),
    }


def create_archive(repo, task_id: str, *, note: str | None = None) -> dict[str, Any]:
    snapshot = build_snapshot(repo, task_id)
    created_at = datetime.now(timezone.utc).isoformat()
    archive_id = "archive-" + uuid.uuid4().hex[:12]
    payload = {
        "archive_id": archive_id,
        "task_id": task_id,
        "schema": ARCHIVE_SCHEMA,
        "created_at": created_at,
        "note": note,
        "snapshot": snapshot,
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode()
    payload["content_sha256"] = sha256(canonical).hexdigest()
    repo.save_archive(archive_id, task_id, payload, created_at)
    return payload


def restore_archive(repo, archive: dict[str, Any]) -> dict[str, Any]:
    if archive.get("schema") != ARCHIVE_SCHEMA:
        raise ValueError("unsupported archive schema")
    snapshot = archive.get("snapshot")
    if not isinstance(snapshot, dict) or not snapshot.get("task"):
        raise ValueError("invalid archive snapshot")
    task_id = archive.get("task_id")
    repo.replace_task_state(task_id, snapshot)
    repo.replace_rag_chunks(task_id, snapshot.get("rag_chunks", []))
    return build_snapshot(repo, task_id)


__all__ = ["ARCHIVE_SCHEMA", "build_snapshot", "create_archive", "restore_archive"]
