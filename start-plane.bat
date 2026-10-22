@echo off
setlocal EnableExtensions DisableDelayedExpansion

set "PLANE_CHECK=0"
set "PLANE_BROWSER=1"
set "PLANE_PAUSE=1"
set "PLANE_PUSHED=0"
set "PLANE_ROOT=%~dp0"

:parse_args
if "%~1"=="" goto args_done
if /I "%~1"=="--check" goto set_check
if /I "%~1"=="--no-browser" goto set_no_browser
if /I "%~1"=="--no-pause" goto set_no_pause
echo Unknown option: %~1
echo Usage: start-plane.bat [--check] [--no-browser] [--no-pause]
goto usage_error

:set_check
set "PLANE_CHECK=1"
shift
goto parse_args

:set_no_browser
set "PLANE_BROWSER=0"
shift
goto parse_args

:set_no_pause
set "PLANE_PAUSE=0"
shift
goto parse_args

:usage_error
set "PLANE_EXIT=2"
goto failed

:args_done
pushd "%PLANE_ROOT%" >nul 2>&1
if errorlevel 1 (
    echo ERROR: Cannot open Plane directory: %PLANE_ROOT%
    set "PLANE_EXIT=1"
    goto failed
)
set "PLANE_PUSHED=1"

for /f "usebackq delims=" %%U in (`powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "Import-Module '.\dev\NativeDev.Common.psm1' -Force; Get-NativeWebBaseUrl"`) do set "PLANE_WEB_URL=%%U"
if not defined PLANE_WEB_URL (
    echo ERROR: Could not resolve the native web URL.
    set "PLANE_EXIT=1"
    goto failed
)

if "%PLANE_CHECK%"=="1" goto run_check
if not exist "dev\start-native.ps1" (
    echo ERROR: Missing launcher: %PLANE_ROOT%dev\start-native.ps1
    set "PLANE_EXIT=1"
    goto failed
)
if not exist "dev\check-native.ps1" (
    echo ERROR: Missing checker: %PLANE_ROOT%dev\check-native.ps1
    set "PLANE_EXIT=1"
    goto failed
)
echo Starting Plane native development services...
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0dev\start-native.ps1"
set "PLANE_EXIT=%ERRORLEVEL%"
if not "%PLANE_EXIT%"=="0" goto finish

echo Checking all Plane native development services...
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0dev\check-native.ps1"
set "PLANE_EXIT=%ERRORLEVEL%"
if not "%PLANE_EXIT%"=="0" goto finish

if "%PLANE_BROWSER%"=="1" (
    echo Opening Plane in the default browser...
    start "" "%PLANE_WEB_URL%"
)
goto finish

:run_check
if not exist "dev\check-native.ps1" (
    echo ERROR: Missing checker: %PLANE_ROOT%dev\check-native.ps1
    set "PLANE_EXIT=1"
    goto failed
)
echo Checking Plane native development services...
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0dev\check-native.ps1"
set "PLANE_EXIT=%ERRORLEVEL%"
goto finish

:finish
popd
set "PLANE_PUSHED=0"
if not "%PLANE_EXIT%"=="0" goto failed
echo.
echo Plane is running at %PLANE_WEB_URL%
if "%PLANE_PAUSE%"=="0" exit /b 0
echo This window will close in 8 seconds.
timeout /t 8 /nobreak >nul 2>&1
exit /b 0

:failed
if "%PLANE_PUSHED%"=="1" popd
echo.
echo Plane did not start successfully. See the error above.
echo For diagnostics, run: start-plane.bat --check --no-pause
if "%PLANE_PAUSE%"=="0" exit /b %PLANE_EXIT%
pause
exit /b %PLANE_EXIT%
