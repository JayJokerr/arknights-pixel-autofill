"""Resolution-independent canvas, palette and official-share recognition."""

from dataclasses import dataclass
from itertools import combinations
from statistics import median

from PIL import Image, ImageOps

from .image_processing import flatten_alpha
from .palette import N, PALETTE, color_distance, nearest_palette_index


@dataclass(frozen=True)
class GridLayout:
    x_lines: tuple
    y_lines: tuple
    confidence: float

    @property
    def bounds(self):
        return self.x_lines[0], self.y_lines[0], self.x_lines[-1], self.y_lines[-1]


@dataclass(frozen=True)
class PaletteLayout:
    columns: tuple
    rows: tuple
    first_global_row: int
    confidence: float

    @property
    def bounds(self):
        pitch_x = median(b - a for a, b in zip(self.columns, self.columns[1:]))
        pitch_y = median(b - a for a, b in zip(self.rows, self.rows[1:]))
        return (
            round(self.columns[0] - pitch_x * 0.45),
            round(self.rows[0] - pitch_y * 0.45),
            round(self.columns[-1] + pitch_x * 0.45),
            round(self.rows[-1] + pitch_y * 0.45),
        )


def _edge_scores(gray, expected_bounds, vertical):
    pixels = gray.load()
    width, height = gray.size
    left, top, right, bottom = expected_bounds
    expected_start = left if vertical else top
    expected_end = right if vertical else bottom
    pitch = max(4.0, (expected_end - expected_start) / N)
    axis_size = width if vertical else height
    cross_start = top if vertical else left
    cross_end = bottom if vertical else right
    cross_limit = height if vertical else width
    sample_start = max(2, round(cross_start - pitch * 1.2))
    sample_end = min(cross_limit - 2, round(cross_end + pitch * 1.2))
    step = max(2, round(pitch / 8))
    scores = [0] * axis_size
    for position in range(2, axis_size - 2):
        total = 0
        for cross in range(sample_start, sample_end, step):
            if vertical:
                a, b, c = pixels[position - 2, cross], pixels[position, cross], pixels[position + 2, cross]
            else:
                a, b, c = pixels[cross, position - 2], pixels[cross, position], pixels[cross, position + 2]
            total += abs(2 * b - a - c) + abs(a - c)
        scores[position] = total
    local = [0] * axis_size
    radius = max(2, min(6, round(pitch * 0.16)))
    for position in range(2, axis_size - 2):
        local[position] = max(scores[max(2, position - radius):min(axis_size - 2, position + radius + 1)])
    return scores, local


