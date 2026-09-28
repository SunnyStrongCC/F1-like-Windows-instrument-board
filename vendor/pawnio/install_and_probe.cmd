@echo off
rem 以管理员身份运行：静默安装 PawnIO 内核驱动，然后用提权态探测全部传感器
setlocal
set BASE=D:\yibiaopan
set BRIDGE=%BASE%\vendor\bridge
set LOG=%BASE%\logs\elevated_setup.log

echo [%DATE% %TIME%] elevated run start > "%LOG%"
echo. >> "%LOG%"

echo --- PawnIO install --- >> "%LOG%"
"%BASE%\vendor\pawnio\PawnIO_setup.exe" -install -silent >> "%LOG%" 2>&1
echo PawnIO exit=%ERRORLEVEL% >> "%LOG%"

echo --- sensor probe (elevated) --- >> "%LOG%"
"%BRIDGE%\SensorBridge.exe" --probe > "%BRIDGE%\probe_admin.txt" 2> "%BRIDGE%\probe_admin.err.txt"
echo Probe exit=%ERRORLEVEL% >> "%LOG%"

echo. >> "%LOG%"
echo [%DATE% %TIME%] elevated run done >> "%LOG%"
endlocal
