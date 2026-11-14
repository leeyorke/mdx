"""复习提醒的可调参数

数值与扩展里原来的实现保持一致（见 vibe-reading
``src/utils/constants/review.ts``），只是把「扩展的 alarm 最短 30 秒」换成了
后端自己的轮询节奏。
"""

from __future__ import annotations

import zlib
from typing import Any, Mapping

# ── 艾宾浩斯阶梯 ──────────────────────────────────────────────────────

#: 经典的艾宾浩斯记忆间隔（分钟）。单词纯靠时间往上爬，评分只是微调。
#:
#: 早期只保留三档（5 分钟 / 30 分钟 / 12 小时）——「今天复习过的词当天不再
#: 出现」，比 12 小时更细的小档都会塌缩成「明天」，纯属浪费级数；级数花在
#: 天级部分：1 / 2 / 3 / 5 / 8 / 12 天，不再一放就是一周半。
SRS_INTERVALS_MINUTES: tuple[int, ...] = (
    5,     # 5 分钟
    30,    # 30 分钟
    720,   # 12 小时
    1440,  # 1 天
    2880,  # 2 天
    4320,  # 3 天
    7200,  # 5 天
    11520, # 8 天
    17280, # 12 天
)

# ── 新词/老词分流（评分高推送频率低、评分低推送频率高）────────────────

#: 走完阶梯（graduated）**或**爬到高阶且星级高（``star ≥`` OLD_WORD_STAR 且
#: ``stage ≥`` OLD_WORD_STAGE）的词算「老词」。老词不再按阶梯天天推，而是概率
#: 露头——star 是用户手动标、stage 是系统累积，两者结合才算真熟练。
OLD_WORD_STAR = 4
OLD_WORD_STAGE = 6

#: 每天分配给老词「露头」的额度比例（占当天配额的 20%）。新词优先占
#: ``1 - OLD_WORD_REVEAL_RATIO``，老词最多占这么多；老词抽不满时名额回流给
#: 新词，绝不硬凑。
OLD_WORD_REVEAL_RATIO = 0.20

#: 每天配额里分给「生词」的比例（生词 = ``review_count = 0``、收藏了但从未在闪卡
#: 里过过的词，intake 接入）。到期旧词**优先占名额**（保住记忆曲线），生词补剩余；
#: 生词名额默认不超过 ``剩余额度 × 此比例``——这个上限其实是给旧词预留份额。旧词
#: 稀缺（占不到预留份额）时生词可越过上限把当天额度填满，绝不为凑比例浪费名额。
DEFAULT_NEW_WORD_RATIO = 0.80

#: 老词每天被抽中露头的概率。用 ``word_id + 日期`` 做确定性哈希判定，同一个词
#: 同一天结果稳定（重跑调度不会反复横跳），不同词/不同天彼此独立。
OLD_WORD_REVEAL_PROBABILITY = 0.15

#: 老词露头一次后的冷却天数——露完头就沉下去，隔一周左右才可能再现身。
OLD_WORD_REVEAL_COOLDOWN_DAYS = 7


def is_old_word(entry: Mapping[str, Any]) -> bool:
    """一个词算「老词」吗：毕业了，或者星级与阶梯都爬上去了。

    老词走概率露头（低频），新词/低星词按阶梯正常到期（高频）。
    """
    if entry.get("graduated"):
        return True
    return (
        int(entry.get("star", 1) or 1) >= OLD_WORD_STAR
        and int(entry.get("stage", 0) or 0) >= OLD_WORD_STAGE
    )


def roll_reveal(word_id: str, day_key: str) -> bool:
    """今天该不该让这个词露头（确定性概率判定）。

    把 ``word_id`` 和日期一起哈希，取值落在概率区间内就算命中。同一个词
    同一天重跑结果一致——不会因为调度器多醒几次就反复横跳。
    """
    digest = zlib.crc32(f"{word_id}:{day_key}".encode("utf-8"))
    return (digest % 10_000) < int(OLD_WORD_REVEAL_PROBABILITY * 10_000)


# ── 调度节奏 ──────────────────────────────────────────────────────────

#: 后台循环的轮询间隔（秒）。到期时间由调度器精确计算，轮询只负责「到点了就跑」。
TICK_INTERVAL_SECONDS = 30

#: 两次唤醒之间的最小间隔（毫秒）。
#:
#: 浏览器里的 alarm 有 30 秒的硬下限；后端没有这个限制，但**必须自己加**：
#: 一堆积压的词会在同一时刻到期，没有下限就会变成「每秒推 3 条」的热循环。
MIN_WAKEUP_DELAY_MS = 30_000

#: 单轮最多把多少条超配额词推迟到今天结束 / 明天。
MAX_DEFERRALS_PER_TICK = 50

