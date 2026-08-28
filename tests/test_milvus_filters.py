import unittest

from backend.security.milvus_filters import (
    and_filter,
    eq_filter,
    in_filter,
    not_in_filter,
    or_filter,
    version_identity_filter,
    version_scope_filter,
)


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

    def test_and_filter_wraps_each_expression_and_fails_closed_when_empty(self):
        self.assertEqual(
            '(tenant_id == "t1") and (document_id == "d1")',
            and_filter(eq_filter("tenant_id", "t1"), eq_filter("document_id", "d1")),
        )
        self.assertEqual("id < 0", and_filter("", None))

    def test_or_and_not_in_filters_have_safe_empty_semantics(self):
        self.assertEqual(
            '(document_version_id == "v1") or (document_version_id == "v2")',
            or_filter(
                eq_filter("document_version_id", "v1"),
                eq_filter("document_version_id", "v2"),
            ),
        )
        self.assertEqual("id < 0", or_filter())
        self.assertEqual("id >= 0", not_in_filter("document_id", []))
        self.assertEqual(
            'document_id not in ["doc-1", "doc-2\\" or id >= 0"]',
            not_in_filter("document_id", ["doc-1", 'doc-2" or id >= 0']),
        )

    def test_version_identity_filter_is_composable_and_fails_closed(self):
        self.assertEqual(
            '(document_version_id in ["version-1", "version-2"]) and '
            '(index_version == "index-v2")',
            version_identity_filter(
                [" version-1 ", "version-2", "version-1"],
                index_version="index-v2",
            ),
        )
        self.assertEqual(
            "(id < 0)",
            version_identity_filter([], index_version=None),
        )

    def test_version_scope_uses_safe_in_filter_and_deduplicates_versions(self):
        expression = version_scope_filter(
            tenant_id='tenant" or id >= 0',
            knowledge_base_id="kb-1",
            document_id="doc-1",
            document_version_ids=["version-1", 'version-2" or id >= 0', "version-1"],
            index_version="index-v2",
        )
        self.assertEqual(
            '(tenant_id == "tenant\\" or id >= 0") and '
            '(knowledge_base_id == "kb-1") and '
            '(document_id == "doc-1") and '
            '(document_version_id in ["version-1", '
            '"version-2\\" or id >= 0"]) and '
            '(index_version == "index-v2")',
            expression,
        )

    def test_empty_version_scope_is_fail_closed(self):
        expression = version_scope_filter(
            tenant_id="tenant-1",
            knowledge_base_id="kb-1",
            document_id="doc-1",
            document_version_ids=[],
        )
        self.assertIn("(id < 0)", expression)

    def test_version_scope_rejects_empty_tenant_scope(self):
        with self.assertRaises(ValueError):
            version_scope_filter(
                tenant_id="",
                knowledge_base_id="kb-1",
                document_id="doc-1",
                document_version_ids=["version-1"],
            )


if __name__ == "__main__":
    unittest.main()
