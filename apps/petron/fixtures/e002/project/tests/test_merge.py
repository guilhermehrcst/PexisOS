import unittest

from intervals.merge import merge, total_length


class MergeTests(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(merge([]), [])

    def test_overlapping(self):
        self.assertEqual(merge([(1, 4), (2, 3), (5, 6)]), [(1, 4), (5, 6)])

    def test_unsorted_input(self):
        self.assertEqual(merge([(5, 6), (1, 3), (2, 4)]), [(1, 4), (5, 6)])

    def test_touching_intervals_merge(self):
        self.assertEqual(merge([(1, 2), (2, 3)]), [(1, 3)])

    def test_input_not_mutated(self):
        data = [(1, 4), (2, 3)]
        merge(data)
        self.assertEqual(data, [(1, 4), (2, 3)])


class TotalLengthTests(unittest.TestCase):
    def test_disjoint(self):
        self.assertEqual(total_length([(0, 1), (2, 4)]), 3)


if __name__ == "__main__":
    unittest.main()
