@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo لم يتم العثور على البيئة الافتراضية. الرجاء تشغيل: python -m venv .venv
    pause
    exit /b 1
)

echo جاري إغلاق أي نسخة سابقة من الخادم...
powershell -NoProfile -Command "Get-CimInstance Win32_Process ^| Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -match 'app.py' } ^| ForEach-Object { Stop-Process -Id $_.ProcessId -Force }" >nul 2>&1

echo جاري تشغيل نظام الكاشير...
".venv\Scripts\python.exe" app.py
pause