def detect_grid_axis(gray, expected_bounds, vertical, search_pitches=4.5):
    """Fit all 25 lines near a broad proportional estimate.

    The strong position prior prevents a neighbouring UI column from winning,
    while the wider search range still covers emulator layouts shifted by one
    or two complete cells.
    """
    pixels = gray.load()
    image_w, image_h = gray.size
    left, top, right, bottom = expected_bounds
    expected_start = left if vertical else top
    expected_end = right if vertical else bottom
    expected_pitch = (expected_end - expected_start) / N
    axis_size = image_w if vertical else image_h
    search_start = max(2, round(expected_start - expected_pitch * search_pitches))
    search_end = min(axis_size - 3, round(expected_end + expected_pitch * search_pitches))
    scores = [0] * axis_size
    if vertical:
        samples = range(max(2, round(top - expected_pitch)),
                        min(image_h - 2, round(bottom + expected_pitch)), 3)
        for pos in range(search_start, search_end + 1):
            scores[pos] = sum(
                abs(2 * pixels[pos, q] - pixels[pos - 2, q] - pixels[pos + 2, q])
                + abs(pixels[pos - 2, q] - pixels[pos + 2, q]) for q in samples
            )
    else:
        samples = range(max(2, round(left - expected_pitch)),
                        min(image_w - 2, round(right + expected_pitch)), 3)
        for pos in range(search_start, search_end + 1):
            scores[pos] = sum(
                abs(2 * pixels[q, pos] - pixels[q, pos - 2] - pixels[q, pos + 2])
                + abs(pixels[q, pos - 2] - pixels[q, pos + 2]) for q in samples
            )
    local = [0] * axis_size
    for pos in range(search_start, search_end + 1):
        local[pos] = max(scores[max(0, pos - 4):min(axis_size, pos + 5)])
    nonzero = [local[pos] for pos in range(search_start, search_end + 1) if local[pos] > 0]
    if not nonzero:
        raise RuntimeError("未检测到网格线")
    baseline = median(nonzero)
    best = None
    pitch_min = round(expected_pitch * .78 * 20)
    pitch_max = round(expected_pitch * 1.22 * 20)
    for pitch_step in range(pitch_min, pitch_max + 1):
        pitch = pitch_step / 20.0
        start_min = max(2, round(expected_start - expected_pitch * search_pitches))
        start_max = min(axis_size - round(N * pitch) - 3,
                        round(expected_start + expected_pitch * search_pitches))
        for start in range(start_min, start_max + 1):
            predicted = [round(start + i * pitch) for i in range(N + 1)]
            values = [local[pos] for pos in predicted]
            proximity = (
                abs(start - expected_start) / expected_pitch
                + abs((start + N * pitch) - expected_end) / expected_pitch
                + 4.0 * abs(pitch - expected_pitch) / expected_pitch
            )
            fit_score = sum(values) - baseline * 60.0 * proximity
            if best is None or fit_score > best[0]:
                best = fit_score, start, pitch, values
    if best is None:
        raise RuntimeError("网格线拟合失败")
    _fit, start, pitch, values = best
    positions = []
    for i in range(N + 1):
        predicted = round(start + i * pitch)
        lo, hi = max(2, predicted - 4), min(axis_size - 2, predicted + 5)
        positions.append(max(range(lo, hi), key=scores.__getitem__))
    if any(b <= a for a, b in zip(positions, positions[1:])):
        raise RuntimeError("网格线顺序异常")
    detected_pitch = (positions[-1] - positions[0]) / N
    residual = max(abs(pos - (positions[0] + i * detected_pitch))
                   for i, pos in enumerate(positions))
    confidence = median(values) / max(1.0, baseline)
    # Anti-aliased/faint game grid lines can make one or two local maxima land
    # several pixels from the ideal line, especially at 90% UI scale.  The
    # center calculation still remains safely inside a 21px cell at 0.35 pitch.
    if confidence < .75 or residual > max(4.0, detected_pitch * .35):
        raise RuntimeError(f"网格识别置信度不足(score={confidence:.2f}, residual={residual:.1f})")
    return tuple(positions), min(1.0, confidence / 2.5)


def detect_canvas_grid(image, expected_bounds):
    gray = ImageOps.grayscale(image)
    x_lines, confidence_x = detect_grid_axis(gray, expected_bounds, True)
    y_lines, confidence_y = detect_grid_axis(gray, expected_bounds, False)
    pitch_x = (x_lines[-1] - x_lines[0]) / N
    pitch_y = (y_lines[-1] - y_lines[0]) / N
    if abs(pitch_x - pitch_y) > max(pitch_x, pitch_y) * 0.09:
        raise RuntimeError(f"画布横纵格距不一致：{pitch_x:.1f}/{pitch_y:.1f}")
    return GridLayout(x_lines, y_lines, min(confidence_x, confidence_y))


