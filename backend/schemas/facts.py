"""JSON serialization for M06 contracts."""
from dataclasses import asdict
from datetime import datetime
from enum import Enum
from typing import Any

def _json(v: Any):
    if isinstance(v, Enum): return v.value
    if isinstance(v, datetime): return v.isoformat()
    if isinstance(v, list): return [_json(x) for x in v]
    if isinstance(v, tuple): return [_json(x) for x in v]
    if isinstance(v, dict): return {k: _json(x) for k,x in v.items()}
    return v

def fact_to_dict(value): return _json(asdict(value))
def evidence_to_dict(value): return _json(asdict(value))

__all__ = ["fact_to_dict", "evidence_to_dict"]
