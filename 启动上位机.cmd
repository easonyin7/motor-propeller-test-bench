@echo off
setlocal
cd /d "%~dp0"
set "BENCH_PYTHON=cv_mvp\.venv\Scripts\python.exe"
set "BENCH_PYTHONW=cv_mvp\.venv\Scripts\pythonw.exe"
if exist "runtime\python.exe" (
  set "BENCH_PYTHON=runtime\python.exe"
  set "BENCH_PYTHONW=runtime\pythonw.exe"
)
if not exist "%BENCH_PYTHONW%" (
  echo Camera Python environment not found. See bench_app\README.md.
  pause
  exit /b 1
)
if exist "package_info.json" (
  "%BENCH_PYTHON%" -I "distribution\prepare_package.py"
  if errorlevel 1 (
    echo Package preparation failed. See distribution\DELIVERY_README.md.
    pause
    exit /b 1
  )
)
start "" "%BENCH_PYTHONW%" -m bench_app
