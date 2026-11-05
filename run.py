"""MDX 服务启动入口 —— 把「走哪个库」做成命令参数，省掉手动设 ``MDX_USER_DB``。

用法::

    # 正式服务（走 user.db，端口默认 4080）
    uv run python -X utf8 run.py

    # 测试服务（走隔离的 user_test.db，端口默认 4081）
    uv run python -X utf8 run.py --test

    # 测试服务换端口 / 重新复制种子
    uv run python -X utf8 run.py --test --port 4082
    uv run python -X utf8 run.py --test --fresh

为什么需要这个包装：uvicorn 自己的 CLI 不认 ``--test``（实测报
``No such option '--test'``），所以没法把它直接加在 ``-m uvicorn`` 后面。
这里用 argparse 自己解析参数、设好环境变量，再调 ``uvicorn.run()``。

**环境变量必须早于 app 的 import**：``app/vocabulary.py`` 的 ``DB_FILE`` 在
import 那一刻就从 ``MDX_USER_DB`` 算好了，``review/store.py``、``eudic.py``、
``goldendict.py`` 都从它 import 这一个值。所以这里把 app 当字符串
``"app.main:app"`` 传给 ``uvicorn.run()``——app 真正的 import 发生在
``run()`` 内部，那时环境变量早已就位。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
REAL_DB = REPO_ROOT / "user.db"
TEST_DB = REPO_ROOT / "user_test.db"

DEFAULT_PORT = 4080
TEST_PORT = 4081


def _disable_review_notifications(db: Path) -> None:
    """关掉测试库的复习提醒，别让测试实例弹 Windows 通知、跟 4080 抢到期词。

    配置存在 ``review_settings`` 表 ``key='config'`` 的 JSON 里，把 ``enabled``
    置 False 后调度器空转——不发通知，也不去抢同一批到期词。
    """
    conn = sqlite3.connect(db)
    try:
        row = conn.execute(
            "SELECT value FROM review_settings WHERE key='config'"
        ).fetchone()
        cfg = json.loads(row[0]) if row and row[0] else {}
        cfg["enabled"] = False
        conn.execute(
            "INSERT INTO review_settings (key, value) VALUES ('config', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (json.dumps(cfg, ensure_ascii=False),),
        )
        conn.commit()
    finally:
        conn.close()


def _prepare_test_db(fresh: bool) -> None:
    """确保测试库存在（缺省从 user.db 复制种子）并关掉它的复习提醒。

    与 ``script/run_test_server.ps1`` 的语义一致：测试库要是有真实数据可检验
    （复习过的词、生词本条目），而不是一张空表。``--fresh`` 丢弃旧测试数据、
    重新复制一份种子。
    """
    if fresh or not TEST_DB.exists():
        if not REAL_DB.exists():
            raise SystemExit(f"真实 user.db 不存在：{REAL_DB}，无法复制测试种子")
        shutil.copy(REAL_DB, TEST_DB)
        print(f"已从 user.db 复制测试种子 → {TEST_DB}")
    else:
        print(f"复用已有测试库 {TEST_DB}（--fresh 可重新复制种子）")

    _disable_review_notifications(TEST_DB)
    print("测试库 review_settings.enabled 已置 False")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="启动 MDX 服务（正式 / 测试二选一，靠 --test 切换库）",
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help=f"走隔离测试库 {TEST_DB.name}（默认端口 {TEST_PORT}），不碰真实 user.db",
    )
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="配合 --test：丢弃旧测试数据，重新从 user.db 复制种子",
    )
    parser.add_argument("--host", default="0.0.0.0", help="监听地址")
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help=f"监听端口（默认：正式 {DEFAULT_PORT} / 测试 {TEST_PORT}）",
    )
    args = parser.parse_args()

    if args.test:
        _prepare_test_db(fresh=args.fresh)
        # 必须在 app 被 import 之前设好——见文件头的时序说明。
        os.environ["MDX_USER_DB"] = str(TEST_DB)
        port = args.port if args.port is not None else TEST_PORT
        print(f"库：{TEST_DB}（独立，不碰真实 user.db）")
    else:
        # 不带 --test 就明确走 user.db：清掉环境里可能残留的覆盖值，
        # 免得上次设过的 MDX_USER_DB 悄悄把正式服务指到测试库。
        os.environ.pop("MDX_USER_DB", None)
        port = args.port if args.port is not None else DEFAULT_PORT
        print(f"库：{REAL_DB}")

    print(f"服务：http://{args.host}:{port}/  文档：/docs")

    import uvicorn

    uvicorn.run("app.main:app", host=args.host, port=port)


if __name__ == "__main__":
    main()