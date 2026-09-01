@echo off
REM Long-lived: owns the serial port, self-slices at 11 PM, flashes on request.
cd /d "%~dp0"
title Axum capture supervisor
python capture_supervisor.py
pause
