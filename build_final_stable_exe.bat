@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul
cd /d "%~dp0"

echo ========================================
echo 视频审核工具 - 最终稳定版 EXE 打包
echo ========================================
echo.

set "PY_CMD="
where py >nul 2>nul
if %errorlevel%==0 (
    set "PY_CMD=py -3"
) else (
    where python >nul 2>nul
    if %errorlevel%==0 (
        set "PY_CMD=python"
    )
)

if "%PY_CMD%"=="" (
    echo [错误] 未找到 Python。
    echo 请先安装 Python，再重新运行本脚本。
    pause
    exit /b 1
)

echo [1/6] 检查 Python...
%PY_CMD% -c "import sys; print(sys.version)"
if errorlevel 1 goto :fail

echo.
echo [2/6] 创建虚拟环境...
if not exist ".venv_build\Scripts\python.exe" (
    %PY_CMD% -m venv .venv_build
    if errorlevel 1 goto :fail
)

call ".venv_build\Scripts\activate.bat"
if errorlevel 1 goto :fail

echo.
echo [3/6] 升级 pip...
python -m pip install --upgrade pip setuptools wheel
if errorlevel 1 goto :fail

echo.
echo [4/6] 安装依赖...
pip install -r requirements_build.txt
if errorlevel 1 goto :fail

echo.
echo [5/6] 清理旧构建...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
if exist "视频审核工具.spec" del /f /q "视频审核工具.spec"

echo.
echo [6/6] 开始打包...
pyinstaller --noconfirm --clean video_review_tool_final_stable.spec
if errorlevel 1 goto :fail

echo.
echo ========================================
echo 打包完成
if exist "dist\视频审核工具稳定版\视频审核工具.exe" (
    echo EXE: %cd%\dist\视频审核工具稳定版\视频审核工具.exe
    start "" "dist\视频审核工具稳定版"
) else (
    echo 已完成，但未找到预期 exe，请检查 dist 目录。
)
echo ========================================
pause
exit /b 0

:fail
echo.
echo [失败] 打包过程中出现错误，请把本窗口截图发我。
pause
exit /b 1
