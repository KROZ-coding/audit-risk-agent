#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Build the Word edition of docs/技术报告.md.

The Markdown file remains the editorial source.  This builder intentionally
implements the small Markdown subset used by the report so the Word edition
does not introduce a second, independently edited body of technical facts.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from docx import Document
from docx.enum.section import WD_SECTION
from docx.enum.style import WD_STYLE_TYPE
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT, WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK, WD_LINE_SPACING
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Inches, Pt, RGBColor


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MARKDOWN = ROOT / "docs" / "技术报告.md"
DEFAULT_OUTPUT = ROOT / "docs" / "技术报告.docx"

BODY_FONT = "Microsoft YaHei"
MONO_FONT = "Consolas"
BLACK = "000000"
NAVY = "17365D"
BLUE = "1F4E78"
PALE_BLUE = "EAF2F8"
PALE_GRAY = "F5F7F9"
BORDER_GRAY = "D9D9D9"
MUTED = "5B6573"


def _set_font(run, name=BODY_FONT, size=10.5, bold=None, italic=None, color=BLACK):
    """Set both Latin and East Asian font slots for reliable LO/Word output."""
    run.font.name = name
    run._element.get_or_add_rPr().rFonts.set(qn("w:ascii"), name)
    run._element.get_or_add_rPr().rFonts.set(qn("w:hAnsi"), name)
    run._element.get_or_add_rPr().rFonts.set(qn("w:eastAsia"), BODY_FONT)
    run.font.size = Pt(size)
    if bold is not None:
        run.bold = bold
    if italic is not None:
        run.italic = italic
    if color:
        run.font.color.rgb = RGBColor.from_string(color)


def _set_paragraph_spacing(paragraph, before=0, after=6, line=1.22):
    fmt = paragraph.paragraph_format
    fmt.space_before = Pt(before)
    fmt.space_after = Pt(after)
    fmt.line_spacing = line


def _set_cell_shading(cell, fill):
    tc_pr = cell._tc.get_or_add_tcPr()
    shd = tc_pr.find(qn("w:shd"))
    if shd is None:
        shd = OxmlElement("w:shd")
        tc_pr.append(shd)
    shd.set(qn("w:fill"), fill)


def _set_cell_margins(cell, top=90, start=110, bottom=90, end=110):
    tc = cell._tc
    tc_pr = tc.get_or_add_tcPr()
    tc_mar = tc_pr.first_child_found_in("w:tcMar")
    if tc_mar is None:
        tc_mar = OxmlElement("w:tcMar")
        tc_pr.append(tc_mar)
    for tag, value in (("top", top), ("start", start), ("bottom", bottom), ("end", end)):
        node = tc_mar.find(qn(f"w:{tag}"))
        if node is None:
            node = OxmlElement(f"w:{tag}")
            tc_mar.append(node)
        node.set(qn("w:w"), str(value))
        node.set(qn("w:type"), "dxa")


def _set_table_borders(table, color=BORDER_GRAY, size="6"):
    tbl_pr = table._tbl.tblPr
    borders = tbl_pr.first_child_found_in("w:tblBorders")
    if borders is None:
        borders = OxmlElement("w:tblBorders")
        tbl_pr.append(borders)
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        tag = f"w:{edge}"
        element = borders.find(qn(tag))
        if element is None:
            element = OxmlElement(tag)
            borders.append(element)
        element.set(qn("w:val"), "single")
        element.set(qn("w:sz"), size)
        element.set(qn("w:space"), "0")
        element.set(qn("w:color"), color)


def _set_repeat_table_header(row):
    tr_pr = row._tr.get_or_add_trPr()
    header = OxmlElement("w:tblHeader")
    header.set(qn("w:val"), "true")
    tr_pr.append(header)


def _set_table_layout_fixed(table):
    tbl_pr = table._tbl.tblPr
    layout = tbl_pr.first_child_found_in("w:tblLayout")
    if layout is None:
        layout = OxmlElement("w:tblLayout")
        tbl_pr.append(layout)
    layout.set(qn("w:type"), "fixed")


