@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Instalando suporte a certificados do Windows (truststore) e CA atualizadas...
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 -m pip install --upgrade truststore certifi
) else (
  python -m pip install --upgrade truststore certifi
)
echo.
echo Feche o servidor antigo e execute INICIAR.bat.
pause
