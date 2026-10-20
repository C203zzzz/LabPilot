"""Evidence-bound candidate discovery from confirmed fetched web sources."""
from __future__ import annotations

from typing import Any, Mapping

from backend.domain.candidates import CandidateSource, ProductCandidate, merge_candidates
from backend.domain.search import normalize_url
from backend.infrastructure.deepseek import DeepSeekClient


SYSTEM_PROMPT = """你是实验室仪器候选发现器。只根据提供的已抓取网页正文识别产品品牌、系列、完整型号和配置。
返回 JSON 对象，包含 candidates 数组。每个候选必须包含 brand、series、model、configuration、sources。
sources 每项必须包含 url 和 excerpt，url 必须来自输入来源；禁止引用搜索摘要，禁止使用模型常识补全型号。
无法从正文定位完整型号时不要输出该候选。所有候选都只是 pending_confirmation 草稿，不能标记为 confirmed。"""


class WebCandidateDiscoveryService:
    def __init__(self, client: DeepSeekClient):
        self.client = client

    def discover(self, *, scenario: str, sources: list[Mapping[str, Any]]) -> list[ProductCandidate]:
        usable = [source for source in sources if source.get("body_status") == "fetched" and str(source.get("body_text") or "").strip()]
        if not usable:
            return []
        # 地址本身不合法的来源不能进这张表：`normalize_url` 对解析不了的串抛
        # ``ValueError``，而列表推导里一个坏地址会让**整轮候选发现**抛错（下面
        # 引用那一处的 ``try/except`` 早就有了，这里漏了）。
        source_map = {}
        for source in usable:
            try:
                source_map[normalize_url(str(source["url"]))] = source
            except ValueError:
                continue
        prompt_sources = [{
            "url": url,
            "title": source.get("title"),
            "source_type": source.get("source_type"),
            "body": str(source.get("body_text") or "")[:30000],
        } for url, source in source_map.items()]
        raw = self.client.analyze(system_prompt=SYSTEM_PROMPT, user_prompt=f"采购场景：{scenario}\n已确认来源：{prompt_sources}")
        candidates: list[ProductCandidate] = []
        for item in raw.get("candidates", []) if isinstance(raw.get("candidates"), list) else []:
            if not isinstance(item, Mapping) or not str(item.get("model") or "").strip():
                continue
            citations = []
            for citation in item.get("sources", []) if isinstance(item.get("sources"), list) else []:
                if not isinstance(citation, Mapping):
                    continue
                try:
                    url = normalize_url(str(citation.get("url") or ""))
                except ValueError:
                    continue
                source = source_map.get(url)
                excerpt = str(citation.get("excerpt") or "").strip()
                if not source or not excerpt or excerpt not in str(source.get("body_text") or ""):
                    continue
                citations.append(CandidateSource(url, "body", excerpt[:2000], str(source.get("source_type") or "official_web")))
            if not citations:
                continue
            candidates.append(ProductCandidate(
                str(item.get("brand") or "").strip() or None,
                str(item.get("series") or "").strip() or None,
                str(item["model"]).strip(),
                str(item.get("configuration") or "").strip() or None,
                citations,
            ))
        return merge_candidates(candidates)


__all__ = ["WebCandidateDiscoveryService"]
