@echo off
REM 使用Nuitka将InvoiceParser 打包成 Windows 系统下可运行的 exe 程序。
REM 在项目根目录执行

set SCRIPT_DIR=%~dp0
cd /d "%SCRIPT_DIR%"

python -m nuitka --standalone --onefile --mingw64 --noinclude-unittest-mode=nofollow --noinclude-pytest-mode=nofollow --lto=yes --jobs=4 --windows-uac-admin --onefile-tempdir-spec="{CACHE_DIR}/todd_dev_studio/invoice" --windows-icon-from-ico=logo.ico --company-name="Todd Dev Studio" --product-name="InvoiceParser" --file-version=1.0.0.0 --product-version=1.0.0.0 --file-description="用于识别、读取发票数据的自动化工具" --copyright="Copyright (c) 2025 Todd Dev Studio. All rights reserved." --output-filename=invoice src\invoice\main.py

echo.
echo Build finished. Check invoice.exe in current directory.
pause