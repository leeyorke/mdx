# api


## API 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/` | API 信息 |
| `GET` | `/api/word/{word}` | 查询单词完整释义（结构化 JSON） |
| `GET` | `/api/search?q=前缀&limit=20` | 按前缀模糊搜索单词 |
| `POST` | `/api/vocabulary` | 添加生词到生词本 |
| `GET` | `/api/vocabulary?limit=20&offset=0&sortBy=createdAt&sortOrder=desc&search=` | 分页查询生词本 |
| `GET` | `/api/vocabulary/{word_id}` | 查看生词详情 |
| `PATCH` | `/api/vocabulary/{word_id}` | 更新生词（star / translation / note） |
| `DELETE` | `/api/vocabulary/{word_id}` | 删除生词 |
| `POST` | `/api/vocabulary/{word_id}/review` | 标记复习一次 |
| `GET` | `/api/review/status` | 复习提醒状态（可复习词数、今日已复习/已提醒、下次到期、通知是否可用） |
| `GET` | `/api/review/config` | 读取复习提醒配置 |
| `PUT` | `/api/review/config` | 更新复习提醒配置 |
| `GET` | `/api/review/session?mode=auto\|manual` | 组装一次复习会话（有序词表 + 第一张卡片） |
| `POST` | `/api/review/example` | 按需生成（并缓存）一个词的例句 |
| `POST` | `/api/review/sync` | 用生词本对齐复习队列（内部，调度器用） |
| `POST` | `/api/review/tick` | 立刻跑一轮调度：一天最多一条批量通知（内部/调试） |
| `DELETE` | `/api/review/queue` | 清空复习队列（内部/调试） |
| `GET` | `/api/review/card?word_id=` | 取一张复习卡片 |
| `POST` | `/api/review/rate` | 记录复习评分 `{word_id, kind, next_word_id?}`，返回下一张卡片 |
| `POST` | `/api/review/notify-test` | 发送一条测试通知（内部/调试，页面已无入口） |
| `GET` | `/api/review/pending` | 待评分的词列表 |
| `GET` | `/review` | 复习页面（会话式复习 + 配置） |
| `GET` | `/docs` | Swagger UI 交互文档（FastAPI 自动生成） |

## 请求及响应示例

### `GET /api/word/{word}`
> 查询一个单词的完整释义

```json
{
  "headword": "apply",
  "id": "apply_h_1",
  "pos": "verb",
  "phonetics": {
    "uk": "/əˈplaɪ/",
    "us": "/əˈplaɪ/"
  },
  "senses": [
    {
      "number": "1",
      "definition": "to make a formal request, usually in writing, for something such as a job, a loan, permission for something, a place at a university, etc.",
      "chinese_definition": "（通常以书面形式）申请，请求",
      "examples": [
        {
          "text": "",
          "chinese": "你应该当面／写信申请。"
        },
        {
          "text": "apply for job",
          "chinese": "我已经决定申请这份新工作。"
        },
        {
          "text": "",
          "chinese": "他们已成功申请政治庇护。"
        },
        {
          "text": "apply for a visa/passport/grant/licence",
          "chinese": "申请签证／护照／拨款／执照"
        },
        {
          "text": "applied for permission",
          "chinese": "开发商申请建造 200 多栋房屋的规划许可。"
        },
        {
          "text": "",
          "chinese": "向公司／大学申请"
        },
        {
          "text": "",
          "chinese": "他们可以向法院申请撤销该决定。"
        },
        {
          "text": "",
          "chinese": "他已报名参军。"
        }
      ]
    },
    {
      "number": "2",
      "definition": "to affect or be relevant to somebody/something",
      "chinese_definition": "有关；涉及",
      "examples": [
        {
          "text": "",
          "chinese": "18 岁以下者按特殊情况处理。"
        },
        {
          "text": "rules applied",
          "chinese": "这些条例适用于员工及其工作中的行为。"
        },
        {
          "text": "",
          "chinese": "我所说的只涉及你们中的一些人。"
        },
        {
          "text": "apply equally",
          "chinese": "该政策将平等地适用于所有人。"
        },
        {
          "text": "law applied",
          "chinese": "该法律仅适用于成员国的工人。"
        },
        {
          "text": "",
          "chinese": "“乏味”这个词绝对和她的小说沾不上边儿。"
        }
      ]
    },
    {
      "number": "3",
      "definition": "to use something or make something work in a particular situation",
      "chinese_definition": "使用；应用",
      "examples": [
        {
          "text": "apply pressure",
          "chinese": "施加政治压力"
        },
        {
          "text": "apply principles",
          "chinese": "这项挑战让学生能够应用他们在课堂上学到的经济原理。"
        },
        {
          "text": "",
          "chinese": "实施经济制裁"
        },
        {
          "text": "",
          "chinese": "应用测试/方法/技术/方法/规则"
        },
        {
          "text": "apply standards",
          "chinese": "我们需要对自己和他人采用相同的标准。"
        },
        {
          "text": "",
          "chinese": "这项新技术已应用于农业。"
        },
        {
          "text": "",
          "chinese": "现在是运用你所学知识的时候了。"
        }
      ]
    },
    {
      "number": "4",
      "definition": "to put or spread something such as paint, cream, etc. onto a surface",
      "chinese_definition": "涂；敷；施",
      "examples": [
        {
          "text": "",
          "chinese": "把乳霜薄薄地抹在脸和脖子上。"
        },
        {
          "text": "",
          "chinese": "胶水应涂在两个表面上。"
        },
        {
          "text": "",
          "chinese": "她梳了梳头，然后仔细地化了妆。"
        },
        {
          "text": "",
          "chinese": "油漆是用细刷子涂的。"
        }
      ]
    },
    {
      "number": "5",
      "definition": "to work at something or study something very hard",
      "chinese_definition": "勤奋工作；努力学习",
      "examples": [
        {
          "text": "",
          "chinese": "努力学习就会通过考试。"
        },
        {
          "text": "",
          "chinese": "我们绞尽脑汁寻求解决问题的办法。"
        }
      ]
    },
    {
      "number": "6",
      "definition": "to press on something hard with your hand, foot, etc. to make something work or have an effect on something",
      "chinese_definition": "手压；脚踩",
      "examples": [
        {
          "text": "",
          "chinese": "踩刹车"
        },
        {
          "text": "",
          "chinese": "压住伤口可以止血。"
        }
      ]
    }
  ]
}
```

