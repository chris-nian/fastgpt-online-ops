#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ab_client.py — SANGFOR Agent Builder（FastGPT 商业版）线上环境 API 客户端。

零第三方依赖（仅标准库 urllib/ssl/hashlib/json）。

凭据纪律（硬规则）：
- 密码只从环境变量（--pass-env VAR）或临时文件（--pass-file PATH，建议 chmod 600 放 /tmp）读入；
- token 缓存文件默认 /tmp/ab_client_token.txt，写入时自动 chmod 600；
- 绝不把密码/token 写进项目文件、日志或报告。

参数位置约定（前端 module 18992 HTTP 封装行为，实测）：
- GET/DELETE：参数放 URL query；
- POST/PUT：参数默认放 JSON body；但 update?appId=、collection/delete?id= 等端点
  要求 id 走 query——显式传 query 时无论方法一律拼入 URL（body 仍按 JSON 发）。
删类接口在 body 传 id 会 500（collection）/400（data）。

安全边界：
- CLI 只提供 login / list-apps / app-detail / search-test / chat 子命令（只读＋画布调试）；
- 创建/更新/删除等写操作只通过 Python API 暴露（见下文方法），调用方必须先取得
  授权并遵守 SKILL.md 安全边界（只在给定 parentId 内操作、删前 detail 核对、
  清理仅限当次自建）。

用法示例：
    python3 ab_client.py login --base https://host:17443 --user u --pass-file /tmp/creds.env --pass-key FG_PASS
    python3 ab_client.py list-apps --base https://host:17443 --folder <parentId>
    python3 ab_client.py app-detail --base https://host:17443 --app-id <appId>
    python3 ab_client.py search-test --base https://host:17443 --dataset-id <id> --text "query"
    python3 ab_client.py chat --base https://host:17443 --app-id <id> --chat-id c1 \\
        --canvas-file detail.json --message "hello"
"""

import argparse
import hashlib
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


class ABError(Exception):
    """API 调用失败（HTTP 非 200 或业务 code 非 200）。"""


def sha256_hex(text):
    """登录协议要求：password 为原始密码的 sha256 十六进制。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_pass_file(path, key=None):
    """读密码文件。支持两种形态：
    1. KEY=VALUE 多行 env 文件：必须给 --pass-key（或文件里含 FG_PASS/PASSWORD/PASS 之一）；
    2. 单行明文密码文件：整行即密码（含 '=' 的明文密码不受支持，请改用 env 文件）。
    """
    with open(path, "r", encoding="utf-8") as f:
        content = f.read().strip()
    is_env_file = any(
        re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", line.strip())
        for line in content.splitlines())
    if is_env_file:
        env = {}
        for line in content.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
        if key:
            if key not in env:
                raise ABError("密码文件 %s 中没有键 %s" % (path, key))
            return env[key]
        for candidate in ("FG_PASS", "PASSWORD", "PASS"):
            if candidate in env:
                return env[candidate]
        raise ABError("密码文件 %s 是 env 格式但未找到密码键；用 --pass-key 指定" % path)
    return content.splitlines()[0].strip()


def new_sse_result():
    return {"text": "", "events": [], "interactive": [], "node_traces": [],
            "workflow_duration": None}


def build_chat_payload(app_id, chat_id, canvas, messages):
    """构造 chatTest 请求体。canvas 接受 detail 回读 dict（modules 键）或显式 nodes 键。"""
    nodes = canvas.get("nodes") or canvas.get("modules")
    if not nodes:
        raise ABError("canvas 中既无 nodes 也无 modules；chatTest 必须带画布")
    norm_messages = []
    for m in messages:
        if isinstance(m, dict):
            norm_messages.append({"role": m.get("role", "user"),
                                  "content": m.get("content", "")})
        else:
            norm_messages.append({"role": "user", "content": str(m)})
    return {
        "appId": app_id,
        "appName": canvas.get("appName") or canvas.get("name") or ("app-%s 测试" % app_id),
        "chatId": chat_id,
        "variables": canvas.get("variables") or {},
        "messages": norm_messages,
        "nodes": nodes,
        "edges": canvas.get("edges") or [],
        "chatConfig": canvas.get("chatConfig") or {},
        "responseChatItemId": None,
        "stream": True,
        "detail": True,
    }


