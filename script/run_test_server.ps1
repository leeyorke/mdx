# 为「检验功能」起一个隔离的测试实例（默认端口 4081），走独立的测试库 user_test.db。
#
# 为什么需要隔离：可写库只有一个 user.db（生词本 + 复习 + 写作评卷 + 欧路配置都在里面）。
# 若 4081 也连它，测试时建的检验任务/笔记会写进真实库，污染生词本与复习队列。
# oaldpe.db / oxford.db / ngsl.db 都是只读词典，跟生词本无关——测试时共用即可，不复制、不占磁盘。
#
# 做法：app/vocabulary.py 的 DB_FILE 支持 ``MDX_USER_DB`` 环境变量覆盖（review/store.py、
# eudic.py、goldendict.py 都从它 import DB_FILE，覆盖一处即全部写路径一起切换）。
# 这里把 MDX_USER_DB 指到 user_test.db，再起服务。**一份仓库都不用复制。**
#
# user_test.db 首次不存在时，从真实 user.db 复制一份种子（这样有复习过的词可检验），
# 并把它的 review_settings.enabled 置 False——别让测试实例也弹 Windows 通知、跟 4080
# 抢同一批到期词。
#
# 用法：
#   pwsh script/run_test_server.ps1              # 起在 4081，走 user_test.db
#   pwsh script/run_test_server.ps1 -Port 4082
#   pwsh script/run_test_server.ps1 -Fresh       # 重新从 user.db 复制种子（丢弃旧测试数据）
#
# 停：关掉那个 pwsh 窗口，或 Stop-Process 掉 uvicorn。**不碰 4080 的计划任务。**

param(
    [int]$Port = 4081,
    [switch]$Fresh
)

$ErrorActionPreference = "Stop"
$RepoRoot = Resolve-Path "$PSScriptRoot\.."
$TestDb = Join-Path $RepoRoot "user_test.db"
$RealDb = Join-Path $RepoRoot "user.db"

Write-Host "原仓库：$RepoRoot"
Write-Host "测试库：$TestDb"
Write-Host "端口：$Port"

# ── 1. 测试库不存在（或 -Fresh）时，从真实 user.db 复制一份种子 ─────────────
if ($Fresh -or -not (Test-Path $TestDb)) {
    if (-not (Test-Path $RealDb)) { throw "真实 user.db 不存在：$RealDb" }
    Copy-Item $RealDb $TestDb -Force
    Write-Host "已从 user.db 复制测试种子 → $TestDb"
} else {
    Write-Host "复用已有测试库 $TestDb（-Fresh 可重新复制种子）"
}

# ── 2. 测试库的 review_settings 关掉复习提醒：别让测试实例也弹通知/跑调度 ──────
# 配置存在 review_settings 表 key='config' 的 JSON 里。关掉 enabled，调度器空转，
# 不会去发 Windows 通知，也不会跟 4080 抢同一批到期词。
& (Join-Path $RepoRoot ".venv\Scripts\python.exe") -X utf8 -c @"
import sqlite3, json
db = r'$TestDb'
conn = sqlite3.connect(db)
row = conn.execute("SELECT value FROM review_settings WHERE key='config'").fetchone()
cfg = json.loads(row[0]) if row and row[0] else {}
cfg['enabled'] = False
conn.execute(
    "INSERT INTO review_settings (key, value) VALUES ('config', ?) "
    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
    (json.dumps(cfg, ensure_ascii=False),),
)
conn.commit()
conn.close()
print("测试库 review_settings.enabled 已置 False")
"@
if ($LASTEXITCODE -ne 0) { throw "关闭测试库复习提醒失败" }

Write-Host ""
Write-Host "===================================================="
Write-Host " 检验功能测试实例（隔离库）"
Write-Host "   页面：  http://127.0.0.1:$Port/verify"
Write-Host "   文档：  http://127.0.0.1:$Port/docs"
Write-Host "   复习页： http://127.0.0.1:$Port/review"
Write-Host "   库：    $TestDb （独立，不碰真实 user.db）"
Write-Host "   词典：  共用只读 oaldpe.db，不复制"
Write-Host " 关掉这个窗口或按 Ctrl+C 即停止，不影响 4080 计划任务"
Write-Host "===================================================="
Write-Host ""

# ── 3. 原目录起服务，只把 MDX_USER_DB 指向测试库 ────────────────────────
# 不换工作目录、不复制仓库：app 全从原仓库 import，词典走只读 oaldpe.db，
# 唯一可写库 user.db 被 MDX_USER_DB 重定向到 user_test.db。
$env:MDX_USER_DB = $TestDb
& (Join-Path $RepoRoot ".venv\Scripts\python.exe") -X utf8 -m uvicorn `
    app.main:app --host 127.0.0.1 --port $Port
