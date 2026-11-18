# CLI option boundary and surface snapshot

The option classification and the committed CLI surface snapshot of the [shared application use-case matrix](../modules/interfaces-cli.md). `tests/architecture/test_shared_use_case_matrix.py` compares the snapshot with the effective Typer surface; update both documents together.

## Option boundary

The committed snapshot below classifies every effective long option. Short
aliases such as `-o` are normalized to their canonical long option and inherit
the same mapping. Click's generated `--help` is recorded once as a universal
presentation option and verified at every command level:

- `domain-input`: content identity or selection, such as languages and chapters;
- `application-policy`: execution, cache, conflict, retry, or mutation policy;
- `adapter-configuration`: configuration of an external adapter, such as an HTTP
  endpoint or translation backend;
- `artifact-destination`: requested output/cache/input artifact location;
- `presentation-only`: rendering or shell interaction with no domain effect;
- `presentation-confirmation`: a CLI confirmation mechanism; the underlying
  permission to mutate is an application policy;
- `compatibility-routing`: legacy CLI routing to an existing use case.

Concrete paths are serializable application inputs today, but future interfaces
must apply their own filesystem authorization before invoking a service. Likewise,
retry/rate limits are stable policies even though the current CLI provides their
defaults. `--mode`, help/completion, prompt wording, tables, colors, and exit codes
remain presentation concerns.

