"""批量水印内核：扫描 → 逐张权威合成 → 编码写盘 → 汇总报告。

与单张「下载图片」共用 app.py 的同一批渲染函数，所以**同一张图 + 同一份配置**，
批量产出与单张下载逐字节相同（`scripts/verify_watermark_features.py` 的 [16] 拿这条当锚点）。

三条铁律：
  1. **绝不写 uploads/**：图片水印 / 品牌贴图 / 字体一律按配置里的 id 只读引用
     （所以要复现同一套效果，把 uploads/、brands/、fonts/ 一起带过来就行）。
  2. **每张图用它自己的 EXIF**：走 payload 的内部键 `_exif_text`（app._wm_exif_text），
     不共用 uploads/original.jpg 那份 —— 一个文件夹里不同相机拍的图各自出各自的参数行。
  3. **一张坏图不连坐**：逐张 try/except，失败原因写进这一项，其余图照跑。

两个壳共用本模块（逻辑只此一份，界面/命令行都只是它的传声筒）：
  - scripts/batch_watermark.py（命令行）
  - scripts/batch_panel.py（Tkinter 小窗）

不依赖 Flask 请求上下文，也不需要预览服务在跑。
"""
import datetime
import io
import json
import os
import sys
import time

from PIL import Image, ImageOps

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

import app as A  # noqa: E402

# 认得出的图片扩展名（Pillow 能解码的常见格式；HEIC/RAW 不在内，会给出明确错误而不是 500）
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff")
DEFAULT_SUFFIX = "_wm"
REPORT_NAME = "batch_report.json"

# 界面/命令行显示用的水印类型中文名（与前端 CFG_TYPE_LABEL 同款说法）
TYPE_LABELS = {"text": "文字水印", "image": "图片水印", "exif": "信息栏",
               "frame": "画框水印", "blur": "背景模糊"}
CONFIG_SOURCES = (("current", "当前自动保存的那份（configs/current.json）"),
                  ("slot", "命名快照（configs/<id>.json）"),
                  ("file", "从文件读（任意 .json，含导出的配置）"))


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _norm(path):
    """比较路径用：绝对化 + 大小写归一（Windows 上 D:\\a 与 d:\\A 是同一个地方）。"""
    return os.path.normcase(os.path.abspath(str(path or "")))


def _is_under(path, folder):
    """path 是否在 folder 里面（folder 自己不算）。"""
    p, f = _norm(path), _norm(folder).rstrip("\\/")
    return bool(f) and p.startswith(f + os.sep)


def _walk_files(root, recursive):
    """目录 → 文件（顺序稳定：目录名与文件名都排序）。"""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for name in sorted(filenames):
            yield os.path.join(dirpath, name)
        if not recursive:
            break


def _is_tmp_name(name):
    return name.startswith(".") or name.startswith("~$")


def _now_text(ts=None):
    return datetime.datetime.fromtimestamp(ts or time.time()).strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# 1. 扫描输入
# ---------------------------------------------------------------------------
def scan_images(inputs, recursive=True, suffix=DEFAULT_SUFFIX, exclude_dirs=()):
    """收集待处理的图片。

    → {"files": [...], "roots": {文件: 它的输入根目录}, "stats": {...}, "missing": [...]}

    - 目录：recursive=True 递归；False 只看第一层
    - 跳过「名字已带后缀」的成品（同一个目录反复跑，不会把自己的产出当原料）
    - 跳过 exclude_dirs（一般是输出目录）里的文件、隐藏文件与 Office 临时文件
    - files 按路径排序：同一批输入每次得到同一个处理顺序，报告能逐项对照
    """
    files, roots, missing = [], {}, []
    stats = {"dirs": 0, "images": 0, "products_skipped": 0, "other_skipped": 0}
    excludes = [_norm(d) for d in (exclude_dirs or ()) if str(d or "").strip()]
    suffix = str(suffix or "")

    for raw in (inputs or []):
        path = str(raw or "").strip()
        if not path:
            continue
        if os.path.isfile(path):
            candidates = [(os.path.abspath(path), os.path.dirname(os.path.abspath(path)))]
        elif os.path.isdir(path):
            stats["dirs"] += 1
            root = os.path.abspath(path)
            candidates = [(p, root) for p in _walk_files(root, recursive)]
        else:
            missing.append(path)
            continue

        for file_path, root in candidates:
            name = os.path.basename(file_path)
            ext = os.path.splitext(name)[1].lower()
            if any(_norm(file_path) == e or _is_under(file_path, e) for e in excludes):
                stats["other_skipped"] += 1
                continue
            if ext not in IMAGE_EXTS or _is_tmp_name(name):
                stats["other_skipped"] += 1
                continue
            if suffix and os.path.splitext(name)[0].lower().endswith(suffix.lower()):
                stats["products_skipped"] += 1
                continue
            files.append(os.path.abspath(file_path))
            roots[os.path.abspath(file_path)] = root

    files.sort(key=lambda p: p.lower())
    stats["images"] = len(files)
    return {"files": files, "roots": roots, "stats": stats, "missing": missing}



