@echo off
setlocal
cd /d "%~dp0"

echo ============================================================
echo   GEM Macro Optimization - Automated Test Suite
echo ============================================================
echo.

wsl bash -lc "cd \"$(wslpath '%CD%')\" && echo '[1/4] Running DSP48E2 Primitive Test...' && nvcc -O3 golden_models/dsp_test.cu -o dsp_test && ./dsp_test && echo '' && echo '[2/4] Running CARRY4 Primitive Test...' && nvcc -O3 golden_models/carry4_test.cu -o carry4_test && ./carry4_test && echo '' && echo '[3/4] Running SRLC32E Primitive Test...' && nvcc -O3 golden_models/srlc32e_test.cu -o srlc32e_test && ./srlc32e_test && echo '' && echo '[4/4] Running Python Behavioral Golden Check...' && python3 tests/macro_gold_check.py ./target/release/naive_sim && rm -f dsp_test carry4_test srlc32e_test"

echo.
echo ============================================================
echo   All tests finished!
echo ============================================================
pause