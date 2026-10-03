import base64

# 1x1 JPEG, enough for pypdf to hand back the raw DCT stream
JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAP//////////////////////////////////////////////////////////////////////////////////////"
    "wgALCAABAAEBAREA/8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABPxA="
)


def build_pdf(pages: list[dict]) -> bytes:
    """Each page: {"text": str | None, "jpeg": bytes | None}."""
    objects: dict[int, bytes] = {}
    next_id = 3
    kids: list[int] = []
    font_id: int | None = None
    for page in pages:
        page_id, content_id = next_id, next_id + 1
        next_id += 2
        resources: list[bytes] = []
        content = b""
        if page.get("text"):
            if font_id is None:
                font_id = next_id
                next_id += 1
                objects[font_id] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
            escaped = page["text"].replace("\\", "\\\\").replace("(", r"\(").replace(")", r"\)")
            content += b"BT /F1 12 Tf 72 700 Td (" + escaped.encode("latin-1") + b") Tj ET\n"
            resources.append(b"/Font << /F1 %d 0 R >>" % font_id)
        if page.get("jpeg"):
            image_id = next_id
            next_id += 1
            objects[image_id] = (
                b"<< /Type /XObject /Subtype /Image /Width 1 /Height 1 /ColorSpace /DeviceRGB /BitsPerComponent 8 "
                b"/Filter /DCTDecode /Length %d >>\nstream\n" % len(page["jpeg"])
            ) + page["jpeg"] + b"\nendstream"
            content += b"q 600 0 0 800 0 0 cm /Im0 Do Q\n"
            resources.append(b"/XObject << /Im0 %d 0 R >>" % image_id)
        objects[content_id] = b"<< /Length %d >>\nstream\n" % len(content) + content + b"endstream"
        objects[page_id] = (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents %d 0 R "
                            b"/Resources << %s >> >>" % (content_id, b" ".join(resources)))
        kids.append(page_id)
    objects[1] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objects[2] = b"<< /Type /Pages /Count %d /Kids [%s] >>" % (len(kids), b" ".join(b"%d 0 R" % k for k in kids))
    out = bytearray(b"%PDF-1.4\n")
    offsets: dict[int, int] = {}
    for object_id in sorted(objects):
        offsets[object_id] = len(out)
        out += b"%d 0 obj\n" % object_id + objects[object_id] + b"\nendobj\n"
    xref_at = len(out)
    size = max(objects) + 1
    out += b"xref\n0 %d\n0000000000 65535 f \n" % size
    for object_id in range(1, size):
        out += b"%010d 00000 n \n" % offsets[object_id]
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (size, xref_at)
    return bytes(out)
