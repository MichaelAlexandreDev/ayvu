# Glossary

> **Translation in progress:** this page still holds the original Portuguese text of the former
> README, moved here unchanged. An English version replaces it shortly.

## Glossário

O glossário aceita o formato simples de pares de termos, mantido por compatibilidade:

```json
{
  "Game Loop": "loop de jogo",
  "Observer": "Observer"
}
```

Também aceita regras explícitas por termo:

```json
{
  "Game Loop": {
    "rule": "translate",
    "translation": "loop de jogo",
    "required": true
  },
  "Observer": {
    "rule": "preserve",
    "required": true
  },
  "AntiPattern": {
    "rule": "forbidden"
  }
}
```

Use `translate` para definir uma tradução preferida e `preserve` para manter um termo padronizado no texto final. Use `required: true` em regras `translate` ou `preserve` quando o termo esperado deve aparecer na saída traduzida. A regra `forbidden` marca termos que não devem aparecer na saída.

Quando um glossário é usado, o relatório final conta quantas aplicações de `translate` e `preserve` ocorreram, inclusive em textos vindos do cache. O relatório também avisa termos obrigatórios ausentes e termos proibidos encontrados na saída.

Use `Glossaries` no modo comum para criar glossários guiados ou `glossary.example.json`
como base para o modo desenvolvedor. O arquivo `glossary.json` local e os glossários privados
são ignorados pelo Git para evitar versionar preferências pessoais ou conteúdo privado.

Hoje cada tradução usa no máximo um glossário ativo: o arquivo escolhido em `--glossary`, o
glossário associado ao perfil, ou nenhum glossário. O Ayvu não aceita vários `--glossary` na
mesma execução. A decisão atual evita conflitos silenciosos entre arquivos, como um termo
marcado para tradução em um glossário e para preservação ou proibição em outro.

A direção planejada, se a necessidade crescer, é preferir glossários por regra em vez de
empilhamento genérico. Nesse modelo futuro, um arquivo poderia declarar no início que todos os
seus termos são `translate`, `preserve` ou `forbidden`, reduzindo repetição para quem mantém
glossários grandes. Mesmo nesse caso, a implementação deve definir antes regras claras de
prioridade, conflito e relatório.

Antes de enviar cada trecho ao tradutor, o Ayvu protege termos especiais como URLs, caminhos de arquivo, comandos de terminal, versões como `v1.2.0`, código inline entre crases, placeholders e identificadores técnicos simples. Esses termos são restaurados antes da aplicação do glossário e antes de salvar a tradução no cache.
