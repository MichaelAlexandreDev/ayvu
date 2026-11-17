# Configuration

> **Translation in progress:** this page still holds the original Portuguese text of the former
> README, moved here unchanged. An English version replaces it shortly.

## Configuração

O Ayvu define um formato inicial para preferências locais. No modo comum, o primeiro uso pergunta o
idioma padrão e salva a configuração. Depois, a opção `Settings` permite alterar idioma padrão,
pasta base dos livros, nomes das pastas das funcionalidades e app leitor de EPUB. O arquivo fica em:

```text
$XDG_CONFIG_HOME/ayvu/config.json
```

Quando `XDG_CONFIG_HOME` não estiver definido, o fallback é:

```text
~/.config/ayvu/config.json
```

Formato inicial:

```json
{
  "version": 1,
  "default_target_language": "pt",
  "books_dir": "~/Documentos/Livros",
  "folders": {
    "original": "Original",
    "translated": "Traduzidos",
    "preview": "Preview",
    "reports": "Relatorios",
    "processing": "Processando"
  },
  "reader_app": null,
  "profiles": {}
}
```

Perfis podem ser adicionados manualmente em `profiles`:

```json
{
  "profiles": {
    "technical": {
      "target_language": "pt",
      "glossary": "technical.json",
      "style": "technical"
    }
  }
}
```

`target_language` define o idioma de destino padrão quando `--target` não for informado.
`glossary` aceita caminho absoluto ou caminho relativo à pasta local de glossários. `style`
aceita `neutral`, `technical`, `literary` ou `academic`, mas no backend LibreTranslate atual
serve apenas como metadado do perfil; ele não muda o prompt nem o comportamento do tradutor.

O contrato de [perfis versionados do projeto](../architecture/modules/projects-profiles.md)
permite migração explícita por chamadores da aplicação. A CLI continua usando os perfis
globais v1 nesta janela de compatibilidade; não existe comando novo de migração nem
migração automática. A migração exige glossário do catálogo gerenciado e preserva o perfil global.

A precedência para campos cobertos por perfis de tradução é:

```text
argumentos da CLI > perfil selecionado > arquivo de configuração > padrões internos
```

Sem caminhos explícitos, a pasta base e os nomes de pastas configurados definem onde o Ayvu salva
previews, traduções, relatórios Markdown e estados de processamento, além das pastas usadas pela
biblioteca. O campo `reader_app` define o comando usado para abrir EPUBs pela biblioteca quando o
leitor padrão do sistema não for suficiente.

Glossários criados pelo modo comum ficam ao lado da configuração, em `glossaries/`, e podem ser
selecionados antes de iniciar uma tradução guiada.

A configuração continua no formato v1. Leituras não regravam o arquivo; alterações
usam gravação atômica e uma revisão para impedir que duas sessões sobrescrevam
mudanças uma da outra. Campos desconhecidos permitem leitura com aviso, mas
bloqueiam gravações até revisão explícita do arquivo, evitando perda de dados.
Se uma gravação falhar, o menu informa que os valores valem apenas para a sessão
atual. Em conflito de revisão, recarregue a configuração antes de tentar salvar
novamente. Comandos do leitor com opções de credenciais, como `--token` ou
`--password`, são recusados; a configuração não é um armazenamento de segredos.
Consulte o [contrato de preferências](../architecture/modules/preferences.md)
para limites, resultados de erro, compatibilidade e permissões.
Arquivos inválidos, futuros ou acima dos limites são preservados: guardar uma
cópia privada e corrigir explicitamente o arquivo é necessário antes de salvar
novamente. O menu não substitui esses arquivos pela configuração padrão.
