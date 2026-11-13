"""Fixtures for the executor tests: async tests run on asyncio, real child processes."""

import pytest


@pytest.fixture
def anyio_backend() -> str:
    """Run ``anyio``-marked tests on asyncio, the loop the executor's subprocess helpers use."""
    return "asyncio"
