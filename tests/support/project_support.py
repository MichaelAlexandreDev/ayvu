from dataclasses import replace
import json
import pytest
from ayvu.projects.model import ProjectError, ProjectManifest, ProjectRequest
from ayvu.projects.store import decode_manifest, encode_manifest

def manifest():
    request = ProjectRequest('/private/source.epub', 10, 'a' * 64, 'en', 'pt')
    return ProjectManifest.create(request, '12345678-1234-4234-8234-123456789abc')
