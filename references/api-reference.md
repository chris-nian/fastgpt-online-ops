# SANGFOR Agent Builder（FastGPT 商业版）线上环境 API 参考

> 实测标注：除单独注明者外，全部端点 **2026-08-24 于 `https://fastgpt.sangfor.com.cn:17443` 实测**。
> 来源：方案三实现验收报告 `r1-dataset-load-20260824.md`、`r1-target-session-20260824.md`、`r1b-target-session-20260824.md`（位于 `10-专项项目/2026-09-Sangfor渠道与客户大会AI活动/05-Demo与实操/方案三实现/acceptance/`）。
> 本文件不含任何凭证、token 或内部 IP。

## 0. 传输与认证

- **TLS**：私有 CA 自签证书。Python 必须用 `ssl._create_unverified_context()`，否则握手失败。
- **认证头**：`token: <值>`。不是 `Authorization`、不是 `Bearer`。
- **响应包裹**：所有接口返回 `{code, data, message}`；`code == 200` 为业务成功（HTTP 同为 200）。
- **参数位置**（前端 `pages/_app` chunk module 18992 的 HTTP 封装行为）：
  - `GET` / `DELETE`：参数一律放 **URL query**；
  - `POST` / `PUT`：参数一律放 **JSON body**（`Content-Type: application/json`）。
  - 后果：删类接口若把 id 放 body，collection 报 500 `missingParams`、data 报 400。

## 1. 登录

### 1.1 preLogin

```
GET /api/support/user/account/preLogin?username=<url编码账号>
```

成功响应（节选）：

```json
{ "code": 200, "data": { "code": "<一次性 code>" }, "message": "" }
```

### 1.2 loginByPassword

```
POST /api/support/user/account/loginByPassword
Content-Type: application/json

{ "username": "<账号>", "password": "<sha256hex(原始密码)>", "code": "<preLogin 的 data.code>", "language": "zh-CN" }
```

成功响应的 data 含 token 字符串；后续请求放 `token` 头。

**陷阱**：
- password 是**原始密码的 sha256 十六进制**，不是明文。
- token 失效表现为 401/403；处理方式是重新走 preLogin → loginByPassword 后重放原请求，不视为致命错误。

## 2. 应用（app）

### 2.1 app/create

```
POST /api/core/app/create
```

实测 payload 形状：

```json
{
  "parentId": "<目标目录id>",
  "name": "P3 Life-Event Coordinator | smoke-r1 | 20260824",
  "type": "advanced",
  "intro": "<应用介绍>",
  "avatar": "/imgs/avatar/brightBlue.svg",
  "modules": [ "<节点数组>" ],
  "edges": [ "<边数组>" ],
  "chatConfig": { }
}
```

**陷阱（实测）**：
- **工作流节点必须放 `modules` 键**。第 1 次尝试用 `nodes` 提交：返回 200 且 edges（33 条）落库，但 `modules` 为空（空画布）——未知字段被静默忽略。
- 成功判据不是 HTTP 200，而是 `GET app/detail` 回读 `modules` 数量 > 0。
- 失败半成品要用 `DELETE /api/core/app/del` 清理（先 detail 核对归属）。

### 2.2 app/detail

```
GET /api/core/app/detail?appId=<id>
```

- query 传参。响应 `data.modules` 即节点数组、`data.edges` 即边、`data.chatConfig` 即聊天配置。
- 服务端良性规范化（与导入件比对）：丢弃 `inputs[].selectedType`（前端渲染提示）、向 datasetSearch 的 datasets 值回填 avatar/name/vectorModel、chatConfig 补 `_id`。
- chatTest 请使用 detail 回读的 modules/edges/chatConfig（服务端规范化形态最稳）。

### 2.3 app/update

```
PUT /api/core/app/update?appId=<id>
```

实测 body 形状：

```json
{
  "name": "<detail 回读的现名，保持不变>",
  "type": "advanced",
  "intro": "<intro>",
  "avatar": "<detail 回读>",
  "nodes": [ "<节点数组>" ],
  "edges": [ "<边数组>" ],
  "chatConfig": { }
}
```

