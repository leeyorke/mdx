# 为「检验功能」起一个隔离的测试实例，走独立的测试库 user_test.db。
#
# 现在这只是 run.py --test 的薄封装：种子复制（user.db -> user_test.db）、
# 关掉测试库复习提醒、MDX_USER_DB 指向测试库，这些逻辑都搬进 run.py 了，
# 这里只负责转发参数，避免两处各维护一份。
#
# 为什么需要隔离：可写库只有一个 user.db（生词本 + 复习 + 写作评卷 + 欧路配置都在里面）。
# 若测试实例也连它，测试时建的检验任务/笔记会写进真实库，污染生词本与复习队列。
# oaldpe.db / oxford.db / ngsl.db 都是只读词典，跟生词本无关——测试时共用即可，不复制、不占磁盘。
#
# 用法：
#   pwsh script/run_test_server.ps1              # 起在 4081，走 user_test.db
#   pwsh script/run_test_server.ps1 -Port 4082
#   pwsh script/run_test_server.ps1 -Fresh       # 重新从 user.db 复制种子（丢弃旧测试数据）
#
# 等价的手动命令（不经过本脚本）：
#   uv run python -X utf8 run.py --test [--port 4082] [--fresh]
#
# 停：关掉那个 pwsh 窗口，或 Stop-Process 掉 uvicorn。**不碰 4080 的计划任务。**

param(
    [int]$Port,
    [switch]$Fresh
)

$ErrorActionPreference = "Stop"
$RepoRoot = Resolve-Path "$PSScriptRoot\.."

# 组装 run.py --test 的参数：-Port / -Fresh 只在用户显式给了才转发，
# 没给就交给 run.py 用它自己的默认值（测试端口 4081）。
$pyArgs = @("-X", "utf8", "run.py", "--test")
if ($PSBoundParameters.ContainsKey("Port")) { $pyArgs += @("--port", "$Port") }
if ($Fresh) { $pyArgs += "--fresh" }

Write-Host "原仓库：$RepoRoot"
Write-Host "启动：.venv python $($pyArgs -join ' ')"

# 走 .venv python（而非 uv run），与 run_server.bat 一致——不联网同步依赖也能起。
& (Join-Path $RepoRoot ".venv\Scripts\python.exe") @pyArgs