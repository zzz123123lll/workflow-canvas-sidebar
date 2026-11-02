param(
  [string]$CanvasHome = "$env:USERPROFILE\.dsh-workflow-canvas",
  [string]$Python = "",
  [string]$Plugin = "",
  [switch]$NoVenv,
  [switch]$RepairDependencies,
  [switch]$RegisterProfile,
  [string]$ProfilePath = "$env:USERPROFILE\.dsh\profiles\desktop"
)

$ErrorActionPreference = "Stop"
if ($PSVersionTable.PSVersion.Major -lt 7) {
  throw '请使用免费的 PowerShell 7 运行安装程序：https://learn.microsoft.com/powershell/scripting/install/installing-powershell-on-windows 。安装后双击 install.cmd，无需修改全局执行策略。'
}
if (-not $Plugin) {
  if (Test-Path (Join-Path $PSScriptRoot "server")) {
    $Plugin = $PSScriptRoot
  } else {
    $candidate = Join-Path (Split-Path -Parent $PSScriptRoot) "dsh-plugins\workflow-canvas-sidebar"
    if (Test-Path (Join-Path $candidate "server")) { $Plugin = $candidate }
    else { $Plugin = Split-Path -Parent $PSScriptRoot }
  }
}
$Seed = Join-Path $Plugin "seed"
$Requirements = Join-Path $Plugin "requirements.txt"
$ConfigPath = Join-Path $Plugin "canvas-home.json"
# An update without explicit overrides must keep using the existing data and
# Python environment; do not make existing tasks disappear into a fresh home.
if (Test-Path -LiteralPath $ConfigPath) {
  $ExistingConfig = Get-Content -LiteralPath $ConfigPath -Raw | ConvertFrom-Json
  if (-not $PSBoundParameters.ContainsKey('CanvasHome') -and $ExistingConfig.home) { $CanvasHome = $ExistingConfig.home }
  if (-not $PSBoundParameters.ContainsKey('Python') -and $ExistingConfig.python -and (Test-Path -LiteralPath $ExistingConfig.python)) { $Python = $ExistingConfig.python }
}

Write-Host "Plugin: $Plugin"
Write-Host "Data:   $CanvasHome"

if (-not (Test-Path $Seed)) { throw "缺少 seed 目录：$Seed" }

$dataExisted = Test-Path -LiteralPath $CanvasHome
$CanvasHome = [System.IO.Path]::GetFullPath($CanvasHome)
New-Item -ItemType Directory -Path $CanvasHome -Force | Out-Null
$markerPath = Join-Path $CanvasHome '.workflow-canvas-home.json'
if (-not (Test-Path -LiteralPath $markerPath)) {
  @{ product = 'dsh-workflow-canvas-sidebar'; schema = 1; data_home = $CanvasHome;
     instance_id = [guid]::NewGuid().ToString('N'); exclusive = (-not $dataExisted) } |
    ConvertTo-Json | Set-Content -LiteralPath $markerPath -Encoding utf8
}
foreach ($name in @("kb", "episodes", "artifacts", ".runs")) {
  New-Item -ItemType Directory -Path (Join-Path $CanvasHome $name) -Force | Out-Null
}

# Seed only when this is a fresh home; never overwrite user data.
if (-not (Test-Path (Join-Path $CanvasHome "kb\modules.json"))) {
  Copy-Item (Join-Path $Seed "kb\*") (Join-Path $CanvasHome "kb") -Recurse -Force
  Write-Host "Seeded default KB and workflow library."
} else {
  Write-Host "Existing KB found; seed skipped."
}

# Existing user-installed skills remain in their data home. Starter templates use original instructions.

# Prefer the bundled runtime: users do not need Python preinstalled.
$BundledPy = Join-Path $Plugin "runtime\python\python.exe"
$UseBundled = $false
if (-not $Python -and (Test-Path $BundledPy)) {
  $Python = $BundledPy
  $UseBundled = $true
  Write-Host "Using bundled Python: $Python"
}
# Pick Python
if (-not $Python) {
  $cmd = Get-Command python -ErrorAction SilentlyContinue
  if (-not $cmd) { $cmd = Get-Command py -ErrorAction SilentlyContinue }
  if (-not $cmd) { throw "找不到 Python。请安装 Python 3.12 后重试，或传入 -Python <路径>。" }
  $Python = $cmd.Source
}
Write-Host "Python: $Python"
if ((Test-Path -LiteralPath $BundledPy) -and [IO.Path]::GetFullPath($Python) -eq [IO.Path]::GetFullPath($BundledPy)) { $UseBundled = $true }

