# group_by_date.ps1
# 按修改日期对视频分组（相邻间隔 < N 分钟为同组），每组移入 R{n} avg 文件夹并加数字前缀
# 自动递归子文件夹（已有 R1 avg / Fi avg 等命名的不再下钻）
# 用法: .\group_by_date.ps1 [-GapMinutes 10] [-Ext "MP4,MOV"] [-DryRun] [-NoRecurse]

param(
    [int]$GapMinutes = 10,
    [string]$Ext = "MP4,MOV,AVI,MKV",
    [switch]$DryRun,
    [switch]$NoRecurse
)

if ($DryRun) { Write-Host "🔍 演习模式 — 不会实际操作" -ForegroundColor Yellow }

# 跳过下钻的已分组文件夹名: "R1 avg", "Fi avg", "Fi 4.78 avg", "3x3 R1 4.78 avg" 等
$script:SkipPattern = '^(R\d+|Fi)\b|\bavg\s*$'
$script:Ext = $Ext
$script:GapMinutes = $GapMinutes
$script:DryRun = $DryRun
$script:TotalMoved = 0
$script:TotalSkipped = 0

function Process-Dir {
    param([string]$Dir)

    # ── 收集视频文件（仅当前层） ──────────────────────────────
    $extensions = $script:Ext.Split(',') | ForEach-Object { "*.$($_.Trim())" }
    $files = @()
    foreach ($pattern in $extensions) {
        $files += Get-ChildItem -LiteralPath $Dir -Filter $pattern -File -ErrorAction SilentlyContinue
    }
    $files = $files | Sort-Object LastWriteTime

    if ($files.Count -gt 0) {
        Write-Host ""
        Write-Host "📁 目录: $Dir" -ForegroundColor Cyan
        Write-Host "🎬 $($files.Count) 个视频，间隔阈值: $($script:GapMinutes) 分钟" -ForegroundColor Cyan

        # ── 分组 ──────────────────────────────────────────────
        $groups = @()
        $current = @($files[0])
        for ($i = 1; $i -lt $files.Count; $i++) {
            $gap = ($files[$i].LastWriteTime - $files[$i-1].LastWriteTime).TotalMinutes
            if ($gap -lt $script:GapMinutes) {
                $current += $files[$i]
            } else {
                $groups += , $current
                $current = @($files[$i])
            }
        }
        $groups += , $current

        Write-Host "📦 分为 $($groups.Count) 组：" -ForegroundColor Cyan
        foreach ($g in $groups) {
            $t0 = $g[0].LastWriteTime.ToString("MM-dd HH:mm")
            $t1 = $g[-1].LastWriteTime.ToString("MM-dd HH:mm")
            Write-Host "   $($g.Count) 个文件  [$t0 → $t1]" -ForegroundColor DarkCyan
        }

        # ── 处理每一组 ────────────────────────────────────────
        $roundNum = 1
        foreach ($group in $groups) {
            if ($group.Count -ge 2) {
                $folderName = "R$roundNum avg"
                $folderPath = Join-Path $Dir $folderName
                Write-Host "📂 [$folderName]  ($($group.Count) 个文件)" -ForegroundColor Magenta

                if (-not $script:DryRun -and -not (Test-Path $folderPath)) {
                    New-Item -ItemType Directory -Path $folderPath | Out-Null
                }

                $idx = 1
                foreach ($file in $group) {
                    $newName = "$idx $($file.Name)"
                    $dest    = Join-Path $folderPath $newName
                    $t       = $file.LastWriteTime.ToString("HH:mm:ss")

                    if (Test-Path $dest) {
                        Write-Host "  [$idx] ⚠️  已存在，跳过: $newName" -ForegroundColor Yellow
                        $script:TotalSkipped++
                    } else {
                        Write-Host "  [$idx] $($file.Name)  →  $folderName\$newName  ($t)" -ForegroundColor Green
                        if (-not $script:DryRun) {
                            Move-Item -LiteralPath $file.FullName -Destination $dest
                        }
                        $script:TotalMoved++
                    }
                    $idx++
                }
                $roundNum++
            } else {
                $file = $group[0]
                $t = $file.LastWriteTime.ToString("MM-dd HH:mm:ss")
                Write-Host "📄 单文件原地保留: $($file.Name)  ($t)" -ForegroundColor DarkYellow
                $script:TotalSkipped++
            }
        }
    }

    # ── 递归子文件夹 ──────────────────────────────────────────
    if (-not $NoRecurse) {
        Get-ChildItem -LiteralPath $Dir -Directory -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -notmatch $script:SkipPattern } |
            ForEach-Object { Process-Dir -Dir $_.FullName }
    }
}

# ═══════════════════════════════════════════════════════════
# 主循环
# ═══════════════════════════════════════════════════════════
while ($true) {

    Write-Host ""
    $Dir = Read-Host "请输入目录路径（直接回车退出）"
    $Dir = $Dir.Trim('"').Trim("'").Trim()

    if ($Dir -eq "") {
        Write-Host "👋 再见！" -ForegroundColor DarkGray
        break
    }
    if (-not (Test-Path -LiteralPath $Dir -PathType Container)) {
        Write-Host "❌ 目录不存在: $Dir" -ForegroundColor Red
        continue
    }

    $script:TotalMoved = 0
    $script:TotalSkipped = 0
    Process-Dir -Dir (Resolve-Path -LiteralPath $Dir).Path

    Write-Host ""
    $verb = if ($script:DryRun) { "演习完成: 将移动" } else { "完成: 移动" }
    Write-Host "✅ $verb $($script:TotalMoved) 个文件，保留/跳过 $($script:TotalSkipped) 个" -ForegroundColor Cyan
}
