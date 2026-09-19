"""Preserved offline regressions; live probes are isolated in tests/live."""
import unittest
import requests
from paper_search_mcp.academic_platforms.zenodo import ZenodoSearcher


class TestZenodoSearcher(unittest.TestCase):

    def setUp(self):
        self.searcher = ZenodoSearcher()


    def test_extract_record_id(self):
        self.assertEqual(self.searcher._extract_record_id("10.5281/zenodo.1234567"), "1234567")
        self.assertEqual(self.searcher._extract_record_id("1234567"), "1234567")
        self.assertEqual(self.searcher._extract_record_id("not-a-zenodo-id"), "")


    def test_parse_record_minimal(self):
        hit = {
            "id": 12345,
            "doi": "10.5281/zenodo.12345",
            "metadata": {
                "title": "Zenodo Parser Test",
                "creators": [{"name": "Alice Example"}, {"name": "Bob Example"}],
                "description": "<p>Test abstract</p>",
                "publication_date": "2024-01-15",
            },
            "files": [
                {
                    "key": "paper.pdf",
                    "links": {"self": "https://zenodo.org/records/12345/files/paper.pdf"},
                }
            ],
            "links": {"html": "https://zenodo.org/record/12345"},
        }

        paper = self.searcher._parse_record(hit)
        self.assertIsNotNone(paper)
        if paper:
            self.assertEqual(paper.source, "zenodo")
            self.assertEqual(paper.title, "Zenodo Parser Test")
            self.assertEqual(paper.doi, "10.5281/zenodo.12345")
            self.assertTrue(paper.pdf_url.endswith("paper.pdf"))


    def test_parse_record_invalid(self):
        paper = self.searcher._parse_record({"metadata": {}})
        self.assertIsNone(paper)
