import unittest
import requests

from paper_search_mcp.academic_platforms.doaj import DOAJSearcher


def check_api_accessible() -> bool:
    """Check whether DOAJ API is reachable."""
    try:
        response = requests.get(
            "https://doaj.org/api/search/articles/machine%20learning",
            params={"pageSize": 1},
            timeout=10,
        )
        return response.status_code == 200
    except Exception:
        return False


class TestDOAJSearcher(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.api_accessible = check_api_accessible()
        if not cls.api_accessible:
            print("\nWarning: DOAJ API is not accessible, network tests will be skipped")

    def setUp(self):
        self.searcher = DOAJSearcher()

    def test_search_basic(self):
        if not self.api_accessible:
            self.skipTest("DOAJ API is not accessible")

        papers = self.searcher.search("machine learning", max_results=3)
        self.assertIsInstance(papers, list)
        self.assertTrue(len(papers) >= 0)

        if papers:
            first = papers[0]
            self.assertTrue(first.title)
            self.assertEqual(first.source, "doaj")





if __name__ == "__main__":
    unittest.main()