<!-- AYVU_CLI_SURFACE_SNAPSHOT_START -->
```json
{
  "universal_options": {
    "--help": {
      "kind": "presentation-only",
      "mappings": [
        "AYVU-UI-004"
      ]
    }
  },
  "root_options": {
    "--network-policy": {
      "kind": "application-policy",
      "mappings": [
        "AYVU-UC-002",
        "AYVU-UC-003",
        "AYVU-UC-004",
        "AYVU-UC-005",
        "AYVU-UC-006",
        "AYVU-UC-007"
      ]
    },
    "--consent-remote": {
      "kind": "application-policy",
      "mappings": [
        "AYVU-UC-004",
        "AYVU-UC-005"
      ]
    },
    "--install-completion": {
      "kind": "presentation-only",
      "mappings": [
        "AYVU-UI-001"
      ]
    },
    "--mode": {
      "kind": "presentation-only",
      "mappings": [
        "AYVU-UI-002"
      ]
    },
    "--preview": {
      "kind": "compatibility-routing",
      "mappings": [
        "AYVU-UC-006"
      ]
    },
    "--show-completion": {
      "kind": "presentation-only",
      "mappings": [
        "AYVU-UI-001"
      ]
    },
    "--translator": {
      "kind": "adapter-configuration",
      "mappings": [
        "AYVU-UC-006"
      ]
    },
    "--model": {
      "kind": "adapter-configuration",
      "mappings": [
        "AYVU-UC-006"
      ]
    },
    "--url": {
      "kind": "adapter-configuration",
      "mappings": [
        "AYVU-UC-006"
      ]
    }
  },
  "commands": {
    "apply-review": {
      "use_cases": [
        "AYVU-UC-009"
      ],
      "options": {
        "--output": "artifact-destination",
        "--overwrite": "application-policy",
        "--validate-only": "application-policy"
      },
      "subcommands": {}
    },
    "cache": {
      "use_cases": [
        "AYVU-UI-003"
      ],
      "options": {},
      "subcommands": {
        "clean": {
          "use_cases": [
            "AYVU-UC-011"
          ],
          "options": {
            "--namespace": "application-policy",
            "--scope-kind": "domain-input",
            "--scope-id": "domain-input",
            "--project-id": "domain-input",
            "--all": "application-policy",
            "--before": "domain-input",
            "--cache": "artifact-destination",
            "--dry-run": "application-policy",
            "--source": "domain-input",
            "--target": "domain-input",
            "--yes": "presentation-confirmation"
          }
        },
        "export": {
          "use_cases": [
            "AYVU-UC-012"
          ],
          "options": {
            "--all": "application-policy",
            "--namespace": "application-policy",
            "--scope-kind": "domain-input",
            "--scope-id": "domain-input",
            "--project-id": "domain-input",
            "--before": "domain-input",
            "--cache": "artifact-destination",
            "--overwrite": "application-policy",
            "--source": "domain-input",
            "--target": "domain-input"
          }
        },
        "import": {
          "use_cases": [
            "AYVU-UC-013"
          ],
          "options": {
            "--all": "application-policy",
            "--dry-run": "application-policy",
            "--scope-map": "artifact-destination",
            "--yes": "presentation-confirmation",
            "--cache": "artifact-destination",
            "--replace": "application-policy"
          }
        },
        "inspect": {
          "use_cases": [
            "AYVU-UC-010"
          ],
          "options": {
            "--all": "application-policy",
            "--namespace": "application-policy",
            "--scope-kind": "domain-input",
            "--scope-id": "domain-input",
            "--project-id": "domain-input",
            "--before": "domain-input",
            "--cache": "artifact-destination",
            "--source": "domain-input",
            "--target": "domain-input"
          }
        }
      }
    },
    "consent": {
      "use_cases": [
        "AYVU-UI-005"
      ],
      "options": {},
      "subcommands": {
        "revoke": {
          "use_cases": [
            "AYVU-UC-017"
          ],
          "options": {
            "--translator": "domain-input"
          },
          "subcommands": {}
        }
      }
    },
    "extract": {
      "use_cases": [
        "AYVU-UC-008"
      ],
      "options": {
        "--output": "artifact-destination",
        "--overwrite": "application-policy"
      },
      "subcommands": {}
    },
    "inspect": {
      "use_cases": [
        "AYVU-UC-001"
      ],
      "options": {},
      "subcommands": {}
    },
    "languages": {
      "use_cases": [
        "AYVU-UC-003"
      ],
      "options": {
        "--network-policy": "application-policy",
        "--requests-per-second": "application-policy",
        "--retries": "application-policy",
        "--retry-backoff": "application-policy",
        "--retry-backoff-max": "application-policy",
        "--timeout": "application-policy",
        "--translator": "adapter-configuration",
        "--url": "adapter-configuration"
      },
      "subcommands": {}
    },
    "resume": {
      "use_cases": [
        "AYVU-UC-007"
      ],
      "options": {
        "--network-policy": "application-policy",
        "--target": "domain-input"
      },
      "subcommands": {}
    },
    "test-translator": {
      "use_cases": [
        "AYVU-UC-002"
      ],
      "options": {
        "--model": "adapter-configuration",
        "--network-policy": "application-policy",
        "--requests-per-second": "application-policy",
        "--retries": "application-policy",
        "--retry-backoff": "application-policy",
        "--retry-backoff-max": "application-policy",
        "--source": "domain-input",
        "--target": "domain-input",
        "--timeout": "application-policy",
        "--translator": "adapter-configuration",
        "--url": "adapter-configuration"
      },
      "subcommands": {}
    },
    "translate": {
      "use_cases": [
        "AYVU-UC-004",
        "AYVU-UC-005"
      ],
      "option_exclusions": {
        "AYVU-UC-005": [
          "--missing-output",
          "--output",
          "--review-output"
        ]
      },
      "options": {
        "--network-policy": "application-policy",
        "--cache": "artifact-destination",
        "--cache-only": "application-policy",
        "--chapters": "domain-input",
        "--chunk-limit": "application-policy",
        "--continue-on-error": "application-policy",
        "--dry-run": "application-policy",
        "--fail-fast": "application-policy",
        "--glossary": "artifact-destination",
        "--missing-output": "artifact-destination",
        "--output": "artifact-destination",
        "--output-dir": "artifact-destination",
        "--overwrite": "application-policy",
        "--performance-profile": "application-policy",
        "--profile": "domain-input",
        "--requests-per-second": "application-policy",
        "--require-full-cache": "application-policy",
        "--retries": "application-policy",
        "--retry-backoff": "application-policy",
        "--retry-backoff-max": "application-policy",
        "--review-output": "artifact-destination",
        "--source": "domain-input",
        "--target": "domain-input",
        "--timeout": "application-policy",
        "--tm-apply-threshold": "application-policy",
        "--tm-conservative-auto-apply": "application-policy",
        "--tm-legacy-cache-compatibility": "application-policy",
        "--tm-suggest-threshold": "application-policy",
        "--translate-alt-text": "application-policy",
        "--translate-metadata": "application-policy",
        "--translation-memory": "application-policy",
        "--no-translation-memory": "application-policy",
        "--translator": "adapter-configuration",
        "--url": "adapter-configuration",
        "--workers": "application-policy",
        "--model": "adapter-configuration"
      },
      "subcommands": {}
    }
  }
}
```
<!-- AYVU_CLI_SURFACE_SNAPSHOT_END -->