# ---------------------------------------------------------------------------
# 2. 配置（与 /api/config/* 同一套规范化：越界夹紧、未知键丢弃）
# ---------------------------------------------------------------------------
def config_path_for(slot=None):
    """current / 命名快照的路径；slot 非法返回 ""。"""
    if slot:
        return A._config_path(str(slot)) or ""
    return os.path.join(A.CONFIG_DIR, "current.json")


def default_config_path():
    """默认配置 = 页面上自动保存的那份（configs/current.json）。"""
    return config_path_for()


def _cfg_error(path, message):
    return {"ok": False, "error": message, "path": path or "", "config": None,
            "invalid": [], "warnings": [], "type": "", "type_label": "",
            "preset": "", "preset_name": "", "missing_images": [],
            "missing_brands": [], "missing_fonts": []}


def _font_unavailable(font_id):
    """字体 id 在本机用不了？系统字体要文件真存在，上传字体要 fonts/ 里真有那个文件。

    直接用 app._resolve_font_choice（信息栏渲染用的就是它，口径一致）：那个 id
    解析不出字体就一定是「本机没有」。
    """
    fid = str(font_id or "").strip()
    if not fid:
        return False
    return A._resolve_font_choice(fid) is None


def load_config(config_path=None, slot=None):
    """读一份配置并规范化。**读不成就一个文件都不该处理**（配置是唯一的风格来源）。

    → {"ok","error","path","config","invalid","warnings","type","type_label",
       "preset","preset_name","missing_images","missing_brands","missing_fonts"}
    """
    if slot:
        path = config_path_for(slot)
        if not path:
            return _cfg_error("", f"快照 id 不合法或不存在：{slot}")
    else:
        path = str(config_path or default_config_path()).strip()
    if not path or not os.path.isfile(path):
        return _cfg_error(path, f"找不到配置文件：{path or '(空)'}")
    try:
        with open(path, "r", encoding="utf-8-sig") as f:   # 认 BOM（导出的文件带过 BOM）
            raw = json.load(f)
    except Exception as exc:
        return _cfg_error(path, f"配置文件不是合法 JSON：{exc}")
    if not isinstance(raw, dict):
        return _cfg_error(path, "配置文件内容不是 JSON 对象")

    version = A._safe_int(raw.get("version"), A._CONFIG_VERSION)
    if version != A._CONFIG_VERSION:
        return _cfg_error(path, f"配置版本不兼容：文件是 v{version}，"
                                f"本程序只认 v{A._CONFIG_VERSION}")

    cfg, invalid, warnings = A._normalize_config(raw)
    preset_id = str(cfg.get("preset") or "")
    preset = A._load_presets().get(preset_id) or {}
    params = cfg.get("params") if isinstance(cfg.get("params"), dict) else {}

    missing_images, missing_brands, missing_fonts = [], [], []
    for item in (cfg.get("image") or {}).get("items") or []:
        wm_path = A._watermark_path(item.get("id"))
        if not (wm_path and os.path.exists(wm_path)):
            missing_images.append(str(item.get("id")))
    for key, value in params.items():
        if key.endswith("_image") and isinstance(value, str) and value.strip():
            brand_path = A._brand_path(value)
            if not (brand_path and os.path.exists(brand_path)):
                missing_brands.append(f"{key}={value}")
        if (key == "font_family" or key.endswith("_font")) \
                and isinstance(value, str) and value.strip():
            if _font_unavailable(value):
                missing_fonts.append(f"{key}={value}")

    wm_type = str(cfg.get("type") or "text")
    return {
        "ok": True, "error": "", "path": path, "config": cfg,
        "invalid": sorted(set(str(k) for k in invalid)),
        "warnings": [str(w) for w in warnings],
        "type": wm_type, "type_label": TYPE_LABELS.get(wm_type, wm_type),
        "preset": preset_id, "preset_name": str(preset.get("name") or ""),
        "missing_images": missing_images,
        "missing_brands": sorted(set(missing_brands)),
        "missing_fonts": sorted(set(missing_fonts)),
    }


