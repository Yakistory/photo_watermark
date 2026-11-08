"""EXIF 结构诊断 / 相机身份标签来源排查（针对「相机制造商/型号/日期不显示」这类问题）。

用法::

    python scripts/inspect_exif.py "C:\\path\\to\\photo.jpg" [更多图片...]

输出每张图的：
  1. piexif 能否直接解析（不能则说明 EXIF 结构里有死指针，历史行为是静默退回 Pillow 丢标签）
  2. 手写 IFD 解析结果：IFD0 / Exif IFD / IFD1 的原始表项（不过滤、不解释类型）
  3. 自愈（剔除死指针）后能否解析、剔除了什么
  4. 从缩略图 IFD(IFD1) 能找回哪些相机身份标签
  5. app.read_exif 的关键字段（含来源标记 source=ifd1）
  6. 信息栏四段文本（品牌型号 / 品牌 / 参数行 / 日期行）

不写任何文件、不改 uploads/，只读输入文件。
"""
import os
import sys

# Windows 控制台默认 GBK：特殊符号（⇒、…）会直接抛 UnicodeEncodeError，这里降级成 ?
try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT)

import piexif  # noqa: E402

import app as A  # noqa: E402

IDENTITY = {tag: label for tag, _key, label in A._CORE_IDENTITY_TAGS}


def _fmt(typ, cnt, value, byteorder, limit=56):
    """按 TIFF 类型展示值：有理数显示成 n/d、整型显示成数字、其余按文本/字节。"""
    if typ in (1, 3, 4, 6, 8, 9) and cnt:
        size = 1 if typ in (1, 6) else (2 if typ in (3, 8) else 4)
        if len(value) == size * cnt:
            return " ".join(str(int.from_bytes(value[i * size:(i + 1) * size], byteorder))
                            for i in range(min(cnt, 8))) + (" ..." if cnt > 8 else "")
    if typ in (5, 10) and cnt == 1 and len(value) == 8:
        return (f"{int.from_bytes(value[:4], byteorder)}/"
                f"{int.from_bytes(value[4:], byteorder)}")
    if isinstance(value, (bytes, bytearray)):
        text = bytes(value).rstrip(b"\x00")
        try:
            text = text.decode("utf-8")
        except UnicodeDecodeError:
            return f"<bytes {len(value)}>"
    else:
        text = str(value)
    text = text.replace("\n", " ")
    return text if len(text) <= limit else text[:limit] + "..."


def dump_tables(tiff, first):
    """打印 IFD0 / Exif IFD / IFD1 的原始表项（tag_id、类型、计数、值预览）。"""
    byteorder = "little" if tiff[:2] == b"II" else "big"
    ifd0, next_off = A._read_ifd(tiff, first)
    if not ifd0:
        print("    IFD0 无法解析（TIFF 头或第一个 IFD 偏移有问题）")
        return ifd0, next_off
    print(f"    IFD0（{len(ifd0)} 项）:")
    for tag, typ, cnt, value, _entry in ifd0:
        print(f"      {tag:>6} {IDENTITY.get(tag, ''):<6} type={typ} count={cnt}  "
              f"{_fmt(typ, cnt, value, byteorder)}")
    exif_ptr = next((int.from_bytes(v[:4], byteorder)
                     for t, _ty, _c, v, _e in ifd0 if t == 0x8769 and len(v) >= 4), None)
    if exif_ptr is not None:
        entries, _ = A._read_ifd(tiff, exif_ptr)
        print(f"    Exif IFD（{len(entries)} 项，偏移 {exif_ptr}）: "
              + ", ".join(str(t) for t, _ty, _c, _v, _e in entries))
        for t, _ty, _c, v, _e in entries:
            if t in (0xA005, 0x8825) and len(v) >= 4:
                target = int.from_bytes(v[:4], byteorder)
                ok = A._ifd_target_valid(tiff, target)
                print(f"      指针 {t:#06x} -> {target}  {'合法' if ok else '**死指针（指向被删数据）**'}")
    if next_off:
        entries, _ = A._read_ifd(tiff, next_off)
        print(f"    IFD1（缩略图 IFD，{len(entries)} 项）:")
        for tag, typ, cnt, value, _entry in entries:
            print(f"      {tag:>6} {IDENTITY.get(tag, ''):<6} type={typ} count={cnt}  "
                  f"{_fmt(typ, cnt, value, byteorder)}")
    return ifd0, next_off


def inspect(path):
    print("=" * 78)
    print(os.path.basename(path), f"({os.path.getsize(path):,} 字节)")
    with open(path, "rb") as f:
        raw = f.read()

    print("[1] piexif.load 直接解析")
    try:
        saved = piexif.load(raw)
        print(f"    OK：IFD0 {len(saved['0th'])} 项 / Exif {len(saved['Exif'])} 项 / "
              f"IFD1 {len(saved['1st'])} 项 / GPS {len(saved['GPS'])} 项")
        piexif_ok = True
    except Exception as exc:
        print(f"    抛错：{type(exc).__name__}: {str(exc).splitlines()[0]}")
        print("    => 历史行为会静默退回 Pillow 分支，非白名单标签（含 GPS）全部丢失")
        piexif_ok = False

    print("[2] 手写 IFD 解析（不解释类型，坏数据也读得动）")
    tiff, _tiff_start = A._first_exif_tiff(raw)
    if tiff is None:
        print("    没找到 APP1(Exif) 段")
        return
    _endian, first = A._tiff_layout(tiff)
    if first is None:
        print("    TIFF 头非法")
        return
    dump_tables(tiff, first)

    print("[3] 自愈（剔除指向被删数据的死指针）")
    repaired, removed = A._repair_exif_pointers(raw)
    if removed:
        print("    剔除：" + "、".join(removed))
        try:
            fixed = piexif.load(repaired)
            print(f"    自愈后 piexif.load OK：IFD0 {len(fixed['0th'])} 项 / Exif {len(fixed['Exif'])} 项 / "
                  f"IFD1 {len(fixed['1st'])} 项")
        except Exception as exc:
            print(f"    自愈后仍无法解析：{type(exc).__name__}")
    elif piexif_ok:
        print("    无需自愈（没有死指针）")
    else:
        print("    没找到可剔除的死指针 ⇒ 结构问题不在 Interop/GPS 指针上")

    print("[4] 从缩略图 IFD(IFD1) 可找回的相机身份标签")
    recovered = A._recover_core_identity(raw)
    print("    " + (str(recovered["display"]) if recovered["display"] else "无（IFD1 里没有、或主 IFD 本来就有）"))

    print("[5] app.read_exif 关键字段")
    exif_data = A.read_exif(raw)
    for key in ("相机制造商", "相机型号", "软件", "日期时间", "拍摄时间", "数字化时间",
                "焦距", "光圈值", "曝光时间", "ISO感光度"):
        info = exif_data.get(key)
        if info is None:
            print(f"    {key:<6} 缺失")
        else:
            print(f"    {key:<6} {info['formatted']!r} (ifd={info['ifd']}"
                  + (f", 来源={info['source']}" if info.get("source") else "") + ")")

    print("[6] 信息栏四段文本")
    left, center, params, date = A.build_exif_bar_text(exif_data)
    print(f"    左={left!r} 中={center!r} 右参数={params!r} 右日期={date!r}")


def main():
    paths = sys.argv[1:]
    if not paths:
        print(__doc__)
        return 2
    for path in paths:
        if not os.path.exists(path):
            print(f"找不到文件: {path}")
            continue
        try:
            inspect(path)
        except Exception as exc:
            print(f"诊断失败: {type(exc).__name__}: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
