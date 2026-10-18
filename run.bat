@echo off
chcp 65001 >nul
cd /d "%~dp0"
call "%~dp0install.bat" || exit /b 1
:loop
.venv\Scripts\python -m bot
echo Бот остановился. Перезапуск через 15 секунд (Ctrl+C — выход)...
timeout /t 15
goto loop
