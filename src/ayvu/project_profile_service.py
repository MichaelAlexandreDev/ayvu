"""Project-owned profile application ports, history, and explicit v1 migration."""
from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Callable, Protocol

from .preferences import GlobalPreferences, PreferencesError
from .preferences_service import PreferencesRepository, get_preferences
from .project_profiles import (
    MAX_HISTORY, MAX_PROFILES, GlossaryBinding, PlanFacts, ProfileError, ProfileOverrides,
    ProfileReference, ProfileRevision, ProjectReference, ProjectTranslationProfile,
    ResolvedProfileSnapshot, SnapshotReference, closed, version,
)

Cancel = Callable[[], bool] | None


def check_cancel(cancel: Cancel) -> None:
    if cancel is not None and cancel():
        raise ProfileError('cancelled')


def _expected(project, revision, reference):
    if (type(project) is not ProjectReference or type(reference) is not ProfileReference
            or type(revision) is not int or not 1 <= revision < 2**63):
        raise ProfileError('invalid_profile')


@dataclass(frozen=True, repr=False)
class ProfileRecord:
    reference: ProfileReference
    value: ProjectTranslationProfile

    def __post_init__(self):
        if type(self.reference) is not ProfileReference or type(self.value) is not ProjectTranslationProfile:
            raise ProfileError('invalid_profile')

    def to_mapping(self) -> dict:
        return {'reference': self.reference.value, 'value': self.value.to_mapping()}


@dataclass(frozen=True, repr=False)
class ProjectProfileState:
    project: ProjectReference
    project_revision: int
    records: tuple[ProfileRecord, ...] = ()
    heads: tuple[tuple[ProfileReference, ProfileRevision], ...] = ()
    migrations: tuple[tuple[ProfileReference, ProfileRevision], ...] = ()
    snapshots: tuple[ResolvedProfileSnapshot, ...] = ()

    def __post_init__(self):
        if (type(self.project) is not ProjectReference or type(self.project_revision) is not int
                or not 1 <= self.project_revision < 2**63):
            raise ProfileError('invalid_profile')
        for values, limit in ((self.records, MAX_HISTORY), (self.heads, MAX_PROFILES),
                              (self.migrations, MAX_PROFILES), (self.snapshots, MAX_HISTORY)):
            if type(values) is not tuple or len(values) > limit:
                raise ProfileError('limit')
        keys = set()
        for record in self.records:
            if type(record) is not ProfileRecord:
                raise ProfileError('invalid_profile')
            key = (record.reference, record.value.revision)
            if key in keys:
                raise ProfileError('invalid_profile')
            keys.add(key)
        for collection in (self.heads, self.migrations):
            seen = set()
            for pair in collection:
                if (type(pair) is not tuple or len(pair) != 2 or type(pair[0]) is not ProfileReference
                        or type(pair[1]) is not ProfileRevision or pair[0] in seen or pair not in keys):
                    raise ProfileError('invalid_profile')
                seen.add(pair[0])
        if {r.reference for r in self.records} != {r for r, _ in self.heads}:
            raise ProfileError('invalid_profile')
        seen = set()
        for snapshot in self.snapshots:
            if (type(snapshot) is not ResolvedProfileSnapshot or snapshot.project != self.project
                    or snapshot.project_revision >= self.project_revision
                    or (snapshot.profile, snapshot.profile_value.revision) not in keys
                    or snapshot.reference in seen):
                raise ProfileError('invalid_profile')
            seen.add(snapshot.reference)
        object.__setattr__(self, 'records', tuple(sorted(self.records, key=lambda r: (r.reference.value, r.value.revision.value))))
        for name in ('heads', 'migrations'):
            object.__setattr__(self, name, tuple(sorted(getattr(self, name), key=lambda p: p[0].value)))
        object.__setattr__(self, 'snapshots', tuple(sorted(self.snapshots, key=lambda s: s.reference.value)))

    def profile(self, reference: ProfileReference, revision: ProfileRevision | None = None) -> ProjectTranslationProfile:
        if revision is None:
            revision = dict(self.heads).get(reference)
        for record in self.records:
            if record.reference == reference and record.value.revision == revision:
                return record.value
        raise ProfileError('not_found')

    def snapshot(self, reference: SnapshotReference) -> ResolvedProfileSnapshot:
        for snapshot in self.snapshots:
            if snapshot.reference == reference:
                return snapshot
        raise ProfileError('not_found')

    def raw_projection(self, effect_reference: str) -> bytes:
        """Address durable raw-only evidence for a future #153 consumer."""
        for snapshot in self.snapshots:
            if snapshot.raw_semantic_contribution()['snapshot_reference'] == effect_reference:
                return snapshot.effects.raw_projection()
        raise ProfileError('not_found')

    def to_mapping(self) -> dict:
        return {'version': 1, 'project': self.project.value, 'project_revision': self.project_revision,
                'records': [r.to_mapping() for r in self.records],
                'heads': [[r.value, v.value] for r, v in self.heads],
                'migrations': [[r.value, v.value] for r, v in self.migrations],
                'snapshots': [s.to_mapping() for s in self.snapshots]}

    @classmethod
    def from_mapping(cls, data: object) -> ProjectProfileState:
        closed(data, ('version', 'project', 'project_revision', 'records', 'heads', 'migrations', 'snapshots'))
        version(data['version'])
        for key, limit in (('records', MAX_HISTORY), ('heads', MAX_PROFILES),
                           ('migrations', MAX_PROFILES), ('snapshots', MAX_HISTORY)):
            if type(data[key]) is not list or len(data[key]) > limit:
                raise ProfileError('invalid_profile')
        records = []
        for entry in data['records']:
            closed(entry, ('reference', 'value'))
            records.append(ProfileRecord(ProfileReference(entry['reference']), ProjectTranslationProfile.from_mapping(entry['value'])))
        collections = []
        for key in ('heads', 'migrations'):
            pairs = []
            for pair in data[key]:
                if type(pair) is not list or len(pair) != 2:
                    raise ProfileError('invalid_profile')
                pairs.append((ProfileReference(pair[0]), ProfileRevision(pair[1])))
            collections.append(tuple(pairs))
        return cls(ProjectReference(data['project']), data['project_revision'], tuple(records), *collections,
                   tuple(ResolvedProfileSnapshot.from_mapping(s) for s in data['snapshots']))

    def diagnostic(self) -> dict:
        return {'schema_version': 1, 'profiles': len(self.heads), 'snapshots': len(self.snapshots)}


