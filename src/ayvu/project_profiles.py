"""Closed project profile v1 and resolution policy v1. No persistence or presentation.

Canonical bytes use a tagged, length-prefixed UTF-8 encoding (not JSON). Mappings
sort by their ASCII schema keys. Null is always explicit in persisted schemas.
Only the glossary owner interprets a GlossaryReference.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import hashlib
import re
from uuid import UUID, uuid4

from .glossary_catalog import GlossaryReference
from .preferences import NamedPreferenceProfile, PreferencesError
from .project import ProjectManifestError, validate_project_reference

SCHEMA_VERSION = 1
RESOLUTION_POLICY_VERSION = 1
MAX_PROFILES = 64  # #152
MAX_HISTORY = 1024  # bounded project-owned history; exhaustion requires explicit archival
FAILURES = frozenset({'invalid_profile', 'unsupported_version', 'conflict',
    'unresolved_glossary', 'cancelled', 'persistence', 'not_found', 'invalid_plan', 'limit'})
GLOSSARY_FAILURES = frozenset({'missing', 'external', 'ambiguous', 'unsafe', 'stale', 'io'})


class ProfileError(Exception):
    """Typed application refusal, distinct from infrastructure parsing errors."""
    def __init__(self, code: str, reason: str | None = None):
        self.code = code if code in FAILURES else 'persistence'
        self.reason = reason if reason in GLOSSARY_FAILURES else None
        super().__init__(self.code)


def closed(data: object, keys: tuple[str, ...]) -> dict:
    if type(data) is not dict or set(data) != set(keys):
        raise ProfileError('invalid_profile')
    return data


def version(value: object) -> None:
    if type(value) is not int:
        raise ProfileError('invalid_profile')
    if value != 1:
        raise ProfileError('unsupported_version')


def canonical(value: object) -> bytes:
    """Canonical, injective encoding of the closed projection's primitive values."""
    if value is None:
        return b'n'
    if type(value) is bool:
        return b't' if value else b'f'
    if type(value) is int:
        return b'i' + str(value).encode('ascii') + b';'
    if type(value) is str:
        try:
            encoded = value.encode('utf-8')
        except UnicodeError:
            raise ProfileError('invalid_profile') from None
        return b's' + str(len(encoded)).encode('ascii') + b':' + encoded
    if type(value) in (tuple, list):
        return b'l' + str(len(value)).encode('ascii') + b':' + b''.join(canonical(x) for x in value)
    if type(value) is dict and all(type(k) is str for k in value):
        return b'm' + canonical(tuple((key, value[key]) for key in sorted(value)))
    raise ProfileError('invalid_profile')


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


@dataclass(frozen=True, repr=False)
class ProjectReference:
    value: str

    def __post_init__(self):
        try:
            if type(self.value) is not str or str(UUID(self.value)) != self.value:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise ProfileError('invalid_profile') from None

    def diagnostic(self) -> dict:
        return {'kind': 'project_reference'}


@dataclass(frozen=True, repr=False)
class ProfileReference:
    value: str

    def __post_init__(self):
        try:
            validate_project_reference(self.value)
            if self.value is None or not self.value.startswith('profile:'):
                raise ProfileError('invalid_profile')
        except ProjectManifestError:
            raise ProfileError('invalid_profile') from None

    @classmethod
    def new(cls) -> ProfileReference:
        return cls('profile:' + hashlib.sha256(uuid4().bytes).hexdigest())

    def diagnostic(self) -> dict:
        return {'kind': 'profile_reference'}


@dataclass(frozen=True, repr=False)
class ProfileRevision:
    value: str

    def __post_init__(self):
        if type(self.value) is not str or not re.fullmatch('[0-9a-f]{64}', self.value):
            raise ProfileError('invalid_profile')

    def diagnostic(self) -> dict:
        return {'kind': 'revision'}


@dataclass(frozen=True, repr=False)
class SnapshotReference(ProfileRevision):
    def diagnostic(self) -> dict:
        return {'kind': 'snapshot_reference'}


@dataclass(frozen=True, repr=False)
class GlossaryBinding:
    reference: GlossaryReference
    revision: ProfileRevision

    def __post_init__(self):
        if type(self.reference) is not GlossaryReference or type(self.revision) is not ProfileRevision:
            raise ProfileError('invalid_profile')
        try:
            GlossaryReference(self.reference.value)
        except ValueError:
            raise ProfileError('invalid_profile') from None

    def to_mapping(self) -> dict:
        return {'reference': self.reference.value, 'revision': self.revision.value}

    @classmethod
    def from_mapping(cls, data: object) -> GlossaryBinding | None:
        if data is None:
            return None
        closed(data, ('reference', 'revision'))
        try:
            return cls(GlossaryReference(data['reference']), ProfileRevision(data['revision']))
        except (ValueError, TypeError):
            raise ProfileError('invalid_profile') from None


