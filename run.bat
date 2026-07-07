@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo لم يتم العثور على البيئة الافتراضية. الرجاء تشغيل: python -m venv .venv
    pause
    exit /b 1
)
echo جاري تشغيل نظام الكاشير...
".venv\Scripts\python.exe" app.py
pause
