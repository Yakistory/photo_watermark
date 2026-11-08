#!/usr/bin/env python3
"""JSON bridge between the DSH camera-watermark plugin and the Flask engine.

Protocol: ``python bridge.py <command>`` reads one JSON object from stdin and
writes exactly one JSON object to stdout. Diagnostics go to stderr only, so a
Host can parse stdout without stripping log lines.

The engine (``engine/app.py`` + ``engine/scripts/batch_core.py``) is vendored
unchanged. This bridge only adds file-oriented entry points, reusing the same
normalization / rendering / EXIF functions the web UI uses, so an agent's
output is byte-identical to the studio's "download image" path.
"""

from __future__ import annotations

import io
import json
import os
import sys
import time
import traceback

PACKAGE = os.path.dirname(os.path.abspath(__file__))
ENGINE = os.path.join(PACKAGE, "engine")
SCRIPTS = os.path.join(ENGINE, "scripts")
for _p in (ENGINE, SCRIPTS):
    if _p not in sys.path:
        sys.path.insert(0, _p)

_CACHE = {}
_MAX_INPUT_BYTES = 300 * 1024 * 1024
_CORE_FIELDS = (
    "相机制造商",
    "相机型号",
    "软件",
    "日期时间",
    "拍摄时间",
    "数字化时间",
)
_TYPE_LABELS = {
    "text": "文字水印",
    "image": "图片水印",
    "exif": "信息栏",
    "frame": "画框水印",
    "blur": "背景模糊",
}


def _log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _load_engine():
    """Import the vendored engine once; raise a readable error if deps are missing."""
    if "app" in _CACHE:
        return _CACHE["app"], _CACHE["batch"]
    try:
        import app as app_module  # noqa: WPS433 - deliberate late import
        import batch_core as batch_module  # noqa: WPS433
    except Exception as exc:  # pragma: no cover - environment dependent
        raise RuntimeError(
            "无法加载相机水印引擎：%s。请确认 Python 已安装 flask/Pillow/piexif"
            "（pip install -r engine/requirements.txt）。" % exc
        ) from exc
    _CACHE["app"] = app_module
    _CACHE["batch"] = batch_module
    return app_module, batch_module


def _abs(path: str) -> str:
    return os.path.abspath(os.path.expanduser(str(path or "")))


def _read_input(path: str) -> bytes:
    target = _abs(path)
    if not os.path.isfile(target):
        raise ValueError("找不到输入图片：%s" % target)
    size = os.path.getsize(target)
    if size <= 0:
        raise ValueError("输入图片是空文件：%s" % target)
    if size > _MAX_INPUT_BYTES:
        raise ValueError("输入图片超过 300MB，拒绝处理：%s" % target)
    with open(target, "rb") as handle:
        return handle.read()


def _ensure_parent(path: str) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)


def _unique_path(path: str) -> str:
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    index = 2
    while True:
        candidate = "%s_%d%s" % (stem, index, ext)
        if not os.path.exists(candidate):
            return candidate
        index += 1


def _resolve_output(input_path: str, output, suffix: str, ext: str, overwrite: bool) -> str:
    """Choose a destination, never silently overwriting the source image."""
    target = str(output or "").strip()
    if target:
        target = _abs(target)
        if os.path.isdir(target) or target.endswith(os.sep) or target.endswith("/"):
            target = os.path.join(target, os.path.splitext(os.path.basename(input_path))[0] + suffix + ext)
        if os.path.splitext(target)[1] == "":
            target += ext
        if os.path.normcase(target) == os.path.normcase(_abs(input_path)):
            raise ValueError("输出路径与输入图片相同，拒绝覆盖原图")
        _ensure_parent(target)
        if os.path.exists(target) and not overwrite:
            target = _unique_path(target)
        return target
    target = os.path.join(
        os.path.dirname(_abs(input_path)),
        os.path.splitext(os.path.basename(input_path))[0] + suffix + ext,
    )
    if os.path.normcase(target) == os.path.normcase(_abs(input_path)):
        raise ValueError("输出路径与输入图片相同，拒绝覆盖原图")
    if os.path.exists(target) and not overwrite:
        target = _unique_path(target)
    return target


