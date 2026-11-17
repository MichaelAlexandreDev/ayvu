# HTTP network policy

> **Translation in progress:** this page still holds the original Portuguese text of the former
> README, moved here unchanged. An English version replaces it shortly.

Use `--network-policy none|loopback` para escolher a política HTTP desta execução.
Sem a opção, o padrão visível é `loopback`: somente **HTTP**, com `localhost`,
`localhost.`, IPv4 decimal em `127.0.0.0/8` ou IPv6 `[::1]` (também na forma
completa), porta 1–65535, ou 80 se omitida. Outros nomes, LAN, VPN, IP privado,
público e HTTPS são recusados; uma URL antiga assim precisa ser substituída
explicitamente por um endpoint HTTP loopback. Não há migração automática.

```bash
uv run ayvu languages --network-policy loopback --url http://127.0.0.1:5000
uv run ayvu translate livro.epub --network-policy none --cache-only
uv run ayvu --network-policy none --preview livro.epub
uv run ayvu resume --network-policy loopback
uv run ayvu --network-policy none
```

`none` bloqueia o HTTP do Ayvu **antes de DNS, sockets, criação de sessão e
construção do payload**. Assim, tradução online, idiomas, teste e preflight são
recusados; `--dry-run` e `--cache-only` continuam disponíveis. A opção na raiz
vale para preview e menus, incluindo a escolha guiada de idiomas e a retomada
oferecida; a opção de um subcomando prevalece para esse pedido. Ela nunca é salva.
Ambas as políticas ignoram proxies/credenciais do ambiente e `.netrc`, recusam
credenciais na URL e qualquer redirect, mesmo local. HTTPS, proxies, credenciais
explícitas e destinos não loopback dependem de contratos futuros.

O leitor EPUB aberto pela biblioteca é **outro processo, fora da garantia
offline**, e recebe um aviso explícito antes de abrir. `none` não contém esse
leitor; um servidor local também pode ter sua própria rede e retenção.
Veja o [contrato da política HTTP](../architecture/cross-cutting/network-policy.md).