**陷阱（实测）**：**update 的节点数组必须放 body 键 `nodes`；放 `modules` 会被静默忽略**（返回 200 无报错，detail 回读无变化）。与 create 恰好相反（create 必须 `modules`）。

### 2.4 app/list

```
POST /api/core/app/list
{ "parentId": "<目录id>" }
```

**陷阱（实测）**：POST body 传 parentId 才生效。用 GET＋query 传 parentId 会被忽略并返回一份全局默认列表（实测恒 123 条、不含新建应用），易误判「创建失败」。目录核验必须 POST。

### 2.5 app/del

```
DELETE /api/core/app/del?appId=<id>
```

- 路径是 **`del` 不是 `delete`**；id 走 query。
- 删除前必须 detail 核对 name＋parentId（见安全边界）。

> 2.6–2.9 为 **2026-11-02 于 `https://agents.sangfor.com`（FastGPT 4.14.11）实测**，来源：`02-客户与机会/DAPENBUN/PoC/Agent/2026-11-02-骨架验收记录.md` 及当次会话的接口探测输出。

### 2.6 app/folder/create

```
POST /api/core/app/folder/create
{ "parentId": null, "name": "<目录名>", "intro": "<介绍>", "type": "folder" }
```

**陷阱（实测）**：
- **必须带 `type: "folder"`**。只带 name/intro（或再加 avatar）返回 500 `missingParams`。
- 成功时 `data` 为 `null`，不返回新目录 id；用 `POST app/list`（body 传同一 parentId）按 name 找回 `_id`，该项 `type` 为 `folder`。
- `parentId: null` 即根目录。

### 2.7 tool/getSystemToolTemplates

```
GET  /api/core/app/tool/getSystemToolTemplates                → 顶层系统工具与工具集
POST /api/core/app/tool/getSystemToolTemplates
{ "parentId": "<工具集 id，如 systemTool-dbops>" }            → 该工具集的子工具
```

- 每项有 `id`、`name`、`intro`。顶层 id 形如 `systemTool-<名称>`；子工具 id 形如 `systemTool-<工具集>/<子工具>`（如 `systemTool-dbops/oracle`）。
- **陷阱（实测）**：GET＋query 传 `parentId` 被忽略，仍返回顶层列表（与 app/list 同理）；取子工具必须 POST body。

### 2.8 tool/getPreviewNode（取工具节点模板）

```
GET /api/core/app/tool/getPreviewNode?appId=<工具 id，如 systemTool-dbops/oracle>
```

- `data` 即工作流 `tool` 节点模板：`flowNodeType: "tool"`、`pluginId`、`toolConfig.systemTool.toolId`、`inputs`（含隐藏的 `system_input_config` 及其 `inputList`，以及工具参数）、`outputs`（结果＋`system_error_text`）、`catchError: false`；**没有 `system_toolData`**。
- 复制后补 `nodeId`/`name`/`position` 并填输入值；`system_input_config` 的值形状、口令处理和错误捕获出口见 `fastgpt-json-agent-creator` 的 `references/generation-guide.md`（Dynamic target-bound nodes）。
- `/api/core/app/plugin/getPreviewNode` 在本环境 404。

### 2.9 getInitData（取模型内部 ID）

```
GET /api/common/system/getInitData
```

- `data.systemVersion` 为 FastGPT 版本；`data.activeModelList[]` 为已启用模型，每项含 `id`、`model`、`name`、`provider`、`type`。
- **陷阱（实测）**：工作流节点的 `modelId` 填 `id`（平台内部 ID），不是 `model`/`name`；填模型名时 `app/create` 返回 500 `common:error.model_not_exist`（statusText `unExistModel`）。

## 3. 知识库与数据（dataset）

### 3.1 dataset/create — ⚠ 未在本环境实测

开源 FastGPT 惯例形状（供首次使用时小样验证，勿直接当实测结论）：