$keepExistingPython = $ExistingConfig -and $ExistingConfig.python -and [IO.Path]::GetFullPath([string]$ExistingConfig.python) -eq [IO.Path]::GetFullPath($Python)
if (-not $NoVenv -and -not $UseBundled -and -not $keepExistingPython) {
  $Venv = Join-Path $CanvasHome 'venv'
  if (-not (Test-Path -LiteralPath (Join-Path $Venv 'Scripts\python.exe'))) {
    & $Python -m venv $Venv
    if ($LASTEXITCODE -ne 0) { throw '创建独立Python环境失败；请使用完整发行ZIP或按开发指南安装Python。' }
  }
  $Python = Join-Path $Venv 'Scripts\python.exe'
}
$dependencyCheck = & $Python -c "import importlib.metadata as m; assert m.version('deepseek-harness-sdk') == '0.2.1rc2'; assert m.version('pypdf') == '6.14.2'; from deepseek_harness import DeepSeekHarness; from pypdf import PdfReader; print('ready')" 2>$null
$dependenciesReady = $LASTEXITCODE -eq 0 -and $dependencyCheck -eq 'ready'
if ($dependenciesReady) { Write-Host 'Dependencies already present. No network or pip installation needed.' }
elseif (-not $RepairDependencies) {
  throw '本地依赖缺失或版本不符。请重新解压完整发行包；或显式加 -RepairDependencies 联网修复。已保留数据和现有配置。'
}
if (-not $dependenciesReady -and $RepairDependencies) {
  if ($UseBundled) {
    & $Python -m pip --version *> $null
    if ($LASTEXITCODE -ne 0) {
      $getpip = Join-Path $Plugin "runtime\get-pip.py"
      if (-not (Test-Path $getpip)) { throw "缺少 runtime\get-pip.py，无法初始化内置 Python" }
      & $Python $getpip --no-warn-script-location
      if ($LASTEXITCODE -ne 0) { throw "初始化内置 Python pip 失败" }
    }
    if (Test-Path $Requirements) {
      & $Python -m pip install --disable-pip-version-check --no-warn-script-location -r $Requirements
      if ($LASTEXITCODE -ne 0) { throw "安装 requirements 失败" }
    }
  } else {
    $Venv = Join-Path $CanvasHome "venv"
    if (-not (Test-Path (Join-Path $Venv "Scripts\python.exe"))) {
      & $Python -m venv $Venv
      if ($LASTEXITCODE -ne 0) { throw "创建 venv 失败" }
    }
    $VenvPython = Join-Path $Venv "Scripts\python.exe"
    if (Test-Path $Requirements) {
      & $VenvPython -m pip install --disable-pip-version-check -r $Requirements
      if ($LASTEXITCODE -ne 0) { throw "安装 requirements 失败" }
    }
    $Python = $VenvPython
  }
}

# Always record the environment actually installed above, including a custom
# Python with the default data home. The machine-local config is not packaged.
& $Python (Join-Path $Plugin 'scripts\seed.py') --home $CanvasHome --seed $Seed
if ($LASTEXITCODE -ne 0) { throw '首发模板导入未完成；现有定义未被覆盖，请查看上方错误。' }
if ($ExistingConfig -and $ExistingConfig.home -eq $CanvasHome -and $ExistingConfig.python -eq $Python) {
  Write-Host 'Existing machine configuration preserved byte for byte.'
} else {
  $configValue = @{}
  if ($ExistingConfig) { foreach ($property in $ExistingConfig.PSObject.Properties) { $configValue[$property.Name] = $property.Value } }
  $configValue.home = $CanvasHome; $configValue.python = $Python
  $configValue | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $ConfigPath -Encoding UTF8
  Write-Host "Wrote $ConfigPath"
}

Write-Host ""
Write-Host "Install complete."
Write-Host "1. Restart DSH (or reopen the plugin)."
Write-Host "2. Open the left sidebar '画布'."
Write-Host "3. If the backend does not start, check:"
Write-Host "   $CanvasHome\.runs\api-server.log"
Write-Host ""
Write-Host "Home: $CanvasHome"
Write-Host "Python: $Python"
Write-Host ""

$preflight = Join-Path $Plugin "preflight-canvas.ps1"
if (Test-Path $preflight) {
  Write-Host "Running preflight checks ..."
  & (Join-Path $PSHOME 'pwsh.exe') -NoProfile -File $preflight -Plugin $Plugin -CanvasHome $CanvasHome -Python $Python
  if ($LASTEXITCODE -ne 0) {
    throw "安装预检查未通过；请按上方 FAIL 项修复后重试。WARN 项仅影响对应功能。"
  }
}

if ($RegisterProfile) {
  & (Join-Path $Plugin 'register-plugin.ps1') -Plugin $Plugin -ProfilePath $ProfilePath
}
