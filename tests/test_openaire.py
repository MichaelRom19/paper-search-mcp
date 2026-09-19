"""Preserved offline regressions; live probes are isolated in tests/live."""
import unittest
import os
import requests
import urllib3
from paper_search_mcp.academic_platforms.openaire import OpenAiresearcher


class TestOpenAiresearcher(unittest.TestCase):

    def setUp(self):
        self.searcher = OpenAiresearcher()


    def test_download_pdf_not_implemented(self):
        """Test that PDF download raises NotImplementedError"""
        searcher = OpenAiresearcher()
        with self.assertRaises(NotImplementedError):
            searcher.download_pdf("test_paper_id", "./downloads")


    def test_read_paper_not_implemented(self):
        """Test that reading paper raises NotImplementedError"""
        searcher = OpenAiresearcher()
        with self.assertRaises(NotImplementedError):
            searcher.read_paper("test_paper_id", "./downloads")


    def test_parse_openaire_result_invalid(self):
        """Test parsing invalid OpenAIRE result"""
        # Create an invalid result dictionary
        invalid_result = {
            'metadata': {}  # Empty metadata
        }

        # This should return None without raising exception
        paper = self.searcher._parse_openaire_result(invalid_result)
        self.assertIsNone(paper)


    def test_parse_openaire_result_minimal(self):
        """Test parsing minimal valid OpenAIRE result"""
        minimal_result = {
            'metadata': {
                'title': {'value': 'Test Paper Title'},
                'creator': [{'value': 'Test Author'}],
            },
            'header': {
                'dri:objIdentifier': {'value': 'test-id-123'}
            }
        }

        paper = self.searcher._parse_openaire_result(minimal_result)
        self.assertIsNotNone(paper)
        if paper:
            self.assertEqual(paper.title, 'Test Paper Title')
            self.assertEqual(paper.authors, ['Test Author'])
            self.assertEqual(paper.source, 'openaire')