```
POST /api/core/dataset/create
{ "parentId": null, "name": "<名称>", "intro": "<介绍>", "type": "dataset", "vectorModel": "<向量模型id>", "agentModel": "<模型id>" }
```

实测会话中 4 个 DEV 数据集为既有对象（CitizenServiceKB-DEV 等），其创建调用未记录在验收报告中。首次使用先建一个可清理的小数据集验证形状，再正式使用。

### 3.2 collection/create/text（建 collection，两段式装载第 1 步）

```
POST /api/core/dataset/collection/create/text
```

```json
{
  "datasetId": "<数据集ID>",
  "parentId": null,
  "name": "01-Life-Event-Service-Catalog",
  "text": "<文件头部块：# 标题 + > 元信息 + Purpose 段>",
  "trainingType": "chunk",
  "chunkSize": 512,
  "chunkSplitter": "\n\n",
  "qaPrompt": "",
  "metadata": {}
}
```

返回 `data.collectionId`。

**陷阱（实测）**：
- `text` 即入库原文（不是 rawText），**参与全库内容查重**：完全相同文本返回 500 `fileContentDuplicate`。每个文件的 text 必须唯一（用各自头部块天然唯一）。
- 服务端自动在 name 后追加 `.txt`。
- 返回的 `data.results.insertLen` **不可信**（本环境恒报 0，实际训练异步完成）。

### 3.3 data/insertData（逐块推数据，两段式装载第 2 步）

```
POST /api/core/dataset/data/insertData
```

```json
{ "collectionId": "<collectionId>", "q": "<整块文本>", "a": "", "indexes": [] }
```

返回 `data` 为该数据条 ID（字符串）。

**行为（实测）**：`q` 原样入库为**一个 chunk**，服务端不再二次切分；chunk 模式 `a` 传空串。分块完全由客户端决定（推荐按 `## ` 小节确定性分块，规则/条件/例外完整同块、绝不跨块拆分）。

### 3.4 collection/listV2

```
POST /api/core/dataset/collection/listV2
{ "datasetId": "<id>", "parentId": null, "offset": 0, "pageSize": 20, "searchText": "" }
```

### 3.5 collection/detail

```
GET /api/core/dataset/collection/detail?id=<cid>
```

- **query 传参**。
- 用途：装载后就绪判定与删除前核对。就绪判据：轮询至 `dataAmount` 达预期且 `trainingAmount = 0`、`hasError = false`（实测批量 <1 分钟完成）。

### 3.6 data/list

```
POST /api/core/dataset/data/list
{ "datasetId": "<id>", "collectionId": "<cid>", "offset": 0, "pageSize": 50, "searchText": "" }
```

用途：装载后与本地期望块集合做逐字比对（exact-match）。

### 3.7 dataset/searchTest

```
POST /api/core/dataset/searchTest
{ "datasetId": "<id>", "text": "<query>" }
```

响应 `data.list[]`，每项含 `sourceName` / `q` / `score`（embedding 相似度）。

### 3.8 collection/delete

```
POST /api/core/dataset/collection/delete?id=<cid>
Body: {}
```

**陷阱（实测）**：**id 在 query**，body 为空对象。body 里传 `id` 或 `collectionId` 均报 500 `missingParams`。注意此处是 POST 不是 DELETE。

### 3.9 data/delete

```
DELETE /api/core/dataset/data/delete?id=<dataId>
```

**陷阱（实测）**：DELETE＋query 传参；JSON body 传 id 报 400。

## 4. chatTest（带画布调试）

```
POST /api/core/chat/chatTest
```

```json
{
  "appId": "<appId>",
  "appName": "<应用名 + 测试>",
  "chatId": "<自造唯一串>",
  "variables": {},
  "messages": [{ "role": "user", "content": "<文本>" }],
  "nodes": [ "<detail 回读的 modules>" ],
  "edges": [ "<detail 回读的 edges>" ],
  "chatConfig": { "<detail 回读的 chatConfig>" },
  "responseChatItemId": null,
  "stream": true,
  "detail": true
}
```

