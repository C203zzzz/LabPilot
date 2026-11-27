"""Pure-SQL cross-task historical document & quote index (no vector store).

Stores task-independent, content-hash-deduplicated documents, historical quotes,
and technical parameter facts in a dedicated SQLite database alongside
``app.sqlite3`` (default ``knowledge_index.sqlite3``), keeping ``app.sqlite3``'s
18 task-scoped table contract untouched.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any

from backend.services.procurement_rules import brand_identity, identity

SCHEMA = """
CREATE TABLE IF NOT EXISTS index_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS indexed_documents (
    content_hash TEXT PRIMARY KEY,
    doc_id       TEXT NOT NULL,
    task_id      TEXT NOT NULL,
    source_name  TEXT NOT NULL,
    category     TEXT NOT NULL DEFAULT '',
    brand        TEXT NOT NULL DEFAULT '',
    brand_key    TEXT NOT NULL DEFAULT '',
    model        TEXT NOT NULL DEFAULT '',
    model_key    TEXT NOT NULL DEFAULT '',
    summary_text TEXT NOT NULL DEFAULT '',
    char_count   INTEGER NOT NULL DEFAULT 0,
    indexed_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_kdoc_model ON indexed_documents(model_key, brand_key);
CREATE INDEX IF NOT EXISTS ix_kdoc_task  ON indexed_documents(task_id);

CREATE TABLE IF NOT EXISTS indexed_quotes (
    quote_key     TEXT PRIMARY KEY,
    task_id       TEXT NOT NULL,
    run_id        TEXT NOT NULL DEFAULT '',
    source_kind   TEXT NOT NULL,
    brand         TEXT NOT NULL DEFAULT '',
    brand_key     TEXT NOT NULL DEFAULT '',
    model         TEXT NOT NULL DEFAULT '',
    model_key     TEXT NOT NULL DEFAULT '',
    category      TEXT NOT NULL DEFAULT '',
    supplier      TEXT NOT NULL DEFAULT '',
    unit_price    REAL NOT NULL,
    currency      TEXT NOT NULL DEFAULT 'CNY',
    quantity      REAL,
    deal_date     TEXT NOT NULL DEFAULT '',
    url           TEXT NOT NULL DEFAULT '',
    excerpt       TEXT NOT NULL DEFAULT '',
    configuration TEXT NOT NULL DEFAULT '',
    warranty      TEXT NOT NULL DEFAULT '',
    delivery      TEXT NOT NULL DEFAULT '',
    content_hash  TEXT NOT NULL DEFAULT '',
    indexed_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_kquote_model    ON indexed_quotes(model_key, brand_key);
CREATE INDEX IF NOT EXISTS ix_kquote_supplier ON indexed_quotes(supplier);
CREATE INDEX IF NOT EXISTS ix_kquote_date     ON indexed_quotes(deal_date);
CREATE INDEX IF NOT EXISTS ix_kquote_task     ON indexed_quotes(task_id);

CREATE TABLE IF NOT EXISTS indexed_facts (
    fact_key     TEXT PRIMARY KEY,
    task_id      TEXT NOT NULL,
    brand        TEXT NOT NULL DEFAULT '',
    brand_key    TEXT NOT NULL DEFAULT '',
    model        TEXT NOT NULL DEFAULT '',
    model_key    TEXT NOT NULL DEFAULT '',
    field_name   TEXT NOT NULL DEFAULT '',
    field_value  TEXT NOT NULL DEFAULT '',
    source_name  TEXT NOT NULL DEFAULT '',
    content_hash TEXT NOT NULL DEFAULT '',
    indexed_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_kfact_model ON indexed_facts(model_key, brand_key);
CREATE INDEX IF NOT EXISTS ix_kfact_task  ON indexed_facts(task_id);
"""

FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(
    record_type,
    record_key UNINDEXED,
    task_id UNINDEXED,
    model,
    brand,
    category,
    supplier,
    body_text
);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe_identity(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return identity(text)
    except (TypeError, ValueError):
        return text.upper()


def _safe_brand_identity(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return brand_identity(text)
    except (TypeError, ValueError):
        return text.casefold()


def _safe_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None or value == "":
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    return num if num > 0 else None


def _text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def _compact_terms(value: Any) -> str:
    if value is None or value == "" or value == "unknown":
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)[:300]
    return str(value).strip()[:300]


def _quote_stats(prices: list[float], elapsed_ms: float) -> dict[str, Any]:
    if not prices:
        return {
            "count": 0,
            "min_price": None,
            "median_price": None,
            "max_price": None,
            "elapsed_ms": round(elapsed_ms, 2),
        }
    return {
        "count": len(prices),
        "min_price": round(min(prices), 2),
        "median_price": round(float(statistics.median(prices)), 2),
        "max_price": round(max(prices), 2),
        "elapsed_ms": round(elapsed_ms, 2),
    }


def _fts_match_tokens(keyword: str) -> str:
    tokens = [part.strip() for part in keyword.replace('"', " ").split() if part.strip()]
    if not tokens:
        return ""
    return " AND ".join('"' + token.replace('"', '""') + '"' for token in tokens)


class SQLKnowledgeIndex:
    """Thread-safe pure-SQL cross-task knowledge index with exact model filtering."""

    def __init__(self, path: str | Path) -> None:
        self._lock = RLock()
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        try:
            self.db.executescript(FTS_SCHEMA)
            self._has_fts = True
        except sqlite3.OperationalError:
            self._has_fts = False
        self.db.commit()

    def close(self) -> None:
        with self._lock:
            self.db.close()

    def _set_meta(self, key: str, value: str) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO index_meta(key, value) VALUES (?, ?)",
            (key, value),
        )

    def _get_meta(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM index_meta WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else None

    def _upsert_fts(
        self,
        record_type: str,
        record_key: str,
        task_id: str,
        model: str,
        brand: str,
        category: str,
        supplier: str,
        body_text: str,
    ) -> None:
        if not self._has_fts:
            return
        self.db.execute(
            "DELETE FROM knowledge_fts WHERE record_type = ? AND record_key = ?",
            (record_type, record_key),
        )
        self.db.execute(
            "INSERT INTO knowledge_fts(record_type, record_key, task_id, model, brand, category, supplier, body_text)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (record_type, record_key, task_id, model, brand, category, supplier, body_text),
        )

    def _fts_keys(self, record_type: str, keyword: str) -> set[str] | None:
        if not keyword.strip():
            return None
        if not self._has_fts:
            return None
        expr = _fts_match_tokens(keyword)
        if not expr:
            return None
        try:
            rows = self.db.execute(
                "SELECT record_key FROM knowledge_fts WHERE record_type = ? AND knowledge_fts MATCH ?",
                (record_type, expr),
            ).fetchall()
            return {str(r["record_key"]) for r in rows}
        except sqlite3.OperationalError:
            return None

    def delete_task(self, task_id: str) -> None:
        with self._lock:
            self.db.execute("DELETE FROM indexed_quotes WHERE task_id = ?", (task_id,))
            self.db.execute("DELETE FROM indexed_facts WHERE task_id = ?", (task_id,))
            self.db.execute("DELETE FROM indexed_documents WHERE task_id = ?", (task_id,))
            if self._has_fts:
                self.db.execute("DELETE FROM knowledge_fts WHERE task_id = ?", (task_id,))
            self.db.commit()

    def _task_context(self, repo, task_id: str) -> dict[str, Any]:
        task = repo.get("tasks", task_id) or {}
        req = repo.get("requirements", task_id) or {}
        candidates = repo.list("candidates", task_id)
        reports = repo.list("reports", task_id)
        records = repo.list("procurement_records", task_id)
        documents = repo.list("documents", task_id)
        facts = repo.list("facts", task_id)
        replies = repo.list("supplier_replies", task_id)

        category = str(req.get("category") or task.get("category") or "").strip()
        primary_brand = ""
        primary_model = ""
        for rec in reversed(sorted(records, key=lambda r: r.get("created_at") or "")):
            if rec.get("kind") == "baseline":
                category = category or str(rec.get("category") or "").strip()
                primary_brand = primary_brand or str(rec.get("brand") or "").strip()
                primary_model = primary_model or str(rec.get("model") or "").strip()
                break
        if not primary_model and candidates:
            primary_brand = str(candidates[0].get("brand") or "").strip()
            primary_model = str(candidates[0].get("model") or "").strip()
        return {
            "task": task,
            "category": category,
            "primary_brand": primary_brand,
            "primary_model": primary_model,
            "candidates": candidates,
            "reports": reports,
            "records": records,
            "documents": documents,
            "facts": facts,
            "replies": replies,
        }

    def sync_task(self, repo, task_id: str) -> dict[str, int]:
        """Synchronize one task's documents, facts, and quotes into the SQL index."""
        ctx = self._task_context(repo, task_id)
        now = _now_iso()
        stats = {"documents_indexed": 0, "documents_deduped": 0, "quotes_indexed": 0, "facts_indexed": 0}
        candidate_lookup: dict[str, tuple[str, str]] = {}
        for c in ctx["candidates"]:
            if c.get("id"):
                candidate_lookup[str(c["id"])] = (str(c.get("brand") or ""), str(c.get("model") or ""))

        with self._lock:
            self.db.execute("DELETE FROM indexed_quotes WHERE task_id = ?", (task_id,))
            self.db.execute("DELETE FROM indexed_facts WHERE task_id = ?", (task_id,))
            self.db.execute("DELETE FROM indexed_documents WHERE task_id = ?", (task_id,))
            if self._has_fts:
                self.db.execute("DELETE FROM knowledge_fts WHERE task_id = ?", (task_id,))

            # 1. Documents (content_hash deduplicated across all tasks)
            for doc in ctx["documents"]:
                text = str(doc.get("extracted_text") or "").strip()
                if not text:
                    pages = doc.get("pages") or []
                    text = "\n".join(str(p.get("text") or "") for p in pages if p.get("text")).strip()
                content_hash = str(doc.get("sha256") or "").strip() or _text_sha256(
                    text or f"{doc.get('source_name', '')}:{doc.get('id', '')}"
                )
                existing = self.db.execute(
                    "SELECT task_id FROM indexed_documents WHERE content_hash = ?",
                    (content_hash,),
                ).fetchone()
                if existing is not None:
                    stats["documents_deduped"] += 1
                    continue
                summary_text = text[:1200] if text else str(doc.get("source_name") or "")
                brand = ctx["primary_brand"]
                model = ctx["primary_model"]
                self.db.execute(
                    "INSERT INTO indexed_documents(content_hash, doc_id, task_id, source_name, category,"
                    " brand, brand_key, model, model_key, summary_text, char_count, indexed_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        content_hash,
                        str(doc.get("id") or ""),
                        task_id,
                        str(doc.get("source_name") or "未命名资料"),
                        ctx["category"],
                        brand,
                        _safe_brand_identity(brand),
                        model,
                        _safe_identity(model),
                        summary_text,
                        len(text),
                        now,
                    ),
                )
                self._upsert_fts(
                    "document",
                    content_hash,
                    task_id,
                    model,
                    brand,
                    ctx["category"],
                    "",
                    f"{doc.get('source_name', '')} {summary_text}",
                )
                stats["documents_indexed"] += 1

            # 2. Quotes from baselines, research runs (prices & alternatives), and supplier replies
            def add_quote(
                *,
                run_id: str,
                source_kind: str,
                brand: str,
                model: str,
                category: str,
                supplier: str,
                unit_price: float,
                currency: str = "CNY",
                quantity: float | None = None,
                deal_date: str = "",
                url: str = "",
                excerpt: str = "",
                configuration: str = "",
                warranty: str = "",
                delivery: str = "",
            ) -> None:
                model_str = str(model or "").strip()
                brand_str = str(brand or "").strip()
                model_key = _safe_identity(model_str)
                brand_key = _safe_brand_identity(brand_str)
                supplier_str = str(supplier or "").strip()
                raw_key = "|".join([
                    task_id, run_id, source_kind, model_key, brand_key,
                    f"{unit_price:.4f}", supplier_str, str(url or "").strip(), str(deal_date or "").strip(),
                ])
                qkey = "q-" + hashlib.sha256(raw_key.encode("utf-8", "replace")).hexdigest()[:24]
                chash = _text_sha256(f"{source_kind}|{model_key}|{unit_price:.4f}|{url}|{excerpt[:200]}")
                self.db.execute(
                    "INSERT OR REPLACE INTO indexed_quotes("
                    "quote_key, task_id, run_id, source_kind, brand, brand_key, model, model_key,"
                    " category, supplier, unit_price, currency, quantity, deal_date, url, excerpt,"
                    " configuration, warranty, delivery, content_hash, indexed_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        qkey, task_id, run_id, source_kind, brand_str, brand_key, model_str, model_key,
                        category, supplier_str, float(unit_price), str(currency or "CNY"),
                        quantity, str(deal_date or ""), str(url or ""), str(excerpt or "")[:600],
                        configuration, warranty, delivery, chash, now,
                    ),
                )
                self._upsert_fts(
                    "quote",
                    qkey,
                    task_id,
                    model_str,
                    brand_str,
                    category,
                    supplier_str,
                    f"{excerpt} {configuration} {warranty} {delivery}",
                )
                stats["quotes_indexed"] += 1

            for rec in ctx["records"]:
                kind = rec.get("kind")
                if kind == "baseline":
                    price = _safe_float(rec.get("supplier_price"))
                    if price is not None and rec.get("model"):
                        add_quote(
                            run_id=str(rec.get("id") or ""),
                            source_kind="supplier_quote",
                            brand=str(rec.get("brand") or ""),
                            model=str(rec.get("model") or ""),
                            category=str(rec.get("category") or ctx["category"]),
                            supplier=str(rec.get("supplier") or rec.get("quoted_brand") or "销售报价"),
                            unit_price=price,
                            currency=str(rec.get("currency") or "CNY"),
                            quantity=_safe_float(rec.get("quantity")),
                            deal_date=str(rec.get("created_at") or "")[:10],
                            excerpt=str(rec.get("configuration") or rec.get("scenario") or ""),
                            configuration=_compact_terms(rec.get("configuration")),
                            warranty=_compact_terms(rec.get("warranty")),
                            delivery=_compact_terms(rec.get("delivery")),
                        )
                elif kind == "research":
                    base = rec.get("baseline") or {}
                    cat = str(base.get("category") or ctx["category"])
                    run_id = str(rec.get("id") or "")
                    for p in rec.get("prices") or []:
                        if not isinstance(p, dict):
                            continue
                        price = _safe_float(p.get("unit_price"))
                        if price is None:
                            continue
                        add_quote(
                            run_id=run_id,
                            source_kind="memory_price" if p.get("from_memory") else "research_price",
                            brand=str(p.get("brand") or base.get("brand") or ""),
                            model=str(p.get("model") or base.get("model") or ""),
                            category=cat,
                            supplier=str(p.get("supplier") or p.get("winner") or p.get("purchaser") or p.get("source_type") or ""),
                            unit_price=price,
                            currency=str(p.get("currency") or "CNY"),
                            quantity=_safe_float(p.get("quantity")),
                            deal_date=str(p.get("date") or p.get("deal_date") or ""),
                            url=str(p.get("url") or ""),
                            excerpt=str(p.get("excerpt") or ""),
                            configuration=_compact_terms(p.get("configuration") or base.get("configuration")),
                            warranty=_compact_terms(p.get("warranty") or base.get("warranty")),
                            delivery=_compact_terms(p.get("delivery") or base.get("delivery")),
                        )
                    for alt in rec.get("alternatives") or []:
                        if not isinstance(alt, dict) or not alt.get("model"):
                            continue
                        alt_prices = list(alt.get("prices") or [])
                        if isinstance(alt.get("price"), dict):
                            alt_prices.append(alt["price"])
                        elif _safe_float(alt.get("unit_price")) is not None:
                            alt_prices.append({"unit_price": alt.get("unit_price"), "url": alt.get("url")})
                        for ap in alt_prices:
                            if not isinstance(ap, dict):
                                continue
                            price = _safe_float(ap.get("unit_price"))
                            if price is None:
                                continue
                            add_quote(
                                run_id=run_id,
                                source_kind="alternative_price",
                                brand=str(alt.get("brand") or ""),
                                model=str(alt.get("model") or ""),
                                category=cat,
                                supplier=str(ap.get("supplier") or ap.get("winner") or alt.get("brand") or ""),
                                unit_price=price,
                                currency=str(ap.get("currency") or "CNY"),
                                quantity=_safe_float(ap.get("quantity")),
                                deal_date=str(ap.get("date") or ""),
                                url=str(ap.get("url") or ""),
                                excerpt=str(ap.get("excerpt") or ""),
                                configuration=_compact_terms(alt.get("configuration")),
                            )

            for reply in ctx["replies"]:
                price = _safe_float(reply.get("unit_price") or reply.get("supplier_price"))
                if price is not None and ctx["primary_model"]:
                    add_quote(
                        run_id=str(reply.get("reply_id") or ""),
                        source_kind="supplier_reply",
                        brand=ctx["primary_brand"],
                        model=ctx["primary_model"],
                        category=ctx["category"],
                        supplier=str(reply.get("supplier_name") or reply.get("responder") or "销售回复"),
                        unit_price=price,
                        currency=str(reply.get("currency") or "CNY"),
                        deal_date=str(reply.get("created_at") or "")[:10],
                        excerpt=str(reply.get("raw_text") or reply.get("reply_notes") or ""),
                        configuration=_compact_terms(reply.get("configuration")),
                        warranty=_compact_terms(reply.get("warranty")),
                        delivery=_compact_terms(reply.get("delivery")),
                    )

            # 3. Facts & DeepSeek extracted fields
            for fact in ctx["facts"]:
                cid = str(fact.get("candidate_id") or "")
                brand, model = candidate_lookup.get(cid, (ctx["primary_brand"], ctx["primary_model"]))
                fname = str(fact.get("field_name") or "").strip()
                fval = _compact_terms(fact.get("value"))
                if not fname or not fval:
                    continue
                fkey = "f-" + hashlib.sha256(f"{task_id}|{cid}|{fname}|{fval}".encode()).hexdigest()[:24]
                self.db.execute(
                    "INSERT OR REPLACE INTO indexed_facts("
                    "fact_key, task_id, brand, brand_key, model, model_key, field_name, field_value,"
                    " source_name, content_hash, indexed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        fkey, task_id, brand, _safe_brand_identity(brand), model, _safe_identity(model),
                        fname, fval, str(fact.get("reviewed_by") or "fact"), _text_sha256(f"{fname}:{fval}"), now,
                    ),
                )
                self._upsert_fts("fact", fkey, task_id, model, brand, ctx["category"], "", f"{fname} {fval}")
                stats["facts_indexed"] += 1

            for rep in ctx["reports"]:
                if rep.get("kind") != "deepseek_analysis":
                    continue
                for cand in rep.get("candidates") or []:
                    brand = str(cand.get("brand") or "")
                    model = str(cand.get("model") or "")
                    if not model:
                        continue
                    for field in cand.get("fields") or []:
                        fname = str(field.get("field_name") or "")
                        fval = _compact_terms(field.get("value"))
                        if not fname or not fval or field.get("status") in {"unknown", "rejected"}:
                            continue
                        fkey = "df-" + hashlib.sha256(f"{task_id}|{model}|{fname}|{fval}".encode()).hexdigest()[:24]
                        self.db.execute(
                            "INSERT OR REPLACE INTO indexed_facts("
                            "fact_key, task_id, brand, brand_key, model, model_key, field_name, field_value,"
                            " source_name, content_hash, indexed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (
                                fkey, task_id, brand, _safe_brand_identity(brand), model, _safe_identity(model),
                                fname, fval, "deepseek_analysis", _text_sha256(f"{fname}:{fval}"), now,
                            ),
                        )
                        self._upsert_fts("fact", fkey, task_id, model, brand, ctx["category"], "", f"{fname} {fval}")
                        stats["facts_indexed"] += 1

            self._set_meta("last_synced_at", now)
            self.db.commit()
        return stats

    def _sync_price_memory(self, price_memory_path: Path | None) -> int:
        if not price_memory_path or not Path(price_memory_path).is_file():
            return 0
        count = 0
        now = _now_iso()
        src_conn = sqlite3.connect(f"file:{Path(price_memory_path).as_posix()}?mode=ro", uri=True)
        src_conn.row_factory = sqlite3.Row
        try:
            rows = src_conn.execute("SELECT * FROM price_memory").fetchall()
        except sqlite3.Error:
            rows = []
        finally:
            src_conn.close()

        with self._lock:
            self.db.execute("DELETE FROM indexed_quotes WHERE task_id = '__price_memory__'")
            if self._has_fts:
                self.db.execute("DELETE FROM knowledge_fts WHERE task_id = '__price_memory__'")
            for row in rows:
                price = _safe_float(row["unit_price"])
                model_str = str(row["model"] or "").strip()
                if price is None or not model_str:
                    continue
                brand_str = str(row["brand"] or "").strip()
                mkey = str(row["model_key"] or _safe_identity(model_str))
                bkey = str(row["brand_key"] or _safe_brand_identity(brand_str))
                url = str(row["url"] or "")
                qkey = "pm-" + hashlib.sha256(f"{mkey}|{bkey}|{price:.4f}|{url}".encode()).hexdigest()[:24]
                excerpt = str(row["excerpt"] or "")[:600]
                self.db.execute(
                    "INSERT OR REPLACE INTO indexed_quotes("
                    "quote_key, task_id, run_id, source_kind, brand, brand_key, model, model_key,"
                    " category, supplier, unit_price, currency, quantity, deal_date, url, excerpt,"
                    " configuration, warranty, delivery, content_hash, indexed_at)"
                    " VALUES (?, '__price_memory__', '', 'price_memory', ?, ?, ?, ?, '', ?, ?, ?, ?, ?, ?, ?, '', '', '', ?, ?)",
                    (
                        qkey, brand_str, bkey, model_str, mkey, str(row["source_type"] or "price_memory"),
                        float(price), str(row["currency"] or "CNY"), _safe_float(row["quantity"]),
                        str(row["deal_date"] or ""), url, excerpt,
                        _text_sha256(f"pm|{mkey}|{price:.4f}|{url}"), now,
                    ),
                )
                self._upsert_fts("quote", qkey, "__price_memory__", model_str, brand_str, "", "", excerpt)
                count += 1
            self.db.commit()
        return count

    def sync_from_repository(
        self,
        repo,
        *,
        price_memory_path: Path | None = None,
    ) -> dict[str, Any]:
        """Rebuild the entire pure-SQL index from all tasks and price memory."""
        totals = {
            "tasks_synced": 0,
            "documents_indexed": 0,
            "documents_deduped": 0,
            "quotes_indexed": 0,
            "facts_indexed": 0,
            "price_memory_synced": 0,
        }
        pm_path = price_memory_path or (Path(repo.path).parent / "price_memory.sqlite3")
        with self._lock:
            self.db.execute("DELETE FROM indexed_documents")
            self.db.execute("DELETE FROM indexed_quotes")
            self.db.execute("DELETE FROM indexed_facts")
            if self._has_fts:
                self.db.execute("DELETE FROM knowledge_fts")
            self.db.commit()

        for task in repo.list_tasks():
            tid = str(task.get("id") or "")
            if not tid:
                continue
            part = self.sync_task(repo, tid)
            totals["tasks_synced"] += 1
            for k in ("documents_indexed", "documents_deduped", "quotes_indexed", "facts_indexed"):
                totals[k] += part[k]
        totals["price_memory_synced"] = self._sync_price_memory(pm_path)
        with self._lock:
            self._set_meta("last_synced_at", _now_iso())
            self.db.commit()
        return totals

    def ensure_initialized(self, repo) -> None:
        if self._get_meta("last_synced_at") is None and repo.list_tasks():
            self.sync_from_repository(repo)

    def summary(self) -> dict[str, Any]:
        with self._lock:
            docs = self.db.execute("SELECT COUNT(*) FROM indexed_documents").fetchone()[0]
            quotes = self.db.execute("SELECT COUNT(*) FROM indexed_quotes").fetchone()[0]
            facts = self.db.execute("SELECT COUNT(*) FROM indexed_facts").fetchone()[0]
            models = self.db.execute(
                "SELECT COUNT(DISTINCT model_key) FROM indexed_quotes WHERE model_key <> ''"
            ).fetchone()[0]
            suppliers = self.db.execute(
                "SELECT COUNT(DISTINCT supplier) FROM indexed_quotes WHERE supplier <> ''"
            ).fetchone()[0]
            last_synced = self._get_meta("last_synced_at")
        return {
            "documents": int(docs),
            "quotes": int(quotes),
            "facts": int(facts),
            "models": int(models),
            "suppliers": int(suppliers),
            "last_synced_at": last_synced,
            "fts_enabled": self._has_fts,
            "database_path": str(self.path),
        }

    def _query_xlyg_quotes(
        self,
        *,
        xlyg_path: Path | None,
        model: str = "",
        brand: str = "",
        category: str = "",
        supplier: str = "",
        keyword: str = "",
        date_from: str = "",
        date_to: str = "",
        exact_model: bool = False,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        if not xlyg_path or not Path(xlyg_path).is_file():
            return []
        clauses = ["verified = 1", "unit_price > 0"]
        params: list[Any] = []
        model_key = _safe_identity(model)
        brand_key = _safe_brand_identity(brand)
        if model.strip():
            # SQL prefilter on stripped alphanumeric prefix, followed by strict Python identity match.
            compact = "".join(ch for ch in model.strip() if ch.isalnum())[:16]
            if compact:
                clauses.append("(REPLACE(REPLACE(REPLACE(UPPER(model), '-', ''), ' ', ''), '_', '') LIKE ? OR model LIKE ?)")
                params.extend([f"%{compact.upper()}%", f"%{model.strip()}%"])
            else:
                clauses.append("model LIKE ?")
                params.append(f"%{model.strip()}%")
        if brand.strip():
            clauses.append("brand LIKE ?")
            params.append(f"%{brand.strip()}%")
        if category.strip():
            clauses.append("category LIKE ?")
            params.append(f"%{category.strip()}%")
        if supplier.strip():
            clauses.append("supplier LIKE ?")
            params.append(f"%{supplier.strip()}%")
        if date_from:
            clauses.append("deal_date >= ?")
            params.append(date_from)
        if date_to:
            clauses.append("deal_date <= ?")
            params.append(date_to)
        if keyword.strip():
            clauses.append("(product_name LIKE ? OR goods_name LIKE ? OR excerpt LIKE ? OR model LIKE ?)")
            kw = f"%{keyword.strip()}%"
            params.extend([kw, kw, kw, kw])

        conn = sqlite3.connect(f"file:{Path(xlyg_path).as_posix()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        items: list[dict[str, Any]] = []
        has_filter = bool(model.strip() or brand.strip() or category.strip() or supplier.strip() or keyword.strip())
        try:
            order_sql = (
                " ORDER BY deal_date DESC LIMIT ?"
                if has_filter
                else " ORDER BY CASE WHEN (COALESCE(brand,'') <> '' OR COALESCE(category,'') <> '' OR unit_price >= 500) THEN 0 ELSE 1 END, deal_date DESC, unit_price DESC LIMIT ?"
            )
            sql = (
                "SELECT order_no, line_no, brand, model, category, supplier, purchaser,"
                " unit_price, quantity, deal_date, detail_url, excerpt"
                " FROM xlyg_item WHERE " + " AND ".join(clauses) + order_sql
            )
            for row in conn.execute(sql, [*params, max(limit * 3, 200)]):
                row_model = str(row["model"] or "")
                row_mkey = _safe_identity(row_model)
                row_bkey = _safe_brand_identity(row["brand"] or "")
                if model_key:
                    if exact_model and row_mkey != model_key:
                        continue
                    if not exact_model and model_key != row_mkey and model_key not in row_mkey:
                        continue
                if brand_key and row_bkey and brand_key != row_bkey and brand_key not in row_bkey:
                    continue
                items.append({
                    "quote_key": f"xlyg-{row['order_no']}-{row['line_no']}",
                    "task_id": "",
                    "run_id": "",
                    "source_kind": "xlyg_award",
                    "brand": str(row["brand"] or ""),
                    "brand_key": row_bkey,
                    "model": row_model,
                    "model_key": row_mkey,
                    "category": str(row["category"] or ""),
                    "supplier": str(row["supplier"] or row["purchaser"] or "小零易购成交"),
                    "unit_price": float(row["unit_price"]),
                    "currency": "CNY",
                    "quantity": _safe_float(row["quantity"]),
                    "deal_date": str(row["deal_date"] or ""),
                    "url": str(row["detail_url"] or ""),
                    "excerpt": str(row["excerpt"] or "")[:400],
                    "configuration": "",
                    "warranty": "",
                    "delivery": "",
                })
                if len(items) >= limit:
                    break
        except sqlite3.Error:
            return []
        finally:
            conn.close()
        return items

    def search_quotes(
        self,
        *,
        model: str = "",
        brand: str = "",
        category: str = "",
        supplier: str = "",
        keyword: str = "",
        date_from: str = "",
        date_to: str = "",
        exact_model: bool = False,
        limit: int = 100,
        include_xlyg: bool = False,
        xlyg_path: Path | None = None,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        model_key = _safe_identity(model)
        brand_key = _safe_brand_identity(brand)
        clauses = ["1=1"]
        params: list[Any] = []
        if model_key:
            if exact_model:
                clauses.append("model_key = ?")
                params.append(model_key)
            else:
                clauses.append("(model_key = ? OR model_key LIKE ? OR model LIKE ?)")
                params.extend([model_key, f"%{model_key}%", f"%{model.strip()}%"])
        if brand_key:
            clauses.append("(brand_key = ? OR brand_key LIKE ? OR brand LIKE ?)")
            params.extend([brand_key, f"%{brand_key}%", f"%{brand.strip()}%"])
        if category.strip():
            clauses.append("category LIKE ?")
            params.append(f"%{category.strip()}%")
        if supplier.strip():
            clauses.append("supplier LIKE ?")
            params.append(f"%{supplier.strip()}%")
        if date_from:
            clauses.append("deal_date >= ?")
            params.append(date_from)
        if date_to:
            clauses.append("deal_date <= ?")
            params.append(date_to)

        with self._lock:
            fts_keys = self._fts_keys("quote", keyword) if keyword.strip() else None
            if keyword.strip() and fts_keys is None:
                kw = f"%{keyword.strip()}%"
                clauses.append("(model LIKE ? OR brand LIKE ? OR supplier LIKE ? OR excerpt LIKE ? OR configuration LIKE ?)")
                params.extend([kw, kw, kw, kw, kw])
            has_filter = bool(model_key or brand_key or category.strip() or supplier.strip() or keyword.strip())
            order_sql = (
                " ORDER BY CASE WHEN model_key = ? THEN 0 ELSE 1 END, deal_date DESC, unit_price ASC LIMIT ?"
                if has_filter
                else " ORDER BY CASE WHEN (COALESCE(brand,'') <> '' OR COALESCE(category,'') <> '' OR unit_price >= 500) THEN 0 ELSE 1 END, deal_date DESC, unit_price DESC LIMIT ?"
            )
            sql = "SELECT * FROM indexed_quotes WHERE " + " AND ".join(clauses) + order_sql
            query_params = [*params, model_key, limit * 2] if has_filter else [*params, limit * 2]
            rows = self.db.execute(sql, query_params).fetchall()

        items: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            if fts_keys is not None and item["quote_key"] not in fts_keys:
                continue
            items.append(item)
            if len(items) >= limit:
                break

        if include_xlyg and len(items) < limit:
            xlyg_items = self._query_xlyg_quotes(
                xlyg_path=xlyg_path,
                model=model,
                brand=brand,
                category=category,
                supplier=supplier,
                keyword=keyword,
                date_from=date_from,
                date_to=date_to,
                exact_model=exact_model,
                limit=limit - len(items),
            )
            items.extend(xlyg_items)

        prices = [float(i["unit_price"]) for i in items if isinstance(i.get("unit_price"), (int, float))]
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        return {
            "items": items,
            "total": len(items),
            "stats": _quote_stats(prices, elapsed_ms),
            "filters": {
                "model": model,
                "brand": brand,
                "category": category,
                "supplier": supplier,
                "keyword": keyword,
                "exact_model": exact_model,
                "include_xlyg": include_xlyg,
            },
        }

    def compare_suppliers(
        self,
        *,
        model: str = "",
        brand: str = "",
        category: str = "",
        supplier: str = "",
        keyword: str = "",
        date_from: str = "",
        date_to: str = "",
        exact_model: bool = False,
        include_xlyg: bool = False,
        xlyg_path: Path | None = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        quotes_res = self.search_quotes(
            model=model,
            brand=brand,
            category=category,
            supplier=supplier,
            keyword=keyword,
            date_from=date_from,
            date_to=date_to,
            exact_model=exact_model,
            include_xlyg=include_xlyg,
            xlyg_path=xlyg_path,
            limit=limit,
        )
        groups: dict[tuple[str, str, str], dict[str, Any]] = {}
        for q in quotes_res["items"]:
            sup = str(q.get("supplier") or "").strip() or "未署名来源"
            br = str(q.get("brand") or "").strip()
            md = str(q.get("model") or "").strip()
            gkey = (sup, _safe_brand_identity(br), _safe_identity(md))
            grp = groups.setdefault(gkey, {
                "supplier": sup,
                "brand": br,
                "model": md,
                "category": str(q.get("category") or ""),
                "prices": [],
                "dates": [],
                "currencies": set(),
                "source_kinds": set(),
                "terms": set(),
                "samples": [],
            })
            grp["prices"].append(float(q["unit_price"]))
            if q.get("deal_date"):
                grp["dates"].append(str(q["deal_date"]))
            if q.get("currency"):
                grp["currencies"].add(str(q["currency"]))
            if q.get("source_kind"):
                grp["source_kinds"].add(str(q["source_kind"]))
            for label, field in (("配置", "configuration"), ("保修", "warranty"), ("交期", "delivery")):
                val = str(q.get(field) or "").strip()
                if val:
                    grp["terms"].add(f"{label}：{val}")
            if len(grp["samples"]) < 5:
                grp["samples"].append({
                    "quote_key": q.get("quote_key"),
                    "unit_price": q.get("unit_price"),
                    "deal_date": q.get("deal_date"),
                    "source_kind": q.get("source_kind"),
                    "url": q.get("url"),
                    "excerpt": q.get("excerpt"),
                })

        rows = []
        all_prices: list[float] = []
        for grp in groups.values():
            plist = grp["prices"]
            all_prices.extend(plist)
            rows.append({
                "supplier": grp["supplier"],
                "brand": grp["brand"],
                "model": grp["model"],
                "category": grp["category"],
                "count": len(plist),
                "min_price": round(min(plist), 2),
                "median_price": round(float(statistics.median(plist)), 2),
                "max_price": round(max(plist), 2),
                "latest_date": max(grp["dates"]) if grp["dates"] else "",
                "currencies": sorted(grp["currencies"]),
                "source_kinds": sorted(grp["source_kinds"]),
                "terms_summary": "；".join(sorted(grp["terms"]))[:300],
                "samples": grp["samples"],
            })
        has_filter = bool(model.strip() or brand.strip() or category.strip() or supplier.strip() or keyword.strip())
        if has_filter:
            rows.sort(key=lambda r: (r["median_price"], -r["count"], r["supplier"]))
        else:
            rows.sort(key=lambda r: (
                0 if (r["brand"] or r["category"] or r["median_price"] >= 500) else 1,
                -(int(r["latest_date"].replace("-", "")[:8]) if r["latest_date"][:4].isdigit() else 0),
                -r["median_price"],
                r["supplier"],
            ))
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        return {
            "items": rows,
            "total": len(rows),
            "stats": _quote_stats(all_prices, elapsed_ms),
        }

    def search_documents(
        self,
        *,
        model: str = "",
        brand: str = "",
        keyword: str = "",
        limit: int = 50,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        model_key = _safe_identity(model)
        brand_key = _safe_brand_identity(brand)
        clauses = ["1=1"]
        params: list[Any] = []
        if model_key:
            clauses.append("(model_key = ? OR model_key LIKE ? OR summary_text LIKE ?)")
            params.extend([model_key, f"%{model_key}%", f"%{model.strip()}%"])
        if brand_key:
            clauses.append("(brand_key = ? OR brand LIKE ? OR summary_text LIKE ?)")
            params.extend([brand_key, f"%{brand.strip()}%", f"%{brand.strip()}%"])

        with self._lock:
            fts_keys = self._fts_keys("document", keyword) if keyword.strip() else None
            if keyword.strip() and fts_keys is None:
                kw = f"%{keyword.strip()}%"
                clauses.append("(source_name LIKE ? OR summary_text LIKE ?)")
                params.extend([kw, kw])
            sql = "SELECT * FROM indexed_documents WHERE " + " AND ".join(clauses) + " ORDER BY indexed_at DESC LIMIT ?"
            rows = self.db.execute(sql, [*params, limit * 2]).fetchall()

        items = []
        for row in rows:
            item = dict(row)
            if fts_keys is not None and item["content_hash"] not in fts_keys:
                continue
            items.append(item)
            if len(items) >= limit:
                break
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        return {"items": items, "total": len(items), "elapsed_ms": round(elapsed_ms, 2)}
