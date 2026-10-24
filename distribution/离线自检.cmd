@echo off
setlocal
cd /d "%~dp0.."
"runtime\python.exe" -I "distribution\prepare_package.py"
if errorlevel 1 goto failed
"runtime\python.exe" -I "distribution\check_package.py" --verify-files
if errorlevel 1 goto failed
echo OFFLINE_CHECK_PASSED - No motor commands or camera access.
pause
exit /b 0
:failed
echo OFFLINE_CHECK_FAILED - Keep this output for diagnosis.
pause
exit /b 1
