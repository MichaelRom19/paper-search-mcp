"""Require opt-in even when a live test file is selected directly."""

import pytest


def pytest_collect_file(file_path, parent):
    if not parent.config.getoption("--live"):
        raise pytest.UsageError("Live suites require --live; ordinary tests run without provider access.")