class ABClient:
    """SANGFOR Agent Builder 线上环境客户端。

    :param base: 环境地址，如 https://host:17443
    :param user: 账号（用于登录与自动重登录）
    :param password: 原始密码（内部只做 sha256，不落盘）
    :param token: 既有 token（与 user/password 二选一或同时给）
    :param token_file: token 缓存文件；login 成功后写入并 chmod 600
    :param timeout: 单请求读超时秒数（重载画布偶发 >300s 停顿，默认给足 600）
    """

    def __init__(self, base, user=None, password=None, token=None,
                 token_file="/tmp/ab_client_token.txt", timeout=600.0):
        self.base = base.rstrip("/")
        self.user = user
        self.password = password
        self.token = token
        self.token_file = token_file
        self.timeout = float(timeout)
        self._ctx = ssl._create_unverified_context()  # 私有 CA，实测必需

    # ---------- 低层 ----------

    def _http(self, method, url, body_bytes=None, headers=None):
        req = urllib.request.Request(url, data=body_bytes, method=method,
                                     headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self._ctx) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")

    def _request(self, method, path, query=None, body=None, with_token=True):
        url = self.base + path
        headers = {"Content-Type": "application/json"}
        if with_token and self.token:
            headers["token"] = self.token  # 是 token 头，不是 Authorization（实测）
        data = None
        # 带 query 时无论方法一律拼入 URL：PUT update?appId=、POST collection/delete?id=
        # 等端点要求 id 走 query（曾只对 GET/DELETE 拼接，PUT/POST 静默丢弃致 appUnExist）。
        if query:
            url += "?" + urllib.parse.urlencode(query)
        if method.upper() not in ("GET", "DELETE"):
            data = json.dumps(body if body is not None else {}, ensure_ascii=False).encode("utf-8")
        status, raw = self._http(method.upper(), url, data, headers)
        try:
            parsed = json.loads(raw) if raw else None
        except ValueError:
            parsed = None
        return status, parsed, raw

    @staticmethod
    def _ensure_ok(status, parsed, raw, what):
        if parsed is None:
            raise ABError("%s：HTTP %s，响应非 JSON：%s" % (what, status, raw[:300]))
        code = parsed.get("code")
        if status != 200 or code != 200:
            raise ABError("%s：HTTP %s code=%s message=%s raw=%s" % (
                what, status, code, parsed.get("message"), raw[:300]))

    def call(self, method, path, query=None, body=None):
        """统一入口：GET/DELETE 参数进 query，POST/PUT 进 body；401/403 自动重登录一次。"""
        status, raw_parsed, raw = self._request(method, path, query=query, body=body)
        expired = status in (401, 403) or (
            isinstance(raw_parsed, dict) and raw_parsed.get("code") in (401, 403))
        if expired:
            if not (self.user and self.password is not None):
                raise ABError("%s %s：token 失效（401/403）且未提供账号密码，无法重登录" % (method, path))
            self.login()
            status, raw_parsed, raw = self._request(method, path, query=query, body=body)
        self._ensure_ok(status, raw_parsed, raw, "%s %s" % (method, path))
        return (raw_parsed or {}).get("data")

    # ---------- 登录 ----------

    def login(self):
        """preLogin → loginByPassword；成功后缓存 token（chmod 600）。"""
        if not self.user or self.password is None:
            raise ABError("缺少账号或密码，无法登录")
        status, parsed, raw = self._request(
            "GET", "/api/support/user/account/preLogin",
            query={"username": self.user}, with_token=False)
        self._ensure_ok(status, parsed, raw, "preLogin")
        data = (parsed or {}).get("data") or {}
        code = data.get("code") if isinstance(data, dict) else data
        body = {"username": self.user, "password": sha256_hex(self.password),
                "code": code, "language": "zh-CN"}
        status, parsed, raw = self._request(
            "POST", "/api/support/user/account/loginByPassword", body=body, with_token=False)
        self._ensure_ok(status, parsed, raw, "loginByPassword")
        data = (parsed or {}).get("data")
        token = data.get("token") if isinstance(data, dict) else data
        if not token or not isinstance(token, str):
            raise ABError("loginByPassword 未返回 token：%s" % raw[:200])
        self.token = token
        if self.token_file:
            with open(self.token_file, "w", encoding="utf-8") as f:
                f.write(token)
            os.chmod(self.token_file, 0o600)
        return token

    def load_cached_token(self):
        if self.token_file and os.path.exists(self.token_file):
            try:
                with open(self.token_file, "r", encoding="utf-8") as f:
                    t = f.read().strip()
                if t:
                    self.token = t
            except OSError:
                pass
        return self.token

    # ---------- 应用（写操作须先取得授权，见模块 docstring） ----------

    def list_apps(self, parent_id):
        """目录列表。必须 POST body 传 parentId（GET+query 被忽略，实测）。"""
        return self.call("POST", "/api/core/app/list", body={"parentId": parent_id})

    def get_app(self, app_id):
        """详情：data.modules=节点、data.edges=边、data.chatConfig=聊天配置。"""
        return self.call("GET", "/api/core/app/detail", query={"appId": app_id})

    def create_app(self, parent_id, name, modules, edges, chat_config,
                   app_type="advanced", intro="", avatar="/imgs/avatar/brightBlue.svg"):
        """建应用：节点数组必须放 modules 键（nodes 被静默忽略成空画布，实测）。
        成功判据：调用方随后 get_app 回读 modules 数 > 0。"""
        body = {"parentId": parent_id, "name": name, "type": app_type,
                "intro": intro, "avatar": avatar,
                "modules": modules, "edges": edges, "chatConfig": chat_config}
        return self.call("POST", "/api/core/app/create", body=body)

    def update_app(self, app_id, nodes, edges, chat_config, name,
                   app_type="advanced", intro="", avatar=None):
        """原地更新：PUT ?appId=，节点数组必须放 nodes 键（modules 被静默忽略，与 create 相反，实测）。
        name 建议传 detail 回读的现名保持不变；avatar 缺省时由调用方从 detail 回读传入。"""
        body = {"name": name, "type": app_type, "intro": intro,
                "nodes": nodes, "edges": edges, "chatConfig": chat_config}
        if avatar:
            body["avatar"] = avatar
        return self.call("PUT", "/api/core/app/update", query={"appId": app_id}, body=body)

    def delete_app(self, app_id):
        """删应用：DELETE /del?appId=（路径是 del 不是 delete）。删前必须 detail 核对归属。"""
        return self.call("DELETE", "/api/core/app/del", query={"appId": app_id})

    def create_folder(self, parent_id, name, intro=""):
        """建应用目录：body 必须带 type="folder"（缺它 500 missingParams，11-02 实测）。
        成功时 data 为 null、不回传 id；调用方随后 list_apps(parent_id) 按 name 找回 _id。"""
        body = {"parentId": parent_id, "name": name, "intro": intro, "type": "folder"}
        return self.call("POST", "/api/core/app/folder/create", body=body)

    # ---------- 系统工具（只读） ----------

    def list_system_tools(self, parent_id=None):
        """系统工具模板：不给 parent_id 取顶层（GET）；给工具集 id 取其子工具，必须 POST body
        （GET＋query 的 parentId 被忽略、仍回顶层，11-02 实测）。"""
        if parent_id is None:
            return self.call("GET", "/api/core/app/tool/getSystemToolTemplates")
        return self.call("POST", "/api/core/app/tool/getSystemToolTemplates",
                         body={"parentId": parent_id})

    def get_tool_preview_node(self, tool_id):
        """工具节点模板（GET ?appId=<工具 id>，如 systemTool-dbops/oracle），可直接作工作流 tool 节点。"""
        return self.call("GET", "/api/core/app/tool/getPreviewNode", query={"appId": tool_id})

    # ---------- 知识库与数据 ----------

    def create_dataset(self, name, parent_id=None, intro="", vector_model=None, agent_model=None):
        """建数据集。⚠ 该端点形状未在本环境实测（开源惯例），首次使用先小样验证。"""
        body = {"parentId": parent_id, "name": name, "intro": intro, "type": "dataset"}
        if vector_model:
            body["vectorModel"] = vector_model
        if agent_model:
            body["agentModel"] = agent_model
        return self.call("POST", "/api/core/dataset/create", body=body)

    def create_text_collection(self, dataset_id, name, text, training_type="chunk",
                               chunk_size=512, chunk_splitter="\n\n", parent_id=None,
                               qa_prompt="", metadata=None):
        """建 collection（两段式第 1 步）。text 参与全库查重（重复文本 500 fileContentDuplicate），
        每个文件的 text 必须唯一；name 会被服务端追加 .txt；返回 data.collectionId。
        insertLen 恒 0 不可信，就绪以 wait_collection_ready 为准。"""
        body = {"datasetId": dataset_id, "parentId": parent_id, "name": name,
                "text": text, "trainingType": training_type, "chunkSize": chunk_size,
                "chunkSplitter": chunk_splitter, "qaPrompt": qa_prompt,
                "metadata": metadata or {}}
        return self.call("POST", "/api/core/dataset/collection/create/text", body=body)

    def insert_chunk(self, collection_id, q, a="", indexes=None):
        """逐块推数据（两段式第 2 步）：一条一 chunk，服务端不再切分。返回数据条 ID。"""
        body = {"collectionId": collection_id, "q": q, "a": a, "indexes": indexes or []}
        return self.call("POST", "/api/core/dataset/data/insertData", body=body)

    def list_collections(self, dataset_id, parent_id=None, offset=0, page_size=20, search_text=""):
        return self.call("POST", "/api/core/dataset/collection/listV2", body={
            "datasetId": dataset_id, "parentId": parent_id, "offset": offset,
            "pageSize": page_size, "searchText": search_text})

    def get_collection_detail(self, collection_id):
        """collection 详情（GET+query）。就绪判据：dataAmount 达标且 trainingAmount=0、hasError=false。"""
        return self.call("GET", "/api/core/dataset/collection/detail",
                         query={"id": collection_id})

    def wait_collection_ready(self, collection_id, expected_amount=None,
                              timeout=300.0, poll_interval=3.0):
        """轮询 collection 详情直至训练完成。返回最终详情 data。"""
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            last = self.get_collection_detail(collection_id)
            amount = last.get("dataAmount")
            training = last.get("trainingAmount")
            has_error = last.get("hasError")
            ok_amount = (expected_amount is None) or (amount or 0) >= expected_amount
            if ok_amount and not training and not has_error:
                return last
            time.sleep(poll_interval)
        raise ABError("collection %s 训练未在 %.0fs 内就绪：%s" % (
            collection_id, timeout, json.dumps(last or {}, ensure_ascii=False)[:300]))

    def list_data(self, dataset_id, collection_id, offset=0, page_size=50, search_text=""):
        return self.call("POST", "/api/core/dataset/data/list", body={
            "datasetId": dataset_id, "collectionId": collection_id, "offset": offset,
            "pageSize": page_size, "searchText": search_text})

    def search_test(self, dataset_id, text):
        """检索测试：返回 data.list[]（sourceName/q/score）。"""
        return self.call("POST", "/api/core/dataset/searchTest",
                         body={"datasetId": dataset_id, "text": text})

    def delete_collection(self, collection_id):
        """删 collection：POST + query id，body 空（body 传 id 报 500，实测）。删前 detail 核对。"""
        return self.call("POST", "/api/core/dataset/collection/delete",
                         query={"id": collection_id}, body={})

    def delete_data(self, data_id):
        """删单条数据：DELETE + query id（body 传 id 报 400，实测）。"""
        return self.call("DELETE", "/api/core/dataset/data/delete", query={"id": data_id})

    # ---------- chatTest（带画布调试） ----------

    def chat_test(self, app_id, chat_id, canvas, messages, response_chat_item_id=None):
        """画布调试对话。

        :param canvas: dict，接受 detail 回读形态（含 modules/edges/chatConfig/name），
                       或显式 {'nodes':..., 'edges':..., 'chatConfig':..., 'appName':...}。
        :param messages: [{role:'user', content:'...'}]；多轮续聊＝同 chat_id 再调，
                         userSelect 点选＝把选项 value 作为下一条消息内容。
        :param response_chat_item_id: 可选；userSelect 恢复时传该确认交互帧的 usageId，
                       后端据此恢复挂起轮次的节点输出记忆（S5 实测：缺失时跨轮引用如
                       normalizePlan.jsonBody 在恢复轮不绑定，写节点报 Invalid JSON body）。
        :return: {'text': 全文, 'events': [...], 'interactive': [...],
                  'node_traces': [...], 'workflow_duration': 秒}
        """
        payload = build_chat_payload(app_id, chat_id, canvas, messages)
        if response_chat_item_id:
            payload["responseChatItemId"] = response_chat_item_id
        url = self.base + "/api/core/chat/chatTest"
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["token"] = self.token
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST", headers=headers)
        result = new_sse_result()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self._ctx) as resp:
                self._parse_sse_stream(resp, result)
        except urllib.error.HTTPError as e:
            raise ABError("chatTest HTTP %s：%s" % (
                e.code, e.read().decode("utf-8", "replace")[:300]))
        return result

    @staticmethod
    def _parse_sse_stream(line_iter, result):
        """解析 SSE 流。line_iter 产出 bytes 行（urllib response 或测试用 list[bytes]）。"""
        event_name = None
        for raw_line in line_iter:
            line = raw_line.decode("utf-8", "replace").rstrip("\r\n")
            if line.startswith("event:"):
                event_name = line.split(":", 1)[1].strip()
                continue
            if not line.startswith("data:"):
                continue
            data_str = line.split(":", 1)[1].strip()
            if data_str == "[DONE]":
                result["events"].append({"event": "done"})
                continue
            try:
                obj = json.loads(data_str)
            except ValueError:
                result["events"].append({"event": event_name, "raw": data_str[:200]})
                continue
            result["events"].append({"event": event_name})
            ABClient._absorb_sse_event(event_name, obj, result)

    @staticmethod
    def _absorb_sse_event(event_name, obj, result):
        if event_name == "fastAnswer":
            # 正文通道（实测）：choices[0].delta.content 逐段累加
            try:
                delta = (obj.get("choices") or [{}])[0].get("delta") or {}
                content = delta.get("content")
                if content:
                    result["_fast_answer_seen"] = True
                    result["text"] += content
            except (AttributeError, IndexError, TypeError):
                pass
        elif event_name == "answer":
            # summit 构建（09-02 实测）answer 也逐 delta 下发正文；nxy 构建仅承载 finish 帧。
            # 判据：fastAnswer 全程未出现正文时，answer 即为正文通道，持续累加（勿用 text 非空做开关，
            # 首个 delta 置非空后会把后续 delta 全吞）。
            try:
                delta = (obj.get("choices") or [{}])[0].get("delta") or {}
                content = delta.get("content")
                if isinstance(content, str) and content and not result.get("_fast_answer_seen"):
                    result["text"] += content
            except (AttributeError, IndexError, TypeError):
                pass
        elif event_name == "flowNodeResponse":
            result["node_traces"].append(obj)
        elif event_name == "interactive":
            result["interactive"].append(obj)
        elif event_name == "workflowDuration":
            try:
                result["workflow_duration"] = obj.get("durationSeconds")
            except AttributeError:
                pass


