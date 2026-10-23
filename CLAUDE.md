# CLAUDE.md

本文件为 Claude Code (claude.ai/code) 在处理此存储库中的代码时提供指导。

## 项目定位

**mdx** 是一个跑在 Windows 本机上的个人英语学习后端（FastAPI 单进程，端口 4080），
把「词典查询 + 生词本 + 查词自动收录 + 艾宾浩斯复习提醒 + 欧路生词本同步」五个功能
收进同一个服务。没有前端框架，只有一个自带的单文件复习页面；主要客户端是浏览器
扩展 vibe-reading（只保留生词本与闪卡）和 GoldenDict 桌面词典。

技术栈：Python 3.14 + FastAPI/uvicorn + SQLite + BeautifulSoup +
httpx + windows-toasts；依赖用 uv 管理（`uv.lock`），虚拟环境在 `.venv/`。

## 常用命令

```bash
# 启动服务（API + GoldenDict 监听 + 复习调度器都随这一个进程起停）
uv run python -X utf8 -m uvicorn app.main:app --host 0.0.0.0 --port 4080

# 安装/同步依赖（改了 pyproject.toml 之后）
uv sync

# 测试（三个纯脚本，各自独立运行，无 pytest）
uv run python -X utf8 script/test_goldendict.py
uv run python -X utf8 script/test_review.py
uv run python -X utf8 script/test_eudic.py

# GoldenDict 独立同步（首次导入 / 调试用，不受免打扰限制）
uv run python -X utf8 -m app.goldendict --once
uv run python -X utf8 -m app.goldendict --once --dry-run   # 只预览不写入

# 停服务（不删计划任务）
pwsh script/stop_server.ps1

# 开机自启安装 / 卸载（双击运行，非管理员自动提权重跑）
scheduled_task_install.cmd
scheduled_task_stop.cmd

# 日志
data/server.log        # 服务运行日志
data/install_task.log  # 自启安装/卸载日志
```

接口交互文档是 FastAPI 自动生成的 `http://127.0.0.1:4080/docs`（Swagger UI）；
各模块的设计说明见 `docs/` 下四份文档（api.md / goldendict-watcher.md /
review.md / eudic.md），**改行为前先读对应文档，文档与代码同等权威**。

## 数据层：SQLite 库（全部被 .gitignore，不入库）

根目录现存 4 个 `.db`，但**业务代码只碰前两个**：

| 库文件 | 内容 | 读写模块 |
|--------|------|----------|
| `oaldpe.db` | 词典本体：`mdx(entry, paraphrase)`，paraphrase 是 OALD 词条的 HTML | `app/dictionary.py` |
| `user.db` | `vocabulary_words`（生词本）、`review_schedule` / `review_stats` / `review_settings`（复习）、`eudic_settings`（欧路配置） | `app/vocabulary.py`、`app/review/store.py`、`app/eudic.py` |
| `ngsl.db` | NGSL 词频表（`ngsl_words`），暂无业务代码引用 | — |
| `oxford.db` | 另一版牛津词典（配 `oxford.mdx`），应用代码未引用，只有 README 里的转换命令提到 | — |

由 mdx 词典文件生成词典库：装 `mdict-utils` 后 `mdict -x .\oaldpe.mdx --exdb`，
或跑 `script/import_mdx.py`（依赖 `readmdict`）。`script/import_json.py` 是当初把
浏览器扩展的 `VibeReadingDB.json` 搬进 `user.db` 的一次性迁移脚本。

## 架构

### 进程形态：一个 lifespan 拉起两个后台任务

`app/main.py` 通过 FastAPI `lifespan` 同时管理：

1. **GoldenDict 监听**（`app/goldendict.py`）——守护线程 `goldendict-watcher`，
   轮询 GoldenDict 的 `favorites` / `history` / `history.tmp` 三个文本文件，
   把新出现的干净英文单词写进生词本（翻译自动查 `oaldpe.db` 生成）。
2. **复习调度器**（`app/review/scheduler.py`）——asyncio 后台任务，按
   `get_next_wakeup_at()` 精确计算下次唤醒时刻（免打扰会把唤醒推后），
   到点把当天批次合成**一条** Windows 系统通知（一天最多提醒一次）。