def describe_config(loaded):
    """一行配置摘要（界面状态行 / 命令行都显示它）。"""
    if not loaded.get("ok"):
        return loaded.get("error") or "配置读取失败"
    text = loaded.get("type_label") or loaded.get("type") or "?"
    if loaded.get("preset_name"):
        text += f" · 模板「{loaded['preset_name']}」"
    elif loaded.get("preset"):
        text += f" · 模板 {loaded['preset']}（不在模板库里）"
    return text


def config_reference_notes(loaded):
    """配置里引用的资源缺了哪些 → [(level, 文案)]；error = 这一批会白跑或明显走样。

    绝不静默：贴图/字体缺了会回落成文字或自动挑体，用户必须看得见这件事。
    """
    if not loaded.get("ok"):
        return [("error", loaded.get("error") or "配置读取失败")]
    notes = []
    if loaded.get("missing_images"):
        notes.append(("error", "配置引用的图片水印不在 uploads/ 里："
                               + "、".join(loaded["missing_images"])
                               + " —— 这一批会全部失败（把 uploads/ 里的水印图带过来即可）"))
    if loaded.get("missing_brands"):
        notes.append(("warning", "配置引用的品牌贴图不在 brands/ 里："
                                 + "、".join(loaded["missing_brands"])
                                 + " —— 这些槽位会回落成文字"))
    if loaded.get("missing_fonts"):
        notes.append(("warning", "配置指定的字体在本机找不到："
                                 + "、".join(loaded["missing_fonts"])
                                 + " —— 会回落成自动挑体"))
    for warning in loaded.get("warnings") or []:
        if warning.startswith("preset_missing:"):
            notes.append(("warning", "模板不在模板库里（"
                                     f"{warning.split(':', 1)[1].strip()}）："
                                     "版式会掉回默认值，建议先确认模板"))
        else:
            notes.append(("warning", warning))
    if loaded.get("invalid"):
        notes.append(("warning", "配置里有被丢弃/夹紧的参数："
                                 + "、".join(loaded["invalid"])))
    return notes


# ---------------------------------------------------------------------------
# 3. 输出规划（跳过已存在 / 覆盖 / 拒绝自己盖自己）
# ---------------------------------------------------------------------------
def sniff_format(path):
    """读文件头拿真实格式（Pillow 只读头部不解码像素）；读不动返回 ""。"""
    try:
        with Image.open(path) as im:
            return str(im.format or "").lower()
    except Exception:
        return ""


def input_root_of(scan, src):
    """这个文件属于哪个输入根目录（递归时用来在输出目录里保留子目录结构）。"""
    roots = (scan or {}).get("roots") or {}
    for key in (src, os.path.abspath(src)):
        if roots.get(key):
            return roots[key]
    return os.path.dirname(os.path.abspath(src))


