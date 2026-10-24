"""Atomic exports: a failed write cannot truncate an existing user file."""

import csv
import json
import os
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Callable

from core.validation import csv_cell


def write_targets_csv(path: str, rows: list[dict]) -> None:
    destination = Path(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", newline="", encoding="utf-8-sig", dir=destination.parent, delete=False
        ) as handle:
            temporary = Path(handle.name)
            fields = ("username", "status", "added_by_account_id", "processed_at", "error_message")
            writer = csv.writer(handle)
            writer.writerow(fields)
            for row in rows:
                writer.writerow(csv_cell(row.get(field)) for field in fields)
        os.replace(temporary, destination)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


def write_logs(path: str, logs: list[dict], formatter: Callable[[dict], str] | None = None) -> None:
    destination = Path(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=destination.parent, delete=False) as handle:
            temporary = Path(handle.name)
            for item in logs:
                if formatter is not None:
                    handle.write(formatter(item) + "\n")
                    continue
                handle.write(
                    f"[{item['created_at']}] [{item['action']}] "
                    f"{item.get('account_phone') or '-'} {item.get('username') or '-'} "
                    f"{item['message']}\n"
                )
        os.replace(temporary, destination)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


def write_logs_xlsx(
    path: str,
    logs: list[dict],
    action_labels: dict[str, str] | None = None,
    message_formatter: Callable[[str], str] | None = None,
) -> None:
    """Write a small, dependency-free Excel workbook with safe inline text cells."""
    destination = Path(path)
    temporary = None
    spreadsheet = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    office_rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    ET.register_namespace("", spreadsheet)
    ET.register_namespace("r", office_rel)

    def cell(reference: str, value: object, *, header=False) -> ET.Element:
        element = ET.Element(f"{{{spreadsheet}}}c", {"r": reference, "t": "inlineStr"})
        inline = ET.SubElement(element, f"{{{spreadsheet}}}is")
        text = ET.SubElement(inline, f"{{{spreadsheet}}}t")
        text.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
        text.text = str(value or "")
        if header:
            element.set("s", "1")
        return element

    headers = ("Дата и время", "Аккаунт", "Получатель", "Событие", "Описание")
    rows = [headers]
    labels = action_labels or {}
    for item in logs:
        username = item.get("username") or ""
        message = str(item.get("message") or "")
        if message_formatter:
            message = message_formatter(message)
        rows.append(
            (
                item.get("created_at") or "",
                item.get("account_phone") or "",
                "@" + username if username else "",
                labels.get(str(item.get("action") or ""), item.get("action") or ""),
                message,
            )
        )

    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, suffix=".xlsx", delete=False) as handle:
            temporary = Path(handle.name)
        sheet = ET.Element(f"{{{spreadsheet}}}worksheet")
        sheet_data = ET.SubElement(sheet, f"{{{spreadsheet}}}sheetData")
        for row_number, values in enumerate(rows, start=1):
            row_element = ET.SubElement(sheet_data, f"{{{spreadsheet}}}row", {"r": str(row_number)})
            for column, value in enumerate(values):
                reference = f"{chr(65 + column)}{row_number}"
                row_element.append(cell(reference, value, header=row_number == 1))
        widths = ET.SubElement(sheet, f"{{{spreadsheet}}}cols")
        for index, width in enumerate((21, 20, 25, 36, 80), start=1):
            ET.SubElement(
                widths,
                f"{{{spreadsheet}}}col",
                {"min": str(index), "max": str(index), "width": str(width), "customWidth": "1"},
            )
        sheet.insert(0, widths)
        sheet_xml = ET.tostring(sheet, encoding="utf-8", xml_declaration=True)

        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as workbook:
            workbook.writestr(
                "[Content_Types].xml",
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                '<Default Extension="xml" ContentType="application/xml"/>'
                '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
                "</Types>",
            )
            workbook.writestr(
                "_rels/.rels",
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
                "</Relationships>",
            )
            workbook.writestr(
                "xl/workbook.xml",
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                '<sheets><sheet name="Журнал" sheetId="1" r:id="rId1"/></sheets></workbook>',
            )
            workbook.writestr(
                "xl/_rels/workbook.xml.rels",
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
                '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
                "</Relationships>",
            )
            workbook.writestr(
                "xl/styles.xml",
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
                '<fills count="1"><fill><patternFill patternType="none"/></fill></fills>'
                '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
                '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
                '<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
                '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0"/></cellXfs>'
                '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>',
            )
            workbook.writestr("xl/worksheets/sheet1.xml", sheet_xml)
        os.replace(temporary, destination)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


def write_parsed(path, rows, format_name):
    destination = Path(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            newline="",
            encoding="utf-8-sig" if format_name == "csv" else "utf-8",
            dir=destination.parent,
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            if format_name == "txt":
                handle.write(
                    "".join(f"@{name}\n" for name in dict.fromkeys(row["username"] for row in rows if row["username"]))
                )
            elif format_name == "json":
                json.dump(rows, handle, ensure_ascii=False, indent=2)
            elif format_name == "csv":
                fields = (
                    "user_id",
                    "username",
                    "first_name",
                    "last_name",
                    "sources",
                    "source_ids",
                    "roles",
                    "activity",
                    "last_seen",
                    "message_count",
                    "first_message_at",
                    "last_message_at",
                    "is_premium",
                    "has_photo",
                    "is_bot",
                    "is_deleted",
                )
                writer = csv.writer(handle)
                writer.writerow(fields)
                for row in rows:
                    writer.writerow(
                        csv_cell(
                            " | ".join(map(str, row.get(key, [])))
                            if key in {"sources", "source_ids", "roles"}
                            else row.get(key)
                        )
                        for key in fields
                    )
            else:
                raise ValueError("Выберите CSV, JSON или TXT")
        os.replace(temporary, destination)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()
