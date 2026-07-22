@echo off
REM Fallback launcher (a console flashes briefly). Prefer "Launch Vibedentify.vbs".
cd /d "%~dp0"
start "" pythonw.exe "%~dp0genre_app.pyw"
