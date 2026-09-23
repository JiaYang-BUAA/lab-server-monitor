"""Paragraph attribution must survive unrelated edits and legacy migration."""
import unittest

from labmon.board_segments import initial_segments, reconcile_segments


class BoardSegmentTests(unittest.TestCase):
    def test_legacy_content_has_no_invented_paragraph_author(self):
        old = initial_segments("第一段\n\n第二段", "2026-09-23T10:00:00+00:00")
        self.assertEqual([item["text"] for item in old], ["第一段", "", "第二段"])
        self.assertTrue(all(item["legacy"] and item["updated_by"] is None for item in old))
        changed = reconcile_segments(old, "第一段\n\n第二段已修改", "张三", "2026-09-24T10:00:00+00:00")
        self.assertEqual(changed[:2], old[:2])
        self.assertEqual(changed[2]["updated_by"], "张三")
        self.assertFalse(changed[2]["legacy"])

    def test_insert_keeps_neighbors_and_exact_text(self):
        first = reconcile_segments([], "甲\n乙\n乙\n丙", "甲", "t1")
        second = reconcile_segments(first, "甲\n新\n乙\n乙\n丙", "乙", "t2")
        self.assertEqual("\n".join(item["text"] for item in second), "甲\n新\n乙\n乙\n丙")
        self.assertEqual([item["updated_at"] for item in second], ["t1", "t2", "t1", "t1", "t1"])
        third = reconcile_segments(second, "甲\n新\n乙\n丙", "丙", "t3")
        self.assertEqual("\n".join(item["text"] for item in third), "甲\n新\n乙\n丙")
        self.assertEqual(third[-1]["updated_at"], "t1")

    def test_large_many_lines_has_bounded_diff_fallback(self):
        source = "\n".join(str(index) for index in range(600))
        first = reconcile_segments([], source, "A", "t1")
        second = reconcile_segments(first, source.replace("300", "updated"), "B", "t2")
        self.assertEqual(second[0]["updated_at"], "t1")
        self.assertEqual(second[300]["updated_at"], "t2")
        self.assertEqual(second[-1]["updated_at"], "t1")


if __name__ == "__main__":
    unittest.main()
