from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path


GLOSSARY_RULE_PRESERVE = "preserve"
GLOSSARY_RULE_TRANSLATE = "translate"
GLOSSARY_RULE_FORBIDDEN = "forbidden"
GLOSSARY_RULES = {GLOSSARY_RULE_PRESERVE, GLOSSARY_RULE_TRANSLATE, GLOSSARY_RULE_FORBIDDEN}


@dataclass(frozen=True, slots=True)
class GlossaryEntry:
    term: str
    rule: str
    translation: str | None = None
    required: bool = False

    @property
    def replacement(self) -> str | None:
        if self.rule == GLOSSARY_RULE_TRANSLATE:
            return self.translation
        if self.rule == GLOSSARY_RULE_PRESERVE:
            return self.term
        return None

    @property
    def required_output(self) -> str | None:
        if not self.required:
            return None
        return self.replacement


@dataclass
class GlossaryUsage:
    applied_terms: dict[str, int] = field(default_factory=dict)
    required_terms_present: dict[str, int] = field(default_factory=dict)
    required_terms_missing: list[str] = field(default_factory=list)
    forbidden_terms_found: dict[str, int] = field(default_factory=dict)

    @property
    def total_applied(self) -> int:
        return sum(self.applied_terms.values())

    @property
    def total_forbidden_found(self) -> int:
        return sum(self.forbidden_terms_found.values())

    @property
    def has_activity(self) -> bool:
        return bool(
            self.applied_terms
            or self.required_terms_present
            or self.required_terms_missing
            or self.forbidden_terms_found
        )

    def record_applied(self, term: str, count: int) -> None:
        _add_count(self.applied_terms, term, count)

    def record_required_present(self, term: str, count: int) -> None:
        _add_count(self.required_terms_present, term, count)

    def record_forbidden_found(self, term: str, count: int) -> None:
        _add_count(self.forbidden_terms_found, term, count)

    def merge(self, other: "GlossaryUsage") -> None:
        _merge_counts(self.applied_terms, other.applied_terms)
        _merge_counts(self.required_terms_present, other.required_terms_present)
        _merge_counts(self.forbidden_terms_found, other.forbidden_terms_found)
        for term in other.required_terms_missing:
            if term not in self.required_terms_missing:
                self.required_terms_missing.append(term)

    def finalize_required_terms(self, glossary: "Glossary") -> None:
        self.required_terms_missing = [
            entry.term
            for entry in glossary.entries
            if entry.required_output is not None
            and self.required_terms_present.get(entry.term, 0) == 0
        ]


@dataclass(frozen=True)
class GlossaryApplication:
    text: str
    usage: GlossaryUsage = field(default_factory=GlossaryUsage)


@dataclass(frozen=True, init=False)
class Glossary:
    entries: tuple[GlossaryEntry, ...]

    def __init__(
        self,
        entries: Mapping[str, str] | Iterable[GlossaryEntry] | None = None,
    ) -> None:
        if entries is None:
            parsed_entries: tuple[GlossaryEntry, ...] = ()
        elif isinstance(entries, Mapping):
            parsed_entries = tuple(
                GlossaryEntry(
                    term=str(term),
                    rule=GLOSSARY_RULE_TRANSLATE,
                    translation=str(translation),
                )
                for term, translation in entries.items()
            )
        else:
            parsed_entries = tuple(entries)
        object.__setattr__(self, "entries", parsed_entries)

    @property
    def terms(self) -> dict[str, str]:
        return {
            entry.term: replacement
            for entry in self.entries
            if (replacement := entry.replacement) is not None
        }

    def apply(self, text: str) -> str:
        return self.apply_with_usage(text).text

    def apply_with_usage(self, text: str) -> GlossaryApplication:
        usage = GlossaryUsage()
        replace_entries = [entry for entry in self._ordered_entries() if entry.replacement is not None]
        if not replace_entries:
            usage.merge(self.scan_output(text))
            return GlossaryApplication(text=text, usage=usage)

        result = text
        for entry in replace_entries:
            if not entry.term or entry.replacement is None:
                continue
            pattern = _term_pattern(entry.term)
            result, count = pattern.subn(
                lambda match: _match_case(match.group(0), entry.replacement),
                result,
            )
            usage.record_applied(entry.term, count)
        usage.merge(self.scan_output(result))
        return GlossaryApplication(text=result, usage=usage)

    def forbidden_terms_in(self, text: str) -> list[str]:
        return [
            entry.term
            for entry in self._ordered_entries()
            if entry.rule == GLOSSARY_RULE_FORBIDDEN and _term_pattern(entry.term).search(text)
        ]

    def scan_output(self, text: str) -> GlossaryUsage:
        usage = GlossaryUsage()
        for entry in self._ordered_entries():
            required_output = entry.required_output
            if required_output is not None:
                usage.record_required_present(entry.term, _count_term(required_output, text))
            if entry.rule == GLOSSARY_RULE_FORBIDDEN:
                usage.record_forbidden_found(entry.term, _count_term(entry.term, text))
        return usage

    def _ordered_entries(self) -> list[GlossaryEntry]:
        return sorted(self.entries, key=lambda entry: len(entry.term), reverse=True)

    def __bool__(self) -> bool:
        return bool(self.entries)