def detect_canvas_grid_dynamic(image, expected_bounds):
    """Locate the unique long outer frame, then refine its 25 grid lines.

    Internal grid lines are phase-equivalent and can shift a fit by one row.
    The outer frame is different: it produces a long edge across almost the
    entire side. Ranking paired long-edge peaks removes that ambiguity and is
    also substantially faster than trying hundreds of 25-line hypotheses.
    """
    gray = ImageOps.grayscale(image)
    pixels = gray.load()
    width, height = gray.size
    left, top, right, bottom = expected_bounds
    expected_side = ((right - left) + (bottom - top)) / 2
    pitch = expected_side / N

    x_min = max(3, round(left - pitch * 4.5))
    x_max = min(width - 4, round(right + pitch * 4.5))
    y_min = max(3, round(top - pitch * 4.5))
    y_max = min(height - 4, round(bottom + pitch * 4.5))

    def vertical_score(x):
        return sum(
            abs(2 * pixels[x, y] - pixels[x - 2, y] - pixels[x + 2, y])
            + abs(pixels[x - 2, y] - pixels[x + 2, y])
            for y in range(y_min, y_max + 1, 2)
        )

    def horizontal_score(y):
        return sum(
            abs(2 * pixels[x, y] - pixels[x, y - 2] - pixels[x, y + 2])
            + abs(pixels[x, y - 2] - pixels[x, y + 2])
            for x in range(x_min, x_max + 1, 2)
        )

    x_scores = {x: vertical_score(x) for x in range(x_min, x_max + 1)}
    y_scores = {y: horizontal_score(y) for y in range(y_min, y_max + 1)}

    def peaks(scores, count=28):
        ordered = sorted(scores, key=scores.get, reverse=True)
        result = []
        for position in ordered:
            if all(abs(position - existing) > 5 for existing in result):
                result.append(position)
                if len(result) >= count:
                    break
        return sorted(result)

    x_peaks, y_peaks = peaks(x_scores), peaks(y_scores)
    best = None
    for x0, x1 in combinations(x_peaks, 2):
        side_x = x1 - x0
        if not expected_side * .72 <= side_x <= expected_side * 1.18:
            continue
        for y0, y1 in combinations(y_peaks, 2):
            side_y = y1 - y0
            if abs(side_x - side_y) > max(side_x, side_y) * .055:
                continue
            frame_strength = x_scores[x0] + x_scores[x1] + y_scores[y0] + y_scores[y1]
            square_penalty = abs(side_x - side_y) * frame_strength / max(1, side_x + side_y)
            center_penalty = (
                abs((x0 + x1) / 2 - (left + right) / 2)
                + abs((y0 + y1) / 2 - (top + bottom) / 2)
            ) * frame_strength / max(1, expected_side) * .05
            score = frame_strength - square_penalty - center_penalty
            if best is None or score > best[0]:
                best = score, x0, y0, x1, y1
    if best is None:
        raise RuntimeError("未能检测到近似正方形的画布外框")
    _score, x0, y0, x1, y1 = best
    # The editor is defined as an exact 24 x 24 board.  Watermarks, coloured
    # cells and anti-aliasing make individual internal grid lines unreliable;
    # snapping every line independently can move a click towards an adjacent
    # cell.  Once the four distinctive long frame edges are known, equal
    # subdivision is both more accurate and invariant across client DPI/UI
    # scales.
    x_lines = tuple(x0 + (x1 - x0) * index / N for index in range(N + 1))
    y_lines = tuple(y0 + (y1 - y0) * index / N for index in range(N + 1))
    square_confidence = 1.0 - abs((x1 - x0) - (y1 - y0)) / max(x1 - x0, y1 - y0)
    return GridLayout(x_lines, y_lines, max(0.0, min(1.0, square_confidence)))


def _palette_like(pixel):
    # Neutral black/white UI surfaces are too common; chromatic palette colors
    # provide a much cleaner projection for locating the four columns.
    return min(color_distance(pixel, color) for color in PALETTE[4:]) < 1050


def _groups(active, max_gap=3):
    result = []
    start = previous = None
    for value in active:
        if start is None or value - previous > max_gap:
            if start is not None:
                result.append((start, previous))
            start = value
        previous = value
    if start is not None:
        result.append((start, previous))
    return result


def _best_four_columns(candidates, weights):
    if len(candidates) < 4:
        raise RuntimeError("未识别到调色板4列色块")
    best = None
    for group in combinations(candidates, 4):
        centers = [(a + b) / 2 for a, b in group]
        widths = [b - a + 1 for a, b in group]
        gaps = [b - a for a, b in zip(centers, centers[1:])]
        pitch = median(gaps)
        if pitch <= 0 or max(abs(gap - pitch) for gap in gaps) > pitch * 0.24:
            continue
        if max(widths) > min(widths) * 1.55:
            continue
        coverage = sum(sum(weights[a:b + 1]) for a, b in group)
        regularity = sum(abs(gap - pitch) for gap in gaps) + (max(widths) - min(widths)) * 3
        score = coverage - regularity * max(1, max(weights))
        if best is None or score > best[0]:
            best = score, tuple(round(center) for center in centers), median(widths)
    if best is None:
        raise RuntimeError("调色板列间距不规则")
    return best[1], best[2]


