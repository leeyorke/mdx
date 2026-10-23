"""复习提醒的可调参数

数值与扩展里原来的实现保持一致（见 vibe-reading
``src/utils/constants/review.ts``），只是把「扩展的 alarm 最短 30 秒」换成了
后端自己的轮询节奏。
"""

from __future__ import annotations

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
