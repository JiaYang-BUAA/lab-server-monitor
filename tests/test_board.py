import concurrent.futures
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest

from labmon.server import HubRuntime, MonitorHTTPServer
from labmon.storage import ConflictError, Store


class BoardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = {"mode": "hub", "listen_host": "127.0.0.1", "data_dir": self.temp.name, "servers": []}
        self.runtime = HubRuntime(self.config)
        self.server = MonitorHTTPServer(("127.0.0.1", 0), self.config, self.runtime)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.runtime.close()
        self.temp.cleanup()

    def request(self, method="GET", path="/api/board", payload=None, cookie=None, origin=True):
        host = f"127.0.0.1:{self.server.server_port}"
        headers = {"Content-Type": "application/json"}
        if origin:
            headers["Origin"] = "http://" + host
        if cookie:
            headers["Cookie"] = cookie
        connection = http.client.HTTPConnection(host, timeout=5)
        connection.request(method, path, json.dumps(payload).encode() if payload is not None else None, headers)
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), json.loads(response.read())
        connection.close()
        return result

    def test_all_sessions_can_edit_and_storage_survives_restart(self):
        _, headers, initial = self.request()
        first = headers["Set-Cookie"].split(";", 1)[0]
        _, headers, _ = self.request()
        second = headers["Set-Cookie"].split(";", 1)[0]
        payload = {"text": "今晚占用\n<script>alert(1)</script>", "revision": initial["revision"], "editor_name": "甲"}
        code, _, saved = self.request("PUT", payload=payload, cookie=first)
        self.assertEqual(code, 200)
        self.assertEqual(saved["board"]["text"], payload["text"])
        payload.update(text="第二位成员修改", revision=saved["board"]["revision"], editor_name="乙")
        self.assertEqual(self.request("PUT", payload=payload, cookie=second)[0], 200)
        restored = Store(Path(self.temp.name) / "labmon.sqlite3").board()
        self.assertEqual(restored["text"], payload["text"])
        self.assertEqual(restored["updated_by"], "乙")
        self.assertEqual(self.request(path="/api/state")[2]["board"], restored)

    def test_conflict_returns_latest_without_overwrite_and_allows_empty_text(self):
        self.assertEqual(self.request("PUT", payload={"text": "first", "revision": 0})[0], 200)
        code, _, body = self.request("PUT", payload={"text": "stale", "revision": 0})
        self.assertEqual(code, 409)
        self.assertEqual(body["board"]["text"], "first")
        self.assertEqual(self.request("PUT", payload={"text": "", "revision": 1})[0], 200)
        self.assertEqual(self.request()[2]["text"], "")

    def test_origin_types_and_limits(self):
        self.assertEqual(self.request("PUT", payload={"text": "x", "revision": 0}, origin=False)[0], 403)
        for payload in ({"text": "x", "revision": True}, {"text": "x"}, {"text": "x" * 20001, "revision": 0},
                        {"text": None, "revision": 0}, {"text": "x", "revision": 0, "editor_name": []}):
            with self.subTest(payload_type=str(type(payload.get("text")))):
                self.assertEqual(self.request("PUT", payload=payload)[0], 400)
        self.assertEqual(self.runtime.store.board()["revision"], 0)

    def test_concurrent_writers_and_independent_connections(self):
        stores = [Store(Path(self.temp.name) / "labmon.sqlite3") for _ in range(2)]
        def save(index):
            try:
                stores[index].update_board(str(index), 0, "")
                return "saved"
            except ConflictError:
                return "conflict"
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(save, [0, 1])), ["conflict", "saved"])

    def test_zero_one_many_servers_and_optional_history(self):
        for count in (0, 1, 2, 6, 12):
            runtime = HubRuntime({**self.config, "servers": [{"id": f"server-{n}", "name": f"节点 {n}", "enabled": False} for n in range(count)]})
            try:
                token, name, _ = runtime.store.session(None)
                state = runtime.state(token, name)
                self.assertEqual(len(state["servers"]), count)
                self.assertIsNone(state["grafana_url"])
            finally:
                runtime.close()


if __name__ == "__main__":
    unittest.main()
