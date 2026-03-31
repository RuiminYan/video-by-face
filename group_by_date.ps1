# group_by_date.ps1
# 按修改日期对视频分组（相邻间隔 < N 分钟为同组），每组移入 R{n} avg 文件夹并加数字前缀
# 用法: .\group_by_date.ps1 [-GapMinutes 10] [-Ext "MP4,MOV"] [-DryRun]

param(
    [int]$GapMinutes = 10,
    [string]$Ext = "MP4,MOV,AVI,MKV",
    [switch]$DryRun
)

if ($DryRun) { Write-Host "🔍 演习模式 — 不会实际操作" -ForegroundColor Yellow }

# ═══════════════════════════════════════════════════════════
# 主循环：每次处理完一个目录后继续询问
# ═══════════════════════════════════════════════════════════
while ($true) {

    Write-Host ""
    $Dir = Read-Host "请输入目录路径（直接回车退出）"
    $Dir = $Dir.Trim('"').Trim("'").Trim()

    if ($Dir -eq "") {
        Write-Host "👋 再见！" -ForegroundColor DarkGray
        break
    }

    if (-not (Test-Path $Dir)) {
        Write-Host "❌ 目录不存在: $Dir" -ForegroundColor Red
        continue
    }

    # ── 收集视频文件（仅当前层，不递归） ──────────────────────
    $extensions = $Ext.Split(',') | ForEach-Object { "*.$($_.Trim())" }
    $files = @()
    foreach ($pattern in $extensions) {
        $files += Get-ChildItem -Path $Dir -Filter $pattern -File
    }
    $files = $files | Sort-Object LastWriteTime

    if ($files.Count -eq 0) {
        Write-Host "⚠️  没有找到视频文件 ($Ext)" -ForegroundColor Yellow
        continue
    }

    Write-Host ""
    Write-Host "📁 目录: $Dir" -ForegroundColor Cyan
    Write-Host "🎬 找到 $($files.Count) 个视频文件，间隔阈值: ${GapMinutes} 分钟" -ForegroundColor Cyan
    Write-Host ""

    # ── 分组 ──────────────────────────────────────────────────
    $groups = @()
    $current = @($files[0])

    for ($i = 1; $i -lt $files.Count; $i++) {
        $gap = ($files[$i].LastWriteTime - $files[$i-1].LastWriteTime).TotalMinutes
        if ($gap -lt $GapMinutes) {
            $current += $files[$i]
        } else {
            $groups += , $current
            $current = @($files[$i])
        }
    }
    $groups += , $current

    Write-Host "📦 共分为 $($groups.Count) 组：" -ForegroundColor Cyan
    foreach ($g in $groups) {
        $t0 = $g[0].LastWriteTime.ToString("MM-dd HH:mm")
        $t1 = $g[-1].LastWriteTime.ToString("MM-dd HH:mm")
        Write-Host "   $($g.Count) 个文件  [$t0 → $t1]" -ForegroundColor DarkCyan
    }
    Write-Host ""

    # ── 处理每一组 ────────────────────────────────────────────
    $roundNum    = 1
    $totalMoved  = 0
    $totalSkipped = 0

    foreach ($group in $groups) {

        if ($group.Count -ge 2) {
            # 多文件组：移入 "R{n} avg" 文件夹
            $folderName = "R$roundNum avg"
            $folderPath = Join-Path $Dir $folderName

            Write-Host "📂 [$folderName]  ($($group.Count) 个文件)" -ForegroundColor Magenta

            if (-not $DryRun -and -not (Test-Path $folderPath)) {
                New-Item -ItemType Directory -Path $folderPath | Out-Null
            }

            $idx = 1
            foreach ($file in $group) {
                $newName = "$idx $($file.Name)"
                $dest    = Join-Path $folderPath $newName
                $t       = $file.LastWriteTime.ToString("HH:mm:ss")

                if (Test-Path $dest) {
                    Write-Host "  [$idx] ⚠️  目标已存在，跳过: $newName" -ForegroundColor Yellow
                    $totalSkipped++
                } else {
                    Write-Host "  [$idx] $($file.Name)  →  $folderName\$newName  ($t)" -ForegroundColor Green
                    if (-not $DryRun) {
                        Move-Item -LiteralPath $file.FullName -Destination $dest
                    }
                    $totalMoved++
                }
                $idx++
            }

            $roundNum++

        } else {
            # 单文件：原地保留
            $file = $group[0]
            $t    = $file.LastWriteTime.ToString("MM-dd HH:mm:ss")
            Write-Host "📄 单文件，原地保留: $($file.Name)  ($t)" -ForegroundColor DarkYellow
            $totalSkipped++
        }

        Write-Host ""
    }

    # ── 汇总 ──────────────────────────────────────────────────
    if ($DryRun) {
        Write-Host "✅ 演习完成：将移动 $totalMoved 个文件，保留/跳过 $totalSkipped 个" -ForegroundColor Cyan
    } else {
        Write-Host "✅ 完成：移动 $totalMoved 个文件，保留/跳过 $totalSkipped 个" -ForegroundColor Cyan
    }
}
