# Administração do cache legado

`ayvu.cache_admin` define contratos independentes de interface e de armazenamento
para inspecionar, pré-visualizar e executar limpeza, exportar e importar o cache
standalone. A referência ao cache e as referências aos arquivos são opacas nas
representações; os resultados não contêm textos de entradas. O resumo identifica
o adaptador `legacy_v1` e o layout `sqlite_translations`, sem declarar uma versão
de esquema que não existe no banco.

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
- O export e o import ficaram cerca de 3 e 4 vezes mais lentos, por causa da validação: 150 mil entradas levam 6,5 s e 10,4 s.