def detect_palette_layout(image, region=None):
    """Locate visible 4×6 game palette swatches and infer global row offset."""
    source = flatten_alpha(image)
    width, height = source.size
    if region is None:
        region = (round(width * 0.62), round(height * 0.17), width, round(height * 0.98))
    left, top, right, bottom = region
    # Sampling every ~2 physical pixels is sufficient for 50–70px swatches
    # and cuts a 1280×720 palette scan from about five seconds to near one.
    step = max(1, round(min(width, height) / 360))
    pixels = source.load()
    x_counts = [0] * width
    for x in range(left, right, step):
        count = 0
        for y in range(top, bottom, step):
            if _palette_like(pixels[x, y]):
                count += 1
        x_counts[x] = count
    peak = max(x_counts)
    if peak <= 0:
        raise RuntimeError("调色板区域没有检测到游戏颜色")
    active_x = [x for x in range(left, right, step) if x_counts[x] >= peak * 0.22]
    raw_x_groups = _groups(active_x, max_gap=step * 3)
    x_groups = [(a, b) for a, b in raw_x_groups if b - a >= max(step, width * 0.012)]
    columns, swatch_width = _best_four_columns(x_groups, x_counts)

    band_half = max(2, round(swatch_width * 0.38))
    y_counts = [0] * height
    for y in range(top, bottom, step):
        count = 0
        for center in columns:
            for x in range(max(left, center - band_half), min(right, center + band_half + 1), step):
                if _palette_like(pixels[x, y]):
                    count += 1
        y_counts[y] = count
    y_peak = max(y_counts)
    active_y = [y for y in range(top, bottom, step) if y_counts[y] >= y_peak * 0.22]
    raw_y_groups = _groups(active_y, max_gap=step * 3)
    y_groups = [(a, b) for a, b in raw_y_groups if b - a >= max(step, height * 0.012)]
    chromatic_centers = [round((a + b) / 2) for a, b in y_groups]
    if len(chromatic_centers) < 3:
        raise RuntimeError("未识别到足够的调色板行")
    differences = [b - a for a, b in zip(chromatic_centers, chromatic_centers[1:])
                   if b - a > height * 0.035]
    if not differences:
        raise RuntimeError("无法估算调色板行距")
    pitch_y = median(differences)
    # Build every plausible six-row phase.  Looking only at chromatic rows is
    # ambiguous when the first neutral row is not counted by `_palette_like`:
    # both ``row 0..5`` and ``row 1..6`` have an equally regular geometry.  We
    # therefore keep the alternatives and let the actual 40-colour sequence
    # decide the phase below.
    row_hypotheses = set()
    for anchor in chromatic_centers:
        for anchor_index in range(6):
            rows = tuple(round(anchor + (index - anchor_index) * pitch_y) for index in range(6))
            if rows[0] < top - pitch_y * 0.4 or rows[-1] > bottom + pitch_y * 0.4:
                continue
            row_hypotheses.add(rows)
    if not row_hypotheses:
        raise RuntimeError("调色板行拟合失败")

    radius = max(2, round(min(swatch_width, pitch_y * 0.75) * 0.18))
    candidates = []
    for rows in row_hypotheses:
        sampled = []
        for y in rows:
            row_colors = []
            for x in columns:
                crop = source.crop((x - radius, y - radius, x + radius + 1, y + radius + 1))
                values = list(crop.getdata())
                color = tuple(round(median(pixel[channel] for pixel in values)) for channel in range(3))
                row_colors.append(color)
            sampled.append(row_colors)

        geometry_error = sum(min(abs(row - value) for row in rows) for value in chromatic_centers)
        for first in range(0, 5):
            cost = geometry_error * 0.12
            for visible_row, colors in enumerate(sampled):
                global_row = first + visible_row
                if global_row >= 10:
                    cost += 1e9
                    continue
                for col, detected_color in enumerate(colors):
                    expected = global_row * 4 + col
                    # Compare the captured colour directly with the expected
                    # swatch.  Reducing it to a nearest index first discarded
                    # exactly the information needed to distinguish phases.
                    cost += color_distance(detected_color, PALETTE[expected]) ** 0.5
            candidates.append((cost, rows, first))
    candidates.sort()
    rows = candidates[0][1]
    first_global_row = candidates[0][2]
    # Nearby hypotheses can differ only by a one-pixel sampling centre.  They
    # should not make an otherwise certain global palette phase look
    # low-confidence, so compare the best cost for each distinct phase.
    phase_costs = sorted(min(cost for cost, _rows, phase in candidates if phase == first)
                         for first in range(5))
    separation = phase_costs[1] - phase_costs[0] if len(phase_costs) > 1 else 0
    confidence = max(0.0, min(1.0, separation / max(1.0, phase_costs[1])))
    return PaletteLayout(columns, rows, first_global_row, confidence)


