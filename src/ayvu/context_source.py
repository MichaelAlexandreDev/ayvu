"""Adapter da única porta de candidatos, sobre revisões já resolvidas pelos donos.

Não abre arquivos, não descobre candidatos e não decide precedência. O envelope
armazena tamanho e identidade antes de qualquer carregamento do conteúdo.
"""
from dataclasses import dataclass
from typing import Callable

from .content import ContentDocument
from .glossary_catalog import Snapshot, _valid_entries
from .projects.profiles import ResolvedProfileSnapshot, GlossaryStage
from .provider import Requirement, UnitReference
from .translation_context import (
    ContextCandidate, ContextEntryRevision, ContextError, ContextKind, ContextPolicyVersion, ContextSnapshot,
    ContextSourceRequest, ContextSourceResult, ContextSourceOversize, content_identity, utf8_size, _hash,
)


@dataclass(frozen=True, repr=False)
class StoredContextRevision:
    source: ContextEntryRevision
    utf8_bytes: int
    load: Callable[[int], str]


class SnapshotContextCandidateSource:
    """Read adapter de snapshots; metadados são checados antes de chamar load.

    Um dono com armazenamento lazy deve honrar o limite de load. A composição
    atual usa strings imutáveis que #140/#157 já possuem; não cria outro banco.
    """
    def __init__(self, stored: tuple[StoredContextRevision, ...]):
        if (type(stored) is not tuple or len(stored) > ContextPolicyVersion.V1.manifest_entries
                or any(type(s) is not StoredContextRevision or type(s.utf8_bytes) is not int or s.utf8_bytes < 0 for s in stored)
                or len({(s.source.project_id, s.source.reference) for s in stored}) != len(stored)):
            raise ContextError('context_integrity')
        self._stored = stored

    def __repr__(self):
        return 'SnapshotContextCandidateSource()'

    def read(self, request: ContextSourceRequest) -> ContextSourceResult:
        if type(request) is not ContextSourceRequest:
            raise ContextError('invalid_context')
        request.__post_init__()
        expected = {e.reference: e for e in request.snapshot.entries}
        project = request.snapshot.profile.project.value
        stored = tuple(s for s in self._stored if s.source.project_id == project and s.source.reference in expected)
        if any(s.source != expected[s.source.reference] or s.source.kind not in request.source_kinds for s in stored):
            raise ContextError('context_integrity')
        limits = request.limits
        if (len(stored) > limits.entries or any(s.utf8_bytes > limits.entry_bytes for s in stored)
                or sum(s.utf8_bytes for s in stored) > limits.total_bytes):
            return ContextSourceResult(oversize=ContextSourceOversize())
        candidates = []
        for s in sorted(stored, key=lambda s: s.source.reference):
            text = s.load(limits.entry_bytes)
            if utf8_size(text) != s.utf8_bytes or content_identity(project, text) != s.source.content_identity:
                raise ContextError('context_integrity')
            candidates.append(ContextCandidate(s.source, text))
        return ContextSourceResult(tuple(candidates))


def snapshot_context_sources(profile: ResolvedProfileSnapshot, document: ContentDocument, unit_id: str, *,
                             neighbors: tuple[str, ...] = (), include_current: bool = False,
                             include_profile: bool = False, terminology: Snapshot | None = None,
                             requirement: Requirement = Requirement.ADVISORY) -> tuple[ContextSnapshot, SnapshotContextCandidateSource]:
    """Composição explícita: nenhum vizinho ou termo é adicionado para ocupar espaço.

    Vizinhos precisam de uma relação estrutural #140 e de seleção explícita do
    chamador. Termos gerenciados precisam do binding exato #163/#157, estágio
    provider e ocorrência literal no texto atual. Style continua inerte.
    """
    if (type(profile) is not ResolvedProfileSnapshot or type(document) is not ContentDocument
            or profile.project.value != document.project_id or type(neighbors) is not tuple
            or len(neighbors) > 8 or len(set(neighbors)) != len(neighbors)
            or type(include_current) is not bool or type(include_profile) is not bool
            or type(requirement) is not Requirement):
        raise ContextError('invalid_context')
    units = {u.unit_id: u for u in document.units}
    current = units.get(unit_id)
    if current is None or any(n not in {link.target_unit_id for link in current.context_links} for n in neighbors):
        raise ContextError('invalid_context')
    entries, stored = [], []

    def add(kind, reference, revision, text, position=(), *, required=False):
        if len(entries) >= ContextPolicyVersion.V1.manifest_entries:
            raise ContextError('context_limit')
        identity = content_identity(document.project_id, text)
        source = ContextEntryRevision(document.project_id, reference, revision, identity, kind,
            Requirement.REQUIRED if required else requirement, position)
        entries.append(source)
        stored.append(StoredContextRevision(source, utf8_size(text), lambda limit, text=text: text))

    if include_profile:
        add(ContextKind.PROFILE, 'profile_context:' + _hash(profile.profile.value),
            _hash(('profile_target_v1', profile.target_language)), profile.target_language)
    if include_current:
        add(ContextKind.CURRENT_UNIT, 'unit_context:' + _hash(unit_id), _hash((document.revision_id, current.source_digest)),
            current.source_text, current.order_key)
    for neighbor in neighbors:
        u = units[neighbor]
        add(ContextKind.NEIGHBOR, 'unit_context:' + _hash(u.unit_id), _hash((document.revision_id, u.source_digest)),
            u.source_text, u.order_key)
    if terminology is not None:
        if (type(terminology) is not Snapshot or not _valid_entries(terminology.entries) or profile.glossary is None
                or profile.facts.glossary_stage is not GlossaryStage.PROVIDER
                or terminology.reference != profile.glossary.reference
                or terminology.revision != profile.glossary.revision.value):
            raise ContextError('context_integrity')
        for term in sorted(terminology.entries, key=lambda e: (e.term, e.rule)):
            if term.term not in current.source_text:
                continue
            if term.rule not in ('translate', 'preserve'):
                continue
            text = term.term if term.rule == 'preserve' else term.term + '\n' + (term.translation or '')
            kind = ContextKind.PROTECTED_TERM if term.rule == 'preserve' else ContextKind.TERMINOLOGY
            add(kind, 'term_context:' + _hash((terminology.reference.value, term.term)),
                terminology.revision, text, required=term.required)
    snapshot = ContextSnapshot(profile, UnitReference(document.document_id, document.revision_id, unit_id), current, tuple(entries))
    return snapshot, SnapshotContextCandidateSource(tuple(stored))