def plan_output(src, out_root, suffix, ext, force=False, input_root=None):
    """算一张图的落点 → {"action": "create|skip|overwrite|error", "dest", "error"}。

    - out_root 为空 = 写回**输入图旁边**（`名字 + suffix + 新扩展名`）
    - 给了 out_root 就保留「相对输入根目录」的子目录结构
      ⇒ 两个子目录下的同名图不会互相盖掉
    - 目标与源同一个文件 → 直接报错：宁可不出，也绝不覆盖原图
    - 已存在：默认跳过（重跑安全），force=True 才覆盖
    """
    stem = os.path.splitext(os.path.basename(src))[0]
    base = f"{stem}{suffix}{ext}"
    out_root = str(out_root or "").strip()
    if out_root:
        rel_dir = ""
        if input_root:
            try:
                rel = os.path.relpath(os.path.dirname(os.path.abspath(src)),
                                      os.path.abspath(input_root))
            except ValueError:                      # 跨盘符（Windows）
                rel = ""
            if rel and rel != "." and not rel.startswith(".."):
                rel_dir = rel
        dest = os.path.join(out_root, rel_dir, base)
    else:
        dest = os.path.join(os.path.dirname(os.path.abspath(src)), base)

    result = {"action": "create", "dest": dest, "error": ""}
    if _norm(dest) == _norm(src):
        result.update({"action": "error",
                       "error": "输出路径与输入文件相同（后缀不能为空，输出目录也不能指回原图）"})
        return result
    if os.path.exists(dest):
        result["action"] = "overwrite" if force else "skip"
    return result


def unique_dest(dest, taken):
    """同一批里两张图算到同一个落点时，追加 _2/_3…（不静默覆盖）。"""
    if _norm(dest) not in taken:
        taken.add(_norm(dest))
        return dest
    stem, ext = os.path.splitext(dest)
    index = 2
    while True:
        candidate = f"{stem}_{index}{ext}"
        if _norm(candidate) not in taken:
            taken.add(_norm(candidate))
            return candidate
        index += 1


def check_output_dir(out_root, create=True):
    """输出目录能不能写 → (ok, error)。

    create=True（真的开跑前）：不存在就建。
    create=False（试运行）：**不建目录、不写探针**；目录不存在就看最近的已存在上级
    能不能写（试运行不该有任何落盘副作用），真开跑时的写失败还会逐张报出来。
    """
    out_root = str(out_root or "").strip()
    if not out_root:
        return True, ""                     # 写到输入图旁边
    if not os.path.isdir(out_root):
        if create:
            try:
                os.makedirs(out_root, exist_ok=True)
            except Exception as exc:
                return False, f"无法创建输出文件夹：{exc}"
        else:
            parent = out_root
            while parent and not os.path.isdir(parent):
                parent = os.path.dirname(parent)
            if not parent:
                return False, f"输出文件夹不存在，也找不到可用的上级目录：{out_root}"
            if not os.access(parent, os.W_OK):
                return False, f"上级目录不可写：{parent}"
            return True, ""
    probe = os.path.join(out_root, f".wm_write_test_{os.getpid()}")
    try:
        with open(probe, "wb") as f:
            f.write(b"ok")
        os.remove(probe)
    except Exception as exc:
        return False, f"输出文件夹不可写：{exc}"
    return True, ""


# ---------------------------------------------------------------------------
# 4. 逐张渲染（与单张「下载图片」同一批函数 ⇒ 同图同配置逐字节相同）
# ---------------------------------------------------------------------------
def build_options(inputs, out_root="", suffix=DEFAULT_SUFFIX, force=False,
                  fmt="", quality=None, recursive=True, dry_run=False):
    """界面/命令行/测试共用的一份选项（默认值只写一次）。"""
    return {
        "inputs": [str(i) for i in (inputs or []) if str(i or "").strip()],
        "out_root": str(out_root or "").strip(),
        "suffix": DEFAULT_SUFFIX if suffix is None else str(suffix),
        "force": bool(force),
        "format": str(fmt or "").strip().lower(),
        "quality": None if quality in (None, "") else int(quality),
        "recursive": bool(recursive),
        "dry_run": bool(dry_run),
    }


def scan_for(options):
    """按 options 扫描输入（输出目录自动排除：重跑不会把成品当原料）。"""
    options = options or {}
    out_root = str(options.get("out_root") or "").strip()
    return scan_images(options.get("inputs"),
                       recursive=bool(options.get("recursive", True)),
                       suffix=str(options.get("suffix") if options.get("suffix") is not None
                                  else DEFAULT_SUFFIX),
                       exclude_dirs=[out_root] if out_root else ())


