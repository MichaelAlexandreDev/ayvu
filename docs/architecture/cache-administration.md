# Administração do cache e seus namespaces

`ayvu.cache_admin` define contratos independentes de interface e de armazenamento
para inspecionar, pré-visualizar e executar limpeza, exportar e importar o cache
standalone. A referência ao cache e as referências aos arquivos são opacas nas
representações; os resultados não contêm textos de entradas. O resumo identifica
o adaptador e o layout selecionados; no caminho legado, eles são `legacy_v1` e
`sqlite_translations`, sem declarar uma versão de esquema que não existe no banco.

A limpeza exige filtros explícitos ou `all_entries=True`. A prévia produz uma
seleção imutável com contagem e digest. O repositório deve comparar ambos sob o
bloqueio de escrita e rejeitar uma seleção desatualizada antes de remover dados.
A confirmação pertence à interface que chama o serviço. A contagem de execução
é devolvida somente após o commit.

A inspeção de cache ausente representa ausência por `exists=False`; a abertura
para exportação deve reportar ausência sem criar cache ou diretório. A exportação
é uma operação explícita que contém os textos originais e traduzidos. O arquivo
é sensível. A publicação pertence à porta de arquivos e deve usar arquivo
temporário restrito no mesmo diretório, substituição atômica e limpeza em falha
ou cancelamento.

A importação valida integralmente o JSON v1 e os hashes antes da mutação. O
repositório aplica conflitos segundo `replace` em uma única transação. Os limites
são 512 MiB por arquivo, 1.000.000 de entradas, 1.000.000 caracteres por texto,
256 caracteres por campo de metadados, profundidade de estrutura 16 e 20 marcadores
estruturais por entrada. Um limite menor recusaria caches reais: 90 mil frases já
passam de 64 MiB, e um export antigo desse tamanho precisa continuar importável. Os limites de arquivo devem ser aplicados durante a
leitura, não apenas depois de carregar o conteúdo. Erros retornam família e
código limitados, sem mensagens de exceções externas.

Os pontos de cancelamento declarados em `SAFE_POINTS` cobrem leitura, intervalo
entre entradas, início de mutação, commit e publicação. Os adaptadores chamam
`checkpoint` nos limites correspondentes; a transação ou publicação deve ser
abortada antes de confirmar quando o cancelamento é observado. Progresso contém
apenas fase e contagens. O contrato não cria uma API para navegar entradas nem
muda a identidade ou formato do cache v1.

## Compatibilidade local

- Um arquivo vazio, ou um SQLite sem a tabela `translations`, é um cache sem entradas e não é alterado. Só um arquivo que
  não é SQLite resulta em `corrupt_cache`. A limpeza e sua prévia não aplicam os limites de intercâmbio, porque leem uma
  linha por vez e são o caminho para remover uma linha grande demais.
- O SQLite espera até 30 s por um lock, como `TranslationCache`, porque uma tradução em andamento segura o lock a cada commit.
- O arquivo temporário do export é restrito enquanto é escrito. Publicado, ele segue o umask, ou mantém o modo do arquivo
  substituído. Em um sistema sem hard links, como pendrive FAT ou exFAT, a publicação sem `--overwrite` usa criação exclusiva
  seguida de substituição: continua sem poder sobrescrever, mas um arquivo vazio fica visível por um instante.
- Em uma medição histórica do formato v1, exportação e importação de 150 mil entradas levaram 6,5 s e 10,4 s. Esses números não medem o formato v2.

## Administração de namespaces v2

Sem opções de namespace, os comandos mantêm a administração legada v1 e sua barreira de compatibilidade. Para escolher um namespace, use `--namespace legacy-v1` ou `--namespace v2 --scope-kind project --scope-id PROJETO --project-id PROJETO`. Os tipos v2 também incluem `local_user` e `work`; ao informá-los, o operador autoriza explicitamente o escopo mais amplo para o projeto indicado. Identificadores de projeto devem coincidir para um escopo `project`.

