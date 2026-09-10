<#
.SYNOPSIS
    zhinengti 项目一键启动脚本（Windows PowerShell）

.DESCRIPTION
    整合环境变量加载、依赖安装、知识库初始化、服务启动等全部流程。

.PARAMETER Mode
    运行模式，可选值：
      web    - 启动 Web 可视化界面 + HTTP 服务（默认）★
      http   - 仅启动 HTTP 服务（无自动打开浏览器）
      flow   - CLI 模式运行完整 Agent 流程
      node   - CLI 模式运行指定节点

.PARAMETER Port
    HTTP 服务端口，默认 5000

.PARAMETER Node
    节点 ID（仅 node 模式需要）

.PARAMETER UserInput
    输入数据，支持 JSON 字符串或纯文本

.PARAMETER SkipSync
    跳过 uv sync 依赖安装步骤

.PARAMETER SkipKB
    跳过知识库初始化检查

.PARAMETER NoBrowser
    不自动打开浏览器（仅 web 模式）

.EXAMPLE
    .\start.ps1
    # 启动 Web 可视化界面 + HTTP 服务（默认端口 5000）

.EXAMPLE
    .\start.ps1 -Mode http -Port 8080
    # 启动 HTTP 服务，端口 8080

.EXAMPLE
    .\start.ps1 -Mode flow
    # CLI 模式运行完整流程

.EXAMPLE
    .\start.ps1 -Mode flow -UserInput "请分析贵州茅台2024年年报"
    # CLI 模式，指定输入

.EXAMPLE
    .\start.ps1 -Mode flow -UserInput '{"messages":[{"role":"user","content":"你好"}]}'
    # CLI 模式，JSON 格式输入

.EXAMPLE
    .\start.ps1 -Mode node -Node node_1 -UserInput '{"text":"测试"}'
    # 运行指定节点

.EXAMPLE
    .\start.ps1 -SkipSync -SkipKB
    # 跳过依赖安装和知识库检查，快速启动
#>

param(
    [ValidateSet("web", "http", "flow", "node")]
    [string]$Mode = "web",

    [int]$Port = 5000,

    [string]$Node = "",

    [string]$UserInput = "",

    [switch]$SkipSync,

    [switch]$SkipKB,

    [switch]$NoBrowser
)

# ─── 全局配置 ──────────────────────────────────────────────
$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$ProjectDir = $ScriptDir                      # start.ps1 在 projects/ 根目录
$SrcDir = Join-Path $ProjectDir "src"
$EnvFile = Join-Path $ProjectDir ".env"
$VenvDir = Join-Path $ProjectDir ".venv"

# ─── 颜色输出辅助函数 ──────────────────────────────────────
function Write-Step   { param([string]$msg) Write-Host "`n▶ $msg" -ForegroundColor Cyan }
function Write-Ok     { param([string]$msg) Write-Host "  ✔ $msg" -ForegroundColor Green }
function Write-Warn   { param([string]$msg) Write-Host "  ⚠ $msg" -ForegroundColor Yellow }
function Write-Err    { param([string]$msg) Write-Host "  ✘ $msg" -ForegroundColor Red }
function Write-Info   { param([string]$msg) Write-Host "  ℹ $msg" -ForegroundColor Gray }

# ─── 错误中止函数 ──────────────────────────────────────────
function Stop-WithHint {
    param([string]$msg, [string]$hint)
    Write-Err $msg
    if ($hint) {
        Write-Host ""
        Write-Host "  💡 修复建议: $hint" -ForegroundColor Yellow
    }
    Write-Host ""
    exit 1
}

# ═══════════════════════════════════════════════════════════
# Step 0: 显示启动信息
# ═══════════════════════════════════════════════════════════
Write-Host ""
Write-Host "╔══════════════════════════════════════════════════╗" -ForegroundColor Magenta
Write-Host "║   上市公司年报风险识别 - 本地启动                ║" -ForegroundColor Magenta
Write-Host "╚══════════════════════════════════════════════════╝" -ForegroundColor Magenta
Write-Host ""
Write-Info "项目目录: $ProjectDir"
Write-Info "运行模式: $Mode"
if ($Mode -eq "http") { Write-Info "服务端口: $Port" }

# ═══════════════════════════════════════════════════════════
# Step 1: 检查 Python 环境
# ═══════════════════════════════════════════════════════════
Write-Step "Step 1/6 - 检查 Python 环境"

$pythonCmd = $null
foreach ($cmd in @("python", "python3", "py")) {
    try {
        $ver = & $cmd --version 2>&1
        if ($ver -match "Python\s+3\.(\d+)") {
            $minor = [int]$Matches[1]
            if ($minor -ge 10) {
                $pythonCmd = $cmd
                break
            }
        }
    } catch { }
}

