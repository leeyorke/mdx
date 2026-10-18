"""GoldenDict 监听模块

监听 GoldenDict 的三个文本文件，提取其中出现的新词，并通过本地 API
（``POST /api/vocabulary``）写入生词本：

- ``favorites``   —— 收藏，XML 结构（``<headword>word</headword>``）
- ``history``     —— 查词痕迹，每行 ``<次数> <词>``
- ``history.tmp`` —— 尚未合并进 history 的暂存记录，每行 ``+ <词>``（新增）

设计要点：

1. **首次全量导入**：第一次运行时（无 state 文件），三个文件中已存在的
   单词会被全部导入生词本；之后每次轮询只处理新增的单词。
2. **不重复**：以 ``user.db`` 中生词本已有单词 + 本地 state 中已处理的
   单词为准（大小写不敏感），任一处已存在的单词都不会再次写入。
3. **格式预处理**：三个文件格式各异，先分别解析出原始条目，再提取
   "干净的单词本身"（仅保留纯英文单词，短语 / URL / 路径 / 中文句子等
   自动跳过）。
4. **翻译**：写入时自动从词典库（oaldpe.db）查询释义，按生词本既有的
   多行格式生成 ``translation``；词典未收录时写入占位符。

用法::

    # 首次导入 + 持续监听（需先启动 API 服务）
    python -m app.goldendict

    # 只同步一次（首次导入）
    python -m app.goldendict --once

    # 预览将要导入的单词，不实际写入
    python -m app.goldendict --once --dry-run
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx

from app.dictionary import query_word
from app.vocabulary import DB_FILE as DEFAULT_VOCAB_DB

log = logging.getLogger("goldendict")

# ── 配置 ──────────────────────────────────────────────────────────────

PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_GOLDENDICT_DIR = Path(
    os.environ.get("GOLDENDICT_DIR", r"C:\Users\leeyorke\AppData\Roaming\GoldenDict")
)
DEFAULT_API_BASE = os.environ.get("MDX_API_BASE_URL", "http://127.0.0.1:8000").rstrip(
    "/"
)
DEFAULT_STATE_FILE = PROJECT_ROOT / "data" / "goldendict_state.json"
DEFAULT_LOCK_FILE = PROJECT_ROOT / "data" / "goldendict_watcher.lock"
DEFAULT_POLL_INTERVAL = float(os.environ.get("MDX_WATCH_INTERVAL", "2"))
HTTP_TIMEOUT = float(os.environ.get("MDX_WATCH_HTTP_TIMEOUT", "10"))

MAX_SENSES = 8  # translation 中最多保留的义项数量
NO_TRANSLATION = "（暂无释义）"

# 监听的文件：source 名 -> 文件名
WATCH_FILES: dict[str, str] = {
    "favorites": "favorites",
    "history": "history",
    "history.tmp": "history.tmp",
}

# 写入生词本 note 字段时使用的来源说明
SOURCE_LABELS: dict[str, str] = {
    "favorites": "favorites(收藏)",
    "history": "history(查词记录)",
    "history.tmp": "history.tmp(待写入查词记录)",
}


# ── 格式解析 ──────────────────────────────────────────────────────────

# favorites: <root> 下的 <headword>word</headword>，一行一个
_HEADWORD_RE = re.compile(r"<headword[^>]*>(.*?)</headword>", re.IGNORECASE | re.DOTALL)
# history: "<查词次数> <条目>"，例如 "0 credential" / "12 claim"
_HISTORY_RE = re.compile(r"^\s*(\d+)\s+(.*)$")
# history.tmp: "+ <条目>" 表示新增，"- <条目>" 表示删除
_HISTORY_TMP_RE = re.compile(r"^\s*([+-])\s+(.*)$")
# 干净的英文单词：字母开头，可含撇号 / 连字符（如 don't, by-product, cyber-）
_WORD_RE = re.compile(r"^[A-Za-z][A-Za-z'’\-]*$")

# 剥离原始条目时去掉的包裹引号与句尾标点
_STRIP_CHARS = "\"'“”‘’"
_TRAILING_PUNCT = ".,;:!?"


def parse_favorites(text: str) -> list[str]:
    """解析 favorites（XML 结构），返回 headword 原始条目列表"""
    return [m.group(1).strip() for m in _HEADWORD_RE.finditer(text)]


def parse_history(text: str) -> list[str]:
    """解析 history，每行 '<次数> <条目>'，返回条目列表"""
    entries: list[str] = []
    for line in text.splitlines():
        m = _HISTORY_RE.match(line)
        if m and m.group(2).strip():
            entries.append(m.group(2).strip())
    return entries


def parse_history_tmp(text: str) -> list[str]:
    """解析 history.tmp，只取 '+ <条目>'（新增）行，忽略 '-'（删除）行"""
    entries: list[str] = []
    for line in text.splitlines():
        m = _HISTORY_TMP_RE.match(line)
        if m and m.group(1) == "+" and m.group(2).strip():
            entries.append(m.group(2).strip())
    return entries


PARSERS = {
    "favorites": parse_favorites,
    "history": parse_history,
    "history.tmp": parse_history_tmp,
}


def extract_clean_word(entry: str) -> Optional[str]:
    """从原始条目中提取干净的单词（小写）。

    非单词条目（短语、URL、文件路径、中文句子、命令行等）返回 None。
    """
    if not entry:
        return None
    word = entry.strip().strip(_STRIP_CHARS).strip().rstrip(_TRAILING_PUNCT).strip()
    if not word:
        return None
    if not _WORD_RE.match(word):
        return None
    if not (2 <= len(word) <= 45):  # 过滤单字母与超长异常串
        return None
    return word.lower()


# ── 翻译格式化 ────────────────────────────────────────────────────────


def format_translation(entry: dict) -> str:
    """把词典条目格式化为生词本既有的多行翻译样式，例如::

        competitor
        英 /kəmˈpetɪtə(r)/ 美 /kəmˈpetɪtər/
        1. noun. 参赛者；竞赛者
        2. noun. （尤指商业方面的）竞争者，对手
    """
    lines: list[str] = []

    headword = (entry.get("headword") or "").strip()
    if headword:
        lines.append(headword)

    phonetics = entry.get("phonetics") or {}
    uk = (phonetics.get("uk") or "").strip()
    us = (phonetics.get("us") or "").strip()
    phon_parts = []
    if uk:
        phon_parts.append(f"英 {uk}")
    if us:
        phon_parts.append(f"美 {us}")
    if phon_parts:
        lines.append(" ".join(phon_parts))

    pos = (entry.get("pos") or "").strip()
    senses = entry.get("senses") or []
    for sense in senses[:MAX_SENSES]:
        number = (sense.get("number") or "").strip() or "❑"
        text = (sense.get("chinese_definition") or "").strip() or (
            sense.get("definition") or ""
        ).strip()
        if not text:
            continue
        prefix = f"{number}. {pos}." if pos else f"{number}."
        lines.append(f"{prefix} {text}".rstrip())

    return "\n".join(lines)


# ── 状态 ──────────────────────────────────────────────────────────────


@dataclass
class State:
    """本地监听状态（记录已处理过的单词，防止重复导入）"""

    initialized: bool = False
    processed: set[str] = field(default_factory=set)
    total_added: int = 0
    last_sync_at: Optional[str] = None
    added_by_source: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "version": 1,
            "initialized": self.initialized,
            "processed": sorted(self.processed),
            "total_added": self.total_added,
            "last_sync_at": self.last_sync_at,
            "added_by_source": self.added_by_source,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "State":
        return cls(
            initialized=bool(data.get("initialized", False)),
            processed=set(data.get("processed") or []),
            total_added=int(data.get("total_added", 0) or 0),
            last_sync_at=data.get("last_sync_at"),
            added_by_source=dict(data.get("added_by_source") or {}),
        )


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def load_state(state_file: Path) -> State:
    """读取 state 文件；不存在或损坏时返回全新状态（依赖生词本去重，安全降级）"""
    if not state_file.exists():
        return State()
    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
        return State.from_dict(data)
    except (OSError, ValueError) as e:
        log.warning("state 文件损坏，将重新开始（生词本去重仍然生效）: %s (%s)", state_file, e)
        return State()


def save_state(state_file: Path, state: State) -> None:
    """原子写入 state 文件"""
    state_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_file.with_suffix(state_file.suffix + ".tmp")
    tmp.write_text(
        json.dumps(state.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(tmp, state_file)


# ── 同步报告 ──────────────────────────────────────────────────────────


@dataclass
class SyncReport:
    found_counts: dict[str, int] = field(default_factory=dict)
    skipped_non_word: dict[str, int] = field(default_factory=dict)
    skipped_existing: int = 0
    skipped_processed: int = 0
    added: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    missing_files: list[str] = field(default_factory=list)
    dry_run: bool = False

    def summary(self) -> str:
        parts = [
            "新增 %d 个单词" % len(self.added),
            "生词本已有(跳过) %d" % self.skipped_existing,
            "已处理过(跳过) %d" % self.skipped_processed,
        ]
        non_word_total = sum(self.skipped_non_word.values())
        if non_word_total:
            detail = ", ".join(
                f"{src}:{cnt}" for src, cnt in self.skipped_non_word.items() if cnt
            )
            parts.append(f"非单词条目(跳过) {non_word_total}（{detail}）")
        if self.errors:
            parts.append(f"失败 {len(self.errors)}")
        if self.missing_files:
            parts.append(f"缺失文件 {len(self.missing_files)}")
        prefix = "[dry-run] " if self.dry_run else ""
        return prefix + "；".join(parts)


# ── 监听器 ────────────────────────────────────────────────────────────


class GoldenDictWatcher:
    def __init__(
        self,
        *,
        goldendict_dir: Optional[Path] = None,
        api_base: Optional[str] = None,
        state_file: Optional[Path] = None,
        vocab_db: Optional[Path] = None,
        dry_run: bool = False,
    ) -> None:
        self.goldendict_dir = Path(goldendict_dir or DEFAULT_GOLDENDICT_DIR)
        self.api_base = (api_base or DEFAULT_API_BASE).rstrip("/")
        self.state_file = Path(state_file or DEFAULT_STATE_FILE)
        self.vocab_db = Path(vocab_db or DEFAULT_VOCAB_DB)
        self.dry_run = dry_run
        self.state = load_state(self.state_file)

    # ── 读取与解析 ──

    def _collect_words(self, report: SyncReport) -> dict[str, list[str]]:
        """解析三个监听文件，返回 {source: [word, ...]}（小写、有序去重）"""
        result: dict[str, list[str]] = {}
        for source, filename in WATCH_FILES.items():
            path = self.goldendict_dir / filename
            if not path.exists():
                log.warning("文件不存在，跳过: %s", path)
                report.missing_files.append(str(path))
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError as e:
                msg = f"读取失败 {path}: {e}"
                log.error(msg)
                report.errors.append(msg)
                continue

            words: list[str] = []
            seen: set[str] = set()
            for entry in PARSERS[source](text):
                word = extract_clean_word(entry)
                if word is None:
                    report.skipped_non_word[source] = (
                        report.skipped_non_word.get(source, 0) + 1
                    )
                    continue
                if word not in seen:
                    seen.add(word)
                    words.append(word)
            result[source] = words
            report.found_counts[source] = len(words)
        return result

    # ── 生词本去重 ──

    def _existing_words(self) -> set[str]:
        """读取生词本（user.db）中已有的单词（小写集合）"""
        if not self.vocab_db.exists():
            return set()
        try:
            conn = sqlite3.connect(str(self.vocab_db))
            try:
                rows = conn.execute("SELECT word FROM vocabulary_words").fetchall()
            finally:
                conn.close()
        except sqlite3.Error as e:
            log.warning("读取生词本失败，将只依赖本地 state 去重: %s", e)
            return set()
        return {(row[0] or "").strip().lower() for row in rows if row[0]}

    # ── 翻译 ──

    def _lookup_translation(self, word: str) -> str:
        try:
            entry = query_word(word)
        except Exception as e:  # 词典库缺失 / 查询异常不应阻塞导入
            log.warning("查询词典失败 %s: %s", word, e)
            entry = None
        if not entry:
            return NO_TRANSLATION
        return format_translation(entry) or NO_TRANSLATION

    # ── API ──

    def _make_client(self) -> httpx.Client:
        """创建 HTTP 客户端。

        监听目标始终是本机 API，不使用代理环境变量（trust_env=False），
        同时避免某些代理环境变量导致 httpx 解析失败。
        """
        return httpx.Client(
            base_url=self.api_base, timeout=HTTP_TIMEOUT, trust_env=False
        )

    def _add_word(self, client: httpx.Client, word: str, source: str) -> bool:
        payload = {
            "word": word,
            "translation": self._lookup_translation(word),
            "note": f"来源：GoldenDict {SOURCE_LABELS.get(source, source)} 自动导入",
        }
        try:
            resp = client.post("/api/vocabulary", json=payload)
        except httpx.HTTPError as e:
            log.error("API 请求失败 %s: %s", word, e)
            return False
        if resp.status_code == 200:
            log.info("已加入生词本: %s（来源 %s）", word, source)
            return True
        log.error(
            "加入生词本失败 %s: HTTP %s %s", word, resp.status_code, resp.text[:200]
        )
        return False

    # ── 主流程 ──

    def sync_once(self) -> SyncReport:
        """执行一次同步：解析文件 -> 去重 -> 通过 API 写入生词本"""
        report = SyncReport(dry_run=self.dry_run)
        first_run = not self.state.initialized
        if first_run:
            log.info("首次运行：先把三个文件中已存在的单词全部导入生词本")

        found = self._collect_words(report)

        # 同一单词可能出现在多个文件中，保留最先出现的来源
        candidates: dict[str, str] = {}
        for source, words in found.items():
            for word in words:
                candidates.setdefault(word, source)

        existing = self._existing_words()
        processed = self.state.processed

        to_add: list[tuple[str, str]] = []
        for word, source in candidates.items():
            if word in processed:
                report.skipped_processed += 1
            elif word in existing:
                report.skipped_existing += 1
            else:
                to_add.append((word, source))

        if self.dry_run:
            report.added = [w for w, _ in to_add]
            log.info("[dry-run] 将要加入 %d 个单词: %s", len(to_add), report.added)
            return report

        if to_add:
            client = self._make_client()
            try:
                for word, source in to_add:
                    if self._add_word(client, word, source):
                        # 只有写入成功才标记为已处理，失败的下一轮重试
                        processed.add(word)
                        report.added.append(word)
                        self.state.total_added += 1
                        self.state.added_by_source[source] = (
                            self.state.added_by_source.get(source, 0) + 1
                        )
                    else:
                        report.errors.append(f"加入生词本失败: {word}")
            finally:
                client.close()

        self.state.initialized = True
        self.state.last_sync_at = _now_iso()
        save_state(self.state_file, self.state)
        return report

    def run_forever(self, interval: float = DEFAULT_POLL_INTERVAL) -> None:
        log.info(
            "开始监听 %s（每 %.1f 秒一次，API: %s，state: %s）",
            self.goldendict_dir,
            interval,
            self.api_base,
            self.state_file,
        )
        while True:
            try:
                report = self.sync_once()
                if report.added or report.errors:
                    log.info("同步完成: %s", report.summary())
                else:
                    log.debug("同步完成: %s", report.summary())
            except Exception:
                log.exception("同步过程发生异常，%.1f 秒后重试", interval)
            time.sleep(interval)


# ── 单实例锁（防止多个监听进程重复写入） ──────────────────────────────


class SingletonLock:
    """Windows 文件锁，保证同一时间只有一个监听进程在运行"""

    def __init__(self, lock_file: Path) -> None:
        self.lock_file = lock_file
        self._fh = None

    def acquire(self) -> bool:
        import msvcrt

        self.lock_file.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.lock_file, "w")
        try:
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            fh.close()
            return False
        fh.write(str(os.getpid()))
        fh.flush()
        self._fh = fh
        return True

    def release(self) -> None:
        if self._fh:
            try:
                import msvcrt

                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
            self._fh.close()
            self._fh = None


# ── CLI ───────────────────────────────────────────────────────────────


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.goldendict",
        description="监听 GoldenDict 查词记录/收藏，自动把新词写入生词本",
    )
    parser.add_argument(
        "--dir",
        default=None,
        help=f"GoldenDict 配置目录（默认 {DEFAULT_GOLDENDICT_DIR}）",
    )
    parser.add_argument(
        "--api-base",
        default=None,
        help=f"生词本 API 地址（默认 {DEFAULT_API_BASE}）",
    )
    parser.add_argument(
        "--state",
        default=None,
        help=f"state 文件路径（默认 {DEFAULT_STATE_FILE}）",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=DEFAULT_POLL_INTERVAL,
        help="轮询间隔秒数（默认 %(default)s）",
    )
    parser.add_argument(
        "--once", action="store_true", help="只同步一次后退出（首次导入用）"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="只预览将要导入的单词，不实际写入"
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="输出调试日志"
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    watcher = GoldenDictWatcher(
        goldendict_dir=args.dir,
        api_base=args.api_base,
        state_file=args.state,
        dry_run=args.dry_run,
    )

    lock_file = (
        Path(args.state).parent / "goldendict_watcher.lock"
        if args.state
        else DEFAULT_LOCK_FILE
    )
    lock = SingletonLock(lock_file)
    if not lock.acquire():
        log.error("另一个监听进程正在运行（锁文件: %s），退出", lock.lock_file)
        return 1
    try:
        if args.once:
            report = watcher.sync_once()
            log.info("同步完成: %s", report.summary())
            if report.added and not args.dry_run:
                log.info("本次新增单词: %s", report.added)
            for err in report.errors:
                log.error("%s", err)
            return 1 if report.errors else 0
        watcher.run_forever(interval=args.interval)
    finally:
        lock.release()
    return 0


if __name__ == "__main__":
    sys.exit(main())
