from typing import Any
from backend.domain.questions import QuestionList, SupplierQuestion
def question_to_dict(value: SupplierQuestion) -> dict[str, Any]: return value.to_dict()
def question_list_to_dict(value: QuestionList) -> dict[str, Any]: return value.to_dict()