`inspect` e `export` aceitam `--all` para selecionar todos os namespaces; `clean --namespace all --all` seleciona todos para remoção e continua exigindo `--yes` ou `--dry-run`. O antigo `clean --all`, sem namespace, permanece limitado ao comportamento legado. Filtros `--source` e `--target` se aplicam somente a entradas legadas; seleções que incluem entradas v2 não aceitam esses filtros. `--before` também seleciona entradas v2 pela comparação lexical do timestamp canônico. `--all` na limpeza não pode ser combinado com filtros.

A importação v2 exige `--scope-map ARQUIVO`, um JSON externo ao arquivo exportado, com uma lista fechada de correspondências. Por exemplo:

```json
[
  {"source": {"kind": "v2", "scope_kind": "project", "scope_id": "00000000-0000-0000-0000-000000004321"},
   "destination": {"kind": "v2", "project_id": "00000000-0000-0000-0000-000000004322", "scope_kind": "project", "scope_id": "00000000-0000-0000-0000-000000004322"}},
  {"source": {"kind": "legacy-v1"}, "destination": {"kind": "legacy-v1"}}
]
```

Cada origem deve estar mapeada explicitamente; as descrições da origem não concedem autorização ao destino. Confira a previsão e execute com o mesmo mapa e cache de destino:

```bash
uv run ayvu cache import export.json --cache destino.sqlite --scope-map mapa.json --dry-run
uv run ayvu cache import export.json --cache destino.sqlite --scope-map mapa.json
```

O segundo comando gera uma nova previsão e pede confirmação, ou aceita `--yes`. `--replace` deve ser informado para substituir conflitos. `--all` não é permitido na importação. A importação v1 sem mapa mantém o comportamento anterior e não aceita `--dry-run` nem `--yes`.

O envelope v2 limita entrada e saída a 1 GiB, 5 milhões de entradas, 10 mil seções ou mapeamentos, mapa externo de 1 MiB, referências UTF-8 de 1024 bytes e profundidade 8. Uma seção legada mantém o limite de 1 milhão de entradas v1. O contrato de linha de DESKTOP-123 permanece inalterado: identidade, metadados e proveniência têm limites separados de 64 KiB; fonte e valor, de 1 MiB; timestamp, de exatamente 27 bytes ASCII no formato UTC `YYYY-MM-DDTHH:MM:SS.ffffffZ`. Exportações, mapas, arquivos temporários de previsão (`.ayvu-cache-import-*.sqlite`) e arquivos de recuperação podem conter dados sensíveis: proteja-os como o cache e remova-os quando não forem mais necessários. A CLI descarta o arquivo temporário após `--dry-run`, recusa ou falha; após uma importação concluída ele é mantido como registro de auditoria. A CLI informa a versão JSON realmente emitida quando o namespace é selecionado explicitamente.


## Formato de intercâmbio JSON v2

O objeto de topo é fechado e contém `version` com valor inteiro `2`, seguido de `namespaces`, uma lista de seções. Uma seção também é fechada e possui uma das formas:

- Legada: `kind` igual a `legacy-v1` e `entries`. Cada entrada contém exatamente `source_lang`, `target_lang`, `original_text_hash`, `original_text`, `translated_text` e `created_at`.
- V2: `kind` igual a `v2`, `scope_kind`, `scope_id` e `entries`. Cada entrada contém exatamente `value_kind`, `semantic_fingerprint`, `source_digest`, `canonical_identity`, `source_text`, `value`, `metadata`, `provenance`, `created_at`, `provenance_digest` e `entry_digest`. `canonical_identity`, `metadata` e `provenance` são strings com os bytes UTF-8 originais de documentos JSON; preservam-se os bytes, inclusive espaços, ao reconstruir as linhas.

