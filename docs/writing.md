# 复习成果检验（写作 + LLM 评卷）

复习走完会话之后的**检验环节**：把本次会话的词（**只给词形、不给释义**）交给用户
写一篇短文或逐词造句 → 发给 LLM 评卷打分 + 校语法 → 结果可保存为「写作笔记」按
分数回顾。与 `docs/review.md` 的主动复习链路是**接力关系**：复习是输入，检验是
输出端的自我校验。

## 设计稿对应的三个前端细节（后端契约）

- **内联高亮要 span 定位**：评卷 JSON 带 `spans: [{start, end, text, kind}]`，
  `kind ∈ good | error`。评卷弹层里 verdict 的绿色下划线、believes 的红色下划线，
  以及笔记列表里错词的波浪线，都由这份数据驱动。服务端按原文校验 span
  （`text` 与原文 `start:end` 切片一致），对不上的条目丢弃，不让前端画错位。
- **分数两套刻度**：评卷弹层显示 `88 / 100`，笔记列表显示 `8.5 / 10`。后端**统一
  存 0–100 整数**（`85` 对应 8.5），笔记列表由前端 `/10` 展示，不引入浮点列。
- **「已使用单词 2/7」实时计数 + chip 灰/亮**：前端拿 `challenge` 的词表在输入框里
  做大小写不敏感整词匹配，纯前端算，**不每次输入都打后端**；后端只在 `grade` 时给
  权威 `used_words`（含 believes 这类词形，LLM 判）做最终对账。
- **chip 上的 🔊 喇叭**：走浏览器 `speechSynthesis`（前端本地，零后端改动、不引
  TTS 依赖）。后端不提供 TTS 接口。

## 组成

| 模块 | 职责 |
|------|------|
| `app/review/llm_client.py` | OpenAI 兼容 LLM 客户端传输层（例句与评卷共用） |
| `app/review/writing_store.py` | 写作三张表的落库（与复习队列同库 `user.db`） |
| `app/review/writing.py` | 组装检验、评卷、用词覆盖匹配、草稿与笔记编排 |
| `app/review/api.py` | `/api/review/writing/*` 路由 + `/verify` 页面 |
| `app/review/static/verify.html` | 检验页面（词 chip + 输入 + 评卷弹层 + 笔记列表） |

## LLM 客户端：例句与评卷共用一份配置

`app/review/example.py` 原本自带一套 OpenAI 兼容调用（代理、OpenRouter 归属头、
`trust_env=False`）。现在把**与「例句」无关的传输部分**抽到 `llm_client.py`，例句
与评卷共用，谁也不复制代理与 TLS 逻辑：

```python
# app/review/llm_client.py
def chat(config, *, system, user, temperature=0.2) -> Optional[str]: ...
def parse_json_payload(raw) -> Optional[dict]: ...
def is_configured(config) -> bool: ...
```

- **评卷复用例句那套 LLM 配置**（`review_settings.llm`：`base_url` / `api_key` /
  `model`），不新增配置项、不在设置层加「评卷模型」项。这是一个明确的取舍：
  零配置改动、开箱即用；代价是不能给评卷单独指一个更强的模型。
- 例句的 `temperature` 是 0.9（要新鲜感），评卷是 0.2（要稳定、可复现的批改）。
- `llm_client` 里 re-export 了 `httpx`：既有例句单测把 `app.review.example.httpx`
  当 mock 目标，而真正发请求的地方已经搬到 `llm_client`——两边引用同一个模块
  对象，mock 才拦得住。`example.py` 保留 `_llm_headers` / `_resolve_proxy` /
  `_request_example` / `parse_llm_payload` 这些旧名字作为薄包装。

**任何失败都返回 `None`，绝不抛出**：例句是复习的锦上添花，评卷是检验的加分项，
都不该因为「LLM 挂了 / 没配 / 超时」把调用方一起拖下水。

