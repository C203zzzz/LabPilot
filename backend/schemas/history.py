from typing import Any

from backend.domain.history import TaskVersion, TaskVersionDiff


def task_version_to_dict(value: TaskVersion) -> dict[str, Any]:
    return value.to_dict()


def task_version_diff_to_dict(value: TaskVersionDiff) -> dict[str, Any]:
    return value.to_dict()


__all__ = ["task_version_to_dict", "task_version_diff_to_dict"]
