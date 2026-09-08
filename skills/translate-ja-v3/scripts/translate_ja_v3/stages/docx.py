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
    with zipfile.ZipFile(path) as source:
        payloads = {
            name: source.read(name)
            for name in source.namelist()
            if not name.endswith("/")
        }
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
        {"version": 1, "skip": options.skip_docx, "template": template_hash}
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
    adjusted = _fix_heading_spacing(paths.docx)
    record_stage(
        paths.manifest,
        "docx",
        "completed",
        input_hash,
        config_hash,
        paths.docx,
        {"heading_pairs": adjusted},
    )
    return {"current_path": str(paths.docx), "completed_stage": "docx"}