class GlossaryError(ValueError):
    pass


def load_glossary(path: str | Path | None) -> Glossary:
    if not path:
        return Glossary()
    glossary_path = Path(path)
    if not glossary_path.exists():
        raise GlossaryError(f"Glossary file not found: {glossary_path}")
    if not glossary_path.is_file():
        raise GlossaryError(f"Glossary path is not a file: {glossary_path}")

    try:
        with glossary_path.open("r", encoding="utf-8") as file:
            data = json.load(file)
    except json.JSONDecodeError as exc:
        raise GlossaryError(f"Glossary file is not valid JSON: {glossary_path}") from exc

    if not isinstance(data, dict):
        raise GlossaryError("Glossary JSON must be an object mapping terms to translations")
    return Glossary(_parse_glossary_entries(data))


def save_glossary(path: str | Path, glossary: Glossary) -> Path:
    glossary_path = Path(path)
    data = glossary_to_dict(glossary)
    try:
        glossary_path.parent.mkdir(parents=True, exist_ok=True)
        glossary_path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        raise GlossaryError(f"Could not write glossary file: {glossary_path}") from exc
    return glossary_path


def glossary_to_dict(glossary: Glossary) -> dict[str, object]:
    data: dict[str, object] = {}
    for entry in glossary.entries:
        data[entry.term] = _glossary_entry_to_value(entry)
    _parse_glossary_entries(data)
    return data


def apply_glossary(text: str, glossary: Glossary | Mapping[object, object] | None) -> str:
    return apply_glossary_with_usage(text, glossary).text


def apply_glossary_with_usage(
    text: str,
    glossary: Glossary | Mapping[object, object] | None,
) -> GlossaryApplication:
    if not glossary:
        return GlossaryApplication(text=text)
    if isinstance(glossary, Glossary):
        return glossary.apply_with_usage(text)
    return Glossary(_parse_glossary_entries(glossary)).apply_with_usage(text)


def _parse_glossary_entries(data: Mapping[object, object]) -> tuple[GlossaryEntry, ...]:
    return tuple(_parse_glossary_entry(str(term), value) for term, value in data.items())


def _glossary_entry_to_value(entry: GlossaryEntry) -> object:
    if entry.rule == GLOSSARY_RULE_TRANSLATE:
        if entry.translation is None:
            raise GlossaryError(
                f"Glossary entry for '{entry.term}' with rule 'translate' must include a string translation"
            )
        value: dict[str, object] = {
            "rule": GLOSSARY_RULE_TRANSLATE,
            "translation": entry.translation,
        }
        if entry.required:
            value["required"] = True
        return value
    if entry.rule == GLOSSARY_RULE_PRESERVE:
        value = {"rule": GLOSSARY_RULE_PRESERVE}
        if entry.required:
            value["required"] = True
        return value
    if entry.rule == GLOSSARY_RULE_FORBIDDEN:
        return {"rule": GLOSSARY_RULE_FORBIDDEN}
    allowed_rules = ", ".join(sorted(GLOSSARY_RULES))
    raise GlossaryError(f"Glossary entry for '{entry.term}' uses unsupported rule '{entry.rule}': {allowed_rules}")


