"""EPUBs built byte by byte, so a test can state exactly what an archive holds."""
from __future__ import annotations

import io
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile, ZipInfo

CONTAINER = b'<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>'
OPF = b'''<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" xmlns:opf="http://www.idpf.org/2007/opf" xmlns:dc="http://purl.org/dc/elements/1.1/" version="3.0" unique-identifier="id">
  <metadata><dc:title id="t">The Title</dc:title><dc:creator opf:role="aut">An Author</dc:creator><dc:creator>Another</dc:creator><dc:language>en</dc:language><dc:identifier id="id">urn:x</dc:identifier><!-- keep --></metadata>
  <manifest>
    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
    <item id="c1" href="text/one.xhtml" media-type="application/xhtml+xml"/>
    <item id="c2" href="text/two%20words.xhtml" media-type="application/xhtml+xml"/>
    <item id="css" href="style.css" media-type="text/css"/>
    <item id="img" href="images/a.png" media-type="image/png"/>
    <item id="remote" href="https://example.test/a.mp3" media-type="audio/mpeg"/>
  </manifest>
  <spine><itemref idref="c1"/><itemref idref="c2"/></spine>
</package>'''
XHTML = '<?xml version="1.0" encoding="utf-8"?>\n<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><head><title>Doc</title><link href="../style.css" rel="stylesheet"/></head><body>{}</body></html>'
ONE = XHTML.format('<p>Hello <a epub:type="noteref" href="two%20words.xhtml#n">world</a></p><img src="../images/a.png" alt="A picture"/>').encode()
TWO = XHTML.format('<p id="n">Second <b>chapter</b></p>').encode()
NAV = XHTML.format('<nav epub:type="toc"><ol><li><a href="text/one.xhtml">First</a></li></ol></nav>').encode()
DATE = (2020, 1, 2, 3, 4, 6)


def members():
    return [
        ("mimetype", b"application/epub+zip", ZIP_STORED),
        ("META-INF/container.xml", CONTAINER, ZIP_DEFLATED),
        ("OEBPS/content.opf", OPF, ZIP_DEFLATED),
        ("OEBPS/nav.xhtml", NAV, ZIP_DEFLATED),
        ("OEBPS/text/one.xhtml", ONE, ZIP_DEFLATED),
        ("OEBPS/text/two words.xhtml", TWO, ZIP_DEFLATED),
        ("OEBPS/style.css", b"p { color: red }", ZIP_STORED),
        ("OEBPS/images/a.png", b"\x89PNG not really", ZIP_STORED),
    ]


def zip_bytes(items, comment=b""):
    buffer = io.BytesIO()
    with ZipFile(buffer, "w") as archive:
        archive.comment = comment
        for name, data, compression in items:
            info = ZipInfo(name, DATE)
            info.compress_type = compression
            info.external_attr = 0o640 << 16
            archive.writestr(info, data)
    return buffer.getvalue()


def epub_bytes(*, replace=None, drop=(), add=(), comment=b""):
    replace = replace or {}
    items = [(name, replace.get(name, data), compression) for name, data, compression in members() if name not in drop]
    return zip_bytes([*items, *add], comment)


def opf_with(old, new):
    assert old in OPF
    return {"OEBPS/content.opf": OPF.replace(old, new)}
