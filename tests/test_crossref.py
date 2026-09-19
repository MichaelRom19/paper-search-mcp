"""Preserved offline regressions; live probes are isolated in tests/live."""
import unittest
import os
import requests
from paper_search_mcp.academic_platforms.crossref import CrossRefSearcher


class TestCrossRefSearcher(unittest.TestCase):

    def setUp(self):
        self.searcher = CrossRefSearcher()


    def test_download_pdf_not_supported(self):
        with self.assertRaises(NotImplementedError) as context:
            self.searcher.download_pdf("10.1038/nature12373", "./downloads")
        
        self.assertIn("CrossRef does not provide direct PDF downloads", str(context.exception))


    def test_read_paper_not_supported(self):
        message = self.searcher.read_paper("10.1038/nature12373")
        self.assertIn("CrossRef papers cannot be read directly", message)
        self.assertIn("metadata and abstracts are available", message)


    def test_search_error_handling(self):
        # Test with invalid search parameters to check error handling
        papers = self.searcher.search("", max_results=0)  # Empty query
        self.assertEqual(len(papers), 0)


    def test_user_agent_header(self):
        # Test that the session has the correct user agent
        self.assertIn("paper-search-mcp", self.searcher.http.headers.get('User-Agent', ''))
        self.assertIn("https://github.com/", self.searcher.http.headers.get('User-Agent', ''))