# ---------- CLI（只读＋画布调试；写操作走 Python API） ----------

def build_client(args, force_login=False):
    password = None
    if args.pass_env:
        password = os.environ.get(args.pass_env)
        if password is None:
            sys.exit("错误：环境变量 %s 未设置" % args.pass_env)
    elif args.pass_file:
        try:
            password = read_pass_file(args.pass_file, args.pass_key)
        except (OSError, ABError) as e:
            sys.exit("错误：%s" % e)
    client = ABClient(args.base, user=args.user, password=password,
                      token_file=args.token_file, timeout=args.timeout)
    if not force_login:
        client.load_cached_token()
    if not client.token:
        if password is not None and args.user:
            client.login()
        else:
            sys.exit("错误：无缓存 token 且未提供账号密码（--user + --pass-env/--pass-file）")
    return client


def cmd_login(args):
    client = ABClient(args.base, user=args.user,
                      password=(os.environ.get(args.pass_env) if args.pass_env else
                                (read_pass_file(args.pass_file, args.pass_key) if args.pass_file else None)),
                      token_file=args.token_file, timeout=args.timeout)
    token = client.login()
    print("LOGIN OK（token 已缓存 %s，长度 %d；值不回显）" % (args.token_file, len(token)))


def cmd_list_apps(args):
    client = build_client(args)
    data = client.list_apps(args.folder)
    items = data if isinstance(data, list) else (data or {}).get("list", [])
    for it in items:
        print("%s\t%s\t%s" % (it.get("_id"), it.get("type", ""), it.get("name", "")))
    print("TOTAL %d" % len(items))


