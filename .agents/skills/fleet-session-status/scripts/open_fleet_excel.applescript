-- Purpose: open one generated local fleet workbook and verify its display zoom.
-- Inputs: workbook POSIX path and zoom percentage as literal argv values.
-- Output: verified full path and zoom; no changes to unrelated workbooks.
-- Fleet Excel command integration, 2026-10-28.
on run argv
    if (count of argv) is not 2 then error "Expected workbook path and zoom"
    set workbookPath to item 1 of argv
    set requestedZoom to (item 2 of argv) as integer
    if requestedZoom < 10 or requestedZoom > 400 then error "Invalid zoom"
    tell application "Microsoft Excel"
        set openedWorkbook to open workbook workbook file name workbookPath
        if (full name of openedWorkbook) is not workbookPath then error "Workbook identity mismatch"
        activate object openedWorkbook
        set workbookWindow to window 1 of openedWorkbook
        set zoom of workbookWindow to requestedZoom
        if (zoom of workbookWindow) is not requestedZoom then error "Workbook zoom mismatch"
        activate
        set verifiedZoom to (zoom of workbookWindow) as integer
        return (full name of openedWorkbook) & linefeed & (verifiedZoom as text)
    end tell
end run