if (-not $pythonCmd) {
    Stop-WithHint `
        "未找到 Python >= 3.10" `
        "请安装 Python 3.10+: https://www.python.org/downloads/ 或通过 uv 安装: uv python install 3.12"
}
Write-Ok "Python: $(& $pythonCmd --version)"

# ═══════════════════════════════════════════════════════════
# Step 2: 加载环境变量（.env）
# ═══════════════════════════════════════════════════════════
Write-Step "Step 2/6 - 加载环境变量"

if (Test-Path $EnvFile) {
    $envCount = 0
    Get-Content $EnvFile -Encoding UTF8 | ForEach-Object {
        $line = $_.Trim()
        # 跳过空行和注释
        if ($line -and -not $line.StartsWith("#")) {
            if ($line -match '^([A-Za-z_][A-Za-z0-9_]*)=(.*)$') {
                $key = $Matches[1]
                $val = $Matches[2]
                # 去除引号
                $val = $val.Trim('"').Trim("'")
                [System.Environment]::SetEnvironmentVariable($key, $val, "Process")
                $envCount++
            }
        }
    }
    Write-Ok "已加载 $envCount 个环境变量 (from .env)"

    # 检查关键变量
    $apiKey = [System.Environment]::GetEnvironmentVariable("OPENAI_API_KEY")
    if (-not $apiKey -or $apiKey -match '^sk-your') {
        Write-Warn "OPENAI_API_KEY 未配置或为模板值，LLM 调用将失败"
        Write-Info "请编辑 $EnvFile 填入真实的 API Key"
    }
} else {
    Write-Warn ".env 文件不存在: $EnvFile"
    Write-Info "将从 .env.example 创建模板..."

    $exampleEnv = Join-Path $ProjectDir ".env.example"
    if (Test-Path $exampleEnv) {
        Copy-Item $exampleEnv $EnvFile
        Write-Ok "已从 .env.example 复制为 .env，请编辑填入 API Key"
    } else {
        # 创建最小模板
        @"
# LLM 配置
OPENAI_API_KEY=sk-your-api-key-here
OPENAI_BASE_URL=https://api.deepseek.com
# 项目路径
COZE_WORKSPACE_PATH=$ProjectDir
# 日志
LOG_LEVEL=INFO
ENV=dev
"@ | Set-Content $EnvFile -Encoding UTF8
        Write-Ok "已创建 .env 模板，请编辑填入 API Key"
    }

    Stop-WithHint `
        "请先配置 .env 文件" `
        "编辑 $EnvFile，填入 OPENAI_API_KEY 后重新运行"
}

# 强制覆盖 COZE_WORKSPACE_PATH 为当前项目目录（避免从别处复制 .env 导致路径错误）
[System.Environment]::SetEnvironmentVariable("COZE_WORKSPACE_PATH", $ProjectDir, "Process")
Write-Info "COZE_WORKSPACE_PATH 已设为: $ProjectDir"

# ═══════════════════════════════════════════════════════════
# Step 3: 检查/安装依赖（uv sync --locked：严格按 uv.lock 安装，锁文件过期即报错）
# ═══════════════════════════════════════════════════════════
Write-Step "Step 3/6 - 检查依赖"

if ($SkipSync) {
    Write-Info "已跳过依赖安装 (-SkipSync)"
} else {
    $uvCmd = "uv"
    try {
        $uvVer = & $uvCmd --version 2>&1
        Write-Ok "uv: $uvVer"
    } catch {
        Stop-WithHint `
            "未找到 uv 包管理器" `
            "安装命令: irm https://astral.sh/uv/install.ps1 | iex"
    }

    # 检查 .venv 是否存在且有效
    $venvPython = Join-Path $VenvDir "Scripts\python.exe"
    if (-not (Test-Path $venvPython)) {
        Write-Info "虚拟环境不存在，正在创建..."
        Push-Location $ProjectDir
        try {
            & $uvCmd venv 2>&1 | Out-Null
            Write-Ok "虚拟环境已创建"
        } catch {
            Pop-Location
            Stop-WithHint "创建虚拟环境失败" "检查 Python 3.10+ 是否已安装"
        }
        Pop-Location
    }

        # 执行 uv sync --locked
    Write-Info "正在同步依赖 (uv sync --locked)..."
    Push-Location $ProjectDir

    # 临时允许 stderr 输出，防止 uv 的正常提示被当成致命异常
    $oldEAP = $ErrorActionPreference
    $ErrorActionPreference = "Continue"

    try {
        & $uvCmd sync --locked
        $syncExitCode = $LASTEXITCODE

        if ($syncExitCode -ne 0) {
            Write-Warn "uv sync --locked 失败，尝试重新同步..."
            & $uvCmd sync --locked
            if ($LASTEXITCODE -ne 0) {
                Pop-Location
                Stop-WithHint "依赖安装失败" "检查 pyproject.toml 和网络连接"
            }
        }
        Write-Ok "依赖已就绪"
    } catch {
        Pop-Location
        Stop-WithHint "依赖安装异常: $_" "运行 uv sync --locked 查看详细错误"
    } finally {
        # 恢复严格模式
        $ErrorActionPreference = $oldEAP
    }
    Pop-Location
}