def render_one(image_bytes, payload, export, source_format=""):
    """一张图 → 编码好的字节。

    payload 由 `A._config_to_wm_payload(cfg)` 造好（整批复用同一份），本函数只补上
    **这张图自己的 EXIF**（内部键 `_exif_text`）—— 信息栏 / 画框四行因此逐张不同，
    而不是共用 uploads/original.jpg 那一张的。

    export = {"format": "jpeg|png|original", "quality": 1~100}
    → {"ok","data","fmt","ext","width","height","ms","error","warnings","applied"}
    """
    started = time.perf_counter()
    out = {"ok": False, "data": None, "fmt": "", "ext": "", "width": 0, "height": 0,
           "ms": 0, "error": "", "warnings": [], "applied": {}}

    def _done():
        out["ms"] = int(round((time.perf_counter() - started) * 1000))
        return out

    try:
        raw = Image.open(io.BytesIO(image_bytes))
        raw.load()
    except Exception:
        out["error"] = "无法解析图片（HEIC/RAW 等编码暂不支持，或文件已损坏）"
        return _done()

    try:
        img = ImageOps.exif_transpose(raw) or raw
        if img.mode != "RGBA":
            img = img.convert("RGBA")
    except Exception as exc:
        out["error"] = f"图片像素读取失败：{exc}"
        return _done()
    out["width"], out["height"] = img.width, img.height

    export = export if isinstance(export, dict) else {}
    fmt, ext, keep_alpha = A._export_plan_for(export.get("format"),
                                              source_format or raw.format or "")
    quality = int(A._clamp(A._safe_int(export.get("quality"), A._EXPORT_DEFAULT_QUALITY), 1, 100))

    # 每张图自己的 EXIF → 信息栏 / 画框四行
    wm_payload = dict(payload or {})
    try:
        wm_payload["_exif_text"] = list(A.build_exif_bar_text(A.read_exif(image_bytes)))
    except Exception:
        wm_payload.pop("_exif_text", None)      # 读不出来就照旧用 uploads 那份

    applied = {}
    try:
        result = A.apply_watermark(img, wm_payload, info=applied)
    except Exception as exc:
        out["applied"], out["error"] = applied, f"合成失败：{exc}"
        return _done()
    out["applied"] = applied
    if result is None:
        out["error"] = "找不到配置引用的图片水印（uploads/ 里没有对应文件）"
        return _done()

    # 像素已经摆正 ⇒ Orientation 归一成 1（与上传路径同款），否则看图软件会再转一次
    try:
        exif_bytes = A._canonical_exif_for_save(image_bytes, orientation=1)
    except Exception:
        exif_bytes = None
        out["warnings"].append("EXIF 解析失败，本次导出不带 EXIF")

    buf = io.BytesIO()
    try:
        A._save_output(buf, result if keep_alpha else A._flatten_rgb(result),
                       fmt, quality, exif_bytes)
    except Exception:
        # 与单张下载同一条兜底链：编码失败退 JPEG，再不行连 EXIF 也退掉（绝不给空文件）
        out["warnings"].append(f"按 {fmt} 编码失败，已回退 JPEG")
        fmt, ext = "jpeg", ".jpg"
        buf = io.BytesIO()
        try:
            A._save_output(buf, A._flatten_rgb(result), fmt, quality, exif_bytes)
        except Exception:
            buf = io.BytesIO()
            A._save_output(buf, A._flatten_rgb(result), fmt, quality, None)
            if exif_bytes:
                out["warnings"].append("EXIF 写入失败，本次导出不带 EXIF")

    data = buf.getvalue()
    if not data:
        out["error"] = "编码结果为空（编码器异常）"
        return _done()
    out.update({"ok": True, "data": data, "fmt": fmt, "ext": ext})
    return _done()