两者启动失败都只记日志、不影响 API 服务。

### 模块划分与职责边界

```
app/
  main.py          # FastAPI 入口：路由 + lifespan + Pydantic schemas（大量 OpenAPI 注解）
  dictionary.py    # 词典查询：SQLite 查 entry → 解析词条 HTML → 结构化 JSON
  vocabulary.py    # 生词本数据层：vocabulary_words CRUD + 欧路列迁移 + 合并 upsert
  goldendict.py    # 文件监听 + 单词提取 + 去重 + 翻译格式化 + 单实例锁
  eudic.py         # 欧路 OpenAPI 客户端（TLS/重试/代理/清洗/同步/写回）+ 配置存储
  eudic_api.py     # 欧路 REST 路由
  review/
    constants.py   # 阶梯间隔、配额、退避、通知文案等全部可调参数
    schedule.py    # 纯函数调度算法（不碰 DB、不发通知，可单测）
    store.py       # 复习三张表的落库（含 last_rated_at 老库补列迁移）
    service.py     # 业务编排：同步队列 / 跑一轮调度（每日一次批量提醒）/ 会话 / 评分 / 组卡片
    scheduler.py   # 常驻循环（asyncio task）
    notifier.py    # windows-toasts 通知（每日一次批量），launch 协议激活地址
    example.py     # 可选 LLM 造句，失败逐级降级（按需生成，不阻塞调度）
    api.py         # /api/review/* 路由 + /review 页面
    static/review.html  # 单文件会话式复习+配置页
```

### 关键设计决策（改代码前必须知道）

- **生词本是唯一数据源，其他功能都向它对齐**。欧路词以 `source='eudic'` 落进
  同一张 `vocabulary_words`，分页/搜索/排序/复习队列天然统一；复习队列只收
  `last_reviewed_at IS NOT NULL`（复习过一次）的词。
- **嵌入监听复用进程内数据层**：`start_embedded_watcher` 注入
  `LocalVocabularyWriter` 直接调 `app.vocabulary.add_word`（即
  `POST /api/vocabulary` 内部同一个函数），不走 HTTP；独立进程模式才走
  `HttpVocabularyWriter`。`data/goldendict_watcher.lock` 文件锁保证同时只有
  一个监听实例（嵌入与独立进程共用，多 worker 也不会双写）。
- **欧路同步「本地优先、失败不回滚」**：本地写成功后推欧路失败只通过响应的
  `eudic_sync` 字段报告，不回滚本地（回滚会让用户以为收藏没成功）。
  `upsert_eudic_words` 的对齐顺序是 `source_id` 命中 → 认领同名本地词 → 新建；
  已有词只补空字段，星级用 `review_count` 裁决（用户没评过分才采用欧路星级）。
- **欧路接口的两个坑**（代码里有详细注释）：OpenSSL 的 TLS 1.3 ClientHello 会被
  链路上中间设备掐断，所以 `_ssl_context()` 默认把上限锁在 TLS 1.2
  （`EUDIC_TLS_MAX=1.3` 可放开）；httpx 的 `trust_env=True` 解析带方括号 IPv6 的
  NO_PROXY 会抛异常，所以代理自己从环境变量取、`trust_env` 保持 `False`。
  重试只重试传输层错误（4 次指数退避），HTTP 4xx/5xx 立刻抛。
- **复习调度是全局闸门**：免打扰时段内不发通知也**不改到期时间**（唤醒点由
  `get_next_wakeup_at` 推到窗口结束——免打扰只挡通知，不挡手动复习）；每日配额
  触发时把当天剩余到期词一起推迟到今天 23:59:59，不是逐条放行；**一天最多提醒
  一次**（一个批次一条通知，超额词进会话）；推送失败按指数退避且不占用阶梯位置。
- **安全约束**：`update_word` 只允许改 `star`/`translation`/`note`；`source` 等
  欧路字段不暴露给客户端 PATCH（否则能把本地词伪造成欧路词、借删除接口删掉
  欧路收藏）；`list_words` 的排序字段/方向走白名单防注入。

### 核心流程

