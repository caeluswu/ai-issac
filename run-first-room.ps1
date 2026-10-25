# One entry point; arguments are forwarded without string evaluation.
$ErrorActionPreference = 'Stop'
$pythonPath = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw 'Missing .venv. Install the project Python environment first.'
}
& $pythonPath (Join-Path $PSScriptRoot 'run_first_room.py') @args
exit $LASTEXITCODE
