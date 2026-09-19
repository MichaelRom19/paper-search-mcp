"""Preserved offline regressions; live probes are isolated in tests/live."""
import unittest
import requests
from paper_search_mcp.academic_platforms.hal import HALSearcher


class TestHALSearcher(unittest.TestCase):

    def setUp(self):
        self.searcher = HALSearcher()


    def test_normalise_id(self):
        self.assertEqual(self.searcher._normalise_id("hal:hal-01234567"), "hal-01234567")
        self.assertEqual(self.searcher._normalise_id("hal-01234567"), "hal-01234567")


    def test_parse_doc_minimal(self):
        doc = {
            "halId_s": "hal-01234567",
            "title_s": ["HAL Parser Test"],
            "authFullName_s": ["Alice Example", "Bob Example"],
            "abstract_s": ["This is a test abstract"],
            "doiId_s": "10.1000/hal-test",
            "publicationDateY_i": 2023,
            "fileMain_s": "https://hal.science/hal-01234567/document",
            "uri_s": "https://hal.science/hal-01234567",
        }

        paper = self.searcher._parse_doc(doc)
        self.assertIsNotNone(paper)
        if paper:
            self.assertEqual(paper.source, "hal")
            self.assertEqual(paper.paper_id, "hal:hal-01234567")
            self.assertEqual(paper.title, "HAL Parser Test")
            self.assertEqual(paper.doi, "10.1000/hal-test")


    def test_parse_doc_invalid(self):
        paper = self.searcher._parse_doc({})
        self.assertIsNone(paper)