# 验证关键依赖
$venvPython = Join-Path $VenvDir "Scripts\python.exe"
if (Test-Path $venvPython) {
    $pythonExe = $venvPython
    Write-Ok "使用虚拟环境 Python: $pythonExe"
} else {
    $pythonExe = $pythonCmd
    Write-Warn "未找到虚拟环境，使用系统 Python: $pythonExe"
}

# ═══════════════════════════════════════════════════════════
# Step 4: 检查知识库文件
# ═══════════════════════════════════════════════════════════
Write-Step "Step 4/6 - 检查知识库"

if ($SkipKB) {
    Write-Info "已跳过知识库检查 (-SkipKB)"
} else {
    $kbDir = Join-Path $ProjectDir "knowledge_base"
    if (Test-Path $kbDir) {
        $kbFiles = Get-ChildItem -Path $kbDir -Filter "*.txt" -ErrorAction SilentlyContinue
        if ($kbFiles -and $kbFiles.Count -gt 0) {
            Write-Ok "知识库文件就绪 ($($kbFiles.Count) 个文件)"
        } else {
            Write-Warn "知识库目录为空: $kbDir"
            Write-Info "请将法规文件放入 knowledge_base/ 目录"
        }
    } else {
        Write-Warn "知识库目录不存在: $kbDir"
        Write-Info "本地知识库模式将在首次检索时自动加载"
    }

    # 检查 assets 目录
    $assetsDir = Join-Path $ProjectDir "assets"
    if (Test-Path (Join-Path $assetsDir "wqy-microhei.ttc")) {
        Write-Ok "中文字体文件已就绪"
    } else {
        Write-Warn "中文字体缺失: assets/wqy-microhei.ttc"
        Write-Info "PDF/图表中的中文可能显示异常"
    }
}

# ═══════════════════════════════════════════════════════════
# Step 5: 检查必要文件
# ═══════════════════════════════════════════════════════════
Write-Step "Step 5/6 - 检查项目文件"

$requiredFiles = @(
    @{ Path = Join-Path $SrcDir "main.py";        Desc = "主入口" },
    @{ Path = Join-Path $SrcDir "local_shims.py";  Desc = "本地兼容层" },
    @{ Path = Join-Path $SrcDir "local_storage.py"; Desc = "本地存储" },
    @{ Path = Join-Path $SrcDir "local_knowledge.py"; Desc = "本地知识库" },
    @{ Path = Join-Path $SrcDir "agents\agent.py"; Desc = "Agent 定义" },
    @{ Path = Join-Path $ProjectDir "config\agent_llm_config.json"; Desc = "LLM 配置" }
)

$allReady = $true
foreach ($f in $requiredFiles) {
    if (Test-Path $f.Path) {
        Write-Ok "$($f.Desc): $(Split-Path -Leaf $f.Path)"
    } else {
        Write-Err "缺失 $($f.Desc): $($f.Path)"
        $allReady = $false
    }
}

if (-not $allReady) {
    Stop-WithHint `
        "缺少必要文件，无法启动" `
        "请确认本地化改造的所有新增文件已创建（local_shims.py, local_storage.py, local_knowledge.py）"
}

# ═══════════════════════════════════════════════════════════
# Step 6: 启动服务
# ═══════════════════════════════════════════════════════════
Write-Step "Step 6/6 - 启动服务"
Write-Host ""

# 将 src 置于 PYTHONPATH 最前（原有值以分号保留，避免覆盖外部配置）
if ($env:PYTHONPATH) {
    $env:PYTHONPATH = "$SrcDir;$env:PYTHONPATH"
} else {
    $env:PYTHONPATH = $SrcDir
}

$mainPy = Join-Path $SrcDir "main.py"
$baseArgs = @($mainPy, "-m")

