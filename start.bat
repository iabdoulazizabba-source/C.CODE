@echo off
REM == Start the Peschaud attendance app ==
cd /d "C:\Users\Dell\OneDrive\Desktop\C.CODE"
REM Auto-find the F18 by its serial number, on any IP in this subnet.
set DEVICE_SERIAL=CQQC243161174
set DEVICE_SUBNET=192.168.10
REM Listen on all interfaces so other PCs on the network can reach it.
set HOST=0.0.0.0
set PORT=5000
echo Starting attendance server...
echo On THIS PC:            http://localhost:5000
echo From other computers:  http://%COMPUTERNAME%:5000  (or http://this-PC-IP:5000)
echo Keep THIS window open. Close it (or press Ctrl+C) to stop the app.
echo.
".venv\Scripts\python.exe" app.py
echo.
echo Server stopped.
pause
