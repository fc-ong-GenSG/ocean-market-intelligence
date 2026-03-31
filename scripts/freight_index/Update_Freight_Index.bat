@echo off
setlocal

cd /d C:\Data\ocean_market_intelligence\scripts\freight_index

"C:\Data\Temasek Polytechnic_Generation SG\anaconda3\python.exe" run_all_freight_indices.py

echo.
echo Update process finished.
pause