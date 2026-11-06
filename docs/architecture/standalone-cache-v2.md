# Cache SQLite standalone v2

O módulo local `cache_schema.py` é independente do executor de projetos. A fronteira
`prepare_cache(path, authorized_directory=None, checkpoint=None)` classifica e prepara
um arquivo antes de iniciar workers ou abrir o repositório. `SQLiteExactCache`
implementa `ExactCachePort` para o caminho rico. A CLI continua usando o cache v1
legado: ela não fabrica identidade de provedor, modelo ou pipeline para produzir
acertos v2. Um cache novo preparado nessa fronteira nasce v2.

A versão é registrada no ledger `cache_schema`; entradas exatas ficam em
`cache_entries_v2`, com índice `cache_entries_v2_compatible` para resolução
compatível. Na migração transacional, a tabela v1 `translations` é
preservada no namespace `legacy-v1`: suas linhas não são copiadas para entradas
exatas, pois não têm identidade semântica suficiente. Uma entrada exata requer
escopo autorizado, tipo de valor, fingerprint semântico e digest do conteúdo de
origem. Escritas concorrentes usam `put_if_absent`; conflito de valor ou
proveniência é reportado sem substituir a entrada existente.
Uma repetição idêntica de valor, metadados e proveniência é idempotente. O
repositório atribui `created_at` somente à inserção vencedora; o chamador não
fornece esse timestamp. `provenance_digest` referencia a projeção operacional
de proveniência, sem inventar um registro de aprovação ou consentimento.

## Limites e dados

`CacheEntryV2Limits` é normativo: payload canônico até 64 KiB, origem até 1 MiB,
valor até 1 MiB, metadados até 64 KiB e proveniência até 64 KiB. `created_at`
tem exatamente 27 bytes ASCII no formato UTC `YYYY-MM-DDTHH:MM:SS.ffffffZ`.
Os outros limites são em bytes UTF-8, separados entre si. Uma entrada excessiva
recebe `not_cacheable`, sem truncar campos nem impedir a tradução normal.
Os metadados aceitam flags e contagens limitadas, sem texto livre ou campos de
credenciais, endpoint, prompt ou contexto. Textos, metadados, proveniência, identidades e hashes
são dados operacionais sensíveis. Sua presença não indica aprovação de tradução
nem consentimento para uso em corpus.

## Bloqueio, identidade e backup

O protocolo cooperativo usa `flock` POSIX exclusivo durante `prepare_cache` e
compartilhado durante operações participantes. Também é obtido o bloqueio
exclusivo mais forte do SQLite antes do backup e do DDL. O protocolo coordena
participantes; não protege contra processos arbitrários que o ignorem. A identidade
do arquivo de lock também é verificada após obtê-lo e novamente antes do backup
e da promoção, para detectar sua substituição.

O arquivo deve estar diretamente em `authorized_directory`, quando informado. São
rejeitados aliases por symlink ou hardlink, arquivos não regulares e caminhos fora
do diretório autorizado. A identidade do arquivo é revalidada após o bloqueio da
aplicação, após abrir o SQLite, antes do backup e antes da promoção. Sem verificações
confiáveis de identidade e suporte ao protocolo, a operação falha de forma fechada;
a migração no lugar é recusada sem alterar o v1 nem criar o diretório pai. Para
um novo v2 vazio é necessário outro destino em sistema de arquivos e plataforma
POSIX suportados. Por exemplo, em um diretório local novo e suportado:

```python
from pathlib import Path
from ayvu.cache_repository import SQLiteExactCache

cache = SQLiteExactCache(Path("/caminho/local/suportado/novo-v2.sqlite"))
```

A origem v1 permanece intacta; nenhuma linha é transferida ou promovida a entrada
exata. Se não houver sistema de arquivos suportado, v2 fica indisponível: conserve
o v1 para leitura ou para a execução legada existente.

O snapshot usa `VACUUM INTO` do SQLite e flush, considerando inclusive WAL;
nunca se copia apenas o arquivo principal. O backup é verificado antes do DDL e
retido após a migração. Banco, backup e arquivos auxiliares podem conter conteúdo
operacional sensível; não os exponha em logs. Arquivos de migração e backup são restritos a `0600`, e
diretórios criados para eles a `0700`. Não se afirma eliminação segura de dados.

## Fases duráveis e recuperação

A classificação local distingue `absent`, `empty`, `v1`, `v2`, `partial`,
`corrupt` e `foreign`. Um marcador `.migration` ou `.migration-next` torna a
classificação `partial`, inclusive quando há journal pendente após queda. Isso
permite que a preparação obtenha locks e deixe o SQLite recuperar o journal.

O campo `phase` no JSON `.migration` por banco registra as fases abaixo. Cada
checkpoint ocorre após a gravação durável do marcador correspondente.
O callback `checkpoint(phase)` deve lançar uma exceção para solicitar
cancelamento; um retorno normal permite prosseguir. Cancelar em `committed`
preserva o v2 confirmado e deixa o marcador para reconciliação.

| Fase | Predecessor | Cancelamento ou reabertura |
| --- | --- | --- |
| `classified` | Nenhum | Banco continua v1; ao reabrir, verifica a origem, descarta um snapshot `.v1-backup.sqlite.partial` interrompido e cria ou verifica o backup. |
| `backup-verified` | `classified` | Banco continua v1; reabertura verifica novamente o backup e prossegue. |
| `migration-pending` | `backup-verified` | Banco continua v1; reabertura verifica backup e inicia o DDL transacional. |
| `v2-built` | `migration-pending` | Antes do commit, rollback deixa v1; reabertura reconstrói o v2 na transação. |
| `integrity-verified` | `v2-built` | Antes do commit, rollback deixa v1 e reabertura reconstrói; após commit, valida v2 e reconcilia o marcador. |
| `committed` | `integrity-verified` | Banco v2 confirmado; reabertura verifica backup e legado, reconcilia o marcador e não recoloca dados. |

Na recuperação antes do commit, o DDL é executado novamente dentro de uma
transação; checkpoints já registrados são pulados, sem retroceder o marcador. Locks
e transação são gates transitórios, não fases duráveis. O backup é conservado e
nunca sobrescreve a origem.

`.migration-next` indica publicação de marcador interrompida e causa
`cache_recovery_required`: preserve todos os arquivos para análise manual. Um
backup ou snapshot parcial preexistente sem registro causa `cache_backup_exists`; backup ausente,
incoerente ou alterado pode causar `cache_backup_invalid`. Marcador inválido,
incompatível ou sem identidade verificável causa `cache_recovery_required`.
Guarde cópias de todos os arquivos, incluindo banco, WAL, journal, marcadores e
backup, e consulte alguém capaz de verificar a integridade e a identidade da origem
e do backup. Não apague marcadores nem substitua a única cópia às cegas. Após
resolver a condição, pode-se tentar novamente com a origem bloqueada, ou escolher
um novo destino v2 vazio em armazenamento suportado.

## Administração

A administração aceita seleção explícita de namespace, com previsão e mapa externo para importação v2. Sem seleção, a barreira legada continua ativa. Veja [administração do cache](cache-administration.md).

O repositório não infere referências de execução a partir de textos ou hashes.
Sem uma vinculação local autenticada de execução, `resolve_pinned` devolve
ausência; cache-only continua sem descoberta, provedor ou transporte. A resolução
compatível usa somente identidades v2 locais autorizadas.
