@echo off
setlocal
cd /d "%~dp0"

if not exist "venv\Scripts\python.exe" (
    echo لم يتم العثور على البيئة الافتراضية venv.
    echo شغل: python -m venv venv ثم pip install -r requirements.txt
    pause
    exit /b 1
)

echo يتم تشغيل التطبيق على http://127.0.0.1:5000
"venv\Scripts\waitress-serve.exe" --listen=127.0.0.1:5000 app:app

pause