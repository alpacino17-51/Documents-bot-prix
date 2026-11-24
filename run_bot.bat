@echo off
REM ==========================================================================
REM  Bot erreurs de prix - lancement local Windows
REM  - trouve Python (3.12 > 3.11 > 3.x), crée le venv si besoin
REM  - vérifie / installe les dépendances (requirements.txt)
REM  - lance un passage toutes les 2 minutes (general.loop_interval_seconds)
REM  Arrêt : Ctrl+C puis "O" (ou fermer la fenêtre)
REM ==========================================================================
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0"
title Bot erreurs de prix

REM ---- 1) Python -----------------------------------------------------------
set "PY="
py -3.12 -V >nul 2>nul && set "PY=py -3.12"
if not defined PY py -3.11 -V >nul 2>nul && set "PY=py -3.11"
if not defined PY py -3 -V >nul 2>nul && set "PY=py -3"
if not defined PY python -V >nul 2>nul && set "PY=python"
if not defined PY (
    echo [ERREUR] Python est introuvable. Installe Python 3.12 depuis https://www.python.org/downloads/
    echo          en cochant "Add python.exe to PATH", puis relance ce script.
    pause
    exit /b 1
)

REM ---- 2) Environnement virtuel ----------------------------------------------
if not exist "venv\Scripts\python.exe" (
    echo [INFO] Création de l'environnement virtuel venv avec %PY% ...
    %PY% -m venv venv
    if errorlevel 1 (
        echo [ERREUR] Impossible de créer le venv.
        pause
        exit /b 1
    )
)
set "VPY=venv\Scripts\python.exe"

REM ---- 3) Dépendances --------------------------------------------------------
echo [INFO] Vérification des dépendances...
"%VPY%" -m pip install --disable-pip-version-check -q -r requirements.txt
if errorlevel 1 (
    echo [ERREUR] Installation des dépendances impossible. Vérifie ta connexion Internet.
    pause
    exit /b 1
)

REM ---- 4) Secrets ------------------------------------------------------------
if not exist ".env" (
    echo [ATTENTION] Fichier .env absent. Copie .env.example en .env et remplis
    echo             TELEGRAM_BOT_TOKEN et TELEGRAM_CHAT_ID, puis relance.
    pause
    exit /b 1
)

REM ---- 5) Intervalle (config.json) ------------------------------------------
set "INTERVAL=120"
for /f "usebackq delims=" %%i in (`%VPY% -c "import json;print(int(json.load(open('config.json',encoding='utf-8'))['general'].get('loop_interval_seconds',120)))"`) do set "INTERVAL=%%i"
echo [INFO] Passage toutes les %INTERVAL% secondes. Journal : logs\bot.log

REM ---- 6) Boucle ---------------------------------------------------------------
:loop
echo.
echo ===================== %date% %time:~0,8% =====================
"%VPY%" main.py --once --log-file
if errorlevel 1 echo [ATTENTION] Passage terminé avec une erreur ^(voir ci-dessus^). Nouvel essai au prochain tour.
timeout /t %INTERVAL% /nobreak >nul
goto loop
