# F1 遥测仪表盘 —— 启动脚本
#
# 默认会尝试以管理员身份启动（CPU 核心温度、主板风扇、硬盘温度都需要读 MSR / SMBus），
# 如果你在 UAC 弹窗里点了“否”，脚本会自动改用普通权限启动，
# 此时仪表盘依然可用，只是 CPU 温度会显示为“待解锁”。

param(
    [switch]$NoElevate,   # 强制普通权限
    [switch]$Elevated     # 内部使用：表示已经处于提权后的分支
)

$ErrorActionPreference = 'Stop'
$BaseDir = Split-Path -Parent $PSScriptRoot


function Find-PythonW {
    $candidates = @()

    $cmd = Get-Command pythonw.exe -ErrorAction SilentlyContinue
    if ($cmd) { $candidates += $cmd.Source }

    $roots = @(
        (Join-Path $env:LOCALAPPDATA 'Programs\Python'),
        'C:\Python312', 'C:\Python311', 'C:\Python310',
        'C:\Program Files\Python312', 'C:\Program Files\Python311'
    )
    foreach ($r in $roots) {
        if (Test-Path $r) {
            $candidates += (Get-ChildItem -Path $r -Filter 'pythonw.exe' -Recurse -Depth 2 -ErrorAction SilentlyContinue |
                            Select-Object -ExpandProperty FullName)
        }
    }
    foreach ($c in $candidates) {
        if ($c -and (Test-Path $c)) { return $c }
    }
    return $null
}


$pythonw = Find-PythonW
if (-not $pythonw) {
    Write-Host "找不到 pythonw.exe。请先安装 Python 3.10+，或把 pythonw.exe 所在目录加入 PATH。" -ForegroundColor Red
    Read-Host "按回车退出"
    exit 1
}

$isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)

if (-not $isAdmin -and -not $NoElevate) {
    try {
        $argList = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-WindowStyle', 'Hidden',
                     '-File', "`"$PSCommandPath`"", '-Elevated')
        Start-Process -FilePath (Get-Command powershell.exe).Source -ArgumentList $argList -Verb RunAs
        exit 0
    } catch {
        Write-Host "未获得管理员权限，改用普通权限启动（CPU 温度将不可用）。" -ForegroundColor Yellow
    }
}

# 先关掉上一个实例，避免出现两个仪表盘
$pidFile = Join-Path $BaseDir 'logs\f1dash.pid'
if (Test-Path $pidFile) {
    $old = Get-Content $pidFile -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($old -match '^\d+$') {
        Get-Process -Id ([int]$old) -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    }
}
Get-Process -Name 'SensorBridge' -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue

# 关键：用 -Wait 让本进程一直作为父进程陪跑。
# 提权后的进程树在父进程退出时有可能被系统连带清理，导致仪表盘“闪退”；
# 保持父进程存活就彻底避免了这个问题。仪表盘关闭后本进程会自动退出。
$proc = Start-Process -FilePath $pythonw -ArgumentList "`"$BaseDir\run.py`"" `
                      -WorkingDirectory $BaseDir -PassThru -Wait
exit $proc.ExitCode
