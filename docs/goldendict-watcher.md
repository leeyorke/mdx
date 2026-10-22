# GoldenDict 监听自动入生词本

`app/goldendict.py` 监听 GoldenDict 的三个文本文件，把出现的新词写入生词本。

## 运行方式

| 方式 | 启动 | 写入路径 |
|------|------|----------|
| **随主 app 嵌入（默认）** | 启动 uvicorn 即自动开始；主应用退出（Ctrl+C / kill）监听立即结束 | 进程内调用 `app.vocabulary.add_word`（`POST /api/vocabulary` 端点内部使用的同一个函数） |
| 独立进程 | `python -m app.goldendict`（`--once` 只同步一次，`--dry-run` 预览） | HTTP `POST /api/vocabulary` |

嵌入模式通过 FastAPI `lifespan` 钩子实现（`app/main.py`）：

- 启动时创建守护线程 `goldendict-watcher` 跑 `run_forever(stop_event=...)`；
- 关闭时置位 stop_event 并 join（10 秒超时），免打扰时段的长睡眠也会被立即唤醒；
- `MDX_WATCH_AUTOSTART=0` 可关闭自动监听；独立 CLI 不受该开关影响；
- 启动/停止任何异常只记日志，**不影响 API 服务**；
- 与独立进程共用同一把文件锁：已有独立监听在跑时，嵌入实例打 warning 后让位，不双写；
  `uvicorn --workers N` 时同理，只有一个实例真正写入。

## 开机自启（本机已配置）

用户登录时自动启动，隐藏后台运行：

| 文件 | 作用 |
|------|------|
| `install_scheduled_task.cmd` | 安装：创建计划任务 `MDX-GoldenDict-API` 并启动 4080 服务；非管理员时自动 RunAs 重跑，成功退出码 `0` |
| `stop_scheduled_task.cmd` | 卸载：停止 4080 服务并删除计划任务；成功退出码 `0`，跑完 netstat 无 LISTENING |
| `script/mdx-task.xml` | 任务定义：登录触发，`wscript → autostart.vbs`，不限时长、电池模式照常 |
| `script/autostart.vbs` | 隐藏窗口启动器 |
| `script/run_server.bat` | cd 到项目根，用 `.venv\Scripts\python.exe` 起 uvicorn（4080），日志追加到 `data\server.log` |
| `script/install_autostart.ps1` | 备用：无管理员权限时改用「启动文件夹」快捷方式 |
| `script/stop_server.ps1` | 只停服务、不删任务（按 4080 端口结束进程） |

两个 .cmd 的共同设计（都是踩坑换来的）：

- 非管理员 → `cmd.exe /c <自身> /elevated` + `Start-Process -Verb RunAs -Wait` 提权重跑
  （直接对 .cmd 用 RunAs 不可靠）；**任何失败都 pause 不闪退**，并写日志
  `data/install_task.log`；父窗口不信任子进程，自己重新验证最终状态（任务在不在、
  端口通不通）再决定退出码
- 提权彻底失败时，安装脚本提供无管理员备选（启动文件夹）；`schtasks` 的
  SUCCESS/ERROR 输出全部进日志，方便事后复盘
- `.bat` / `.cmd` 必须保持**纯 ASCII + CRLF**：cmd 按 OEM 代码页逐字节解析，
  非 ASCII 字节混进 echo 行会吃掉命令名字符、LF 行尾会破坏括号块解析；
  休眠用 `ping -n N 127.0.0.1`（`timeout` 在 stdin 重定向时报
  "Input redirection is not supported"）
- 杀服务进程不会产生服务端 TIME_WAIT（连接直接被 RST，父进程自行退出）；
  `netstat` 里残留的 4080 TIME_WAIT 行是**客户端**自己的（PID 0），与服务无关，
  最多 4 分钟自动消失——停止脚本会区分"仍在监听"（失败）和"客户端残留"（正常），
  也可选等待残留自然消失
- 注册计划任务需要**提权**（非提权进程会被拒 Access is denied，本机任务库经过加固）

## 监听的文件与格式

| 文件 | 格式 | 示例 | 解析方式 |
|------|------|------|----------|
| `favorites` | XML，`<root>` 下每行一个 `<headword>` | ` <headword>artificial</headword>` | 正则提取 `<headword>` 内容 |
| `history` | 每行 `<查词次数> <条目>` | `0 credential`、`12 claim` | 去掉行首数字 |
| `history.tmp` | 每行 `+ <条目>`（新增）/ `- <条目>`（删除） | `+ simplify` | 只取 `+` 行 |

