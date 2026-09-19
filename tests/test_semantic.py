"""Preserved offline regressions; live probes are isolated in tests/live."""
import unittest
import os
import requests
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from paper_search_mcp.academic_platforms.semantic import SemanticSearcher


class TestSemanticSearcher(unittest.TestCase):

    def setUp(self):
        self.searcher = SemanticSearcher()


    def test_download_pdf_saves_file_when_pdf_url_available(self):
        paper = SimpleNamespace(pdf_url="https://example.com/paper.pdf")
        response = Mock()
        response.content = b"%PDF-1.4 test content"
        response.raise_for_status.return_value = None

        with tempfile.TemporaryDirectory(prefix="semantic_mock_download_") as test_dir:
            with patch.object(self.searcher, "get_paper_details", return_value=paper):
                with patch("paper_search_mcp.academic_platforms.semantic.requests.get", return_value=response):
                    result = self.searcher.download_pdf("paper/123", test_dir)

            expected_path = Path(test_dir) / "semantic_paper_123.pdf"
            self.assertEqual(result, str(expected_path))
            self.assertTrue(expected_path.exists())
            self.assertEqual(expected_path.read_bytes(), b"%PDF-1.4 test content")


    def test_parse_paper_handles_missing_publication_date(self):
        item = {
            "paperId": "paper-123",
            "title": "Paper without a publication date",
            "authors": [{"name": "Ada Lovelace"}],
            "abstract": "",
            "url": "https://www.semanticscholar.org/paper/paper-123",
            "publicationDate": None,
            "externalIds": {},
            "fieldsOfStudy": None,
            "openAccessPdf": None,
            "citationCount": 0,
        }

        paper = self.searcher._parse_paper(item)

        self.assertIsNotNone(paper)
        self.assertIsNone(paper.published_date)
