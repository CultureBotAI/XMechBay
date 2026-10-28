"""Synthetic Excel export checks; no GUI, SSH, or live evidence, 2026-10-27."""

import importlib.util
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET
import zipfile


SCRIPTS = Path(__file__).parents[1] / ".agents/skills/fleet-session-status/scripts"
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location("fleet_excel_test", SCRIPTS / "fleet_excel.py")
EXCEL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(EXCEL)
NS = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def report(label="Laptop A"):
    return {"key": "a", "label": label, "observed_at": "2026-01-01T12:00:00+00:00",
            "reachable": True, "source": "ssh", "errors": [],
            "coverage": {"processes": "complete", "agent_discovery": "complete",
                         "screens_read": 1, "sessions_seen": 1, "terminal": "complete", "tmux": "complete"},
            "health": {"status": "ok", "occupancy_ratio": .02, "age_seconds": 5},
            "sessions": [{"repo_label": "SampleMech", "pid": 101, "agent": "codex",
                          "model_compact": "Example U", "target": {"transport": "tmux", "pane": "%1"},
                          "status": "Pursuing goal", "timer": {"kind": "goal", "text": "2h 3m"},
                          "activity": "Reviewed 8/20 records; 12 remain.",
                          "age_seconds": 100, "delta": {"kind": "unchanged", "seconds": 60},
                          "listeners": {"count": None, "status": "unknown"}}]}


def workbook_xml(path):
    with zipfile.ZipFile(path) as archive:
        return {name: ET.fromstring(archive.read(name))
                for name in archive.namelist() if name.endswith(".xml") or name.endswith(".rels")}


def cell_values(parts, sheet):
    strings = ["".join(item.itertext()) for item in parts["xl/sharedStrings.xml"].findall("x:si", NS)]
    values = {}
    for cell in parts[sheet].findall(".//x:c", NS):
        value = cell.find("x:v", NS)
        if value is not None:
            values[cell.attrib["r"]] = strings[int(value.text)] if cell.attrib.get("t") == "s" else value.text
    return values


