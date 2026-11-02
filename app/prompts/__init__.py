"""Versioned system prompts, stored as Markdown files under `app/prompts/<version>/`.

To change a prompt without affecting running behavior, copy the folder to a new version
(e.g. `v2/`), edit it there, and switch `PROMPT_VERSION`.
"""

import re
from functools import lru_cache
from pathlib import Path

from app.models import DocumentType

PROMPTS_DIR = Path(__file__).parent
_VERSION_PATTERN = re.compile(r"^v\d+$")


class PromptNotFoundError(LookupError):
    pass


@lru_cache
def load_prompt(document_type: DocumentType, version: str) -> str:
    """Return the system prompt for a document type at a given prompt version."""
    if not _VERSION_PATTERN.match(version):
        raise PromptNotFoundError(f"Invalid prompt version {version!r}.")
    path = PROMPTS_DIR / version / f"{document_type.value}.md"
    if not path.is_file():
        raise PromptNotFoundError(f"No {version} prompt for document type {document_type}.")
    return path.read_text(encoding="utf-8").strip()
