# Use Chunk JSONL as the translation source

Translate JA will generate Chunk JSONL directly from Gold JSON instead of exporting a full English Markdown document first. This avoids depending on whether Docling Serve can re-export corrected Docling Schema JSON to Markdown, and makes chunking, resume behavior, and structure-preserving translation use one stable source of truth.
