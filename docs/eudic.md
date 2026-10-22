# 欧路（Eudic）生词本接入

把欧路词典（`api.frdic.com`）的生词本并进本地生词表，让浏览器扩展
vibe-reading 的**生词本**和**闪卡**能同时用两边的词。

## 为什么放后端

- **token 不能进浏览器**。欧路 OpenAPI 用 `Authorization: NIS <token>` 鉴权。
  扩展的配置文件是明文存储的（还支持一键导出成 JSON），token 放那儿等于
  跟着配置一起导出去了。放后端就只留在本机。
- **生词本的源头本来就是后端**。`/api/vocabulary` 读写的是 `user.db` 的
  `vocabulary_words` 表；扩展的 Dexie `vocabularyWords` 表在迁移到后端时已经
  清空了。欧路词以 `source='eudic'` 落进**同一张表**，分页/搜索/排序天然统一，
  复习提醒的队列（挑 `last_reviewed_at IS NOT NULL` 的词）也自动覆盖。
- **在扩展里做双源归并很难看**。两个来源各自分页、各自排序，合并后的
  「第 N 页」和「按星级排序」都得在前端重新算。

## 组成

| 模块 | 职责 |
|------|------|
| `app/eudic.py` | 欧路客户端（HTTP + TLS 配置 + 重试）、配置存储、文本清洗、同步、写回 |
| `app/eudic_api.py` | REST 路由（配置 / 生词本列表 / 验证 / 同步） |
| `app/vocabulary.py` | 生词表新增 `source` / `source_id` / `source_category` / `phonetic` 四列与合并查询 |
| `app/main.py` | 挂载路由；写操作（新增/改星级/删除）附带欧路同步结果 |

## 配置

配置存在 `user.db` 的 `eudic_settings` 表（key=`config` 的 JSON），扩展通过
`/api/eudic/config` 读写。

| 字段 | 含义 |
|------|------|
| `enabled` | 是否启用欧路同步 |
| `fallbackToken` | 备用 token（环境变量没生效时用） |
| `categoryId` | 要同步的生词本 id |
| `categoryName` | 生词本名称（仅显示用） |
| `syncNewWords` | 本地新收藏的词是否同时推给欧路 |
| `lastSyncedAt` | 上次同步时间 |

**token 的来源优先级**：

1. 环境变量 `EUDIT_API_KEY`（别名 `EUDIC_API_KEY`）——**推荐**。机器级
   环境变量（`setx /M`）新进程都会继承，计划任务开机拉起时也能拿到。
2. 设置页保存的 `fallbackToken`——环境变量没生效时的兜底，**明文**存库。

`GET /api/eudic/config` **不回传 token 明文**，只给 `token_source`
（`env`/`settings`/`none`）和 `has_fallback_token` 两个布尔值。

> 改了环境变量必须**重启后端**（`script/stop_server.ps1` + `run_server.bat`）：
> 进程启动时就把环境块拷走了，跑着的进程读不到新值。

## 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/eudic/config` | 读配置（不含 token 明文） |
| PUT | `/api/eudic/config` | 改配置（按字段合并） |
| GET | `/api/eudic/categories` | 实时列出欧路生词本 |
| POST | `/api/eudic/verify` | 验证连通性，顺便回传生词本列表 |
| POST | `/api/eudic/sync` | 把欧路生词本同步进本地表 |

生词表本身多了一个查询参数：

- `GET /api/vocabulary?source=all|local|eudic`——按来源筛选；
- 响应多一个 `by_source`（各来源计数，不受当前搜索/筛选影响），给扩展的
  来源筛选做计数提示用。

## 同步规则

`sync_from_eudic()` 拉全量（`sort=asc` 翻页，`page` 上限 50、`page_size`
上限 100 → 单本最多 5000 词），逐条清洗后 upsert。

**清洗**：欧路的 `exp` 和 `context_line` 里带 HTML（`<br>` / `<b>`），直接存
进去闪卡背面会显示原始标签。`<br>` 换成空格 → 去标签 → 解 HTML 实体 → 压
空白。

**对齐顺序**（`upsert_eudic_words`）：

1. `source_id`（`<category_id>:<word.lower()>`）命中 → 同一个欧路词，更新它；
2. 同名的本地词 → **认领**为欧路词（同一个词只留一条，不出现两份）；
3. 都没有 → 新建。