# ---------------------------------------------------------------------------
# 5. 开跑前的体检（这一层挡住「跑了才发现白跑」）
# ---------------------------------------------------------------------------
def validate_plan(scan, loaded, options):
    """→ [(level, 文案)]，level ∈ error / warning / info；有 error 就不该开跑。

    error：配置读不成、0 张图、输出目录不可写、后缀为空又指回原图（会覆盖原图）。
    warning：资源缺失会回落、路径不存在、有参数被丢弃。
    """
    options = options or {}
    notes = list(config_reference_notes(loaded))

    if scan.get("missing"):
        notes.append(("warning", "这些输入路径不存在，已忽略：" + "、".join(scan["missing"])))
    stats = scan.get("stats") or {}
    if not scan.get("files"):
        notes.append(("error", "没有找到可处理的图片（支持 " + " ".join(IMAGE_EXTS) + "）"))
    else:
        detail = f"待处理 {len(scan['files'])} 张"
        if stats.get("dirs"):
            detail += f"，来自 {stats['dirs']} 个文件夹"
        if stats.get("products_skipped"):
            detail += f"；跳过成品 {stats['products_skipped']} 张"
        if stats.get("other_skipped"):
            detail += f"；忽略非图片 {stats['other_skipped']} 个"
        notes.append(("info", detail))
    if options.get("dry_run"):
        notes.append(("info", "试运行：照常渲染（能提前暴露坏图/缺水印），只是不写盘"))

    out_root = str(options.get("out_root") or "").strip()
    suffix = str(options.get("suffix") if options.get("suffix") is not None else "")
    inputs = [os.path.abspath(str(i)) for i in (options.get("inputs") or [])
              if os.path.isdir(str(i))]
    if out_root:
        ok, error = check_output_dir(out_root, create=False)
        if not ok:
            notes.append(("error", error))
        elif any(_is_under(out_root, i) for i in inputs):
            notes.append(("info", "输出文件夹在输入文件夹里面：输出目录已自动排除，"
                                  "重跑不会把成品当原料"))
    if not suffix:
        if not out_root or any(_norm(out_root) == _norm(i) for i in inputs):
            notes.append(("error", "后缀为空且输出目录与输入目录相同 ⇒ 会覆盖原图，不予执行"))
        else:
            notes.append(("warning", "后缀为空：输出文件与输入图同名（只是换了目录）"))
    return notes


# ---------------------------------------------------------------------------
# 6. 跑一批 + 汇总报告
# ---------------------------------------------------------------------------
def _resolve_export(loaded, options):
    """导出选项：命令行/界面显式给的值优先，否则用配置里存的那份。"""
    cfg_export = (loaded.get("config") or {}).get("export") or {}
    fmt = str(options.get("format") or "").strip().lower() or str(cfg_export.get("format") or "jpeg")
    quality = options.get("quality")
    if quality in (None, ""):
        quality = cfg_export.get("quality")
    return {"format": fmt,
            "quality": int(A._clamp(A._safe_int(quality, A._EXPORT_DEFAULT_QUALITY), 1, 100))}


