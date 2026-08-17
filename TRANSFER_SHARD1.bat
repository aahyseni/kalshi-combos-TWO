@echo off
rem One-click SHARD-1 wallet funding (2026-08-16 shard discovery): moves
rem ~57.9%% of cash from shard-0 to shard-1 (the wallet 58%% of RFQ flow
rem clears on; it held $8.79). Intra-account, asynchronous, REVERSIBLE.
rem Prints the live balances, the amount, and polls until it lands.
cd /d %~dp0
.venv\Scripts\python.exe "C:\Users\aahys\AppData\Local\Temp\claude\C--Users-aahys\179628aa-9b8c-46a4-9de5-0695cb163ee2\scratchpad\shard_transfer.py"
pause