**已有词只补空字段**，译文、语境、音标都不覆盖——扩展自己的那份更丰富。
手动推给欧路的词在欧路那边 `exp` 是空字符串，不「只补空」就会把本地译文
擦成空。

**星级冲突用 `review_count` 裁决**：本地 `review_count = 0`（用户还没评过分）
才采用欧路的星级；`> 0` 就以本地为准——用户的评分是主动行为，比远端状态更近。

代价：**在欧路 App 里手动改星级不会回流到扩展**。欧路没给 `updated_at`，
回流会造成两边乒乓。

**同步是 upsert-only，不删本地行**。欧路侧删掉的词本地还留着（本地是超集）；
反过来本地删除会写回欧路。

## 写回（本地 → 欧路）

| 操作 | 欧路接口 |
|------|----------|
| 改星级 | `POST /studylist/word`（只传 `language`/`word`/`star`/`category_ids`） |
| 删除 | `DELETE /studylist/words`（`language`/`category_id`/`words`） |
| 新增 | `POST /studylist/word`（带 `context_line`） |

**写星级只用 `POST /studylist/word`**，别碰 `POST /studylist/words`：后者
（items 形式）会把词**追加**进请求里指定的 `category_id`，是个副作用。
`POST /studylist/word` 对已收藏的词也是 upsert，实测能直接改星级。

**省略 `context_line` 时原值保留，传空会把语境清掉**——所以改星级时不要带
这个参数。

**任何写操作都会刷新 `add_time`**，所以同步时绝不能覆盖已有行的
`created_at`（否则「按添加时间排序」会被写回操作打乱）。

**写回失败不回滚本地**：本地已经改成功了，回滚会让用户以为没改成。失败
信息通过响应里的 `eudic_sync` 字段带出去，扩展据此提示
「星级已保存，但同步到欧路失败：xxx」。

## 两个必须知道的坑

### 1. OpenSSL 的 TLS 1.3 ClientHello 会被中间设备掐断

同一条链路上，**OpenSSL 的 TLS 1.3 握手有大约 70% 的概率被掐断**，报
`UNEXPECTED_EOF_WHILE_READING`。实测同一个请求：允许 TLS 1.3 时成功率 30%，
把上限压到 **TLS 1.2** 之后 100%。

.NET（浏览器、PowerShell、curl）不受影响——它们的 ClientHello 长得不一样。
所以「浏览器里好使」完全不能证明网络没问题。

`_ssl_context()` 就是干这个的（`EUDIC_TLS_MAX=1.3` 可以放开）。

### 2. httpx 的 `trust_env=True` 在本机是坏的

它解析 `NO_PROXY` 时碰到 `[::1]` 这种带方括号的 IPv6 会抛
`InvalidURL: Invalid port: ':1]'`。所以 `app/eudic.py` 自己从环境变量里取
代理 URL、直接塞给 httpx 的 `proxy=` 参数，绕开那个坑（`trust_env` 保持
`False`）。

## 错误处理

传输层错误（连接被掐断、超时）统一归一成 `EudicNetworkError`（`EudicError`
的子类），路由层转成 **502 + 中文原因**。不归一的话，`httpx.ConnectError`
会直接穿到 FastAPI，变成裸的 `500 Internal Server Error`（`text/plain`，
连原因都没有）。

**只重试传输层错误**（最多 4 次，指数退避），HTTP 层的 4xx/5xx 是欧路明确
拒绝，重试只是浪费时间。

## 已知限制

- 欧路只支持 `en` / `fr` / `de` / `es` 四种语言，`app/eudic.py` 的 `LANGUAGE`
  固定为 `en`。
- 欧路接口只暴露「生词本」这一组。**语料库（`vocab_entries`）和例句
  （`sentences`）在多数账号上是空的**，App 里显示的「很多单词」如果是这些
  内容，OpenAPI 拿不到。
- 「已掌握词」（`mastered_words`）是**独立集合**，与生词本不重叠，且只有
  `word` + `add_time`（无释义/音标/星级），本方案**不同步**它。

## 测试

```bash
uv run python -X utf8 script/test_eudic.py
```

覆盖：token 解析优先级、HTML 清洗、ISO8601 解析、错误归一、网络重试、
翻页聚合、upsert 规则（建行/不覆盖/认领/星级裁决）、写回三条路径、配置落库、
REST 接口。
