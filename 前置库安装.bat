@echo off
chcp 65001 >nul 2>&1
setlocal EnableDelayedExpansion
title AI 审计系统 · 一键启动
cd /d "%~dp0"

echo.
echo ╔══════════════════════════════════════════════════╗
echo ║   AI 审计系统 · 一键启动向导                    ║
echo ║   全自动检测环境 / 安装依赖 / 启动服务           ║
echo ╚══════════════════════════════════════════════════╝
echo.

REM ═══════════════════════════════════════════════════
REM Step 0: 检查 VC++ 运行库
REM ═══════════════════════════════════════════════════
echo [0/5] 正在检查 VC++ 运行库...

set "VC_DLL=%SystemRoot%\System32\msvcp140.dll"
if not exist "%VC_DLL%" (
    echo   [!] 未检测到 VC++ 运行库，正在自动安装...
    if exist "%~dp0VC_redist.x64.exe" (
        "%~dp0VC_redist.x64.exe" /install /quiet /norestart
        if !errorlevel! neq 0 (
            echo   [!] VC++ 运行库安装可能需要管理员权限
            echo       请右键以管理员身份运行本脚本，或手动安装 VC_redist.x64.exe
            echo.
        ) else (
            echo   [OK] VC++ 运行库已安装
        )
    ) else (
        echo   [!] 未找到 VC_redist.x64.exe 安装包
        echo       请手动下载并安装 Microsoft Visual C++ Redistributable
        echo       下载地址: https://aka.ms/vs/17/release/vc_redist.x64.exe
        echo.
    )
) else (
    echo   [OK] VC++ 运行库已就绪
)
echo.

REM ═══════════════════════════════════════════════════
REM Step 1: 检查 Python
REM ═══════════════════════════════════════════════════
echo [1/5] 正在检查 Python 环境...

set "PYTHON_CMD="
for %%C in (python python3 py) do (
    %%C --version >nul 2>&1
    if !errorlevel! == 0 (
        for /f "tokens=2 delims= " %%V in ('%%C --version 2^>^&1') do (
            for /f "tokens=1,2 delims=." %%A in ("%%V") do (
                if %%B GEQ 10 (
                    set "PYTHON_CMD=%%C"
                )
            )
        )
    )
)

if "%PYTHON_CMD%"=="" (
    echo.
    echo   [!] 未检测到 Python 3.10+
    echo.
    echo   请先安装 Python 3.10 或更高版本:
    echo   1. 打开浏览器访问: https://www.python.org/downloads/
    echo   2. 下载 Python 3.10+ 安装包
    echo   3. 安装时务必勾选 "Add python.exe to PATH"
    echo   4. 安装完成后，重新双击本脚本
    echo.
    echo   或者，如果你的安装包文件夹中有 python-3.12.10-amd64.exe，
    echo   请先双击安装它（记得勾选 Add to PATH），然后重新运行本脚本。
    echo.
    pause
    exit /b 1
)

for /f "tokens=*" %%V in ('%PYTHON_CMD% --version 2^>^&1') do echo   [OK] 已找到 %%V
echo.

REM ═══════════════════════════════════════════════════
REM Step 2: 检查/安装 uv 包管理器
REM ═══════════════════════════════════════════════════
echo [2/5] 正在检查 uv 包管理器...

uv --version >nul 2>&1
if %errorlevel% neq 0 (
    echo   [!] uv 未安装，正在自动安装...
    echo.
    %PYTHON_CMD% -m pip install uv -i https://pypi.tuna.tsinghua.edu.cn/simple --quiet
    if !errorlevel! neq 0 (
        echo   [!] pip 安装 uv 失败，尝试备用方案...
        %PYTHON_CMD% -m pip install uv --quiet
    )

    REM 刷新环境变量 - 检查常见安装路径
    set "PATH=%USERPROFILE%\.local\bin;%USERPROFILE%\.cargo\bin;%PATH%"

    uv --version >nul 2>&1
    if !errorlevel! neq 0 (
        echo.
        echo   [!] uv 安装后仍无法找到。
        echo   请关闭本窗口，重新双击本脚本再试。
        echo.
        pause
        exit /b 1
    )
)

for /f "tokens=*" %%V in ('uv --version 2^>^&1') do echo   [OK] uv: %%V
echo.

REM ═══════════════════════════════════════════════════
REM Step 3: 检查 .env 配置
REM ═══════════════════════════════════════════════════
echo [3/5] 正在检查环境配置...

set "NEED_CONFIG=0"

