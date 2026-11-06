# Perfis de tradução versionados do projeto

O contrato da issue #163 usa os donos das dependências #134 (manifesto/CAS),
#152 (preferências v1), #153 (identidade semântica do cache bruto) e #157
(referência/revisão de glossário). Nenhuma migração acontece durante leitura,
abertura de projeto ou tradução. O pipeline de produção ainda não consome os
snapshots nem as projeções descritas aqui.

## Donos e esquema fechado

`project_profiles.py` contém valores imutáveis e a política de resolução v1;
`project_profile_service.py` contém histórico, portas e casos de uso. Ambos
são contratos da aplicação, sem JSON, filesystem, SQLite, provedor ou interface.
A referência do glossário continua pertencendo a `glossary_catalog`; não é
substituída por UUID, hash de caminho ou novo registro de identidade.

O esquema v1 exige exatamente `version`, `target_language`, `glossary`, `style`.
Os três últimos aceitam null. Um glossário presente exige exatamente `reference`
e `revision`. O idioma preserva a validação e o limite de 35 caracteres da #152;
o estilo aceita null ou `neutral`, `technical`, `literary`, `academic`. A referência
gerenciada usa a validação e os limites da #157; a revisão é o token exato de
64 dígitos hexadecimais retornado por ela. Não existe campo de caminho, nome de
perfil, extensão ou configuração do provedor. Campos e versões desconhecidos,
tipos incorretos e chaves duplicadas são recusados integralmente.

Referências de projeto reutilizam a identidade UUID da #134. Referências de
perfil são tokens `profile:` opacos aceitos pela #134, emitidos de aleatoriedade
independente de conteúdo, nome, processo e relógio. Uma revisão é o SHA-256 da
projeção canônica versionada do conteúdo. O hash inclui o estilo inerte para
registrar mudanças de metadados, sem torná-las semântica do provedor.

A serialização canônica interna tem tags: null `n`, booleanos `t`/`f`, inteiro
`i<decimal>;`, string UTF-8 `s<bytes>:<conteúdo>`, sequência
`l<quantidade>:<itens>`, mapa `m<sequência de pares ordenados por chave>`.
As chaves do esquema são ASCII fixas; o conteúdo legado não é renormalizado.
O adaptador JSON usa chaves ordenadas, null explícito e rejeita constantes e
duplicatas em todos os níveis. Nenhum JSON entra nas portas.

Audiência, tom, formalidade, termos protegidos, voz de obra/personagem, contexto
de capítulo, restrições de formato e ativação do estilo são adições futuras de
esquema/política versionados. Não são inferidos do nome, do livro ou do glossário.

## Migração explícita e janela de compatibilidade

Durante toda a janela v1, `--profile`, `--target`, `--glossary` e o modo guiado
mantêm o comportamento existente. Não há comando novo de migração. O consumidor
legado continua disponível nesta versão, sem data de remoção; encerrar a janela
exige uma mudança futura explícita e documentada. O consumidor
da aplicação deve fornecer a raiz de processamento e a raiz gerenciada pela
composição, o projeto existente, sua revisão esperada e a referência de destino.
Exemplo para um chamador autorizado, com repositório de preferências já composto:

```python
from ayvu.project_profile_composition import project_profile_service
from ayvu.project_profiles import ProfileReference, ProjectReference

service = project_profile_service(processing_root, managed_glossary_root)
destination = ProfileReference.new()  # conserve este token para repetir a operação
result = service.migrate(preferences_repository, legacy_name,
    ProjectReference(manifest.project_id), manifest.revision, destination)
print(result.diagnostic())  # outcome tipado, sem dados privados
```

O chamador deve tratar `migrated`, `already_migrated`, `conflict`,
`unresolved_glossary`, `unsupported_version`, `invalid_profile`, `cancelled`,
`persistence`, `not_found`, `invalid_plan`, `limit`. Só os resultados de sucesso
contêm estado/snapshot publicado. Não descarte recusas ou suponha sucesso porque
o perfil foi validado. Uma origem com campos desconhecidos/read-only é recusada.
Não há avisos retornados e ignorados por uma interface.