def _config_summary(app_module, cfg: dict) -> dict:
    preset_id = str(cfg.get("preset") or "")
    preset = app_module._load_presets().get(preset_id) or {}
    wm_type = str(cfg.get("type") or "")
    return {
        "type": wm_type,
        "type_label": _TYPE_LABELS.get(wm_type, wm_type),
        "preset": preset_id,
        "preset_name": str(preset.get("name") or ""),
        "params": len(cfg.get("params") or {}),
    }


def _load_config(app_module, batch_module, source: str):
    """Load ``current`` / a named slot / an exported JSON file.

    Returns ``(cfg, meta, warnings, invalid)``; raises ValueError when the
    requested source is unusable.
    """
    raw_source = str(source or "").strip()
    if raw_source and raw_source.lower() not in ("current", "default", "__current__"):
        looks_like_path = (
            os.path.sep in raw_source
            or "/" in raw_source
            or raw_source.lower().endswith(".json")
        )
        if looks_like_path:
            path = _abs(raw_source)
            loaded = batch_module.load_config(config_path=path)
        else:
            loaded = batch_module.load_config(slot=raw_source)
        if not loaded.get("ok"):
            raise ValueError(loaded.get("error") or "配置文件读取失败")
        return loaded["config"], loaded, list(loaded.get("warnings") or []), list(loaded.get("invalid") or [])

    loaded = batch_module.load_config()
    if loaded.get("ok"):
        return loaded["config"], loaded, list(loaded.get("warnings") or []), list(loaded.get("invalid") or [])
    # Fresh plugin data directory: allow preset-only rendering without a current.json.
    cfg, invalid, warnings = app_module._normalize_config({})
    return cfg, {"ok": False, "path": "", "error": loaded.get("error") or ""}, list(warnings), list(invalid)


def _apply_overrides(app_module, cfg: dict, args: dict):
    """Overlay preset/type/params/export onto a normalized config and re-normalize."""
    presets = app_module._load_presets()
    preset_id = str(args.get("preset") or "").strip()
    params = args.get("params")
    type_override = str(args.get("type") or "").strip().lower()

    cfg = dict(cfg or {})
    cfg["params"] = dict(cfg.get("params") or {})

    if preset_id:
        preset = presets.get(preset_id)
        if preset is None:
            raise ValueError(
                "模板不存在：%s（可用：%s）"
                % (preset_id, "、".join(sorted(presets)) or "无")
            )
        previous_preset = str(cfg.get("preset") or "")
        new_kind = str(preset.get("kind") or "")
        cfg["preset"] = preset_id
        if not type_override:
            cfg["type"] = app_module._PRESET_KIND_TO_TYPE.get(new_kind, cfg.get("type"))
        # Switching presets means "start from that preset's look": its defaults are
        # resolved at render time, and explicit params are the only overrides. The
        # current config's tuned params are kept only when the preset is unchanged.
        if previous_preset != preset_id:
            cfg["params"] = {}
        if isinstance(params, dict):
            cfg["params"].update(params)
    elif isinstance(params, dict):
        cfg["params"].update(params)
    elif params is not None:
        raise ValueError("params 必须是对象")

    if type_override:
        if type_override not in app_module._CONFIG_TYPES:
            raise ValueError(
                "type 不合法：%s（可用：%s）"
                % (type_override, "、".join(app_module._CONFIG_TYPES))
            )
        cfg["type"] = type_override
        # A preset only serves its own watermark type; drop it when the caller
        # explicitly asks for a different type.
        preset_kind = str((presets.get(str(cfg.get("preset") or "")) or {}).get("kind") or "")
        if preset_kind and app_module._PRESET_KIND_TO_TYPE.get(preset_kind) != type_override:
            cfg["preset"] = ""
            cfg["params"] = {}

    export = dict(cfg.get("export") or {})
    if args.get("format") is not None:
        export["format"] = str(args.get("format") or "").strip().lower()
    if args.get("quality") is not None:
        export["quality"] = args.get("quality")
    cfg["export"] = export

    clean, invalid, warnings = app_module._normalize_config(cfg)
    return clean, list(invalid), list(warnings)


