"""Preserved offline regressions; live probes are isolated in tests/live."""
import unittest
import asyncio
import os
from paper_search_mcp import server


class TestPaperSearchServer(unittest.TestCase):

    def test_all_sources_include_new_platforms(self):
        self.assertIn("dblp", server.ALL_SOURCES)
        self.assertIn("openaire", server.ALL_SOURCES)
        self.assertIn("citeseerx", server.ALL_SOURCES)
        self.assertIn("doaj", server.ALL_SOURCES)
        self.assertNotIn("base", server.ALL_SOURCES)
        self.assertIn("zenodo", server.ALL_SOURCES)
        self.assertIn("hal", server.ALL_SOURCES)
        self.assertIn("ssrn", server.ALL_SOURCES)
        self.assertEqual("unpaywall" in server.ALL_SOURCES, server.SOURCES["unpaywall"].describe().available)


    def test_parse_sources_with_new_platforms(self):
        parsed = server._parse_sources("dblp,doaj,base,zenodo,hal,ssrn,unpaywall,invalid")
        self.assertEqual(parsed, [name for name in ["dblp", "doaj", "zenodo", "hal", "ssrn", "unpaywall"] if name in server.ALL_SOURCES])
