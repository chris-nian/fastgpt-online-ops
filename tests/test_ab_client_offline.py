#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线自测（不联网、不触网任何环境）。

覆盖：
1. 登录哈希（sha256hex）正确性；
2. 密码文件两种形态（env 文件 / 单行明文）解析；
3. chatTest payload 归一化（modules/nodes 键、消息形态、必备字段）；
4. SSE 解析（fastAnswer 累加、answer 兜底、flowNodeResponse/interactive/workflowDuration、[DONE]）；
5. 源码无硬编码凭据（粗检）；
6. _request 回归：PUT/POST 带 query 时参数进 URL、body 仍按 JSON 发
   （曾整段丢弃 query 致线上 update 500 appUnExist）；
7. 建目录（type="folder"）、系统工具模板（子工具走 POST body）、工具节点模板的请求形状。

运行：python3 tests/test_ab_client_offline.py
（前两项为纯函数测试；线上只读验证 login/list/detail 见 SKILL.md §9 与技能索引状态列。）
"""
import hashlib
import importlib.util
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(SKILL_ROOT, "scripts"))

spec = importlib.util.spec_from_file_location(
    "ab_client", os.path.join(SKILL_ROOT, "scripts", "ab_client.py"))
ab = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ab)


class TestLoginCrypto(unittest.TestCase):
    def test_sha256_hex_matches_hashlib(self):
        self.assertEqual(ab.sha256_hex("abc"), hashlib.sha256(b"abc").hexdigest())
        self.assertEqual(ab.sha256_hex(""),
                         hashlib.sha256(b"").hexdigest())


class TestPassFile(unittest.TestCase):
    def _tmp(self, content):
        f = tempfile.NamedTemporaryFile("w", suffix=".env", delete=False, encoding="utf-8")
        f.write(content)
        f.close()
        self.addCleanup(os.unlink, f.name)
        return f.name

    def test_env_file_with_key(self):
        path = self._tmp("# comment\nFG_USER=demo\nFG_PASS=s3cret-value\n")
        self.assertEqual(ab.read_pass_file(path, "FG_PASS"), "s3cret-value")

    def test_env_file_default_candidates(self):
        path = self._tmp("FG_USER=demo\nFG_PASS=fallback\n")
        self.assertEqual(ab.read_pass_file(path), "fallback")

    def test_env_file_missing_key_raises(self):
        path = self._tmp("FG_USER=demo\n")
        with self.assertRaises(ab.ABError):
            ab.read_pass_file(path, "FG_PASS")

    def test_plain_single_line(self):
        path = self._tmp("plainpw\n")
        self.assertEqual(ab.read_pass_file(path), "plainpw")


class TestChatPayload(unittest.TestCase):
    def test_canvas_modules_key_normalized(self):
        canvas = {"modules": [{"moduleId": "n1"}], "edges": [{"id": "e1"}],
                  "chatConfig": {"questionGuide": False}, "name": "DemoApp"}
        payload = ab.build_chat_payload("app1", "chat1", canvas,
                                        [{"role": "user", "content": "hi"}])
        self.assertEqual(payload["nodes"], [{"moduleId": "n1"}])
        self.assertEqual(payload["appName"], "DemoApp")
        self.assertTrue(payload["stream"] and payload["detail"])
        self.assertIsNone(payload["responseChatItemId"])

    def test_canvas_nodes_key_and_plain_message(self):
        canvas = {"nodes": [{"id": "n1"}], "edges": [], "chatConfig": {},
                  "appName": "X"}
        payload = ab.build_chat_payload("app1", "chat1", canvas, ["plain text"])
        self.assertEqual(payload["nodes"], [{"id": "n1"}])
        self.assertEqual(payload["messages"], [{"role": "user", "content": "plain text"}])

    def test_canvas_without_nodes_raises(self):
        with self.assertRaises(ab.ABError):
            ab.build_chat_payload("app1", "chat1", {"edges": []}, ["m"])


class TestSSEParsing(unittest.TestCase):
    def _parse(self, lines):
        result = ab.new_sse_result()
        ab.ABClient._parse_sse_stream(lines, result)
        return result

    def test_fast_answer_accumulation_and_done(self):
        result = self._parse([
            b"event: fastAnswer",
            b'data: {"choices":[{"delta":{"content":"Hello"}}]}',
            b"",
            b"event: fastAnswer",
            b'data: {"choices":[{"delta":{"content":" world"}}]}',
            b"event: answer",
            b"data: [DONE]",
        ])
        self.assertEqual(result["text"], "Hello world")
        self.assertEqual(len(result["events"]), 3)
        self.assertEqual(result["events"][-1], {"event": "done"})

    def test_node_traces_interactive_duration(self):
        result = self._parse([
            b"event: flowNodeResponse",
            b'data: {"nodeId":"p3-plan","finishReason":"stop"}',
            b"event: interactive",
            b'data: {"type":"userSelect","options":[{"value":"OK"}]}',
            b"event: workflowDuration",
            b'data: {"durationSeconds":12.77}',
        ])
        self.assertEqual(result["node_traces"][0]["nodeId"], "p3-plan")
        self.assertEqual(result["interactive"][0]["type"], "userSelect")
        self.assertEqual(result["workflow_duration"], 12.77)
        self.assertEqual(result["text"], "")

    def test_answer_fallback_only_when_no_fast_answer(self):
        result = self._parse([
            b"event: answer",
            b'data: {"choices":[{"delta":{"content":"fallback"}}]}',
        ])
        self.assertEqual(result["text"], "fallback")
        # fastAnswer 先出正文后，answer 帧不再覆盖
        result2 = self._parse([
            b"event: fastAnswer",
            b'data: {"choices":[{"delta":{"content":"real"}}]}',
            b"event: answer",
            b'data: {"choices":[{"delta":{"content":"noise"}}]}',
        ])
        self.assertEqual(result2["text"], "real")

    def test_bad_json_kept_as_raw_event(self):
        result = self._parse([b"event: answer", b"data: not-json"])
        self.assertEqual(result["text"], "")
        self.assertEqual(result["events"][0]["raw"], "not-json")


class TestRequestBodyWithQuery(unittest.TestCase):
    """回归：PUT/POST 带 query 时参数必须进 URL、body 仍按 JSON 发
    （曾只对 GET/DELETE 拼 query，PUT/POST 静默丢弃致线上 update 500 appUnExist）。
    实例级替换 _http 捕获请求，全程离线不触网。"""

    def _call_captured(self, method, path, query, body):
        captured = {}

        def fake_http(method_, url, body_bytes=None, headers=None):
            captured["method"] = method_
            captured["url"] = url
            captured["body"] = body_bytes
            captured["headers"] = headers or {}
            return 200, '{"code":200,"data":{"ok":true}}'

        client = ab.ABClient("https://host:17443", token="t0ken")
        client._http = fake_http  # 实例属性替换（普通函数不绑定 self），不触网
        captured["returned"] = client.call(method, path, query=query, body=body)
        return captured

    def test_put_with_query_params_in_url_and_json_body(self):
        cap = self._call_captured("PUT", "/api/core/app/update",
                                  {"appId": "app-1"}, {"name": "n", "nodes": []})
        self.assertEqual(cap["method"], "PUT")
        self.assertEqual(cap["url"],
                         "https://host:17443/api/core/app/update?appId=app-1")
        self.assertEqual(json.loads(cap["body"].decode("utf-8")),
                         {"name": "n", "nodes": []})
        self.assertEqual(cap["headers"].get("token"), "t0ken")
        self.assertEqual(cap["returned"], {"ok": True})

    def test_post_with_query_params_in_url_and_json_body(self):
        cap = self._call_captured("POST", "/api/core/dataset/collection/delete",
                                  {"id": "col-9"}, {})
        self.assertEqual(cap["method"], "POST")
        self.assertEqual(cap["url"],
                         "https://host:17443/api/core/dataset/collection/delete?id=col-9")
        self.assertEqual(json.loads(cap["body"].decode("utf-8")), {})
        self.assertEqual(cap["headers"].get("token"), "t0ken")


class TestFolderAndToolEndpoints(unittest.TestCase):
    """建目录与系统工具模板的请求形状（11-02 实测），离线捕获不触网。"""

    def _capture(self, invoke):
        captured = {}

        def fake_http(method_, url, body_bytes=None, headers=None):
            captured["method"] = method_
            captured["url"] = url
            captured["body"] = json.loads(body_bytes.decode("utf-8")) if body_bytes else None
            return 200, '{"code":200,"data":null}'

        client = ab.ABClient("https://host", token="t0ken")
        client._http = fake_http
        captured["returned"] = invoke(client)
        return captured

    def test_create_folder_sends_folder_type(self):
        cap = self._capture(lambda c: c.create_folder(None, "POC", intro="internal"))
        self.assertEqual((cap["method"], cap["url"]), ("POST", "https://host/api/core/app/folder/create"))
        self.assertEqual(cap["body"], {"parentId": None, "name": "POC", "intro": "internal", "type": "folder"})
        self.assertIsNone(cap["returned"])  # 成功 data 为 null，id 需 list_apps 找回

    def test_list_system_tools_top_level_uses_get(self):
        cap = self._capture(lambda c: c.list_system_tools())
        self.assertEqual((cap["method"], cap["url"], cap["body"]),
                         ("GET", "https://host/api/core/app/tool/getSystemToolTemplates", None))

    def test_list_system_tools_children_use_post_body(self):
        cap = self._capture(lambda c: c.list_system_tools("systemTool-dbops"))
        self.assertEqual((cap["method"], cap["url"]),
                         ("POST", "https://host/api/core/app/tool/getSystemToolTemplates"))
        self.assertEqual(cap["body"], {"parentId": "systemTool-dbops"})

    def test_tool_preview_node_passes_app_id_in_query(self):
        cap = self._capture(lambda c: c.get_tool_preview_node("systemTool-dbops/oracle"))
        self.assertEqual(cap["method"], "GET")
        self.assertEqual(cap["url"],
                         "https://host/api/core/app/tool/getPreviewNode?appId=systemTool-dbops%2Foracle")
        self.assertIsNone(cap["body"])


class TestNoHardcodedSecrets(unittest.TestCase):
    def test_source_has_no_literal_credentials(self):
        with open(os.path.join(SKILL_ROOT, "scripts", "ab_client.py"),
                  encoding="utf-8") as f:
            src = f.read()
        self.assertNotRegex(src, r'password\s*=\s*"[^"]+"')
        self.assertNotRegex(src, r'token\s*=\s*"[A-Za-z0-9_\-]{16,}"')


if __name__ == "__main__":
    unittest.main(verbosity=2)
