<#
.SYNOPSIS
    一键处理魔方比赛视频: OCR识别成绩 → 重命名 → 按人脸分类

.DESCRIPTION
    交互式流程 (推荐): 直接运行脚本, 按提示输入目录、选择步骤、模式
    非交互模式 (老用法兼容): 命令行直接传 switch, 跳过对应提问

    Step 1: 用 ocr_timer.py 提取帧, 识别成绩后重命名 (如 IMG_1234.MP4 → 4.716.MP4)
    Step 2: 用 classify.py 按人脸识别将视频分到选手文件夹

.PARAMETER VideoDir
    包含视频文件的源目录 (可选, 不提供时交互输入)

.PARAMETER DryRun
    仅预览, 不实际操作 (传了则跳过对应提问)

.PARAMETER Copy
    分类时复制而非移动视频 (传了则跳过对应提问)

.PARAMETER SkipOCR
    跳过 OCR 重命名步骤

.PARAMETER SkipClassify
    跳过人脸分类步骤

.EXAMPLE
    .\process-videos.ps1                                       # 完全交互模式
    .\process-videos.ps1 "Z:\cube-comp\260307"                 # 给目录, 其余选项交互
    .\process-videos.ps1 "Z:\cube-comp\260307" -DryRun         # 老用法, 全部用 flag
    .\process-videos.ps1 "Z:\cube-comp\260307" -SkipOCR
#>

