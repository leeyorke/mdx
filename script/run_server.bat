@echo off
rem MDX Dictionary API + GoldenDict watcher - background start
rem Called by script/autostart.vbs at user logon (hidden window).
rem Uses the .venv python directly (NOT "uv run") so boot never needs
rem the network to sync dependencies. Run "uv sync" on this machine
rem after changing dependencies.
rem Log (appended): data\server.log
rem Foreground debug run:
rem   uv run python -X utf8 -m uvicorn app.main:app --host 0.0.0.0 --port 4080

cd /d D:\myworkspace\mdx
if not exist data mkdir data
.venv\Scripts\python.exe -X utf8 -m uvicorn app.main:app --host 0.0.0.0 --port 4080 >> data\server.log 2>&1