def _compact_applied(applied: dict) -> dict:
    if not isinstance(applied, dict):
        return {}
    out = {"type": applied.get("type")}
    bar = applied.get("bar")
    if isinstance(bar, dict):
        out["bar"] = {
            key: bar.get(key)
            for key in ("mode", "preset", "font_scale", "bar_height", "margin",
                        "divider_x", "divider", "image_fallback", "warnings")
            if key in bar
        }
        segments = bar.get("segments")
        if isinstance(segments, dict):
            compact_segments = {}
            for name, seg in segments.items():
                if not isinstance(seg, dict):
                    compact_segments[name] = None
                    continue
                image = seg.get("image") if isinstance(seg.get("image"), dict) else {}
                compact_segments[name] = seg.get("text") or image.get("src")
            out["bar"]["segments"] = compact_segments
    frame = applied.get("frame")
    if isinstance(frame, dict):
        out["frame"] = {
            key: frame.get(key)
            for key in ("preset", "canvas", "photo_box", "gap", "text_area",
                        "text_margin", "valign", "line_gap", "border",
                        "photo_logo", "warnings", "skipped")
            if key in frame
        }
        lines = frame.get("lines")
        if isinstance(lines, dict):
            out["frame"]["lines"] = {
                name: (line.get("text") if isinstance(line, dict) else None)
                for name, line in lines.items()
            }
    backdrop = applied.get("backdrop")
    if isinstance(backdrop, dict):
        out["backdrop"] = {
            key: backdrop.get(key)
            for key in ("blur_px", "zoom", "brightness", "saturation",
                        "vignette", "opacity", "cached", "work_scale")
            if key in backdrop
        }
    if applied.get("skipped"):
        out["skipped"] = applied.get("skipped")
    if applied.get("warnings"):
        out["warnings"] = applied.get("warnings")
    return out


def _output_size(applied: dict, width, height) -> tuple[int, int]:
    """Final canvas size: ``render_one`` reports the source image size.

    Frame/blur expose their canvas directly; an appended EXIF bar adds its
    height below the photo. Everything else keeps the source size.
    """
    frame = applied.get("frame") if isinstance(applied, dict) else None
    if isinstance(frame, dict):
        canvas = frame.get("canvas")
        if isinstance(canvas, (list, tuple)) and len(canvas) == 2:
            try:
                return int(canvas[0]), int(canvas[1])
            except (TypeError, ValueError):
                pass
    bar = applied.get("bar") if isinstance(applied, dict) else None
    if isinstance(bar, dict) and str(bar.get("mode") or "") == "append":
        try:
            return int(width), int(height) + int(bar.get("bar_height") or 0)
        except (TypeError, ValueError):
            pass
    return int(width or 0), int(height or 0)


def _exif_fields(app_module, raw: bytes, include_binary: bool) -> tuple[dict, dict, dict]:
    """Return ``(compact, raw_fields, meta)``.

    ``raw_fields`` keeps the engine's ``formatted``/``raw`` shape because
    ``build_exif_bar_text`` reads that shape; ``compact`` is the model-facing
    projection (value + editability).
    """
    fields = app_module.read_exif(raw)
    compact = {}
    for name, info in fields.items():
        if not isinstance(info, dict):
            continue
        if info.get("binary") and not include_binary:
            continue
        entry = {"value": info.get("formatted"), "editable": bool(info.get("editable"))}
        if info.get("source"):
            entry["source"] = info.get("source")
        compact[name] = entry
    try:
        warnings = list(app_module._load_exif_dict(raw)[2] or [])
    except Exception:
        warnings = []
    return compact, fields, {"warnings": warnings, "total": len(fields)}