def binding_data(value: GlossaryBinding | None) -> dict | None:
    return None if value is None else value.to_mapping()


def language(value: object, *, nullable=False) -> None:
    try:
        NamedPreferenceProfile(target_language=value if value is not None else None)
    except PreferencesError:
        raise ProfileError('invalid_profile') from None
    if value is None and not nullable:
        raise ProfileError('invalid_profile')


@dataclass(frozen=True, repr=False)
class ProjectTranslationProfile:
    target_language: str | None = None
    glossary: GlossaryBinding | None = None
    style: str | None = None
    version: int = SCHEMA_VERSION

    def __post_init__(self):
        version(self.version)
        try:
            NamedPreferenceProfile(target_language=self.target_language, style=self.style)
        except PreferencesError:
            raise ProfileError('invalid_profile') from None
        if self.glossary is not None and type(self.glossary) is not GlossaryBinding:
            raise ProfileError('invalid_profile')

    def to_mapping(self) -> dict:
        return {'version': self.version, 'target_language': self.target_language,
                'glossary': binding_data(self.glossary), 'style': self.style}

    @classmethod
    def from_mapping(cls, data: object) -> ProjectTranslationProfile:
        closed(data, ('version', 'target_language', 'glossary', 'style'))
        version(data['version'])
        return cls(data['target_language'], GlossaryBinding.from_mapping(data['glossary']), data['style'], data['version'])

    def canonical_payload(self) -> bytes:
        return canonical(self.to_mapping())

    @property
    def revision(self) -> ProfileRevision:
        return ProfileRevision(digest(('ayvu.project_profile', self.to_mapping())))

    def diagnostic(self) -> dict:
        return {'schema_version': self.version, 'has_glossary': self.glossary is not None}


@dataclass(frozen=True, repr=False)
class ProfileOverrides:
    target_language: str | None = None
    glossary_present: bool = False
    glossary: GlossaryBinding | None = None

    def __post_init__(self):
        if self.target_language is not None:
            if type(self.target_language) is not str:
                raise ProfileError('invalid_profile')
            # CLI's blank target means no override; a nonblank target is stripped.
            object.__setattr__(self, 'target_language', self.target_language.strip() or None)
        language(self.target_language, nullable=True)
        if (type(self.glossary_present) is not bool
                or (self.glossary is not None and type(self.glossary) is not GlossaryBinding)
                or (not self.glossary_present and self.glossary is not None)):
            raise ProfileError('invalid_profile')

    def to_mapping(self) -> dict:
        return {'target_language': self.target_language, 'glossary_present': self.glossary_present,
                'glossary': binding_data(self.glossary)}

    @classmethod
    def from_mapping(cls, data: object) -> ProfileOverrides:
        closed(data, ('target_language', 'glossary_present', 'glossary'))
        result = cls(data['target_language'], data['glossary_present'], GlossaryBinding.from_mapping(data['glossary']))
        if result.to_mapping() != data:
            raise ProfileError('invalid_profile')
        return result


class ExecutionStage(StrEnum):
    TRANSLATE = 'translate'
    REPLAY = 'replay'
    INSPECT = 'inspect'


class GlossaryStage(StrEnum):
    NONE = 'none'
    POST_CACHE = 'post_cache'
    PROVIDER = 'provider'


class TerminologySupport(StrEnum):
    SUPPORTED = 'supported'
    UNSUPPORTED = 'unsupported'
    UNKNOWN = 'unknown'


