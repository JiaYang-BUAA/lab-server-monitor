from concurrent.futures import ThreadPoolExecutor
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest

from labmon.estimates import iso
from labmon.server import AgentRuntime, HubRuntime, MonitorHTTPServer, group_jobs, validate_claim, validate_public_origin
from labmon.storage import ConflictError, OwnershipError, Store


def process(pid=1, parent=None, start=100, cpu=10, software="abaqus", role="solver", name="standard.exe"):
    return {"key": f"{pid}:{start}", "pid": pid, "parent_pid": parent, "start_time": start,
            "name": name, "software": software, "role": role,
            "cpu_pct": cpu, "memory_bytes": 1024, "owner": "shared"}


def mpi_proxy(pid=10, parent=None, start=90):
    return process(pid, parent, start, cpu=20, software="system", role="system", name="hydra_pmi_proxy")


def fluent_rank(pid=1, parent=10, start=100):
    return process(pid, parent, start, software="fluent", name="fl_mpi2520")


def snapshot(now=None, processes=None, status="ok"):
    return {"host_id": "lab-new", "observed_at": iso(time.time() if now is None else now),
            "telemetry_status": status, "processes": [process()] if processes is None else processes,
            "cpu": {"percent": 15}, "memory": {"percent": 30}, "gpus": [], "warnings": []}


def claim_payload(**kwargs):
    return {"owner_name": "小王", "task_name": "换热器仿真", "expected_end": None,
            "notes": "", "log_path": "", "log_kind": "abaqus", "total_units": None, **kwargs}


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "data.sqlite3"
        self.store = Store(self.path)
        self.a = self.store.session(None)[0]
        self.b = self.store.session(None)[0]

    def tearDown(self):
        self.temp.cleanup()

    def test_competing_claims_are_atomic(self):
        barrier = threading.Barrier(2)
        def create(token):
            other_store = Store(self.path)
            barrier.wait()
            try:
                return other_store.create(token, "lab-new", "job", claim_payload())
            except ConflictError:
                return "conflict"
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(create, [self.a, self.b]))
        self.assertEqual(results.count("conflict"), 1)
        self.assertEqual(len(self.store.claims()), 1)

    def test_identity_claims_and_ownership_survive_restart(self):
        self.store.identity(self.a, "小王")
        claim_id = self.store.create(self.a, "lab-new", "job", claim_payload())
        reloaded = Store(self.path)
        self.assertEqual(reloaded.session(self.a)[1], "小王")
        self.assertTrue(reloaded.claim(claim_id, self.a)["can_edit"])
        self.assertFalse(reloaded.claim(claim_id, self.b)["can_edit"])
        with self.assertRaises(OwnershipError):
            reloaded.update(self.b, claim_id, None)
        reloaded.update(self.a, claim_id, claim_payload(task_name="变更"))
        self.assertEqual(reloaded.claim(claim_id)["task_name"], "变更")
        reloaded.update(self.a, claim_id, None)
        self.assertEqual(reloaded.claims(), [])
        self.assertEqual(len(reloaded.claims(recent=True)), 1)
        reloaded.create(self.b, "lab-new", "job", claim_payload())
        self.assertEqual(len(reloaded.claims()), 1)

    def test_member_directory_backfills_legacy_claims_and_keeps_names(self):
        self.store.create(self.a, "lab-new", "first", claim_payload(owner_name="旧成员"))
        self.store.create(self.b, "lab-new", "second", claim_payload(owner_name="另一成员"))
        with self.store._connect() as db:
            db.execute("DELETE FROM members")
        restored = Store(self.path)
        self.assertEqual(restored.members(), ["旧成员", "另一成员"])
        self.assertEqual(restored.add_member("  新成员  "), "新成员")
        self.assertEqual(restored.add_member("新成员"), "新成员")
        self.assertEqual(restored.members(), ["旧成员", "另一成员", "新成员"])
        for invalid in ("", " ", "a" * 41, "含\x00空字节", 7):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                restored.add_member(invalid)

    def test_annotation_version_prevents_stale_full_edit(self):
        claim_id = self.store.create(self.a, "lab-new", "job", claim_payload())
        old = self.store.claim(claim_id)
        changed = self.store.upsert_annotation(self.b, "lab-new", "job", "ignored", "小李", "新备注",
                                                old["updated_at"])
        self.assertFalse(changed["can_edit"])
        with self.assertRaises(ConflictError):
            self.store.update(self.a, claim_id, claim_payload(notes="旧内容"), old["updated_at"])
        self.assertEqual(self.store.claim(claim_id)["notes"], "新备注")