def run_batch(scan, loaded, options, on_event=None, should_cancel=None):
    """跑一批（逐张容错：一张失败不影响其余）。

    options 见 build_options()；另外认 inputs（体检用）。
    on_event(kind, payload)：kind ∈ "start"（{total, options}）/ "item"（该项 dict）
      / "done"（summary）—— 界面靠它刷进度，命令行靠它逐行打印。
    should_cancel() 返回 True ⇒ 处理完当前这张就停，剩下的记成 cancelled。

    → summary：{"ok","total","done","ok_count","failed","skipped","cancelled","bytes",
      "seconds","dry_run","items","config","export","output_dir","suffix","started_at"}
    """
    cfg = loaded.get("config")
    if cfg is None:
        raise ValueError(loaded.get("error") or "配置读取失败，不能开跑")
    options = options or {}
    export = _resolve_export(loaded, options)
    payload = A._config_to_wm_payload(cfg)

    files = list((scan or {}).get("files") or [])
    total = len(files)
    out_root = str(options.get("out_root") or "").strip()
    suffix = str(options.get("suffix") if options.get("suffix") is not None else DEFAULT_SUFFIX)
    force = bool(options.get("force"))
    dry_run = bool(options.get("dry_run"))

    started = time.time()
    items, taken = [], set()
    counts = {"ok": 0, "failed": 0, "skipped": 0, "cancelled": 0, "bytes": 0}
    if on_event:
        on_event("start", {"total": total, "options": options})

    for index, src in enumerate(files, 1):
        if should_cancel and should_cancel():
            counts["cancelled"] = total - index + 1
            break
        row = {"index": index, "src": src, "dest": "", "ok": False, "action": "",
               "error": "", "warnings": [], "fmt": "", "bytes": 0,
               "width": 0, "height": 0, "ms": 0, "applied": {}}
        try:
            source_format = sniff_format(src)
            fmt_name, ext, _keep = A._export_plan_for(export["format"], source_format)
            row["fmt"] = fmt_name
            plan = plan_output(src, out_root, suffix, ext, force,
                               input_root_of(scan, src))
            if plan["action"] == "error":
                raise RuntimeError(plan["error"])
            row["dest"] = unique_dest(plan["dest"], taken)
            row["action"] = plan["action"]
            if plan["action"] == "skip":
                # 已存在 + 没开覆盖 ⇒ 直接跳过，连渲染都不做（重跑才够快）
                row.update({"ok": True, "error": ""})
                counts["skipped"] += 1
                if on_event:
                    on_event("item", row)
                items.append(row)
                continue

            with open(src, "rb") as f:
                image_bytes = f.read()
            if not image_bytes:
                raise RuntimeError("文件是空的（0 字节）")
            rendered = render_one(image_bytes, payload, export, source_format)
            row["warnings"] = list(rendered["warnings"])
            row["applied"] = rendered["applied"]
            row["width"], row["height"] = rendered["width"], rendered["height"]
            row["ms"], row["fmt"] = rendered["ms"], rendered["fmt"]
            if not rendered["ok"]:
                raise RuntimeError(rendered["error"] or "合成失败")

            if dry_run:
                row["action"] = f"dry-run:{plan['action']}"
                row["bytes"] = len(rendered["data"])
            else:
                dest_dir = os.path.dirname(row["dest"])
                if dest_dir:
                    os.makedirs(dest_dir, exist_ok=True)
                with open(row["dest"], "wb") as f:
                    f.write(rendered["data"])
                row["bytes"] = os.path.getsize(row["dest"])
                if not row["bytes"]:
                    raise RuntimeError("写出的文件是 0 字节（磁盘满？）")
            row["ok"] = True
            counts["ok"] += 1
            counts["bytes"] += row["bytes"]
        except Exception as exc:
            row["error"] = str(exc) or exc.__class__.__name__
            counts["failed"] += 1
            # 0 字节的半成品绝不留下（否则下次重跑会被当成有效成品跳过）
            if not dry_run and row["dest"] and os.path.exists(row["dest"]):
                try:
                    if os.path.getsize(row["dest"]) == 0:
                        os.remove(row["dest"])
                except OSError:
                    pass
        items.append(row)
        if on_event:
            on_event("item", row)

    summary = {
        "ok": counts["failed"] == 0,
        "total": total, "done": len(items),
        "ok_count": counts["ok"], "failed": counts["failed"],
        "skipped": counts["skipped"], "cancelled": counts["cancelled"],
        "bytes": counts["bytes"], "seconds": round(time.time() - started, 2),
        "dry_run": dry_run, "items": items, "started_at": _now_text(started),
        "output_dir": out_root, "suffix": suffix, "export": dict(export),
        "config": {"path": loaded.get("path"), "type": loaded.get("type"),
                   "type_label": loaded.get("type_label"),
                   "preset": loaded.get("preset"), "preset_name": loaded.get("preset_name")},
    }
    if on_event:
        on_event("done", summary)
    return summary


# ---------------------------------------------------------------------------
# 7. 汇总文案 / 报告 / 退出码（两个壳共用，说法保持一致）
# ---------------------------------------------------------------------------
def exit_code(summary, loaded=None):
    """命令行退出码：0 全部成功 / 1 部分失败 / 3 全都没成。

    （2 = 参数或配置错误，由命令行壳自己给：那种情况根本没开跑。）
    """
    if loaded is not None and not loaded.get("ok"):
        return 2
    if not summary or not summary.get("total"):
        return 2
    if not summary.get("failed"):
        return 0
    if summary.get("ok_count", 0) + summary.get("skipped", 0) == 0:
        return 3
    return 1


def format_item_line(row, total=None):
    """一行进度文案（命令行逐行打印 / 界面日志，说的是同一句话）。"""
    name = os.path.basename(row.get("src") or "")
    head = f"[{row.get('index')}/{total}]" if total else f"[{row.get('index')}]"
    if row.get("error"):
        return f"{head} {name} 失败：{row['error']}"
    action = str(row.get("action") or "")
    if action.startswith("dry-run"):
        state = "试运行 OK"
    elif action == "skip":
        state = "已存在，跳过"
    elif row.get("ok"):
        state = "完成"
    else:
        state = "未处理"
    parts = [p for p in (state,
                         f"{row.get('width')}×{row.get('height')}" if row.get("width") else "",
                         row.get("fmt"),
                         f"{row.get('bytes', 0) / 1024:.0f}KB" if row.get("bytes") else "",
                         f"{row.get('ms')}ms" if row.get("ms") else "") if p]
    line = f"{head} {name} → " + " · ".join(parts)
    if row.get("dest") and not action.startswith("dry-run"):
        line += f"  → {row['dest']}"
    for warning in row.get("warnings") or []:
        line += f"\n      ！{warning}"
    return line


