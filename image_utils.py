from io import BytesIO

from PIL import Image, ImageOps


MAX_IMAGE_SIZE = 800
JPEG_QUALITY = 85


def resize_image_to_800(image_bytes: bytes) -> bytes:
    """Normalize an uploaded photo to JPEG with a maximum side of 800px."""
    with Image.open(BytesIO(image_bytes)) as image:
        image = ImageOps.exif_transpose(image)

        # Photos are normally RGB, but support images with alpha as well.
        if image.mode in ("RGBA", "LA") or (
            image.mode == "P" and "transparency" in image.info
        ):
            rgba = image.convert("RGBA")
            background = Image.new("RGB", rgba.size, "white")
            background.paste(rgba, mask=rgba.getchannel("A"))
            image = background
        else:
            image = image.convert("RGB")

        if max(image.size) > MAX_IMAGE_SIZE:
            image.thumbnail(
                (MAX_IMAGE_SIZE, MAX_IMAGE_SIZE),
                Image.Resampling.LANCZOS,
            )

        output = BytesIO()
        image.save(
            output,
            format="JPEG",
            quality=JPEG_QUALITY,
            optimize=True,
            progressive=True,
        )
        return output.getvalue()