O nome seleciona uma entrada validada da #152, sem participar da identidade.
O recibo persistido contém destino e revisão inicial, sem nome ou caminho.
Repetir a mesma migração para o mesmo destino devolve `already_migrated`, inclusive
após uma edição posterior no projeto: não desfaz essa edição. Conteúdo divergente
para esse destino retorna conflito. Um destino novo requer a revisão atual do
projeto e uma migração explícita distinta. A origem global não é salva nem apagada.

Caminhos relativos de glossário usam somente a raiz gerenciada injetada.
Caminhos absolutos são aceitos apenas quando o arquivo canonicalizado é filho
direto dessa raiz. O adaptador consulta #157, reutiliza suas regras de leitura e
seu lock e fixa a revisão exata. Ausência, caminho externo, aliases de caixa/NFC,
link simbólico, múltiplos hard links, arquivo inválido ou revisão obsoleta bloqueiam
a migração com motivo fechado (`missing`, `external`, `ambiguous`, `unsafe`,
`stale`, `io`) e sem atualizar o projeto. Resolva o problema no catálogo e repita
explicitamente. Um glossário externo continua utilizável pelo caminho legado da
CLI; ele não recebe referência gerenciada fictícia.

O alvo null preserva a intenção de default. Na resolução, a ordem é override
explícito não vazio, alvo do perfil, default do chamador (padrão `pt`). O fluxo
guiado pode fornecer seu default global, como já faz hoje. Overrides ficam no
snapshot, nunca no perfil migrado. `glossary_present=False` herda o glossário;
`True` seleciona a referência fornecida e null explícito desativa a seleção.
Valor presente com `glossary_present=False` é ambíguo e recusado.

## Resolução, fatos confiáveis e efeitos

`service.write` cria/atualiza um perfil com as revisões esperadas de projeto e
perfil. `service.resolve` exige essas revisões, `PlanFacts` e `ProfileOverrides`;
`repository.read(project)` permite ler os perfis, revisões e snapshots históricos.
Resolva o alvo e a seleção antes de fornecer a projeção exata do pedido planejado.
Não construa um pedido ao provedor a partir deste contrato nesta issue.

Um snapshot fixa projeto/revisão de entrada, referência/revisão/conteúdo do perfil,
overrides explícitos, default, glossário/revisão, política v1, fatos do plano e
classificação completa. Mesmos valores e ordem distinta dos mapas produzem os
mesmos bytes e identidade entre processos. Publicar o snapshot avança a revisão
do manifesto; o snapshot registra a revisão de entrada anterior à publicação.
Repetir sua resolução exata imediatamente retorna o mesmo snapshot, revalidando
glossários e a revisão atual do perfil. Mudanças posteriores do manifesto exigem
nova resolução ou leitura explícita do histórico. Ler histórico não exige
que o glossário atual ainda tenha a revisão histórica; criar um snapshot novo
exige revalidar todas as referências vinculadas, inclusive a seleção sobrescrita.

Somente a política v1 deriva as classes; o chamador fornece fatos tipados do plano
negociado: estágio, projeção exata do pedido/capacidade e estágio do glossário.
Esses fatos são fornecidos pela composição confiável, não por documento, resposta
do provedor ou metadados do adaptador. Não há campo para escolher a classe.
Pedido incompatível com alvo, suporte desconhecido de terminologia, política
ausente e classificação incompleta falham antes da publicação.