def _set_page_number(paragraph):
    run = paragraph.add_run()
    _set_font(run, size=9, color=MUTED)
    begin = OxmlElement("w:fldChar")
    begin.set(qn("w:fldCharType"), "begin")
    instr = OxmlElement("w:instrText")
    instr.set(qn("xml:space"), "preserve")
    instr.text = " PAGE "
    separate = OxmlElement("w:fldChar")
    separate.set(qn("w:fldCharType"), "separate")
    text = OxmlElement("w:t")
    text.text = "1"
    end = OxmlElement("w:fldChar")
    end.set(qn("w:fldCharType"), "end")
    run._r.extend([begin, instr, separate, text, end])


def _set_paragraph_shading(paragraph, fill):
    p_pr = paragraph._p.get_or_add_pPr()
    shd = p_pr.find(qn("w:shd"))
    if shd is None:
        shd = OxmlElement("w:shd")
        p_pr.append(shd)
    shd.set(qn("w:fill"), fill)


def _set_keep_with_next(paragraph, value=True):
    paragraph.paragraph_format.keep_with_next = value


def _remove_paragraph_borders(element):
    """Remove built-in Word title rules that conflict with the report style."""
    p_pr = element.find(qn("w:pPr"))
    if p_pr is None:
        return
    borders = p_pr.find(qn("w:pBdr"))
    if borders is not None:
        p_pr.remove(borders)


def _split_table_row(line: str) -> list[str]:
    value = line.strip()
    if value.startswith("|"):
        value = value[1:]
    if value.endswith("|"):
        value = value[:-1]
    cells = re.split(r"(?<!\\)\|", value)
    return [cell.strip().replace("\\|", "|") for cell in cells]


def _is_table_separator(line: str) -> bool:
    cells = _split_table_row(line)
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell.replace(" ", "")) for cell in cells)


