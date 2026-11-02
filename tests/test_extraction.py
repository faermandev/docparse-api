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
from app.prompts import PromptNotFoundError, load_prompt
from app.schemas.extraction import (
    EXTRACTION_SCHEMAS,
    Contract,
    ContractParty,
    ExtractionResult,
    Invoice,
    InvoiceLineItem,
    Resume,
    ResumeContact,
)
from app.services.extraction import ExtractionError, ExtractionService
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
        contact=ResumeContact(email="ana@example.com", phone=None, location=None, links=[]),
        experience=[],
        education=[],
        skills=["Python"],
    ),
}


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


def test_invoice_parses_iso_date_from_json() -> None:
    invoice = Invoice.model_validate_json(
        '{"invoice_number": null, "issuer_name": null, "issuer_tax_id": null,'
        ' "issue_date": "2026-03-01", "currency": null, "line_items": [], "total_amount": 10.5}'
    )
    assert invoice.issue_date == date(2026, 3, 1)


# Prompts


@pytest.mark.parametrize("document_type", list(DocumentType))
def test_v1_prompt_exists_for_every_document_type(document_type: DocumentType) -> None:
    prompt = load_prompt(document_type, "v1")
    assert "<document>" in prompt


@pytest.mark.parametrize("version", ["v999", "../v1", "latest"])
def test_load_prompt_rejects_unknown_or_invalid_versions(version: str) -> None:
    with pytest.raises(PromptNotFoundError):
        load_prompt(DocumentType.INVOICE, version)


# ExtractionService


@pytest.mark.parametrize("document_type", list(DocumentType))
def test_extract_returns_schema_instance(document_type: DocumentType) -> None:
    expected = SAMPLES[document_type]
    llm = _mock_llm(returns=expected)

    result = ExtractionService(llm, prompt_version="v1").extract("some text", document_type)

    assert result is expected
    llm.with_structured_output.assert_called_once_with(
        EXTRACTION_SCHEMAS[document_type], method="json_schema"
    )


def test_extract_sends_versioned_prompt_and_wrapped_document() -> None:
    llm = _mock_llm(returns=SAMPLES[DocumentType.INVOICE])

    ExtractionService(llm, prompt_version="v1").extract("TOTAL R$ 10,00", DocumentType.INVOICE)

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
        ExtractionService(llm, prompt_version="v1").extract(text, DocumentType.RESUME)

    llm.with_structured_output.assert_not_called()


def test_extract_wraps_parsing_errors() -> None:
    llm = _mock_llm(raises=OutputParserException("bad json"))

    with pytest.raises(ExtractionError, match="Invoice schema") as exc_info:
        ExtractionService(llm, prompt_version="v1").extract("text", DocumentType.INVOICE)

    assert isinstance(exc_info.value.__cause__, OutputParserException)


def test_extract_rejects_unexpected_output_type() -> None:
    llm = _mock_llm(returns={"not": "a model"})

    with pytest.raises(ExtractionError, match="Expected Contract"):
        ExtractionService(llm, prompt_version="v1").extract("text", DocumentType.CONTRACT)


def test_extract_lets_provider_errors_propagate() -> None:
    """Transient provider errors must reach the caller so the worker can retry them."""
    llm = _mock_llm(raises=TimeoutError("provider timeout"))

    with pytest.raises(TimeoutError):
        ExtractionService(llm, prompt_version="v1").extract("text", DocumentType.INVOICE)


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
