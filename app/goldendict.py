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
5. **轮询节奏**：默认每小时轮询一次；00:00–08:00 为免打扰时段，暂停轮询，
   08:00 自动恢复（环境变量 ``MDX_WATCH_QUIET_HOURS`` 可调整，置空关闭）。
6. **两种运行方式**：
   - 独立进程：``python -m app.goldendict``（首次导入 ``--once`` / 预览
     ``--dry-run``），通过 HTTP API 写入；
   - 随主 app 嵌入：启动 uvicorn 时自动开始监听、主应用停止时自动结束，
     进程内调用 API 使用的同一数据层写入（见 ``start_embedded_watcher``）。

用法::

    # 首次导入 + 持续监听（需先启动 API 服务）
    # 默认每小时轮询一次，00:00-08:00 免打扰暂停
    python -m app.goldendict

    # 只同步一次（首次导入；手动同步不受免打扰时段限制）
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
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import httpx

from app.dictionary import query_word
from app.vocabulary import DB_FILE as DEFAULT_VOCAB_DB, add_word

log = logging.getLogger("goldendict")

# ── 配置 ──────────────────────────────────────────────────────────────

PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_GOLDENDICT_DIR = Path(
    os.environ.get("GOLDENDICT_DIR", r"C:\Users\leeyorke\AppData\Roaming\GoldenDict")
)
DEFAULT_API_BASE = os.environ.get("MDX_API_BASE_URL", "http://127.0.0.1:4080").rstrip(
    "/"
)
DEFAULT_STATE_FILE = Path(
    os.environ.get(
        "MDX_WATCH_STATE_FILE", PROJECT_ROOT / "data" / "goldendict_state.json"
    )
)
# 锁文件始终放在 state 文件同目录（与 start_embedded_watcher 保持一致）
DEFAULT_LOCK_FILE = DEFAULT_STATE_FILE.parent / "goldendict_watcher.lock"
DEFAULT_POLL_INTERVAL = float(os.environ.get("MDX_WATCH_INTERVAL", "3600"))
HTTP_TIMEOUT = float(os.environ.get("MDX_WATCH_HTTP_TIMEOUT", "10"))

# 免打扰时段（按本机时间的小时数），格式 "起始-结束"，如 "0-8"；置空关闭
QUIET_HOURS_SPEC = os.environ.get("MDX_WATCH_QUIET_HOURS", "0-8")


def _parse_quiet_hours(spec: str) -> Optional[tuple[int, int]]:
    """解析免打扰时段，返回 (起始小时, 结束小时)；空值或非法返回 None（不启用）"""
    spec = (spec or "").strip()
    if not spec:
        return None
    m = re.fullmatch(r"(\d{1,2})\s*-\s*(\d{1,2})", spec)
    if not m:
        log.warning("免打扰时段配置无效，已忽略: %r", spec)
        return None
    start, end = int(m.group(1)), int(m.group(2))
    if not (0 <= start <= 23 and 0 <= end <= 23) or start == end:
        log.warning("免打扰时段配置无效，已忽略: %r", spec)
        return None
    return start, end


QUIET_HOURS: Optional[tuple[int, int]] = _parse_quiet_hours(QUIET_HOURS_SPEC)

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


# ── 免打扰时段 ────────────────────────────────────────────────────────


def in_quiet_hours(now: Optional[datetime] = None) -> bool:
    """当前是否处于免打扰时段（按本机时间）"""
    if QUIET_HOURS is None:
        return False
    start, end = QUIET_HOURS
    hour = (now or datetime.now().astimezone()).hour
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end  # 跨午夜，如 22-6


def seconds_until_quiet_end(now: Optional[datetime] = None) -> float:
    """距离免打扰时段结束还有多少秒（未配置免打扰时返回 0）"""
    if QUIET_HOURS is None:
        return 0.0
    now = now or datetime.now().astimezone()
    end = now.replace(hour=QUIET_HOURS[1], minute=0, second=0, microsecond=0)
    if end <= now:
        end += timedelta(days=1)
    return (end - now).total_seconds()


def _sleep_in_chunks(
    seconds: float,
    chunk: float = 60.0,
    stop_event: Optional[threading.Event] = None,
) -> None:
    """分片休眠（默认每片 60 秒）。

    提供 stop_event 时每片都用 ``stop_event.wait``，主应用关闭能立即唤醒；
    否则退回 ``time.sleep``（Ctrl+C 可中断）。
    """
    deadline = time.monotonic() + max(0.0, seconds)
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        if stop_event is not None:
            if stop_event.wait(min(chunk, remaining)):
                return
        else:
            time.sleep(min(chunk, remaining))


