@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist .venv\installed.ok exit /b 0
python -c "import sys; sys.exit(sys.version_info < (3, 12))" >nul 2>nul
if errorlevel 1 (
    echo Нужен Python 3.12 или новее: https://www.python.org/downloads/ — при установке отметьте «Add python.exe to PATH».
    echo Python 3.12+ is required: https://www.python.org/downloads/ — tick "Add python.exe to PATH" when installing.
    pause
    exit /b 1
)
echo Первый запуск: ставлю зависимости и браузер Chromium, это займёт несколько минут...
echo First run: installing dependencies and Chromium, this takes a few minutes...
if not exist .venv\Scripts\python.exe python -m venv .venv || goto fail
.venv\Scripts\python -m pip install --disable-pip-version-check -r requirements.txt || goto fail
.venv\Scripts\python -m playwright install chromium || goto fail
echo ok> .venv\installed.ok
exit /b 0
:fail
echo Установка не удалась — проверьте интернет и запустите снова.
echo Installation failed — check the internet connection and run it again.
pause
exit /b 1
