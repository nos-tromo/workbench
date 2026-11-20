"""The pages of a PDF as pictures: open it, find the pages with no text, and draw one at a fixed size.

Everything here is pdfium's (``pypdfium2``). Each page, text page and bitmap opened is closed before the function
returns, and the document is the caller's to close. The file is untrusted: whatever pdfium or Pillow raises becomes
:class:`~workbench.executor.readers.UnreadableFile` with a short fixed text that names no path and quotes no library;
a time limit (:class:`~workbench.executor.readers.ReadTimeout`, which is not an ``Exception``) passes through.
"""

from pathlib import Path
from typing import Any, Final

import pypdfium2
import pypdfium2.raw
from PIL import Image

from workbench.executor import readers
from workbench.executor.readers import DAMAGED, TOO_BIG, UnreadableFile
from workbench.executor.readers.pdf import _ENCRYPTED
from workbench.renders import PAGE_LONG_SIDE

_NO_PAGES: Final = "the PDF has no pages"

#: The scale is made this much smaller than the long side asks. pdfium rounds a drawn side up, and floating point puts
#: ``edge * (1600 / edge)`` a hair over 1600 for one edge in sixteen, which would draw 1601 pixels.
_SCALE_MARGIN: Final = 1e-9


def open_pdf(path: Path) -> Any:
    """Open a PDF without a password.

    Args:
        path: The file.

    Returns:
        The ``pypdfium2.PdfDocument``, which the caller closes.

    Raises:
        UnreadableFile: The PDF asks for a password to open, is damaged or truncated or not a PDF, has no pages, or
            is too large for the memory limit.
    """
    try:
        pdf = pypdfium2.PdfDocument(path)
    except pypdfium2.PdfiumError as exc:
        if exc.err_code == pypdfium2.raw.FPDF_ERR_PASSWORD:
            raise UnreadableFile(_ENCRYPTED) from None
        raise UnreadableFile(DAMAGED) from None
    except MemoryError:
        raise UnreadableFile(TOO_BIG) from None
    except Exception:
        raise UnreadableFile(DAMAGED) from None
    try:
        if len(pdf) == 0:
            raise UnreadableFile(_NO_PAGES)
    except BaseException:
        pdf.close()
        raise
    return pdf


def pages_without_text(pdf: Any, limit: int) -> tuple[list[int], bool]:
    """Find the pages whose text layer holds nothing but whitespace: scans and pictures.

    The clock is read between pages, since a page can take long to load.

    Args:
        pdf: The open document.
        limit: How many pages, from the first, to look at.

    Returns:
        The page numbers, counted from 1, of those looked at that have no text, in order; and whether pages past
        ``limit`` were left unchecked.

    Raises:
        UnreadableFile: A page cannot be loaded or read, or memory ran out.
        ReadTimeout: The deadline (:func:`~workbench.executor.readers.set_deadline`) passed.
    """
    total = len(pdf)
    checked = min(total, limit)
    blank: list[int] = []
    try:
        for index in range(checked):
            readers.check()
            page = pdf[index]
            try:
                text = page.get_textpage()
                try:
                    if not text.get_text_range().strip():
                        blank.append(index + 1)
                finally:
                    text.close()
            finally:
                page.close()
    except MemoryError:
        raise UnreadableFile(TOO_BIG) from None
    except Exception:
        raise UnreadableFile(DAMAGED) from None
    return blank, total > checked


def render_page(pdf: Any, n: int) -> Image.Image:
    """Draw one page, its long side ``PAGE_LONG_SIDE`` pixels.

    The page is drawn as pdfium draws it, which is with the page's own rotation (``/Rotate``) applied, and its
    size (:meth:`get_size`) is the turned one; no rotation is asked for on top, or a turned page would be turned
    twice. Pdfium fills the page with white, so there is no transparency to lay down.

    Args:
        pdf: The open document.
        n: The page, counted from 1.

    Returns:
        The page as an ``RGB`` picture whose pixels are its own (the bitmap is closed before this returns).

    Raises:
        UnreadableFile: The page cannot be loaded or drawn, has no size, or is too large for the memory limit.
        ReadTimeout: The deadline passed.
    """
    try:
        page = pdf[n - 1]
        try:
            width, height = page.get_size()
            if not (width > 0 and height > 0):
                raise UnreadableFile(DAMAGED)
            scale = PAGE_LONG_SIDE / max(width, height) * (1 - _SCALE_MARGIN)
            bitmap = page.render(scale=scale)
            try:
                image: Image.Image = bitmap.to_pil().convert("RGB")  # a copy: the bitmap's buffer goes with it
                return image
            finally:
                bitmap.close()
        finally:
            page.close()
    except UnreadableFile:
        raise
    except MemoryError:
        raise UnreadableFile(TOO_BIG) from None
    except Exception:
        raise UnreadableFile(DAMAGED) from None
