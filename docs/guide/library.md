# Library

The library uses the configured base folder, by default:

```text
~/Documentos/Livros
```

With the default names, EPUBs are organised like this:

```text
~/Documentos/Livros/
├── Original/
└── Traduzidos/
```

Put original EPUBs in `Original` and final translations in `Traduzidos`. When you choose `Open library` in
the common menu, Ayvu lists the books alphabetically, shows whether the original exists and which
translations were found, lets you see the book's details, and opens the selected version in the reader app.
If no default reader is detected, set the reader command in `Settings`, in the `Reader app` field.

When opening a version, Ayvu checks again that the EPUB is still the same and still inside the books
folder. Links outside that folder are not part of the library, and skipped items produce a notice. In
`Reader app`, enter the executable and its options, without a shell such as `sh`.
