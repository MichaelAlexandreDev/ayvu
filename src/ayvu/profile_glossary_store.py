"""Legacy path compatibility adapter over #157's managed catalog and lock rules."""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path

from .glossary_catalog import GlossaryReference, Snapshot, read_glossary
from .glossary_catalog_store import ManagedGlossaryRepository, _Failure, _key
from .project_profile_service import check_cancel
from .project_profiles import GlossaryBinding, ProfileError, ProfileRevision


def _blocked(code: str) -> ProfileError:
    if code == 'cancelled':
        return ProfileError('cancelled')
    reason = {'missing': 'missing', 'stale-revision': 'stale',
              'unsafe-root': 'unsafe', 'unsafe-link': 'unsafe', 'unsafe-multilink': 'unsafe',
              'invalid-reference': 'unsafe', 'invalid': 'unsafe', 'oversized': 'unsafe'}.get(code, 'io')
    return ProfileError('unresolved_glossary', reason)


class LegacyGlossaryResolver:
    def __init__(self, repository: ManagedGlossaryRepository):
        self.repository = repository

    def _read(self, reference, cancel):
        result = read_glossary(self.repository, reference, cancel)
        if result.failure is not None:
            raise _blocked(result.failure)
        if type(result.value) is not Snapshot or result.value.reference != reference:
            raise _blocked('invalid')
        return GlossaryBinding(reference, ProfileRevision(result.value.revision))

    def _aliases(self, fd, reference):
        aliases = [n for n in self.repository._names(fd) if _key(n) == _key(reference.value)]
        if len(aliases) > 1 or (aliases and aliases != [reference.value]):
            raise ProfileError('unresolved_glossary', 'ambiguous')

    def resolve_legacy(self, path: str, cancel=None) -> GlossaryBinding:
        try:
            check_cancel(cancel)
            # Relative legacy selections use the injected catalog, as the CLI does.
            candidate = Path(path).expanduser()
            if not candidate.is_absolute():
                candidate = self.repository._root / candidate
            candidate = Path(os.path.abspath(candidate))
            # Do not resolve away a symlink before the catalog can refuse it.
            if any(p.is_symlink() for p in (candidate, *candidate.parents)):
                raise ProfileError('unresolved_glossary', 'unsafe')
            canonical = candidate.resolve(strict=False)
            if canonical.parent != self.repository._root:
                raise ProfileError('unresolved_glossary', 'external')
            reference = GlossaryReference(canonical.name)
            with self.repository._directory() as fd:
                self._aliases(fd, reference)
                return self._read(reference, cancel)
        except ProfileError:
            raise
        except _Failure as error:
            raise _blocked(str(error)) from None
        except (OSError, ValueError, RuntimeError):
            raise ProfileError('unresolved_glossary', 'unsafe') from None

    @contextmanager
    def hold(self, bindings, cancel=None):
        try:
            check_cancel(cancel)
            if not bindings:
                yield lambda: check_cancel(cancel)
                return
            with self.repository._directory() as fd:
                with self.repository._lock(fd) as lock:
                    def validate():
                        check_cancel(cancel)
                        self.repository._check_root(fd)
                        self.repository._check_lock(fd, lock)
                        for binding in bindings:
                            self._aliases(fd, binding.reference)
                            if self._read(binding.reference, cancel) != binding:
                                raise ProfileError('unresolved_glossary', 'stale')
                    validate()
                    yield validate
        except ProfileError:
            raise
        except _Failure as error:
            raise _blocked(str(error)) from None
        except OSError:
            raise ProfileError('unresolved_glossary', 'io') from None
