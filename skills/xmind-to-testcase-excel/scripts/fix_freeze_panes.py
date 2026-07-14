#!/usr/bin/env python3
"""Ensure the first row is frozen when artifact-tool omits pane XML on export."""

from __future__ import annotations

import argparse
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET


MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
ET.register_namespace("x", MAIN_NS)


class FreezePaneError(RuntimeError):
    """Raised when a workbook cannot be patched safely."""


def _tag(name: str) -> str:
    return f"{{{MAIN_NS}}}{name}"


def add_first_row_freeze(xml_bytes: bytes) -> bytes:
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise FreezePaneError(f"工作表 XML 无法解析：{exc}") from exc

    sheet_views = root.find(_tag("sheetViews"))
    if sheet_views is None:
        sheet_views = ET.Element(_tag("sheetViews"))
        insert_at = 0
        for index, child in enumerate(list(root)):
            if child.tag in {_tag("sheetFormatPr"), _tag("cols"), _tag("sheetData")}:
                insert_at = index
                break
            insert_at = index + 1
        root.insert(insert_at, sheet_views)

    sheet_view = sheet_views.find(_tag("sheetView"))
    if sheet_view is None:
        sheet_view = ET.SubElement(sheet_views, _tag("sheetView"), {"workbookViewId": "0"})

    for element in list(sheet_view):
        if element.tag in {_tag("pane"), _tag("selection")}:
            sheet_view.remove(element)
    pane = ET.Element(
        _tag("pane"),
        {
            "ySplit": "1",
            "topLeftCell": "A2",
            "activePane": "bottomLeft",
            "state": "frozen",
        },
    )
    selection = ET.Element(
        _tag("selection"),
        {"pane": "bottomLeft", "activeCell": "A2", "sqref": "A2"},
    )
    sheet_view.insert(0, pane)
    sheet_view.insert(1, selection)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def patch_workbook(path: Path) -> None:
    if path.suffix.lower() != ".xlsx" or not path.is_file():
        raise FreezePaneError(f"Excel 文件不存在或扩展名无效：{path}")

    worksheet_name = "xl/worksheets/sheet1.xml"
    temporary = path.with_name(f".{path.name}.freeze.tmp")
    try:
        with zipfile.ZipFile(path, "r") as source:
            if worksheet_name not in source.namelist():
                raise FreezePaneError("工作簿缺少 xl/worksheets/sheet1.xml")
            patched_xml = add_first_row_freeze(source.read(worksheet_name))
            with zipfile.ZipFile(temporary, "w") as destination:
                destination.comment = source.comment
                for info in source.infolist():
                    data = patched_xml if info.filename == worksheet_name else source.read(info.filename)
                    destination.writestr(info, data)
        temporary.replace(path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise FreezePaneError(f"修复冻结窗格失败：{exc}") from exc
    finally:
        if temporary.exists():
            temporary.unlink()

    with zipfile.ZipFile(path, "r") as workbook:
        root = ET.fromstring(workbook.read(worksheet_name))
        pane = root.find(f".//{_tag('pane')}")
        if pane is None or pane.get("state") != "frozen" or pane.get("ySplit") != "1":
            raise FreezePaneError("冻结首行校验失败")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="为 artifact-tool 导出的单工作表 Excel 补充冻结首行")
    parser.add_argument("workbook", type=Path, help="输入并原位修复的 .xlsx 文件")
    args = parser.parse_args(argv)
    try:
        patch_workbook(args.workbook)
    except FreezePaneError as exc:
        print(f"冻结窗格修复失败：{exc}", file=sys.stderr)
        return 1
    print(f"已冻结首行：{args.workbook}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
