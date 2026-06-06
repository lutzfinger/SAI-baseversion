"""Apply approved student-participation-check proposals.

Fires AFTER operator ✅ on the staged YAML proposal at
`~/.sai-runtime/eval/proposed/student-participation-check/<thread_id>.yaml`.

Per PRINCIPLES.md §16e — every side-effecting skill ships with a kill-switch
env var that defaults OFF. Operator flips ON explicitly after green eval.

Per #6 (fail closed) — re-reads the proposal body, re-validates the
crosscheck invariant, and refuses to write if anything looks off.
"""
from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

_SAI_ROOT = Path(__file__).resolve().parents[2]
if str(_SAI_ROOT) not in sys.path:
    sys.path.insert(0, str(_SAI_ROOT))

from app.connectors.google_sheet import (  # noqa: E402
    open_sheet, index_to_col_letter,
)

KILL_SWITCH_ENV = "SAI_STUDENT_PARTICIPATION_SEND_ENABLED"
WORKFLOW_ID = "student-participation-check"

log = logging.getLogger("sai.student-participation-check.send_tool")


@dataclass
class ApplyResult:
    wrote_sheet: bool
    sheet_a1: Optional[str]
    rows_written: int
    cols_written: int
    reason: str


def apply_approved_proposal(proposal_body: dict[str, Any]) -> ApplyResult:
    """Apply an operator-approved proposal — actually writes to the Google Sheet.

    proposal_body is the dict staged by the human-tier handler. Expected
    keys (under `draft`):
      - workflow_id (must equal WORKFLOW_ID — defense in depth)
      - matrix_preview (list of rows, with header row first)
      - new_session_columns (int)
      - transcripts_loaded (int)
      - sheet_url (str)
      - worksheet (Optional[str])
      - student_rows ([(sheet_row, full_name), ...])
    """
    draft = proposal_body.get("draft") or proposal_body  # tolerate both shapes
    if draft.get("workflow_id") != WORKFLOW_ID:
        return ApplyResult(False, None, 0, 0, "wrong_workflow_id_in_proposal")

    # Kill switch — defaults OFF. Operator must explicitly enable.
    if os.environ.get(KILL_SWITCH_ENV, "0") != "1":
        log.info("kill_switch_off — would have written to %s", draft.get("sheet_url"))
        return ApplyResult(
            False, None, 0, 0,
            f"kill_switch_off:set_{KILL_SWITCH_ENV}=1_to_enable",
        )

    # Re-validate crosscheck invariant.
    cols = draft.get("new_session_columns", 0)
    n_t = draft.get("transcripts_loaded", 0)
    if cols != n_t:
        return ApplyResult(False, None, 0, 0,
                           f"crosscheck_revalidation_failed:{cols}_vs_{n_t}")

    matrix = draft.get("matrix_preview") or []
    if not matrix or len(matrix) < 2:
        return ApplyResult(False, None, 0, 0, "empty_matrix")

    sheet_url = draft.get("sheet_url")
    if not sheet_url:
        return ApplyResult(False, None, 0, 0, "missing_sheet_url")

    # Open the sheet, find the next free column to append at.
    ws = open_sheet(sheet_url, draft.get("worksheet"))
    student_rows = draft.get("student_rows") or []
    if not student_rows:
        return ApplyResult(False, None, 0, 0, "no_student_rows")

    new_column_count = cols + 1  # + Total
    start_col_idx = len(ws.row_values(1))
    end_col_idx = start_col_idx + new_column_count - 1
    last_row = student_rows[-1][0] if student_rows else 2
    a1 = (f"{index_to_col_letter(start_col_idx)}1"
          f":{index_to_col_letter(end_col_idx)}{last_row}")

    ws.update(a1, matrix, value_input_option="USER_ENTERED")
    log.info("wrote_sheet workflow=%s a1=%s rows=%d cols=%d",
             WORKFLOW_ID, a1, len(matrix), new_column_count)

    return ApplyResult(
        wrote_sheet=True,
        sheet_a1=a1,
        rows_written=len(matrix),
        cols_written=new_column_count,
        reason="ok",
    )


if __name__ == "__main__":
    # CLI for manual approval: `python -m skills.student-participation-check.send_tool <path-to-proposal.yaml>`
    import yaml
    if len(sys.argv) != 2:
        print("usage: python -m skills.student-participation-check.send_tool <proposal.yaml>")
        sys.exit(2)
    proposal_path = Path(sys.argv[1])
    if not proposal_path.exists():
        print(f"ERROR: proposal not found: {proposal_path}")
        sys.exit(2)
    body = yaml.safe_load(proposal_path.read_text())
    result = apply_approved_proposal(body)
    print(f"wrote_sheet:  {result.wrote_sheet}")
    print(f"sheet_a1:     {result.sheet_a1}")
    print(f"rows_written: {result.rows_written}")
    print(f"cols_written: {result.cols_written}")
    print(f"reason:       {result.reason}")
    sys.exit(0 if result.wrote_sheet else 1)
