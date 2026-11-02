"""Temporary helper: run the extraction pipeline synchronously on a local file.

Calls the real LLM configured in .env (LLM_PROVIDER, LLM_MODEL, LLM_API_KEY).
Will be superseded by the asynchronous worker.

Usage:
    uv run python -m scripts.extract_file path/to/invoice.pdf --type invoice
"""

import argparse
import sys
import time
from pathlib import Path

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.models import DocumentType
from app.services.document_reader import DocumentReadError
from app.services.extraction import ExtractionError, build_extraction_service


def main() -> int:
    parser = argparse.ArgumentParser(description="Extract structured data from a document.")
    parser.add_argument("path", type=Path, help="PDF or image file")
    parser.add_argument("--type", required=True, choices=[t.value for t in DocumentType])
    args = parser.parse_args()

    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    service = build_extraction_service(settings)

    started = time.perf_counter()
    try:
        result = service.extract_document(
            args.path.read_bytes(), args.path.name, DocumentType(args.type)
        )
    except (DocumentReadError, ExtractionError) as exc:
        print(f"Extraction failed: {exc}", file=sys.stderr)
        return 1

    print(result.model_dump_json(indent=2))
    print(f"Done in {time.perf_counter() - started:.1f}s", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
