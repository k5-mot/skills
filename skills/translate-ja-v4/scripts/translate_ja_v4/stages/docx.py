"""DocxStageでpandoc出力と連続見出し余白を処理する。"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from ..config import PipelineState, state_options, state_paths
from ..io import LOGGER, hash_file, hash_json, record_stage, stage_cached

WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
W = f"{{{WORD_NS}}}"


def _read_package(path: Path) -> dict[str, bytes]:
    """DOCX packageの全fileをmemoryへ読む。

    Args:
        path: 読み込むDOCX path。

    Returns:
        package内pathからbinary内容への対応。
    """

    with zipfile.ZipFile(path) as source:
        return {
            name: source.read(name)
            for name in source.namelist()
            if not name.endswith("/")
        }


def _write_package(path: Path, payloads: dict[str, bytes]) -> None:
    """memory上のfile群でDOCX packageを安全に置換する。

    Args:
        path: 更新対象DOCX path。
        payloads: package内pathからbinary内容への対応。

    Returns:
        なし。

    Side Effects:
        一時fileを経由してpathを置換する。
    """

    with tempfile.NamedTemporaryFile(
        suffix=".docx", dir=path.parent, delete=False
    ) as temporary:
        temporary_path = Path(temporary.name)
    try:
        with zipfile.ZipFile(temporary_path, "w", zipfile.ZIP_DEFLATED) as target:
            for name, payload in payloads.items():
                target.writestr(name, payload)
        temporary_path.replace(path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _paragraph_text(paragraph: ET.Element) -> str:
    """Word段落の表示文字列を返す。

    Args:
        paragraph: Word paragraph XML。

    Returns:
        全text nodeを連結した文字列。
    """

    return "".join(node.text or "" for node in paragraph.findall(f".//{W}t"))


def _all_text_bold(paragraph: ET.Element) -> bool:
    """表示文字を持つrunがすべて太字か判定する。

    Args:
        paragraph: Word paragraph XML。

    Returns:
        1件以上のtext runがすべて太字ならTrue。
    """

    runs = [run for run in paragraph.findall(f"{W}r") if _paragraph_text(run)]
    return bool(runs) and all(run.find(f"{W}rPr/{W}b") is not None for run in runs)


def _field_paragraph(instruction: str, text: str = "") -> ET.Element:
    """更新可能なWord fieldを含む段落を作る。

    Args:
        instruction: TOCまたはSEQ field instruction。
        text: field前に表示する固定文字列。

    Returns:
        新しいWord paragraph XML。
    """

    paragraph = ET.Element(f"{W}p")
    if text:
        run = ET.SubElement(paragraph, f"{W}r")
        ET.SubElement(run, f"{W}t").text = text
    field = ET.SubElement(paragraph, f"{W}fldSimple")
    field.set(f"{W}instr", instruction)
    return paragraph


def _index_title(text: str) -> ET.Element:
    """自動index用の見出し段落を作る。

    Args:
        text: 「目次」などの表示文字列。

    Returns:
        TOCHeading styleのWord paragraph XML。
    """

    paragraph = ET.Element(f"{W}p")
    properties = ET.SubElement(paragraph, f"{W}pPr")
    style = ET.SubElement(properties, f"{W}pStyle")
    style.set(f"{W}val", "TOCHeading")
    run = ET.SubElement(paragraph, f"{W}r")
    ET.SubElement(run, f"{W}t").text = text
    return paragraph


def _prepend_field(paragraph: ET.Element, instruction: str) -> None:
    """caption段落の先頭へSEQ fieldと空白を追加する。

    Args:
        paragraph: 更新対象caption段落。
        instruction: SEQ field instruction。

    Returns:
        なし。

    Side Effects:
        paragraphの子要素を追加する。
    """

    offset = 1 if paragraph.find(f"{W}pPr") is not None else 0
    field = ET.Element(f"{W}fldSimple")
    field.set(f"{W}instr", instruction)
    spacer = ET.Element(f"{W}r")
    text = ET.SubElement(spacer, f"{W}t")
    text.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    text.text = " "
    paragraph.insert(offset, field)
    paragraph.insert(offset + 1, spacer)


def _enhance_word(path: Path) -> dict[str, int]:
    """水平線、caption番号、自動indexをWord固有表現へ変換する。

    Args:
        path: pandoc生成DOCX。

    Returns:
        horizontal rule、caption、indexの変更件数。

    Side Effects:
        DOCX package内のdocument.xmlとsettings.xmlを更新する。
    """

    payloads = _read_package(path)
    root = ET.fromstring(payloads["word/document.xml"])
    body = root.find(f"{W}body")
    counts = {"horizontal_rules": 0, "captions": 0, "indexes": 0}
    if body is None:
        return counts

    children = list(body)
    for index, paragraph in enumerate(children):
        if paragraph.tag != f"{W}p":
            continue
        if _paragraph_text(paragraph).strip() == "---":
            properties = paragraph.find(f"{W}pPr")
            if properties is None:
                properties = ET.Element(f"{W}pPr")
                paragraph.insert(0, properties)
            for child in list(paragraph):
                if child is not properties:
                    paragraph.remove(child)
            borders = properties.find(f"{W}pBdr")
            if borders is None:
                borders = ET.SubElement(properties, f"{W}pBdr")
            bottom = ET.SubElement(borders, f"{W}bottom")
            for key, value in {
                "val": "single",
                "sz": "6",
                "space": "1",
                "color": "auto",
            }.items():
                bottom.set(f"{W}{key}", value)
            counts["horizontal_rules"] += 1

        style = paragraph.find(f"{W}pPr/{W}pStyle")
        style_name = style.get(f"{W}val", "").lower() if style is not None else ""
        previous = children[index - 1] if index else None
        following = children[index + 1] if index + 1 < len(children) else None
        figure = "imagecaption" in style_name or "figurecaption" in style_name
        table = "tablecaption" in style_name
        if style_name == "caption":
            figure = (
                previous is not None and previous.find(f".//{W}drawing") is not None
            )
            table = following is not None and following.tag == f"{W}tbl"
        elif not table and following is not None and following.tag == f"{W}tbl":
            table = _all_text_bold(paragraph)
        has_sequence = paragraph.find(f"{W}fldSimple") is not None or any(
            "SEQ " in (node.text or "")
            for node in paragraph.findall(f".//{W}instrText")
        )
        if (figure or table) and not has_sequence:
            _prepend_field(
                paragraph, " SEQ 図 \\* ARABIC " if figure else " SEQ 表 \\* ARABIC "
            )
            counts["captions"] += 1

    existing = [
        field.get(f"{W}instr", "") for field in root.findall(f".//{W}fldSimple")
    ] + [node.text or "" for node in root.findall(f".//{W}instrText")]
    indexes = (
        ("目次", ' TOC \\o "1-6" \\h \\z \\u ', '\\o "1-6"'),
        ("図目次", ' TOC \\h \\z \\c "図" ', '\\c "図"'),
        ("表目次", ' TOC \\h \\z \\c "表" ', '\\c "表"'),
    )
    missing = [
        item for item in indexes if not any(item[2] in value for value in existing)
    ]
    for title, instruction, _marker in reversed(missing):
        body.insert(0, _field_paragraph(instruction))
        body.insert(0, _index_title(title))
    counts["indexes"] = len(missing)

    settings_payload = payloads.get("word/settings.xml")
    if settings_payload:
        settings = ET.fromstring(settings_payload)
        update = settings.find(f"{W}updateFields")
        if update is None:
            update = ET.SubElement(settings, f"{W}updateFields")
        update.set(f"{W}val", "true")
        payloads["word/settings.xml"] = ET.tostring(
            settings, encoding="utf-8", xml_declaration=True
        )
    payloads["word/document.xml"] = ET.tostring(
        root, encoding="utf-8", xml_declaration=True
    )
    _write_package(path, payloads)
    return counts


def _fix_heading_spacing(path: Path) -> int:
    """直接連続する見出し段落間の前後余白だけを0にする。

    Args:
        path: pandoc生成DOCX。

    Returns:
        補正した見出し組数。

    Side Effects:
        DOCX package内のdocument.xmlを更新する。
    """

    namespace = {"w": WORD_NS}
    payloads = _read_package(path)
    root = ET.fromstring(payloads["word/document.xml"])
    paragraphs = root.findall(".//w:body/w:p", namespace)

    def heading(paragraph: ET.Element) -> bool:
        """段落がHeading styleか判定する。

        Args:
            paragraph: Word paragraph XML。

        Returns:
            Heading1からHeading6ならTrue。
        """

        style = paragraph.find("w:pPr/w:pStyle", namespace)
        value = style.get(f"{{{WORD_NS}}}val", "") if style is not None else ""
        return value.lower().replace(" ", "").startswith("heading")

    changed = 0
    for left, right in zip(paragraphs, paragraphs[1:]):
        if not heading(left) or not heading(right):
            continue
        for paragraph, key in ((left, "after"), (right, "before")):
            properties = paragraph.find("w:pPr", namespace)
            if properties is None:
                properties = ET.SubElement(paragraph, f"{{{WORD_NS}}}pPr")
            spacing = properties.find("w:spacing", namespace)
            if spacing is None:
                spacing = ET.SubElement(properties, f"{{{WORD_NS}}}spacing")
            spacing.set(f"{{{WORD_NS}}}{key}", "0")
        changed += 1
    if not changed:
        return 0
    payloads["word/document.xml"] = ET.tostring(
        root, encoding="utf-8", xml_declaration=True
    )
    _write_package(path, payloads)
    return changed


def docx_stage(state: PipelineState) -> PipelineState:
    """MarkdownをpandocでWordへ変換するLangGraph node。

    Args:
        state: Markdown成果物を含むgraph state。

    Returns:
        DOCX成果物パスを設定した部分state。
    """

    options, paths = state_options(state), state_paths(state)
    template_hash = hash_file(options.template) if options.template else None
    input_hash = hash_file(paths.markdown)
    config_hash = hash_json(
        {"version": 2, "skip": options.skip_docx, "template": template_hash}
    )
    if options.skip_docx:
        record_stage(
            paths.manifest, "docx", "skipped", input_hash, config_hash, paths.docx
        )
        return {"current_path": str(paths.markdown), "completed_stage": "docx"}
    if stage_cached(paths.manifest, "docx", input_hash, config_hash, paths.docx):
        LOGGER.info("Resumed DocxStage output=%s", paths.docx)
        return {"current_path": str(paths.docx), "completed_stage": "docx"}
    if not shutil.which("pandoc"):
        raise RuntimeError("pandoc is required for DocxStage")
    paths.docx.parent.mkdir(parents=True, exist_ok=True)
    record_stage(paths.manifest, "docx", "running", input_hash, config_hash, paths.docx)
    command = [
        "pandoc",
        str(paths.markdown),
        "--from",
        "markdown",
        "--to",
        "docx",
        "--output",
        str(paths.docx),
    ]
    if options.template:
        command.extend(["--reference-doc", str(options.template.resolve())])
    subprocess.run(command, cwd=paths.output_dir, check=True)
    enhancements = _enhance_word(paths.docx)
    adjusted = _fix_heading_spacing(paths.docx)
    record_stage(
        paths.manifest,
        "docx",
        "completed",
        input_hash,
        config_hash,
        paths.docx,
        {"heading_pairs": adjusted, **enhancements},
    )
    return {"current_path": str(paths.docx), "completed_stage": "docx"}
