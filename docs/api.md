# api


## API 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/` | API 信息 |
| `GET` | `/api/word/{word}` | 查询单词完整释义（结构化 JSON） |
| `GET` | `/api/search?q=前缀&limit=20` | 按前缀模糊搜索单词 |
| `GET` | `/docs` | Swagger UI 交互文档（FastAPI 自动生成） |

## 响应

### `/api/word/{word}`

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


## `/api/search?q=前缀&limit=10`

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