提取"干净单词本身"的规则（`extract_clean_word`）：

- 去掉首尾空白、包裹引号和句尾标点（`"claim"`、`claim.` → `claim`）；
- 只接受纯英文单词：字母开头，可含撇号 / 连字符（`don't`、`by-product`、`cyber-`），
  长度 2–45，统一转小写；
- 以下条目自动跳过并计数：短语（`step by step`）、URL、文件路径、
  中文句子、命令行等（history 中约 42 条、history.tmp 中约 3 条）。

## 行为

1. **首次全量导入**：没有 `data/goldendict_state.json` 时，先把三个文件中已存在的
   单词全部导入生词本（已实际执行：344 个新词，10 个生词本已有单词跳过）。
2. **增量监听**：之后每 1 小时（`--interval` / `MDX_WATCH_INTERVAL` 可调）重新解析
   三个文件，只处理新出现的单词。GoldenDict 重写 / 轮转文件不影响正确性
   （每次全量解析 + 差集）。
3. **不重复**：单词进入生词本前经过两道过滤——
   - 生词本（`user.db`）中已存在（大小写不敏感）；
   - 本地 state 的 `processed` 集合中已处理过。

   任一处命中都不会再次写入；即使删除 state 文件，生词本去重仍然兜底。
4. **失败重试**：API 不可用时该轮所有词都不会被标记为已处理，下一轮自动重试；
   单词逐个写入，中途失败不影响已成功的部分。
5. **单实例**：通过 `data/goldendict_watcher.lock` 文件锁保证同时只有一个监听进程，
   防止并发重复写入。
6. **免打扰时段**：默认 00:00–08:00（按本机时间）暂停轮询，08:00 自动恢复；
   期间日志会提示预计恢复时间。`--once` 是手动同步，不受免打扰限制。

## 写入内容

- `word`：提取出的干净单词（小写）；
- `translation`：自动查询词典库（`oaldpe.db`）生成的多行释义，样式与既有生词一致：
  ```
  competitor
  英 /kəmˈpetɪtə(r)/ 美 /kəmˈpetɪtər/
  1. noun. 参赛者；竞赛者
  ```

  词典未收录时（如拼写错误的 `contradicte`）写入 `（暂无释义）`；
- `note`：来源标注，如 `来源：GoldenDict history(查词记录) 自动导入`。

## 配置

命令行参数：`--dir`（GoldenDict 目录）、`--api-base`、`--state`、`--interval`、
`--once`、`--dry-run`、`-v`。

环境变量：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `GOLDENDICT_DIR` | `C:\Users\leeyorke\AppData\Roaming\GoldenDict` | GoldenDict 配置目录 |
| `MDX_API_BASE_URL` | `http://127.0.0.1:4080` | 生词本 API 地址（仅独立进程模式使用） |
| `MDX_WATCH_INTERVAL` | `3600` | 轮询间隔（秒，默认 1 小时） |
| `MDX_WATCH_QUIET_HOURS` | `0-8` | 免打扰时段（小时，`起始-结束`，支持跨午夜如 `22-6`；置空关闭） |
| `MDX_WATCH_AUTOSTART` | `1` | 是否随主 app 自动启动监听（`0` 关闭） |
| `MDX_WATCH_STATE_FILE` | `data/goldendict_state.json` | state 文件路径（锁文件放在同目录） |
| `MDX_WATCH_HTTP_TIMEOUT` | `10` | API 请求超时（秒，仅独立进程模式） |

state 文件（`data/goldendict_state.json`）记录 `initialized`、`processed`（已处理
单词集合）、`total_added`、`added_by_source`、`last_sync_at`，损坏时安全降级
（重建空状态，靠生词本去重兜底）。

## 测试

```bash
uv run python -X utf8 script/test_goldendict.py
```

77 项检查：三种格式解析、干净单词提取、翻译格式化、state 读写、单实例锁、免打扰时段
（含跨午夜配置与持续监听暂停逻辑）、两种写入方式（HTTP / 进程内）、随主 app 启停
（lifespan 自动导入、daemon 线程、停止后线程退出与锁释放、开关关闭、锁让位），
以及基于真实 uvicorn 服务的端到端场景（首次导入 / 再次运行不重复 / 新词增量 /
state 丢失不重复 / API 故障恢复补写）。
