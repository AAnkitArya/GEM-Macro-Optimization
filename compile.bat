@echo off
setlocal enabledelayedexpansion
REM =========================================================================
REM  compile.bat -- Windows entry point: compile, run and benchmark GEM.
REM
REM  Why this shells out to WSL2 instead of running natively on Windows:
REM  this repo's toolchain is Linux-only by construction, not by omission.
REM    - .cargo\config.toml points CXX at scripts/gxx-cstdint, a POSIX
REM      shell script (#!/bin/sh) that patches mt-kahypar-sc's build for
REM      modern GCC. cc-rs cannot execute a shebang script as a compiler
REM      on native Windows.
REM    - docs\TECHNICAL_REPORT.md's own measured environment is
REM      "RTX 3050 Laptop ... under WSL2" -- the team built and benchmarked
REM      this exact repo through WSL2, not natively.
REM    - Yosys, mt-kahypar, and the CUDA toolkit's nvcc all need a Linux
REM      userspace here.
REM  So: install WSL2 + a Linux distro (see Microsoft's docs, `wsl --install`
REM  from an elevated PowerShell), install Rust/CUDA/Yosys *inside* that
REM  WSL2 distro per usage.md, and this script will drive it from Windows.
REM
REM  Usage (from a normal Windows cmd.exe, in this repo's folder):
REM    compile.bat                  compile + verify + synth + map + bench
REM    compile.bat --no-cuda        compile + CPU-side correctness only
REM    compile.bat --blocks 40      override NUM_BLOCKS (default: see run_all.sh)
REM    compile.bat --skip-bench     skip the slow Nsight Compute pass
REM  All arguments are forwarded verbatim to scripts/run_all.sh.
REM =========================================================================

where wsl >nul 2>&1
if errorlevel 1 (
    echo [compile.bat] FATAL: "wsl" was not found on PATH.
    echo.
    echo   This repo's build ^(Rust + mt-kahypar-sc + CUDA + Yosys^) only runs
    echo   under Linux / WSL2 -- see the comment block at the top of this file
    echo   for why. Install WSL2 first:
    echo       1^) Open PowerShell as Administrator
    echo       2^) Run: wsl --install
    echo       3^) Reboot if prompted, then follow usage.md inside the WSL2 shell
    echo          to install Rust, the CUDA Toolkit, and Yosys.
    echo   Then re-run compile.bat.
    exit /b 1
)

REM Translate this folder's Windows path into its WSL2 path (e.g.
REM C:\Users\me\GEM -> /mnt/c/Users/me/GEM) so the WSL side cds to the
REM right place regardless of where this repo was cloned/extracted.
for /f "usebackq delims=" %%P in (`wsl wslpath -a "%~dp0."`) do set "WSL_REPO_PATH=%%P"

if "%WSL_REPO_PATH%"=="" (
    echo [compile.bat] FATAL: could not translate "%~dp0" to a WSL path.
    echo   Is your default WSL distro installed and working? Try "wsl echo ok".
    exit /b 1
)

echo [compile.bat] Repo path inside WSL2: %WSL_REPO_PATH%
echo [compile.bat] Handing off to scripts/run_all.sh ...
echo.

wsl bash -lc "cd '%WSL_REPO_PATH%' && bash scripts/run_all.sh %*"
set "RC=%ERRORLEVEL%"

echo.
if "%RC%"=="0" (
    echo [compile.bat] Done. See build\results.csv for benchmark output.
) else (
    echo [compile.bat] run_all.sh exited with code %RC% -- see the output above.
)
exit /b %RC%
