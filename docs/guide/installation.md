# Installation and setup

> **Translation in progress:** this page still holds the original Portuguese text of the former
> README, moved here unchanged. An English version replaces it shortly.

## Aviso de Uso

Este projeto é destinado a uso pessoal, estudo e acessibilidade com arquivos EPUB fornecidos por você. Ele não remove DRM, não baixa livros e não deve ser usado para distribuir conteúdo protegido por copyright.

Por padrão, arquivos `.epub`, cache local e glossários privados ficam fora do Git.

## Requisitos

- Python 3.11 ou mais recente. A disponibilidade das dependências nativas do
  desktop depende da versão do Python, sistema operacional e arquitetura.
- `uv` ou `pip`.
- Um servidor compatível com LibreTranslate para traduções reais.
- Para o desktop, uma sessão gráfica e as dependências nativas exigidas pelo Qt.

## Instalação com uv

Dentro do diretório do projeto, instale apenas a CLI:

```bash
cd ayvu
uv sync
uv run ayvu --help
```

Para instalar o suporte desktop opcional e executar seu ponto de entrada:

```bash
uv sync --extra desktop
uv run --extra desktop ayvu-desktop
```

O extra instala `PySide6-Essentials==6.11.2`, sem Addons ou WebEngine. O ponto
de entrada informa que a interface ainda não está disponível e termina com código
1; sua implementação pertence a DESKTOP-010. `ayvu-desktop --help` retorna 0.
Sem o extra, o comando informa como instalar a dependência e termina com código 1.
Os fluxos de tradução documentados abaixo estão disponíveis pela CLI, cuja
instalação padrão não exige PySide6.

Para desenvolver e executar os testes da CLI:

```bash
uv sync --extra dev
uv run --extra dev pytest
```

Para desenvolver o desktop, selecione ambos os extras:

```bash
uv sync --extra dev --extra desktop
uv run --extra dev --extra desktop pytest
```

Ao alternar entre esses perfis, inclua os extras desejados no comando `uv run`,
pois o uv sincroniza o ambiente conforme a seleção informada.

## Instalação com pip

```bash
cd ayvu
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .              # CLI
# Ou: python -m pip install -e ".[desktop]"
# Para desenvolvimento: python -m pip install -e ".[dev,desktop]"
```

Com o ambiente ativado, use `ayvu --help` ou, com o extra desktop,
`ayvu-desktop`.

## Servidor de Tradução

Suba o LibreTranslate localmente com Docker:

```bash
docker run -it -p 5000:5000 libretranslate/libretranslate
```

Endpoint usado pela CLI:

```text
http://localhost:5000/translate
```

Teste a conexão:

```bash
uv run ayvu test-translator --url http://localhost:5000
```
