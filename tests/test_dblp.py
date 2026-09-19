"""Preserved offline regressions; live probes are isolated in tests/live."""
import unittest
import os
import requests
from paper_search_mcp.academic_platforms.dblp import DBLPSearcher


class TestDBLPSearcher(unittest.TestCase):

    def setUp(self):
        self.searcher = DBLPSearcher()


    def test_download_pdf_not_supported(self):
        """Test that PDF download raises NotImplementedError"""
        searcher = DBLPSearcher()
        with self.assertRaises(NotImplementedError):
            searcher.download_pdf("test_paper_id", "./downloads")


    def test_read_paper_not_supported(self):
        """Test that reading paper raises NotImplementedError"""
        searcher = DBLPSearcher()
        with self.assertRaises(NotImplementedError):
            searcher.read_paper("test_paper_id", "./downloads")


    def test_parse_dblp_hit_invalid(self):
        """Test parsing invalid dblp hit"""
        import xml.etree.ElementTree as ET

        # Create an invalid XML element
        root = ET.Element('root')
        hit = ET.SubElement(root, 'hit')
        # Missing 'info' element

        # This should return None without raising exception
        paper = self.searcher._parse_dblp_hit(hit)
        self.assertIsNone(paper)
