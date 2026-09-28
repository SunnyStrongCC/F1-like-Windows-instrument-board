# F1 遥测仪表盘 —— 停止脚本
$BaseDir = Split-Path -Parent $PSScriptRoot
$pidFile = Join-Path $BaseDir 'logs\f1dash.pid'
$killed = 0

if (Test-Path $pidFile) {
    $val = Get-Content $pidFile -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($val -match '^\d+$') {
        $p = Get-Process -Id ([int]$val) -ErrorAction SilentlyContinue
        if ($p) { Stop-Process -Id $p.Id -Force; $killed++; Write-Host "已结束仪表盘进程 PID=$($p.Id)" }
    }
}

# 连带清掉传感器桥接器，避免留下孤儿进程
Get-Process -Name 'SensorBridge' -ErrorAction SilentlyContinue | ForEach-Object {
    Stop-Process -Id $_.Id -Force; $killed++
}

if ($killed -eq 0) { Write-Host "仪表盘没有在运行。" } else { Write-Host "已停止 $killed 个进程。" }
Start-Sleep -Milliseconds 700