def format_summary_lines(summary):
    """汇总文案（界面进度条下面那段 / 命令行结尾那段）。"""
    if not summary:
        return ["没有可汇总的结果"]
    head = (f"成功 {summary.get('ok_count', 0)} / 失败 {summary.get('failed', 0)} / "
            f"跳过 {summary.get('skipped', 0)}")
    if summary.get("cancelled"):
        head += f" / 取消 {summary['cancelled']}"
    head += (f"，共 {summary.get('total', 0)} 张，用时 {summary.get('seconds', 0):.1f}s，"
             f"产出 {summary.get('bytes', 0) / 1024 / 1024:.1f}MB")
    lines = [head]
    if summary.get("dry_run"):
        lines.append("（试运行：没有写盘；「成功」= 这些图能正常出片）")
    if summary.get("output_dir"):
        lines.append(f"输出文件夹：{summary['output_dir']}")
    for row in summary.get("items") or []:
        if row.get("error"):
            lines.append(f"失败 {os.path.basename(row.get('src') or '')}：{row['error']}")
    return lines


def write_report(summary, path):
    """写报告 JSON（逐张 src/dest/失败原因/回显都在里面）→ (ok, error)。"""
    path = str(path or "").strip()
    if not path:
        return False, "没有指定报告文件"
    data = {
        "app": getattr(A, "_CONFIG_APP", "camera-watermark"),
        "kind": "batch-watermark-report",
        "version": 1,
        "generated_at": _now_text(),
        "config": summary.get("config"), "export": summary.get("export"),
        "output_dir": summary.get("output_dir"), "suffix": summary.get("suffix"),
        "counts": {key: summary.get(key) for key in
                   ("total", "ok_count", "failed", "skipped", "cancelled",
                    "bytes", "seconds", "dry_run", "ok", "started_at")},
        "items": [{"index": row.get("index"), "src": row.get("src"), "dest": row.get("dest"),
                   "ok": bool(row.get("ok")), "action": row.get("action"),
                   "error": row.get("error"), "warnings": row.get("warnings") or [],
                   "format": row.get("fmt"), "bytes": row.get("bytes"),
                   "width": row.get("width"), "height": row.get("height"),
                   "ms": row.get("ms"), "applied": row.get("applied") or {}}
                  for row in (summary.get("items") or [])],
    }
    try:
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, default=str)
    except Exception as exc:
        return False, f"报告写入失败：{exc}"
    return True, ""


# ---------------------------------------------------------------------------
# 8. 一网打尽：体检 → 扫描 → 跑（界面/命令行都直接调它，少写一遍同样的顺序）
# ---------------------------------------------------------------------------
def run_from_options(loaded, options, on_event=None, should_cancel=None):
    """扫描输入 + 体检 + 跑完，返回 (scan, notes, summary)；notes 里有 error 时不开跑。

    顺序是契约：先扫（含排除输出目录）→ 体检（配置/0 张/可写/覆盖原图）→ 才跑。
    """
    scan = scan_for(options)
    notes = validate_plan(scan, loaded, options)
    if any(level == "error" for level, _msg in notes):
        return scan, notes, None
    if on_event:
        on_event("notes", {"notes": notes})
    summary = run_batch(scan, loaded, options, on_event=on_event,
                        should_cancel=should_cancel)
    return scan, notes, summary


if __name__ == "__main__":
    # 本文件是「内核」：被两个壳 import。直接跑它只打印用法，不做别的事。
    print(__doc__)
    print("命令行入口: python scripts/batch_watermark.py <图片或文件夹...>")
    print("图形界面入口: scripts/batch_panel.bat（或 python scripts/batch_panel.py）")