## 数据层（`user.db` 三张表）

沿用 `store._CREATE_SQL`（`_ensure_db` 每次连接幂等执行），老库第一次用到检验时
表自动补上，无需单独迁移。**与 `review_schedule` 完全独立**——检验读写再多也绝不
碰 SRS 进度（评分仍由「记得/忘了」驱动，写作不打分回记忆曲线）。

| 表 | 内容 |
|----|------|
| `review_writing_tasks` | 一次检验任务的词表快照（`word_ids` / `words` / `mode`） |
| `review_writing_submissions` | 一次提交 + 评卷结果（`score` / `grade` JSON / `used_words` / `llm_source`） |
| `review_writing_notes` | 写作笔记（从 submission 复制，冗余 `score` 供筛选） |

筛选口径：**高分练习** `score >= 80`；**待优化** `score < 70`（`score IS NULL` 的
草稿只出现在「全部笔记」）。`grade` / `words` / `spans` 这些结构化字段存 JSON
文本，读出来时统一在 `writing_store` 反序列化并做形状兜底（库里被塞了畸形 JSON
也不能让接口 500）。

## 评卷

### `build_challenge(word_ids=None, mode="paragraph")`

组装一次检验，返回 `task_id` + 词列表。`word_ids` 为 `None` 时取「立即复习」会话
（`build_session("manual")`）的词表——检验本来就该跟着刚复习完的这组走；前端也可以
把页面手里已有的会话队列 ID 直接传进来。传 `[]` 则就地返回 `empty`（不误回落到
会话词表，否则「检验空词表」会被偷偷换成会话的词）。**词表只含 `word`，绝不含
`translation`**——检验的意义就是凭记忆用词，把翻译摆旁边等于抄。

### `grade_writing(task_id, text, mode)`

1. 校验 `text` 非空、长度 ≤ 8000 字符；`task_id` 无效返回 `None`（路由转 404）。
2. 配置了 LLM → `llm_client.chat` 评卷 → `parse_json_payload` 规整。
3. **LLM 未配置/失败 → 本地兜底**：`match_used_words` 做大小写不敏感整词匹配
   （带 `_stem` 词形归一化，believes→believ、believed→believe），`score=None`、
   `feedback` 给「未配置 LLM，仅做用词覆盖统计」、`llm_source="matcher"`。保证功能
   不因缺配置而不可用，页面能明确提示「这次没评上分」，而不是点了没反应。
4. 写 `review_writing_submissions`，返回评卷结果 + `submission_id`。
5. 评卷在 `run_in_threadpool` 里跑（阻塞 httpx，别占事件循环拖慢状态接口与调度器）。

LLM 输出契约（system 要求「只输出一个 JSON 对象」）：

```json
{
  "score": 88,
  "usage": [{"word": "verdict", "used": true, "form_in_text": "verdict"}],
  "spans": [{"start": 27, "end": 34, "text": "verdict", "kind": "good"}],
  "feedback": "遣词造句十分地道……",
  "corrections": [{"original": "most of the people believes", "suggestion": "most people believe",
                   "explanation": "主谓一致错误……", "type": "grammar"}],
  "vocab_suggestions": [{"word": "deliberation", "note": "用语高级且准确……"}],
  "polished": "修改后的完整短文"
}
```

- `type` 白名单：`grammar | spelling | tense | collocation | word_choice`（之外一律
  归到 `word_choice`）。
- 分数钳制 `max(0, min(100, int))`；缺失字段一律回落空值，**模型输出畸形也不能 500**。
- `spans` 按 `text` 与原文切片校验，对不上就丢弃。
- LLM 没给 `usage` 时，用本地匹配补一份，保证「用了哪些词」始终有值。

### 草稿与笔记

- `save_draft(task_id, text, mode)`：先把稿子存成 `llm_source="draft"` 的 submission
  （`score`/`grade` 为空），再复制成笔记 —— 对应检验页面的「保存草稿」。
