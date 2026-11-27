from typing import Any


def requirement_response(spec: Any) -> dict[str, Any]:
    """统一 API/CLI 输出，避免暴露 dataclass 实现细节。"""
    return spec.to_dict()