# ── 写入生词本的两种方式 ──────────────────────────────────────────────


class HttpVocabularyWriter:
    """通过 HTTP API 写入生词本（独立进程模式）"""

    def __init__(self, api_base: str) -> None:
        self.api_base = api_base.rstrip("/")
        # 监听目标始终是本机 API，不走代理环境变量
        self._client = httpx.Client(
            base_url=self.api_base, timeout=HTTP_TIMEOUT, trust_env=False
        )

    def add(self, word: str, translation: str, note: str) -> bool:
        payload = {"word": word, "translation": translation, "note": note}
        try:
            resp = self._client.post("/api/vocabulary", json=payload)
        except httpx.HTTPError as e:
            log.error("API 请求失败 %s: %s", word, e)
            return False
        if resp.status_code != 200:
            log.error(
                "加入生词本失败 %s: HTTP %s %s", word, resp.status_code, resp.text[:200]
            )
            return False
        return True

    def close(self) -> None:
        self._client.close()


class LocalVocabularyWriter:
    """进程内写入生词本（随主 app 嵌入模式）。

    调用 ``app.vocabulary.add_word``——即 ``POST /api/vocabulary`` 端点内部
    使用的同一个数据层函数；不依赖端口 / 网络 / ASGI 桥接。
    """

    def add(self, word: str, translation: str, note: str) -> bool:
        try:
            add_word(word=word, translation=translation, note=note)
        except Exception as e:
            log.error("写入生词本失败 %s: %s", word, e)
            return False
        return True

    def close(self) -> None:
        pass


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
        writer=None,
    ) -> None:
        self.goldendict_dir = Path(goldendict_dir or DEFAULT_GOLDENDICT_DIR)
        self.api_base = (api_base or DEFAULT_API_BASE).rstrip("/")
        self.state_file = Path(state_file or DEFAULT_STATE_FILE)
        self.vocab_db = Path(vocab_db or DEFAULT_VOCAB_DB)
        self.dry_run = dry_run
        # writer：独立进程默认走 HTTP API；嵌入模式注入 LocalVocabularyWriter
        self._owns_writer = writer is None
        self.writer = writer if writer is not None else HttpVocabularyWriter(
            self.api_base
        )
        self.state = load_state(self.state_file)

    def close(self) -> None:
        """释放 writer 持有的资源（仅限自己创建的 writer）"""
        if self._owns_writer:
            self.writer.close()

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

    # ── 写入 ──

    def _build_payload(self, word: str, source: str) -> dict:
        """构造写入生词本的 payload（translation 查词典生成）"""
        return {
            "word": word,
            "translation": self._lookup_translation(word),
            "note": f"来源：GoldenDict {SOURCE_LABELS.get(source, source)} 自动导入",
        }

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
            for word, source in to_add:
                payload = self._build_payload(word, source)
                if self.writer.add(word, payload["translation"], payload["note"]):
                    # 只有写入成功才标记为已处理，失败的下一轮重试
                    processed.add(word)
                    report.added.append(word)
                    self.state.total_added += 1
                    self.state.added_by_source[source] = (
                        self.state.added_by_source.get(source, 0) + 1
                    )
                    log.info("已加入生词本: %s（来源 %s）", word, source)
                else:
                    report.errors.append(f"加入生词本失败: {word}")

        self.state.initialized = True
        self.state.last_sync_at = _now_iso()
        save_state(self.state_file, self.state)
        return report

    def run_forever(
        self,
        interval: float = DEFAULT_POLL_INTERVAL,
        stop_event: Optional[threading.Event] = None,
    ) -> None:
        """持续监听；stop_event 置位时尽快退出（供随主 app 嵌入模式使用）"""
        stop_event = stop_event if stop_event is not None else threading.Event()
        quiet_desc = (
            f"{QUIET_HOURS[0]:02d}:00-{QUIET_HOURS[1]:02d}:00 免打扰"
            if QUIET_HOURS
            else "无免打扰时段"
        )
        log.info(
            "开始监听 %s（每 %.0f 秒一次，%s，state: %s）",
            self.goldendict_dir,
            interval,
            quiet_desc,
            self.state_file,
        )
        while not stop_event.is_set():
            if in_quiet_hours():
                wait = seconds_until_quiet_end()
                log.info("免打扰时段内，暂停轮询，约 %.0f 分钟后恢复", wait / 60)
                _sleep_in_chunks(wait, stop_event=stop_event)
                continue
            try:
                report = self.sync_once()
                if report.added or report.errors:
                    log.info("同步完成: %s", report.summary())
                else:
                    log.debug("同步完成: %s", report.summary())
            except Exception:
                log.exception("同步过程发生异常，%.0f 秒后重试", interval)
            if stop_event.wait(interval):
                break  # 等待期间收到停止信号
        log.info("监听循环已结束")


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


