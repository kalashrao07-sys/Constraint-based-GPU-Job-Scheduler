import os, sys, tempfile, unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from e2_persistence import Repository
from e4_web import auth, create_app


class E4Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "t.db")
        self.repo = Repository(self.db)
        self.repo.init()
        self.repo.create_user("alice", "pw-alice", "submitter")
        self.repo.create_user("bob", "pw-bob", "submitter")
        self.repo.create_user("root", "pw-root", "admin")
        self.gpu = self.repo.add_gpu("RTX", "RTX", 4)
        auth._FAILS.clear()
        self.app = create_app(self.db)

    def tearDown(self):
        self.repo.close()

    def client(self, user=None):
        c = self.app.test_client()
        if user:
            r = c.post("/login", json={"username": user, "password": "pw-" + user})
            self.assertEqual(r.status_code, 200)
        return c

    GOOD = {"template": "matmul", "memory_gb": 1, "duration_min": 1, "priority": 3}

    def test_login_required(self):
        c = self.app.test_client()
        self.assertEqual(c.get("/pool", headers={"Accept": "application/json"}).status_code, 401)
        self.assertEqual(c.get("/jobs/new").status_code, 302)

    def test_wrong_password_and_lockout(self):
        c = self.app.test_client()
        for _ in range(5):
            self.assertEqual(c.post("/login", json={"username": "alice", "password": "x"}).status_code, 401)
        self.assertEqual(c.post("/login", json={"username": "alice", "password": "pw-alice"}).status_code, 429)

    def test_valid_submission_fr1(self):
        r = self.client("alice").post("/jobs", json=self.GOOD)
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.get_json()["state"], "queued")

    def test_invalid_input_rejected_and_never_stored_fr3(self):
        c = self.client("alice")
        bad = [dict(self.GOOD, priority=9), dict(self.GOOD, priority=0), dict(self.GOOD, priority=2.5),
               dict(self.GOOD, memory_gb=0), dict(self.GOOD, memory_gb=-1), dict(self.GOOD, memory_gb="abc"),
               dict(self.GOOD, duration_min=0), dict(self.GOOD, duration_min=10 ** 6),
               dict(self.GOOD, template="rm -rf /"), dict(self.GOOD, deadline="tomorrow-ish"),
               {"template": "matmul"}, dict(self.GOOD, memory_gb=float("nan"))]
        for payload in bad:
            r = c.post("/jobs", json=payload)
            self.assertEqual(r.status_code, 400, payload)
            self.assertIn("field", r.get_json())
        self.assertEqual(self.repo.list_jobs(), [])

    def test_deadline_is_optional_metadata_fr2(self):
        r = self.client("alice").post("/jobs", json=dict(self.GOOD, deadline="2026-11-05T18:30"))
        self.assertEqual(r.status_code, 201)
        self.assertEqual(self.repo.get_job(r.get_json()["job_id"])["deadline"], "2026-11-05T18:30")

    def test_own_job_only_fr18(self):
        jid = self.client("alice").post("/jobs", json=self.GOOD).get_json()["job_id"]
        j = {"Accept": "application/json"}
        self.assertEqual(self.client("alice").get(f"/jobs/{jid}", headers=j).status_code, 200)
        self.assertEqual(self.client("bob").get(f"/jobs/{jid}", headers=j).status_code, 404)
        self.assertEqual(self.client("root").get(f"/jobs/{jid}", headers=j).status_code, 200)
        self.assertEqual(self.client("bob").get("/jobs", headers=j).get_json(), [])

    def test_admin_pages_are_server_side_guarded_fr19_fr20(self):
        c = self.client("alice")
        self.assertEqual(c.get("/admin").status_code, 403)
        self.assertEqual(c.get("/admin/held").status_code, 403)
        self.assertEqual(c.post("/admin/gpus", json={"name": "x", "gpu_type": "y", "memory_gb": 8}).status_code, 403)
        self.assertEqual(c.post(f"/admin/gpus/{self.gpu}/remove", json={}).status_code, 403)
        self.assertEqual(self.client("root").get("/admin").status_code, 200)

    def test_simulated_gpus_are_refused_until_approved(self):
        r = self.client("root").post("/admin/gpus", json={"name": "x", "gpu_type": "y", "memory_gb": 8})
        self.assertEqual(r.status_code, 403)
        self.assertIn("approves", r.get_json()["error"])

    def test_remove_gpu_with_running_job_blocked_uc6(self):
        jid = self.client("alice").post("/jobs", json=self.GOOD).get_json()["job_id"]
        self.repo.commit_assignment(jid, self.gpu, 1, 61, "x")
        r = self.client("root").post(f"/admin/gpus/{self.gpu}/remove", json={})
        self.assertEqual(r.status_code, 409)
        self.repo.complete_job(jid, "done")
        self.assertEqual(self.client("root").post(f"/admin/gpus/{self.gpu}/remove", json={}).status_code, 200)

    def test_pool_hides_other_users_names(self):
        jid = self.client("alice").post("/jobs", json=self.GOOD).get_json()["job_id"]
        self.repo.commit_assignment(jid, self.gpu, 1, 61, "x")
        j = {"Accept": "application/json"}
        bob = self.client("bob").get("/pool", headers=j).get_json()["gpus"][0]
        self.assertEqual((bob["busy"], bob["who"], bob["job_id"]), (True, "another user", None))
        me = self.client("alice").get("/pool", headers=j).get_json()["gpus"][0]
        self.assertEqual((me["who"], me["job_id"]), ("you", jid))
        adm = self.client("root").get("/pool", headers=j).get_json()["gpus"][0]
        self.assertEqual(adm["who"], "alice")

    def test_form_post_without_csrf_token_is_rejected(self):
        c = self.client("alice")
        r = c.post("/jobs", data={"template": "matmul", "memory_gb": "1", "duration_min": "1", "priority": "3"})
        self.assertEqual(r.status_code, 400)

    def test_form_post_with_token_works_and_pages_render(self):
        c = self.app.test_client()
        page = c.get("/login").get_data(as_text=True)
        token = page.split('name="csrf_token" value="')[1].split('"')[0]
        r = c.post("/login", data={"username": "alice", "password": "pw-alice", "csrf_token": token})
        self.assertEqual(r.status_code, 302)
        token = c.get("/jobs/new").get_data(as_text=True).split('name="csrf_token" value="')[1].split('"')[0]
        r = c.post("/jobs", data={"template": "train", "memory_gb": "1", "duration_min": "0.5", "priority": "2",
                                  "csrf_token": token})
        self.assertEqual(r.status_code, 302)
        for path in ("/jobs", "/pool", r.headers["Location"]):
            self.assertEqual(c.get(path).status_code, 200, path)

    def test_html_is_escaped(self):
        self.repo.rename_gpu(self.gpu, "<script>alert(1)</script>")
        html = self.client("alice").get("/pool").get_data(as_text=True)
        self.assertNotIn("<script>alert(1)</script>", html)


if __name__ == "__main__":
    unittest.main()
