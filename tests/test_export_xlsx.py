import unittest
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from core.export import write_logs_xlsx


class LogXlsxExportTests(unittest.TestCase):
    def test_workbook_contains_russian_columns_and_safe_text_cells(self):
        directory = Path(".audit/xlsx-export-test")
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "journal.xlsx"
        try:
            write_logs_xlsx(
                str(path),
                [
                    {
                        "created_at": "2026-10-22 12:30:00",
                        "account_phone": "+380000000000",
                        "username": "=1+1",
                        "action": "MESSAGE_SENT",
                        "message": "Привет & добро пожаловать",
                    }
                ],
                action_labels={"MESSAGE_SENT": "Сообщение отправлено"},
            )
            self.assertTrue(zipfile.is_zipfile(path))
            with zipfile.ZipFile(path) as workbook:
                sheet = ET.fromstring(workbook.read("xl/worksheets/sheet1.xml"))
            ns = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
            cells = [node.findtext("x:is/x:t", namespaces=ns) for node in sheet.findall(".//x:c", ns)]
            self.assertEqual(cells[:5], ["Дата и время", "Аккаунт", "Получатель", "Событие", "Описание"])
            self.assertEqual(
                cells[5:],
                ["2026-10-22 12:30:00", "+380000000000", "@=1+1", "Сообщение отправлено", "Привет & добро пожаловать"],
            )
            self.assertTrue(all(cell.get("t") == "inlineStr" for cell in sheet.findall(".//x:c", ns)))
        finally:
            path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
