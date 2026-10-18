@echo off
rem ─────────────────────────────────────────────────────────────────────
rem  MDX Dictionary API + GoldenDict watcher: background start
rem
rem  Invoked by the Startup-folder shortcut (task "MDX-GoldenDict-API"
rem  style) via script/autostart.vbs at user logon, hidden window.
rem  Uses the .venv python directly (not "uv run") so boot never needs
rem  the network to sync dependencies; run "uv sync" manually on the dev
rem  machine after changing dependencies.
rem
rem  Log: data\server.log (appended)
rem  Foreground debug run:
rem    uv run python -X utf8 -m uvicorn app.main:app --host 0.0.0.0 --port 4080
rem
rem  NOTE: keep this file ASCII-only (cmd reads batch in the ANSI
rem  codepage; non-ASCII comments only display garbled but stay valid).
rem ─────────────────────────────────────────────────────────────────────
cd /d D:\myworkspace\mdx
if not exist data mkdir data
.venv\Scripts\python.exe -X utf8 -m uvicorn app.main:app --host 0.0.0.0 --port 4080 >> data\server.log 2>&1
