"""Status recording and gap/failure message generation.

消息构造的唯一实现在 ``backend.services.analysis_messages``；这里只做转出，并把
``parameters_status`` 的取值集中在一处。拆分时这里曾自造第二套消息构造器与第二套
状态字符串（``not_in_source`` / ``incomplete``），与主流程的
``not_available_in_source`` / ``extraction_incomplete`` 并存，报告里会出现两种口径。
"""
from __future__ import annotations

from backend.services.analysis_messages import (
    parameter_extraction_incomplete_failure,
    parameter_fallback_failure,
    parameter_fallback_gap,
    parameters_absent_gap,
)

#: 候选 ``parameters_status`` 的取值，必须与拆分前的 deepseek_analysis 逐字一致。
PARAMETERS_STATUS_EXTRACTED = "extracted"
PARAMETERS_STATUS_NOT_IN_SOURCE = "not_available_in_source"
PARAMETERS_STATUS_NOT_LOCATED = "not_located"
PARAMETERS_STATUS_INCOMPLETE = "extraction_incomplete"
PARAMETERS_STATUS_FALLBACK_EMPTY = "fallback_empty"

__all__ = [
    "PARAMETERS_STATUS_EXTRACTED",
    "PARAMETERS_STATUS_FALLBACK_EMPTY",
    "PARAMETERS_STATUS_INCOMPLETE",
    "PARAMETERS_STATUS_NOT_IN_SOURCE",
    "PARAMETERS_STATUS_NOT_LOCATED",
    "parameter_extraction_incomplete_failure",
    "parameter_fallback_failure",
    "parameter_fallback_gap",
    "parameters_absent_gap",
]