if not exist ".env" (
    if exist ".env.example" (
        copy ".env.example" ".env" >nul 2>&1
        echo   [OK] 已从模板创建 .env 配置文件
    ) else (
        echo   [!] 缺少 .env.example 模板，正在创建最小配置...
        (
            echo # LLM 配置
            echo OPENAI_API_KEY=sk-your-api-key-here
            echo OPENAI_BASE_URL=https://api.deepseek.com
            echo # 项目路径
            echo COZE_WORKSPACE_PATH=
            echo # 日志
            echo LOG_LEVEL=INFO
            echo ENV=dev
        ) > ".env"
        echo   [OK] 已创建 .env 配置文件
    )
    set "NEED_CONFIG=1"
) else (
    REM 检查 API Key 是否为模板值（sk-your 前缀）
    findstr /B /C:"OPENAI_API_KEY=sk-your" ".env" >nul 2>&1
    if !errorlevel! == 0 (
        echo   [!] 检测到 API Key 仍为模板值
        set "NEED_CONFIG=1"
    ) else (
        echo   [OK] .env 配置文件已存在
    )
)

REM 自动修正 COZE_WORKSPACE_PATH 为当前项目目录
findstr /B "COZE_WORKSPACE_PATH=" ".env" >nul 2>&1
if !errorlevel! == 0 (
    set "PROJECT_DIR=%~dp0"
    set "PROJECT_DIR=!PROJECT_DIR:\=/!"
    set "PROJECT_DIR=!PROJECT_DIR:~0,-1!"
    powershell -NoProfile -Command "$content = Get-Content '.env' -Raw -Encoding UTF8; $dir = '!PROJECT_DIR!'; $content = $content -replace '(?m)^COZE_WORKSPACE_PATH=.*', \"COZE_WORKSPACE_PATH=$dir\"; [System.IO.File]::WriteAllText((Resolve-Path '.env').Path, $content, [System.Text.Encoding]::UTF8)"
)

if "!NEED_CONFIG!"=="1" (
    echo.
    echo   ============================================
    echo   !! 需要配置 API Key !!
    echo   ============================================
    echo.
    echo   即将打开配置文件，请按以下步骤操作：
    echo.
    echo   1. 找到 OPENAI_API_KEY= 这一行
    echo   2. 把等号右边替换成你自己的 DeepSeek API Key
    echo      (以 sk- 开头，在 https://platform.deepseek.com/ 获取)
    echo   3. 按 Ctrl+S 保存，然后关闭记事本
    echo.
    echo   提示：如果你还没有 API Key，可以先关掉本窗口，
    echo   获取 Key 后再重新双击运行。
    echo.
    pause
    notepad ".env"
)
echo.

REM ═══════════════════════════════════════════════════
REM Step 4: 同步依赖
REM ═══════════════════════════════════════════════════
echo [4/5] 正在同步项目依赖（首次运行需下载，请耐心等待）...

uv sync
if %errorlevel% neq 0 (
    echo.
    echo   [!] 依赖同步失败，正在重试...
    uv sync
    if !errorlevel! neq 0 (
        echo.
        echo   [!] 依赖安装失败，请检查网络连接。
        echo   如果使用校园网/公司网，可能需要切换网络或配置代理。
        echo.
        pause
        exit /b 1
    )
)
echo   [OK] 依赖已就绪
echo.

REM ═══════════════════════════════════════════════════
REM Step 5: 启动服务
REM ═══════════════════════════════════════════════════
echo [5/5] 正在启动 AI 审计系统...
echo.
echo   启动后浏览器将自动打开，访问地址: http://localhost:5000
echo   如需停止服务，按 Ctrl+C
echo.
echo ══════════════════════════════════════════════════════════
echo.

REM 延迟 4 秒后自动打开浏览器
start "" cmd /c "timeout /t 4 /nobreak >nul && start http://localhost:5000"

REM 自动修复 start.ps1 编码：清除多余BOM，确保UTF-8+单BOM（PowerShell 5.1必需）
powershell -NoProfile -Command "$f='.\start.ps1';if(Test-Path $f){$c=[IO.File]::ReadAllText($f,[Text.Encoding]::UTF8)-replace'^\uFEFF+','';[IO.File]::WriteAllText($f,$c,[Text.UTF8Encoding]::new($true))}"

REM 调用 start.ps1 启动服务
powershell -ExecutionPolicy Bypass -File ".\start.ps1" -Mode web -SkipSync

echo.
echo   服务已停止。
pause
