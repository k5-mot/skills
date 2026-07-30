# Translate JA

Translate JA converts source documents into Japanese deliverables while preserving document structure through Docling-derived intermediate representations.

## Language

**Docling Schema JSON**:
A structured JSON representation of a source document produced by Docling. It is the structural source of truth before chunking.
_Avoid_: Docling JSON when the schema meaning matters

**Chunk JSONL**:
The line-delimited JSON format that stores translation units derived from Docling Schema JSON. It is the source of truth for chunk translation and resume behavior.
_Avoid_: Chunk Markdown, split Markdown

**Bronze JSON**:
The first Docling Schema JSON produced directly from the source document.
_Avoid_: Raw JSON

**Silver JSON**:
The Docling Schema JSON after LLM/VLM structural realignment.
_Avoid_: Corrected JSON

**Gold JSON**:
The Docling Schema JSON after deterministic text cleanup.
_Avoid_: Preprocessed JSON
