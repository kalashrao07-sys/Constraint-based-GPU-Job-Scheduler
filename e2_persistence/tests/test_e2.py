import os, sys, tempfile, unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from e2_persistence import AssignmentRejected, BadState, GpuBusy, Repository


class E2Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = Repository(os.path.join(self.tmp, "t.db"))
        self.repo.init()
        self.uid = self.repo.create_user("u", "secret", "submitter")
        self.gpu = self.repo.add_gpu("G", "A", 8, now=0)
        self.job = self.repo.submit_job(self.uid, "matmul", 4, 1, 3, now=1)

    def tearDown(self):
        self.repo.close()

    def test_atomic_commit_success(self):
        self.repo.commit_assignment(self.job, self.gpu, 10, 70, "only valid GPU", now=10)
        self.assertEqual(self.repo.get_job(self.job)["state"], "running")
        self.assertEqual(self.repo.list_active_gpus()[0]["current_job_id"], self.job)
        self.assertEqual(self.repo.check_consistency(), [])

    def test_nfr1_failure_before_commit_leaves_nothing_behind(self):
        with self.assertRaises(RuntimeError):
            self.repo.commit_assignment(self.job, self.gpu, 10, 70, "x", _fail_before_commit=True)
        j = self.repo.get_job(self.job)
        self.assertEqual(j["state"], "queued")
        self.assertIsNone(j["gpu_id"])                                  # no assignment row
        self.assertIsNone(self.repo.list_active_gpus()[0]["current_job_id"])
        self.assertEqual(self.repo.check_consistency(), [])

    def test_hc2_enforced_inside_the_database(self):
        self.repo.commit_assignment(self.job, self.gpu, 10, 70, "x")
        j2 = self.repo.submit_job(self.uid, "train", 1, 1, 5)
        with self.assertRaises(AssignmentRejected):
            self.repo.commit_assignment(j2, self.gpu, 11, 71, "x")
        self.assertEqual(self.repo.get_job(j2)["state"], "queued")

    def test_hc1_enforced_inside_the_database(self):
        big = self.repo.submit_job(self.uid, "train", 16, 1, 5)
        with self.assertRaises(AssignmentRejected):
            self.repo.commit_assignment(big, self.gpu, 10, 70, "x")

    def test_cannot_assign_a_running_job_twice(self):
        self.repo.commit_assignment(self.job, self.gpu, 10, 70, "x")
        g2 = self.repo.add_gpu("G2", "A", 8)
        with self.assertRaises(BadState):
            self.repo.commit_assignment(self.job, g2, 10, 70, "x")

    def test_remove_gpu_blocked_while_running_uc6(self):
        self.repo.commit_assignment(self.job, self.gpu, 10, 70, "x")
        with self.assertRaises(GpuBusy):
            self.repo.remove_gpu(self.gpu)
        self.repo.complete_job(self.job, "done")
        self.repo.remove_gpu(self.gpu)
        self.assertEqual(self.repo.list_active_gpus(), [])

    def test_complete_releases_gpu_fr16(self):
        self.repo.commit_assignment(self.job, self.gpu, 10, 70, "x")
        self.assertTrue(self.repo.complete_job(self.job, "ok", now=80))
        g = self.repo.list_active_gpus()[0]
        self.assertEqual((g["current_job_id"], g["free_since"]), (None, 80))
        self.assertFalse(self.repo.complete_job(self.job, "again"))     # idempotent

    def test_requeue_frees_gpu_and_clears_assignment(self):
        self.repo.commit_assignment(self.job, self.gpu, 10, 70, "x")
        self.assertTrue(self.repo.requeue_job(self.job, "crash"))
        self.assertEqual(self.repo.get_job(self.job)["state"], "queued")
        self.assertEqual(self.repo.check_consistency(), [])

    def test_database_rejects_invalid_values(self):
        with self.assertRaises(ValueError):
            self.repo.submit_job(self.uid, "matmul", 4, 1, 9)           # priority 9
        with self.assertRaises(ValueError):
            self.repo.submit_job(self.uid, "rm -rf", 4, 1, 3)           # unknown workload
        with self.assertRaises(ValueError):
            self.repo.submit_job(self.uid, "matmul", 0, 1, 3)

    def test_login(self):
        self.assertEqual(self.repo.verify_login("u", "secret")["role"], "submitter")
        self.assertIsNone(self.repo.verify_login("u", "wrong"))
        self.assertIsNone(self.repo.verify_login("nobody", "secret"))

    def test_consistency_check_detects_damage(self):
        self.repo.conn.execute("UPDATE job SET state='running' WHERE job_id=?", (self.job,))   # running, no assignment
        self.assertTrue(any("HC3" in p for p in self.repo.check_consistency()))


if __name__ == "__main__":
    unittest.main()