- `save_note(submission_id)`：把一次（已评卷的）提交复制成笔记 —— 对应评卷弹层的
  「保存为笔记」。同一 `submission_id` 允许多条（用户可能反复保存），不做唯一约束。
- `list_notes(filter, limit, offset)` / `get_note(id)` / `delete_note(id)`：筛选
  `all | high | improve`，返回每种筛选各自的计数（`counts`，给三个 tab 的角标）与
  总数（`grand_total`，页面底部「已累计练习 N 篇短文」）。排序字段走白名单防注入
  （沿用生词本 `list_words` 的做法）。

## 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/api/review/writing/challenge` | 取检验词表（不显示释义），可选 `word_ids`（逗号分隔）/ `mode` |
| `POST` | `/api/review/writing/grade` | `{task_id, text, mode}` → 分数 + 批改 + 用词覆盖 + span |
| `POST` | `/api/review/writing/draft` | `{task_id, text, mode}` 保存草稿为笔记 |
| `POST` | `/api/review/writing/notes` | `{submission_id}` 保存为写作笔记 |
| `GET` | `/api/review/writing/notes` | 分页 + `filter=all\|high\|improve` + `sortBy`/`sortOrder` |
| `GET` | `/api/review/writing/notes/{id}` | 单篇详情（含完整评卷结果） |
| `DELETE` | `/api/review/writing/notes/{id}` | 删除笔记 |
| `GET` | `/verify` | 检验页面 |

安全口径沿用项目约定：`grade` / `used_words` / `score` / `created_at` 一律服务端算，
不接受客户端 PATCH。

## 页面

`GET /verify`——独立的单文件页面（`app/review/static/verify.html`），不往
`review.html` 里堆视图。接线点：`review.html` 完成态（🎉 那屏）加一个「检验一下
成果」入口，带上本次会话 `words[].word_id` 跳 `/verify?word_ids=...`。视觉沿用复习
页的苹果式极简（浅灰底、白色大圆角卡片、深色跟随系统）。响应同样带
`Cache-Control: no-cache`。

## 边界与失败模式

- **词形覆盖**：依赖 LLM 判 `used`（能认 believes/believed）；兜底 matcher 只做整词
  + 词干前缀匹配，`used_words` 可能偏少 —— 前端只当提示，不当硬校验，不拦提交。
- **评卷超时**：`LLM_TIMEOUT_SECONDS = 20.0`（与例句共用）。超时走 matcher 降级，
  返回体里 `llm_source` 让前端能提示「这次没评上分」。
- **并发**：三个写入方（评卷、草稿、保存笔记）都走 `_get_conn()` 短事务；SQLite
  单写者，与现有模块一致，无需加锁。
- **空词表**：`challenge` 返回 `state=empty` + `empty_reason`（复用 `noWords /
  allGraduated / nothingDue` 口径），页面给人话提示。

## 测试

```bash
uv run python -X utf8 script/test_writing.py
```

覆盖建库（幂等 + 老库补表）、`build_challenge`（词表来源与**不泄露释义**）、评卷
解析（正常 JSON / 带 ``` 围栏 / 畸形输出不 500 / 分数钳制）、LLM 未配置与调用异常
两条降级 → matcher 兜底、用词覆盖匹配（大小写 / 词形归一化）、span 校验（错位 /
负偏移 / 空区间 / 非法 kind / 非数字 offset）、草稿 → 评卷 → 保存笔记 的落库读回
（含 `grade` JSON 往返与 `/10` 展示）、笔记分页与 `all/high/improve` 筛选 + 排序
白名单、REST 接口与 `/verify` 页面 `no-cache`。

**测试里所有 LLM 调用都做了 mock**，跑测试不发网络请求、不弹通知。

同时确认 `uv run python -X utf8 script/test_review.py` 全绿（证明 `example.py`
重构没改行为）。