**要求（实测，前端调试聊天即如此）**：
- 本构建要求把**完整工作流随请求传入**（nodes/edges/chatConfig/appName 四者必带，zod 校验报错会列出）；仅给 appId 会 400。
- messages 是 **`role` 判别的 `{role, content}`**（OpenAI 风格）；用 `{obj:"Human", value:...}` 报 400 zod `No matching discriminator` at `messages.0.role`。
- 响应为 SSE：
  - **正文在 `event: fastAnswer` 的 `data.choices[0].delta.content`**（累加）；
  - `event: answer` 只承载 finish 帧与 `[DONE]`；
  - `event: flowNodeResponse` 给逐节点执行痕迹（extractResult、quoteList、finishReason、reasoningText、runningTime 等）；
  - `event: interactive` 下发 userSelect（含选项列表）；
  - `event: workflowDuration` 给 `durationSeconds`。
- **多轮续聊**：同 chatId 再次调用即可；请求只带最新一条消息。
- **userSelect 点选**：把挂起选项的 value 作为同 chatId 的下一条消息发送，服务端识别挂起的 userSelect 并路由到确认分支（实测 0.26s 完成）。

## 5. 画布构建行为陷阱（实测汇总）

1. **ifElse 相等算子**：本构建枚举为 `equalTo / notEqual / isEmpty / isNotEmpty / include / notInclude / startWith / endWith / greaterThan(greaterThanOrEqualTo) / lessThan(lessThanOrEqualTo) / reg`。用 `isEqual` **静默判假**（返回 200，条件恒走 else）——route_ok="ok" 仍落兜底的直接根因。
2. **推理型模型正文通道可能为空**：
   - finishReason=length：中文推理耗尽 maxToken（600 token 全耗在 reasoning，正文 0 字）→ 规划类节点 maxToken ≥2400、能力类 ≥1200。
   - finishReason=stop 但结论留在 reasoningText、answerText 仍为 null → 下游解析节点要同时引用 answerText＋reasoningText 兜底。
   - SSE 事件里的 answerText 字段恒为 null 属显示层假象，引用通道有值。
3. **偶发长停顿**：重载轮 2 次读超时（300s/600s 各一次），同请求重试即成功（62.11s / 31.17s）。疑模型侧排队；演示前预热或对重载轮保留重试。
4. **contentExtract 非确定性漏抽**：requested_services 三类只抽一类、lease_status 明示仍漏（3 次观测 2 次漏）。对策：解析节点做关键词补判（school/enroll/child/education→education；water/electricity/gas/utility/utilities/health/community/local service→local_public_services；resid/lease/register/moved→residence）。
5. **代码节点 JSON 提取**：推理文本常先回显 schema 再输出最终 JSON，贪心 `{...}` 会裹入散文；先贪心匹配，失败后从串尾向前做配平大括号扫描。
6. **insertLen 恒 0**：见 §3.2，以 collection 详情为准。
7. **规划器判断波动**：协调性问句（如 "Can it be done in one trip?"）偶发被误判 unclassifiable → ROUTE-04；系统提示词中应声明「已列事项的顺序/合并/一次办结问句不是服务事项」。

## 6. 前端逆向方法论（找新接口）

1. `GET /_next/static/<buildId>/_buildManifest.js` 列出全部 chunk。
2. 扫 chunk 内 `/api/...` 字符串与调用点上下文（例：app/detail 页 chunk 的 API 模块给出 `create=POST`、`del=DELETE+query`、`update=PUT`、`list=POST`；chatTest 调用点在 `40581.*.js` module 91620）。
3. HTTP 封装（参数位置规则）在 `pages/_app-*.js` module 18992。

## 7. 安全边界（同 SKILL.md §1）

- 只在调用方给定 parentId 内新建/修改；绝不修改/删除他人应用与知识库。
- 删除任何对象前必须 detail 核对 name＋parentId/创建时间，全部匹配才执行。
- 失败创建物清理仅限当次自建。
- 凭据只放环境变量或 /tmp 临时文件（chmod 600），绝不写进项目文件、报告或技能文档。
