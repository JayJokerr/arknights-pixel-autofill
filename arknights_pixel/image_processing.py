"""Pure Pillow image conversion pipeline, independent from Tk and Win32."""

from collections import Counter

from PIL import Image, ImageChops, ImageFilter, ImageOps, ImageStat

from .palette import N, PALETTE, nearest_palette_index


RESAMPLE_METHODS = {
    "Lanczos（原版）": Image.Resampling.LANCZOS,
    "BOX（减少混色）": Image.Resampling.BOX,
    "Bicubic（柔和）": Image.Resampling.BICUBIC,
    "Nearest（像素画）": Image.Resampling.NEAREST,
}


def flatten_alpha(img):
    image = ImageOps.exif_transpose(img).convert("RGBA")
    background = Image.new("RGBA", image.size, (255, 255, 255, 255))
    background.alpha_composite(image)
    return background.convert("RGB")


def _resize_with_method(img, mode, method):
    if mode == "stretch":
        return img.resize((N, N), method)
    if mode == "contain":
        output = Image.new("RGB", (N, N), (255, 255, 255))
        resized = img.copy()
        resized.thumbnail((N, N), method)
        output.paste(resized, ((N - resized.width) // 2, (N - resized.height) // 2))
        return output
    return ImageOps.fit(img, (N, N), method=method, centering=(0.5, 0.5))


def _fit_working_image(img, mode, side=192):
    if mode == "stretch":
        return img.resize((side, side), Image.Resampling.BILINEAR)
    if mode == "contain":
        output = Image.new("RGB", (side, side), (255, 255, 255))
        resized = img.copy()
        resized.thumbnail((side, side), Image.Resampling.LANCZOS)
        output.paste(resized, ((side - resized.width) // 2, (side - resized.height) // 2))
        return output
    return ImageOps.fit(img, (side, side), Image.Resampling.LANCZOS, centering=(0.5, 0.5))


def structure_aware_source(img, mode="crop", strength=0.0):
    """Compress broad illumination while retaining local object boundaries.

    This is a traditional CPU pipeline: low-frequency illumination estimation,
    bounded gain correction and edge-preserving blending. It deliberately does
    not use a global contrast stretch, which tends to amplify cast shadows.
    """
    amount = max(0.0, min(1.0, float(strength)))
    source = flatten_alpha(img)
    if amount <= 0.001:
        return source
    working = _fit_working_image(source, mode)
    luminance = ImageOps.grayscale(working)
    illumination = luminance.filter(ImageFilter.GaussianBlur(radius=18))
    anchor = ImageStat.Stat(illumination).median[0]
    pixels = []
    for rgb, light in zip(working.getdata(), illumination.getdata()):
        gain = max(0.76, min(1.30, (max(24.0, anchor) / max(24.0, light)) ** 0.42))
        corrected = tuple(max(0, min(255, round(channel * gain))) for channel in rgb)
        pixels.append(tuple(round(a * (1.0 - amount) + b * amount) for a, b in zip(rgb, corrected)))
    result = Image.new("RGB", working.size)
    result.putdata(pixels)
    # A small median pass suppresses isolated photographic texture without
    # spreading colors across the much larger object boundaries.
    if amount >= 0.35:
        smoothed = result.filter(ImageFilter.MedianFilter(3))
        result = Image.blend(result, smoothed, min(0.45, amount * 0.45))
    return result


def resize_to_24(img, mode="crop", resample="Lanczos（原版）", resample_strength=1.0,
                 structure_strength=0.0):
    source = flatten_alpha(img)
    if structure_strength > 0.001:
        source = structure_aware_source(source, mode=mode, strength=structure_strength)
        mode = "stretch"
    method = RESAMPLE_METHODS.get(resample, Image.Resampling.LANCZOS)
    selected = _resize_with_method(source, mode, method)
    strength = max(0.0, min(1.0, float(resample_strength)))
    if strength >= 0.999 or method == Image.Resampling.NEAREST:
        return selected
    baseline = _resize_with_method(source, mode, Image.Resampling.NEAREST)
    return Image.blend(baseline, selected, strength)


def _spatial_cleanup(matrix, max_changes=72):
    """Remove only low-confidence isolated cells; preserve real thin edges."""
    cleaned = [row[:] for row in matrix]
    changes = 0
    for y in range(N):
        for x in range(N):
            neighbours = []
            for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                xx, yy = x + dx, y + dy
                if 0 <= xx < N and 0 <= yy < N:
                    neighbours.append(matrix[yy][xx])
            common, count = Counter(neighbours).most_common(1)[0]
            if count >= 3 and common != matrix[y][x] and changes < max_changes:
                cleaned[y][x] = common
                changes += 1
    return cleaned


def quantize_image(img, mode="crop", dither=False, perceptual=False,
                   reduce_transitions=False, resample="Lanczos（原版）",
                   resample_strength=1.0, match_strength=1.0,
                   structure_strength=0.0):
    src = resize_to_24(
        img, mode, resample=resample, resample_strength=resample_strength,
        structure_strength=structure_strength,
    )
    work = [[[float(v) for v in src.getpixel((x, y))] for x in range(N)] for y in range(N)]
    result = [[3 for _ in range(N)] for _ in range(N)]
    if reduce_transitions:
        dither = False

    def add_error(x, y, error, factor):
        if 0 <= x < N and 0 <= y < N:
            for channel in range(3):
                work[y][x][channel] = max(
                    0.0, min(255.0, work[y][x][channel] + error[channel] * factor)
                )

    for y in range(N):
        for x in range(N):
            old = tuple(int(round(v)) for v in work[y][x])
            index = nearest_palette_index(
                old, perceptual=perceptual, match_strength=match_strength
            )
            result[y][x] = index
            if dither:
                target = PALETTE[index]
                error = tuple(old[c] - target[c] for c in range(3))
                add_error(x + 1, y, error, 7 / 16)
                add_error(x - 1, y + 1, error, 3 / 16)
                add_error(x, y + 1, error, 5 / 16)
                add_error(x + 1, y + 1, error, 1 / 16)

    if reduce_transitions:
        counts = Counter(index for row in result for index in row)
        retained = [index for index, _count in counts.most_common(16)]
        result = [[
            nearest_palette_index(
                src.getpixel((x, y)), perceptual=perceptual,
                candidates=retained, match_strength=match_strength,
            )
            for x in range(N)] for y in range(N)]
    if structure_strength >= 0.35:
        result = _spatial_cleanup(result, max_changes=round(24 + 72 * structure_strength))
    return result


def import_24_bitmap(img, perceptual=False, match_strength=1.0):
    if img.size != (N, N):
        raise ValueError(f"位图尺寸必须为 {N}×{N}，当前为 {img.width}×{img.height}。")
    source = flatten_alpha(img)
    return [[nearest_palette_index(
        source.getpixel((x, y)), perceptual=perceptual, match_strength=match_strength,
    ) for x in range(N)] for y in range(N)]


def matrix_to_image(matrix):
    image = Image.new("RGB", (N, N))
    image.putdata([PALETTE[index] for row in matrix for index in row])
    return image
