## mdx

## 启动方式

```bash
cd D:\myworkspace\mdx
uv run python -X utf8 -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

## GoldenDict 查词自动入生词本

监听 GoldenDict 的 `favorites`（收藏）、`history`（查词痕迹）、`history.tmp`（待写入查词记录）
三个文件，出现新词时自动通过 API 写入生词本：

- **首次运行**会把三个文件中已存在的单词全部导入；
- 之后每次轮询只处理新增的单词，**生词本不会重复**（以 `user.db` 已有单词 +
  本地 `data/goldendict_state.json` 已处理记录为准，大小写不敏感）；
- 自动过滤短语 / URL / 路径 / 中文句子等非单词条目；
- 写入时自动从词典库查询释义，按生词本既有多行格式生成 translation。

```bash
# 终端 1：启动 API 服务（监听依赖它）
uv run python -X utf8 -m uvicorn app.main:app --host 127.0.0.1 --port 8000

# 终端 2：首次导入 + 持续监听（默认每 2 秒轮询一次）
uv run python -X utf8 -m app.goldendict

# 只同步一次（首次导入）
uv run python -X utf8 -m app.goldendict --once

# 预览将要导入的单词，不实际写入
uv run python -X utf8 -m app.goldendict --once --dry-run
```

详见 [docs/goldendict-watcher.md](docs/goldendict-watcher.md)。
测试：`uv run python -X utf8 script/test_goldendict.py`

## 制作sqliteDB

1. 安装 [mdict-utils](https://github.com/liuyug/mdict-utils)： `pip install git+https://github.com/liuyug/mdict-utils.git`
2. 转换为数据库： `mdict -x .\oxford.mdx --exdb`