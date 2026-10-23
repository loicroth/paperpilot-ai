@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel% equ 0 (
    py -3 launcher.py
) else (
    where python >nul 2>nul
    if errorlevel 1 (
        echo ERREUR : Python 3.11 ou plus recent est requis.
        echo Installez Python depuis https://www.python.org/downloads/
        echo Cochez Add Python to PATH pendant l'installation.
        pause
        exit /b 1
    )
    python launcher.py
)
if errorlevel 1 (
    echo.
    echo Le lancement a echoue. Lisez le message ci-dessus.
    pause
    exit /b 1
)
endlocal