class ProjectProfileRepository(Protocol):
    """CAS shares #134's project lock and publishes the manifest pointer atomically.

    A failed/cancelled CAS retains the previous state. validate runs immediately
    before publication while glossary ownership's revision guard is held.
    """
    def read(self, project: ProjectReference) -> ProjectProfileState: ...
    def publish(self, state: ProjectProfileState, expected_revision: int,
                cancel: Cancel, validate: Callable[[], None]) -> ProjectProfileState: ...


class ProfileGlossaries(Protocol):
    def resolve_legacy(self, path: str, cancel: Cancel = None) -> GlossaryBinding: ...
    def hold(self, bindings: tuple[GlossaryBinding, ...], cancel: Cancel = None
             ) -> AbstractContextManager[Callable[[], None]]: ...


class ProfileOutcome(StrEnum):
    MIGRATED = 'migrated'
    ALREADY_MIGRATED = 'already_migrated'
    UPDATED = 'updated'
    CREATED = 'created'
    RESOLVED = 'resolved'
    CONFLICT = 'conflict'
    UNRESOLVED_GLOSSARY = 'unresolved_glossary'
    UNSUPPORTED_VERSION = 'unsupported_version'
    INVALID_PROFILE = 'invalid_profile'
    CANCELLED = 'cancelled'
    PERSISTENCE = 'persistence'
    NOT_FOUND = 'not_found'
    INVALID_PLAN = 'invalid_plan'
    LIMIT = 'limit'


@dataclass(frozen=True, repr=False)
class ProfileResult:
    outcome: ProfileOutcome
    state: ProjectProfileState | None = None
    snapshot: ResolvedProfileSnapshot | None = None
    glossary_failure: str | None = None

    def diagnostic(self) -> dict:
        return {'outcome': self.outcome.value, 'glossary_failure': self.glossary_failure}


def _failure(error: ProfileError) -> ProfileResult:
    return ProfileResult(ProfileOutcome(error.code), glossary_failure=error.reason)