# 启动前预检端口占用（web/http 模式）：这是最常见的启动失败场景，
# 提前给出明确提示，而不是等 uvicorn 报错退出后用户对着窗口猜
if ($Mode -in @("web", "http")) {
    try {
        $busy = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
        if ($busy) {
            Write-Warn "端口 $Port 已被占用（PID: $(($busy.OwningProcess | Select-Object -Unique) -join ', ')）"
            Write-Info "请先停掉占用端口的进程，或换端口重启: .\start.ps1 -Port 8080"
            Write-Host ""
        }
    } catch { }  # 检测失败不阻断启动，uvicorn 自己会报错
}

switch ($Mode) {
    "web" {
        $baseArgs += @("http", "-p", $Port)
        Write-Host "  🌐 Web 可视化界面: http://localhost:$Port" -ForegroundColor Green
        Write-Host ""
        Write-Host "  📋 API 端点:" -ForegroundColor Gray
        Write-Host "     GET  /             - Web 可视化界面" -ForegroundColor Gray
        Write-Host "     GET  /health       - 健康检查" -ForegroundColor Gray
        Write-Host "     GET  /api/status   - 系统状态" -ForegroundColor Gray
        Write-Host "     POST /run          - 同步运行" -ForegroundColor Gray
        Write-Host "     POST /stream_run   - 流式运行 (SSE)" -ForegroundColor Gray
        Write-Host ""
        Write-Host "  ⏹  按 Ctrl+C 停止服务" -ForegroundColor Gray
        Write-Host ""

        if (-not $NoBrowser) {
            # 独立隐藏进程轮询健康检查，服务就绪后再开浏览器。
            # 不用 Start-Job：Job 会话里 Start-Process 打 URL 常静默失败；
            # 探测用 127.0.0.1 而非 localhost，避免 Windows 上 localhost 优先解析
            # IPv6 ::1 而 uvicorn 只绑 IPv4 导致探不通。
            $watcherPath = Join-Path $env:TEMP "zhinengti_open_browser.ps1"
            @"
for (`$i = 0; `$i -lt 240; `$i++) {
    try {
        `$r = Invoke-WebRequest -Uri 'http://127.0.0.1:$Port/health' -UseBasicParsing -TimeoutSec 2
        if (`$r.StatusCode -eq 200) { Start-Process 'http://localhost:$Port'; exit }
    } catch { }
    Start-Sleep -Milliseconds 500
}
"@ | Set-Content $watcherPath -Encoding UTF8
            Start-Process powershell.exe -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-WindowStyle','Hidden','-File',$watcherPath -WindowStyle Hidden | Out-Null
            Write-Host "  ⏳ 服务初始化中（首次约 10-30 秒），就绪后将自动打开浏览器..." -ForegroundColor Yellow
            Write-Host "  💡 若未自动弹出，请手动访问: http://localhost:$Port" -ForegroundColor Gray
            Write-Host ""
        }
    }
    "http" {
        $baseArgs += @("http", "-p", $Port)
        Write-Host "  🚀 HTTP 服务: http://localhost:$Port" -ForegroundColor Green
        Write-Host ""
    }
    "flow" {
        $baseArgs += "flow"
        if ($UserInput) { $baseArgs += @("-i", $UserInput) }
        Write-Host "  🚀 CLI 模式 - 运行完整 Agent 流程" -ForegroundColor Green
        Write-Host ""
    }
    "node" {
        if (-not $Node) {
            Stop-WithHint "node 模式需要指定 -Node 参数" `
                "示例: .\start.ps1 -Mode node -Node node_1 -UserInput '{`"text`":`"测试`"}'"
        }
        $baseArgs += @("node", "-n", $Node)
        if ($UserInput) { $baseArgs += @("-i", $UserInput) }
        Write-Host "  🚀 CLI 模式 - 运行节点: $Node" -ForegroundColor Green
        Write-Host ""
    }
}

try {
    & $pythonExe @baseArgs
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0) {
        Write-Host ""
        Write-Err "进程退出，退出码: $exitCode"
    }
} catch {
    Write-Host ""
    Write-Err "启动失败: $_"
    Write-Host ""
    Write-Host "  💡 常见问题排查:" -ForegroundColor Yellow
    Write-Host "     1. ModuleNotFoundError → 运行 .\start.ps1 (不加 -SkipSync)" -ForegroundColor Gray
    Write-Host "     2. OPENAI_API_KEY 错误 → 检查 .env 文件" -ForegroundColor Gray
    Write-Host "     3. 端口被占用 → 换端口: .\start.ps1 -Port 8080" -ForegroundColor Gray
    Write-Host "     4. 查看详细日志 → 设置 .env 中 LOG_LEVEL=DEBUG" -ForegroundColor Gray
}
