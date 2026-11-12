from contextlib import contextmanager
from dataclasses import FrozenInstanceError, replace
import ast
import json
from pathlib import Path
import subprocess
import sys
import pytest
from ayvu.knowledge.cache import OpaqueSemanticRevision, SemanticOperationV2, SemanticRouteHop
from ayvu.knowledge.glossary.catalog import GlossaryReference
from ayvu.preferences.model import GlobalPreferences, NamedPreferenceProfile, PreferencesError
from ayvu.preferences.service import PreferencesSnapshot
from ayvu.provenance.model import ProducerIdentity
from ayvu.projects.profiles.model import (
    ExecutionStage, GlossaryBinding, GlossaryStage, PlanFacts, ProfileError, ProfileOverrides,
    ProfileReference, ProfileRevision, ProjectReference, ProjectTranslationProfile,
    ProviderRequestProjection, ResolvedProfileSnapshot, SemanticEffect, SnapshotReference,
    TerminologySupport, canonical,
)
from ayvu.projects.profiles.service import (
    ProfileOutcome, ProfileRecord, ProjectProfileService, ProjectProfileState,
)
from ayvu.projects.profiles.store import decode_state, encode_state

PROJECT = ProjectReference('00000000-0000-0000-0000-000000000001')


PROFILE = ProfileReference('profile:' + '1' * 64)


REV = ProfileRevision('a' * 64)


def binding(revision='b', name='canary-glossary.json'):
    return GlossaryBinding(GlossaryReference(name), ProfileRevision(revision * 64))


def facts(target='pt', glossary=None, stage=ExecutionStage.TRANSLATE, native=False):
    if stage is ExecutionStage.INSPECT:
        return PlanFacts(stage, GlossaryStage.NONE, None)
    request = ProviderRequestProjection(target, REV,
        TerminologySupport.SUPPORTED if native else TerminologySupport.UNSUPPORTED,
        glossary if native else None, REV if native else None)
    return PlanFacts(stage, GlossaryStage.PROVIDER if native else
                     GlossaryStage.POST_CACHE if glossary else GlossaryStage.NONE, request)


def snapshot(profile=None, plan=None, overrides=ProfileOverrides(), default='pt', project_revision=3):
    profile = profile or ProjectTranslationProfile()
    return ResolvedProfileSnapshot(PROJECT, project_revision, PROFILE, profile, overrides,
                                   default, plan or facts())


class Projects:
    def __init__(self):
        self.state = ProjectProfileState(PROJECT, 1)
        self.writes = 0
        self.fail = False

    def read(self, project):
        assert project == PROJECT
        return self.state

    def publish(self, state, expected_revision, cancel, validate):
        assert self.state.project_revision == expected_revision
        validate()
        if self.fail:
            raise ProfileError('persistence')
        if cancel and cancel():
            raise ProfileError('cancelled')
        self.writes += 1
        self.state = state
        return state


class Glossaries:
    def __init__(self):
        self.resolved = binding()
        self.block = None
        self.requests = []
        self.held = []

    def resolve_legacy(self, path, cancel=None):
        self.requests.append(path)
        if self.block:
            raise ProfileError('unresolved_glossary', self.block)
        return self.resolved

    @contextmanager
    def hold(self, bindings, cancel=None):
        self.held.append(bindings)
        def validate():
            for b in bindings:
                if b != self.resolved:
                    raise ProfileError('unresolved_glossary', 'stale')
        validate()
        yield validate


class Preferences:
    def __init__(self, profile=NamedPreferenceProfile('fr', 'legacy-private/path.json', 'literary')):
        self.profile = profile
        self.saved = 0

    def load(self):
        return PreferencesSnapshot(GlobalPreferences(profiles=(('private-name-canary', self.profile),)), 'a' * 64, True)

    def save(self, *args):
        self.saved += 1
        raise AssertionError('Migration must never write preferences')


@pytest.fixture
def service():
    return ProjectProfileService(Projects(), Glossaries())


def migrate(service, prefs=None, expected=1):
    return service.migrate(prefs or Preferences(), 'private-name-canary', PROJECT, expected, PROFILE)
