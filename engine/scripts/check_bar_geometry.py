"""相机信息栏几何检测 / 金标准比对工具。

用法::

    python scripts/check_bar_geometry.py assets/参考图.jpg              # 打印归一化几何
    python scripts/check_bar_geometry.py assets/参考图.jpg out.png 3    # 两者逐项比对，容差 3px

为什么单独写脚本：信息栏的版式参数（条高、边距、分栏位置、字号、颜色）都应当由
「参照图的实测像素」推导，而不是凭手感填。本脚本对任何图做同一套量测，因此
「模板数值是否还原参照图」这件事可以脱离肉眼、自动化验证。

量测口径（全部归一化，天然与分辨率无关）：
- 条高    bar_height / 画布总高
- 边距    margin_x / 画布宽
- 分栏线  x / 画布宽、线高 / 条高、颜色
- 每段文字 ink bbox（含透明边距的真实墨迹范围）→ 字号按 ink 高度 / 画布宽 记录
- 每段 ink 中心 y / 条中心 y（验证「所有文字块在条内垂直居中」）
"""
import json
import sys

from PIL import Image

# 墨迹阈值：白色信息栏在 JPEG 里会有 250~254 的振铃噪声，用 230 才能把噪声排除、
# 同时保住浅灰分隔线（#cbcbcb ≈ L203）与灰色日期文字（#7d7d7d ≈ L125）。
INK_LEVEL = 230
# 「照片行」判定：照片的每一行通常布满内容，信息栏行只有少量文字
PHOTO_ROW_FILL = 0.5


def _ink_mask(img):
    return img.convert("L").point(lambda v: 255 if v < INK_LEVEL else 0)


def _ink_bbox(img):
    """墨迹 bbox；全无返回 None。"""
    return _ink_mask(img).getbbox()


def _ink_count(img):
    return _ink_mask(img).histogram()[255]


def _col_has_ink(img, x):
    return _ink_bbox(img.crop((x, 0, x + 1, img.height))) is not None


def _row_has_ink(img, y):
    return _ink_bbox(img.crop((0, y, img.width, y + 1))) is not None


def _segments(flags):
    """把布尔序列压成 [(start, end_inclusive, length), ...]。"""
    out, start = [], None
    for i, flag in enumerate(flags):
        if flag and start is None:
            start = i
        elif not flag and start is not None:
            out.append((start, i - 1, i - start))
            start = None
    if start is not None:
        out.append((start, len(flags) - 1, len(flags) - start))
    return out


def _merge_close(segs, max_gap):
    """把间距 ≤ max_gap 的相邻分段合并，使「一个词」不被拆成一个个字形。"""
    merged = []
    for start, end, _ in segs:
        if merged and start - merged[-1][1] - 1 <= max_gap:
            merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))
    return merged


def _dominant_ink_color(img, box):
    """区域内出现最多的墨迹颜色（抗锯齿边缘像素少，不会盖过正文色）。"""
    region = img.crop(box).convert("RGB")
    levels = list(img.convert("L").crop(box).getdata())
    counts = {}
    for level, pixel in zip(levels, region.getdata()):
        if level >= INK_LEVEL:
            continue
        counts[pixel] = counts.get(pixel, 0) + 1
    if not counts:
        return None
    return max(counts.items(), key=lambda kv: kv[1])[0]


