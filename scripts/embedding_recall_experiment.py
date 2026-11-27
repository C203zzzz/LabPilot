"""Offline comparison of lexical, embedding and hybrid retrieval for M19.

This is an experiment only; it does not change the production RAG path.
Model weights are loaded from HF_HOME (use a temp/cache directory outside git).
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import numpy as np
from sentence_transformers import SentenceTransformer

from backend.domain.rag import DocumentChunk, RetrievalQuery
from backend.services.rag_service import LexicalRetriever


CORPUS = [
    ("d1", "支持 450 nm 光学检测，检测波长范围 340–850 nm。"),
    ("d2", "设备具有恒温培养功能，温度控制范围 4–45 °C。"),
    ("d3", "配备自动加样机械臂，可进行多通道移液。"),
    ("d4", "结果支持导出 CSV、Excel 和 PDF 文件。"),
    ("d5", "96 孔板兼容，支持终点法和动力学检测。"),
    ("d6", "样品室采用密闭结构，适合减少环境光干扰。"),
    ("d7", "最大振荡速度 1200 rpm，轨道直径 3 mm。"),
    ("d8", "仪器提供 USB 和以太网接口，可连接实验室信息系统。"),
    ("d9", "软件支持多用户权限和审计日志。"),
    ("d10", "具备荧光检测模块，适用于核酸定量分析。"),
    ("d11", "厂家提供两年质保和远程技术支持。"),
    ("d12", "试剂耗材采用开放式配置，不绑定专用耗材。"),
]

# Relevant IDs are deliberately defined by the experiment author, not inferred
# from retrieval output. This prevents the model from grading itself.
QUERIES = [
    ("光学检测波长", {"d1"}),
    ("恒温培养温度控制", {"d2"}),
    ("自动移液", {"d3"}),
    ("导出实验结果文件", {"d4"}),
    ("96孔板动力学模式", {"d5"}),
    ("实验室信息系统接口", {"d8"}),
    ("荧光核酸定量", {"d10"}),
    ("售后保修服务", {"d11"}),
    ("精确参数 1200 rpm", {"d7"}),
    ("CSV 格式", {"d4"}),
]


def lexical_rank(chunks, text, top_k):
    rows = LexicalRetriever(chunks).retrieve(RetrievalQuery(text, top_k=top_k))
    return [x.chunk.chunk_id for x in rows]


def embedding_rank(model, texts, query, top_k):
    vectors = model.encode(texts + [query], normalize_embeddings=True, show_progress_bar=False)
    scores = vectors[:-1] @ vectors[-1]
    order = np.argsort(-scores, kind="stable")[:top_k]
    return [CORPUS[int(i)][0] for i in order]


def hybrid_rank(lexical, semantic, top_k):
    # Reciprocal rank fusion; it keeps exact lexical hits influential while
    # allowing semantic-only candidates to enter the result set.
    scores = {}
    for rank, item in enumerate(lexical, 1):
        scores[item] = scores.get(item, 0) + 1 / (60 + rank)
    for rank, item in enumerate(semantic, 1):
        scores[item] = scores.get(item, 0) + 1 / (60 + rank)
    return [k for k, _ in sorted(scores.items(), key=lambda x: (-x[1], x[0]))[:top_k]]


def recall_at(ranked, relevant, k):
    return 1.0 if set(ranked[:k]) & relevant else 0.0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")
    parser.add_argument("--output", type=Path, default=Path("output/embedding_recall_experiment.json"))
    args = parser.parse_args()
    model = SentenceTransformer(args.model)
    chunks = [DocumentChunk(i, i, text) for i, text in CORPUS]
    texts = [text for _, text in CORPUS]
    rows = []
    aggregates = {name: {"recall_at_3": [], "recall_at_5": []} for name in ("lexical", "embedding", "hybrid")}
    for query, relevant in QUERIES:
        lexical = lexical_rank(chunks, query, 5)
        semantic = embedding_rank(model, texts, query, 5)
        hybrid = hybrid_rank(lexical, semantic, 5)
        for name, ranked in (("lexical", lexical), ("embedding", semantic), ("hybrid", hybrid)):
            aggregates[name]["recall_at_3"].append(recall_at(ranked, relevant, 3))
            aggregates[name]["recall_at_5"].append(recall_at(ranked, relevant, 5))
        rows.append({"query": query, "relevant": sorted(relevant), "lexical": lexical,
                     "embedding": semantic, "hybrid": hybrid,
                     "lexical_hit_at_3": bool(set(lexical[:3]) & relevant),
                     "embedding_hit_at_3": bool(set(semantic[:3]) & relevant),
                     "hybrid_hit_at_3": bool(set(hybrid[:3]) & relevant)})
    summary = {name: {metric: round(float(np.mean(values)), 3) for metric, values in metrics.items()}
               for name, metrics in aggregates.items()}
    result = {"model": args.model, "dimension": model.get_embedding_dimension(), "query_count": len(QUERIES),
              "corpus_count": len(CORPUS), "summary": summary, "rows": rows,
              "note": "Synthetic, manually labelled benchmark; semantic hits still require source verification."}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(args.output), "summary": summary}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
