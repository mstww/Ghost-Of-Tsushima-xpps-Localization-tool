@echo off
title Build xpps_tool.exe
cd /d "%~dp0"
echo ===================================================
echo Building Ghost of Tsushima Localization Tool EXE
echo ===================================================

pip install pyinstaller
pyinstaller --onefile --clean --noconfirm --name "xpps_tool" xpps_tool.py

echo.
if exist "dist\xpps_tool.exe" (
    copy /Y "dist\xpps_tool.exe" "xpps_tool.exe"
    echo [SUCCESS] xpps_tool.exe created successfully!
) else (
    echo [ERROR] Build failed! Check the log above.
)

pause
