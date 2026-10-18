# GoldenDict 监听自动入生词本

`app/goldendict.py` 监听 GoldenDict 的三个文本文件，把出现的新词通过 API
（`POST /api/vocabulary`）写入生词本。

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
2. **增量监听**：之后每 2 秒（`--interval` 可调）重新解析三个文件，只处理新出现的单词。
   GoldenDict 重写 / 轮转文件不影响正确性（每次全量解析 + 差集）。
3. **不重复**：单词进入生词本前经过两道过滤——
   - 生词本（`user.db`）中已存在（大小写不敏感）；
   - 本地 state 的 `processed` 集合中已处理过。

   任一处命中都不会再次写入；即使删除 state 文件，生词本去重仍然兜底。
4. **失败重试**：API 不可用时该轮所有词都不会被标记为已处理，下一轮自动重试；
   单词逐个写入，中途失败不影响已成功的部分。
5. **单实例**：通过 `data/goldendict_watcher.lock` 文件锁保证同时只有一个监听进程，
   防止并发重复写入。

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
| `MDX_API_BASE_URL` | `http://127.0.0.1:8000` | 生词本 API 地址 |
| `MDX_WATCH_INTERVAL` | `2` | 轮询间隔（秒） |
| `MDX_WATCH_HTTP_TIMEOUT` | `10` | API 请求超时（秒） |

state 文件（`data/goldendict_state.json`）记录 `initialized`、`processed`（已处理
单词集合）、`total_added`、`added_by_source`、`last_sync_at`，损坏时安全降级
（重建空状态，靠生词本去重兜底）。

## 测试

```bash
uv run python -X utf8 script/test_goldendict.py
```

46 项检查：三种格式解析、干净单词提取、翻译格式化、state 读写、单实例锁，
以及基于真实 uvicorn 服务的端到端场景（首次导入 / 再次运行不重复 / 新词增量 /
state 丢失不重复 / API 故障恢复补写）。
