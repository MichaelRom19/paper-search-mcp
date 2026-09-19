"""Preserved offline regressions; live probes are isolated in tests/live."""
import unittest
import tempfile
import shutil
import os
import requests
from paper_search_mcp.academic_platforms.sci_hub import SciHubFetcher


class TestSciHubFetcher(unittest.TestCase):

    def setUp(self):
        # Create temporary directory for downloads
        self.test_dir = tempfile.mkdtemp(prefix="sci_hub_test_")
        self.fetcher = SciHubFetcher(output_dir=self.test_dir)


    def tearDown(self):
        # Clean up temporary directory
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir)


    def test_init(self):
        """Test initialization of SciHubFetcher"""
        self.assertEqual(self.fetcher.base_url, "https://sci-hub.se")
        self.assertTrue(os.path.exists(self.test_dir))
        self.assertIsNotNone(self.fetcher.session)


    def test_init_custom_url(self):
        """Test initialization with custom URL"""
        custom_fetcher = SciHubFetcher(base_url="https://sci-hub.ru/", output_dir=self.test_dir)
        self.assertEqual(custom_fetcher.base_url, "https://sci-hub.ru")


    def test_download_pdf_empty_query(self):
        """Test download with empty query"""
        result = self.fetcher.download_pdf("")
        self.assertIsNone(result)

        result = self.fetcher.download_pdf("   ")
        self.assertIsNone(result)


    def test_generate_filename(self):
        """Test filename generation"""
        # Mock response object
        class MockResponse:
            def __init__(self, url, content):
                self.url = url
                self.content = content.encode()
        
        # Test with PDF URL
        response = MockResponse("https://example.com/paper.pdf", "fake pdf content")
        filename = self.fetcher._generate_filename(response, "10.1234/test")
        self.assertTrue(filename.endswith('.pdf'))
        self.assertIn('_', filename)  # Should contain hash separator
        
        # Test with non-PDF URL
        response = MockResponse("https://example.com/page", "fake content")
        filename = self.fetcher._generate_filename(response, "test-paper")
        self.assertTrue(filename.endswith('.pdf'))
        self.assertIn('test-paper', filename)


    def test_get_direct_url_pdf_url(self):
        """Test _get_direct_url with direct PDF URL"""
        pdf_url = "https://example.com/paper.pdf"
        result = self.fetcher._get_direct_url(pdf_url)
        self.assertEqual(result, pdf_url)


    def test_session_headers(self):
        """Test that session has proper headers"""
        self.assertIn('User-Agent', self.fetcher.session.headers)
        user_agent = self.fetcher.session.headers['User-Agent']
        self.assertIn('Mozilla', user_agent)


    def test_output_directory_creation(self):
        """Test that output directory is created"""
        new_dir = os.path.join(self.test_dir, "subdir", "nested")
        fetcher = SciHubFetcher(output_dir=new_dir)
        self.assertTrue(os.path.exists(new_dir))
