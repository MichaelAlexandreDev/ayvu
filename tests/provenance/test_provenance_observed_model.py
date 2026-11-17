"""An observed model identity, ``<name>@<sha256 hex>``, as Ollama reports it (#376)."""
from __future__ import annotations

from dataclasses import replace

import pytest

from ayvu.provenance import ProducerIdentity, ProvenanceValidationError, observed_model, valid_observed_model_name
from ayvu.providers.libretranslate.adapter import LIBRETRANSLATE_PRODUCER

DIGEST = "0123456789abcdef" * 4


@pytest.mark.parametrize("name", ["qwen2.5:7b", "llama3.2", "hf.co/bartowski/Llama-3.2-1B-Instruct-GGUF:Q4_K_M",
                                  "a" * 128])
def test_a_reported_name_and_digest_make_a_producer_model(name):
    model = observed_model(name, DIGEST)
    assert model == f"{name}@{DIGEST}"
    producer = ProducerIdentity("ollama", model, "ollama_chat@1", "llm_core@1")
    assert producer.model == model and producer.fully_identified


@pytest.mark.parametrize("name", ["", "a" * 129, ":7b", "two words", "a@b", "declared:x", "a\nb", "árvore", None, 3])
def test_an_invalid_name_is_a_bounded_error(name):
    assert not valid_observed_model_name(name)
    with pytest.raises(ProvenanceValidationError, match="^invalid_model_name$"):
        observed_model(name, DIGEST)


@pytest.mark.parametrize("digest", ["", "abc", DIGEST.upper(), "sha256:" + DIGEST, DIGEST + "0", None])
def test_only_a_bare_lowercase_sha256_is_a_digest(digest):
    with pytest.raises(ProvenanceValidationError, match="^invalid_model_digest$"):
        observed_model("qwen2.5:7b", digest)


def test_an_observed_model_needs_its_digest_and_libretranslate_is_unchanged():
    with pytest.raises(ProvenanceValidationError):
        ProducerIdentity("ollama", "qwen2.5:7b", "ollama_chat@1", "llm_core@1")
    with pytest.raises(ProvenanceValidationError):
        replace(LIBRETRANSLATE_PRODUCER, model=f"declared:qwen2.5:7b@{DIGEST}")
    assert (LIBRETRANSLATE_PRODUCER.provider, LIBRETRANSLATE_PRODUCER.model, LIBRETRANSLATE_PRODUCER.adapter,
            LIBRETRANSLATE_PRODUCER.pipeline) == ("libretranslate", "unknown", "libretranslate_compat@1", "not_applicable")