@unittest.skipUnless(importlib.util.find_spec("xlsxwriter"), "Install requirements-excel.txt for workbook tests")
class ExcelExportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)

    def test_cells_zoom_filters_and_frozen_panes_match_observation(self):
        parts = workbook_xml(EXCEL.export_reports([report()], self.directory))
        cells = cell_values(parts, "xl/worksheets/sheet1.xml")
        self.assertEqual(cells["A1"], "Laptop A · 2026-01-01 12:00:00 UTC · SSH observed")
        self.assertIn("Static snapshot", cells["A2"])
        self.assertIn("1 sessions", cells["A3"])
        self.assertIn("screens 1/1", cells["A3"])
        self.assertEqual([cells[key] for key in ("A6", "B6", "C6", "D6", "E6", "F6")],
                         ["SampleMech · %1", "Cx / Example U", "🟢 goal · 2h 3m",
                          "~Reviewed 8/20 records; 12 remain.", "1m · =1m", "—"])
        page = parts["xl/worksheets/sheet1.xml"]
        view = page.find("x:sheetViews/x:sheetView", NS)
        self.assertEqual(view.attrib["zoomScale"], "150")
        pane = view.find("x:pane", NS)
        self.assertEqual((pane.attrib["xSplit"], pane.attrib["ySplit"], pane.attrib["state"]),
                         ("1", "5", "frozen"))
        self.assertEqual(parts["xl/tables/table1.xml"].find("x:autoFilter", NS).attrib["ref"], "A5:F6")
        self.assertTrue(any("~ screen claim" in value and "not proven progress" in value for value in cells.values()))
        self.assertTrue(any("reconfirm the current session ID" in value for value in cells.values()))

    def test_each_host_gets_unique_sheet_and_failed_host_remains_explicit(self):
        a = report("Laptop/A")
        a.update(source="snapshot", snapshot_stale=True)
        b = report("laptop:a")
        b.update(reachable=False, sessions=[], errors=["ssh_probe_failed"])
        parts = workbook_xml(EXCEL.export_reports([a, b], self.directory))
        names = [sheet.attrib["name"] for sheet in parts["xl/workbook.xml"].findall("x:sheets/x:sheet", NS)]
        self.assertEqual(names, ["Laptop-A", "laptop-a (2)"])
        first = cell_values(parts, "xl/worksheets/sheet1.xml")
        second = cell_values(parts, "xl/worksheets/sheet2.xml")
        self.assertIn("OFFLINE snapshot · STALE", first["A1"])
        self.assertIn("UNREACHABLE", second["A1"])
        self.assertIn("current session count and state are unknown", second["A3"])
        self.assertIn("ssh_probe_failed", second["A3"])
        self.assertNotIn("0 sessions", second["A3"])
        self.assertNotIn("A6", second)
        for name in ("xl/worksheets/sheet1.xml", "xl/worksheets/sheet2.xml"):
            self.assertEqual(parts[name].find("x:sheetViews/x:sheetView", NS).attrib["zoomScale"], "150")

    def test_all_states_have_distinct_expected_fills_with_text_labels(self):
        data = report()
        states = [("Pursuing goal", "E4F2E8"), ("idle", "F0F2F4"), ("QUESTION", "FFF1CB"),
                  ("Goal paused", "FCE4CE"), ("Goal stalled", "FBE4E5"),
                  ("Goal achieved", "E7EDF8"), ("UNKNOWN", "E2E5E9")]
        original = data["sessions"][0]
        data["sessions"] = [dict(original, repo_label="Example" + str(i), status=state)
                            for i, (state, _) in enumerate(states)]
        parts = workbook_xml(EXCEL.export_reports([data], self.directory))
        formats = parts["xl/styles.xml"].find("x:cellXfs", NS)
        fills = parts["xl/styles.xml"].find("x:fills", NS)
        cells = {cell.attrib["r"]: cell for cell in parts["xl/worksheets/sheet1.xml"].findall(".//x:c", NS)}
        for index, (_, color) in enumerate(states, start=6):
            style = formats[int(cells["C" + str(index)].attrib["s"])]
            fill = fills[int(style.attrib["fillId"])].find("x:patternFill/x:fgColor", NS)
            self.assertEqual(fill.attrib["rgb"], "FF" + color)

    def test_markdown_escapes_remain_literal_and_no_formula_or_link_is_created(self):
        data = report('=HYPERLINK("https://example.invalid","Host")')
        data["sessions"][0].update(repo_label="=1+1", activity="Pipe | slash \\ and `code` [label] <tag> & _x_")
        parts = workbook_xml(EXCEL.export_reports([data], self.directory))
        cells = cell_values(parts, "xl/worksheets/sheet1.xml")
        self.assertEqual(cells["A6"], "=1+1 · %1")
        self.assertEqual(cells["D6"], "~Pipe | slash \\ and `code` [label] <tag> & _x_")
        for name, part in parts.items():
            self.assertFalse(part.findall(".//x:f", NS), name)
            self.assertFalse(part.findall(".//x:hyperlink", NS), name)
            self.assertFalse(any(element.attrib.get("TargetMode") == "External" for element in part.iter()), name)
        self.assertFalse(any("externalLink" in name or "vba" in name.lower() for name in parts))

    def test_export_is_private_and_never_overwrites_prior_snapshot(self):
        first = EXCEL.export_reports([report()], self.directory)
        original = first.read_bytes()
        second = EXCEL.export_reports([report()], self.directory)
        self.assertNotEqual(first, second)
        self.assertEqual(first.read_bytes(), original)
        self.assertEqual(stat.S_IMODE(first.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(second.stat().st_mode), 0o600)

    def test_invalid_request_or_missing_dependency_creates_no_file(self):
        for zoom in (True, 9, 401, "150"):
            with self.assertRaises(ValueError):
                EXCEL.export_reports([report()], self.directory, zoom)
        with self.assertRaises(ValueError):
            EXCEL.export_reports([], self.directory)
        with patch.dict(sys.modules, {"xlsxwriter": None}):
            with self.assertRaisesRegex(RuntimeError, "requirements-excel.txt"):
                EXCEL.export_reports([report()], self.directory)
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_empty_successful_host_retains_explicit_inventory_row(self):
        data = report()
        data["sessions"] = []
        data["coverage"].update(screens_read=0, sessions_seen=0)
        parts = workbook_xml(EXCEL.export_reports([data], self.directory))
        cells = cell_values(parts, "xl/worksheets/sheet1.xml")
        self.assertEqual(cells["A6"], "No agent sessions observed")
        self.assertIn("0 sessions", cells["A3"])

    def test_failed_workbook_write_removes_incomplete_artifact(self):
        import xlsxwriter
        with patch.object(xlsxwriter, "Workbook", side_effect=xlsxwriter.exceptions.FileCreateError("example failure")):
            with self.assertRaisesRegex(RuntimeError, "workbook generation failed"):
                EXCEL.export_reports([report()], self.directory)
        self.assertEqual(list(self.directory.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
