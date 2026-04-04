import unittest

from src.core.tiling import compute_tile_positions


class ComputeTilePositionsTests(unittest.TestCase):
    def test_two_windows_split_single_monitor_without_outer_overflow(self) -> None:
        tiles = compute_tile_positions(
            count=2,
            start_monitor=1,
            monitor_count=1,
            monitor_width=1920,
            monitor_height=1080,
            taskbar_height=40,
            margin=0,
            border_offset=7,
        )

        self.assertEqual(len(tiles), 2)

        left, right = tiles
        self.assertEqual(left.x, 0)
        self.assertEqual(left.width, 967)
        self.assertEqual(right.x, 953)
        self.assertEqual(right.width, 967)
        self.assertEqual(left.height, 1040)
        self.assertEqual(right.height, 1040)

        self.assertGreaterEqual(left.x, 0)
        self.assertLessEqual(right.x + right.width, 1920)

    def test_internal_overlap_preserved_for_multi_column_layout(self) -> None:
        tiles = compute_tile_positions(
            count=4,
            start_monitor=1,
            monitor_count=1,
            monitor_width=1920,
            monitor_height=1080,
            taskbar_height=40,
            margin=0,
            border_offset=7,
        )

        self.assertEqual(len(tiles), 4)

        top_left, top_right, bottom_left, bottom_right = tiles
        self.assertEqual(top_left.x, 0)
        self.assertEqual(top_right.x, 953)
        self.assertEqual(bottom_left.x, 0)
        self.assertEqual(bottom_right.x, 953)

        self.assertLessEqual(top_right.x + top_right.width, 1920)
        self.assertLessEqual(bottom_right.x + bottom_right.width, 1920)
        self.assertEqual(top_left.height, 527)
        self.assertEqual(bottom_left.height, 520)

    def test_start_monitor_offsets_tiles_to_selected_display(self) -> None:
        tiles = compute_tile_positions(
            count=2,
            start_monitor=2,
            monitor_count=1,
            monitor_width=1920,
            monitor_height=1080,
            taskbar_height=40,
            margin=0,
            border_offset=7,
        )

        self.assertEqual(len(tiles), 2)

        left, right = tiles
        self.assertEqual(left.x, 1920)
        self.assertEqual(right.x, 2873)
        self.assertLessEqual(right.x + right.width, 3840)


if __name__ == "__main__":
    unittest.main()