A ordem é obrigatória para o intercâmbio mapeado: `version` precede `namespaces` no topo, e todos os campos descritores precedem `entries` em cada seção. Na importação mapeada de JSON v1 separado, `version` precede `entries`. Os campos das entradas podem chegar em qualquer ordem, mas não admitem campos extras ou duplicados. A saída usa ordem determinística: campos da linha na ordem do esquema, seção legada primeiro, seções v2 ordenadas por `scope_kind` e `scope_id` e linhas ordenadas pela chave de cada namespace. A CLI mostra a versão realmente produzida nas exportações com seleção explícita.

A importação v1 sem mapa mantém a aceitação antiga de ordem livre, campos desconhecidos ignorados, BOM UTF-8 e JSON UTF-16/32, agora com transcodificação incremental. A validação inteira precede a transação; duplicatas de chaves JSON continuam recusadas. A política antiga de entradas repetidas e `--replace` também permanece. Esse caminho usa staging privado, com índice de digests das chaves desconhecidas em disco, teto de dezesseis vezes o limite v1 de entrada mais 1 MiB e remoção após a operação. A composição de produção não usa as antigas portas que carregam o payload inteiro. O envelope v2 continua estritamente UTF-8.

O limite de 1 MiB do mapa também se aplica à representação canônica interna: uma lista ordenada de pares de referências de origem e destino, serializada como JSON UTF-8 compacto. Cada referência é uma lista `['legacy-v1']` ou `['v2', tipo_de_escopo, id_de_escopo]`; essa notação descreve a representação interna, enquanto o arquivo aceito pela CLI usa os objetos do exemplo acima e tem seu próprio limite de 1 MiB bruto. O mapa deve cobrir exatamente as seções presentes, sem omissões nem origens extras. Um arquivo com somente um escopo exige somente a correspondência desse escopo; inclua a linha legada do exemplo apenas se o arquivo contiver a seção legada. Se o conjunto ultrapassar algum teto, exporte seleções explícitas menores; nenhum limite é elevado automaticamente.

A importação mapeada exige um banco de destino já existente; destinos v1 aceitam somente `legacy-v1 → legacy-v1`. Ela não prepara nem migra o destino automaticamente. O modo de journal do banco deve ser `delete`, `truncate` ou `persist`. Os modos `wal`, `memory` e `off` são recusados com `unsupported_audit_mode` antes de criar a previsão. Não altere o modo automaticamente para contornar a recusa: prepare outro destino v2 em armazenamento suportado pela API e consulte o [protocolo de migração e recuperação de DESKTOP-123](standalone-cache-v2.md).

O chamador sem CLI usa `preview_import`, entrega a mesma previsão a `execute_import` e chama `discard_import` quando abandona uma previsão. A revisão é opaca e válida somente no processo que a emitiu. Após reiniciar, gere outra previsão. O staging SQLite fica junto ao cache, com modo `0600` e teto de quatro vezes o limite de entrada mais 1 MiB; não integra a tabela consultada pelo runtime. A transação confirma cache e auditoria em conjunto. A auditoria registra o mapa e a política separadamente das entradas, sem alterar proveniência ou data e sem representar aprovação linguística ou consentimento. Após uma interrupção, preserve os arquivos para análise antes de descartar previsões abandonadas; a linha `committed` na tabela `audit` identifica uma auditoria confirmada. Não se afirma eliminação segura nem proteção contra processos arbitrários que ignorem o protocolo cooperativo.

A previsão faz duas passagens de parsing em lotes: a primeira confere o envelope e a cobertura completa do mapa antes de criar o staging; a segunda prepara as linhas. Ambas verificam o digest dos bytes efetivamente consumidos, além de revalidar o arquivo, para recusar conteúdo alterado durante a leitura mesmo quando ele é restaurado antes da checagem final.

Na exportação explícita, uma substituição usa também uma cópia temporária restrita e incremental do destino anterior para restaurá-lo se a sincronização do diretório falhar após o rename. Essa cópia respeita o limite de saída configurado; um destino anterior maior exige outro caminho de saída. Arquivos novos seguem o umask; substituições preservam as permissões anteriores. Nenhum resultado parcial é anunciado como sucesso.
