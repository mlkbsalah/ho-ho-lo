#!/usr/bin/env python3
"""Read-only semantic cell comparison outside the refresh-owned ranges."""
import json
from pathlib import Path
import re
import sys
import xml.etree.ElementTree as ET
from zipfile import ZipFile

NS = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}


def number(col):
    result = 0
    for char in col:
        result = result * 26 + ord(char) - 64
    return result


def position(address):
    col, row = re.fullmatch(r'([A-Z]+)([0-9]+)', address).groups()
    return number(col), int(row)


def read_cells(path):
    with ZipFile(path) as z:
        strings = []
        if 'xl/sharedStrings.xml' in z.namelist():
            strings = [''.join(n.itertext()) for n in ET.fromstring(z.read('xl/sharedStrings.xml')).findall('s:si', NS)]
        rels = {n.get('Id'): n.get('Target') for n in ET.fromstring(z.read('xl/_rels/workbook.xml.rels'))}
        result = {}
        for sheet in ET.fromstring(z.read('xl/workbook.xml')).findall('s:sheets/s:sheet', NS):
            target = rels[sheet.get('{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id')]
            target = target.lstrip('/') if target.startswith('/') else 'xl/' + target
            cells = {}
            for cell in ET.fromstring(z.read(target)).findall('.//s:sheetData/s:row/s:c', NS):
                formula, value, inline = cell.find('s:f', NS), cell.find('s:v', NS), cell.find('s:is', NS)
                if formula is not None:
                    content = ('formula', (formula.text or '').lstrip('='))
                elif cell.get('t') == 's':
                    content = ('string', strings[int(value.text)])
                elif inline is not None:
                    content = ('string', ''.join(inline.itertext()))
                elif value is not None and value.text is not None:
                    content = ('value', cell.get('t', 'n'), value.text)
                else:
                    continue
                cells[cell.get('r')] = content
            result[sheet.get('name')] = cells
        return result


def verify(before, after, allowed):
    old, new = read_cells(before), read_cells(after)
    errors, checked = [], 0
    def annotations(path):
        with ZipFile(path) as archive:
            return {name: ET.canonicalize(archive.read(name).decode('utf-8'))
                    for name in archive.namelist()
                    if name.endswith('.xml') and any(part in name.lower()
                    for part in ('comments', 'threadedcomment', 'persons'))}
    if annotations(before) != annotations(after):
        errors.append('Workbook comment/person annotation parts changed; refusing replacement')
    for sheet, cells in old.items():
        if sheet not in new:
            errors.append(f'Missing sheet: {sheet}')
            continue
        ranges = []
        for ref in allowed.get(sheet, []):
            start, _, end = ref.partition(':')
            x1, y1 = position(start)
            x2, y2 = position(end or start)
            ranges.append((x1, y1, x2, y2))
        for address in set(cells) | set(new[sheet]):
            x, y = position(address)
            if any(a <= x <= c and b <= y <= d for a, b, c, d in ranges):
                continue
            checked += 1
            if cells.get(address) != new[sheet].get(address):
                errors.append(f'{sheet}!{address}: non-managed content changed')
    if errors:
        raise RuntimeError('; '.join(errors[:30]))
    return {'preserved_nonmanaged_cells_checked': checked, 'original_sheets_preserved': len(old),
            'comment_annotation_parts_preserved': True}


if __name__ == '__main__':
    print(json.dumps(verify(sys.argv[1], sys.argv[2], json.loads(Path(sys.argv[3]).read_text()))))
