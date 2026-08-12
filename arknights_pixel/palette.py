"""Game palette constants and perceptual color matching."""

N = 24

PALETTE = [
    (34, 34, 34), (180, 180, 180), (234, 231, 223), (255, 255, 255),
    (211, 47, 54), (156, 10, 0), (214, 12, 74), (230, 150, 141),
    (254, 152, 117), (247, 208, 192), (252, 239, 234), (251, 246, 232),
    (220, 210, 200), (226, 206, 171), (213, 99, 34), (212, 140, 66),
    (242, 153, 0), (249, 201, 51), (252, 228, 153), (179, 180, 122),
    (194, 218, 114), (108, 110, 0), (170, 139, 82), (169, 143, 116),
    (170, 146, 40), (63, 43, 18), (116, 73, 31), (83, 70, 88),
    (42, 36, 70), (57, 69, 153), (90, 69, 157), (186, 163, 215),
    (182, 188, 223), (169, 172, 190), (99, 171, 185), (180, 210, 220),
    (145, 216, 230), (71, 174, 160), (182, 211, 200), (39, 56, 100),
]


def color_distance(c1, c2):
    """CompuPhase perceptual-ish RGB distance."""
    r1, g1, b1 = c1
    r2, g2, b2 = c2
    rmean = (r1 + r2) / 2.0
    dr, dg, db = r1 - r2, g1 - g2, b1 - b2
    return (
        (2.0 + rmean / 256.0) * dr * dr
        + 4.0 * dg * dg
        + (2.0 + (255.0 - rmean) / 256.0) * db * db
    )


def _srgb_to_linear(value):
    value = max(0.0, min(255.0, float(value))) / 255.0
    return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4


def rgb_to_oklab(rgb):
    """Convert an sRGB triplet to OKLab."""
    r, g, b = (_srgb_to_linear(value) for value in rgb)
    ll = 0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b
    mm = 0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b
    ss = 0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b
    l, m, s = max(0.0, ll) ** (1 / 3), max(0.0, mm) ** (1 / 3), max(0.0, ss) ** (1 / 3)
    return (
        0.2104542553 * l + 0.7936177850 * m - 0.0040720468 * s,
        1.9779984951 * l - 2.4285922050 * m + 0.4505937099 * s,
        0.0259040371 * l + 0.7827717662 * m - 0.8086757660 * s,
    )


OKLAB_PALETTE = [rgb_to_oklab(color) for color in PALETTE]


def nearest_palette_index(rgb, perceptual=False, candidates=None, match_strength=1.0):
    """Return the closest game color with a controllable RGB/OKLab blend."""
    choices = list(range(len(PALETTE)) if candidates is None else candidates)
    if not choices:
        raise ValueError("候选色列表不能为空。")
    strength = max(0.0, min(1.0, float(match_strength)))
    value = rgb_to_oklab(rgb)
    rgb_distances = []
    lab_distances = []
    for index in choices:
        rgb_distances.append(color_distance(rgb, PALETTE[index]) ** 0.5 / 765.0)
        target = OKLAB_PALETTE[index]
        lab_distances.append(sum((value[c] - target[c]) ** 2 for c in range(3)) ** 0.5)
    selected = lab_distances if perceptual else rgb_distances
    alternate = rgb_distances if perceptual else lab_distances
    distances = [
        strength * primary + (1.0 - strength) * secondary
        for primary, secondary in zip(selected, alternate)
    ]
    return choices[min(range(len(choices)), key=distances.__getitem__)]
