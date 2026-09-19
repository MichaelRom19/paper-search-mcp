"""ACM Digital Library stub. All operations are unimplemented, with or without a key.

The environment key records configuration only. Legacy MCP tools may be
registered when it is present; this source is never aggregate discovery.
"""

from __future__ import annotations

import logging
from typing import List

from .base import PaperSource
from ..paper import Paper
from ..config import get_env

logger = logging.getLogger(__name__)

_NOT_CONFIGURED_MSG = (
    "ACM Digital Library is an unimplemented stub. PAPER_SEARCH_MCP_ACM_API_KEY "
    "(or ACM_API_KEY) records configuration only; it does not enable operations. "
    "Use browser search until this adapter is implemented."
)


class ACMSearcher(PaperSource):
    """Unimplemented ACM Digital Library connector; key presence only indicates configuration."""

    # ACM DL base URL (placeholder — real endpoint TBD once API key is available)
    BASE_URL = "https://dl.acm.org/action/doSearch"

    def __init__(self) -> None:
        self.api_key: str = get_env("ACM_API_KEY", "").strip()
        if not self.api_key:
            logger.warning(
                "ACMSearcher initialised without PAPER_SEARCH_MCP_ACM_API_KEY/ACM_API_KEY.  "
                "All calls raise NotImplementedError, even when a key is set."
            )

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    def is_configured(self) -> bool:
        """Return True only when a non-empty ACM API key is available."""
        return bool(self.api_key)

    # ------------------------------------------------------------------
    # PaperSource interface
    # ------------------------------------------------------------------

    def search(self, query: str, max_results: int = 10, **kwargs) -> List[Paper]:  # type: ignore[override]
        """Search ACM Digital Library — requires PAPER_SEARCH_MCP_ACM_API_KEY or ACM_API_KEY.

        Raises:
            NotImplementedError: Always, when ACM API key env is not set.
        """
        if not self.is_configured():
            raise NotImplementedError(_NOT_CONFIGURED_MSG)

        # TODO: implement real ACM DL API call here once key is available
        raise NotImplementedError(
            "ACM DL search is not yet implemented.  "
            "Contribute at https://github.com/openags/paper-search-mcp."
        )

    def download_pdf(self, paper_id: str, save_path: str = "./downloads") -> str:
        """Download a PDF from ACM DL — requires ACM API key env and institutional access.

        Raises:
            NotImplementedError: Always, until key + download logic are implemented.
        """
        if not self.is_configured():
            raise NotImplementedError(_NOT_CONFIGURED_MSG)

        raise NotImplementedError(
            "ACM DL PDF download is not yet implemented.  "
            "Note: full-text access also requires ACM membership or institutional access."
        )

    def read_paper(self, paper_id: str, save_path: str = "./downloads") -> str:
        """Read paper content from ACM DL — requires ACM API key env.

        Raises:
            NotImplementedError: Always, until download + extraction are implemented.
        """
        if not self.is_configured():
            raise NotImplementedError(_NOT_CONFIGURED_MSG)

        raise NotImplementedError(
            "ACM DL paper reading is not yet implemented."
        )
