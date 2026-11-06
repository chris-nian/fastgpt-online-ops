---
name: fastgpt-online-ops
description: Log into a SANGFOR Agent Builder (FastGPT commercial) online environment over REST and perform authorized API debugging - list/create/update/delete apps, edit the workflow canvas, create datasets, load chunked data, run searchTest and SSE chatTest smoke runs. Use when the user provides an environment base URL plus account credentials and asks for Agent Builder online operations, knowledge-base loading, app lifecycle via API, or canvas-driven chat testing.
---

# SANGFOR Agent Builder（FastGPT 商业版）线上环境 API 操作

拿到「环境地址＋账号密码」后，用纯 API（无浏览器）登录目标环境并完成应用与知识库的调试测试。输入：base URL、账号、密码（或既有 token）、调用方授权的目录 parentId 与写操作边界。输出：操作结果与证据、清理记录。本技能只沉淀登录协议、API 约定与陷阱清单；业务事实回权威业务源，冲突时停止写入并报告。

## 1. 安全边界（硬规则，先于一切操作）

- 凭据只放环境变量或 /tmp 临时文件（chmod 600）；绝不写进项目文件、报告、技能文档或聊天输出。
- 只在调用方明确给定的 parentId 目录内新建/修改对象。
- 绝不修改/删除他人应用与知识库；对不确定归属的对象先 detail 核对。
- 删除任何对象前必须 detail 核对 name＋parentId/创建时间，全部匹配才执行。
- 失败创建物的清理仅限当次自建对象。
- 未经授权不做任何写操作；首次接入一个新环境时只跑 login/list/detail 只读验证。

## 2. 登录协议（逐步）

Python 需 `ssl._create_unverified_context()`（私有 CA）。逐步：

1. `GET /api/support/user/account/preLogin?username=<url编码账号>` → 取 `data.code`。
2. `POST /api/support/user/account/loginByPassword`，JSON body：`{"username": <账号>, "password": <sha256hex(原始密码)>, "code": <上一步 code>, "language": "zh-CN"}` → 响应 data 中取 token。
3. 后续所有请求带请求头 **`token: <值>`**（不是 Authorization、不是 Bearer）。
4. 401/403 时不报错中断，重新执行 1–2 重登录后重放原请求。

## 3. 通用约定

- 响应统一 `{code, data, message}`，业务成功判定 `code == 200`（HTTP 同为 200）。
- 参数位置由前端 HTTP 封装决定：**GET/DELETE 走 URL query，POST/PUT 走 JSON body**。删类接口在 body 传 id 会 500（collection）或 400（data）。
- `create/text` 返回的 `insertLen` 恒为 0 不代表失败：训练异步，以 collection 详情 `dataAmount/trainingAmount/hasError` 为准（实测批量 <1 分钟完成）。

## 4. API 速查表（实测形状；逐端点完整版见 references/api-reference.md）

| 能力 | 调用形状 | 关键陷阱 |
|---|---|---|
| 登录 | 见 §2 | token 放 `token:` 头 |
| 应用列表 | `POST /api/core/app/list` body `{"parentId":"<目录id>"}` | GET＋query 传 parentId 会被忽略，返回全局默认列表（不可用于目录核验） |
| 应用详情 | `GET /api/core/app/detail?appId=<id>` | 节点在 `data.modules`、边在 `data.edges`；chatTest 用 detail 回读形态最稳 |
| 建应用 | `POST /api/core/app/create`，节点数组放 **`modules`** 键 | 放 `nodes` 被静默忽略 → 返回 200 但空画布；成功判据＝detail 回读 modules 数 >0 |
| 改应用 | `PUT /api/core/app/update?appId=<id>`，节点数组放 **`nodes`** 键 | 放 `modules` 被静默忽略（与 create 恰好相反） |
| 删应用 | `DELETE /api/core/app/del?appId=<id>`（是 `del` 不是 `delete`） | 删前 detail 核对 name＋parentId |
| 建目录 | `POST /api/core/app/folder/create` body `{parentId, name, intro, type:"folder"}` | 缺 `type:"folder"` 报 500 missingParams；成功 data 为 null，用 app/list 按 name 找 id |
| 系统工具列表 | `GET /api/core/app/tool/getSystemToolTemplates`；子工具 `POST` body `{parentId:"<工具集id>"}` | GET＋query 的 parentId 被忽略 |
| 工具节点模板 | `GET /api/core/app/tool/getPreviewNode?appId=<工具id>` | 返回即 `tool` 节点；无 `system_toolData`，绑定在 `toolConfig.systemTool.toolId` |
| 模型内部 ID | `GET /api/common/system/getInitData` → `activeModelList[].id` | `modelId` 填模型名时 app/create 报 `model_not_exist` |
| 建数据集 | `POST /api/core/dataset/create` | 本端点形状未在本环境实测（开源惯例，见 api-reference 标注），首次使用先小样验证 |
| 建 collection | `POST /api/core/dataset/collection/create/text` body `{datasetId, parentId:null, name, text, trainingType:"chunk", chunkSize, chunkSplitter, qaPrompt:"", metadata:{}}` | `text` 参与全库查重（重复文本 500 `fileContentDuplicate`）；name 自动追加 `.txt` |
| 灌数据 | `POST /api/core/dataset/data/insertData` body `{collectionId, q, a:"", indexes:[]}` | 一条一 chunk，服务端不再二次切分；分块由客户端决定 |
| collection 列表 | `POST /api/core/dataset/collection/listV2` body `{datasetId, parentId:null, offset, pageSize, searchText}` | |
| collection 详情 | `GET /api/core/dataset/collection/detail?id=<cid>` | 轮询 dataAmount/trainingAmount/hasError 判就绪 |
| 数据条列表 | `POST /api/core/dataset/data/list` body `{datasetId, collectionId, offset, pageSize, searchText}` | |
| 检索测试 | `POST /api/core/dataset/searchTest` body `{datasetId, text}` → `data.list[]`（sourceName/q/score） | |
| 删 collection | `POST /api/core/dataset/collection/delete?id=<cid>`，body `{}` | **id 在 query**；body 传 id/collectionId 报 500 missingParams |
| 删数据条 | `DELETE /api/core/dataset/data/delete?id=<dataId>` | DELETE＋query；body 传 id 报 400 |
| 对话调试 | `POST /api/core/chat/chatTest` | 请求必须带 `nodes/edges/chatConfig/appName`（带画布调试语义，仅给 appId 会 400）；messages 用 `{role:"user", content}`；SSE 正文在 `event: fastAnswer` 的 `choices[0].delta.content`；多轮续聊＝同 chatId 再调；userSelect 点选＝同 chatId 下一条消息直接发选项 value |

