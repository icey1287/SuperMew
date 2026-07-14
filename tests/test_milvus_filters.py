import unittest

from backend.security.milvus_filters import eq_filter, in_filter


class MilvusFilterTests(unittest.TestCase):
    def test_string_value_is_escaped_as_one_literal(self):
        expression = eq_filter("filename", 'a" or id >= 0 or filename == "b')
        self.assertEqual(
            'filename == "a\\" or id >= 0 or filename == \\"b"',
            expression,
        )

    def test_field_name_cannot_inject_expression(self):
        with self.assertRaises(ValueError):
            eq_filter("filename or id", "x")

    def test_in_filter_escapes_every_value(self):
        self.assertEqual(
            'chunk_id in ["a", "b\\"c"]', in_filter("chunk_id", ["a", 'b"c'])
        )


if __name__ == "__main__":
    unittest.main()
