"""The content types of the Office packages the job opens, and the one way to give a template the type of a document.

python-docx and python-pptx open a package only if its main part has the content type of a document or a
presentation; a template (``.dotx``, ``.potx``) has the same parts under the type of a template. :func:`retyped`
copies a package with that one type changed, and :func:`main_type` tells which type a package's main part has, so that
a macro-enabled presentation or a slide show can be refused before a library opens it.

lxml is loaded inside the functions, like every library a job needs: a job pays only for its own.
"""

import io
import zipfile
from typing import Final

#: The main part of a presentation, as a template, a presentation, a macro-enabled presentation and a slide show.
PML_TEMPLATE_MAIN: Final = "application/vnd.openxmlformats-officedocument.presentationml.template.main+xml"
PML_PRESENTATION_MAIN: Final = "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"
PML_MACRO_MAIN: Final = "application/vnd.ms-powerpoint.presentation.macroEnabled.main+xml"
PML_SLIDESHOW_MAIN: Final = "application/vnd.openxmlformats-officedocument.presentationml.slideshow.main+xml"

#: The main part of a Word file, as a template and as a document.
WML_TEMPLATE_MAIN: Final = "application/vnd.openxmlformats-officedocument.wordprocessingml.template.main+xml"
WML_DOCUMENT_MAIN: Final = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"

_TYPES: Final = "{http://schemas.openxmlformats.org/package/2006/content-types}"
_CONTENT_TYPES: Final = "[Content_Types].xml"
_MAIN_PARTS: Final = frozenset({"/ppt/presentation.xml", "/word/document.xml"})


def retyped(data: bytes, old: str, new: str) -> bytes:
    """Give a package's parts of one content type another.

    Args:
        data: The package.
        old: The content type to replace, such as :data:`WML_TEMPLATE_MAIN`.
        new: The content type it becomes, such as :data:`WML_DOCUMENT_MAIN`.

    Returns:
        The package as it is when no ``Override`` of its ``[Content_Types].xml`` has ``old``, else a copy whose
        ``Override`` entries of ``old`` have ``new``.
    """
    from lxml import etree

    from workbench.executor.readers.docx import parser

    with zipfile.ZipFile(io.BytesIO(data)) as source:
        types = etree.fromstring(source.read(_CONTENT_TYPES), parser())
        overrides = [o for o in types.iter(f"{_TYPES}Override") if o.get("ContentType") == old]
        if not overrides:
            return data
        for override in overrides:
            override.set("ContentType", new)
        copied = io.BytesIO()
        with zipfile.ZipFile(copied, "w", zipfile.ZIP_DEFLATED) as target:
            for item in source.infolist():
                content = source.read(item.filename)
                if item.filename == _CONTENT_TYPES:
                    content = etree.tostring(types, xml_declaration=True, encoding="UTF-8", standalone=True)
                target.writestr(item, content)
        return copied.getvalue()


def main_type(data: bytes) -> str | None:
    """Tell the content type of a package's main part.

    Args:
        data: The package.

    Returns:
        The type of ``/ppt/presentation.xml`` or ``/word/document.xml``, whichever the package declares; ``None`` if
        it declares neither.
    """
    from lxml import etree

    from workbench.executor.readers.docx import parser

    with zipfile.ZipFile(io.BytesIO(data)) as source:
        types = etree.fromstring(source.read(_CONTENT_TYPES), parser())
    for override in types.iter(f"{_TYPES}Override"):
        if override.get("PartName") in _MAIN_PARTS:
            return override.get("ContentType")
    return None
