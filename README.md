## mdx

## 启动方式

```bash
cd D:\myworkspace\mdx
uv run python -X utf8 -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

## 制作sqliteDB

1. 安装 [mdict-utils](https://github.com/liuyug/mdict-utils)： `pip install git+https://github.com/liuyug/mdict-utils.git`
2. 转换为数据库： `mdict -x .\oxford.mdx --exdb`