def cmd_info(args: dict) -> dict:
    app_module, batch_module = _load_engine()
    cfg, meta, warnings, invalid = _load_config(app_module, batch_module, "current")
    presets = [
        {
            "id": pid,
            "name": preset.get("name"),
            "kind": preset.get("kind"),
            "type": app_module._PRESET_KIND_TO_TYPE.get(preset.get("kind"), ""),
        }
        for pid, preset in app_module._load_presets().items()
    ]
    return {
        "ok": True,
        "engine": ENGINE,
        "python": sys.version.split()[0],
        "config": {
            "ok": bool(meta.get("ok")),
            "path": meta.get("path") or "",
            "error": meta.get("error") or "",
            "exists": bool(meta.get("ok")),
            **_config_summary(app_module, cfg),
        },
        "presets": presets,
        "warnings": warnings,
        "invalid": invalid,
    }


def cmd_presets(args: dict) -> dict:
    app_module, batch_module = _load_engine()
    presets = app_module._load_presets()
    detail_id = str(args.get("id") or "").strip()
    if detail_id:
        preset = presets.get(detail_id)
        if preset is None:
            raise ValueError(
                "模板不存在：%s（可用：%s）" % (detail_id, "、".join(sorted(presets)) or "无")
            )
        item = {
            "id": detail_id,
            "name": preset.get("name"),
            "kind": preset.get("kind"),
            "type": app_module._PRESET_KIND_TO_TYPE.get(preset.get("kind"), ""),
            "description": preset.get("description"),
            "params": preset.get("params") or {},
            "controls": preset.get("controls") or [],
            "invalid_params": preset.get("invalid_params") or [],
        }
        return {"ok": True, "preset": item}

    items = [
        {
            "id": pid,
            "name": preset.get("name"),
            "kind": preset.get("kind"),
            "type": app_module._PRESET_KIND_TO_TYPE.get(preset.get("kind"), ""),
            "description": preset.get("description"),
            "param_count": len(preset.get("params") or {}),
            "control_count": len(preset.get("controls") or []),
        }
        for pid, preset in presets.items()
    ]
    items.sort(key=lambda entry: (entry["kind"], entry["id"]))
    cfg, meta, warnings, invalid = _load_config(app_module, batch_module, "current")
    return {
        "ok": True,
        "presets": items,
        "kinds": sorted(app_module._PRESET_PARAM_SPECS),
        "current": {
            "ok": bool(meta.get("ok")),
            "error": meta.get("error") or "",
            **_config_summary(app_module, cfg),
        },
        "warnings": warnings,
        "invalid": invalid,
    }


def cmd_render(args: dict) -> dict:
    app_module, batch_module = _load_engine()
    input_path = _abs(args.get("input") or "")
    raw = _read_input(input_path)
    source = str(args.get("config") or "current")
    cfg, meta, cfg_warnings, cfg_invalid = _load_config(app_module, batch_module, source)
    if not meta.get("ok") and not (args.get("preset") or args.get("params") or args.get("type")):
        raise ValueError(
            "当前没有可用的配置（%s）。请显式传 preset / params 选择水印样式。"
            % (meta.get("error") or "configs/current.json 不存在")
        )
    cfg, invalid, warnings = _apply_overrides(app_module, cfg, args)
    payload = app_module._config_to_wm_payload(cfg)
    export = {
        "format": str(args.get("format") or cfg.get("export", {}).get("format") or "jpeg"),
        "quality": args.get("quality", cfg.get("export", {}).get("quality") or 95),
    }
    started = time.perf_counter()
    result = batch_module.render_one(raw, payload, export)
    if not result.get("ok"):
        raise RuntimeError(result.get("error") or "渲染失败")

    ext = result.get("ext") or ".jpg"
    output = _resolve_output(input_path, args.get("output"), "_wm", ext, bool(args.get("overwrite")))
    with open(output, "wb") as handle:
        handle.write(result["data"])
    out_width, out_height = _output_size(
        result.get("applied") or {}, result.get("width"), result.get("height")
    )

    return {
        "ok": True,
        "input": input_path,
        "output": output,
        "width": out_width,
        "height": out_height,
        "source_width": result.get("width"),
        "source_height": result.get("height"),
        "format": result.get("fmt"),
        "bytes": len(result.get("data") or b""),
        "ms": result.get("ms") or int(round((time.perf_counter() - started) * 1000)),
        "config": _config_summary(app_module, cfg),
        "applied": _compact_applied(result.get("applied") or {}),
        "warnings": list(cfg_warnings) + list(warnings),
        "invalid": list(cfg_invalid) + list(invalid),
    }