class ProjectProfileService:
    def __init__(self, repository: ProjectProfileRepository, glossaries: ProfileGlossaries):
        self.repository = repository
        self.glossaries = glossaries

    def _publish(self, state, expected, bindings, cancel):
        check_cancel(cancel)
        with self.glossaries.hold(tuple(dict.fromkeys(b for b in bindings if b is not None)), cancel) as validate:
            return self.repository.publish(state, expected, cancel, validate)

    def write(self, project: ProjectReference, expected_project_revision: int, reference: ProfileReference,
              profile: ProjectTranslationProfile, expected_profile_revision: ProfileRevision | None = None,
              cancel: Cancel = None) -> ProfileResult:
        try:
            check_cancel(cancel)
            _expected(project, expected_project_revision, reference)
            ProfileRecord(reference, profile)
            if expected_profile_revision is not None and type(expected_profile_revision) is not ProfileRevision:
                raise ProfileError('invalid_profile')
            state = self.repository.read(project)
            current = dict(state.heads).get(reference)
            if state.project_revision != expected_project_revision or current != expected_profile_revision:
                raise ProfileError('conflict')
            if current == profile.revision:
                with self.glossaries.hold(() if profile.glossary is None else (profile.glossary,), cancel) as validate:
                    validate()
                    check_cancel(cancel)
                return ProfileResult(ProfileOutcome.UPDATED, state)
            heads = dict(state.heads)
            heads[reference] = profile.revision
            records = state.records
            if not any(r.reference == reference and r.value == profile for r in records):
                records += (ProfileRecord(reference, profile),)
            updated = replace(state, project_revision=state.project_revision + 1, records=records, heads=tuple(heads.items()))
            committed = self._publish(updated, expected_project_revision, (profile.glossary,), cancel)
            return ProfileResult(ProfileOutcome.CREATED if current is None else ProfileOutcome.UPDATED, committed)
        except ProfileError as error:
            return _failure(error)

    def migrate(self, preferences: PreferencesRepository, name: str, project: ProjectReference,
                expected_project_revision: int, destination: ProfileReference, cancel: Cancel = None) -> ProfileResult:
        """The caller supplies the durable destination, never derived from the name.

        Names select one validated input only. A receipt records the initial
        content revision, so retries cannot overwrite later project edits.
        """
        try:
            check_cancel(cancel)
            _expected(project, expected_project_revision, destination)
            if type(name) is not str or not 1 <= len(name) <= 128:
                raise ProfileError('invalid_profile')
            source = get_preferences(preferences)
            if source.error is not None:
                raise ProfileError('unsupported_version' if source.error.code == 'unsupported_version' else 'invalid_profile')
            if source.read_only or source.warnings or type(source.preferences) is not GlobalPreferences:
                raise ProfileError('invalid_profile')
            source.preferences.__post_init__()
            legacy = dict(source.preferences.profiles).get(name)
            if legacy is None or type(destination) is not ProfileReference:
                raise ProfileError('invalid_profile')
            binding = None if legacy.glossary is None else self.glossaries.resolve_legacy(legacy.glossary, cancel)
            profile = ProjectTranslationProfile(legacy.target_language, binding, legacy.style)
            state = self.repository.read(project)
            receipt = dict(state.migrations).get(destination)
            if receipt is not None:
                if receipt != profile.revision:
                    raise ProfileError('conflict')
                # An idempotent retry revalidates exact glossary ownership too.
                with self.glossaries.hold(() if binding is None else (binding,), cancel) as validate:
                    validate()
                    check_cancel(cancel)
                return ProfileResult(ProfileOutcome.ALREADY_MIGRATED, state)
            if state.project_revision != expected_project_revision or destination in dict(state.heads):
                raise ProfileError('conflict')
            updated = replace(state, project_revision=state.project_revision + 1,
                records=state.records + (ProfileRecord(destination, profile),),
                heads=state.heads + ((destination, profile.revision),),
                migrations=state.migrations + ((destination, profile.revision),))
            committed = self._publish(updated, expected_project_revision, (binding,), cancel)
            return ProfileResult(ProfileOutcome.MIGRATED, committed)
        except PreferencesError:
            return _failure(ProfileError('invalid_profile'))
        except ProfileError as error:
            return _failure(error)

    def resolve(self, project: ProjectReference, expected_project_revision: int, reference: ProfileReference,
                expected_profile_revision: ProfileRevision, facts: PlanFacts, overrides: ProfileOverrides = ProfileOverrides(),
                default_target: str = 'pt', cancel: Cancel = None) -> ProfileResult:
        try:
            check_cancel(cancel)
            _expected(project, expected_project_revision, reference)
            if type(expected_profile_revision) is not ProfileRevision:
                raise ProfileError('invalid_profile')
            state = self.repository.read(project)
            try:
                value = state.profile(reference, expected_profile_revision)
            except ProfileError as error:
                if error.code != 'not_found':
                    raise
                raise ProfileError('conflict') from None
            snapshot = ResolvedProfileSnapshot(project, expected_project_revision, reference, value, overrides, default_target, facts)
            if any(s.reference == snapshot.reference for s in state.snapshots):
                if (state.project_revision != expected_project_revision + 1
                        or dict(state.heads).get(reference) != expected_profile_revision):
                    raise ProfileError('conflict')
                bindings = tuple(dict.fromkeys(b for b in (value.glossary, overrides.glossary) if b is not None))
                with self.glossaries.hold(bindings, cancel) as validate:
                    validate()
                    check_cancel(cancel)
                return ProfileResult(ProfileOutcome.RESOLVED, state, state.snapshot(snapshot.reference))
            if (state.project_revision != expected_project_revision
                    or dict(state.heads).get(reference) != expected_profile_revision):
                raise ProfileError('conflict')
            updated = replace(state, project_revision=state.project_revision + 1, snapshots=state.snapshots + (snapshot,))
            # Bind even an overridden/inert stored glossary, since it remains exact snapshot metadata.
            committed = self._publish(updated, expected_project_revision, (value.glossary, overrides.glossary), cancel)
            return ProfileResult(ProfileOutcome.RESOLVED, committed, snapshot)
        except ProfileError as error:
            return _failure(error)
