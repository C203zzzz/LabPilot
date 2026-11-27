$ErrorActionPreference = 'Stop'
Set-Location (Split-Path $PSScriptRoot -Parent)
# Import current-user settings even when the parent terminal predates the change.
foreach ($name in @('DEEPSEEK_API_KEY', 'TAVILY_API_KEY')) {
    $value = [Environment]::GetEnvironmentVariable($name, 'User')
    if ($value) { [Environment]::SetEnvironmentVariable($name, $value, 'Process') }
}
python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
