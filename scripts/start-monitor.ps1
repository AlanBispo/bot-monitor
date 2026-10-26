param([switch]$Background)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw 'Python do projeto ausente. Instale as dependencias conforme o README.'
}
if (-not (Test-Path -LiteralPath (Join-Path $projectRoot '.env'))) {
    throw 'Crie o arquivo .env conforme o README antes de iniciar.'
}

# Prevent a manual start and Task Scheduler from polling the same bot together.
$mutex = [System.Threading.Mutex]::new($false, 'Local\PriceMonitorBot')
$ownsMutex = $false
try {
    try { $ownsMutex = $mutex.WaitOne(0) }
    catch [System.Threading.AbandonedMutexException] { $ownsMutex = $true }
    if (-not $ownsMutex) { throw 'O monitor ja esta em execucao nesta sessao.' }
    $env:PYTHONUNBUFFERED = '1'
    $env:PYTHONUTF8 = '1'
    [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
    & $pythonPath -c "import os, sys; from dotenv import load_dotenv; load_dotenv('.env'); sys.exit(0 if os.getenv('TELEGRAM_BOT_TOKEN', '').strip() and os.getenv('TELEGRAM_ALLOWED_CHAT_ID', '').strip().lstrip('-').isdigit() else 2)"
    if ($LASTEXITCODE -eq 2) {
        Write-Output 'Preencha TELEGRAM_BOT_TOKEN e TELEGRAM_ALLOWED_CHAT_ID no .env e inicie novamente.'
        exit 0
    }
    if ($LASTEXITCODE -ne 0) { throw 'Falha ao validar o ambiente Python.' }
    # Keep native stderr as log output instead of PowerShell terminating errors.
    $ErrorActionPreference = 'Continue'
    if ($Background) {
        $logDirectory = Join-Path $projectRoot 'logs'
        New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
        $logPath = Join-Path $logDirectory ('monitor-{0}.log' -f (Get-Date -Format 'yyyy-MM-dd'))
        & $pythonPath -u (Join-Path $projectRoot 'main.py') >> $logPath 2>&1
    } else {
        & $pythonPath -u (Join-Path $projectRoot 'main.py')
    }
    $monitorExitCode = $LASTEXITCODE
} finally {
    if ($ownsMutex) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
exit $monitorExitCode
