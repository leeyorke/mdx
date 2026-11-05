@echo off
rem MDX Dictionary API + GoldenDict watcher - background start
rem Called by script/autostart.vbs at user logon (hidden window).
rem Uses the .venv python directly (NOT "uv run") so boot never needs
rem the network to sync dependencies. Run "uv sync" on this machine
rem after changing dependencies.
rem Log (appended): data\server.log
rem Foreground debug run:
rem   uv run python -X utf8 run.py

cd /d D:\myworkspace\mdx
if not exist data mkdir data
rem run.py owns host/port/db defaults (0.0.0.0:4080, user.db); pass no args here
rem so this stays the single source of truth for startup config.
.venv\Scripts\python.exe -X utf8 run.py >> data\server.log 2>&1