## 5. 目标构建行为陷阱清单（画布调试必读）

- ifElse 相等算子是 **`equalTo`**；`isEqual` 静默判假（不报错、走 else）。算子枚举：equalTo/notEqual/isEmpty/isNotEmpty/include/notInclude/startWith/endWith/greaterThan(greaterThanOrEqualTo)/lessThan(lessThanOrEqualTo)/reg。
- 推理型模型下 chatNode 的 `answerText` 可能为空而结论在 `reasoningText`（即使 finishReason=stop）。maxToken 给足：规划类 ≥2400、能力类 ≥1200；下游解析节点同时引用 answerText＋reasoningText 兜底。
- SSE 事件里的 answerText 字段恒为 null 属显示层假象，节点输出引用通道有值。
- 偶发 >300s 停顿（疑模型侧排队）：同请求重试即过；重载轮保留重试与长读超时（600s）。
- contentExtract 类目/字段非确定性漏抽：先用确定性关键词补判兜底，few-shot 列全字段示例为二线。
- 代码节点提取 JSON：先贪心 `{...}`，失败后从串尾向前做配平大括号扫描（推理文本常先回显 schema 再输出最终 JSON）。

## 6. 客户端脚本

`scripts/ab_client.py`（零第三方依赖，仅 urllib/ssl/hashlib）封装以上全部协议：自动重登录、query/body 分流、token 头、SSE 解析、collection 就绪轮询。写操作（create/update/delete/insert）只通过 Python API 暴露；CLI 子命令仅 login/list-apps/app-detail/search-test/chat（只读＋调试）。

```bash
python3 scripts/ab_client.py login --base https://<host>:<port> --user <账号> --pass-file /tmp/creds.env --pass-key FG_PASS
python3 scripts/ab_client.py list-apps --base https://<host>:<port> --folder <parentId>
python3 scripts/ab_client.py app-detail --base https://<host>:<port> --app-id <appId>
python3 scripts/ab_client.py search-test --base https://<host>:<port> --dataset-id <id> --text "<query>"
python3 scripts/ab_client.py chat --base https://<host>:<port> --app-id <appId> --chat-id <唯一串> --canvas-file <detail回读.json> --message "<文本>"
```

Python 用法：`ABClient(base, user, password)` → `.login()` → `.list_apps(parent_id)` / `.get_app(app_id)` / `.create_app(parent_id, name, modules, edges, chat_config, ...)`（自动放 modules 键）/ `.update_app(app_id, nodes, edges, chat_config, name=...)`（自动放 nodes 键）/ `.delete_app(app_id)` / `.create_dataset(...)` / `.create_text_collection(...)` / `.insert_chunk(...)` / `.wait_collection_ready(...)` / `.search_test(...)` / `.chat_test(app_id, chat_id, canvas, messages)`（canvas 接受 detail 回读 dict，modules/nodes 键都认；返回全文、逐节点痕迹、interactive、耗时）。

## 7. 前端逆向方法论（找新接口时）

1. `GET /_next/static/<buildId>/_buildManifest.js` 列出全部 chunk 路径。
2. 下载相关 chunk（如 app/detail 页 chunk、`pages/_app-*.js`），扫 `/api/...` 字符串及调用点上下文，确认 method 与参数位置（query 还是 body）。
3. HTTP 封装在 `pages/_app` chunk 的 module 18992：GET/DELETE→query，POST/PUT→body——删类接口「body 传参报错」的根因。

## 8. 参考路由

- 逐端点完整 API 参考（payload、成功/失败响应示例、陷阱备注）：`references/api-reference.md`
- 客户端与 CLI：`scripts/ab_client.py`
- 离线自测：`tests/test_ab_client_offline.py`

按需读取；速查表够用时不加载参考文件。

## 9. 验收

- 结构：frontmatter 仅 name/description；仓库无官方 `quick_validate.py` 时按技能开发说明手工核验结构与链接。
- 脚本：`python3 tests/test_ab_client_offline.py` 全绿（离线、不联网）。
- 线上：新环境首次接入只跑 login＋list＋detail 只读验证，不做任何写操作。
