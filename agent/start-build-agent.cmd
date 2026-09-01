@echo off
REM Long-lived: watches GitHub for finished nightly builds.
cd /d "%~dp0"
title Axum build agent
python build_agent.py
pause
