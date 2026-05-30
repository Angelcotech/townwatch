"""
Controlled-DPI PDF rasterization for the vision extraction path.

The vision path currently ships the raw scanned PDF to the model (base64
document block), which means we have no control over resolution — a 600-DPI
scan goes up the wire at full size, costing latency and payload on pixels the
model can't use past a point. There's a resolution knee: below it text goes
illegible and accuracy drops; above it only cost and latency rise.

This renders PDF pages to images at a chosen DPI (pypdfium2 — permissive
license, bundled binary, no system deps) and builds the vision message
content as image blocks instead of a PDF document block. It applies ONLY to
the vision path — those PDFs are scanned with no text layer, so we lose
nothing by rasterizing (there was no text layer to extract).

Default behaviour is unchanged: with dpi=None the content is the raw PDF
document, exactly as before. Set a DPI (via config.VISION_RENDER_DPI or the
sweep) to switch to rasterized images at that resolution.
"""

from __future__ import annotations

import base64
import io
from pathlib import Path

import pypdfium2 as pdfium

# JPEG at this quality is plenty for document vision and far smaller than PNG;
# text edges survive fine. PNG available for a lossless comparison in sweeps.
DEFAULT_FORMAT = "JPEG"
DEFAULT_QUALITY = 85


def render_pdf_to_images(
    pdf_path: Path,
    dpi: int,
    fmt: str = DEFAULT_FORMAT,
    quality: int = DEFAULT_QUALITY,
) -> list[tuple[bytes, str]]:
    """Render every page of a PDF to image bytes at ``dpi``.
    Returns [(image_bytes, media_type), ...] in page order."""
    scale = dpi / 72.0  # PDF user space is 72 DPI; scale maps to target DPI
    out: list[tuple[bytes, str]] = []
    pdf = pdfium.PdfDocument(str(pdf_path))
    try:
        for i in range(len(pdf)):
            page = pdf[i]
            bitmap = page.render(scale=scale)
            img = bitmap.to_pil()
            if fmt == "JPEG" and img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            buf = io.BytesIO()
            img.save(buf, format=fmt, **({"quality": quality} if fmt == "JPEG" else {}))
            media = "image/jpeg" if fmt == "JPEG" else "image/png"
            out.append((buf.getvalue(), media))
    finally:
        pdf.close()
    return out


def vision_content(pdf_path: Path, instructions: str, dpi: int | None = None) -> list[dict]:
    """Build the `content` array for a vision request.

    dpi is None  → one PDF document block (raw, current behaviour, unchanged).
    dpi is an int → one image block per page, rasterized at that DPI.
    The instruction text is always appended last.
    """
    if dpi is None:
        pdf_b64 = base64.standard_b64encode(pdf_path.read_bytes()).decode("utf-8")
        return [
            {
                "type": "document",
                "source": {"type": "base64", "media_type": "application/pdf", "data": pdf_b64},
            },
            {"type": "text", "text": instructions},
        ]

    blocks: list[dict] = []
    for img_bytes, media in render_pdf_to_images(pdf_path, dpi):
        blocks.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media,
                    "data": base64.standard_b64encode(img_bytes).decode("utf-8"),
                },
            }
        )
    blocks.append({"type": "text", "text": instructions})
    return blocks
