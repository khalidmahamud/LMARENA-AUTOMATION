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

    has_batches = run_result.total_batches > 1

    # Header row
    headers = ["Window #"]
    if has_batches:
        headers.append("Batch")
    headers += [
        "Prompt",
        "Model A",
        "Response A",
        "Model B",
        "Response B",
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
        c = 1
        ws.cell(row=row_idx, column=c, value=wr.worker_id + 1)
        c += 1
        if has_batches:
            ws.cell(row=row_idx, column=c, value=wr.batch_index + 1)
            c += 1
        ws.cell(row=row_idx, column=c, value=wr.prompt or "").alignment = WRAP_ALIGNMENT
        c += 1
        ws.cell(row=row_idx, column=c, value=wr.model_a_name or "")
        c += 1
        ws.cell(row=row_idx, column=c, value=wr.model_a_response or "").alignment = WRAP_ALIGNMENT
        c += 1
        ws.cell(row=row_idx, column=c, value=wr.model_b_name or "")
        c += 1
        ws.cell(row=row_idx, column=c, value=wr.model_b_response or "").alignment = WRAP_ALIGNMENT
        c += 1
        ws.cell(row=row_idx, column=c, value=round(wr.elapsed_seconds, 1) if wr.elapsed_seconds else "")
        c += 1
        ws.cell(row=row_idx, column=c, value="success" if wr.success else "error")
        c += 1
        ws.cell(row=row_idx, column=c, value=wr.error or "")

    # Summary sheet
    summary = wb.create_sheet("Summary")
    summary["A1"] = "Run ID"
    summary["B1"] = run_result.run_id

    # Show all prompts if they differ, otherwise just the single prompt
    unique_prompts = list(dict.fromkeys(run_result.prompts)) if run_result.prompts else [run_result.prompt]
    if len(unique_prompts) > 1:
        summary["A2"] = "Prompts"
        summary["B2"] = "\n".join(
            f"#{i+1}: {p[:200]}" for i, p in enumerate(unique_prompts)
        )
        summary["B2"].alignment = WRAP_ALIGNMENT
    else:
        summary["A2"] = "Prompt"
        summary["B2"] = run_result.prompt[:1000]

    summary["A3"] = "Total Batches"
    summary["B3"] = run_result.total_batches
    summary["A4"] = "Total Prompts"
    summary["B4"] = run_result.total_windows
    summary["A5"] = "Successful"
    summary["B5"] = run_result.successful_windows
    summary["A6"] = "Failed"
    summary["B6"] = run_result.failed_windows
    summary["A7"] = "Total Time (s)"
    summary["B7"] = (
        round(run_result.total_elapsed_seconds, 1)
        if run_result.total_elapsed_seconds
        else ""
    )

    # Column widths
    col_letter = "A"
    ws.column_dimensions["A"].width = 10  # Window #
    col_letter = "B"
    if has_batches:
        ws.column_dimensions["B"].width = 8  # Batch
        col_letter = "C"
    ws.column_dimensions[col_letter].width = 40  # Prompt
    remaining = ["D", "E", "F", "G", "H", "I", "J"]
    offset = 1 if has_batches else 0
    widths = [25, 50, 25, 50, 12, 10, 30]
    for i, w in enumerate(widths):
        letter = chr(ord("C") + offset + i)
        ws.column_dimensions[letter].width = w

    wb.save(str(filename))
    logger.info("Excel exported to %s", filename)
    return filename