def parse_markdown(path: Path) -> list[dict]:
    """Parse the report's Markdown subset into typed blocks."""
    lines = path.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocks: list[dict] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue

        fence = re.match(r"^\s*(`{3,}|~{3,})([^`]*)$", line)
        if fence:
            marker = fence.group(1)
            info = fence.group(2).strip()
            code_lines = []
            i += 1
            while i < len(lines) and not re.match(rf"^\s*{re.escape(marker[0])}{{{len(marker)},}}\s*$", lines[i]):
                code_lines.append(lines[i])
                i += 1
            if i < len(lines):
                i += 1
            blocks.append({"kind": "code", "lang": info, "text": "\n".join(code_lines)})
            continue

        heading = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
        if heading:
            blocks.append({"kind": "heading", "level": len(heading.group(1)), "text": heading.group(2)})
            i += 1
            continue

        if i + 1 < len(lines) and "|" in line and _is_table_separator(lines[i + 1]):
            rows = [_split_table_row(line)]
            i += 2
            while i < len(lines) and lines[i].strip() and "|" in lines[i]:
                rows.append(_split_table_row(lines[i]))
                i += 1
            blocks.append({"kind": "table", "rows": rows})
            continue

        if re.match(r"^\s*>\s?", line):
            quote_lines = []
            while i < len(lines) and re.match(r"^\s*>\s?", lines[i]):
                quote_lines.append(re.sub(r"^\s*>\s?", "", lines[i]))
                i += 1
            blocks.append({"kind": "quote", "lines": quote_lines})
            continue

        if re.match(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)", line):
            list_lines = []
            while i < len(lines) and (re.match(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)", lines[i]) or not lines[i].strip()):
                if not lines[i].strip():
                    # A blank line terminates a list unless the next line is another list item.
                    if i + 1 < len(lines) and re.match(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)", lines[i + 1]):
                        i += 1
                        continue
                    break
                match = re.match(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$", lines[i])
                if match:
                    list_lines.append({
                        "ordered": match.group(2)[0].isdigit(),
                        "indent": len(match.group(1)),
                        "text": match.group(3),
                    })
                i += 1
            blocks.append({"kind": "list", "items": list_lines})
            continue

        if re.fullmatch(r"\s*(?:---+|\*\*\*+|___+)\s*", line):
            i += 1
            continue

        paragraph_lines = [line.rstrip()]
        i += 1
        while i < len(lines) and lines[i].strip():
            next_line = lines[i]
            if (re.match(r"^(#{1,6})\s+", next_line)
                    or re.match(r"^\s*(?:`{3,}|~{3,})", next_line)
                    or re.match(r"^\s*>\s?", next_line)
                    or re.match(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)", next_line)
                    or (i + 1 < len(lines) and "|" in next_line and _is_table_separator(lines[i + 1]))):
                break
            paragraph_lines.append(next_line.rstrip())
            i += 1
        blocks.append({"kind": "paragraph", "text": "\n".join(paragraph_lines)})
    return blocks


# Underscores inside identifiers and paths are literal Markdown text, not
# emphasis markers. Require underscore emphasis to be separated from word
# characters so names such as ``build_final_snapshot`` survive DOCX export.
INLINE_RE = re.compile(
    r"(\*\*.+?\*\*|(?<!\w)__.+?__(?!\w)|`[^`]+`|\*[^*\n]+\*|"
    r"(?<!\w)_([^_\n]+)_(?!\w)|\[[^\]]+\]\([^\)]+\))"
)


def add_inline(paragraph, text: str, *, size=10.5, color=BLACK, code_size=9.0):
    """Add a restrained subset of Markdown inline formatting to a paragraph."""
    pos = 0
    for match in INLINE_RE.finditer(str(text)):
        if match.start() > pos:
            run = paragraph.add_run(text[pos:match.start()])
            _set_font(run, size=size, color=color)
        token = match.group(0)
        if token.startswith("**") and token.endswith("**"):
            run = paragraph.add_run(token[2:-2])
            _set_font(run, size=size, bold=True, color=color)
        elif token.startswith("__") and token.endswith("__"):
            run = paragraph.add_run(token[2:-2])
            _set_font(run, size=size, bold=True, color=color)
        elif token.startswith("`") and token.endswith("`"):
            run = paragraph.add_run(token[1:-1])
            _set_font(run, name=MONO_FONT, size=code_size, color="17365D")
        elif token.startswith("*") and token.endswith("*"):
            run = paragraph.add_run(token[1:-1])
            _set_font(run, size=size, italic=True, color=color)
        elif token.startswith("_") and token.endswith("_"):
            run = paragraph.add_run(token[1:-1])
            _set_font(run, size=size, italic=True, color=color)
        else:
            link = re.match(r"\[([^\]]+)\]\(([^\)]+)\)", token)
            if link:
                run = paragraph.add_run(f"{link.group(1)} ({link.group(2)})")
                _set_font(run, size=size, color="1F4E78")
            else:
                run = paragraph.add_run(token)
                _set_font(run, size=size, color=color)
        pos = match.end()
    if pos < len(text):
        run = paragraph.add_run(text[pos:])
        _set_font(run, size=size, color=color)


def _table_widths(rows: list[list[str]], total_cm=17.0) -> list[float]:
    cols = max(len(row) for row in rows)
    weights = []
    for col in range(cols):
        samples = [row[col] for row in rows[: min(len(rows), 8)] if col < len(row)]
        longest = max((len(re.sub(r"[`*_]", "", str(value))) for value in samples), default=4)
        # Narrative columns need more room, but no single column may monopolize a page.
        weight = min(34.0, max(5.0, longest))
        weights.append(weight)
    minimum = 1.25
    # HTTP method/status columns should keep short tokens such as POST and
    # PATCH on one line; allow the width allocator to reserve a little more
    # room for these compact categorical fields.
    for col in range(cols):
        samples = [str(row[col]).strip().upper() for row in rows[1:] if col < len(row)]
        if samples and all(value in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
                           for value in samples if value):
            weights[col] = max(weights[col], 8.0)
    available = total_cm - minimum * cols
    if available <= 0:
        return [total_cm / cols] * cols
    residual = sum(max(0.0, weight - 5.0) for weight in weights)
    widths = []
    for weight in weights:
        extra = available * (max(0.0, weight - 5.0) / residual) if residual else available / cols
        widths.append(minimum + extra)
    # Keep very wide narrative columns readable and redistribute any excess.
    cap = total_cm * 0.58
    excess = sum(max(0.0, width - cap) for width in widths)
    if excess:
        widths = [min(width, cap) for width in widths]
        room = sum(max(0.0, cap - width) for width in widths)
        if room:
            widths = [width + excess * max(0.0, cap - width) / room for width in widths]
    scale = total_cm / sum(widths)
    return [width * scale for width in widths]


def add_table(doc: Document, rows: list[list[str]]):
    cols = max(len(row) for row in rows)
    normalized = [row + [""] * (cols - len(row)) for row in rows]
    table = doc.add_table(rows=len(normalized), cols=cols)
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.autofit = False
    _set_table_layout_fixed(table)
    _set_table_borders(table)
    widths = _table_widths(normalized)
    font_size = 8.5 if cols >= 6 else 9.0 if cols >= 4 else 9.5

    for row_index, values in enumerate(normalized):
        row = table.rows[row_index]
        if row_index == 0:
            _set_repeat_table_header(row)
        for col_index, value in enumerate(values):
            cell = row.cells[col_index]
            cell.width = Cm(widths[col_index])
            cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
            _set_cell_margins(cell)
            if row_index == 0:
                _set_cell_shading(cell, NAVY)
            elif row_index % 2 == 0:
                _set_cell_shading(cell, PALE_BLUE)
            else:
                _set_cell_shading(cell, "FFFFFF")
            paragraph = cell.paragraphs[0]
            paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER if row_index == 0 else WD_ALIGN_PARAGRAPH.LEFT
            _set_paragraph_spacing(paragraph, after=0, line=1.08)
            if row_index == 0:
                # Keep a table header from being stranded at the bottom of a page.
                _set_keep_with_next(paragraph)
            add_inline(paragraph, value, size=font_size, color="FFFFFF" if row_index == 0 else BLACK,
                       code_size=max(7.5, font_size - 1))
    doc.add_paragraph().paragraph_format.space_after = Pt(2)
    return table


def add_code_block(doc: Document, text: str, lang: str):
    paragraph = doc.add_paragraph()
    paragraph.paragraph_format.left_indent = Cm(0.35)
    paragraph.paragraph_format.right_indent = Cm(0.2)
    paragraph.paragraph_format.space_before = Pt(3)
    paragraph.paragraph_format.space_after = Pt(8)
    paragraph.paragraph_format.line_spacing = 1.05
    _set_paragraph_shading(paragraph, PALE_GRAY)
    run = paragraph.add_run(text)
    _set_font(run, name=MONO_FONT, size=8.4, color="263238")
    # Allow long JSON and schema lines to wrap rather than overflow the page.
    p_pr = paragraph._p.get_or_add_pPr()
    word_wrap = OxmlElement("w:wordWrap")
    word_wrap.set(qn("w:val"), "true")
    p_pr.append(word_wrap)
    return paragraph


def _configure_styles(doc: Document):
    styles = doc.styles
    normal = styles["Normal"]
    normal.font.name = BODY_FONT
    normal._element.rPr.rFonts.set(qn("w:eastAsia"), BODY_FONT)
    normal.font.size = Pt(10.5)
    normal.font.color.rgb = RGBColor.from_string(BLACK)

    title = styles["Title"]
    title.font.name = BODY_FONT
    title._element.rPr.rFonts.set(qn("w:eastAsia"), BODY_FONT)
    title.font.size = Pt(25)
    title.font.bold = True
    title.font.color.rgb = RGBColor.from_string(BLACK)
    title.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.CENTER
    title.paragraph_format.space_after = Pt(15)
    _remove_paragraph_borders(title._element)

    for name, size, before, after in (("Heading 1", 16, 16, 7), ("Heading 2", 12.5, 12, 5), ("Heading 3", 11, 8, 4)):
        style = styles[name]
        style.font.name = BODY_FONT
        style._element.rPr.rFonts.set(qn("w:eastAsia"), BODY_FONT)
        style.font.size = Pt(size)
        style.font.bold = True
        style.font.color.rgb = RGBColor.from_string(BLACK)
        style.paragraph_format.space_before = Pt(before)
        style.paragraph_format.space_after = Pt(after)
        style.paragraph_format.keep_with_next = True

    if "Report Metadata" not in styles:
        metadata = styles.add_style("Report Metadata", WD_STYLE_TYPE.PARAGRAPH)
    else:
        metadata = styles["Report Metadata"]
    metadata.font.name = BODY_FONT
    metadata._element.rPr.rFonts.set(qn("w:eastAsia"), BODY_FONT)
    metadata.font.size = Pt(10)
    metadata.font.color.rgb = RGBColor.from_string(MUTED)
    metadata.paragraph_format.space_after = Pt(5)

    if "Report Code" not in styles:
        code = styles.add_style("Report Code", WD_STYLE_TYPE.PARAGRAPH)
    else:
        code = styles["Report Code"]
    code.font.name = MONO_FONT
    code.font.size = Pt(8.4)
    code.paragraph_format.space_after = Pt(8)


def _set_document_properties(doc: Document):
    props = doc.core_properties
    props.title = "上市公司年报风险智能识别系统技术报告"
    props.subject = "v5.3 GA 技术实现与审计证据链说明"
    props.author = "Audit Risk Agent"
    props.keywords = "年报风险,审计,Agent,report_snapshot,v5.3 GA"


def _clear_paragraph(paragraph):
    """Remove existing runs while preserving paragraph properties and fields."""
    for child in list(paragraph._p):
        if child.tag != qn("w:pPr"):
            paragraph._p.remove(child)


def _clear_document_body(doc: Document):
    """Clear template body content while retaining section properties and styles."""
    body = doc._element.body
    section_properties = body.find(qn("w:sectPr"))
    for child in list(body):
        if child is not section_properties:
            body.remove(child)


def _add_header_footer(doc: Document):
    for section in doc.sections:
        section.header_distance = Cm(0.8)
        section.footer_distance = Cm(0.8)
        header = section.header.paragraphs[0]
        _clear_paragraph(header)
        header.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        _set_paragraph_spacing(header, after=0, line=1.0)
        run = header.add_run("上市公司年报风险智能识别系统  |  v5.3 GA")
        _set_font(run, size=8.5, color=MUTED)
        footer = section.footer.paragraphs[0]
        _clear_paragraph(footer)
        footer.alignment = WD_ALIGN_PARAGRAPH.CENTER
        _set_paragraph_spacing(footer, after=0, line=1.0)
        left = footer.add_run("上市公司年报风险智能识别系统技术报告  |  第 ")
        _set_font(left, size=8.5, color=MUTED)
        _set_page_number(footer)
        right = footer.add_run(" 页")
        _set_font(right, size=8.5, color=MUTED)


def _metadata_from_blocks(blocks: list[dict]) -> tuple[str, list[tuple[str, str]], int, int]:
    title_index = next((i for i, block in enumerate(blocks) if block["kind"] == "heading" and block["level"] == 1), -1)
    title = blocks[title_index]["text"] if title_index >= 0 else "技术报告"
    metadata: list[tuple[str, str]] = []
    quote_index = -1
    if title_index >= 0:
        for i in range(title_index + 1, min(len(blocks), title_index + 4)):
            if blocks[i]["kind"] != "quote":
                continue
            quote_index = i
            for line in blocks[i]["lines"]:
                line = line.strip()
                if not line:
                    continue
                if "：" in line:
                    key, value = line.split("：", 1)
                elif ":" in line:
                    key, value = line.split(":", 1)
                else:
                    key, value = "说明", line
                metadata.append((key.strip(), value.strip()))
            break
    return title, metadata, title_index, quote_index


def _chapter_index(blocks: list[dict]) -> list[tuple[str, str]]:
    items = []
    for block in blocks:
        if block["kind"] == "heading" and block["level"] == 2:
            title = block["text"]
            match = re.match(r"(\d+)[.、]?\s+(.+)", title)
            if match:
                items.append((match.group(1), match.group(2)))
    return items


def build(markdown_path: Path, output_path: Path, template_path: Path | None = None):
    blocks = parse_markdown(markdown_path)
    title, metadata, title_index, quote_index = _metadata_from_blocks(blocks)
    doc = Document(template_path) if template_path else Document()
    if template_path:
        _clear_document_body(doc)
    _configure_styles(doc)
    _set_document_properties(doc)
    section = doc.sections[0]
    section.page_width = Cm(21.0)
    section.page_height = Cm(29.7)
    section.top_margin = Cm(1.9)
    section.bottom_margin = Cm(1.8)
    section.left_margin = Cm(2.0)
    section.right_margin = Cm(2.0)
    _add_header_footer(doc)

    # Cover: typography and whitespace carry the hierarchy; no decorative title rule.
    kicker = doc.add_paragraph()
    kicker.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _set_paragraph_spacing(kicker, before=28, after=18, line=1.0)
    run = kicker.add_run("TECHNICAL REPORT")
    _set_font(run, size=9, bold=True, color=BLUE)

    title_paragraph = doc.add_paragraph(style="Title")
    _remove_paragraph_borders(title_paragraph._p)
    title_paragraph.add_run(title)
    for run in title_paragraph.runs:
        _set_font(run, size=25, bold=True, color=BLACK)

    subtitle = doc.add_paragraph()
    subtitle.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _set_paragraph_spacing(subtitle, after=30, line=1.1)
    run = subtitle.add_run("v5.3 GA 技术实现与审计证据链说明")
    _set_font(run, size=13, color=MUTED)

    if metadata:
        metadata_table = doc.add_table(rows=len(metadata), cols=2)
        metadata_table.alignment = WD_TABLE_ALIGNMENT.CENTER
        metadata_table.autofit = False
        _set_table_layout_fixed(metadata_table)
        _set_table_borders(metadata_table, color="E4E8EC", size="5")
        # The first metadata row is the table's semantic header for readers
        # that navigate DOCX tables structurally; the visual cover remains a
        # compact key/value table.
        _set_repeat_table_header(metadata_table.rows[0])
        for idx, (key, value) in enumerate(metadata):
            left, right = metadata_table.rows[idx].cells
            left.width = Cm(4.0)
            right.width = Cm(12.0)
            left.vertical_alignment = right.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
            _set_cell_margins(left, top=100, bottom=100)
            _set_cell_margins(right, top=100, bottom=100)
            _set_cell_shading(left, PALE_BLUE)
            _set_cell_shading(right, "FFFFFF")
            p_left = left.paragraphs[0]
            p_left.alignment = WD_ALIGN_PARAGRAPH.RIGHT
            _set_paragraph_spacing(p_left, after=0, line=1.0)
            r_left = p_left.add_run(key)
            _set_font(r_left, size=9.5, bold=True, color=NAVY)
            p_right = right.paragraphs[0]
            _set_paragraph_spacing(p_right, after=0, line=1.0)
            r_right = p_right.add_run(value)
            _set_font(r_right, size=9.5, color=BLACK)

    cover_note = doc.add_paragraph(style="Report Metadata")
    cover_note.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _set_paragraph_spacing(cover_note, before=24, after=0, line=1.15)
    r = cover_note.add_run("本报告基于系统实际实现及 v5.3 GA 验证结果编制")
    _set_font(r, size=9.5, color=MUTED)
    doc.add_page_break()

    # Static navigation page keeps the report useful in Word and in PDF conversion.
    nav_heading = doc.add_heading("内容导航", level=1)
    _set_keep_with_next(nav_heading)
    nav_intro = doc.add_paragraph("章节按输入安全、事实契约、确定性计算、Agent 复核、终局发布和交付验收的依赖顺序组织。")
    _set_paragraph_spacing(nav_intro, after=10, line=1.2)
    add_inline(nav_intro, "", size=10.5)
    chapters = _chapter_index(blocks)
    nav_rows = [["章节", "主题"]] + [[number, chapter] for number, chapter in chapters]
    add_table(doc, nav_rows)

    # Body excludes the Markdown title and its metadata quote, which are represented on the cover.
    skip = {title_index}
    if quote_index >= 0:
        skip.add(quote_index)
    for index, block in enumerate(blocks):
        if index in skip:
            continue
        kind = block["kind"]
        if kind == "heading":
            if block["level"] == 1:
                continue
            # The detailed financial metric ledgers are intentionally paginated
            # as complete reading units. This prevents a ledger heading from
            # being separated from its table or leaving a single trailing row
            # on the following page after the report was expanded.
            if block["level"] >= 3 and block["text"].startswith("6.1."):
                doc.add_page_break()
            level = min(3, max(1, block["level"] - 1))
            paragraph = doc.add_heading(block["text"], level=level)
            for run in paragraph.runs:
                _set_font(run, size={1: 16, 2: 12.5, 3: 11}[level], bold=True, color=BLACK)
            _set_keep_with_next(paragraph)
        elif kind == "paragraph":
            paragraph = doc.add_paragraph()
            _set_paragraph_spacing(paragraph, after=7, line=1.22)
            add_inline(paragraph, block["text"], size=10.5)
        elif kind == "quote":
            for line in block["lines"]:
                paragraph = doc.add_paragraph()
                paragraph.paragraph_format.left_indent = Cm(0.45)
                paragraph.paragraph_format.right_indent = Cm(0.2)
                _set_paragraph_spacing(paragraph, after=4, line=1.15)
                add_inline(paragraph, line, size=9.5, color=MUTED)
                for run in paragraph.runs:
                    run.italic = True
        elif kind == "list":
            for item in block["items"]:
                style = "List Number" if item["ordered"] else "List Bullet"
                paragraph = doc.add_paragraph(style=style)
                paragraph.paragraph_format.left_indent = Cm(0.55 + min(item["indent"], 8) * 0.04)
                paragraph.paragraph_format.first_line_indent = Cm(-0.25)
                _set_paragraph_spacing(paragraph, after=3, line=1.16)
                add_inline(paragraph, item["text"], size=10.2)
        elif kind == "table":
            add_table(doc, block["rows"])
        elif kind == "code":
            add_code_block(doc, block["text"], block["lang"])

    output_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(output_path)
    print(f"[docx] generated: {output_path}")
    print(f"[docx] source: {markdown_path}")
    print(f"[docx] chapters: {len(chapters)}")


def main():
    parser = argparse.ArgumentParser(description="Build the technical report Word edition")
    parser.add_argument("--markdown", type=Path, default=DEFAULT_MARKDOWN)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--template", type=Path, default=None,
                        help="Optional DOCX template whose page, style, header and footer system is retained")
    args = parser.parse_args()
    template = args.template.resolve() if args.template else None
    build(args.markdown.resolve(), args.output.resolve(), template)


if __name__ == "__main__":
    main()
