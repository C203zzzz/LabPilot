from dataclasses import asdict
from backend.domain.search import SearchQuery, SearchResult, SourceRecord

def query_to_dict(v: SearchQuery) -> dict:
    d = asdict(v)
    d["target_source_types"] = [x.value for x in v.target_source_types]
    return d
def result_to_dict(v: SearchResult) -> dict: return asdict(v)
def source_to_dict(v: SourceRecord) -> dict:
    d = asdict(v)
    for k in ("retrieved_at", "crawled_at"):
        if d[k] is not None: d[k] = d[k].isoformat()
    d["source_type"] = v.source_type.value
    d["body_status"] = v.body_status.value
    return d