#: 队列上限：只跟踪最近这么多已复习过的生词。
MAX_QUEUE_SIZE = 200

#: 扫描生词本时的分页大小。
VOCABULARY_PAGE_SIZE = 100

#: 超过这个天数没有被同步确认的词会被丢弃。
ENTRY_TTL_DAYS = 30

#: 每日配额统计保留天数。
STATS_TTL_DAYS = 7

#: 推送失败后的退避：``min(BACKOFF_BASE_MINUTES * 2^失败次数, BACKOFF_MAX_MINUTES)``。
BACKOFF_BASE_MINUTES = 2
BACKOFF_MAX_MINUTES = 30

# ── 默认配置 ──────────────────────────────────────────────────────────

#: 每天最多提醒多少个词（一天最多发一条通知，一批全覆盖）。
DEFAULT_MAX_PER_DAY = 8
DEFAULT_QUIET_HOURS_START = "23:00"
DEFAULT_QUIET_HOURS_END = "08:00"
DEFAULT_GRADUATED_ACTION = "stop"

#: 通知点击后打开的页面。一天只有一条批量提醒，点进去直接进复习会话——
#: 地址固定，不再做成可配置项（旧版按词推通知时才需要 ``{word_id}`` 模板）。
REVIEW_PAGE_URL = "http://127.0.0.1:4080/review"

# ── 通知文案 ──────────────────────────────────────────────────────────

#: 每日唯一一条批量提醒的标题，``{count}`` 是这批到期的词数。
NOTIFY_BATCH_TITLE = "今天有 {count} 个单词要复习"

#: 批量提醒正文里预览多少个词（其余折叠成"等 N 个"）。
NOTIFY_BATCH_PREVIEW_WORDS = 3

#: 批量提醒正文的结尾引导语。
NOTIFY_BATCH_BODY_SUFFIX = "，点击开始复习"

#: 通知归属的应用标识（AUMID）。Windows 靠它决定通知归属；默认值是系统自带的
#: Windows PowerShell，保证任何机器上都能弹出来。需要换成自己的应用名时，
#: 用环境变量 ``MDX_REVIEW_TOAST_AUMID`` 覆盖。
DEFAULT_TOAST_AUMID = "Microsoft.WindowsPowerShell"

#: 通知归属文本（显示在通知底部）。
TOAST_ATTRIBUTION = "mdx"

# ── 例句生成 ──────────────────────────────────────────────────────────

#: 请求 OpenRouter 时带上的应用归属头（官方推荐，用于展示调用来源）。
OPENROUTER_REFERER = "https://pi.dev"
OPENROUTER_TITLE = "pi"

#: base_url 里出现这个域名时才带归属头——别的提供商不认识这两个头。
OPENROUTER_HOST = "openrouter.ai"

#: 每次推送轮换的「使用场景」，让例句真正换角度而不是同义改写。
REVIEW_EXAMPLE_ANGLES: tuple[str, ...] = (
    "daily conversation",
    "work email",
    "tech blog writing",
    "movie or TV dialogue",
    "academic writing",
    "casual slang",
    "business news",
    "travel situation",
    "social media post",
    "idiom / collocation drill",
)

#: 单次例句生成最多保留的搭配数量。
EXAMPLE_COLLOCATION_LIMIT = 5

#: 调用 LLM 的超时时间（秒）。
EXAMPLE_LLM_TIMEOUT_SECONDS = 20.0

# ── 复习成果检验（写作 + LLM 评卷）───────────────────────────────────

#: 写作模式：一篇短文用上所有词，或逐词造句。
WRITING_MODE_PARAGRAPH = "paragraph"
WRITING_MODE_SENTENCES = "sentences"
WRITING_MODES: tuple[str, ...] = (WRITING_MODE_PARAGRAPH, WRITING_MODE_SENTENCES)

#: 单次检验最多带多少个目标词（等于复习会话的队列上限，留个硬顶防滥用）。
WRITING_MAX_WORDS = 200

#: 提交文本的长度上限（字符）。评卷 prompt 会整段塞进去，别让人贴一本书。
WRITING_MAX_TEXT_CHARS = 8000

#: 笔记列表「高分练习」阈值：``score >= 80``。
WRITING_NOTE_HIGH_SCORE = 80

#: 笔记列表「待优化」阈值：``score < 70``（70–79 区间两边都不算）。
WRITING_NOTE_IMPROVE_BELOW = 70

#: 批改条目的类型白名单（LLM 输出之外的类型一律归到 ``word_choice``）。
WRITING_CORRECTION_TYPES: tuple[str, ...] = (
    "grammar",
    "spelling",
    "tense",
    "collocation",
    "word_choice",
)

#: 内联高亮标记：用对了（绿）/ 有错（红）。
WRITING_SPAN_GOOD = "good"
WRITING_SPAN_ERROR = "error"
