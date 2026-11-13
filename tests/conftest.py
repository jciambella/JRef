"""Shared fixtures: isolated library roots, synthetic PDFs, and a network-free resolver."""

from __future__ import annotations

import itertools
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path

import httpx
import pymupdf
import pytest

from bibmanager.core import AppConfig, Library, MetadataResolver

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = REPO_ROOT / "config.json"

_nonce = itertools.count()


def _no_network(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"Unexpected network access during tests: {request.url}")


@pytest.fixture(scope="session")
def protected_words() -> tuple[str, ...]:
    return AppConfig.load(CONFIG_PATH).protected_words


@pytest.fixture
def config(tmp_path: Path) -> AppConfig:
    """The repository config.json, redirected to a throw-away library root."""
    return AppConfig.load(CONFIG_PATH, library_root=tmp_path / "library", import_mode="move")


@pytest.fixture
def library(config: AppConfig) -> Iterator[Library]:
    client = httpx.Client(transport=httpx.MockTransport(_no_network))
    with Library(config, resolver=MetadataResolver.from_config(config, client=client)) as lib:
        yield lib
    client.close()


@pytest.fixture
def make_pdf(tmp_path: Path) -> Callable[..., Path]:
    """Create a real (PyMuPDF-generated) PDF in a fake 'Downloads' folder.

    Each PDF carries a unique nonce line, so distinct calls never share a SHA-256.
    """
    downloads = tmp_path / "downloads"
    downloads.mkdir()

    def _make(
        name: str = "paper.pdf",
        *,
        title: str = "A synthetic test document",
        lines: Sequence[str] = (),
        page2_lines: Sequence[str] = (),
    ) -> Path:
        doc = pymupdf.open()
        page = doc.new_page()
        page.insert_textbox(pymupdf.Rect(72, 60, 540, 150), title, fontsize=18)
        y = 180.0
        for line in (*lines, f"nonce {next(_nonce)}"):
            page.insert_text((72, y), line, fontsize=10)
            y += 14
        if page2_lines:
            page2 = doc.new_page()
            y = 72.0
            for line in page2_lines:
                page2.insert_text((72, y), line, fontsize=9)
                y += 12
        path = downloads / name
        doc.save(path)
        doc.close()
        return path

    return _make
