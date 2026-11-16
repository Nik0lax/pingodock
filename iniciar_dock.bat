@echo off
rem Inicia o Pingo Dock sem janela de console, num processo so (Python direto,
rem sem o lancador do .venv, que ficaria ocupando memoria a toa).
cd /d "%~dp0"
if not exist ".venv\pyvenv.cfg" (
    echo Criando ambiente virtual e instalando dependencias...
    python -m venv .venv || exit /b 1
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt || exit /b 1
)
for /f "tokens=1,* delims== " %%a in ('findstr /b /c:"home" ".venv\pyvenv.cfg"') do set "PYHOME=%%b"
start "" "%PYHOME%\pythonw.exe" "%~dp0dock.py"
