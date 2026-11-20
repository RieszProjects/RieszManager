"""rieszctl's message argument handling (client.py)."""

from __future__ import annotations

import unittest

from riesz_runner.client import CliError, read_message


class ReadMessageTest(unittest.TestCase):
    def test_words_are_the_message(self):
        self.assertEqual(read_message(["你好", "世界"]), "你好 世界")

    def test_a_dash_among_other_words_is_refused(self):
        # A session id split by a glob into two arguments once sent "<half an id> -" instead of stdin.
        with self.assertRaises(CliError):
            read_message(["2877-467d-a10a-897444be1f26", "-"])


if __name__ == "__main__":
    unittest.main()
