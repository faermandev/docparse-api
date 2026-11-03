from datetime import date
from typing import Any
from unittest.mock import MagicMock

import pytest
from langchain_anthropic import ChatAnthropic
from langchain_core.exceptions import OutputParserException
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, SecretStr

from app.core.config import Settings
from app.models import DocumentType
from app.prompts import TRANSCRIPTION_PROMPT, PromptNotFoundError, load_prompt
from app.schemas.extraction import (
    EXTRACTION_SCHEMAS,
    Contract,
    ContractParty,
    ExtractionResult,
    Invoice,
    InvoiceLineItem,
    Project,
    Resume,
    ResumeContact,
    WorkExperience,
)
from app.services.document_reader import DocumentReader, DocumentText
from app.services.extraction import (
    ExtractionError,
    ExtractionService,
    build_extraction_service,
)
from app.services.llm import build_chat_model

SAMPLES: dict[DocumentType, ExtractionResult] = {
    DocumentType.INVOICE: Invoice(
        invoice_number="NF-123",
        issuer_name="ACME Ltda",
        issuer_tax_id="12.345.678/0001-90",
        issue_date=date(2026, 3, 1),
        currency="BRL",
        line_items=[
            InvoiceLineItem(description="Consulting", quantity=2, unit_price=500, total=1000)
        ],
        total_amount=1000,
    ),
    DocumentType.CONTRACT: Contract(
        parties=[ContractParty(name="ACME Ltda", role="contractor", tax_id=None)],
        subject="Software development services",
        start_date=date(2026, 1, 1),
        end_date=None,
        term_description="12 months",
        value=120000,
        currency="BRL",
        key_clauses=[],
    ),
    DocumentType.RESUME: Resume(
        full_name="Ana Silva",
        summary="Backend developer focused on Python.",
        contact=ResumeContact(email="ana@example.com", phone=None, location=None, links=[]),
        experience=[],
        projects=[],
        education=[],
        skills=["Python"],
    ),
}


def _service(llm: MagicMock, reader: MagicMock | None = None) -> ExtractionService:
    return ExtractionService(llm, prompt_version="v1", reader=reader or MagicMock())


def _mock_llm(returns: Any = None, raises: Exception | None = None) -> MagicMock:
    llm = MagicMock(spec=BaseChatModel)
    structured = llm.with_structured_output.return_value
    if raises is not None:
        structured.invoke.side_effect = raises
    else:
        structured.invoke.return_value = returns
    return llm


# Schemas


def test_every_document_type_has_a_schema() -> None:
    assert set(EXTRACTION_SCHEMAS) == set(DocumentType)


def _object_schemas(schema: dict[str, Any]) -> list[dict[str, Any]]:
    return [schema, *schema.get("$defs", {}).values()]


@pytest.mark.parametrize("schema", EXTRACTION_SCHEMAS.values(), ids=lambda s: s.__name__)
def test_schemas_require_every_field(schema: type[BaseModel]) -> None:
    """Strict structured-output modes need all properties listed as required."""
    for obj in _object_schemas(schema.model_json_schema()):
        assert set(obj["required"]) == set(obj["properties"]), obj.get("title")


def _resume(**overrides: Any) -> Resume:
    fields: dict[str, Any] = {
        "full_name": "Ana Silva",
        "summary": None,
        "contact": ResumeContact(email=None, phone=None, location=None, links=[]),
        "experience": [],
        "projects": [],
        "education": [],
        "skills": [],
    }
    return Resume(**(fields | overrides))


PROJECT = Project(
    name="Docs Extraction API",
    description="LLM-based extraction",
    technologies=["Python"],
    url=None,
)
JOB = WorkExperience(
    company="ACME", title="Developer", start_date="2024-01", end_date=None, description=None
)


def test_resume_keeps_projects_when_there_is_no_work_experience() -> None:
    assert _resume(projects=[PROJECT]).projects == [PROJECT]


def test_resume_drops_projects_when_there_is_work_experience() -> None:
    resume = _resume(experience=[JOB], projects=[PROJECT])

    assert resume.experience == [JOB]
    assert resume.projects == []


def test_invoice_parses_iso_date_from_json() -> None:
    invoice = Invoice.model_validate_json(
        '{"invoice_number": null, "issuer_name": null, "issuer_tax_id": null,'
        ' "issue_date": "2026-03-01", "currency": null, "line_items": [], "total_amount": 10.5}'
    )
    assert invoice.issue_date == date(2026, 3, 1)


# Prompts


@pytest.mark.parametrize("version", ["v1", "v2"])
@pytest.mark.parametrize("document_type", list(DocumentType))
def test_prompt_exists_for_every_document_type(document_type: DocumentType, version: str) -> None:
    prompt = load_prompt(document_type, version)
    assert "<document>" in prompt


def test_default_prompt_version_has_every_prompt() -> None:
    version = Settings(_env_file=None).prompt_version
    for name in [*DocumentType, TRANSCRIPTION_PROMPT]:
        assert load_prompt(name, version)


