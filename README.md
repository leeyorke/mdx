## mdx

## 启动方式

```bash
cd D:\myworkspace\mdx
# 一条命令同时启动 API 和 GoldenDict 监听（监听随主应用启停）
# --host 0.0.0.0 允许局域网访问；只本机使用可改成 127.0.0.1
uv run python -X utf8 -m uvicorn app.main:app --host 0.0.0.0 --port 4080
```

## 开机自启

已配置为**用户登录时自动启动**（隐藏后台运行，无需每次手动开）：

- 启动链：「启动」文件夹的 `MDX-GoldenDict-API.lnk` → `script/autostart.vbs` →
  `script/run_server.bat`（用 `.venv` 的 python 直接起 uvicorn，日志写 `data/server.log`）
- 安装/重建：`pwsh script/install_autostart.ps1`
- 停止服务：`pwsh script/stop_server.ps1`（只结束监听 4080 的进程）
- 卸载：删除「启动」文件夹（`shell:startup`）里的 `MDX-GoldenDict-API.lnk`
- 说明：自启用 `.venv` python 而非 `uv run`，避免开机时联网同步依赖导致启动失败；
  改了依赖后在开发机 `uv sync` 即可。计划任务方案需要管理员权限，故用启动文件夹。

## GoldenDict 查词自动入生词本

监听**随主应用自动启动/停止**：启动 uvicorn 即开始监听，主应用退出（Ctrl+C / kill）监听立即结束，无需第二个命令。监听 GoldenDict 的 `favorites`（收藏）、`history`（查词痕迹）、`history.tmp`（待写入查词记录）三个文件，出现新词时自动写入生词本：

- **首次运行**会把三个文件中已存在的单词全部导入；
- 之后默认每 1 小时轮询一次，只处理新增的单词，**生词本不会重复**（以 `user.db` 已有单词 +
  本地 `data/goldendict_state.json` 已处理记录为准，大小写不敏感）；
- 00:00–08:00 为免打扰时段，暂停轮询（`MDX_WATCH_QUIET_HOURS` 可调整，置空关闭）；
- 嵌入模式写入走进程内数据层（`POST /api/vocabulary` 端点内部使用的同一个 `add_word`），
  与 API 行为一致，且不受端口影响。

不需要自动监听：设 `MDX_WATCH_AUTOSTART=0`。也可独立运行监听（首次导入 / 调试用）：

```bash
# 只同步一次（首次导入；手动同步不受免打扰限制）
uv run python -X utf8 -m app.goldendict --once

# 预览将要导入的单词，不实际写入
uv run python -X utf8 -m app.goldendict --once --dry-run

# 独立进程持续监听（通过 HTTP API 写入）
uv run python -X utf8 -m app.goldendict
```

详见 [docs/goldendict-watcher.md](docs/goldendict-watcher.md)。
测试：`uv run python -X utf8 script/test_goldendict.py`

## 制作sqliteDB

1. 安装 [mdict-utils](https://github.com/liuyug/mdict-utils)： `pip install git+https://github.com/liuyug/mdict-utils.git`
2. 转换为数据库： `mdict -x .\oxford.mdx --exdb`