def cmd_batch(args: dict) -> dict:
    app_module, batch_module = _load_engine()
    inputs = args.get("inputs")
    if isinstance(inputs, str):
        inputs = [inputs]
    if not isinstance(inputs, list) or not inputs:
        raise ValueError("inputs 必须是非空数组（文件夹或图片路径）")
    resolved_inputs = [_abs(item) for item in inputs if str(item or "").strip()]
    missing = [item for item in resolved_inputs if not os.path.exists(item)]
    if missing and len(missing) == len(resolved_inputs):
        raise ValueError("输入路径都不存在：%s" % "、".join(missing))

    source = str(args.get("config") or "current")
    cfg, meta, cfg_warnings, cfg_invalid = _load_config(app_module, batch_module, source)
    if not meta.get("ok") and not (args.get("preset") or args.get("params") or args.get("type")):
        raise ValueError(
            "当前没有可用的配置（%s）。请显式传 preset / params 选择水印样式。"
            % (meta.get("error") or "configs/current.json 不存在")
        )
    cfg, invalid, warnings = _apply_overrides(app_module, cfg, args)
    # Re-use batch_core's loaded shape so validation and rendering see one config.
    loaded = dict(meta)
    loaded.update({"ok": True, "config": cfg, "invalid": list(cfg_invalid) + list(invalid),
                   "warnings": list(cfg_warnings) + list(warnings)})

    out_root = str(args.get("out") or "")
    if out_root:
        out_root = _abs(out_root)
    suffix = args.get("suffix")
    options = batch_module.build_options(
        resolved_inputs,
        out_root=out_root,
        suffix="_wm" if suffix is None else str(suffix),
        force=bool(args.get("force")),
        fmt=str(args.get("format") or ""),
        quality=args.get("quality"),
        recursive=bool(args.get("recursive", True)),
        dry_run=bool(args.get("dry_run")),
    )
    scan = batch_module.scan_for(options)
    notes = batch_module.validate_plan(scan, loaded, options)
    errors = [text for level, text in notes if level == "error"]
    if errors:
        raise ValueError("；".join(errors))

    summary = batch_module.run_batch(scan, loaded, options)
    items = list(summary.get("items") or [])
    compact_items = []
    for item in items[:200]:
        row = {
            "src": item.get("src"),
            "dest": item.get("dest"),
            "action": item.get("action"),
            "ok": bool(item.get("ok")),
        }
        if item.get("error"):
            row["error"] = item.get("error")
        if item.get("ms") is not None:
            row["ms"] = item.get("ms")
        compact_items.append(row)
    failures = [
        {"src": item.get("src"), "error": item.get("error") or "未知错误"}
        for item in items
        if not item.get("ok")
    ]
    return {
        "ok": bool(summary.get("ok")) and int(summary.get("total") or 0) > 0,
        "total": summary.get("total"),
        "ok_count": summary.get("ok_count"),
        "failed": summary.get("failed"),
        "skipped": summary.get("skipped"),
        "cancelled": summary.get("cancelled"),
        "seconds": summary.get("seconds"),
        "dry_run": summary.get("dry_run"),
        "output_dir": summary.get("output_dir") or out_root,
        "suffix": summary.get("suffix") or options.get("suffix"),
        "format": (summary.get("export") or {}).get("format"),
        "quality": (summary.get("export") or {}).get("quality"),
        "exit_code": batch_module.exit_code(summary, loaded),
        "config": _config_summary(app_module, cfg),
        "items": compact_items,
        "truncated": len(items) > len(compact_items),
        "failures": failures[:50],
        "notes": [{"level": level, "text": text} for level, text in notes],
        "warnings": list(cfg_warnings) + list(warnings),
        "invalid": list(cfg_invalid) + list(invalid),
    }


