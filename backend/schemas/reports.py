"""JSON-friendly serialization helpers for M11 report contracts."""
from __future__ import annotations

from typing import Any

from backend.domain.reports import (
    ExportResult,
    ReportSection,
    ReportSnapshot,
    ReportVersion,
    SelectionReport,
)


def report_section_to_dict(value: ReportSection) -> dict[str, Any]:
    return value.to_dict()


def selection_report_to_dict(value: SelectionReport) -> dict[str, Any]:
    return value.to_dict()


def report_version_to_dict(value: ReportVersion) -> dict[str, Any]:
    return value.to_dict()


def report_snapshot_to_dict(value: ReportSnapshot) -> dict[str, Any]:
    return value.to_dict()


def export_result_to_dict(value: ExportResult) -> dict[str, Any]:
    return value.to_dict()


report_to_dict = selection_report_to_dict
snapshot_to_dict = report_snapshot_to_dict

__all__ = [
    "report_section_to_dict",
    "selection_report_to_dict",
    "report_to_dict",
    "report_version_to_dict",
    "report_snapshot_to_dict",
    "snapshot_to_dict",
    "export_result_to_dict",
]
