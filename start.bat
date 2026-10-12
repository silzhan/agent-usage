@echo off
rem Start Qoder usage dashboard at http://127.0.0.1:8765
start "" http://127.0.0.1:8765/
python "%~dp0server.py" %1
