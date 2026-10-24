param(
    [ValidateSet('ports', 'probe', 'pulse', 'stop')]
    [string]$Action = 'probe',
    [string]$Port = 'COM42',
    [int]$Motor = 1,
    [double]$Value = 0,
    [double]$Seconds = 1,
    [switch]$BenchReady,
    [switch]$Parameters,
    [switch]$ObserveOutput,
    [switch]$NoMotorConnected
)
$taskPython = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
if (-not (Test-Path -LiteralPath $taskPython)) {
    $taskPython = (Get-Command python -ErrorAction Stop).Source
}
$taskArgs = @((Join-Path $PSScriptRoot 'px4_control_mvp.py'), $Action, '--port', $Port,
              '--motor', "$Motor", '--value', $Value.ToString([cultureinfo]::InvariantCulture),
              '--seconds', $Seconds.ToString([cultureinfo]::InvariantCulture))
if ($BenchReady) { $taskArgs += '--bench-ready' }
if ($Parameters) { $taskArgs += '--parameters' }
if ($ObserveOutput) { $taskArgs += '--observe-output' }
if ($NoMotorConnected) { $taskArgs += '--no-motor-connected' }
& $taskPython @taskArgs
exit $LASTEXITCODE
