@echo off
REM Запуск бота без активации venv (обходит ошибку политики PowerShell)
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" main.py
) else (
    python main.py
)
pause
