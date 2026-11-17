# Translation memory

> **Translation in progress:** this page still holds the original Portuguese text of the former
> README, moved here unchanged. An English version replaces it shortly.

## Memória de Tradução

O cache normal só reaproveita traduções de trechos **idênticos**. A memória de
tradução é uma camada opcional que também reaproveita trechos **parecidos**, útil
em livros técnicos, séries e textos com frases repetidas. Ela fica **desligada por
padrão** e é ativada com `--translation-memory`:

```bash
uv run ayvu translate livro.epub \
  --output livro-ptbr.epub \
  --cache .cache/traducoes.sqlite \
  --translation-memory \
  --tm-apply-threshold 0.95 \
  --tm-suggest-threshold 0.80
```

Sem `translator_models.libretranslate`, a memória mantém o comportamento legado
e usa os mesmos pares original/traduzido já guardados no cache SQLite, em disco. Para cada trecho novo sem correspondência exata, o Ayvu busca candidatos
parecidos do mesmo par de idiomas e mede a similaridade. O comportamento é em
camadas, controlado por dois limiares de similaridade (entre 0 e 1):

No momento, `--translation-memory` exige `--workers 1`. Para execução paralela,
remova `--translation-memory`; para usar memória de tradução, mantenha o padrão
sequencial.

- **Similaridade ≥ `--tm-apply-threshold`** (padrão `0.95`): a tradução guardada é
  reaproveitada direto, sem chamar o tradutor. Conta como `Texts from memory` no
  relatório.
- **Similaridade em `[--tm-suggest-threshold, --tm-apply-threshold)`** (padrão
  `0.80` a `0.95`): o trecho é traduzido normalmente, mas a correspondência
  parecida é registrada como sugestão de revisão e contada em `Memory
  suggestions`.
- **Similaridade abaixo de `--tm-suggest-threshold`**: nada muda, o trecho segue o
  fluxo normal de tradução.

Por segurança, a memória atua **somente em trechos de texto puro**: blocos com
tags inline (como `<em>` ou `<a>`) não reaproveitam memória, e correspondências
que envolvam marcação são descartadas, para nunca injetar a formatação de outro
trecho. A reutilização **não** é regravada no cache sob o novo texto.

Os pares reaproveitáveis são os do próprio cache, então o formato exportável já
existente serve à memória: use `ayvu cache export` para salvar os pares
original/traduzido em JSON.

> **Risco de qualidade (falsos positivos).** Trechos parecidos podem ter
> significados diferentes (negações, números, nomes, pequenas trocas de palavra).
> Quanto menor o limiar de aplicação, maior o risco de reaproveitar uma tradução
> errada. Por isso a memória é opt-in e usa limiares altos por padrão; o nível de
> sugestão existe justamente para que correspondências menos seguras sejam
> revisadas em vez de aplicadas automaticamente. Para um livro novo e sensível,
> prefira manter a memória desligada ou usar `--review-output` para conferir o
> resultado.
