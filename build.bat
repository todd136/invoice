@echo off
REM Build InvoiceParser exe with Nuitka. Run from project root.

set SCRIPT_DIR=%~dp0
cd /d "%SCRIPT_DIR%"

python -m nuitka ^
  --standalone ^
  --onefile ^
  --mingw64 ^
  --assume-yes-for-downloads ^
  --noinclude-unittest-mode=nofollow ^
  --noinclude-pytest-mode=nofollow ^
  --lto=yes ^
  --jobs=4 ^
  --windows-uac-admin ^
  --onefile-tempdir-spec="{CACHE_DIR}/todd_dev_studio/invoice" ^
  --windows-icon-from-ico=logo.ico ^
  --company-name="Todd Dev Studio" ^
  --product-name="InvoiceParser" ^
  --file-version=1.0.0.0 ^
  --product-version=1.0.0.0 ^
  --file-description="Invoice PDF parser" ^
  --copyright="Copyright ^(c^) 2025 Todd Dev Studio. All rights reserved." ^
  --output-filename=invoice ^
  src\invoice\main.py

if errorlevel 1 exit /b %errorlevel%

echo.
echo Build finished. Check invoice.exe in current directory.
if defined GITHUB_ACTIONS exit /b 0
pause
exit /b 0
