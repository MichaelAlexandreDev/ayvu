# Library

> **Translation in progress:** this page still holds the original Portuguese text of the former
> README, moved here unchanged. An English version replaces it shortly.

## Biblioteca

A biblioteca inicial usa a pasta base configurada, por padrão:

```text
~/Documentos/Livros
```

Com os nomes padrão, os EPUBs ficam organizados assim:

```text
~/Documentos/Livros/
├── Original/
└── Traduzidos/
```

Coloque EPUBs originais em `Original` e traduções finais em `Traduzidos`. Ao escolher
`Open library` no menu comum, o Ayvu lista os livros em ordem alfabética, mostra se existe
original e quais traduções foram encontradas, permite ver informações do livro e abre a versão
selecionada no app leitor. Se nenhum leitor padrão for detectado, configure o comando do leitor em
`Settings` no campo `Reader app`.

Ao abrir uma versão, o Ayvu verifica novamente se o EPUB continua o mesmo e dentro da pasta de
livros. Links para fora dessa pasta não entram na biblioteca; itens ignorados geram um aviso.
Em `Reader app`, informe o executável e suas opções, sem interpretador de comandos como `sh`.
