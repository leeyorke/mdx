# 计划：生词接入复习队列（80% 生词 + 到期旧词优先）

> 状态：已实现（测试全绿，见 `script/test_review.py` 的 `test_new_word_intake`）
> 日期：2026-06-30
> 关联：`docs/review.md`、`app/review/service.py`、`app/vocabulary.py`

## 0. 背景与诊断

### 现状死锁（用户诊断正确）

复习队列 `review_schedule` 的唯一来源是 `sync_review_queue()` → `_fetch_reviewed_words()`
→ `list_reviewed_words()`，而 `list_reviewed_words()` 只捞
`WHERE last_reviewed_at IS NOT NULL` 的词（复习次数 ≥ 1）。

所以 **`review_count = 0` 的生词永远进不了复习队列**。唯一的入队途径是先在
扩展闪卡里手动评一次（`POST /api/vocabulary/{id}/review` → `review_word`
刷新 `last_reviewed_at`），等下一次 `sync_review_queue()` 才会被收进队列。
页面空闲态那句「复习计划里还没有词——先用扩展的闪卡复习几个生词吧」
（`review.html:534`）就是这个死锁的自白。

### 纠正一个误解：现在的 80/20 不是「生词 vs 旧词」

现在的 `OLD_WORD_REVEAL_RATIO = 0.20` 是在**已经进队列的词内部**再分：

- 「新词」（fresh）= 还没毕业、还在爬阶梯的词；
- 「老词」（reveal）= 毕业了，或 `star ≥ 4 且 stage ≥ 6` 的词（那 20% 是
  「毕业词隔几天概率露个头」）。

**两拨都复习过至少一次。** 所以「永远推那几个词、阶梯走了一遍又一遍」的根因
不在 80/20，而在**队列入口把生词挡死了**——池子本身就只有那几个已复习的词。

## 1. 用户方案评估

**方案**：从总生词本抽词，80% 生词 + 20% 旧词；页面上「计划总数」改成生词本总数。

### 优点

1. **打破死锁**：生词自动进复习，不再依赖扩展先评一次。
2. **符合 Anki 式标准流程**：第一次见卡（带词典释义）本身就是「学」，
   `review_count = 0` 的词推出来就是它的第一课，不需要独立「预习」环节。
3. **真正用上整个生词本**：欧路同步、GoldenDict 收的词也能被复习到，
   不会收了就沉底。

### 缺点 / 风险

1. **首次曝光没有语境**：生词第一次出现就是一张卡，缺少扩展里「读过句子
   才收藏」的铺垫，偏硬背。靠词典释义 + 按需例句补，可接受但体验打折。
2. **保留会偏弱**：80/20 偏向输入。每天 8 个 ≈ 6 生词 + 2 旧词，阶梯复习
   容易被挤。**已通过对冲决策消解**（见下「旧词优先」）。
3. **通知数与会话可能不一致**：生词不提前建 SRS 档案（首次评分才建），
   推送批次抽的生词和会话实际给的生词可能不同。未评分的生词下次仍是
   `review_count = 0`，会被再次抽中，不会丢——可接受。
4. **「计划总数 = 生词本总数」是库存不是进度**：磁贴直接显示生词本总数即可，
   **不在页面加任何「别被大数字误导」之类的说明**（用户明确要求）。

> **`review_count` 语义（前端已改）**：现在扩展闪卡里**只要过一次就
> `review_count + 1`，跳过（skip）也算一次**（以前是评分一次才 +1）。所以
> `review_count = 0` 的实际含义是「**收藏了但从未在闪卡里过过**」的词。
> 这个池子定义不受影响，仍是要接入的目标；但它意味着 生词池 比想象中更纯粹——
> 只要被闪卡刷到过（哪怕跳过）就已经 `review_count ≥ 1`、走原 SRS 队列了。

## 2. 设计决策（已由用户拍板）

| 决策点 | 选择 |
|--------|------|
| 生词抽词顺序 | **最久收藏优先**（`created_at ASC`，FIFO） |
| 旧词优先级 | **到期旧词优先，生词补位**（保住记忆曲线） |
| 生词占比 | **做成设置项** `new_word_ratio`，默认 0.80 |

### 每日额度分配规则（核心算法）

设当天剩余额度 `remaining = max_per_day - today_notified`：

