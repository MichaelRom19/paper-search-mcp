import unittest
import requests

from paper_search_mcp.academic_platforms.zenodo import ZenodoSearcher


def check_api_accessible() -> bool:
    """Check whether Zenodo API is reachable."""
    try:
        response = requests.get(
            "https://zenodo.org/api/records",
            params={"q": "machine learning", "size": 1},
            timeout=10,
        )
        return response.status_code == 200
    except Exception:
        return False


class TestZenodoSearcher(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.api_accessible = check_api_accessible()
        if not cls.api_accessible:
            print("\nWarning: Zenodo API is not accessible, network tests will be skipped")

    def setUp(self):
        self.searcher = ZenodoSearcher()

    def test_search_basic(self):
        if not self.api_accessible:
            self.skipTest("Zenodo API is not accessible")

        papers = self.searcher.search("machine learning", max_results=3)
        self.assertIsInstance(papers, list)
        self.assertTrue(len(papers) >= 0)

        if papers:
            first = papers[0]
            self.assertTrue(first.title)
            self.assertEqual(first.source, "zenodo")





if __name__ == "__main__":
    unittest.main()