class GroupTests(unittest.TestCase):
    def test_groups_matching_software_tree_and_sums(self):
        jobs = group_jobs("new", [process(1, start=100), process(2, 1, start=101)], now=200)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["pids"], [1, 2])
        self.assertEqual(jobs[0]["cpu_pct"], 20)
        self.assertEqual(jobs[0]["elapsed_seconds"], 100)

    def test_pid_reuse_never_joins_new_parent_to_old_child(self):
        jobs = group_jobs("new", [process(1, start=200), process(2, 1, start=101)])
        self.assertEqual(len(jobs), 2)
        first = group_jobs("new", [process(1, start=100)])[0]["id"]
        second = group_jobs("new", [process(1, start=200)])[0]["id"]
        self.assertNotEqual(first, second)

    def test_missing_creation_time_has_no_stable_job(self):
        self.assertEqual(group_jobs("new", [process(start=None)]), [])
        jobs = group_jobs("new", [process(1, start=None), process(2, 1, start=101)])
        self.assertEqual(jobs[0]["pids"], [2])

    def test_missing_cpu_is_unknown_not_idle_zero(self):
        job = group_jobs("new", [process(cpu=None)])[0]
        self.assertIsNone(job["cpu_pct"])
        self.assertEqual(job["state"], "unknown")

    def test_services_and_different_families_not_grouped(self):
        jobs = group_jobs("new", [process(1, software="system", role="service"),
                                  process(2, 1, start=101), process(3, 2, start=102, software="fluent")])
        self.assertEqual(len(jobs), 2)

    def test_mpi_siblings_group_without_counting_proxy_or_unrelated_gui(self):
        proxy = mpi_proxy()
        proxy["name"] = "HYDRA_PMI_PROXY.EXE"
        gui = process(3, parent=999, start=80, software="fluent", name="fluent")
        jobs = group_jobs("old", [proxy, fluent_rank(1), fluent_rank(2), gui], now=200)
        self.assertEqual(len(jobs), 2)
        job = next(job for job in jobs if job["process_count"] == 2)
        self.assertEqual(job["pids"], [1, 2])
        self.assertEqual(job["process_keys"], ["1:100", "2:100"])
        self.assertEqual(job["software"], "fluent")
        self.assertEqual(job["name"], "fl_mpi2520")
        self.assertEqual(job["cpu_pct"], 20)
        self.assertEqual(job["memory_bytes"], 2048)
        self.assertEqual(job["elapsed_seconds"], 110)
        self.assertEqual(group_jobs("old", [proxy]), [])

    def test_different_mpi_proxies_under_shared_daemon_stay_separate(self):
        daemon = process(20, start=50, software="system", role="service", name="hydra_service")
        jobs = group_jobs("old", [daemon, mpi_proxy(10, 20), mpi_proxy(11, 20),
                                 fluent_rank(1, 10), fluent_rank(2, 10),
                                 fluent_rank(3, 11), fluent_rank(4, 11),
                                 fluent_rank(5, 20), fluent_rank(6, 20)])
        self.assertEqual(sorted(job["pids"] for job in jobs), [[1, 2], [3, 4], [5], [6]])
        self.assertEqual(len({job["id"] for job in jobs}), 4)

    def test_mpi_proxy_requires_live_identity_and_valid_parent_creation_time(self):
        no_key = mpi_proxy()
        no_key["key"] = None
        for parents in ([], [mpi_proxy(start=101)], [mpi_proxy(start=None)], [no_key]):
            with self.subTest(parents=parents):
                jobs = group_jobs("old", parents + [fluent_rank(1), fluent_rank(2)])
                self.assertEqual(sorted(job["pids"] for job in jobs), [[1], [2]])

    def test_same_mpi_proxy_does_not_merge_different_software(self):
        jobs = group_jobs("old", [mpi_proxy(), fluent_rank(1), fluent_rank(2), process(3, 10), process(4, 10)])
        self.assertEqual(sorted(job["pids"] for job in jobs), [[1, 2], [3, 4]])
        self.assertEqual({job["software"] for job in jobs}, {"fluent", "abaqus"})

    def test_mpi_job_identity_survives_rank_churn_but_not_proxy_pid_reuse(self):
        original = group_jobs("old", [mpi_proxy(), fluent_rank(1), fluent_rank(2)])[0]
        for processes in ([fluent_rank(2), mpi_proxy(), fluent_rank(1)],
                          [mpi_proxy(), fluent_rank(2)],
                          [mpi_proxy(), fluent_rank(3, start=110), fluent_rank(4, start=111)]):
            with self.subTest(processes=processes):
                job = group_jobs("old", processes)[0]
                self.assertEqual(job["id"], original["id"])
                self.assertEqual(job["started_at"], original["started_at"])
        replacement = group_jobs("old", [mpi_proxy(start=120), fluent_rank(1, start=121)])[0]
        self.assertNotEqual(replacement["id"], original["id"])


class HubTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = time.time()
        self.current = snapshot(self.now)
        self.fail = False
        self.config = {"mode": "hub", "data_dir": self.temp.name, "listen_host": "127.0.0.1",
                       "poll_seconds": 5, "stale_seconds": 30,
                       "servers": [{"id": "lab-new", "name": "新服务器", "agent_url": "http://127.0.0.1:8767",
                                    "token": "test-token-123456789", "enabled": True},
                                   {"id": "lab-old", "name": "旧服务器", "enabled": False}]}
        def fetch(config, path, data=None):
            if self.fail:
                raise OSError("unavailable")
            return self.current
        self.runtime = HubRuntime(self.config, fetcher=fetch, clock=lambda: self.now)
        self.token, self.name, _ = self.runtime.store.session(None)

    def tearDown(self):
        self.runtime.close()
        self.temp.cleanup()

    def state(self):
        return self.runtime.state(self.token, self.name)

    def test_unconfigured_and_offline_are_explicit(self):
        state = self.state()
        self.assertEqual(state["servers"][0]["status"], "offline")
        self.assertEqual(state["servers"][1]["status"], "unconfigured")
        self.assertIsNone(state["servers"][1]["snapshot"])

    def test_mpi_claim_survives_rank_churn_and_proxy_alone_does_not_keep_job_alive(self):
        proxy = mpi_proxy()
        self.current = snapshot(self.now, [proxy, fluent_rank(1), fluent_rank(2)])
        self.runtime.poll_once()
        job = self.state()["servers"][0]["jobs"][0]
        claim_id = self.runtime.store.create(self.token, "lab-new", job["id"], claim_payload())
        self.now += 5
        self.current = snapshot(self.now, [proxy, fluent_rank(3, start=110), fluent_rank(4, start=111)])
        self.runtime.poll_once()
        jobs = self.state()["servers"][0]["jobs"]
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["id"], job["id"])
        self.assertEqual(jobs[0]["claim"]["id"], claim_id)
        self.assertEqual(jobs[0]["pids"], [3, 4])
        for _ in range(2):
            self.now += 5
            self.current = snapshot(self.now, [proxy])
            self.runtime.poll_once()
        self.assertEqual(self.state()["servers"][0]["jobs"], [])
        self.assertIsNotNone(self.runtime.store.claim(claim_id)["ended_at"])

    def test_stale_data_never_claims_current_job_or_eta(self):
        self.runtime.poll_once()
        self.assertEqual(self.state()["servers"][0]["status"], "online")
        job = self.state()["servers"][0]["jobs"][0]
        self.runtime.store.create(self.token, "lab-new", job["id"], claim_payload())
        self.now += 40
        server = self.state()["servers"][0]
        self.assertEqual(server["status"], "stale")
        self.assertEqual(server["jobs"][0]["state"], "unknown")
        with self.assertRaises(ConflictError):
            self.runtime.require_job("lab-new", job["id"])

    def test_two_healthy_absences_required_and_disconnect_not_end(self):
        self.runtime.poll_once()
        job = self.state()["servers"][0]["jobs"][0]
        claim_id = self.runtime.store.create(self.token, "lab-new", job["id"], claim_payload())
        self.current = snapshot(self.now + 5, [])
        self.now += 5
        self.runtime.poll_once()
        self.assertEqual(self.state()["servers"][0]["jobs"][0]["state"], "unknown")
        self.fail = True
        self.runtime.poll_once()
        self.assertIsNone(self.runtime.store.claim(claim_id)["ended_at"])
        self.fail = False
        self.now += 5
        self.current = snapshot(self.now, [])
        self.runtime.poll_once()
        self.assertIsNone(self.runtime.store.claim(claim_id)["ended_at"])
        self.now += 5
        self.current = snapshot(self.now, [])
        self.runtime.poll_once()
        self.assertIsNotNone(self.runtime.store.claim(claim_id)["ended_at"])
        self.assertEqual(self.state()["servers"][0]["jobs"], [])

    def test_partial_snapshot_does_not_end_missing_jobs(self):
        self.runtime.poll_once()
        for _ in range(3):
            self.now += 5
            self.current = snapshot(self.now, [], "partial")
            self.runtime.poll_once()
        self.assertEqual(len(self.state()["servers"][0]["jobs"]), 1)
        self.assertEqual(self.state()["servers"][0]["jobs"][0]["state"], "unknown")

    def test_healthy_processes_can_end_despite_optional_gpu_warning(self):
        self.runtime.poll_once()
        for _ in range(2):
            self.now += 5
            self.current = {**snapshot(self.now, [], "partial"), "process_status": "ok"}
            self.runtime.poll_once()
        self.assertEqual(self.state()["servers"][0]["jobs"], [])

    def test_duplicate_cached_sample_does_not_confirm_end(self):
        self.runtime.poll_once()
        self.now += 5
        self.current = snapshot(self.now, [])
        self.runtime.poll_once()
        self.runtime.poll_once()
        self.assertEqual(len(self.state()["servers"][0]["jobs"]), 1)
        self.now += 5
        self.current = snapshot(self.now, [])
        self.runtime.poll_once()
        self.assertEqual(self.state()["servers"][0]["jobs"], [])

    def test_general_job_becoming_idle_does_not_mean_ended(self):
        generic = {**process(1, software="general", role="general"), "memory_bytes": 1024 ** 3}
        self.current = snapshot(self.now, [generic])
        self.runtime.poll_once()
        for _ in range(3):
            self.now += 5
            self.current = snapshot(self.now, [{**generic, "cpu_pct": 0}])
            self.runtime.poll_once()
        self.assertEqual(self.state()["servers"][0]["jobs"][0]["state"], "idle")

    def test_parent_exit_preserves_claim_id_on_live_children(self):
        self.current = snapshot(self.now, [process(1), process(2, 1, start=101)])
        self.runtime.poll_once()
        first = self.state()["servers"][0]["jobs"][0]["id"]
        self.now += 5
        self.current = snapshot(self.now, [process(2, 1, start=101)])
        self.runtime.poll_once()
        self.assertEqual(self.state()["servers"][0]["jobs"][0]["id"], first)

    def test_manual_group_claim_survives_one_member_ending(self):
        self.current = snapshot(self.now, [process(1), process(2, start=101)])
        self.runtime.poll_once()
        ids = [job["id"] for job in self.state()["servers"][0]["jobs"]]
        group = self.runtime.create_job_group("lab-new", ids, "组合仿真")
        claim_id = self.runtime.store.create(self.token, "lab-new", group["id"], claim_payload())
        for _ in range(3):
            self.now += 5
            self.current = snapshot(self.now, [process(2, start=101)])
            self.runtime.poll_once()
        only = self.state()["servers"][0]["jobs"]
        self.assertEqual(len(only), 1)
        self.assertEqual(only[0]["id"], group["id"])
        self.assertEqual(only[0]["claim"]["id"], claim_id)
        self.assertIsNone(self.runtime.store.claim(claim_id)["ended_at"])
        for _ in range(5):
            self.now += 5
            self.current = snapshot(self.now, [])
            self.runtime.poll_once()
        self.assertEqual(self.state()["servers"][0]["jobs"], [])
        self.assertIsNotNone(self.runtime.store.claim(claim_id)["ended_at"])

    def test_pid_reuse_does_not_take_over_claim(self):
        self.runtime.poll_once()
        old = self.state()["servers"][0]["jobs"][0]
        self.runtime.store.create(self.token, "lab-new", old["id"], claim_payload())
        self.now += 5
        self.current = snapshot(self.now, [process(1, start=200)])
        self.runtime.poll_once()
        new = next(job for job in self.state()["servers"][0]["jobs"] if job["state"] != "unknown")
        self.assertNotEqual(new["id"], old["id"])
        self.assertIsNone(new["claim"])