def cmd_app_detail(args):
    client = build_client(args)
    d = client.get_app(args.app_id) or {}
    print("appId=%s" % d.get("_id"))
    print("name=%s" % d.get("name"))
    print("type=%s" % d.get("type"))
    print("parentId=%s" % d.get("parentId"))
    print("modules=%d" % len(d.get("modules") or []))
    print("edges=%d" % len(d.get("edges") or []))
    if args.dump_canvas_file:
        with open(args.dump_canvas_file, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
        print("canvas dumped to %s" % args.dump_canvas_file)


def cmd_search_test(args):
    client = build_client(args)
    data = client.search_test(args.dataset_id, args.text) or {}
    for i, hit in enumerate(data.get("list") or [], 1):
        print("%d.\t%s\tscore=%s" % (i, hit.get("sourceName", ""), hit.get("score", "")))
        q = (hit.get("q") or "").replace("\n", " ")
        print("   %s" % q[:160])


def cmd_chat(args):
    client = build_client(args)
    with open(args.canvas_file, "r", encoding="utf-8") as f:
        canvas = json.load(f)
    result = client.chat_test(args.app_id, args.chat_id, canvas,
                              [{"role": "user", "content": args.message}])
    print("=== TEXT ===")
    print(result["text"])
    print("=== workflowDuration=%s interactive=%d nodeTraces=%d ===" % (
        result["workflow_duration"], len(result["interactive"]), len(result["node_traces"])))


_GLOBAL_SPECS = [
    ("--base", dict(help="环境地址，如 https://host:17443（必填）")),
    ("--user", dict(help="账号")),
    ("--pass-env", dict(help="密码所在环境变量名（推荐）")),
    ("--pass-file", dict(help="密码文件路径（KEY=VALUE env 文件或单行明文，建议 chmod 600 放 /tmp）")),
    ("--pass-key", dict(help="--pass-file 为 env 文件时的密码键名，如 FG_PASS")),
    ("--token-file", dict(default="/tmp/ab_client_token.txt",
                          help="token 缓存文件（默认 /tmp/ab_client_token.txt）")),
    ("--timeout", dict(type=float, default=600.0,
                       help="单请求读超时秒数（默认 600，重载画布偶发长停顿）")),
]


def _add_globals(parser, suppressed):
    """全局参数同时挂到主解析器与各子命令，两种顺序（前后置）都可用。"""
    for flag, kw in _GLOBAL_SPECS:
        kw = dict(kw)
        if suppressed:
            kw["default"] = argparse.SUPPRESS
        parser.add_argument(flag, **kw)


def main(argv=None):
    p = argparse.ArgumentParser(description="SANGFOR Agent Builder 线上环境 API 客户端（CLI 仅只读＋画布调试）")
    _add_globals(p, suppressed=False)
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("login", help="登录并缓存 token（强制重新登录，忽略缓存）")
    _add_globals(sp, suppressed=True)
    sp.set_defaults(func=cmd_login)

    sp = sub.add_parser("list-apps", help="列出目录内应用（POST body parentId）")
    _add_globals(sp, suppressed=True)
    sp.add_argument("--folder", required=True, help="目录 parentId")
    sp.set_defaults(func=cmd_list_apps)

    sp = sub.add_parser("app-detail", help="应用详情（modules/edges 计数）")
    _add_globals(sp, suppressed=True)
    sp.add_argument("--app-id", required=True)
    sp.add_argument("--dump-canvas-file", help="可选：把 detail 全文写到此 JSON 文件，供 chat 子命令复用")
    sp.set_defaults(func=cmd_app_detail)

    sp = sub.add_parser("search-test", help="知识库检索测试")
    _add_globals(sp, suppressed=True)
    sp.add_argument("--dataset-id", required=True)
    sp.add_argument("--text", required=True)
    sp.set_defaults(func=cmd_search_test)

    sp = sub.add_parser("chat", help="chatTest 画布调试（SSE 解析为全文）")
    _add_globals(sp, suppressed=True)
    sp.add_argument("--app-id", required=True)
    sp.add_argument("--chat-id", required=True, help="自造唯一串；多轮续聊/点选复用同一值")
    sp.add_argument("--canvas-file", required=True, help="detail 回读 JSON（app-detail --dump-canvas-file 产物）")
    sp.add_argument("--message", required=True, help="本轮用户消息（userSelect 点选＝发选项 value）")
    sp.set_defaults(func=cmd_chat)

    args = p.parse_args(argv)
    if not getattr(args, "base", None):
        p.error("--base 为必填（环境地址，如 https://host:17443）")
    try:
        args.func(args)
    except ABError as e:
        sys.exit("错误：%s" % e)


if __name__ == "__main__":
    main()
