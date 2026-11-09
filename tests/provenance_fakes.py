"""A synthetic mixed-source lineage over a real job and a real artifact, for reuse by later integrations."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from ayvu.artifact import ArtifactRequest
from ayvu.job import JobRequest, JobState
from ayvu.project_schema import open_artifact_service, open_job_repository, open_provenance_repository
from ayvu.provenance import ActorKind, ProducerIdentity, ProvenanceRequest, SourceKind, SubjectRef

PROJECT = str(UUID(int=1))
INPUT_FINGERPRINT = 'sha256:' + 'e' * 64
START = datetime(2026, 11, 3, 12, tzinfo=UTC)
PIPELINE = ProducerIdentity(provider='not_applicable', model='not_applicable', adapter='epub@1', pipeline='segments@1')
MACHINE = ProducerIdentity(provider='libretranslate@1.6', model='unknown', adapter='epub@1', pipeline='segments@1')
PERSON = ProducerIdentity(provider='not_applicable', model='not_applicable', adapter='not_applicable',
                          pipeline='not_applicable')


def subject(value: str) -> SubjectRef:
    return SubjectRef('content_unit', 1, value)


class SyntheticLineage:
    def __init__(self, tmp_path) -> None:
        self.database = tmp_path / 'project.sqlite'
        self.backups = tmp_path / 'backups'
        self.artifact_root = tmp_path / 'artifacts'
        self.now = START
        self.next_id = 2000
        self.jobs = open_job_repository(self.database, self.backups, PROJECT, clock=self.clock)
        self.repository = self.open()
        self.job = self.running_job()

    def clock(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now

    def new_id(self) -> str:
        self.next_id += 1
        return str(UUID(int=self.next_id))

    def open(self):
        return open_provenance_repository(self.database, self.backups, PROJECT, clock=self.clock, new_id=self.new_id)

    def running_job(self):
        job = self.jobs.create(JobRequest(PROJECT, 'translate_epub', 1, INPUT_FINGERPRINT))
        for state in (JobState.PREFLIGHT, JobState.RUNNING):
            job = self.jobs.transition(job.job_id, state, expected_revision=job.revision)
        return job

    def request(self, operation: str = 'translate', **changes) -> ProvenanceRequest:
        return ProvenanceRequest(**{'project_id': PROJECT, 'job_id': self.job.job_id, 'operation': operation,
                                    'actor_kind': ActorKind.SYSTEM, 'source_kind': SourceKind.PIPELINE,
                                    'producer': PIPELINE, **changes})

    def append(self, operation: str = 'translate', **changes):
        return self.repository.append(self.request(operation, **changes))

    def publish_artifact(self, role: str = 'translated_document'):
        lease = self.jobs.get(self.job.job_id).job.lease
        token = lease.owner_token if lease else self.jobs.acquire_lease(self.job.job_id, timedelta(hours=1)).lease.owner_token
        service = open_artifact_service(self.database, self.backups, self.artifact_root, PROJECT, clock=self.clock,
                                        new_id=self.new_id)
        return service.publish(ArtifactRequest(PROJECT, self.job.job_id, role, 'publish', 1, INPUT_FINGERPRINT),
                               (b'synthetic output',), owner_token=token)

    def build_mixed_graph(self) -> dict:
        """Input to output, through every source kind a translated book can mix."""
        records = {}
        records['extract'] = self.append('extract', configuration_refs=())
        parent = (records['extract'].record_id,)
        records['cache'] = self.append('reuse_exact_cache', source_kind=SourceKind.CACHE, subject=subject('unit-1'), parents=parent)
        records['memory'] = self.append('apply_translation_memory', source_kind=SourceKind.TRANSLATION_MEMORY,
                                        subject=subject('unit-2'), parents=parent)
        records['machine'] = self.append('translate', actor_kind=ActorKind.PROVIDER, source_kind=SourceKind.PROVIDER,
                                         producer=MACHINE, subject=subject('unit-3'), parents=parent)
        records['review'] = self.append('human_review', actor_kind=ActorKind.HUMAN, source_kind=SourceKind.HUMAN,
                                        producer=PERSON, subject=subject('unit-3'), outcome_code='approved',
                                        parents=(records['machine'].record_id,))
        records['assemble'] = self.append('reconstruct', parents=(
            records['cache'].record_id, records['memory'].record_id, records['review'].record_id))
        records['validate'] = self.append('validate', source_kind=SourceKind.VALIDATION, outcome_code='passed',
                                          parents=(records['assemble'].record_id,))
        artifact = self.publish_artifact()
        records['publish'] = self.append('publish', parents=(records['validate'].record_id,),
                                         artifacts=(artifact.artifact_id,))
        self.artifact = artifact
        return records
