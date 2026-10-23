@echo off
chcp 65001 >nul
cd /d "%~dp0"
python "%~dp0type_text.py" -a %*
echo.
pause