| Dimensão | Plano | Classe | Efeito |
| --- | --- | --- | --- |
| Idioma alvo | translate/replay | raw-provider-affecting | Contribui para a identidade bruta |
| Glossário | post_cache | downstream-only | Muda replay/invalidação downstream; mantém contribuição bruta |
| Glossário | provider, com suporte negociado e seleção/política exatas | raw-provider-affecting | Referência, revisão, capacidade e política contribuem antes de lookup futuro |
| Glossário | none | inert | Seleção registrada, sem contribuição efetiva |
| Estilo legado | todos | inert | Revisão de metadados e snapshot mudam; pedido, saída bruta e cache não |
| Estágio de execução | todos | operational-only | Snapshot muda; contribuição semântica bruta não |
| Alvo/glossário | inspect | inert | Nenhum pedido ou lookup autorizado por esse plano |

`raw_semantic_contribution()` fornece argumentos limitados para
`cache_identity.OpaqueSemanticRevision` da #153. Sua referência `profile_effect:`
é resolvida por `state.raw_projection(reference)` a evidência bruta durável
presente nos snapshots. Ela depende só da partição bruta e versão da política;
nunca use o hash do snapshot completo no cache. A identidade geral de provedor,
rota e demais políticas continua pertencendo à #153, não é substituída aqui.
`effects.downstream_projection()` entrega a partição separada. Nomes, caminhos,
texto de glossário, estilo, labels, overrides inertes e estágio operacional não
entram na contribuição bruta. A terminologia enviada usa contribuição opaca
derivada da referência/revisão/política efetivas, sem expor nome ou conteúdo.

Não existe alteração de chave/schema do cache, migração de linhas, lookup ou
reconstrução automática. O cache continua armazenando tradução bruta; glossário
pós-cache deve ser reaplicado em futuros consumidores. Proveniência futura deve
guardar a referência histórica do snapshot, sem reabrir o ponteiro atual e
atribuir retroativamente uma revisão nova a um artefato antigo. Ativar estilo
exige nova política/esquema e contribuição raw explícita, sem reinterpretar v1.

## Publicação, retenção e privacidade

`project_profile_store` usa o mesmo lock de workspace e CAS da #134. O manifesto
continua v1: `x-translation-profiles` aponta para um registro local imutável
`profiles:<digest>`. A identidade imutável original do projeto não é alterada.
O registro guarda heads, revisões anteriores, recibos e snapshots; uma atualização
é append-only. Limites: 64 perfis/recibos, 1.024 revisões e 1.024 snapshots por
projeto, 8 MiB por registro serializado. Exaustão retorna `limit`, sem truncamento.

Arquivos privados de estado são preparados e sincronizados antes da substituição
atômica do manifesto, com modo 0600 e diretório 0700. A substituição preserva um
modo anterior mais restrito do manifesto. Um cancelamento antes da publicação,
revisão obsoleta ou erro mantém o manifesto anterior. Erro de sync após rename
restaura os bytes anteriores e retorna falha; como na #157, filesystem com falha
persistente não permite prometer durabilidade do rollback. Processos que ignoram
locks têm a mesma janela residual POSIX da #157 após a última revalidação.
Não existe ponto de cancelamento entre publicação bem-sucedida e retorno.

Registros preparados que nunca receberam ponteiro podem permanecer órfãos após
falha; eles não são lidos/publicados como sucesso. Esta versão não coleta lixo nem
remove revisões/snapshots históricos. Referências de jobs/artefatos futuros exigem
retenção, não mutação do histórico. Todos os leitores usam o ponteiro publicado;
leitura de um snapshot desconhecido falha com `not_found`.

Persistência local contém as referências/revisões necessárias, mas não caminhos
legados, conteúdo do glossário/livro, credenciais, endpoint ou nome do perfil.
Repr, exceções normalizadas e `diagnostic()` omitem identificadores, revisões e
fingerprints. Mapas/bytes canônicos são payload privado de persistência/contrato,
não diagnóstico ou bundle de suporte. Apresentação autorizada pode mostrar o nome
legado ou local deliberadamente; não os propague para eventos ou proveniência
padrão. Estes casos de uso não emitem eventos nem efetuam chamadas de tradução.