def test_v2_resume_prompt_covers_summary_and_projects() -> None:
    prompt = load_prompt(DocumentType.RESUME, "v2")
    assert "summary:" in prompt
    assert "projects:" in prompt


def test_transcription_prompt_exists() -> None:
    assert load_prompt(TRANSCRIPTION_PROMPT, "v1")


@pytest.mark.parametrize(
    ("name", "version"),
    [("invoice", "v999"), ("invoice", "../v1"), ("invoice", "latest"), ("../v1/invoice", "v1")],
)
def test_load_prompt_rejects_unknown_or_invalid_references(name: str, version: str) -> None:
    with pytest.raises(PromptNotFoundError):
        load_prompt(name, version)


# ExtractionService


@pytest.mark.parametrize("document_type", list(DocumentType))
def test_extract_returns_schema_instance(document_type: DocumentType) -> None:
    expected = SAMPLES[document_type]
    llm = _mock_llm(returns=expected)

    result = _service(llm).extract("some text", document_type)

    assert result is expected
    llm.with_structured_output.assert_called_once_with(
        EXTRACTION_SCHEMAS[document_type], method="json_schema"
    )


def test_extract_sends_versioned_prompt_and_wrapped_document() -> None:
    llm = _mock_llm(returns=SAMPLES[DocumentType.INVOICE])

    _service(llm).extract("TOTAL R$ 10,00", DocumentType.INVOICE)

    (messages,), _ = llm.with_structured_output.return_value.invoke.call_args
    system, human = messages
    assert isinstance(system, SystemMessage)
    assert system.content == load_prompt(DocumentType.INVOICE, "v1")
    assert isinstance(human, HumanMessage)
    assert human.content == "<document>\nTOTAL R$ 10,00\n</document>"


@pytest.mark.parametrize("text", ["", "   \n\t"])
def test_extract_rejects_blank_text_without_calling_llm(text: str) -> None:
    llm = _mock_llm()

    with pytest.raises(ExtractionError, match="no text"):
        _service(llm).extract(text, DocumentType.RESUME)

    llm.with_structured_output.assert_not_called()


def test_extract_wraps_parsing_errors() -> None:
    llm = _mock_llm(raises=OutputParserException("bad json"))

    with pytest.raises(ExtractionError, match="Invoice schema") as exc_info:
        _service(llm).extract("text", DocumentType.INVOICE)

    assert isinstance(exc_info.value.__cause__, OutputParserException)


def test_extract_rejects_unexpected_output_type() -> None:
    llm = _mock_llm(returns={"not": "a model"})

    with pytest.raises(ExtractionError, match="Expected Contract"):
        _service(llm).extract("text", DocumentType.CONTRACT)


def test_extract_lets_provider_errors_propagate() -> None:
    """Transient provider errors must reach the caller so the worker can retry them."""
    llm = _mock_llm(raises=TimeoutError("provider timeout"))

    with pytest.raises(TimeoutError):
        _service(llm).extract("text", DocumentType.INVOICE)


def test_extract_document_reads_file_then_extracts_text() -> None:
    expected = SAMPLES[DocumentType.RESUME]
    llm = _mock_llm(returns=expected)
    reader = MagicMock(spec=DocumentReader)
    reader.read.return_value = DocumentText(text="Ana Silva", source="pdf_text", page_count=1)

    result = _service(llm, reader).extract_document(b"%PDF", "cv.pdf", DocumentType.RESUME)

    assert result is expected
    reader.read.assert_called_once_with(b"%PDF", "cv.pdf", None)
    (messages,), _ = llm.with_structured_output.return_value.invoke.call_args
    assert messages[1].content == "<document>\nAna Silva\n</document>"


def test_extract_document_reports_extracting_after_reading() -> None:
    llm = _mock_llm(returns=SAMPLES[DocumentType.INVOICE])
    reader = MagicMock(spec=DocumentReader)
    stages: list[str] = []
    reader.read.side_effect = lambda *_: (
        stages.append("reading") or DocumentText(text="x", source="pdf_text", page_count=1)
    )

    _service(llm, reader).extract_document(b"%PDF", "a.pdf", DocumentType.INVOICE, stages.append)

    assert stages == ["reading", "extracting"]


# LLM factory


@pytest.mark.parametrize(
    ("provider", "model", "expected_cls"),
    [("anthropic", "claude-opus-5-5", ChatAnthropic), ("openai", "gpt-test", ChatOpenAI)],
)
def test_build_chat_model_uses_configured_provider(
    provider: str, model: str, expected_cls: type[BaseChatModel]
) -> None:
    settings = Settings(
        _env_file=None,
        llm_provider=provider,  # type: ignore[arg-type]
        llm_model=model,
        llm_api_key=SecretStr("test-key"),
    )

    chat_model = build_chat_model(settings)

    assert isinstance(chat_model, expected_cls)


def test_build_extraction_service_wires_configured_model() -> None:
    settings = Settings(_env_file=None, llm_api_key=SecretStr("test-key"))

    assert isinstance(build_extraction_service(settings), ExtractionService)