def cmd_exif_read(args: dict) -> dict:
    app_module, _batch = _load_engine()
    input_path = _abs(args.get("input") or "")
    raw = _read_input(input_path)
    fields, raw_fields, meta = _exif_fields(app_module, raw, bool(args.get("include_binary")))
    if not fields:
        raise ValueError("这张图片没有可读的 EXIF（或 EXIF 结构损坏）")
    core = {name: fields.get(name) for name in _CORE_FIELDS}
    camera_bar = app_module.build_exif_bar_text(raw_fields)
    return {
        "ok": True,
        "input": input_path,
        "fields": fields,
        "core": core,
        "missing_core": [name for name in _CORE_FIELDS if not core.get(name)],
        "camera_bar": list(camera_bar),
        "warnings": meta["warnings"],
    }


def cmd_exif_write(args: dict) -> dict:
    app_module, _batch = _load_engine()
    input_path = _abs(args.get("input") or "")
    raw = _read_input(input_path)
    edits = args.get("edits")
    if not isinstance(edits, dict) or not edits:
        raise ValueError("edits 必须是非空对象（键 = exif_read 返回的字段名，值 = 新值）")

    known = app_module._EXIF_NAME_TO_TAG
    unknown = sorted(key for key in edits if key not in known)
    if unknown:
        raise ValueError("未知的 EXIF 字段：%s" % "、".join(unknown))

    exif_dict, from_piexif, load_warnings = app_module._load_exif_dict(raw)
    exif_ifd_tags = {
        getattr(app_module.ExifIFD, attr)
        for attr in dir(app_module.ExifIFD)
        if not attr.startswith("_") and isinstance(getattr(app_module.ExifIFD, attr), int)
    }

    applied = []
    for name, value in edits.items():
        tag_id = known.get(name)
        if tag_id is None:
            continue
        ifd_name = "Exif" if tag_id in exif_ifd_tags else "0th"
        if isinstance(value, str):
            if tag_id == app_module.ExifIFD.FNumber:
                try:
                    number = float(value.replace("f/", ""))
                    exif_dict[ifd_name][tag_id] = (int(number * 10), 10)
                except ValueError:
                    continue
            elif tag_id in (app_module.ExifIFD.FocalLength, app_module.ExifIFD.FocalLengthIn35mmFilm):
                try:
                    number = float(value.replace("mm", ""))
                    exif_dict[ifd_name][tag_id] = (int(number * 10), 10)
                except ValueError:
                    continue
            elif tag_id in (
                app_module.ExifIFD.ISOSpeedRatings,
                app_module.ExifIFD.ISOSpeed,
                app_module.ExifIFD.StandardOutputSensitivity,
                app_module.ExifIFD.RecommendedExposureIndex,
            ):
                number = app_module._parse_int_text(value)
                if number is None:
                    continue
                exif_dict[ifd_name][tag_id] = number
            elif tag_id == app_module.ExifIFD.ExposureTime:
                try:
                    text = value.replace("s", "")
                    if "/" in text:
                        num, den = text.split("/")
                        exif_dict[ifd_name][tag_id] = (int(num), int(den))
                    else:
                        number = float(text)
                        if number < 1:
                            exif_dict[ifd_name][tag_id] = (1, int(round(1 / number)))
                        else:
                            exif_dict[ifd_name][tag_id] = (int(number * 10), 10)
                except (ValueError, ZeroDivisionError):
                    continue
            elif tag_id == app_module.ExifIFD.UserComment:
                exif_dict[ifd_name][tag_id] = app_module.UserComment.dump(value, encoding="unicode")
            else:
                tag_type = app_module._spec_type(ifd_name, tag_id)
                if tag_type in (1, 3, 4, 6, 8, 9):
                    code = app_module._enum_code_from_text(tag_id, value)
                    if code is None:
                        continue
                    exif_dict[ifd_name][tag_id] = code
                elif tag_type == 2:
                    exif_dict[ifd_name][tag_id] = app_module._encode_ascii_text(value)
                else:
                    exif_dict[ifd_name][tag_id] = value.encode("utf-8")
        elif isinstance(value, (int, float)):
            exif_dict[ifd_name][tag_id] = value
        else:
            raise ValueError("字段 %s 的值必须是字符串或数字" % name)
        applied.append(name)

    if not applied:
        raise ValueError("没有一个字段能写入（值格式与标签类型不匹配）")

    exif_bytes = app_module._dumpable_exif_bytes(exif_dict, from_piexif)
    warnings = list(load_warnings)
    fallback = False
    try:
        updated = app_module.piexif.insert(exif_bytes, raw)
    except Exception:
        # Non-JPEG containers (or malformed APP1) cannot be patched in place.
        image = app_module.Image.open(io.BytesIO(raw))
        if image.mode == "RGBA":
            image = image.convert("RGB")
        buffer = io.BytesIO()
        image.save(buffer, "JPEG", exif=exif_bytes, quality=95)
        updated = buffer.getvalue()
        fallback = True
        warnings.append("无法原地替换 APP1 段，已重新编码为 JPEG（画质按 95 输出）")

    output = _resolve_output(input_path, args.get("output"), "_exif", ".jpg", bool(args.get("overwrite")))
    if fallback and os.path.splitext(output)[1].lower() not in (".jpg", ".jpeg"):
        output = os.path.splitext(output)[0] + ".jpg"
    _ensure_parent(output)
    with open(output, "wb") as handle:
        handle.write(updated)

    fields, _raw_fields, _meta = _exif_fields(app_module, updated, False)
    return {
        "ok": True,
        "input": input_path,
        "output": output,
        "changed": applied,
        "warnings": warnings,
        "fields": {name: fields.get(name) for name in applied if name in fields},
        "core": {name: fields.get(name) for name in _CORE_FIELDS},
    }


