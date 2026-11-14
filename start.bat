@echo off
REM start.bat - double-click to launch the whole CirrhosisRegistry stack.
REM Picks the interpreter that actually has the project dependencies:
REM on this machine the default 'python' is 3.14 and has none of them.
cd /d "%~dp0"

py -3.13 -c "import flask, redis, rq" >nul 2>&1
if %errorlevel%==0 (
    py -3.13 start.py
    goto :end
)

python -c "import flask, redis, rq" >nul 2>&1
if %errorlevel%==0 (
    python start.py
    goto :end
)

echo.
echo   No Python with the project dependencies was found.
echo   Install them with:
echo       py -3.13 -m pip install -r requirements.txt
echo.

:end
pause