def _parse_glossary_entry(term: str, value: object) -> GlossaryEntry:
    _validate_term(term)
    if isinstance(value, str):
        return GlossaryEntry(term=term, rule=GLOSSARY_RULE_TRANSLATE, translation=value)
    if not isinstance(value, dict):
        raise GlossaryError(
            f"Glossary entry for '{term}' must be a string translation or an object with a rule"
        )

    rule = value.get("rule")
    if not isinstance(rule, str):
        raise GlossaryError(f"Glossary entry for '{term}' must include a string rule")
    if rule not in GLOSSARY_RULES:
        allowed_rules = ", ".join(sorted(GLOSSARY_RULES))
        raise GlossaryError(f"Glossary entry for '{term}' uses unsupported rule '{rule}': {allowed_rules}")

    allowed_fields = _allowed_fields_for_rule(rule)
    unknown_fields = sorted(set(value) - allowed_fields)
    if unknown_fields:
        fields = ", ".join(unknown_fields)
        raise GlossaryError(f"Glossary entry for '{term}' has unsupported fields: {fields}")

    required = _parse_required(term, value.get("required", False))
    if rule == GLOSSARY_RULE_TRANSLATE:
        translation = value.get("translation")
        if not isinstance(translation, str):
            raise GlossaryError(
                f"Glossary entry for '{term}' with rule 'translate' must include a string translation"
            )
        return GlossaryEntry(term=term, rule=rule, translation=translation, required=required)

    return GlossaryEntry(term=term, rule=rule, required=required)


def _validate_term(term: str) -> None:
    if not term:
        raise GlossaryError("Glossary terms must not be empty")
    if term != term.strip():
        raise GlossaryError(f"Glossary term '{term}' must not start or end with whitespace")


def _allowed_fields_for_rule(rule: str) -> set[str]:
    if rule == GLOSSARY_RULE_TRANSLATE:
        return {"rule", "translation", "required"}
    if rule == GLOSSARY_RULE_PRESERVE:
        return {"rule", "required"}
    return {"rule"}


def _parse_required(term: str, value: object) -> bool:
    if not isinstance(value, bool):
        raise GlossaryError(f"Glossary entry for '{term}' field 'required' must be a boolean")
    return value


def _add_count(counts: dict[str, int], term: str, count: int) -> None:
    if count > 0:
        counts[term] = counts.get(term, 0) + count


def _merge_counts(target: dict[str, int], source: Mapping[str, int]) -> None:
    for term, count in source.items():
        _add_count(target, term, count)


def _count_term(term: str, text: str) -> int:
    return len(_term_pattern(term).findall(text))


def _term_pattern(term: str) -> re.Pattern[str]:
    return re.compile(rf"(?<!\w){re.escape(term)}(?!\w)", re.IGNORECASE)


def _match_case(original: str, replacement: str) -> str:
    if original.isupper():
        return replacement.upper()
    if original[:1].isupper() and original[1:].islower():
        return replacement[:1].upper() + replacement[1:]
    return replacement


def effective_glossary_reference(glossary: Glossary | Mapping[object, object] | None) -> str:
    """Immutable reference to the effective ordered rules, including explicit absence."""
    import hashlib
    from dataclasses import asdict
    entries = (() if not glossary else glossary.entries if isinstance(glossary, Glossary)
               else _parse_glossary_entries(glossary))
    payload = json.dumps([asdict(entry) for entry in entries], sort_keys=True,
                         ensure_ascii=True, separators=(",", ":"))
    return 'glossary:' + hashlib.sha256(payload.encode('ascii')).hexdigest()
