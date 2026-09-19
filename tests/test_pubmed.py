"""Preserved offline regressions; live probes are isolated in tests/live."""
import unittest
from paper_search_mcp.academic_platforms.pubmed import PubMedSearcher


class TestPubMedSearcher(unittest.TestCase):

    def test_pdf_unsupported(self):
        searcher = PubMedSearcher()
        with self.assertRaises(NotImplementedError):
            searcher.download_pdf("12345678", "./downloads")


    def test_read_paper_message(self):
        searcher = PubMedSearcher()
        message = searcher.read_paper("12345678")
        self.assertIn("PubMed papers cannot be read directly", message)
