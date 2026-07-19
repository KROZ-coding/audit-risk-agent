@echo off
chcp 65001 >nul 2>&1
title 审计风险识别系统
cd /d "%~dp0"
REM 自动修复 start.ps1 编码：清除多余BOM，确保UTF-8+单BOM（PowerShell 5.1必需）
powershell -NoProfile -Command "$f='.\start.ps1';if(Test-Path $f){$c=[IO.File]::ReadAllText($f,[Text.Encoding]::UTF8)-replace'^\uFEFF+','';[IO.File]::WriteAllText($f,$c,[Text.UTF8Encoding]::new($true))}"
powershell -ExecutionPolicy Bypass -File ".\start.ps1" -SkipSync