@dataclass(frozen=True, repr=False)
class ProviderRequestProjection:
    """Trusted capability negotiation and exact request facts, never adapter metadata.

    This is a profile-specific projection, not a new provider request interface.
    The capability revision and terminology policy are owner-issued opaque tokens.
    """
    target_language: str
    capability_revision: ProfileRevision
    terminology_support: TerminologySupport = TerminologySupport.UNSUPPORTED
    terminology: GlossaryBinding | None = None
    terminology_policy: ProfileRevision | None = None

    def __post_init__(self):
        language(self.target_language)
        if (type(self.capability_revision) is not ProfileRevision
                or type(self.terminology_support) is not TerminologySupport
                or (self.terminology is not None and type(self.terminology) is not GlossaryBinding)
                or (self.terminology_policy is not None and type(self.terminology_policy) is not ProfileRevision)
                or (self.terminology is None) != (self.terminology_policy is None)
                or (self.terminology is not None and self.terminology_support is not TerminologySupport.SUPPORTED)):
            raise ProfileError('invalid_plan')

    def to_mapping(self) -> dict:
        return {'target_language': self.target_language, 'capability_revision': self.capability_revision.value,
                'terminology_support': self.terminology_support.value, 'terminology': binding_data(self.terminology),
                'terminology_policy': None if self.terminology_policy is None else self.terminology_policy.value}

    @classmethod
    def from_mapping(cls, data: object) -> ProviderRequestProjection | None:
        if data is None:
            return None
        closed(data, ('target_language', 'capability_revision', 'terminology_support', 'terminology', 'terminology_policy'))
        try:
            return cls(data['target_language'], ProfileRevision(data['capability_revision']),
                TerminologySupport(data['terminology_support']), GlossaryBinding.from_mapping(data['terminology']),
                None if data['terminology_policy'] is None else ProfileRevision(data['terminology_policy']))
        except (ValueError, TypeError):
            raise ProfileError('invalid_plan') from None


@dataclass(frozen=True, repr=False)
class PlanFacts:
    stage: ExecutionStage
    glossary_stage: GlossaryStage
    provider: ProviderRequestProjection | None
    version: int = 1

    def __post_init__(self):
        version(self.version)
        if (type(self.stage) is not ExecutionStage or type(self.glossary_stage) is not GlossaryStage
                or (self.provider is not None and type(self.provider) is not ProviderRequestProjection)):
            raise ProfileError('invalid_plan')
        if self.stage is ExecutionStage.INSPECT:
            if self.provider is not None or self.glossary_stage is not GlossaryStage.NONE:
                raise ProfileError('invalid_plan')
        elif self.provider is None:
            raise ProfileError('invalid_plan')
        if self.provider is not None and ((self.glossary_stage is GlossaryStage.PROVIDER) != (self.provider.terminology is not None)):
            raise ProfileError('invalid_plan')

    def to_mapping(self) -> dict:
        return {'version': self.version, 'stage': self.stage.value, 'glossary_stage': self.glossary_stage.value,
                'provider': None if self.provider is None else self.provider.to_mapping()}

    @classmethod
    def from_mapping(cls, data: object) -> PlanFacts:
        closed(data, ('version', 'stage', 'glossary_stage', 'provider'))
        version(data['version'])
        try:
            return cls(ExecutionStage(data['stage']), GlossaryStage(data['glossary_stage']),
                       ProviderRequestProjection.from_mapping(data['provider']), data['version'])
        except (ValueError, TypeError):
            raise ProfileError('invalid_plan') from None


class SemanticEffect(StrEnum):
    RAW_PROVIDER = 'raw_provider_affecting'
    DOWNSTREAM = 'downstream_only'
    OPERATIONAL = 'operational_only'
    INERT = 'inert'


@dataclass(frozen=True, repr=False, init=False)
class SemanticEffectProjection:
    """Only classify_effects constructs this value; callers cannot assign classes."""
    dimensions: tuple[tuple[str, SemanticEffect, str | None], ...]

    def partition(self, effect: SemanticEffect) -> tuple[tuple[str, str | None], ...]:
        if type(effect) is not SemanticEffect:
            raise ProfileError('invalid_plan')
        return tuple((name, value) for name, classification, value in self.dimensions if classification is effect)

    def to_mapping(self) -> dict:
        return {'version': RESOLUTION_POLICY_VERSION,
                **{effect.value: dict(self.partition(effect)) for effect in SemanticEffect}}

    def raw_projection(self) -> bytes:
        return canonical({'version': RESOLUTION_POLICY_VERSION, 'raw': dict(self.partition(SemanticEffect.RAW_PROVIDER))})

    def downstream_projection(self) -> bytes:
        return canonical({'version': RESOLUTION_POLICY_VERSION, 'downstream': dict(self.partition(SemanticEffect.DOWNSTREAM))})

    def diagnostic(self) -> dict:
        return {'policy_version': RESOLUTION_POLICY_VERSION,
                'classes': tuple((name, effect.value) for name, effect, _ in self.dimensions)}


