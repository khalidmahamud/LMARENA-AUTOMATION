from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill

from src.models.results import RunResult

logger = logging.getLogger(__name__)

HEADER_FONT = Font(bold=True, color="FFFFFF")
HEADER_FILL = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
WRAP_ALIGNMENT = Alignment(wrap_text=True, vertical="top")


def export_to_excel(run_result: RunResult, output_dir: str = "outputs") -> Path:
    """Generate an ``.xlsx`` file from a ``RunResult``.

    Returns the ``Path`` to the written file.
    """
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = out / f"arena_results_{run_result.run_id}_{ts}.xlsx"

    wb = Workbook()
    ws = wb.active
    ws.title = "Results"

    # Header row
    headers = [
        "Window #",
        "Model",
        "Response",
        "Elapsed (s)",
        "Status",
        "Error",
    ]
    for col, header in enumerate(headers, start=1):
        cell = ws.cell(row=1, column=col, value=header)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL

    # Data rows
    for row_idx, wr in enumerate(run_result.window_results, start=2):
        ws.cell(row=row_idx, column=1, value=wr.worker_id + 1)
        ws.cell(row=row_idx, column=2, value=wr.model_name or "")
        ws.cell(row=row_idx, column=3, value=wr.response or "").alignment = WRAP_ALIGNMENT
        ws.cell(row=row_idx, column=4, value=round(wr.elapsed_seconds, 1) if wr.elapsed_seconds else "")
        ws.cell(row=row_idx, column=5, value="success" if wr.success else "error")
        ws.cell(row=row_idx, column=6, value=wr.error or "")

    # Summary sheet
    summary = wb.create_sheet("Summary")
    summary["A1"] = "Run ID"
    summary["B1"] = run_result.run_id
    summary["A2"] = "Prompt"
    summary["B2"] = run_result.prompt[:1000]
    summary["A3"] = "Total Windows"
    summary["B3"] = run_result.total_windows
    summary["A4"] = "Successful"
    summary["B4"] = run_result.successful_windows
    summary["A5"] = "Failed"
    summary["B5"] = run_result.failed_windows
    summary["A6"] = "Total Time (s)"
    summary["B6"] = (
        round(run_result.total_elapsed_seconds, 1)
        if run_result.total_elapsed_seconds
        else ""
    )

    # Column widths
    ws.column_dimensions["A"].width = 10
    ws.column_dimensions["B"].width = 20
    ws.column_dimensions["C"].width = 60
    ws.column_dimensions["D"].width = 12
    ws.column_dimensions["E"].width = 10
    ws.column_dimensions["F"].width = 30

    wb.save(str(filename))
    logger.info("Excel exported to %s", filename)
    return filename
