from __future__ import annotations


class ModuleValidationError(ValueError):
    """跨模块输入不符合约定时使用的统一错误。"""

    def __init__(self, message: str, *, code: str = "invalid_input", field: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.field = field

    def to_dict(self) -> dict[str, str]:
        result = {"code": self.code, "message": str(self)}
        if self.field:
            result["field"] = self.field
        return result