class HTTPTests(unittest.TestCase):
    def setUp(self):
        HubTests.setUp(self)
        self.runtime.poll_once()
        self.server = MonitorHTTPServer(("127.0.0.1", 0), self.config, self.runtime)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.host = f"127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        HubTests.tearDown(self)

    def request(self, method, path, body=None, cookie=None, origin=True, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        prepared = {"Content-Type": "application/json"}
        if origin:
            prepared["Origin"] = "http://" + self.host
        if cookie:
            prepared["Cookie"] = cookie
        prepared.update(headers or {})
        connection.request(method, path, body=json.dumps(body).encode() if body is not None else None, headers=prepared)
        response = connection.getresponse()
        raw = response.read()
        result = (response.status, dict(response.getheaders()), raw)
        connection.close()
        return result

    def test_first_state_session_cookie_and_claim_owner(self):
        code, headers, raw = self.request("GET", "/api/state")
        self.assertEqual(code, 200)
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertIn("SameSite=Strict", headers["Set-Cookie"])
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        state = json.loads(raw)
        job_id = state["servers"][0]["jobs"][0]["id"]
        body = claim_payload(host_id="lab-new", job_id=job_id)
        self.assertEqual(self.request("POST", "/api/claims", body, cookie)[0], 200)
        mine = json.loads(self.request("GET", "/api/state", cookie=cookie)[2])["servers"][0]["jobs"][0]["claim"]
        self.assertTrue(mine["can_edit"])
        theirs = json.loads(self.request("GET", "/api/state")[2])["servers"][0]["jobs"][0]["claim"]
        self.assertFalse(theirs["can_edit"])
        self.assertEqual(self.request("DELETE", "/api/claims/" + mine["id"], {})[0], 403)
        self.assertEqual(self.request("POST", "/api/claims", body)[0], 409)
        self.assertEqual(self.request("PATCH", "/api/claims/" + mine["id"], {"notes": "更新"}, cookie)[0], 200)
        self.assertEqual(self.request("DELETE", "/api/claims/" + mine["id"], {}, cookie)[0], 200)

    def test_member_endpoint_and_shared_task_annotation(self):
        code, headers, raw = self.request("GET", "/api/state")
        self.assertEqual(code, 200)
        first_cookie = headers["Set-Cookie"].split(";", 1)[0]
        job = json.loads(raw)["servers"][0]["jobs"][0]
        path = f"/api/tasks/lab-new/{job['id']}/annotation"
        self.assertEqual(self.request("POST", "/api/members", {"name": "甲"}, origin=False)[0], 403)
        self.assertEqual(self.request("POST", "/api/members", {"name": " "})[0], 400)
        self.assertEqual(self.request("POST", "/api/members", {"name": "  甲  "})[0], 200)
        self.assertEqual(json.loads(self.request("POST", "/api/members", {"name": "甲"})[2])["members"], ["甲"])
        self.assertEqual(self.request("PUT", path, {"owner_name": "甲", "notes": "备注"}, first_cookie)[0], 400)
        self.assertEqual(self.request("PUT", path, {"owner_name": "甲", "notes": "备注", "expected_updated_at": None},
                                      first_cookie, origin=False)[0], 403)
        code, _, raw = self.request("PUT", path, {"owner_name": "甲", "notes": "备注", "expected_updated_at": None}, first_cookie)
        self.assertEqual(code, 200)
        created = json.loads(raw)["claim"]
        self.assertTrue(created["can_edit"])
        self.assertEqual(created["task_name"], job["name"])
        self.assertEqual(created["notes"], "备注")
        self.assertEqual(created["expected_end"], None)
        self.assertEqual(json.loads(self.request("GET", "/api/state")[2])["members"], ["甲"])

        # A second visitor can edit the public fields without taking over the
        # original session's task name, ETA, log binding, or release rights.
        updated_full = claim_payload(owner_name="甲", task_name="自定义任务", notes="备注",
                                     expected_end="2026-09-23T18:00:00+08:00", log_path="E:/private/job.sta")
        self.assertEqual(self.request("PATCH", "/api/claims/" + created["id"], updated_full, first_cookie)[0], 200)
        current = self.runtime.store.claim(created["id"])
        code, _, raw = self.request("PUT", path, {"owner_name": "乙", "notes": "他人补充",
                                                  "expected_updated_at": current["updated_at"]})
        self.assertEqual(code, 200)
        changed = json.loads(raw)["claim"]
        self.assertFalse(changed["can_edit"])
        self.assertNotIn("log_path", changed)
        self.assertEqual((changed["task_name"], changed["expected_end"]),
                         ("自定义任务", "2026-09-23T10:00:00Z"))
        self.assertEqual(self.runtime.store.claim(created["id"])["log_path"], "E:/private/job.sta")
        self.assertEqual(json.loads(self.request("GET", "/api/state")[2])["members"], ["甲", "乙"])
        self.assertEqual(self.request("PATCH", "/api/claims/" + created["id"], {"notes": "越权"})[0], 403)
        self.assertEqual(self.request("DELETE", "/api/claims/" + created["id"], {})[0], 403)
        self.assertEqual(self.request("PUT", path, {"owner_name": "丙", "notes": "旧版本",
                                                   "expected_updated_at": current["updated_at"]})[0], 409)
        self.assertEqual(self.request("PATCH", "/api/claims/" + created["id"],
                                      {"notes": "旧窗口覆盖", "expected_updated_at": current["updated_at"]},
                                      first_cookie)[0], 409)
        self.assertEqual(self.request("DELETE", "/api/claims/" + created["id"],
                                      {"expected_updated_at": current["updated_at"]}, first_cookie)[0], 409)
        self.assertEqual(self.runtime.store.claim(created["id"])["owner_name"], "乙")
        self.assertEqual(self.request("DELETE", "/api/claims/" + created["id"], {}, first_cookie)[0], 200)

    def test_annotation_rejects_missing_or_unavailable_jobs_and_bad_inputs(self):
        job_id = self.runtime.servers["lab-new"]["jobs"][0]["id"]
        path = f"/api/tasks/lab-new/{job_id}/annotation"
        for body in ({"owner_name": "", "notes": "", "expected_updated_at": None},
                     {"owner_name": "甲", "notes": "x" * 501, "expected_updated_at": None},
                     {"owner_name": "甲", "notes": "", "expected_updated_at": True}):
            with self.subTest(body=body):
                self.assertEqual(self.request("PUT", path, body)[0], 400)
        self.assertEqual(self.request("PUT", "/api/tasks/lab-new/absent/annotation",
                                      {"owner_name": "甲", "notes": "", "expected_updated_at": None})[0], 409)
        self.now += 31
        self.assertEqual(self.request("PUT", path, {"owner_name": "甲", "notes": "",
                                                    "expected_updated_at": None})[0], 409)

    def test_cross_origin_missing_origin_and_wrong_host_rejected(self):
        self.assertEqual(self.request("POST", "/api/identity", {"name": "A"}, origin=False)[0], 403)
        self.assertEqual(self.request("POST", "/api/identity", {"name": "A"}, headers={"Origin": "http://evil.example"})[0], 403)
        self.assertEqual(self.request("GET", "/api/state", headers={"Host": "evil.example:" + str(self.server.server_port)})[0], 403)
        self.assertEqual(self.request("POST", "/api/identity", {"name": "A"}, headers={"Content-Type": "text/plain"})[0], 400)

    def test_server_name_and_capacity_api(self):
        self.runtime.servers["lab-new"]["snapshot"]["cpu"]["logical_processors"] = 64
        self.runtime.servers["lab-new"]["snapshot"]["cpu"]["observed_processors"] = 64
        self.runtime.servers["lab-new"]["snapshot"]["memory"] = {
            "total_bytes": 128 * 1024 ** 3, "available_bytes": 80 * 1024 ** 3}
        code, headers, raw = self.request("GET", "/api/capacity?host_id=lab-new&cpu_cores=16&memory_gb=32")
        self.assertEqual(code, 200)
        self.assertNotIn("Set-Cookie", headers)  # Automation clients do not create browser identities.
        report = json.loads(raw)
        self.assertEqual(report["servers"][0]["verdict"], "likely_available")
        self.assertEqual(self.request("GET", "/api/capacity?cpu_cores=1&cpu_cores=2")[0], 400)
        self.assertEqual(self.request("GET", "/api/capacity?host_id=missing")[0], 404)
        self.assertEqual(self.request("PUT", "/api/servers/lab-new/name", {"name": "  自定义服务器  "})[0], 200)
        self.assertEqual(json.loads(self.request("GET", "/api/state")[2])["servers"][0]["name"], "自定义服务器")
        self.assertEqual(self.request("PUT", "/api/servers/lab-new/name", {"name": " "})[0], 400)
        self.assertEqual(self.request("PUT", "/api/servers/missing/name", {"name": "其他"})[0], 404)
        reloaded = HubRuntime(self.config, fetcher=self.runtime.fetcher, clock=lambda: self.now)
        self.assertEqual(reloaded.servers["lab-new"]["name"], "自定义服务器")
        reloaded.close()

    def test_manual_group_is_one_claim_and_can_be_undone(self):
        self.now += 5
        self.current = snapshot(self.now, [process(1), process(2, start=101)])
        self.runtime.poll_once()
        ids = [job["id"] for job in self.runtime.servers["lab-new"]["jobs"]]
        self.assertEqual(len(ids), 2)
        code, _, raw = self.request("POST", "/api/job-groups",
                                    {"host_id": "lab-new", "job_ids": ids, "name": "一次仿真"})
        self.assertEqual(code, 200)
        group_id = json.loads(raw)["group"]["id"]
        code, headers, raw = self.request("GET", "/api/state")
        job = json.loads(raw)["servers"][0]["jobs"][0]
        self.assertEqual(job["id"], group_id)
        self.assertEqual(job["process_count"], 2)
        self.assertEqual(set(job["grouped_job_ids"]), set(ids))
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        self.assertEqual(self.request("POST", "/api/claims", {**claim_payload(),
                         "host_id": "lab-new", "job_id": group_id}, cookie)[0], 200)
        claim_id = self.runtime.store.claims()[0]["id"]
        self.assertEqual(self.request("DELETE", "/api/job-groups/" + group_id)[0], 409)
        self.assertEqual(self.request("DELETE", "/api/claims/" + claim_id, {}, cookie)[0], 200)
        self.assertEqual(self.request("DELETE", "/api/job-groups/" + group_id)[0], 200)
        self.assertEqual(len(json.loads(self.request("GET", "/api/state")[2])["servers"][0]["jobs"]), 2)

    def test_public_https_origin_supports_board_without_accepting_http_writes(self):
        public = "https://lab.example.ts.net"
        self.server.public_origin = validate_public_origin(public)
        headers = {"Host": "lab.example.ts.net", "Origin": public}
        snapshot_data = self.runtime.servers["lab-new"]["snapshot"]
        snapshot_data["hostname"] = "private-machine"
        snapshot_data["ssh"] = {"tcp_connections": 1, "connections": [{"remote_address": "10.0.0.12"}]}
        job_id = self.runtime.servers["lab-new"]["jobs"][0]["id"]
        owner_token, _, _ = self.runtime.store.session(None)
        self.runtime.store.create(owner_token, "lab-new", job_id, claim_payload(log_path="E:/private/job.sta"))
        code, response_headers, raw = self.request("GET", "/api/state", headers=headers)
        self.assertEqual(code, 200)
        self.assertIn("Secure", response_headers["Set-Cookie"])
        self.assertEqual(response_headers["Strict-Transport-Security"], "max-age=31536000")
        public_state = json.loads(raw)
        self.assertEqual(public_state["servers"][0]["snapshot"]["ssh"]["tcp_connections"], 1)
        for private_value in ("private-machine", "10.0.0.12", "E:/private/job.sta", '"owner": "shared"'):
            self.assertNotIn(private_value, raw.decode())
        revision = public_state["board"]["revision"]
        self.assertEqual(self.request("PUT", "/api/board", {"text": "公网公告", "revision": revision, "editor_name": "测试者"}, headers=headers)[0], 200)
        self.assertEqual(self.request("GET", "/api/board", headers=headers)[0], 200)
        self.assertEqual(self.request("PUT", "/api/board", {"text": "wrong", "revision": revision + 1},
                                      headers={**headers, "Origin": "http://lab.example.ts.net"})[0], 403)
        self.assertEqual(self.request("GET", "/api/state", headers={"Host": "evil.example"})[0], 403)
        local = self.request("GET", "/api/state")
        self.assertEqual(local[0], 200)
        self.assertNotIn("Secure", local[1]["Set-Cookie"])
        self.assertIn("10.0.0.12", local[2].decode())

    def test_public_origin_requires_https_dns_and_loopback_binding(self):
        for origin in ("http://lab.example.ts.net", "https://lab.example.ts.net/path",
                       "https://lab.example.ts.net:8766", "https://user@lab.example.ts.net",
                       "https://127.0.0.1"):
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                validate_public_origin(origin)
        with self.assertRaisesRegex(ValueError, "127.0.0.1"):
            MonitorHTTPServer(("0.0.0.0", 0), {**self.config, "public_origin": "https://lab.example.ts.net"}, self.runtime)

    def test_snapshot_contains_no_agent_credentials(self):
        raw = self.request("GET", "/api/state")[2]
        self.assertNotIn(b"test-token", raw)
        self.assertNotIn(b"agent_url", raw)

    def test_proxy_preserves_path_query_json_and_content_type(self):
        calls = []
        class FakeGrafana(BaseHTTPRequestHandler):
            def do_GET(fake):
                calls.append((fake.command, fake.path, dict(fake.headers), None))
                fake.send_response(200)
                fake.send_header("Content-Type", "text/css; charset=utf-8")
                fake.end_headers()
                fake.wfile.write(b"body {color: blue}")
            def do_POST(fake):
                data = fake.rfile.read(int(fake.headers.get("Content-Length", 0)))
                calls.append((fake.command, fake.path, dict(fake.headers), json.loads(data)))
                fake.send_response(200)
                fake.send_header("Content-Type", "application/json")
                fake.end_headers()
                fake.wfile.write(b'{"results":{}}')
            def log_message(self, *args):
                pass
        grafana = ThreadingHTTPServer(("127.0.0.1", 0), FakeGrafana)
        thread = threading.Thread(target=grafana.serve_forever, daemon=True)
        thread.start()
        self.config["grafana_upstream"] = f"http://127.0.0.1:{grafana.server_port}"
        try:
            code, headers, raw = self.request("GET", "/grafana/public/build/app.css?x=1")
            self.assertEqual(code, 200)
            self.assertEqual(headers["Content-Type"], "text/css; charset=utf-8")
            self.assertEqual(calls[-1][1], "/grafana/public/build/app.css?x=1")
            self.assertEqual(self.request("POST", "/grafana/api/ds/query?ds_type=prometheus", {"queries": []}, cookie="private-cookie=abc", headers={"Authorization": "private"})[0], 200)
            self.assertEqual(calls[-1][3], {"queries": []})
            self.assertNotIn("Cookie", calls[-1][2])
            self.assertNotIn("Authorization", calls[-1][2])
            denied = self.request("POST", "/grafana/api/admin/users", {})
            self.assertEqual(denied[0], 403)
            self.assertEqual(denied[1].get("Connection"), "close")
            self.assertEqual(self.request("GET", "/grafana/../api/admin")[0], 400)
            self.config["grafana_upstream"] = "http://example.com:3000"
            self.assertEqual(self.request("GET", "/grafana/")[0], 400)
        finally:
            grafana.shutdown()
            grafana.server_close()
            thread.join()


class AgentTests(unittest.TestCase):
    def test_machine_estimate_post_uses_bearer_without_browser_origin(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "task.sta"
            log.write_text("1 1 1 0 1 1 .2 .2 .1\n", encoding="utf-8")
            config = {"mode": "agent", "token": "long-test-token-123456789", "allowed_log_roots": [directory]}
            runtime = AgentRuntime(config, collector=object())
            server = MonitorHTTPServer(("127.0.0.1", 0), config, runtime)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
                body = json.dumps({"log_path": str(log), "log_kind": "abaqus", "total_units": 1})
                connection.request("POST", "/estimate", body=body,
                    headers={"Authorization": "Bearer " + config["token"], "Content-Type": "application/json"})
                response = connection.getresponse()
                result = json.loads(response.read())
                self.assertEqual(response.status, 200)
                self.assertEqual(result["status"], "warming")
                self.assertEqual(result["progress_pct"], 20)
                connection.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join()

    def test_fixed_exporter_metrics_proxy_auth_query_and_redirect_boundaries(self):
        calls = []
        class FakeExporter(BaseHTTPRequestHandler):
            def do_GET(fake):
                calls.append(fake.path)
                if fake.path == "/redirect":
                    fake.send_response(302)
                    fake.send_header("Location", "/secret")
                    fake.end_headers()
                else:
                    fake.send_response(200)
                    fake.end_headers()
                    fake.wfile.write(b"windows_test_metric 42\n")
            def log_message(self, *args):
                pass
        exporter = ThreadingHTTPServer(("127.0.0.1", 0), FakeExporter)
        exporter_thread = threading.Thread(target=exporter.serve_forever, daemon=True)
        exporter_thread.start()
        config = {"mode": "agent", "token": "long-test-token-123456789", "allowed_log_roots": [],
                  "exporter_url": f"http://127.0.0.1:{exporter.server_port}/metrics"}
        runtime = AgentRuntime(config, collector=object())
        server = MonitorHTTPServer(("127.0.0.1", 0), config, runtime)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def request(path, authenticated=True):
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
            headers = {"Authorization": "Bearer " + config["token"]} if authenticated else {}
            connection.request("GET", path, headers=headers)
            response = connection.getresponse()
            result = (response.status, response.getheader("Content-Type"), response.read())
            connection.close()
            return result
        try:
            self.assertEqual(request("/metrics", False)[0], 401)
            result = request("/metrics?url=http://evil.example/steal")
            self.assertEqual(result[0], 200)
            self.assertIn("text/plain", result[1])
            self.assertEqual(result[2], b"windows_test_metric 42\n")
            self.assertEqual(calls, ["/metrics"])
            config["exporter_url"] = f"http://127.0.0.1:{exporter.server_port}/redirect"
            self.assertEqual(request("/metrics")[0], 502)
            self.assertNotIn("/secret", calls)
            config["exporter_url"] = "http://example.com/metrics"
            self.assertEqual(request("/metrics")[0], 400)
        finally:
            server.shutdown()
            server.server_close()
            exporter.shutdown()
            exporter.server_close()
            thread.join()
            exporter_thread.join()

    def test_snapshot_cache_and_bearer_enforcement(self):
        class FakeCollector:
            calls = 0
            def sample(self):
                self.calls += 1
                return snapshot()
        collector = FakeCollector()
        config = {"mode": "agent", "token": "long-test-token-123456789", "allowed_log_roots": [], "poll_seconds": 5}
        runtime = AgentRuntime(config, collector)
        runtime.sample()
        runtime.sample()
        self.assertEqual(collector.calls, 1)
        server = MonitorHTTPServer(("127.0.0.1", 0), config, runtime)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for token, expected in ((None, 401), ("wrong", 401), (config["token"], 200)):
                connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
                headers = {"Authorization": "Bearer " + token} if token else {}
                connection.request("GET", "/snapshot", headers=headers)
                response = connection.getresponse()
                response.read()
                self.assertEqual(response.status, expected)
                connection.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_claim_validation_timezones_and_limits(self):
        with self.assertRaises(ValueError):
            validate_claim(claim_payload(expected_end="2026-09-23T12:00:00"))
        with self.assertRaises(ValueError):
            validate_claim(claim_payload(owner_name="a" * 41))
        with self.assertRaises(ValueError):
            validate_claim(claim_payload(total_units=True))
        result = validate_claim(claim_payload(expected_end="2026-09-23T12:00:00+08:00"))
        self.assertEqual(result["expected_end"], "2026-09-23T04:00:00Z")


if __name__ == "__main__":
    unittest.main()
