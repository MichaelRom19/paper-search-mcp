import unittest
import requests
from unittest.mock import Mock, patch

from paper_search_mcp.academic_platforms.citeseerx import CiteSeerXSearcher


def check_api_accessible() -> bool:
    """Check whether CiteSeerX search API is reachable and *not* redirecting to archive."""
    try:
        response = requests.get(
            "https://citeseerx.ist.psu.edu/api/search",
            params={"q": "test", "max": 1, "start": 0},
            timeout=10,
        )
        return response.status_code == 200 and "web.archive.org" not in response.url
    except Exception:
        return False


class TestCiteSeerXSearcher(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.api_accessible = check_api_accessible()
        if not cls.api_accessible:
            print("\nWarning: CiteSeerX API is not accessible, network tests will be skipped")

    def setUp(self):
        self.searcher = CiteSeerXSearcher()

    def test_search_basic(self):
        if not self.api_accessible:
            self.skipTest("CiteSeerX API is not accessible")

        papers = self.searcher.search("machine learning", max_results=3)
        self.assertIsInstance(papers, list)
        self.assertTrue(len(papers) >= 0)

        if papers:
            first = papers[0]
            self.assertTrue(first.title)
            self.assertEqual(first.source, "citeseerx")





if __name__ == "__main__":
    unittest.main()
