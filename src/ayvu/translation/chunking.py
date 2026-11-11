from __future__ import annotations

import re


DEFAULT_CHUNK_LIMIT = 3000


def split_text(text: str, limit: int = DEFAULT_CHUNK_LIMIT) -> list[str]:
    """Split text into ordered chunks without cutting words when possible."""
    if limit <= 0:
        raise ValueError("limit must be greater than zero")
    if len(text) <= limit:
        return [text] if text else []

    chunks: list[str] = []
    for paragraph in _split_paragraphs(text):
        if len(paragraph) <= limit:
            _append_chunk(chunks, paragraph, limit)
            continue
        for sentence in _split_sentences(paragraph):
            if len(sentence) <= limit:
                _append_chunk(chunks, sentence, limit)
                continue
            chunks.extend(_split_words(sentence, limit))
    return [chunk for chunk in chunks if chunk]


def _split_paragraphs(text: str) -> list[str]:
    parts = re.split(r"(\n\s*\n)", text)
    paragraphs: list[str] = []
    current = ""
    for part in parts:
        if not part:
            continue
        current += part
        if re.fullmatch(r"\n\s*\n", part):
            paragraphs.append(current)
            current = ""
    if current:
        paragraphs.append(current)
    return paragraphs


def _split_sentences(text: str) -> list[str]:
    pieces = re.split(r"(?<=[.!?])(\s+)", text)
    sentences: list[str] = []
    current = ""
    for piece in pieces:
        if not piece:
            continue
        current += piece
        if re.search(r"[.!?]\s*$", current):
            sentences.append(current)
            current = ""
    if current:
        sentences.append(current)
    return sentences


def _split_words(text: str, limit: int) -> list[str]:
    chunks: list[str] = []
    current = ""
    for match in re.finditer(r"\S+\s*", text):
        token = match.group(0)
        if len(token) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.extend(token[i : i + limit] for i in range(0, len(token), limit))
            continue
        if current and len(current) + len(token) > limit:
            chunks.append(current.rstrip())
            current = token
            continue
        current += token
    if current:
        chunks.append(current.rstrip())
    return chunks


def _append_chunk(chunks: list[str], text: str, limit: int) -> None:
    if not text:
        return
    if chunks and len(chunks[-1]) + len(text) <= limit:
        chunks[-1] += text
        return
    chunks.append(text)
