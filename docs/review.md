# 复习提醒（艾宾浩斯主动复习）

复习提醒原本实现在浏览器扩展 vibe-reading 里（`src/utils/review/`、
`src/entrypoints/background/review-scheduler.ts`、独立复习卡片页）。现在整套
搬到后端：**调度、状态、Windows 通知、复习卡片与配置页面都由本服务提供**，
扩展侧只保留生词本与闪卡。

## 为什么放后端

- 状态本来就不在扩展里——`/api/vocabulary` 没有「上次推送时间」字段，扩展
  只能把 SRS 队列整个镜像到 `chrome.storage.local`，还得自己同步、自己判断
  「这个词复习过没有」。后端本来就是生词本的源头，直接在源头判断即可。
- 扩展的 service worker 随时会被回收，提醒精度取决于浏览器什么时候唤醒它。
  后端是一个常驻进程，到点就推。
- 通知从「浏览器通知」变成「Windows 系统通知」：不依赖浏览器开着，点击后用
  默认浏览器打开复习页面就行。

## 组成

| 模块 | 职责 |
|------|------|
| `app/review/constants.py` | 阶梯间隔、配额、免打扰默认值等可调参数 |
| `app/review/schedule.py` | 纯函数：阶梯推进、到期挑选、免打扰时段、退避 |
| `app/review/store.py` | SQLite 落库（与生词本同库 `user.db`） |
| `app/review/notifier.py` | Windows 系统通知（`windows-toasts`） |
| `app/review/example.py` | 例句生成（可选 LLM，回落到收藏时的上下文） |
| `app/review/service.py` | 同步队列、跑一轮调度、评分、组装卡片 |
| `app/review/scheduler.py` | 后台常驻循环（随 FastAPI 起停） |
| `app/review/api.py` | REST 接口 + 复习页面路由 |
| `app/review/static/review.html` | 复习页面（复习 + 配置） |

## 艾宾浩斯阶梯

间隔阶梯（分钟）：`5 / 30 / 720 / 1440 / 2880 / 5760 / 10080 / 21600`
（5 分钟 → 30 分钟 → 12 小时 → 1 天 → 2 天 → 4 天 → 7 天 → 15 天）。

- 单词纯靠时间往上爬（`auto`）。
- 复习页面上的「记得 / 忘了」只做微调：**记得跨两级**（并多等一级），**忘了退
  一级**（不会掉到第一级以下）。
- 走完整个阶梯后按 `graduated_action` 处理：`stop`（默认，停止推送）或
  `restart`（清零重新开始）。

## 队列从哪来

只有**至少复习过一次**的生词才进队列——刚收藏、还没看过的词没有记忆曲线可排。
扩展的闪卡评分会调 `POST /api/vocabulary/{word_id}/review`，写入
`last_reviewed_at` 与 `review_count`，后端据此挑词。

## 推送与配额

- 单轮最多推 `MAX_PUSHES_PER_TICK`（3）条，避免通知中心被刷屏；
- 每天最多 `max_per_day`（8）条，每词每天最多 `max_per_word_per_day`（3）条；
- 免打扰时段（默认 23:00–08:00，可跨零点）内到期的词**整队一起**推迟到窗口
  结束，不是一条一条慢慢放；
- 推送失败按指数退避（2 分钟起步，30 分钟封顶），**不占用**这个词在阶梯上的
  位置——下次还会再推。

## Windows 通知

用 `windows-toasts`（WinRT 的 Python 封装）弹通知，并设置 `launch` 为**协议
激活地址**：

```xml
<toast launch="http://127.0.0.1:4080/review?word=<生词ID>" activationType="protocol">
```

点击通知 → 系统用默认浏览器打开那个地址 → 直接进入该词的复习卡片。

通知归属的 AUMID 默认借用系统自带的 `Microsoft.WindowsPowerShell`（保证任何
Windows 机器上都能弹出来）。想换成自己的应用名，设环境变量
`MDX_REVIEW_TOAST_AUMID`。

非 Windows 平台（或没装 `windows-toasts`）上会安静降级：状态接口里
`notification_available` 为 `false`，调度按「推送失败」处理并退避。

## 复习页面

`GET /review`——一个自带的单文件页面（`app/review/static/review.html`），不依赖
浏览器扩展：

- 复习卡片：单词、翻译、例句、固定搭配、词典释义、朗读（浏览器
  `speechSynthesis`）、「记得 / 忘了」；
- 状态：队列大小、今日推送数、待评分数、已毕业数、下次到期时间；
- 配置：开关、每日配额、单词配额、免打扰时段、毕业后的处置、是否用 LLM 生成
  例句、LLM 的 base_url / api_key / model、通知点击后打开的地址模板；
- 动作：同步队列、立即推送、测试通知、清空队列。

## 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/api/review/status` | 队列大小、今日推送数、下次到期、通知是否可用 |
| `GET` | `/api/review/config` | 读取配置 |
| `PUT` | `/api/review/config` | 按字段合并更新配置 |
| `POST` | `/api/review/sync` | 用生词本对齐复习队列 |
| `POST` | `/api/review/tick` | 立刻跑一轮调度（免打扰与配额照常生效） |
| `DELETE` | `/api/review/queue` | 清空复习队列（不动生词本） |
| `GET` | `/api/review/card?word_id=` | 取一张待复习卡片（不传则接着上次会话） |
| `POST` | `/api/review/rate` | 记录评分 `{word_id, kind}`，返回下一张卡片 |
| `POST` | `/api/review/notify-test` | 发一条测试通知 |
| `GET` | `/api/review/pending` | 待评分的词列表 |
| `GET` | `/review` | 复习页面 |

## 例句

例句是可选的加分项，**任何一步失败都不影响推送**：

1. 配了 LLM（`base_url` + `model`）且 `generate_example` 为真 → 调
   OpenAI 兼容接口生成例句 + 中文翻译 + 固定搭配，角度按推送次数轮换
   （日常对话 / 邮件 / 技术博客 / 影视对白 / 学术写作 ……），避免连续两次
   同义改写；
2. 没配 LLM 或调用失败 → 退回收藏这个词时抓到的上下文句子；
3. 上下文就是词本身（GoldenDict 常这样）→ 通知正文用兜底文案。

## 存储

与生词本同库（`user.db`），三张表：

- `review_schedule`：每个词的进度（阶梯、到期时间、推送次数、例句……）
- `review_stats`：每日推送配额统计
- `review_settings`：配置（JSON）与上次同步时间

## 测试

```bash
uv run python -X utf8 script/test_review.py
```

覆盖阶梯推进、毕业处置、退避、到期挑选、免打扰时段、日历工具、落库、队列
同步、一轮调度（推送 / 配额 / 推迟 / 失败退避）、评分、通知点击地址、REST
接口与页面。