COMMANDS = {
    "info": cmd_info,
    "presets": cmd_presets,
    "render": cmd_render,
    "batch": cmd_batch,
    "exif_read": cmd_exif_read,
    "exif_write": cmd_exif_write,
}


def _fail(message: str, **extra) -> dict:
    payload = {"ok": False, "error": message}
    payload.update(extra)
    if os.environ.get("CAMERA_WATERMARK_DEBUG") == "1":
        payload["traceback"] = traceback.format_exc()
    return payload


def main(argv: list[str]) -> int:
    request_path = argv[2] if len(argv) > 2 else ""
    response_path = argv[3] if len(argv) > 3 else ""
    if len(argv) < 2 or argv[1] not in COMMANDS:
        result = _fail(
            "用法：bridge.py <%s> [request.json] [response.json]"
            % "|".join(sorted(COMMANDS)),
            commands=sorted(COMMANDS),
        )
    else:
        command = argv[1]
        try:
            if request_path:
                with open(request_path, "r", encoding="utf-8-sig") as handle:
                    text = handle.read()
            else:
                # Read bytes and decode explicitly: Windows Python defaults stdin
                # to the ANSI code page, which corrupts UTF-8 paths.
                text = sys.stdin.buffer.read().decode("utf-8-sig")
            args = json.loads(text) if text.strip() else {}
            if not isinstance(args, dict):
                raise ValueError("请求体必须是 JSON 对象")
            result = COMMANDS[command](args)
        except Exception as exc:  # noqa: BLE001 - one JSON error per invocation
            result = _fail(str(exc) or exc.__class__.__name__)

    data = json.dumps(result, ensure_ascii=False, default=str)
    if response_path:
        try:
            with open(response_path, "w", encoding="utf-8") as handle:
                handle.write(data)
                handle.write("\n")
        except Exception as exc:  # pragma: no cover - filesystem failure
            _log("cannot write response file: %s" % exc)
            return 1
    else:
        sys.stdout.buffer.write(data.encode("utf-8"))
        sys.stdout.buffer.write(b"\n")
        sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
