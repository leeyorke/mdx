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

- 安装：双击根目录 `install_scheduled_task.cmd`——创建计划任务 `MDX-GoldenDict-API`
  并把服务拉起来；**非管理员运行时自动提权重跑**，成功退出码 `0`，全程日志在
  `data/install_task.log`（提权失败时脚本会停下显示原因，不会闪退）
- 停止/卸载：双击根目录 `stop_scheduled_task.cmd`——停止 4080 服务并删除计划任务，
  成功退出码 `0`；跑完 `netstat -ano | findstr 4080` 无 LISTENING 输出
- 任务定义在 `script/mdx-task.xml`（登录时运行 wscript → `script/autostart.vbs` →
  `script/run_server.bat`，隐藏窗口、不限运行时长、电池模式照常启动）
- 备用方式（没有管理员权限时）：`pwsh script/install_autostart.ps1`（启动文件夹）
- 停止服务（不删任务）：`pwsh script/stop_server.ps1`（只结束监听 4080 的进程）
- 日志：`data/server.log`
- 说明：自启用 `.venv` python 而非 `uv run`，避免开机时联网同步依赖导致启动失败；
  改了依赖后在开发机 `uv sync` 即可
- 注意：`*.bat` / `*.cmd` 保持**纯 ASCII + CRLF**——cmd 按 OEM 代码页逐字节解析，
  非 ASCII 字节混进 echo 行会把命令名吃掉；脚本内用 `ping` 而不是 `timeout` 休眠
  （`timeout` 在 stdin 重定向时报错）

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

## 复习提醒（艾宾浩斯主动复习）

复习提醒整套逻辑从浏览器扩展 vibe-reading 搬到了后端：调度、状态、Windows
系统通知、复习卡片与配置页面都由本服务提供（扩展侧只保留生词本与闪卡）。

- **Windows 通知**：到点弹出系统通知，**点击直接用默认浏览器打开复习页面**
  （`http://127.0.0.1:4080/review`），无需浏览器扩展在场；
- **复习页面**：`/review` 一个页面搞定复习（记得 / 忘了、词典释义、朗读）和配置
  （开关、每日配额、免打扰时段、LLM 例句等）；
- **阶梯**：5 分钟 / 30 分钟 / 12 小时 / 1 天 / 2 天 / 4 天 / 7 天 / 15 天；
  「记得」跨两级、「忘了」退一级；
- **配额**：每天最多 8 条、每词每天最多 3 条，默认 23:00–08:00 免打扰；
- **队列来源**：只有复习过（闪卡评过分）的生词才进队列。

```bash
# 打开复习页面（通知点击后也是这个地址）
start http://127.0.0.1:4080/review
```

接口与实现细节见 [docs/review.md](docs/review.md)。
测试：`uv run python -X utf8 script/test_review.py`

## 制作sqliteDB

1. 安装 [mdict-utils](https://github.com/liuyug/mdict-utils)： `pip install git+https://github.com/liuyug/mdict-utils.git`
2. 转换为数据库： `mdict -x .\oxford.mdx --exdb`