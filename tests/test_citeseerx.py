"""Preserved offline regressions; live probes are isolated in tests/live."""
import unittest
import requests
from unittest.mock import Mock, patch
from paper_search_mcp.academic_platforms.citeseerx import CiteSeerXSearcher


class TestCiteSeerXSearcher(unittest.TestCase):

    def setUp(self):
        self.searcher = CiteSeerXSearcher()


    def test_parse_citeseerx_result_minimal(self):
        result = {
            "info": {
                "id": "12345",
                "title": "A Test Paper",
                "authors": [{"name": "Alice"}, {"name": "Bob"}],
                "abstract": "Test abstract for parser.",
                "year": "2024",
                "venue": "TestConf",
                "doi": "10.1000/test-doi",
                "url": "https://citeseerx.ist.psu.edu/viewdoc/summary?doi=10.1.1.1",
                "pdf": "https://example.org/test.pdf",
            }
        }

        paper = self.searcher._parse_citeseerx_result(result)
        self.assertIsNotNone(paper)
        if paper:
            self.assertEqual(paper.title, "A Test Paper")
            self.assertEqual(paper.source, "citeseerx")
            self.assertEqual(paper.doi, "10.1000/test-doi")
            self.assertEqual(paper.authors, ["Alice", "Bob"])


    def test_parse_citeseerx_result_invalid(self):
        paper = self.searcher._parse_citeseerx_result({"info": {}})
        self.assertIsNone(paper)


    def test_archive_redirect_returns_empty_search(self):
        """_get should raise HTTPError when the API redirects to web.archive.org."""
        archive_response = Mock()
        archive_response.url = "https://web.archive.org/web/20251230112235/https://citeseerx.ist.psu.edu/api/search"
        archive_response.raise_for_status.return_value = None

        with patch.object(self.searcher.session, "get", return_value=archive_response):
            papers = self.searcher.search("machine learning", max_results=3)

        # Should return empty list rather than crashing
        self.assertIsInstance(papers, list)
        self.assertEqual(papers, [])
