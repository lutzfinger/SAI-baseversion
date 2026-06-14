"""SAI shared library: Google Sheets I/O via gspread.

Atomic primitives any SAI skill can use to read/write Google Sheets.
Pulls credentials from the SAI private location at `~/.SAI/credentials.json`
with `~/.SAI/token.json` for the cached OAuth token.

Public API
----------
- open_sheet(sheet_url, worksheet_name=None)         → gspread.Worksheet
- open_workbook(sheet_url)                           → gspread.Spreadsheet
- col_letter_to_index(letter)                        → 0-based int
- index_to_col_letter(idx)                           → 'A', 'AA', ...
- read_column_values(ws, col_letter, header_rows=1)  → [str, str, ...]
- append_columns(ws, header_row, headers, data_matrix, name_col='A')
                                                      → A1 range string of what was written
- create_or_replace_tab(workbook, title, rows=120, cols=10)
                                                      → gspread.Worksheet
- add_basic_chart(workbook, sheet_id, title, chart_type, domain_range, series_range,
                  anchor_row, anchor_col, x_title='', y_title='', legend='NO_LEGEND')
                                                      → None  (chart is added in-place)

All higher-level helpers raise ValueError on bad input. Auth failure
raises gspread.exceptions.APIError or google.auth's RefreshError; callers
should handle / propagate appropriately.
"""
from __future__ import annotations

import sys
from pathlib import Path

try:
    import gspread
    from google.auth.exceptions import RefreshError
except ImportError:
    sys.exit("ERROR: gspread not installed. Run: pip3 install gspread google-auth google-auth-oauthlib")


# --- Credential resolution -------------------------------------------------

SAI_PRIVATE_DIR = Path.home() / ".SAI"


def credentials_path() -> Path:
    return SAI_PRIVATE_DIR / "credentials.json"


def token_path() -> Path:
    return SAI_PRIVATE_DIR / "token.json"


def _auth_gspread_client():
    creds = credentials_path()
    tok = token_path()
    if not creds.exists():
        raise FileNotFoundError(
            f"Missing {creds}. See ~/SAI/skills/student-participation-check/SETUP.md"
        )
    try:
        return gspread.oauth(
            credentials_filename=str(creds),
            authorized_user_filename=str(tok),
        )
    except RefreshError:
        tok.unlink(missing_ok=True)
        raise RuntimeError(
            "Google auth token expired. Deleted stale token; re-run and a browser will open."
        )


# --- Spreadsheet / worksheet open ------------------------------------------

def open_workbook(sheet_url: str):
    return _auth_gspread_client().open_by_url(sheet_url)


def open_sheet(sheet_url: str, worksheet_name: str | None = None):
    sh = open_workbook(sheet_url)
    return sh.worksheet(worksheet_name) if worksheet_name else sh.sheet1


# --- Column letter math ----------------------------------------------------

def col_letter_to_index(letter: str) -> int:
    letter = letter.strip().upper()
    if not letter or not letter.isalpha():
        raise ValueError(f"Invalid column letter: {letter!r}")
    n = 0
    for ch in letter:
        n = n * 26 + (ord(ch) - ord("A") + 1)
    return n - 1


def index_to_col_letter(idx: int) -> str:
    n = idx + 1
    out = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        out = chr(ord("A") + r) + out
    return out


# --- Reading ----------------------------------------------------------------

def read_column_values(ws, col_letter: str, header_rows: int = 1) -> list[str]:
    """Return the data values from one column, skipping the header rows."""
    idx = col_letter_to_index(col_letter)
    vals = ws.col_values(idx + 1)
    return vals[header_rows:] if len(vals) > header_rows else []


# --- Writing ---------------------------------------------------------------

def append_columns(
    ws,
    header_row: int,
    headers: list[str],
    data_matrix: list[list],
    name_col: str = "A",
) -> str:
    """Append new columns to the right of the existing data.

    Args:
      header_row: 1-indexed row holding column headers (typically 1).
      headers: list of column headers to add (e.g. ['session - 2026-05-08', ...]).
      data_matrix: list of rows, each a list of cell values matching `headers` length.
                   Index 0 in data_matrix corresponds to the first NAMED row
                   (header_row + 1) in the sheet. Use empty strings to skip rows.
      name_col: column letter holding the name column (default 'A'). Used to
                figure out which row to start writing from.

    Returns:
      A1 range string of what was written (e.g. 'C1:F94').
    """
    existing_header = ws.row_values(header_row)
    start_col_idx = len(existing_header)  # next free column, 0-based
    new_col_count = len(headers)
    end_col_idx = start_col_idx + new_col_count - 1

    last_row = header_row + len(data_matrix)
    a1 = (
        f"{index_to_col_letter(start_col_idx)}{header_row}"
        f":{index_to_col_letter(end_col_idx)}{last_row}"
    )
    matrix = [headers] + data_matrix
    ws.update(a1, matrix, value_input_option="USER_ENTERED")
    return a1


# --- Tabs ------------------------------------------------------------------

def create_or_replace_tab(workbook, title: str, rows: int = 120, cols: int = 10):
    """Add a new tab, deleting any existing tab of the same name first."""
    try:
        existing = workbook.worksheet(title)
        workbook.del_worksheet(existing)
    except gspread.WorksheetNotFound:
        pass
    return workbook.add_worksheet(title=title, rows=rows, cols=cols)


# --- Charts ----------------------------------------------------------------

def add_basic_chart(
    workbook,
    sheet_id: int,
    title: str,
    chart_type: str,                          # 'COLUMN', 'LINE', 'SCATTER', 'BAR', etc.
    domain_range: tuple[int, int, int, int],  # (startRow, endRow, startCol, endCol) 0-indexed; end exclusive
    series_range: tuple[int, int, int, int],
    anchor_row: int,                          # 0-indexed anchor row for chart
    anchor_col: int,                          # 0-indexed anchor col
    x_title: str = "",
    y_title: str = "",
    legend: str = "NO_LEGEND",
    width_px: int = 480,
    height_px: int = 320,
) -> None:
    """Add a basic-chart (column/line/scatter/bar) overlaid on the sheet."""
    workbook.batch_update({
        "requests": [{
            "addChart": {
                "chart": {
                    "spec": {
                        "title": title,
                        "basicChart": {
                            "chartType": chart_type,
                            "legendPosition": legend,
                            "axis": [
                                {"position": "BOTTOM_AXIS", "title": x_title},
                                {"position": "LEFT_AXIS", "title": y_title},
                            ],
                            "domains": [{"domain": {"sourceRange": {"sources": [{
                                "sheetId": sheet_id,
                                "startRowIndex": domain_range[0],
                                "endRowIndex": domain_range[1],
                                "startColumnIndex": domain_range[2],
                                "endColumnIndex": domain_range[3],
                            }]}}}],
                            "series": [{"series": {"sourceRange": {"sources": [{
                                "sheetId": sheet_id,
                                "startRowIndex": series_range[0],
                                "endRowIndex": series_range[1],
                                "startColumnIndex": series_range[2],
                                "endColumnIndex": series_range[3],
                            }]}}, "targetAxis": "LEFT_AXIS"}],
                            "headerCount": 0,
                        },
                    },
                    "position": {"overlayPosition": {
                        "anchorCell": {
                            "sheetId": sheet_id,
                            "rowIndex": anchor_row,
                            "columnIndex": anchor_col,
                        },
                        "widthPixels": width_px,
                        "heightPixels": height_px,
                    }},
                }
            }
        }]
    })