1. **查词**：`GET /api/word/{word}` → `query_word()` 查 `oaldpe.db` →
   遇 `@@@LINK=` 重定向再查一次 → `parse_dictionary_entry()` 用 BeautifulSoup
   提取 headword/pos/phonetics/senses（含中文释义、例句、主题 CEFR 标签）。
2. **查词自动入生词本**：GoldenDict 写文件 → watcher 轮询解析 →
   `extract_clean_word()` 过滤（只留纯英文单词，长度 2–45）→ 生词本 + state
   双重去重（大小写不敏感）→ `format_translation()` 生成多行翻译 → 写入
   `user.db`。00:00–08:00 免打扰暂停轮询，`--once` 手动同步不受限。
3. **复习提醒**：扩展闪卡评分调 `POST /api/vocabulary/{id}/review` → 后端
   `sync_review_queue()` 把复习过的词收进 `review_schedule`（阶梯 0、立即到期）
   → 调度器到点把当天批次合成**一条** Windows 通知（标题="今天有 N 个单词要复习"，
   点击用默认浏览器打开 `/review`）→ 页面直接进入复习会话，一张张复习完
   （「记得跨两级 / 忘了退一级」）；「立即复习」随时可开一场不受配额限制的手动
   会话，例句在会话开始后按队列顺序**预加载**（页面保持最多 3 个请求在飞，
`/api/review/example` 在线程池里跑，不占事件循环），切到某个词时例句和
   固定搭配通常已经在了。
4. **欧路同步**：`POST /api/eudic/sync` → 翻页拉全量（page≤50、page_size≤100，
   单本最多 5000 词）→ `clean_text()` 洗掉 HTML → `upsert_eudic_words()` 合并；
   本地新增/改星级/删除按需写回欧路。

## 项目约定

- **所有 Python 命令带 `-X utf8`**（Windows 默认代码页会毁掉中文输出），测试和
  启动脚本都是这么跑的。
- 注释、文档、日志一律中文；文档在 `docs/`，改行为要同步更新对应文档。
- `*.cmd` / `*.bat` 必须保持**纯 ASCII + CRLF**：cmd 按 OEM 代码页逐字节解析，
  非 ASCII 字节会吃掉命令名；休眠用 `ping -n N 127.0.0.1`（`timeout` 在 stdin
  重定向时报错）。`.vbs` 同理保持 ASCII。
- 自启链路：计划任务 `MDX-GoldenDict-API`（登录触发）→ `wscript script/autostart.vbs`
  → `script/run_server.bat` → `.venv\Scripts\python.exe -X utf8 -m uvicorn ...`。
  开机用 `.venv` 而不是 `uv run`，避免启动时联网同步依赖。
- **改了 `EUDIT_API_KEY` 等环境变量必须重启后端**（`script/stop_server.ps1` 后
  重启），进程启动时已拷走环境块。

### 环境变量一览

| 变量 | 默认 | 用途 |
|------|------|------|
| `GOLDENDICT_DIR` | `C:\Users\leeyorke\AppData\Roaming\GoldenDict` | GoldenDict 配置目录 |
| `MDX_WATCH_INTERVAL` | `3600` | 轮询间隔（秒） |
| `MDX_WATCH_QUIET_HOURS` | `0-8` | 免打扰时段（支持跨午夜 `22-6`，置空关闭） |
| `MDX_WATCH_AUTOSTART` | `1` | 是否随主 app 自动监听（`0` 关闭） |
| `MDX_WATCH_STATE_FILE` | `data/goldendict_state.json` | state 文件（锁文件同目录） |
| `MDX_API_BASE_URL` | `http://127.0.0.1:4080` | 独立监听模式的 API 地址 |
| `EUDIT_API_KEY`（别名 `EUDIC_API_KEY`） | — | 欧路 NIS token，优先于设置页备用 token |
| `EUDIC_PROXY` / `HTTP(S)_PROXY` | — | 欧路请求代理 |
| `EUDIC_TLS_MAX` | `1.2` | 欧路请求 TLS 上限 |
| `MDX_REVIEW_TOAST_AUMID` | `Microsoft.WindowsPowerShell` | 通知归属 AUMID |
| `MDX_LLM_PROXY` | — | 例句 LLM 请求的代理，优先于 `HTTPS_PROXY` 等标准变量 |
