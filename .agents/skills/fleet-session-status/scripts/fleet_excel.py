#!/usr/bin/env python3
"""Export normalized fleet observations to a private, static Excel workbook.

Inputs: normalized host reports and a local output directory.
Output: unique .xlsx with one sheet per host, matching the Markdown report.
Serves the fleet-command Excel feature, 2026-10-27. No probes or GUI actions.
Python 3.9+; XlsxWriter is optional until an export is requested.
"""

import html
import os
from pathlib import Path
import re
import tempfile
from datetime import datetime, timezone

from fleet_view import render_host


STATE_COLORS = {
    "🟢": "#E4F2E8", "⚪": "#F0F2F4", "🟡": "#FFF1CB",
    "🟠": "#FCE4CE", "🔴": "#FBE4E5", "🔵": "#E7EDF8", "⚫": "#E2E5E9",
}


def _plain(text):
    # Remove generated inline-code delimiters before restoring escaped literals.
    text = re.sub(r"(?<!\\)`", "", text.strip())
    return html.unescape(re.sub(r"\\([\\|`\[\]*_])", r"\1", text))


def _rendered_table(report):
    """Consume our renderer's Markdown; do not reinterpret screen evidence."""
    lines = [line for line in render_host(report).splitlines() if line.strip()]
    heading = lines[0]
    if heading.startswith("**") and heading.endswith("**"):
        heading = heading[2:-2]
    heading = _plain(heading)
    table = [line for line in lines[1:] if line.startswith("|")]
    prose = [_plain(line) for line in lines[1:] if not line.startswith("|")]
    if not table:
        return heading, prose[0] if prose else "", [], [], prose[1:]

    def cells(line):
        return [_plain(value) for value in re.split(r"(?<!\\)\|", line)[1:-1]]

    headers = cells(table[0])
    if (len(headers) != 6 or len(table) < 2 or
            not all(re.fullmatch(r":?-+:?", value) for value in cells(table[1]))):
        raise ValueError("unexpected_fleet_table_shape")
    rows = [cells(line) for line in table[2:]]
    if any(len(row) != len(headers) for row in rows):
        raise ValueError("unexpected_fleet_table_width")
    return heading, prose[0] if prose else "", headers, rows, prose[1:]


def _sheet_name(label, used):
    base = re.sub(r"[\[\]:*?/\\\x00-\x1f]", "-", str(label)).strip("' ").strip()
    base = base[:31].strip("' ") or "Host"
    if base.lower() == "history":
        base = "Host history"
    name, counter = base, 1
    while name.lower() in used:
        counter += 1
        suffix = " (" + str(counter) + ")"
        name = base[:31 - len(suffix)] + suffix
    used.add(name.lower())
    return name


def export_reports(reports, directory, zoom=150):
    """Return a new private workbook path; never replace an earlier snapshot."""
    if type(zoom) is not int or not 10 <= zoom <= 400:
        raise ValueError("excel_zoom_must_be_between_10_and_400")
    reports = list(reports)
    if not reports:
        raise ValueError("excel_export_requires_a_host")
    try:
        import xlsxwriter
    except ImportError as error:
        raise RuntimeError(
            "Excel export requires XlsxWriter; install with "
            "python3 -m pip install -r requirements-excel.txt, or use --no-excel."
        ) from error

    rendered = [_rendered_table(report) for report in reports]
    directory = Path(directory).expanduser()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    descriptor, filename = tempfile.mkstemp(prefix="fleet-status-" + stamp + "-", suffix=".xlsx", dir=directory)
    destination = Path(filename).resolve()
    try:
        with os.fdopen(descriptor, "w+b") as stream:
            os.fchmod(stream.fileno(), 0o600)
            with xlsxwriter.Workbook(stream, {
                "in_memory": True, "strings_to_formulas": False, "strings_to_urls": False,
            }) as book:
                book.set_properties({"title": "Fleet session status", "author": "XMechBay",
                                     "comments": "Static observation; no live refresh or external connections."})
                base = {"font_name": "Calibri", "font_size": 11, "valign": "top", "text_wrap": True}
                body = book.add_format(base)
                title = book.add_format({**base, "font_size": 16, "bold": True,
                                         "font_color": "#FFFFFF", "bg_color": "#20344D"})
                header = book.add_format({**base, "bold": True, "font_color": "#FFFFFF", "bg_color": "#20344D"})
                muted = book.add_format({**base, "font_color": "#526375"})
                summary = book.add_format({**base, "bold": True, "bg_color": "#EAF0F6"})
                states = {symbol: book.add_format({**base, "bold": True, "bg_color": color})
                          for symbol, color in STATE_COLORS.items()}
                used = set()
                for index, (report, parts) in enumerate(zip(reports, rendered), start=1):
                    heading, scope, headers, rows, notes = parts
                    sheet = book.add_worksheet(_sheet_name(report.get("label", report.get("key", "Host")), used))
                    sheet.set_zoom(zoom)
                    sheet.hide_gridlines(2)
                    for column, width in enumerate((38, 22, 28, 64, 20, 9)):
                        sheet.set_column(column, column, width, body)
                    sheet.merge_range(0, 0, 0, 5, heading, title)
                    sheet.set_row(0, 42)
                    sheet.merge_range(1, 0, 1, 5,
                        "Static snapshot · screen claims are not verified progress · no automatic refresh", muted)
                    sheet.set_row(1, 30)
                    sheet.merge_range(2, 0, 2, 5, scope, summary)
                    sheet.set_row(2, 44)
                    if headers and rows:
                        sheet.add_table(4, 0, 4 + len(rows), 5, {
                            "name": "FleetSessions" + str(index), "style": "Table Style Medium 2",
                            "data": rows, "columns": [{"header": value, "header_format": header} for value in headers],
                        })
                        sheet.set_row(4, 28)
                        for row_index, row in enumerate(rows, start=5):
                            sheet.set_row(row_index, 42)
                            sheet.write_string(row_index, 2, row[2], states.get(row[2][:1], body))
                        sheet.freeze_panes(5, 1)
                        sheet.set_selection("A6")
                    else:
                        sheet.freeze_panes(3, 1)
                    note_row = 6 + len(rows)
                    for offset, note in enumerate(notes):
                        sheet.merge_range(note_row + offset, 0, note_row + offset, 5, note, muted)
                        sheet.set_row(note_row + offset, 48)
                    sheet.set_landscape()
                    sheet.fit_to_pages(1, 0)
                    sheet.repeat_rows(0, 4)
                    sheet.set_footer("&CStatic snapshot · &P / &N")
            stream.flush()
    except BaseException as error:
        destination.unlink(missing_ok=True)
        if isinstance(error, xlsxwriter.exceptions.XlsxWriterException):
            raise RuntimeError("Excel workbook generation failed; no complete workbook was retained.") from error
        raise
    return destination