def measure(path):
    """量测一张图的相机信息栏几何（参照图与自渲染结果用同一套口径）。"""
    img = Image.open(path).convert("RGB")
    width, height = img.size

    # 1) 条顶 = 最后一行「整行 ≥50% 是墨迹」的下一行（照片占满行宽，信息栏只有少量文字）
    #    注意：overlay 模式（信息栏叠在照片上）没有这样的边界，会得到 bar_height=0
    bar_top = height
    for y in range(height - 1, -1, -1):
        if _ink_count(img.crop((0, y, width, y + 1))) >= width * PHOTO_ROW_FILL:
            bar_top = y + 1
            break

    bar = img.crop((0, bar_top, width, height))
    bar_height = height - bar_top
    result = {
        "image": {
            "width": width, "height": height, "bar_top": bar_top, "bar_height": bar_height,
            "photo_height": bar_top,
            "bar_height_ratio_of_width": round(bar_height / width, 5) if width else 0,
        },
        "lines": {},
        "words": {},
        "divider": None,
    }
    if bar_height <= 0:
        return result

    # 2) 栏内列分段：找出「细高」的那条分隔线，其余为文字
    col_segs = _segments([_col_has_ink(bar, x) for x in range(width)])
    divider = None
    for start, end, seg_w in col_segs:
        box = _ink_bbox(bar.crop((start, 0, end + 1, bar_height)))
        if box is None:
            continue
        seg_h = box[3] - box[1]
        if seg_w <= 8 and seg_h >= bar_height * 0.4 and len(col_segs) > 1:
            divider = {"x0": start, "x1": end, "width": seg_w,
                       "y0": bar_top + box[1], "y1": bar_top + box[3] - 1, "height": seg_h,
                       "center_x": (start + end) / 2,
                       "center_x_ratio": round((start + end) / 2 / width, 5),
                       "height_ratio_of_bar": round(seg_h / bar_height, 5),
                       "color": "#%02x%02x%02x" % _dominant_ink_color(bar, (start, box[1], end + 1, box[3]))}
            break

    result["divider"] = divider
    split = divider["x0"] if divider else int(width * 0.6)
    regions = {"left": (0, split), "right": ((divider["x1"] + 1) if divider else split, width)}

    def describe(box, ox, oy):
        x0, y0, x1, y1 = ox + box[0], oy + box[1], ox + box[2] - 1, oy + box[3] - 1
        return {
            "x0": x0, "y0": y0, "x1": x1, "y1": y1,
            "w": x1 - x0 + 1, "h": y1 - y0 + 1,
            "h_ratio": round((y1 - y0 + 1) / width, 5),
            "margin_left_ratio": round(x0 / width, 5),
            "margin_right_ratio": round((width - 1 - x1) / width, 5),
            "center_y": (y0 + y1) / 2,
            "center_offset_from_bar_center": round((y0 + y1) / 2 - (bar_top + bar_height / 2), 2),
            "color": "#%02x%02x%02x" % _dominant_ink_color(img, (x0, y0, x1 + 1, y1 + 1)),
        }

    for name, (x0, x1) in regions.items():
        region = bar.crop((x0, 0, x1, bar_height))
        if _ink_bbox(region) is None:
            continue
        row_segs = _segments([_row_has_ink(region, y) for y in range(bar_height)])
        for i, (ry0, ry1, _) in enumerate(row_segs):
            line = region.crop((0, ry0, region.width, ry1 + 1))
            lbox = _ink_bbox(line)
            if lbox is None:
                continue
            key = f"{name}_line{i + 1}" if len(row_segs) > 1 else name
            result["lines"][key] = describe(lbox, x0, bar_top + ry0)
            # 词组划分：阈值取行高的 0.35 倍（≈ 一个空格宽度），高于它才算「另一个词」
            gap = max(6, int(0.35 * (lbox[3] - lbox[1])))
            words = _merge_close(_segments([_col_has_ink(line, x) for x in range(line.width)]), gap)
            result["words"][key] = [describe(_ink_bbox(line.crop((cx0, 0, cx1 + 1, line.height))),
                                            x0 + cx0, bar_top + ry0)
                                    for cx0, cx1 in words]
    return result


def compare(ref, got, tol):
    """行级 bbox + 分隔线 + 条几何逐项比对，返回不一致项列表。"""
    bad = []
    for field in ("bar_top", "bar_height"):
        if abs(ref["image"][field] - got["image"][field]) > tol:
            bad.append(f"条几何.{field}: 参照 {ref['image'][field]} vs 实得 {got['image'][field]}")
    for key in ("width", "height"):
        if ref["image"][key] != got["image"][key]:
            bad.append(f"画布.{key}: 参照 {ref['image'][key]} vs 实得 {got['image'][key]}")

    for key in sorted(set(ref["lines"]) | set(got["lines"])):
        a, b = ref["lines"].get(key), got["lines"].get(key)
        if a is None or b is None:
            bad.append(f"文字行 {key}: 有一侧缺失（参照 {'有' if a else '无'} / 实得 {'有' if b else '无'}）")
            continue
        for field in ("x0", "y0", "x1", "y1"):
            if abs(a[field] - b[field]) > tol:
                bad.append(f"文字行 {key}.{field}: 参照 {a[field]} vs 实得 {b[field]}")
        if a["color"] != b["color"]:
            bad.append(f"文字行 {key}.color: 参照 {a['color']} vs 实得 {b['color']}")

    da, db = ref["divider"], got["divider"]
    if (da is None) != (db is None):
        bad.append(f"分隔线: 参照 {'有' if da else '无'} / 实得 {'有' if db else '无'}")
    elif da and db:
        for field in ("x0", "x1", "y0", "y1"):
            if abs(da[field] - db[field]) > tol:
                bad.append(f"分隔线.{field}: 参照 {da[field]} vs 实得 {db[field]}")
        if da["color"] != db["color"]:
            bad.append(f"分隔线.color: 参照 {da['color']} vs 实得 {db['color']}")
    return bad


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    ref = measure(argv[1])
    print(json.dumps(ref, indent=2, ensure_ascii=False))
    if len(argv) >= 3:
        tol = float(argv[3]) if len(argv) > 3 else 3.0
        bad = compare(ref, measure(argv[2]), tol)
        print("\n比对结果:", "一致（容差 ±%gpx）" % tol if not bad else f"不一致（容差 ±%gpx）" % tol)
        for line in bad:
            print("  -", line)
        return 1 if bad else 0
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
