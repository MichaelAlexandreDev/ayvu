# Escopo de produto e taxonomia de capacidades

> **Status deste documento:** evidência de produto atualizada em 2026-09-03.
> Propostas, adiamentos e experimentos não são capacidades entregues nem
> decisões arquiteturais aceitas.

## Como ler esta taxonomia

Cada capacidade tem um único status:

| Status | Significado |
| --- | --- |
| **Existente** | Comportamento entregue e comprovado por documentação, código ou testes versionados. |
| **Adaptação de interface** | Capacidade atual oferecida por uma nova interface, sem outro núcleo de regras. |
| **Melhoria local-first** | Evolução planejada do fluxo local, ainda não entregue. |
| **Adiada** | Intenção reconhecida, bloqueada por contratos, evidência ou decisão anterior. |
| **Experimental** | Pesquisa ou prova limitada, sem promessa de fidelidade ou compatibilidade. |
| **Intencionalmente não suportada** | Comportamento incompatível com os limites do produto. |

As afirmações de estado atual são sustentadas pelo [README](../README.md), pela
[baseline de fluxos caracterizados](translation-workflow-migration-baseline.md),
pela [CLI](../src/ayvu/interfaces/cli/app.py) e pelos testes versionados. A issue
[#132](https://github.com/DevStarrk1137/ayvu/issues/132) fornece a
caracterização que migrações futuras devem preservar.

## Limites e termos de produto

O Ayvu é hoje uma CLI local para traduzir EPUBs fornecidos pelo próprio usuário
por meio de um serviço HTTP compatível com LibreTranslate. O original não é
sobrescrito; o resultado é sempre um artefato derivado, como EPUB, preview, CSV
de revisão, Markdown ou relatório.

| Termo ou invariante | Definição |
| --- | --- |
| **Conteúdo visível** | Texto destinado ao leitor; código, estilos, scripts, SVG, MathML e metadados técnicos não são texto comum. |
| **Preservação** | Manter estrutura, assets, links e membros não substituídos do EPUB no nível documentado e testado. |
| **Cache operacional** | Reuso local por texto e par de idiomas; não é aprovação humana, corpus ou consentimento para treino. |
| **Proveniência** | Ligação completa de resultado a fonte, configuração e execução; é uma melhoria planejada, não completa hoje. |
| **Decisão proposta** | Direção sujeita a aceite humano; sua menção não cria compromisso público. |

“Local-first” não significa que todo endpoint atual seja verificavelmente
offline. O usuário escolhe o endpoint HTTP; uma política de egress estrita é
trabalho futuro.

## Taxonomia

| Área | Capacidade | Status | Evidência ou direção pública |
| --- | --- | --- | --- |
| Tradução | Traduzir texto visível de XHTML/HTML em EPUB | **Existente** | [README](../README.md) e [baseline](translation-workflow-migration-baseline.md). |
| Tradução | Preview, capítulos selecionados, batch, cache-only e retomada | **Existente** | [Baseline](translation-workflow-migration-baseline.md) e [CLI](../src/ayvu/interfaces/cli/app.py). |
| Preservação | Original imutável, ZIP conservador, `mimetype` não comprimido e assets não substituídos | **Existente** | [Baseline](translation-workflow-migration-baseline.md). |
| Preservação | Garantia pixel-perfect para PDF, imagem, vídeo ou formatos não validados | **Intencionalmente não suportada** | O único vertical de produção é EPUB. |
| Formatos | EPUB | **Existente** | [README](../README.md). |
| Formatos | PDF/OCR, legendas, áudio, vídeo, quadrinhos e imagens | **Adiada** | Não há adaptador de produção nem promessa de round-trip. |
| Revisão | Exportar CSV e reconstruir novo EPUB revisado | **Existente** | [README](../README.md) e [baseline](translation-workflow-migration-baseline.md). |
| Revisão | Reaplicação estruturalmente segura de markup inline | **Melhoria local-first** | [#148](https://github.com/DevStarrk1137/ayvu/issues/148). |
| Projetos e jobs | Projeto durável, jobs por etapas, artefatos e proveniência completos | **Melhoria local-first** | [#134](https://github.com/DevStarrk1137/ayvu/issues/134), [#136](https://github.com/DevStarrk1137/ayvu/issues/136), [#137](https://github.com/DevStarrk1137/ayvu/issues/137) e [#138](https://github.com/DevStarrk1137/ayvu/issues/138). |
| Providers | Adaptador LibreTranslate com descoberta de idiomas e rota intermediária | **Existente** | [README](../README.md) e [baseline](translation-workflow-migration-baseline.md). |
| Providers | Registry, contratos versionados, capabilities e discovery compartilhado | **Melhoria local-first** | [#141](https://github.com/DevStarrk1137/ayvu/issues/141), [#103](https://github.com/DevStarrk1137/ayvu/issues/103) e [#128](https://github.com/DevStarrk1137/ayvu/issues/128). |
| Providers | Segundo backend de tradução | **Adiada** | [#100](https://github.com/DevStarrk1137/ayvu/issues/100) está deferred. |
| Memória e glossário | Glossário pós-cache e memória fuzzy opcional | **Existente** | [README](../README.md) e [baseline](translation-workflow-migration-baseline.md). |
| Memória e glossário | Memória aprovada, cache v2 e administração segura | **Melhoria local-first** | [#153](https://github.com/DevStarrk1137/ayvu/issues/153), [#154](https://github.com/DevStarrk1137/ayvu/issues/154), [#155](https://github.com/DevStarrk1137/ayvu/issues/155) e [#161](https://github.com/DevStarrk1137/ayvu/issues/161). |
| Interfaces | CLI com modos guiado e desenvolvedor | **Existente** | [README](../README.md) e [CLI](../src/ayvu/interfaces/cli/app.py). |
| Interfaces | Desktop, API local e MCP sobre os mesmos casos de uso | **Adaptação de interface** | [#123](https://github.com/DevStarrk1137/ayvu/issues/123); nenhuma dessas interfaces está entregue. |
| IA | Assistência de terminologia, qualidade, estilo ou layout com autoridade limitada | **Experimental** | [#166](https://github.com/DevStarrk1137/ayvu/issues/166); não há runtime de IA entregue. |
| Rede e privacidade | Endpoint de tradução escolhido explicitamente pelo usuário | **Existente** | [README](../README.md). |
| Rede e privacidade | Offline estrito, loopback-only e referências opacas para credenciais | **Melhoria local-first** | [#159](https://github.com/DevStarrk1137/ayvu/issues/159) e [#160](https://github.com/DevStarrk1137/ayvu/issues/160). |
| Corpus | Envio de conteúdo para treino, corpus ou telemetria por padrão | **Intencionalmente não suportada** | Cache não equivale a consentimento nem a corpus aprovado. |
| Distribuição | Instalação e execução local da CLI Python | **Existente** | [README](../README.md). |
| Distribuição | Desktop installer, publicação de pacote e pipeline de release | **Adiada** | Não pertencem ao fluxo de CI ou à entrega atual. |

## Consumidores do núcleo

A CLI é o consumidor entregue. Desktop, API local, MCP e assistência por IA são
consumidores ou adaptadores futuros: não devem duplicar regras de EPUB, cache,
HTTP, arquivos, glossário ou revisão.

## Não suportado por intenção

- remoção de DRM, download de livros ou distribuição de conteúdo protegido;
- fallback remoto silencioso, upload automático ou telemetria com conteúdo do
  usuário;
- contribuição automática de traduções, cache ou revisões para corpus de treino;
- promessa de suporte estrutural ou visual sem validação específica;
- publicação automática de traduções, revisões ou artefatos.

## Evolução desta página

Uma capacidade só passa a **Existente** com artefato versionado e evidência
verificável. Issue aberta, nota local ou direção proposta não bastam. Toda mudança
de status deve preservar os limites acima e apontar para evidência pública.
