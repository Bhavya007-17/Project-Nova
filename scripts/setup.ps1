<#
  Nova setup for a fresh clone (Windows 11). Safe to run again: finished steps are skipped or quick.

    powershell -ExecutionPolicy Bypass -File scripts\setup.ps1 [-SkipModels] [-SkipOllama] [-SkipChrome]

  1. checks uv   2. Python 3.12 + dependencies   3. speech models (~670 MB, SHA-256 checked)
  4. local LLMs through Ollama (~4 GB)   5. Chrome native-messaging host   6. nova doctor
#>
param([switch]$SkipModels, [switch]$SkipOllama, [switch]$SkipChrome)
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

function Step($text) { Write-Host "`n==> $text" -ForegroundColor Cyan }
function Warn($text) { Write-Host "    ! $text" -ForegroundColor Yellow }
function Check-Exit($what) { if ($LASTEXITCODE -ne 0) { Write-Host "    x $what failed" -ForegroundColor Red; exit 1 } }

Step 'uv'
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Warn 'uv is not installed. Install it, open a NEW terminal, and run this script again:'
    Write-Host '    powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"'
    exit 1
}
$uvVersion = ((uv --version) -split ' ')[1]
Write-Host "    uv $uvVersion"
if ([version]($uvVersion -replace '[^0-9.].*$', '') -lt [version]'0.12') {
    Warn 'uv < 0.12 makes a pythonw.exe that opens a console window (uv #19226). Run: uv self update'
}

Step 'Python 3.12 + dependencies'
uv python install 3.12; Check-Exit 'uv python install'
uv sync; Check-Exit 'uv sync'

if (-not $SkipModels) {
    Step 'Speech models (Parakeet + Silero, ~670 MB the first time)'
    uv run python scripts/get_models.py; Check-Exit 'model download'
}

if (-not $SkipOllama) {
    Step 'Local LLMs (Ollama)'
    $py = "import yaml; l = yaml.safe_load(open('config/settings.yaml', encoding='utf-8'))['llm']; print(l['gpu_model']); print(l['cpu_model'])"
    $models = uv run python -c $py | Select-Object -Unique
    if (-not (Get-Command ollama -ErrorAction SilentlyContinue)) {
        Warn 'Ollama is not installed: https://ollama.com/download/windows (or: winget install Ollama.Ollama). Then run:'
        $models | ForEach-Object { Write-Host "    ollama pull $_" }
    } else {
        foreach ($m in $models) {
            ollama pull $m
            if ($LASTEXITCODE -ne 0) { Warn "ollama pull $m failed. Is Ollama running? (Start menu > Ollama)" }
        }
    }
}

if (-not $SkipChrome) {
    Step 'Chrome extension (native-messaging host)'
    uv run nova chrome --install; Check-Exit 'nova chrome --install'
    Write-Host "    One manual step: chrome://extensions > Developer mode > Load unpacked > $Root\extension"
}

Step 'nova doctor'
uv run nova doctor
Write-Host "`nSetup finished. Start Nova with:  uv run nova" -ForegroundColor Green
