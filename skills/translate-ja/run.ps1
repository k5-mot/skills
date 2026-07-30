param(
    [string]$InputPath = "./docs/source/source.pdf",
    [string]$OutputDir = "",
    [string]$Template = "",
    [switch]$Force
)

$ErrorActionPreference = "Stop"

function Write-StepLog {
    param([string]$Message)
    Write-Host "[translate-ja] $Message"
}

function Import-DotEnv {
    param([string]$Path)

    if (-not (Test-Path -LiteralPath $Path)) {
        return
    }

    Get-Content -LiteralPath $Path | ForEach-Object {
        $line = $_.Trim()
        if ($line.Length -eq 0 -or $line.StartsWith("#")) {
            return
        }

        $parts = $line -split "=", 2
        if ($parts.Count -ne 2) {
            return
        }

        $name = $parts[0].Trim()
        $value = $parts[1].Trim().Trim('"').Trim("'")
        [Environment]::SetEnvironmentVariable($name, $value, "Process")
    }
}

function Invoke-Step {
    param(
        [string]$Label,
        [string]$OutputPath,
        [string[]]$Command
    )

    if (-not $Force -and (Test-Path -LiteralPath $OutputPath)) {
        Write-StepLog "skip ${Label}: ${OutputPath} already exists"
        return
    }

    Write-StepLog "run ${Label}"
    $exe = $Command[0]
    $args = $Command[1..($Command.Count - 1)]
    & $exe @args
}

function Add-ForceArg {
    param([string[]]$Command)

    if ($Force) {
        return @($Command + "--force")
    }

    return $Command
}

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Resolve-Path (Join-Path $ScriptDir "../..")

if (-not (Test-Path -LiteralPath $InputPath)) {
    throw "Input file not found: $InputPath"
}

$InputItem = Get-Item -LiteralPath $InputPath
$InputAbs = $InputItem.FullName
$InputDir = $InputItem.DirectoryName
$Stem = [System.IO.Path]::GetFileNameWithoutExtension($InputItem.Name)

if ([string]::IsNullOrWhiteSpace($OutputDir)) {
    $OutputDir = Join-Path $InputDir "output"
}

if ([string]::IsNullOrWhiteSpace($Template)) {
    $Template = Join-Path $ScriptDir "template.dotx"
}

New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $OutputDir "artifacts") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $OutputDir "chunks-en") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $OutputDir "chunks-ja") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $OutputDir "reports") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $OutputDir "logs") | Out-Null

Import-DotEnv -Path (Join-Path $RepoRoot ".env")

$PythonBin = if ($env:PYTHON_BIN) { $env:PYTHON_BIN } else { "python" }
$ScriptsDir = Join-Path $ScriptDir "scripts"

$BronzeJson = Join-Path $OutputDir "$Stem.bronze.json"
$SilverJson = Join-Path $OutputDir "$Stem.silver.json"
$GoldJson = Join-Path $OutputDir "$Stem.gold.json"
$ChunksEnDir = Join-Path $OutputDir "chunks-en"
$ChunksJaDir = Join-Path $OutputDir "chunks-ja"
$ChunksEnJsonl = Join-Path $ChunksEnDir "chunks.source.jsonl"
$ChunksJaJsonl = Join-Path $ChunksJaDir "chunks.ja.jsonl"
$JaMd = Join-Path $OutputDir "$Stem.ja.md"
$JaDocx = Join-Path $OutputDir "$Stem.ja.docx"
$PreprocessReport = Join-Path (Join-Path $OutputDir "reports") "preprocess_report.json"

Invoke-Step "1 preprocess document with Docling" $BronzeJson (Add-ForceArg -Command @(
    $PythonBin, (Join-Path $ScriptsDir "preprocess_doc_with_docling.py"),
    "--input", $InputAbs,
    "--output", $BronzeJson
))

Invoke-Step "2 realign document structure with LLM" $SilverJson (Add-ForceArg -Command @(
    $PythonBin, (Join-Path $ScriptsDir "realign_doc_struct_with_llm.py"),
    "--input", $BronzeJson,
    "--output", $SilverJson
))

Invoke-Step "3 clean Docling schema JSON" $GoldJson (Add-ForceArg -Command @(
    $PythonBin, (Join-Path $ScriptsDir "clean_doc.py"),
    "--input", $SilverJson,
    "--output", $GoldJson,
    "--report", $PreprocessReport
))

Invoke-Step "4 chunk Docling schema JSON" $ChunksEnJsonl (Add-ForceArg -Command @(
    $PythonBin, (Join-Path $ScriptsDir "chunk_docling_json.py"),
    "--input", $GoldJson,
    "--output", $ChunksEnJsonl
))

Invoke-Step "5 translate chunks" $ChunksJaJsonl (Add-ForceArg -Command @(
    $PythonBin, (Join-Path $ScriptsDir "translate_chunks.py"),
    "--input", $ChunksEnDir,
    "--output", $ChunksJaDir
))

Invoke-Step "6 concatenate translated chunks" $JaMd (Add-ForceArg -Command @(
    $PythonBin, (Join-Path $ScriptsDir "concat_chunks.py"),
    "--input", $ChunksJaDir,
    "--output", $JaMd
))

Invoke-Step "7 convert Markdown to docx" $JaDocx (Add-ForceArg -Command @(
    $PythonBin, (Join-Path $ScriptsDir "convert_md_to_docx_with_docling.py"),
    "--input", $JaMd,
    "--output", $JaDocx,
    "--template", $Template
))

Write-StepLog "done: $JaDocx"