1. **到期旧词优先**：`due = pick_due_entries(...)`（阶梯到期 + 毕业露头），
   `旧词批次 = due[:remaining]`，优先全推；
2. **生词补位**：`生词名额 = remaining - len(旧词批次)`，再受
   `floor(quota × new_word_ratio)` 上限约束；
3. **不浪费额度**：若到期旧词不足 reserved 份额（旧词稀缺），生词可越过
   占比上限把当天额度填满。

> 命名纪律（改代码必守）：区分两个「新词」——
> **生词** = `review_count = 0` 待接入的词（intake）；
> **阶梯新词** = 已入档案、未毕业的词（现有 fresh）；
> **毕业词** = 已毕业（现有 reveal）。
> 注释与变量统一用「生词 / 阶梯词 / 毕业词」，绝不含混。

## 3. 实现方案（按子系统）

### 3.1 `app/vocabulary.py` — 生词数据访问

- 新增 `list_unreviewed_words(*, limit, offset) -> list[dict]`：
  `WHERE last_reviewed_at IS NULL ORDER BY created_at ASC LIMIT ? OFFSET ?`。
  用 `last_reviewed_at IS NULL` 判定「从未复习」（与 `review_count = 0` 等价，
  且能命中现有 `idx_vw_reviewed` 索引），与 `list_reviewed_words` 互补。
- 新增 `count_words() -> int`：`SELECT COUNT(*) FROM vocabulary_words`，
  给「计划总数」用。

### 3.2 `app/review/constants.py` + `store.py` — 配置项

- `constants.py`：新增 `DEFAULT_NEW_WORD_RATIO = 0.80`。
- `store.py` `default_config()`：加 `"new_word_ratio": DEFAULT_NEW_WORD_RATIO`。
- `load_config()`：读取时对 `new_word_ratio` 兜底 `0.80`（老库无此键）并夹取
  到 `[0, 1]`。`save_config` 走现有 JSON 合并，写入时同样夹取。

### 3.3 `app/review/service.py` — 核心逻辑（改动最大）

**抽生词助手** `_pick_new_words(limit, exclude_ids) -> list[dict]`：

- 分页调 `list_unreviewed_words`，跳过 `exclude_ids`（已在会话/批次的词），
  最多返回 `limit` 个；
- 返回结构能直接喂 `build_card_data`（`word_id / word / translation / star /
  context_text / ...`，`stage = 0`、`graduated = False`）。

**首评建卡** `rate_word(word_id, kind, next_word_id)`：

- 现在 `get_schedule_entry(word_id)` 为空就返回 `None`；
- 改成：无档案时查 `vocabulary.get_word(word_id)`，查到就用
  `_create_entry(word, now)` 建一条（阶梯 0、立即到期），再按
  `remembered / forgotten` 走 `advance_stage` + `apply_advanced_stage`；
- 后续回写 `review_word` 记账逻辑不变（本就 `review_count + 1`）。
- 这一步把生词正式纳入 SRS。

**卡片** `build_card_data(entry, pending_count)`：

- 让它能接受「尚未入档案的生词」——词典释义照查（`query_word`），
  `stage = 0`、`total_stages` 照给，例句走现有按需异步生成。

**批次合成** `_compose_push_batch(...)`：按上面「每日额度分配规则」重排
为「旧词优先 + 生词补位」。现有的阶梯到期挑选（`pick_due_entries`）、
毕业露头（`split_new_and_old` + `pick_reveal_pool`）逻辑不动，只是它们
合成的结果现在作为「旧词批次」优先占名额，生词补剩余。

**会话组装** `_compose_session(schedule, config, stats, now, mode)`：

- `auto`：`pending + due[:remaining]` 之后，用同样的「生词补位」规则补足到
  当天额度；生词在会话队列里带「生词」标记（前端可选显示新词角标）。
- `manual`：不受额度限制，按 `max_per_day` 补生词；现有「提前复习 / 兜底
  补齐」逻辑保留，生词优先于「提前复习」的阶梯词。

**状态** `get_status()`：新增

- `vocab_total`：生词本总数（`count_words()`）；
- `new_word_available`：还能抽多少生词（可选，用于提示）。

保留 `queue_size`（驱动 CTA 禁用与空态逻辑）。

