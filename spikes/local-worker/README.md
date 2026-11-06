# Pesquisa sintética de worker local — #167

A [decisão](../../docs/architecture/adr-local-worker-isolation.md) permanece
`Proposed`. Este diretório é descartável e não contém um runner utilizável por
Ayvu. `probe.py` e `measure.py` usam apenas stdlib; somente testes importam
contratos de produção para verificar compatibilidade. Não executar com dados
reais ou estender os comandos para aceitar paths, textos ou credenciais.

Da raiz do checkout, sem instalar dependências e sem rede:

```bash
timeout 120s .venv/bin/python -m pytest -q tests/test_local_worker_spike.py
timeout 120s .venv/bin/python spikes/local-worker/measure.py > worker-measurements.json
.venv/bin/python -m pytest -q tests/test_documentation_links.py tests/test_architecture_boundaries.py
git diff --check
```

O segundo comando gera somente métricas estruturadas; o arquivo de saída pode
ser comparado com [a execução registrada](results/linux-x86_64.json) e removido.
Diretórios de trabalho são `TemporaryDirectory`/`tmp_path`, com fixtures geradas.
`HOME`/`XDG_CONFIG_HOME` não são herdados pelo worker. A única execução da CLI de
Ayvu nesta pesquisa foi o baseline `--help`, com home/config/cwd temporários.

O coletor tem orçamento global de 120 s. Cada teste tem watchdog SIGALRM de 8 s.
Cada operação de frame usa deadline único; startup 1+3 s, cancelamento 250 ms,
TERM 250 ms, KILL 1 s. Não há sleep de sincronização. Readiness, pipe, select,
EOF, timeout e pidfd coordenam os processos. Os waits nas cargas sintéticas
`hang`/`late_success` modelam trabalho bloqueado; não sincronizam o harness.

## Matriz reproduzível e limites

Alvo aceito interpretado da #124: Linux x86_64. Rodado em Python 3.12.12, kernel
6.18.60-1-lts, no sandbox disponível, em 2026-11-06. Windows/macOS não foram
executados e não são apresentados como plataformas aprovadas. Em outro alvo,
os testes são skipped e o coletor Linux não fornece validação nativa.

| Requisito | Teste em `tests/test_local_worker_spike.py` |
| --- | --- |
| Startup/sucesso e progresso #126 | `test_success_startup_and_canonical_progress`, ambos transportes |
| Cancelamento e ack/outcome | `test_cooperative_cancel_uses_canonical_ack_and_outcome`, decoder/ordenador reais |
| Hang, ignorar cancel e SIGKILL | `test_timeout_and_ignored_cancel_require_sigkill` |
| Crash, JSON inválido, tamanho, versão, EOF parcial, peer falso | `test_failure_is_specific_and_never_success`, ambos transportes |
| Deadline e resultado tardio | `test_late_result_is_not_accepted_after_deadline`, `test_fault_poisoning_prevents_later_success` |
| Framing e prazo de cabeçalho parcial | `test_frame_bounds_and_partial_header_use_one_deadline` |
| Backpressure instrumentado | `test_real_transport_backpressure_and_queue_credit`: lê 8 frames e conta bytes, recusa enfileirar sem ler, worker não consegue completar 100000 frames sem consumo, volta a drenar |
| Limite de stdout/stderr e privacidade | `test_stdout_stderr_are_bounded_discarded_and_refused` |
| Allowlist, ambiente, fd herdável, cwd, filesystem, criação de processo | `test_launch_allowlist_environment_descriptors_cwd_and_residual_authority` |
| Contraprova de herança sem mitigação | `test_default_subprocess_inherits_environment_descriptor_and_cwd` |
| Filho/neto e morte do pai | `test_force_cleanup_includes_child_and_grandchild`, `test_parent_crash_eof_cleans_worker_child_and_grandchild`, ambos transportes |
| Injeção sem descritor e endpoint obsoleto | `test_peer_injection_and_stale_endpoint_cannot_address_anonymous_channel` |
| Correlação/ordem/direção | `test_bound_peer_correlation_order_and_direction` |
| Desserialização executável, canários e schemas | `test_executable_deserialization_and_redaction`, `test_command_schema_rejects_object_hooks_and_raw_values_in_worker`, `test_invalid_handshake_and_executable_bytes_are_rejected_over_real_transport` |
| Restrição de rede | `test_controlled_loopback_reachability_is_a_residual_risk`: skipped neste host; nunca contata Internet ou listeners do usuário |
| Limites de memória e CPU | `test_rlimit_address_space_is_not_resident_memory_or_tree_containment`, `test_rlimit_cpu_is_per_process_not_cpu_rate_or_tree_quota` |
| Artefato parcial e restart | `test_partial_artifact_remains_staging_after_crash_and_restart`, `test_real_artifact_reconciliation_after_worker_crash_uses_existing_contract` |

As métricas registram 7 startups por transporte e 12 falhas/cargas adicionais,
mais 128 frames por transporte para throughput. `clean_exit` significa que o
transporte terminou sem erro observado, **não** que um job foi validado ou um
artefato ficou publicável. `stdout_bytes`/`stderr_bytes` são contadores saturados,
não armazenamento de saída. `max_frame_buffer` mede bytes realmente lidos por
frame, não memória RSS. O coletor não exporta IDs ou corpos de IPC.

O sandbox bloqueou socket AF_INET, `getsockopt` e `setsockopt`. Socketpair, pipes,
processos, sinais, fork, pidfds e rlimits foram exercitados realmente. A restrição
de sockets é do host e não pode ser anunciada como negação da baseline. Não há
medição de teto de buffer do kernel, bundle, RSS/CPU da árvore, GPU, setsid hostil,
worker parado por SIGSTOP ou morte simultânea de pai/worker. Uma repetição fora
do sandbox é necessária para concluir a observação de loopback.

A reconciliação usa o serviço real #137 com ports sintéticos; não substitui os
testes de integração SQLite/payload store. A suíte completa precisa de um host
cuja proteção de ancestrais de paths aceite o root temporário. Não relaxar
`unsafe_path` para fazer os testes passarem.

## Execução e revisão adversarial registradas

A validação focada final teve 182 testes passando e um skip de loopback
(56 passando e um skip no módulo do spike, além de arquitetura e links).
Tempos de casos inteiros, incluindo setup/cleanup, observados no XML do pytest:

| Caso | socketpair | pipes |
| --- | ---: | ---: |
| Bloqueio real por backpressure e retomada do consumo | 281 ms | 283 ms |
| Forçar término de worker, filho e neto | 282 ms | 286 ms |
| Crash do pai, EOF e reap de worker, filho e neto | 53 ms | 50 ms |

O probe RLIMIT_CPU terminou por SIGXCPU em um caso de 1186 ms; a reconciliação
do serviço real com ports sintéticos levou 32 ms. Esses números incluem overhead
do harness, não são promessa de latência nem substituem repetir os testes.

Foram mutadas individualmente 13 regras: teto do frame, peer, sequência,
ambiente, fechamento de fds, quota de saída, JSON canônico, fila, sessão
inutilizada por erro, resultado obrigatório, allowlist, schema de comando e
cleanup do grupo. A mutação de JSON canônico sobreviveu inicialmente; envelopes
válidos com whitespace/duplicatas/NaN/Infinity fortaleceram a cobertura. Na rodada
final todas as 13 fizeram testes falhar, e todas foram revertidas. A proteção
de emergência do harness matou e recolheu os descendentes mesmo
quando a limpeza do grupo estava deliberadamente quebrada.
