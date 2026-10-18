# ─────────────────────────────────────────────────────────────────────
#  安装“登录时自动启动”：在「启动」文件夹创建指向 script/autostart.vbs
#  的快捷方式（无需管理员权限）。
#
#  用法： pwsh script/install_autostart.ps1
#  卸载： 删除启动文件夹中的 MDX-GoldenDict-API.lnk
#         （路径：shell:startup，即 %APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup）
# ─────────────────────────────────────────────────────────────────────
$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$vbs = Join-Path $projectRoot "script\autostart.vbs"
if (-not (Test-Path $vbs)) { throw "找不到 $vbs" }

$startup = [Environment]::GetFolderPath("Startup")
if (-not (Test-Path $startup)) { throw "找不到启动文件夹: $startup" }
$lnkPath = Join-Path $startup "MDX-GoldenDict-API.lnk"

$sh = New-Object -ComObject WScript.Shell
$lnk = $sh.CreateShortcut($lnkPath)
$lnk.TargetPath = Join-Path $env:SystemRoot "System32\wscript.exe"
$lnk.Arguments = "`"$vbs`""
$lnk.WorkingDirectory = $projectRoot
$lnk.Description = "MDX 词典 API(4080) + GoldenDict 监听，登录时自动启动"
$lnk.Save()

Write-Output "已创建启动快捷方式: $lnkPath"
Write-Output "目标: $($lnk.TargetPath) $($lnk.Arguments)"
Write-Output "下次登录自动生效；想现在启动: wscript `"$vbs`""