### 3.4 `app/review/static/review.html` — 页面

- 「计划总数」磁贴（`#s-queue`，line 351）改绑 `status.vocab_total`
  （生词本总数）；`queue_size` 仍在 payload 里驱动禁用/空态。
- 空态文案改写（line 533-534）：不再说「先用扩展闪卡复习几个生词」；
  只有生词本真的为空才提示 `noWords`，有生词就总有可复习内容。
- 设置层（`#settings-dialog`）新增「**生词占比**」控件（数字输入或滑块，
  0–100%，默认 80），读写 `new_word_ratio`，与 `max_per_day` 同排。
- （可选）会话卡片对生词显示「新词」角标。

### 3.5 `docs/review.md` — 文档同步（文档与代码同等权威）

- 「队列从哪来」：不再只收复习过的词，生词自动接入，说明两条入队路径
  （生词 intake + 已复习词 sync）。
- 「新词 / 老词分流」：叠加「生词 vs 阶梯词」两层，说明旧词优先 + 生词补位
  + 可配置占比 `new_word_ratio`。
- 「复习页面」：计划总数 = 生词本总数、新增生词占比设置项。
- 「接口」：`/api/review/status` 返回新增 `vocab_total`。

### 3.6 `script/test_review.py` — 测试

- **生词抽词**：FIFO 顺序、`exclude_ids` 生效、`review_count ≥ 1` 的词不被
  当生词抽。
- **首评建卡**：`rate_word` 对无档案生词能建档案、推进阶梯、回写
  `review_word`；词不在生词本时仍优雅返回。
- **批次分配**：有到期旧词时旧词优先、生词补位；旧词不足时生词越上限填满；
  `new_word_ratio` 上限生效。
- **会话**：`auto` 受额度、生词计入；`manual` 补齐且生词优先于提前复习词。
- **配置**：`new_word_ratio` 读写与越界夹取（老库兜底）。
- **状态**：`vocab_total` 正确。
- 所有通知发送照旧 mock。

## 4. 数据流与边界情况

| 场景 | 行为 |
|------|------|
| 生词本为空 | 会话 `state = empty / noWords`，通知 `nothingDue`，不报错 |
| 全是生词、无到期旧词 | 生词填满当天额度（旧词名额回流给生词） |
| 全已毕业、无生词 | 走现有 `allGraduated` + 毕业词露头逻辑 |
| 生词在 /review 被推送但没评分 | 仍是 `review_count = 0`，下次照旧 FIFO 抽中，不会丢（/review 页只有点「记得 / 忘了」才调 `review_word` 记账；关掉会话不记） |
| 生词在扩展闪卡被刷过（哪怕跳过） | `review_count ≥ 1` → 已在原 SRS 队列，不再算生词、不重复接入 |
| 扩展仍先评了一次 | `review_count ≥ 1` → 进 `list_reviewed_words` → 走原 SRS 同步，两路不冲突 |
| 老库无 `new_word_ratio` | 读取兜底 0.80 并夹取 `[0, 1]` |
| 生词本很大（欧路 5000 词） | 生词不提前写 `review_schedule`，按需 FIFO 抽，不撑爆队列表 |

**关键架构约束**：生词**不提前写 `review_schedule`**，仅首次评分时建档。
这样避开 `sync_review_queue()` 的删除逻辑（它会删掉「不在
`list_reviewed_words` 里的档案」）——若提前给生词建档，下次同步就会被误删。

## 5. 验收标准

1. `uv run python -X utf8 script/test_review.py` 全绿（隔离 `user_test.db`）。
2. 收藏几个新词（不碰扩展）→ 打开 `/review` 能看到这些生词进入会话；
   评分后 `review_count + 1`、下次按阶梯到期。
3. 设置页可改「生词占比」并生效。
4. 「计划总数」显示生词本总数。
5. 到期旧词在有生词竞争时仍优先出现（记忆曲线不被挤掉）。

## 6. 假设

- 「复习次数为 0」用 `last_reviewed_at IS NULL` 判定。
- 生词首次曝光在复习卡上完成（无独立预习环节）。
- 生词不提前写 `review_schedule`，仅首次评分时建档。
- 现有阶梯间隔、毕业处置、露头概率、配额、免打扰、退避逻辑全部保持不变。