def classify_effects(target: str, glossary: GlossaryBinding | None, style: str | None,
                     facts: PlanFacts) -> SemanticEffectProjection:
    if type(facts) is not PlanFacts:
        raise ProfileError('invalid_plan')
    facts.__post_init__()
    ProjectTranslationProfile(target, glossary, style)
    inert = SemanticEffect.INERT
    target_effect = inert if facts.stage is ExecutionStage.INSPECT else SemanticEffect.RAW_PROVIDER
    if facts.provider is not None and facts.provider.target_language != target:
        raise ProfileError('invalid_plan')
    glossary_effect, contribution = inert, None
    if facts.glossary_stage is not GlossaryStage.NONE:
        if glossary is None:
            raise ProfileError('invalid_plan')
        if facts.glossary_stage is GlossaryStage.POST_CACHE:
            glossary_effect = SemanticEffect.DOWNSTREAM
            contribution = 'glossary-effect:' + digest(('post-cache-v1', glossary.to_mapping()))
        else:
            if facts.provider.terminology != glossary:
                raise ProfileError('invalid_plan')
            glossary_effect = SemanticEffect.RAW_PROVIDER
            contribution = 'glossary-effect:' + digest(('provider-terminology-v1', glossary.to_mapping(),
                facts.provider.terminology_policy.value, facts.provider.capability_revision.value))
    result = object.__new__(SemanticEffectProjection)
    object.__setattr__(result, 'dimensions', (
        ('target_language', target_effect, target),
        ('glossary', glossary_effect, contribution),
        ('style', inert, style),
        ('execution_stage', SemanticEffect.OPERATIONAL, facts.stage.value),
    ))
    return result


@dataclass(frozen=True, repr=False)
class ResolvedProfileSnapshot:
    project: ProjectReference
    project_revision: int
    profile: ProfileReference
    profile_value: ProjectTranslationProfile
    overrides: ProfileOverrides
    default_target: str
    facts: PlanFacts
    policy_version: int = RESOLUTION_POLICY_VERSION

    def __post_init__(self):
        version(self.policy_version)
        if (type(self.project) is not ProjectReference or type(self.profile) is not ProfileReference
                or type(self.project_revision) is not int or not 1 <= self.project_revision < 2**63
                or type(self.profile_value) is not ProjectTranslationProfile or type(self.overrides) is not ProfileOverrides):
            raise ProfileError('invalid_profile')
        language(self.default_target)
        self.effects  # complete classification is mandatory, even on decode

    @property
    def target_language(self) -> str:
        return self.overrides.target_language or self.profile_value.target_language or self.default_target

    @property
    def glossary(self) -> GlossaryBinding | None:
        return self.overrides.glossary if self.overrides.glossary_present else self.profile_value.glossary

    @property
    def effects(self) -> SemanticEffectProjection:
        return classify_effects(self.target_language, self.glossary, self.profile_value.style, self.facts)

    def to_mapping(self) -> dict:
        return {'version': 1, 'project': self.project.value, 'project_revision': self.project_revision,
                'profile': self.profile.value, 'profile_revision': self.profile_value.revision.value,
                'profile_value': self.profile_value.to_mapping(), 'overrides': self.overrides.to_mapping(),
                'default_target': self.default_target, 'facts': self.facts.to_mapping(),
                'policy_version': self.policy_version, 'effects': self.effects.to_mapping()}

    @classmethod
    def from_mapping(cls, data: object) -> ResolvedProfileSnapshot:
        closed(data, ('version', 'project', 'project_revision', 'profile', 'profile_revision', 'profile_value',
                      'overrides', 'default_target', 'facts', 'policy_version', 'effects'))
        version(data['version'])
        result = cls(ProjectReference(data['project']), data['project_revision'], ProfileReference(data['profile']),
                     ProjectTranslationProfile.from_mapping(data['profile_value']), ProfileOverrides.from_mapping(data['overrides']),
                     data['default_target'], PlanFacts.from_mapping(data['facts']), data['policy_version'])
        if canonical(result.to_mapping()) != canonical(data):
            raise ProfileError('invalid_profile')
        return result

    @property
    def reference(self) -> SnapshotReference:
        return SnapshotReference(digest(('ayvu.profile_snapshot', self.to_mapping())))

    def raw_semantic_contribution(self) -> dict:
        """Arguments for #153 OpaqueSemanticRevision; never use the full snapshot hash.

        Project ownership persists the full evidence, while this address selects
        only the raw partition. Downstream/style/operational changes stay out.
        """
        value = hashlib.sha256(self.effects.raw_projection()).hexdigest()
        return {'owner': 'ayvu', 'namespace': 'project_profile', 'version': 1,
                'value': 'profile-effect:' + value, 'snapshot_reference': 'profile_effect:' + value}

    def diagnostic(self) -> dict:
        return {'schema_version': 1, **self.effects.diagnostic()}
