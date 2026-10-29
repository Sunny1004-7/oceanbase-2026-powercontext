"""Author PDFs from independently supplied page strings, including Unicode."""

from __future__ import annotations

import io
import textwrap

from pypdf import PdfWriter
from pypdf.generic import ArrayObject, DecodedStreamObject, DictionaryObject, NameObject, NumberObject, TextStringObject


def make_pdf(pages: list[str], *, user_password: str | None = None) -> bytes:
    writer = PdfWriter()
    characters = sorted(set("".join(pages)) - {"\n", "\r"})
    codes = {character: index + 1 for index, character in enumerate(characters)}
    pairs = "\n".join(f"<{code:04x}> <{character.encode('utf-16-be').hex()}>" for character, code in codes.items())
    mapping = DecodedStreamObject()
    mapping.set_data(
        (
            "/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n"
            "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n"
            "/CMapName /IdentityUnicode def\n/CMapType 2 def\n1 begincodespacerange\n<0000> <FFFF>\nendcodespacerange\n"
            f"{len(codes)} beginbfchar\n{pairs}\nendbfchar\nendcmap\nCMapName currentdict /CMap defineresource pop\nend\nend"
        ).encode()
    )
    cid = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/CIDFontType2"),
        NameObject("/BaseFont"): NameObject("/TestUnicode"),
        NameObject("/CIDSystemInfo"): DictionaryObject({
            NameObject("/Registry"): TextStringObject("Adobe"),
            NameObject("/Ordering"): TextStringObject("Identity"),
            NameObject("/Supplement"): NumberObject(0),
        }),
    })
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type0"),
        NameObject("/BaseFont"): NameObject("/TestUnicode"),
        NameObject("/Encoding"): NameObject("/Identity-H"),
        NameObject("/DescendantFonts"): ArrayObject([writer._add_object(cid)]),
        NameObject("/ToUnicode"): writer._add_object(mapping),
    })
    font_ref = writer._add_object(font)
    for text in pages:
        page = writer.add_blank_page(width=612, height=792)
        page[NameObject("/Resources")] = DictionaryObject({
            NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_ref})
        })
        if text:
            commands = []
            lines = [
                line
                for original in text.splitlines()
                for line in textwrap.wrap(original, width=84, break_long_words=False, break_on_hyphens=False)
            ]
            for index, line in enumerate(lines):
                encoded = "".join(f"{codes[character]:04x}" for character in line)
                commands.append(f"BT /F1 10 Tf 1 0 0 1 36 {756 - index * 13} Tm <{encoded}> Tj ET")
            content = DecodedStreamObject()
            content.set_data("\n".join(commands).encode())
            page[NameObject("/Contents")] = writer._add_object(content)
    if user_password is not None:
        writer.encrypt(user_password=user_password, owner_password="owner")  # noqa: S106 - authored public test PDF
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()