def _dominant_cell_color(image, box):
    left, top, right, bottom = box
    inset_x = max(1, round((right - left) * 0.10))
    inset_y = max(1, round((bottom - top) * 0.10))
    sample = image.crop((left + inset_x, top + inset_y, right - inset_x, bottom - inset_y))
    width, height = sample.size
    pixels = []
    for y in range(height):
        for x in range(width):
            rx, ry = (x + 0.5) / width, (y + 0.5) / height
            if rx <= 0.24 or rx >= 0.76 or ry <= 0.24 or ry >= 0.76:
                pixels.append(sample.getpixel((x, y)))
    buckets = {}
    for pixel in pixels:
        key = tuple(channel // 12 for channel in pixel[:3])
        buckets.setdefault(key, []).append(pixel[:3])
    dominant = max(buckets.values(), key=len)
    return tuple(median(pixel[channel] for pixel in dominant) for channel in range(3))


def _cyan_bounds(source):
    width, height = source.size
    probe_width = min(600, width)
    probe = source.resize((probe_width, round(height * probe_width / width)), Image.Resampling.BILINEAR)
    pw, ph = probe.size
    pixels = probe.load()

    def cyan(pixel):
        r, g, b = pixel
        return g > 135 and b > 145 and r < 165 and g - r > 32 and b - r > 42

    x_scores = [sum(cyan(pixels[x, y]) for y in range(round(ph * .08), round(ph * .82), 2))
                for x in range(pw)]
    y_scores = [sum(cyan(pixels[x, y]) for x in range(round(pw * .03), round(pw * .97), 2))
                for y in range(ph)]
    x_peaks = sorted(range(round(pw * .03), round(pw * .97)), key=x_scores.__getitem__, reverse=True)[:40]
    y_peaks = sorted(range(round(ph * .05), round(ph * .85)), key=y_scores.__getitem__, reverse=True)[:50]
    best = None
    for left_p, right_p in combinations(sorted(x_peaks), 2):
        side = right_p - left_p
        if side < pw * .45 or side > pw * .94:
            continue
        for top_p in y_peaks:
            bottom_p = round(top_p + side)
            if bottom_p >= ph:
                continue
            score = x_scores[left_p] + x_scores[right_p] + y_scores[top_p]
            score += max(y_scores[max(0, bottom_p - 3):min(ph, bottom_p + 4)])
            if best is None or score > best[0]:
                best = score, left_p, top_p, right_p, bottom_p
    if best is None:
        raise ValueError("未找到分享图青色正方形边框。")
    _score, left_p, top_p, right_p, bottom_p = best
    scale = width / pw
    return tuple(round(value * scale) for value in (left_p, top_p, right_p, bottom_p))


def detect_official_share_grid(img):
    """Recover official share canvases across full, cropped and resized cards."""
    source = flatten_alpha(img)
    width, height = source.size
    if width < 260 or height < 260:
        raise ValueError("分享图片尺寸过小，无法稳定恢复24×24画布。")
    try:
        left, top, right, bottom = _cyan_bounds(source)
    except ValueError:
        # Fallback for templates where the cyan frame was recolored or cropped:
        # use a broad periodic-grid search around the largest plausible square.
        side = min(width * .84, height * .62)
        expected = (
            (width - side) / 2,
            max(height * .08, (height - side) * .20),
            (width + side) / 2,
            max(height * .08, (height - side) * .20) + side,
        )
        layout = detect_canvas_grid(source, expected)
        left, top, right, bottom = layout.bounds
    side = min(right - left, bottom - top)
    if side <= 0 or abs((right - left) - (bottom - top)) > side * .10:
        raise ValueError("识别到的分享画布不是稳定正方形。")
    border = max(1, round(side * .006))
    left, top = left + border, top + border
    side -= border * 2
    right, bottom = left + side, top + side
    matrix = []
    for row in range(N):
        matrix_row = []
        for col in range(N):
            box = (
                round(left + col * side / N), round(top + row * side / N),
                round(left + (col + 1) * side / N), round(top + (row + 1) * side / N),
            )
            color = _dominant_cell_color(source, box)
            matrix_row.append(nearest_palette_index(color, perceptual=True))
        matrix.append(matrix_row)
    unique = len({index for row in matrix for index in row})
    if unique < 2:
        raise ValueError("已定位画布，但没有识别到有效的多色像素内容。")
    confidence = min(1.0, 0.72 + unique / 120.0)
    return matrix, (left, top, right, bottom), confidence
