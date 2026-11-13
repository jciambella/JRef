"""Pure-Python core of bibmanager (no Qt imports allowed in this package)."""

from .bibtex_manager import BibtexManager, escape_latex, protect_title, render_entry, slugify
from .config import AppConfig
from .database import Database
from .extractor import MetadataResolver, PdfContent, Resolution, read_pdf
from .inbox import InboxWatcher
from .library import (
    DuplicateDoiError,
    ForeignBibFileError,
    ImportCandidate,
    IngestResult,
    IngestStatus,
    Library,
    LibraryError,
    ReferenceCandidate,
)
from .models import Author, Collection, Paper, PaperMetadata, SearchHit
from .storage import PdfStorage, base_citation_key, normalize_key_component, sha256_file

__all__ = [
    "AppConfig",
    "Author",
    "BibtexManager",
    "Collection",
    "Database",
    "DuplicateDoiError",
    "ForeignBibFileError",
    "ImportCandidate",
    "InboxWatcher",
    "IngestResult",
    "IngestStatus",
    "Library",
    "LibraryError",
    "MetadataResolver",
    "Paper",
    "PaperMetadata",
    "ReferenceCandidate",
    "PdfContent",
    "PdfStorage",
    "Resolution",
    "SearchHit",
    "base_citation_key",
    "escape_latex",
    "normalize_key_component",
    "protect_title",
    "read_pdf",
    "render_entry",
    "sha256_file",
    "slugify",
]
