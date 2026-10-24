param(
    [ValidateSet('run','probe','cameras')][string]$Action = 'run',
    [string]$Camera = '2MP USB Camera',
    [int]$Width = 1280,
    [int]$Height = 720,
    [double]$Seconds = 0
)
$taskPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $taskPython)) { throw 'CV environment missing; see README.md.' }
& $taskPython (Join-Path $PSScriptRoot 'cv_camera_test.py') $Action --camera $Camera --width $Width --height $Height --seconds $Seconds.ToString([cultureinfo]::InvariantCulture)
exit $LASTEXITCODE
