"""Realistic identity boundaries for one-click simulation job claims."""
import unittest

from labmon.grouping import group_jobs


def proc(pid, parent=None, start=100, *, name="standard.exe", software="abaqus",
         role="solver", owner="researcher", cpu=10):
    item = {"pid": pid, "parent_pid": parent, "key": f"{pid}:{start}",
            "start_time": start, "name": name, "software": software, "role": role,
            "owner": owner, "cpu_pct": cpu, "memory_bytes": 1024}
    return item


class GroupingTests(unittest.TestCase):
    def test_solver_tree_remains_one_claimable_job(self):
        jobs = group_jobs("host", [proc(10), proc(11, 10, 101), proc(12, 11, 102)], now=200)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["pids"], [10, 11, 12])
        self.assertEqual(jobs[0]["cpu_pct"], 30)
        self.assertEqual(jobs[0]["elapsed_seconds"], 100)

    def test_mpi_proxy_groups_ranks_without_counting_proxy(self):
        proxy = proc(10, start=90, name="HYDRA_PMI_PROXY.EXE", software="system", role="system")
        ranks = [proc(i, 10, 100 + i / 10, name="fl_mpi2520", software="fluent") for i in (11, 12)]
        jobs = group_jobs("old", [proxy] + ranks)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["pids"], [11, 12])
        self.assertEqual(jobs[0]["process_count"], 2)
        self.assertEqual(group_jobs("old", [proxy]), [])
        original_id = jobs[0]["id"]
        self.assertEqual(group_jobs("old", [proxy, ranks[1], proc(13, 10, 103,
            name="fl_mpi2520", software="fluent")])[0]["id"], original_id)

    def test_distinct_mpi_invocations_under_shared_service_stay_separate(self):
        shared = proc(1, start=10, name="hydra_service", software="system", role="system")
        first = proc(2, 1, 20, name="mpiexec.exe", software="system", role="system")
        second = proc(3, 1, 21, name="mpiexec.exe", software="system", role="system")
        workers = [proc(4, 2, 22, name="fl_mpi2520", software="fluent"),
                   proc(5, 2, 23, name="fl_mpi2520", software="fluent"),
                   proc(6, 3, 24, name="fl_mpi2520", software="fluent"),
                   proc(7, 3, 25, name="fl_mpi2520", software="fluent")]
        jobs = group_jobs("old", [shared, first, second] + workers)
        self.assertEqual(sorted(job["pids"] for job in jobs), [[4, 5], [6, 7]])
        self.assertEqual(len({job["id"] for job in jobs}), 2)
        replacement = proc(2, 1, 40, name="mpiexec.exe", software="system", role="system")
        new_workers = [proc(8, 2, 41, name="fl_mpi2520", software="fluent")]
        new_id = group_jobs("old", [shared, replacement] + new_workers)[0]["id"]
        self.assertNotIn(new_id, {job["id"] for job in jobs})

    def test_generic_shell_and_reused_parent_cannot_group_siblings(self):
        shell = proc(1, start=10, name="cmd.exe", software="system", role="system")
        workers = [proc(2, 1, 20), proc(3, 1, 21)]
        self.assertEqual(sorted(job["pids"] for job in group_jobs("host", [shell] + workers)), [[2], [3]])
        reused_launcher = proc(1, start=30, name="mpirun", software="system", role="system")
        self.assertEqual(sorted(job["pids"] for job in group_jobs("host", [reused_launcher] + workers)),
                         [[2], [3]])

    def test_shared_gui_does_not_merge_independent_solver_children(self):
        gui = proc(1, start=10, name="ABQcaeK.exe", role="application")
        solvers = [proc(2, 1, 20), proc(3, 1, 21)]
        jobs = group_jobs("host", [gui] + solvers)
        self.assertEqual(sorted(job["pids"] for job in jobs), [[1], [2], [3]])

    def test_disconnected_fluent_trees_require_manual_grouping(self):
        # This mirrors the old machine: the main process and MPI proxy are in
        # disconnected visible trees. Time/owner/software alone are not proof.
        main = proc(100, 80, 100, name="fluent", software="fluent")
        proxy = proc(200, 90, 103, name="hydra_pmi_proxy", software="system", role="system")
        ranks = [proc(201, 200, 104, name="fl_mpi2520", software="fluent"),
                 proc(202, 200, 105, name="fl_mpi2520", software="fluent")]
        self.assertEqual(sorted(job["pids"] for job in group_jobs("old", [main, proxy] + ranks)),
                         [[100], [201, 202]])

    def test_mpi_launcher_only_groups_same_software(self):
        launcher = proc(1, start=10, name="mpirun", software="system", role="system")
        workers = [proc(2, 1, 20, name="standard.exe", software="abaqus"),
                   proc(3, 1, 21, name="fl_mpi2520", software="fluent")]
        self.assertEqual(sorted(job["pids"] for job in group_jobs("host", [launcher] + workers)),
                         [[2], [3]])


if __name__ == "__main__":
    unittest.main()