param(
    [Parameter(Mandatory = $false, Position = 0)]
    [string]$VideoDir,

    [switch]$DryRun,
    [switch]$Copy,
    [switch]$SkipOCR,
    [switch]$SkipClassify
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

# ---------- 退出辅助 ----------

function Wait-Exit
{
    # 防止双击/快捷方式启动时窗口立即关闭, 看不到结果
    param([int]$Code = 0)
    if ([Environment]::UserInteractive -and -not [Console]::IsInputRedirected)
    {
        Write-Host ""
        Write-Host "按任意键退出..." -ForegroundColor DarkGray
        [Console]::ReadKey($true) | Out-Null
    }
    exit $Code
}

# ---------- 交互辅助函数 ----------

function Read-Choice
{
    param(
        [string]$Prompt,
        [string[]]$Options,
        [object[]]$Values,
        [int]$Default = 0
    )
    $ESC = [char]27
    Write-Host ""
    Write-Host $Prompt -ForegroundColor Cyan

    $selected = $Default
    $cursorWasVisible = [Console]::CursorVisible

    function _drawLine([int]$idx, [int]$sel, [string[]]$opts, [char]$esc)
    {
        $isSel = ($idx -eq $sel)
        $line = if ($isSel) { "  ▶ " + $opts[$idx] } else { "    " + $opts[$idx] }
        $clr  = if ($isSel) { 'Cyan' } else { 'DarkGray' }
        Write-Host ($line + "$esc[K") -ForegroundColor $clr
    }

    try
    {
        [Console]::CursorVisible = $false

        # 初次绘制 (此时若在终端底部会触发滚动, 之后再算 startTop 才稳)
        for ($i = 0; $i -lt $Options.Count; $i++)
        {
            _drawLine $i $selected $Options $ESC
        }
        $startTop = [Console]::CursorTop - $Options.Count

        while ($true)
        {
            $key = [Console]::ReadKey($true)
            $changed = $false
            switch ($key.Key)
            {
                'UpArrow'   { $selected = ($selected - 1 + $Options.Count) % $Options.Count; $changed = $true }
                'DownArrow' { $selected = ($selected + 1) % $Options.Count; $changed = $true }
                'Enter'     { return $Values[$selected] }
                'Escape'    {
                    [Console]::SetCursorPosition(0, $startTop + $Options.Count)
                    Write-Host "`n已取消" -ForegroundColor DarkGray
                    Wait-Exit 0
                }
            }
            if ($changed)
            {
                [Console]::SetCursorPosition(0, $startTop)
                for ($i = 0; $i -lt $Options.Count; $i++)
                {
                    _drawLine $i $selected $Options $ESC
                }
            }
        }
    }
    finally
    {
        [Console]::CursorVisible = $cursorWasVisible
    }
}

function Read-YesNo
{
    param(
        [string]$Prompt,
        [bool]$Default = $false
    )
    $defIdx = if ($Default) { 0 } else { 1 }
    return Read-Choice -Prompt $Prompt `
        -Options @("是", "否") `
        -Values  @($true, $false) `
        -Default $defIdx
}

# ---------- 收集参数 ----------

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  Cube Competition Video Processor" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan

# 1. 视频目录
while (-not $VideoDir -or -not (Test-Path -LiteralPath $VideoDir -PathType Container))
{
    if ($VideoDir)
    {
        Write-Host "  ❌ 目录不存在: $VideoDir" -ForegroundColor Red
    }
    $VideoDir = (Read-Host "`n请输入视频目录路径").Trim('"').Trim("'").Trim()
    if (-not $VideoDir)
    {
        Write-Host "  ⚠ 路径不能为空" -ForegroundColor Yellow
    }
}
$VideoDir = (Resolve-Path -LiteralPath $VideoDir).Path

# 视频数量 (帮用户判断要不要继续)
$videoExt = '\.(mp4|mov|avi|mkv|wmv|flv)$'
$videoCount = @(Get-ChildItem -LiteralPath $VideoDir -File |
        Where-Object { $_.Extension -match $videoExt }).Count

Write-Host ""
Write-Host "  📁 目录    : $VideoDir"
Write-Host "  🎬 视频文件: $videoCount 个"

if ($videoCount -eq 0)
{
    Write-Host ""
    Write-Host "  ⚠ 该目录下没有支持的视频文件 (mp4/mov/avi/mkv/wmv/flv)" -ForegroundColor Yellow
    Wait-Exit 1
}

# 2. 选步骤 (用户没指定任何 SkipXxx 时才问)
if (-not $PSBoundParameters.ContainsKey('SkipOCR') -and -not $PSBoundParameters.ContainsKey('SkipClassify'))
{
    $stepChoice = Read-Choice -Prompt "请选择要执行的步骤:" `
        -Options @("OCR 重命名  +  人脸分类", "仅 OCR 重命名", "仅人脸分类") `
        -Values  @("both", "ocr", "classify") `
        -Default 0
    switch ($stepChoice)
    {
        "ocr"      { $SkipClassify = $true }
        "classify" { $SkipOCR = $true }
    }
}

# 3. DryRun
if (-not $PSBoundParameters.ContainsKey('DryRun'))
{
    $DryRun = Read-YesNo -Prompt "`n是否仅预览不实际操作 (DryRun)?" -Default $false
}

# ---------- 摘要 ----------

$steps = @()
if (-not $SkipOCR)      { $steps += "OCR 重命名" }
if (-not $SkipClassify) { $steps += "人脸分类 (移动)" }

if ($steps.Count -eq 0)
{
    Write-Host ""
    Write-Host "  ⚠ 没有要执行的步骤 (OCR 和 分类 都被跳过), 退出" -ForegroundColor Yellow
    Wait-Exit 1
}

Write-Host ""
Write-Host "----------------------------------------" -ForegroundColor DarkCyan
Write-Host "  执行摘要" -ForegroundColor Cyan
Write-Host "----------------------------------------" -ForegroundColor DarkCyan
Write-Host "  目录    : $VideoDir"
Write-Host "  视频数  : $videoCount"
Write-Host "  步骤    : $($steps -join '   →   ')"
Write-Host "  模式    : $(if ($DryRun) { 'Dry Run (仅预览, 不动文件)' } else { '正式执行' })"

# ---------- 执行 ----------

$OcrScript = Join-Path $ScriptDir "ocr_timer.py"
$ClassifyScript = Join-Path $ScriptDir "classify.py"

# Step 1: OCR Rename
if (-not $SkipOCR)
{
    Write-Host ""
    Write-Host "Step 1/2: OCR Timer Recognition" -ForegroundColor Yellow
    Write-Host "----------------------------------------"

    $ocrArgs = @("run", "python", $OcrScript, $VideoDir, "--rename")
    if ($DryRun) { $ocrArgs += "--dry-run" }

    uv @ocrArgs

    if ($LASTEXITCODE -ne 0)
    {
        Write-Host "OCR step failed!" -ForegroundColor Red
        Wait-Exit 1
    }
    Write-Host ""
}
else
{
    Write-Host ""
    Write-Host "Step 1/2: OCR skipped" -ForegroundColor DarkGray
}

# Step 2: Face Classify
if (-not $SkipClassify)
{
    Write-Host ""
    Write-Host "Step 2/2: Face Classification" -ForegroundColor Yellow
    Write-Host "----------------------------------------"

    $classifyArgs = @("run", "python", $ClassifyScript, $VideoDir)
    if ($DryRun) { $classifyArgs += "--dry-run" }
    if ($Copy)   { $classifyArgs += "--copy" }

    uv @classifyArgs

    if ($LASTEXITCODE -ne 0)
    {
        Write-Host "Classify step failed!" -ForegroundColor Red
        Wait-Exit 1
    }
    Write-Host ""
}
else
{
    Write-Host ""
    Write-Host "Step 2/2: Classify skipped" -ForegroundColor DarkGray
}

Write-Host "========================================" -ForegroundColor Green
Write-Host "  All done!" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Green
Wait-Exit 0
