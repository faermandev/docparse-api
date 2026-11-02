"""Versioned system prompts, stored as Markdown files under `app/prompts/<version>/<name>.md`.

To change a prompt without affecting running behavior, copy the folder to a new version
(e.g. `v2/`), edit it there, and switch `PROMPT_VERSION`.
"""

import re
from functools import lru_cache
from pathlib import Path

PROMPTS_DIR = Path(__file__).parent
TRANSCRIPTION_PROMPT = "transcription"

_VERSION_PATTERN = re.compile(r"^v\d+$")
_NAME_PATTERN = re.compile(r"^[a-z_]+$")


class PromptNotFoundError(LookupError):
    pass


@lru_cache
def load_prompt(name: str, version: str) -> str:
    """Return a prompt by name (a DocumentType value or TRANSCRIPTION_PROMPT) and version."""
    if not _VERSION_PATTERN.match(version) or not _NAME_PATTERN.match(name):
        raise PromptNotFoundError(f"Invalid prompt reference {name!r} @ {version!r}.")
    path = PROMPTS_DIR / version / f"{name}.md"
    if not path.is_file():
        raise PromptNotFoundError(f"No {version} prompt named {name!r}.")
    return path.read_text(encoding="utf-8").strip()