### `GET /api/search?q=前缀&limit=20`
> 按前缀模糊搜索单词
```json
{
  "query": "aban",
  "results": [
    "abandon",
    "abandon's",
    "abandon_1",
    "abandon_2",
    "abandonable",
    "abandoned",
    "abandoned",
    "abandonedly",
    "abandoner",
    "abandoner's"
  ],
  "count": 10
}
```

### `POST /api/vocabulary`

> 添加生词。

**请求体**

```json
{
  "word": "apple",
  "translation": "苹果",
  "contextUrl": "https://example.com/article/1",
  "contextText": "I ate an apple.",
  "sourceLanguage": "eng",
  "targetLanguage": "zho",
  "note": "test"
}
```

**响应**

```json
{
  "id": "f96978fb-1b87-40ca-999d-9fa908dd7464",
  "word": "apple",
  "translation": "苹果",
  "context_url": "https://example.com/article/1",
  "context_text": "I ate an apple.",
  "source_language": "eng",
  "target_language": "zho",
  "star": 1,
  "created_at": 1748832000000,
  "last_reviewed_at": null,
  "review_count": 0,
  "note": "test"
}
```

### `GET /api/vocabulary`

> 分页查询生词本。

**查询参数**

- `limit`：返回数量，默认 20，范围 1-100
- `offset`：偏移量，默认 0
- `sortBy`：排序字段，可选 `createdAt` | `star` | `word`，默认 `createdAt`
- `sortOrder`：排序方向，可选 `asc` | `desc`，默认 `desc`
- `search`：按 `word` 模糊搜索，可选

**响应**

```json
{
  "records": [
    {
      "id": "f96978fb-1b87-40ca-999d-9fa908dd7464",
      "word": "apple",
      "translation": "苹果",
      "context_url": "https://example.com/article/1",
      "context_text": "I ate an apple.",
      "source_language": "eng",
      "target_language": "zho",
      "star": 5,
      "created_at": 1748832000000,
      "last_reviewed_at": 1748832600000,
      "review_count": 1,
      "note": "记住了"
    }
  ],
  "total": 1,
  "limit": 20,
  "offset": 0
}
```

### `GET /api/vocabulary/{word_id}`

> 查看单个生词详情

**响应**

```json
{
  "id": "f96978fb-1b87-40ca-999d-9fa908dd7464",
  "word": "apple",
  "translation": "苹果",
  "context_url": "https://example.com/article/1",
  "context_text": "I ate an apple.",
  "source_language": "eng",
  "target_language": "zho",
  "star": 1,
  "created_at": 1748832000000,
  "last_reviewed_at": null,
  "review_count": 0,
  "note": "test"
}
```

### `PATCH /api/vocabulary/{word_id}`

> 更新生词，仅允许修改 `star`、`translation`、`note`。

**请求体**

```json
{
  "star": 5,
  "note": "记住了"
}
```

### `DELETE /api/vocabulary/{word_id}`

> 删除生词。

**响应**

```json
{
  "message": "删除成功"
}
```

### `POST /api/vocabulary/{word_id}/review`

> 标记复习一次，`review_count` +1 并更新 `last_reviewed_at`。

**响应**

```json
{
  "id": "f96978fb-1b87-40ca-999d-9fa908dd7464",
  "word": "apple",
  "translation": "苹果",
  "context_url": "https://example.com/article/1",
  "context_text": "I ate an apple.",
  "source_language": "eng",
  "target_language": "zho",
  "star": 5,
  "created_at": 1748832000000,
  "last_reviewed_at": 1748832600000,
  "review_count": 1,
  "note": "记住了"
}
```