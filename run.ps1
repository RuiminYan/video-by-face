<#
.SYNOPSIS
    一键处理魔方比赛视频: OCR识别成绩 → 重命名 → 按人脸分类

.DESCRIPTION
    Step 1: 用 ocr_timer.py 识别大显上的成绩, 重命名视频文件 (如 IMG_1234.MP4 → 4.716.MP4)
    Step 2: 用 classify.py 按人脸识别将视频分到选手文件夹

.PARAMETER VideoDir
    包含视频文件的源目录 (必填)

.PARAMETER DryRun
    仅预览, 不实际操作

.PARAMETER Copy
    分类时复制而非移动视频

.PARAMETER SkipOCR
    跳过 OCR 重命名步骤 (视频已正确命名时使用)

.PARAMETER SkipClassify
    跳过人脸分类步骤

.EXAMPLE
    .\run.ps1 "Z:\cube-comp\260307"
    .\run.ps1 "Z:\cube-comp\260307" -DryRun
    .\run.ps1 "Z:\cube-comp\260307" -SkipOCR
#>

param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string]$VideoDir,

    [switch]$DryRun,
    [switch]$Copy,
    [switch]$SkipOCR,
    [switch]$SkipClassify
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

# Resolve paths
$VideoDir = (Resolve-Path $VideoDir).Path
$OcrScript = Join-Path $ScriptDir "ocr_timer.py"
$ClassifyScript = Join-Path $ScriptDir "classify.py"

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  Cube Competition Video Processor" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  Video dir : $VideoDir"
Write-Host "  Mode      : $(if ($DryRun) { 'Dry Run (preview only)' } else { 'Live' })"
Write-Host ""

# ---------- Step 1: OCR Rename ----------
if (-not $SkipOCR) {
    Write-Host "Step 1/2: OCR Timer Recognition" -ForegroundColor Yellow
    Write-Host "----------------------------------------"

    $ocrArgs = @($OcrScript, $VideoDir, "--rename")
    if ($DryRun) { $ocrArgs += "--dry-run" }

    python @ocrArgs

    if ($LASTEXITCODE -ne 0) {
        Write-Host "OCR step failed!" -ForegroundColor Red
        exit 1
    }
    Write-Host ""
}
else {
    Write-Host "Step 1/2: OCR skipped (--SkipOCR)" -ForegroundColor DarkGray
    Write-Host ""
}

# ---------- Step 2: Face Classify ----------
if (-not $SkipClassify) {
    Write-Host "Step 2/2: Face Classification" -ForegroundColor Yellow
    Write-Host "----------------------------------------"

    $classifyArgs = @($ClassifyScript, $VideoDir)
    if ($DryRun) { $classifyArgs += "--dry-run" }
    if ($Copy)   { $classifyArgs += "--copy" }

    python @classifyArgs

    if ($LASTEXITCODE -ne 0) {
        Write-Host "Classify step failed!" -ForegroundColor Red
        exit 1
    }
    Write-Host ""
}
else {
    Write-Host "Step 2/2: Classify skipped (--SkipClassify)" -ForegroundColor DarkGray
    Write-Host ""
}

Write-Host "========================================" -ForegroundColor Green
Write-Host "  All done!" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Green
