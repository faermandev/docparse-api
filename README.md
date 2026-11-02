# Docs Extraction API

Upload a document (PDF or image of a contract, invoice, or resume) and get back validated JSON with the extracted fields.

> Work in progress. Full documentation will be added in a later step.

## Development

```bash
uv sync
uv run uvicorn app.main:app --reload
uv run pytest
uv run ruff check . && uv run ruff format --check .
```
