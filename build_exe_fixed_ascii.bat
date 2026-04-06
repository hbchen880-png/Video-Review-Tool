@echo off
setlocal
cd /d "%~dp0"

set "PY_CMD=python"
where py >nul 2>nul
if not errorlevel 1 set "PY_CMD=py"

echo [1/3] Upgrade pip
%PY_CMD% -m pip install --upgrade pip
if errorlevel 1 goto :fail

echo [2/3] Install dependencies
%PY_CMD% -m pip install -r requirements.txt
if errorlevel 1 goto :fail

echo [3/3] Build exe
if exist "app_logo.ico" (
    %PY_CMD% -m PyInstaller --noconfirm --clean --windowed --onefile --icon "app_logo.ico" --name "video_review_tool" "video_review_app.py"
) else (
    %PY_CMD% -m PyInstaller --noconfirm --clean --windowed --onefile --name "video_review_tool" "video_review_app.py"
)
if errorlevel 1 goto :fail

echo.
echo Build completed.
echo Output: dist\video_review_tool.exe
pause
exit /b 0

:fail
echo.
echo Build failed.
pause
exit /b 1