# ── 随主 app 嵌入模式 ─────────────────────────────────────────────────

_embedded_lock: Optional[SingletonLock] = None
_embedded_watcher: Optional[GoldenDictWatcher] = None
_embedded_thread: Optional[threading.Thread] = None
_embedded_stop: Optional[threading.Event] = None


def start_embedded_watcher() -> bool:
    """随主 app 启动监听（守护线程），供 FastAPI lifespan 调用。

    - ``MDX_WATCH_AUTOSTART=0`` 时不启动；
    - 锁被占用（已有独立监听进程在跑）时跳过，避免双写；
    - 任何失败只记日志并返回 False，**不影响主应用启动**。
    """
    global _embedded_lock, _embedded_watcher, _embedded_thread, _embedded_stop
    if _embedded_thread is not None:
        return True  # 已在运行
    if os.environ.get("MDX_WATCH_AUTOSTART", "1") == "0":
        log.info("MDX_WATCH_AUTOSTART=0，不随主 app 启动监听")
        return False

    # uvicorn 默认只配置自己的 logger，这里保证监听日志可见
    if not logging.getLogger().handlers:
        logging.basicConfig(
            level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
        )

    lock = SingletonLock(DEFAULT_STATE_FILE.parent / "goldendict_watcher.lock")
    if not lock.acquire():
        log.warning(
            "已有 GoldenDict 监听在运行（锁文件 %s），本实例跳过自动监听",
            lock.lock_file,
        )
        return False

    try:
        from app import vocabulary as vocabulary_module

        # vocab_db 在调用时解析，便于测试替换；其余用模块级默认值
        watcher = GoldenDictWatcher(
            writer=LocalVocabularyWriter(),
            vocab_db=vocabulary_module.DB_FILE,
        )
    except Exception:
        log.exception("创建监听器失败，本实例跳过自动监听")
        lock.release()
        return False

    stop_event = threading.Event()
    thread = threading.Thread(
        target=watcher.run_forever,
        kwargs={"stop_event": stop_event},
        name="goldendict-watcher",
        daemon=True,
    )
    thread.start()
    _embedded_lock = lock
    _embedded_watcher = watcher
    _embedded_thread = thread
    _embedded_stop = stop_event
    log.info("GoldenDict 监听已随主 app 启动（守护线程 %s）", thread.name)
    return True


def stop_embedded_watcher(timeout: float = 10.0) -> None:
    """停止随主 app 启动的监听（主应用关闭时调用）"""
    global _embedded_lock, _embedded_watcher, _embedded_thread, _embedded_stop
    if _embedded_thread is None:
        return
    if _embedded_stop is not None:
        _embedded_stop.set()
    if _embedded_thread.is_alive():
        _embedded_thread.join(timeout=timeout)
        if _embedded_thread.is_alive():
            log.warning("监听线程未能在 %.0f 秒内退出（守护线程将随进程结束）", timeout)
    try:
        if _embedded_watcher is not None:
            _embedded_watcher.close()
    finally:
        if _embedded_lock is not None:
            _embedded_lock.release()
        _embedded_lock = None
        _embedded_watcher = None
        _embedded_thread = None
        _embedded_stop = None
    log.info("GoldenDict 监听已停止")


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
        help=f"轮询间隔秒数（默认 {DEFAULT_POLL_INTERVAL:g}，即 1 小时）",
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
            if in_quiet_hours():
                log.info("当前处于免打扰时段，但 --once 是手动同步，仍执行一次")
            report = watcher.sync_once()
            log.info("同步完成: %s", report.summary())
            if report.added and not args.dry_run:
                log.info("本次新增单词: %s", report.added)
            for err in report.errors:
                log.error("%s", err)
            return 1 if report.errors else 0
        try:
            watcher.run_forever(interval=args.interval)
        except KeyboardInterrupt:
            log.info("收到中断信号，监听停止")
    finally:
        watcher.close()
        lock.release()
    return 0


if __name__ == "__main__":
    sys.exit(main())
