import io
import os
import re
import base64
import json
import uuid
import numbers
import struct
import threading
import time
import functools
from flask import Flask, request, jsonify, render_template, send_file
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps
import piexif
from piexif import ImageIFD, ExifIFD, GPSIFD
from piexif.helper import UserComment
from fractions import Fraction

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024
# 模板热重载：改完 templates/index.html 刷新浏览器即生效。
# 不开这个的话 Flask 会把页面缓存住，出现「presets.json 是热的、index.html 是冷的」
# 这种不对称（模板 chip 看得见、里面却没有新控件）——历史排查过的坑。
# 故意不设 debug=True：那会带 reloader，与预览服务的启动方式冲突；改 app.py 仍需重启。
app.config["TEMPLATES_AUTO_RELOAD"] = True
UPLOAD_DIR = os.path.join(os.path.dirname(__file__), "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)
# 品牌贴图库：与 uploads/ 的区别是**生命周期** —— 贴图是长期资产，
# 跨会话保留、也不随 /api/reset 清空（要删就用贴图旁的「删除」按钮）。
BRAND_DIR = os.path.join(os.path.dirname(__file__), "brands")
os.makedirs(BRAND_DIR, exist_ok=True)
# 字体库：信息栏文字可以指定用哪个字体（系统字体 + 自己上传的 .ttf/.otf）。
# 与品牌贴图同一套生命周期：跨会话保留的**长期资产**，/api/reset 不清，
# 只由字体旁的「删除」按钮（POST /api/font/remove）清掉。
FONT_DIR = os.path.join(os.path.dirname(__file__), "fonts")
os.makedirs(FONT_DIR, exist_ok=True)
# 配置（cfg）目录：调好的那一套水印参数也是**长期资产** —— 与 brands/、fonts/ 同级，
# 跨会话保留、/api/reset 不清；里面 current.json = 自动保存槽（页面打开即还原），
# <id>.json = 自己「另存为」的命名快照（见「配置持久化」一节）。
CONFIG_DIR = os.path.join(os.path.dirname(__file__), "configs")
os.makedirs(CONFIG_DIR, exist_ok=True)

# 允许处理高像素相机照片，同时防止解压炸弹导致内存耗尽
Image.MAX_IMAGE_PIXELS = 400_000_000


@app.errorhandler(413)
def too_large(e):
    return jsonify({"error": "图片超过 100MB 上限，请压缩后再试"}), 413


_io_lock = threading.Lock()


def _locked(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with _io_lock:
            return fn(*args, **kwargs)
    return wrapper


# ---------------------------------------------------------------------------
# 实时预览加速
#
# 全尺寸相机图（如 5504×4128）每帧都要「解码 working.png → 合成 → 重编码 PNG」，
# 实测单帧 ~3.9s，且 PNG 有 48MB，base64 后响应体接近 65MB —— 这是拖滑块卡顿的主因。
# 方案：上传时生成一张「预览底图」（最长边 ≤1600px 的 JPEG），预览只在底图上合成，
# 绝对字号按 render_scale 等比缩小，视觉与全尺寸一致；下载仍走全尺寸原图。
# ---------------------------------------------------------------------------
PREVIEW_MAX_DIM = 1600
PREVIEW_BASE_JPEG_QUALITY = 90
PREVIEW_JPEG_QUALITY = 85

_ORIGINAL_NAME = "original.jpg"
_WORKING_NAME = "working.png"
# 上传时的原始信息（格式/文件名/是否带 alpha）：下载「保持原格式」需要它
_SOURCE_NAME = "source.json"

# 预览底图内存缓存（只在 _io_lock 内读写），mtime 变化即失效
_preview_base_cache = {"key": None, "bytes": None, "size": None, "scale": 1.0}


def _flatten_rgb(img):
    """带 alpha 的图像合成到白底再转 RGB（JPEG 无透明通道，避免透明区域变黑）。"""
    if img.mode in ("RGBA", "LA", "P"):
        rgba = img.convert("RGBA")
        bg = Image.new("RGB", rgba.size, (255, 255, 255))
        bg.paste(rgba, mask=rgba.split()[3])
        return bg
    return img.convert("RGB")


def _working_image():
    """打开全尺寸工作图（RGBA）；无主图返回 None。"""
    working_path = os.path.join(UPLOAD_DIR, _WORKING_NAME)
    if os.path.exists(working_path):
        return Image.open(working_path).convert("RGBA")
    original_path = os.path.join(UPLOAD_DIR, _ORIGINAL_NAME)
    if os.path.exists(original_path):
        return Image.open(original_path).convert("RGBA")
    return None


def _preview_base_key():
    """用主图 mtime+size 当缓存键：换图后缓存自动失效，无需显式清理。"""
    for name in (_WORKING_NAME, _ORIGINAL_NAME):
        path = os.path.join(UPLOAD_DIR, name)
        if os.path.exists(path):
            st = os.stat(path)
            return f"{name}:{st.st_mtime_ns}:{st.st_size}"
    return None


def _build_preview_base(img):
    """生成预览底图，返回 (jpeg_bytes, (width, height), scale)。"""
    scale = min(1.0, PREVIEW_MAX_DIM / max(img.width, img.height))
    base = img
    if scale < 1.0:
        base = img.resize(
            (max(1, round(img.width * scale)), max(1, round(img.height * scale))),
            Image.LANCZOS,
        )
    rgb = _flatten_rgb(base)
    buf = io.BytesIO()
    rgb.save(buf, "JPEG", quality=PREVIEW_BASE_JPEG_QUALITY)
    return buf.getvalue(), rgb.size, scale


def _cache_preview_base(img):
    """用已在内存中的图像直接生成预览底图并写入缓存（上传时调用，省一次解码）。"""
    key = _preview_base_key()
    if key is None:
        return None
    data, size, scale = _build_preview_base(img)
    _preview_base_cache.update({"key": key, "bytes": data, "size": size, "scale": scale})
    return data, size, scale


def _preview_base():
    """取预览底图 (jpeg_bytes, (width, height), scale)；无主图返回 None。"""
    key = _preview_base_key()
    if key is None:
        return None
    cache = _preview_base_cache
    if cache["key"] == key and cache["bytes"]:
        return cache["bytes"], cache["size"], cache["scale"]
    img = _working_image()
    if img is None:
        return None
    data, size, scale = _build_preview_base(img)
    cache.update({"key": key, "bytes": data, "size": size, "scale": scale})
    return data, size, scale


def _clear_preview_base():
    _preview_base_cache.update({"key": None, "bytes": None, "size": None, "scale": 1.0})


def _data_url(data, mime):
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


# Common EXIF tag mappings
EXIF_TAG_NAMES = {
    # --- 0th IFD（基础信息） ---
    ImageIFD.Make: "相机制造商",
    ImageIFD.Model: "相机型号",
    ImageIFD.Software: "软件",
    ImageIFD.DateTime: "日期时间",
    ImageIFD.Artist: "作者",
    ImageIFD.Copyright: "版权",
    ImageIFD.Orientation: "方向",
    ImageIFD.XResolution: "X分辨率",
    ImageIFD.YResolution: "Y分辨率",
    ImageIFD.ResolutionUnit: "分辨率单位",
    ImageIFD.ImageDescription: "图像描述",
    ImageIFD.HostComputer: "主机",
    ImageIFD.YCbCrPositioning: "YCbCr定位",
    ImageIFD.Compression: "压缩方式",
    ImageIFD.BitsPerSample: "位深",
    ImageIFD.PhotometricInterpretation: "色彩解析",
    ImageIFD.SamplesPerPixel: "通道数",
    ImageIFD.PlanarConfiguration: "平面配置",
    ImageIFD.WhitePoint: "白点",
    ImageIFD.PrimaryChromaticities: "原色色度",
    ImageIFD.YCbCrCoefficients: "YCbCr系数",
    ImageIFD.YCbCrSubSampling: "YCbCr子采样",
    ImageIFD.ReferenceBlackWhite: "黑白参考",
    ImageIFD.XPTitle: "XP标题",
    ImageIFD.XPComment: "XP备注",
    ImageIFD.XPAuthor: "XP作者",
    ImageIFD.XPKeywords: "XP关键词",
    ImageIFD.XPSubject: "XP主题",
    ImageIFD.Rating: "评分",
    ImageIFD.PrintImageMatching: "PrintIM信息",
    # --- Exif IFD（拍摄参数） ---
    ExifIFD.ExposureTime: "曝光时间",
    ExifIFD.FNumber: "光圈值",
    # 34855：相机普遍写入的 ISO（PhotographicSensitivity）
    ExifIFD.ISOSpeedRatings: "ISO感光度",
    # 34867：CIPA 2010 新增的 ISO Speed。显示名必须与上面区分，
    # 否则 {显示名: tag_id} 逆向映射互相覆盖，编辑「ISO感光度」会写到错误的标签
    ExifIFD.ISOSpeed: "ISO速度",
    ExifIFD.StandardOutputSensitivity: "标准输出感光度",
    ExifIFD.RecommendedExposureIndex: "推荐曝光指数",
    ExifIFD.SensitivityType: "感光度类型",
    ExifIFD.FocalLength: "焦距",
    ExifIFD.LensMake: "镜头制造商",
    ExifIFD.LensModel: "镜头型号",
    ExifIFD.LensSpecification: "镜头规格",
    ExifIFD.LensSerialNumber: "镜头序列号",
    ExifIFD.DateTimeOriginal: "拍摄时间",
    ExifIFD.DateTimeDigitized: "数字化时间",
    ExifIFD.OffsetTime: "时区偏移",
    ExifIFD.OffsetTimeOriginal: "拍摄时区",
    ExifIFD.OffsetTimeDigitized: "数字化时区",
    ExifIFD.SubSecTime: "亚秒时间",
    ExifIFD.SubSecTimeOriginal: "亚秒(拍摄)",
    ExifIFD.SubSecTimeDigitized: "亚秒(数字化)",
    ExifIFD.ExposureBiasValue: "曝光补偿",
    ExifIFD.MeteringMode: "测光模式",
    ExifIFD.Flash: "闪光灯",
    ExifIFD.WhiteBalance: "白平衡",
    ExifIFD.ExposureProgram: "曝光程序",
    ExifIFD.ExposureMode: "曝光模式",
    ExifIFD.FocalLengthIn35mmFilm: "35mm等效焦距",
    ExifIFD.PixelXDimension: "图像宽度",
    ExifIFD.PixelYDimension: "图像高度",
    ExifIFD.ShutterSpeedValue: "快门速度值",
    ExifIFD.ApertureValue: "光圈值(APEX)",
    ExifIFD.MaxApertureValue: "最大光圈",
    ExifIFD.BrightnessValue: "亮度值",
    ExifIFD.ExposureIndex: "曝光指数",
    ExifIFD.SubjectDistance: "主体距离",
    ExifIFD.SubjectArea: "主体区域",
    ExifIFD.LightSource: "光源",
    ExifIFD.ColorSpace: "色彩空间",
    ExifIFD.ExifVersion: "Exif版本",
    ExifIFD.FlashpixVersion: "FlashPix版本",
    ExifIFD.ComponentsConfiguration: "色彩空间配置",
    ExifIFD.CompressedBitsPerPixel: "压缩位率",
    ExifIFD.FileSource: "文件来源",
    ExifIFD.SceneType: "场景类型",
    ExifIFD.CFAPattern: "CFA图案",
    ExifIFD.CustomRendered: "自定义渲染",
    ExifIFD.DigitalZoomRatio: "数码变焦",
    ExifIFD.SceneCaptureType: "场景拍摄类型",
    ExifIFD.GainControl: "增益控制",
    ExifIFD.Contrast: "对比度",
    ExifIFD.Saturation: "饱和度",
    ExifIFD.Sharpness: "锐度",
    ExifIFD.SubjectDistanceRange: "主体距离范围",
    ExifIFD.DeviceSettingDescription: "设备设置描述",
    ExifIFD.ImageUniqueID: "图像唯一ID",
    ExifIFD.BodySerialNumber: "机身序列号",
    ExifIFD.CameraOwnerName: "相机拥有者",
    ExifIFD.FocalPlaneXResolution: "焦平面X分辨率",
    ExifIFD.FocalPlaneYResolution: "焦平面Y分辨率",
    ExifIFD.FocalPlaneResolutionUnit: "焦平面分辨率单位",
    ExifIFD.SensingMethod: "感光方式",
    ExifIFD.MakerNote: "厂商备注",
    ExifIFD.UserComment: "用户注释",
    ExifIFD.RelatedSoundFile: "关联音频文件",
    ExifIFD.SpectralSensitivity: "光谱灵敏度",
    ExifIFD.OECF: "光电转换函数",
    ExifIFD.Gamma: "伽马",
}


# 枚举型标签的取值表（用于把数字转成可读文本）
_ENUM_TAG_LABELS = {
    ImageIFD.ResolutionUnit: {1: "无单位", 2: "英寸", 3: "厘米"},
    ImageIFD.YCbCrPositioning: {1: "居中", 2: "同置"},
    ImageIFD.Orientation: {
        1: "正常", 2: "水平镜像", 3: "旋转180°", 4: "垂直镜像",
        5: "镜像并旋转90°", 6: "顺时针90°", 7: "镜像并旋转270°", 8: "逆时针90°",
    },
    ImageIFD.Compression: {1: "未压缩", 6: "JPEG"},
    ExifIFD.SensitivityType: {
        0: "未知", 1: "标准输出感光度", 2: "推荐曝光指数", 3: "ISO感光度",
        4: "标准输出感光度(高)", 5: "标准输出感光度(低)", 6: "推荐曝光指数(高)",
        7: "推荐曝光指数(低)",
    },
    ExifIFD.MeteringMode: {0: "未知", 1: "平均", 2: "中央重点", 3: "点测", 4: "分区", 5: "评价", 6: "局部", 255: "其他"},
    ExifIFD.Flash: {
        0: "未闪光", 1: "闪光", 5: "闪光(未回电)", 7: "强制闪光", 9: "强制闪光(未回电)",
        16: "未闪光", 24: "未闪光", 25: "闪光(自动)", 29: "闪光(自动,未回电)",
        32: "未闪光", 65: "红眼", 69: "红眼(未回电)", 71: "红眼(强制)", 73: "红眼(强制,未回电)",
        89: "红眼(自动)",
    },
    ExifIFD.WhiteBalance: {0: "自动", 1: "手动"},
    ExifIFD.ExposureProgram: {
        0: "未定义", 1: "手动", 2: "正常程序", 3: "光圈优先", 4: "快门优先",
        5: "创意程序", 6: "动作优先", 7: "肖像模式", 8: "风景模式",
    },
    ExifIFD.ExposureMode: {0: "自动曝光", 1: "手动曝光", 2: "自动包围"},
    ExifIFD.LightSource: {
        0: "未知", 1: "日光", 2: "荧光灯", 3: "钨丝灯", 4: "闪光灯", 9: "晴天",
        10: "阴天", 11: "阴影", 12: "日光荧光灯(D)", 13: "日光荧光灯(N)",
        14: "白色荧光灯", 15: "暖白荧光灯", 16: "标准光A", 17: "标准光B",
        18: "标准光C", 19: "D55", 20: "D65", 21: "D75", 22: "D50",
        23: "ISO影室钨丝灯", 24: "日光荧光灯(D)", 255: "其他",
    },
    ExifIFD.ColorSpace: {1: "sRGB", 2: "Adobe RGB", 65535: "未校准"},
    ExifIFD.CustomRendered: {0: "正常处理", 1: "自定义处理"},
    ExifIFD.SceneCaptureType: {0: "标准", 1: "风景", 2: "人像", 3: "夜景", 4: "夜景人像"},
    ExifIFD.GainControl: {0: "无", 1: "低增益(向上)", 2: "高增益(向上)", 3: "低增益(向下)", 4: "高增益(向下)"},
    ExifIFD.Contrast: {0: "标准", 1: "柔和", 2: "强烈"},
    ExifIFD.Saturation: {0: "标准", 1: "低饱和", 2: "高饱和"},
    ExifIFD.Sharpness: {0: "标准", 1: "柔和", 2: "强烈"},
    ExifIFD.SubjectDistanceRange: {0: "未知", 1: "微距", 2: "近景", 3: "远景"},
    ExifIFD.FileSource: {1: "胶片扫描仪", 2: "反射式扫描仪", 3: "数码相机"},
    ExifIFD.SceneType: {1: "直接拍摄"},
    ExifIFD.SensingMethod: {1: "未定义", 2: "单芯片彩色面阵", 3: "双芯片彩色面阵", 4: "三芯片彩色面阵", 5: "彩色顺序面阵", 7: "三线性", 8: "彩色顺序线性"},
    ExifIFD.FocalPlaneResolutionUnit: {1: "无单位", 2: "英寸", 3: "厘米"},
}


# 显示名 → tag_id（编辑回写用）。标签表内显示名唯一，因此不会互相覆盖；
# 若新增标签请保证显示名不重复，否则编辑会写到错误标签（见 ISOSpeed 处的注释）。
_EXIF_NAME_TO_TAG = {label: tag_id for tag_id, label in EXIF_TAG_NAMES.items()}

# UNDEFINED(7) 但语义是「编码位/版本」的标签，不能当二进制块展示
_BYTE_CODE_TAGS = frozenset((
    ExifIFD.ComponentsConfiguration,   # 37121：4 字节，1=Y 2=Cb 3=Cr 4=R 5=G 6=B，0=未使用
))

_COMPONENT_NAMES = {0: "-", 1: "Y", 2: "Cb", 3: "Cr", 4: "R", 5: "G", 6: "B"}

_INT_TEXT_RE = re.compile(r"-?\d+")


def _parse_int_text(value):
    """从 'ISO 400' / '400' / '6' 这类文本里取整数；取不到返回 None。"""
    m = _INT_TEXT_RE.search(str(value))
    if not m:
        return None
    try:
        return int(m.group())
    except ValueError:
        return None


def _as_enum_code(value):
    """把枚举标签的取值规整成整数，取不到返回 None。

    FileSource / SceneType 这类枚举在 EXIF 规范里是 UNDEFINED 类型，
    piexif 读出来是单字节 bytes（如 b'\\x03'），需先取出数值才能查到中文枚举名。
    """
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, numbers.Integral):
        return int(value)
    if isinstance(value, (bytes, bytearray)):
        data = bytes(value).rstrip(b"\x00")
        if len(data) == 1:
            return data[0]
        try:
            return int(data.decode("ascii"))
        except (UnicodeDecodeError, ValueError):
            return None
    return None


def _enum_code_from_text(tag_id, text):
    """把前端回传的枚举中文名（「正常」→1）或数字文本还原成枚举值。"""
    text = str(text).strip()
    for code, label in _ENUM_TAG_LABELS.get(tag_id, {}).items():
        if label == text:
            return code
    return _parse_int_text(text)



def rational_to_float(value):
    if isinstance(value, tuple) and len(value) == 2 and value[1] != 0:
        return value[0] / value[1]
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def format_exif_value(tag_id, value):
    if tag_id in (ExifIFD.ExposureTime,):
        v = rational_to_float(value)
        if v > 0:
            return f"1/{int(round(1/v))}s" if v < 1 else f"{v:.1f}s"
        return str(value)
    if tag_id in (ExifIFD.FNumber, ExifIFD.FocalLength, ExifIFD.FocalLengthIn35mmFilm):
        v = rational_to_float(value)
        if tag_id == ExifIFD.FNumber:
            return f"f/{v:.1f}"
        return f"{v:.0f}mm"
    if tag_id == ExifIFD.ExposureBiasValue:
        v = rational_to_float(value)
        sign = "+" if v >= 0 else ""
        return f"{sign}{v:.1f} EV"
    if tag_id in (ExifIFD.ISOSpeedRatings, ExifIFD.ISOSpeed,
                  ExifIFD.StandardOutputSensitivity, ExifIFD.RecommendedExposureIndex):
        # 个别相机以 RATIONAL 存 ISO（如 100/1），Pillow 读出为有理数
        try:
            return f"ISO {int(round(rational_to_float(value)))}"
        except Exception:
            return f"ISO {value}"
    if tag_id in _BYTE_CODE_TAGS and isinstance(value, (bytes, bytearray)):
        # 如 ComponentsConfiguration：b"\x01\x02\x03\x00" → "Y Cb Cr -"
        return " ".join(_COMPONENT_NAMES.get(b, str(b)) for b in value)
    if tag_id in _ENUM_TAG_LABELS:
        labels = _ENUM_TAG_LABELS[tag_id]
        code = _as_enum_code(value)
        if code is not None and code in labels:
            return labels[code]
        return str(value)
    if tag_id == ExifIFD.LensSpecification and isinstance(value, tuple) and len(value) == 4:
        fl1, fl2, ap1, ap2 = (rational_to_float(v) for v in value)
        focal = f"{fl1:g}-{fl2:g}mm" if fl2 and fl2 != fl1 else f"{fl1:g}mm"
        if ap2 and ap2 != ap1:
            return f"{focal} f/{ap1:g}-{ap2:g}"
        return f"{focal} f/{ap1:g}"
    if tag_id in (ExifIFD.ApertureValue, ExifIFD.MaxApertureValue):
        v = rational_to_float(value)
        return f"f/{2 ** (v / 2):.1f}" if v else str(value)
    if tag_id == ExifIFD.ShutterSpeedValue:
        v = rational_to_float(value)
        if v:
            sec = 2 ** (-v)
            return f"1/{int(round(1 / sec))}s" if 0 < sec < 1 else f"{sec:.2f}s"
        return str(value)
    if tag_id == ExifIFD.DigitalZoomRatio:
        v = rational_to_float(value)
        return "不使用" if v <= 1 else f"{v:g}x"
    if tag_id == ExifIFD.SubjectDistance:
        v = rational_to_float(value)
        return "未知" if v <= 0 else f"{v:g}m"
    if isinstance(value, tuple) and len(value) == 2:
        v = rational_to_float(value)
        return f"{v:.2f}" if v != int(v) else str(int(v))
    return str(value)


_EXIF_POINTER_TAGS = (0x8769, 0x8825, 0xA005)  # ExifOffset / GPSInfo / InteropOffset

# EXIF 文本字段的编码探测顺序。
# 索尼等日系相机会把 Artist / Copyright / ImageDescription / LensModel 写成 Shift-JIS，
# 直接按 UTF-8 解会得到 "�B�e�F�R..." 这类乱码，所以按「能无损解出就用」逐个尝试。
_TEXT_ENCODINGS = ("ascii", "utf-8", "shift_jis", "cp932", "euc_jp", "gb18030")

# 规范类型是 UNDEFINED(7)，但内容确实为文本的标签
_UNDEFINED_TEXT_TAGS = frozenset((
    ExifIFD.UserComment,        # 37510，带 8 字节编码前缀，交给 piexif.helper 解析
    ExifIFD.ImageUniqueID,      # 42016
    ExifIFD.BodySerialNumber,   # 42033
    ExifIFD.LensSerialNumber,   # 42037
))

# Windows XP 系列标签用 UTF-16LE 存放
_UTF16LE_TEXT_TAGS = frozenset((
    ImageIFD.XPTitle, ImageIFD.XPComment, ImageIFD.XPAuthor,
    ImageIFD.XPKeywords, ImageIFD.XPSubject,
))


def _spec_type(ifd_name, tag_id):
    """查 EXIF 规范中该标签的类型（2=ASCII、7=UNDEFINED…），未知返回 None。"""
    entry = piexif._exif.TAGS.get(ifd_name, {}).get(tag_id)
    return entry.get("type") if entry else None


def _decode_text(value, tag_id=None):
    """多编码还原 EXIF 文本，避免日文/中文相机写出的非 UTF-8 文本变成乱码。"""
    if isinstance(value, str):
        return value
    if not isinstance(value, (bytes, bytearray)):
        return str(value)
    data = bytes(value)
    if tag_id == ExifIFD.UserComment:
        try:
            return UserComment.load(data)
        except Exception:
            data = data[8:] if len(data) > 8 else b""
    if tag_id in _UTF16LE_TEXT_TAGS:
        data = data.rstrip(b"\x00")
        return data[: len(data) - len(data) % 2].decode("utf-16-le", errors="replace")
    if data[:2] in (b"\xfe\xff", b"\xff\xfe"):
        order = "utf-16-be" if data[:2] == b"\xfe\xff" else "utf-16-le"
        data = data[2:]
        return data[: len(data) - len(data) % 2].decode(order, errors="replace")
    data = data.rstrip(b"\x00")
    if not data:
        return ""
    for enc in _TEXT_ENCODINGS:
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("latin-1", errors="replace")


def _is_binary_value(tag_id, value, spec_type):
    """判断是否为不应按文本展示的二进制块（如 MakerNote / PrintIM 信息）。"""
    if not isinstance(value, (bytes, bytearray)):
        return False
    if (tag_id in _UNDEFINED_TEXT_TAGS or tag_id in _UTF16LE_TEXT_TAGS
            or tag_id in _BYTE_CODE_TAGS or tag_id in _ENUM_TAG_LABELS):
        # 文本标签、编码位标签、枚举标签都不算二进制块：
        # FileSource / SceneType 在 EXIF 规范里是 UNDEFINED 类型，但语义是枚举
        return False
    if spec_type == 2:
        return False
    if spec_type in (1, 6, 7):  # BYTE / SBYTE / UNDEFINED
        data = bytes(value).rstrip(b"\x00")
        # 短且全可打印的值（ExifVersion "0232"、OffsetTime "+08:00"…）依旧按文本展示
        if data and len(data) <= 32 and all(0x20 <= b <= 0x7E or b >= 0xA0 for b in data):
            return False
        return True
    return False



def _pil_exif_to_piexif_dict(image_bytes):
    """Pillow 兜底：当 piexif 无法解析时，用 Pillow 已解码的 EXIF 组装 piexif 风格字典。

    值为 Pillow 解码结果（str/int/float/bytes），不能直接 piexif.dump，
    但可用于 read_exif 展示，或经 _coerce_exif_value 转换后落盘。
    """
    exif_dict = {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}}
    try:
        img = Image.open(io.BytesIO(image_bytes))
        exif = img.getexif()
    except Exception:
        return exif_dict
    if not exif:
        return exif_dict

    for tag_id, value in exif.items():
        if tag_id in _EXIF_POINTER_TAGS:
            continue
        exif_dict["0th"][tag_id] = value

    # Exif 子 IFD 才是拍摄参数（曝光/光圈/ISO/焦距/时间等），通过 0x8769 指针进入
    try:
        sub_ifd = exif.get_ifd(0x8769)
    except Exception:
        sub_ifd = {}
    for tag_id, value in sub_ifd.items():
        if tag_id == 0xA005:  # InteropOffset，结构指针
            continue
        exif_dict["Exif"][tag_id] = value

    # GPS 经 Pillow 解析为嵌套 float 元组，无法直接 dump，本项目不使用故不保留
    return exif_dict


# ---------------------------------------------------------------------------
# EXIF 结构自愈 & 相机身份标签恢复
#
# 背景（实测 Sony ILCE-6700 原片 9b34969c…jpg）：上游平台/工具做「去拍摄信息」时
# 只删 IFD0 的表项，而 IFD1（缩略图 IFD）里同一批标签的值往往原封不动；同时把
# Exif IFD 的 InteroperabilityTag(0xA005) 指针留成指向被删数据的死指针 ——
# piexif.load 遇到这种结构会直接抛 ValueError("incorrect value type to decode")，
# 历史上我们静默退回 Pillow 分支，真实 EXIF 就此丢失且用户无感。
#
# 下面三件事都不依赖 piexif 的类型解释，只按表项取字节，所以坏数据也读得动：
#   _read_ifd              —— 带边界校验的手写 IFD 读取
#   _repair_exif_pointers  —— 剔除指向被删数据的指针表项，让 piexif 重新可读
#   _recover_core_identity —— 主 IFD 缺身份标签时从 IFD1 取回（同一文件内的搬移）
# ---------------------------------------------------------------------------

_TIFF_TYPE_SIZES = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8}

# 相机身份标签：(tag_id, 展示键, 中文名)。上游「去拍摄信息」通常只删这几个
_CORE_IDENTITY_TAGS = (
    (ImageIFD.Make, "make", "相机制造商"),
    (ImageIFD.Model, "model", "相机型号"),
    (ImageIFD.Software, "software", "软件"),
    (ImageIFD.DateTime, "datetime", "日期时间"),
    (ImageIFD.Artist, "artist", "作者"),
    (ImageIFD.Copyright, "copyright", "版权"),
)


def _tiff_layout(tiff):
    """校验 TIFF 头，返回 (字节序, 第一个 IFD 的偏移)；非法返回 (None, None)。"""
    if not tiff or len(tiff) < 8 or tiff[:2] not in (b"II", b"MM"):
        return None, None
    endian = "<" if tiff[:2] == b"II" else ">"
    try:
        magic, offset = struct.unpack(endian + "HI", tiff[2:8])
    except struct.error:
        return None, None
    if magic != 42:
        return None, None
    return endian, offset


def _read_ifd(tiff, offset, limit=512):
    """手写 IFD 读取：只按表项取原始字节、不做类型解释，坏指针/坏类型也不会抛异常。

    返回 (entries, next_offset)，entries = [(tag, type, count, value_bytes, entry_offset)]。
    """
    endian, _ = _tiff_layout(tiff)
    if endian is None or offset is None or offset < 8 or offset + 2 > len(tiff):
        return [], None
    byteorder = "little" if endian == "<" else "big"
    count = int.from_bytes(tiff[offset:offset + 2], byteorder)
    # 只要求「计数 + 全部表项」在界内：Exif/GPS 子 IFD 后面不一定有 next 指针槽
    # （piexif 写出的子 IFD 就没有），按 TIFF 规范的 4 字节 next 指针只在存在时才读
    if count <= 0 or count > limit or offset + 2 + 12 * count > len(tiff):
        return [], None
    entries = []
    for i in range(count):
        entry = offset + 2 + 12 * i
        tag, typ, cnt = struct.unpack(endian + "HHI", tiff[entry:entry + 8])
        size = _TIFF_TYPE_SIZES.get(typ, 0) * cnt
        if 0 < size <= 4:
            value = tiff[entry + 8:entry + 8 + size]
        elif size > 4:
            voff = int.from_bytes(tiff[entry + 8:entry + 12], byteorder)
            value = tiff[voff:voff + size] if voff + size <= len(tiff) else b""
        else:
            value = b""  # 未知类型或 count=0：视为无效表项
        entries.append((tag, typ, cnt, value, entry))
    tail = offset + 2 + 12 * count
    next_off = int.from_bytes(tiff[tail:tail + 4], byteorder) if tail + 4 <= len(tiff) else 0
    return entries, next_off


def _ifd_target_valid(tiff, offset):
    """判断某偏移处是否是一个「结构完好」的 IFD（用来识别指向被删数据的死指针）。"""
    entries, _ = _read_ifd(tiff, offset)
    if not entries:
        return False
    for _tag, typ, _cnt, value, _entry in entries:
        if typ not in _TIFF_TYPE_SIZES or not value:
            return False
    return True


def _first_exif_tiff(image_bytes):
    """定位第一个 APP1(Exif) 段里的 TIFF 数据，返回 (tiff, tiff_start)。

    tiff_start 是 TIFF 数据在文件中的绝对偏移，改字节时需要它。找不到返回 (None, None)。
    """
    if not image_bytes or image_bytes[:2] != b"\xff\xd8":
        return None, None
    pos, total = 2, len(image_bytes)
    while pos + 4 <= total:
        if image_bytes[pos] != 0xFF:
            return None, None
        marker = image_bytes[pos + 1]
        if marker == 0x01 or 0xD0 <= marker <= 0xD8:
            pos += 2
            continue
        if marker == 0xDA:  # 图像数据开始，EXIF(APP1) 只会出现在它之前
            break
        seg_len = int.from_bytes(image_bytes[pos + 2:pos + 4], "big")
        if seg_len < 2 or pos + 2 + seg_len > total:
            return None, None
        if marker == 0xE1 and image_bytes[pos + 4:pos + 10] == b"Exif\x00\x00":
            return image_bytes[pos + 10:pos + 2 + seg_len], pos + 10
        pos += 2 + seg_len
    return None, None


def _drop_ifd_entry(buf, tiff_start, table_offset, entry_offset, next_field=False):
    """从 IFD 表里摘掉一条表项。next_field 表示表尾紧跟 4 字节 next IFD 指针。

    - `next_field=True`（IFD0 / IFD1：标准 TIFF 与 piexif 都这么写）：后续表项整体左移
      12 字节，并把这 4 字节 next 指针一起搬到新表尾，最后清零腾出来的 12 字节。
      漏掉这一步的后果实测过：next 指针留在旧位置，解析方按新计数去读就拿到垃圾偏移，
      IFD1 直接读崩（先前的「把最后一项搬到被摘位置」写法就是这么坏的）。
    - `next_field=False`（Exif / GPS 子 IFD）：它们表尾那 4 字节在 piexif 写出的文件里其实
      是**值区开头**（子 IFD 的 next 字段没人读，piexif 也不写），所以只左移表项、绝不碰表尾；
      被摘项本来就是最后一项时更是只把计数 -1、一个字节都不动。

    两种情况都只动表自身占用的表项槽位，表外的值区与其它 IFD 一律不受影响。
    """
    tiff = buf[tiff_start:]
    endian, _ = _tiff_layout(tiff)
    if endian is None:
        return False
    byteorder = "little" if endian == "<" else "big"
    count = int.from_bytes(tiff[table_offset:table_offset + 2], byteorder)
    index = (entry_offset - (table_offset + 2)) // 12
    if not (0 <= index < count):
        return False
    need = 12 * (count - index - 1) + (4 if next_field else 0)
    if need:
        block = bytes(tiff[table_offset + 2 + 12 * (index + 1):
                           table_offset + 2 + 12 * (index + 1) + need])
        if len(block) < need:  # 表尾 4 字节缺失（子 IFD 顶到文件末）时按 0 补齐
            block += b"\x00" * (need - len(block))
        start = tiff_start + table_offset + 2
        buf[start + 12 * index:start + 12 * index + need] = block
        if next_field:
            freed = start + 12 * (count - 1)
            buf[freed + 4:freed + 16] = b"\x00" * 12
    buf[tiff_start + table_offset:tiff_start + table_offset + 2] = (count - 1).to_bytes(2, byteorder)
    return True


def _exif_tag_map(tiff, first):
    """拍下 IFD0 / Exif / GPS / IFD1 的「标签 → (类型, 计数, 原始值字节)」整表快照。

    自愈前后各拍一张逐项比对：除了被剔除的死指针，其它任何标签的存在性、类型、计数、
    值都不允许变 —— 变了就说明自愈动到了不该动的地方，必须回滚。
    """
    endian, _ = _tiff_layout(tiff)
    if endian is None:
        return {}
    byteorder = "little" if endian == "<" else "big"

    def table(offset):
        return {t: (ty, c, v) for t, ty, c, v, _e in _read_ifd(tiff, offset)[0]}

    ifd0, next_off = _read_ifd(tiff, first)
    out = {"0th": table(first)}
    for name, tag in (("Exif", ImageIFD.ExifTag), ("GPS", ImageIFD.GPSTag)):
        ptr = next((int.from_bytes(v[:4], byteorder) for t, _ty, _c, v, _e in ifd0
                    if t == tag and len(v) >= 4), None)
        if ptr:
            out[name] = table(ptr)
    if next_off:
        out["1st"] = table(next_off)
    return out


def _repair_is_lossless(before, after):
    """自愈前后比对：除死指针外没有任何差异才算无损。"""
    for ifd_name in set(before) | set(after):
        a, b = before.get(ifd_name), after.get(ifd_name)
        if a is None or b is None:
            continue  # 整个子 IFD 消失 = 它的指针被剔除，属于预期内的修复
        for tag in set(a) | set(b):
            if tag in (ExifIFD.InteroperabilityTag, ImageIFD.GPSTag):
                continue  # 死指针本身就该消失
            if a.get(tag) != b.get(tag):
                return False
    return True


def _drop_bad_pointer_entries(buf, tiff_start):
    """剔除指向非法位置的指针表项（Exif IFD 的 Interop、IFD0 的 GPS）。

    每轮重新解析一次表（摘掉一条后索引会变），最多四轮足够覆盖常见损坏。
    返回被剔除项的说明列表。
    """
    removed = []
    for _ in range(4):
        tiff = bytes(buf[tiff_start:])
        endian, first = _tiff_layout(tiff)
        if endian is None:
            return removed
        byteorder = "little" if endian == "<" else "big"
        ifd0, _ = _read_ifd(tiff, first)
        exif_ptr = None
        for tag, _typ, _cnt, value, _entry in ifd0:
            if tag == ImageIFD.ExifTag and len(value) >= 4:
                exif_ptr = int.from_bytes(value[:4], byteorder)
        dropped = False
        if exif_ptr is not None:
            for tag, _typ, _cnt, value, entry in _read_ifd(tiff, exif_ptr)[0]:
                if tag != ExifIFD.InteroperabilityTag or len(value) < 4:
                    continue
                target = int.from_bytes(value[:4], byteorder)
                if not _ifd_target_valid(tiff, target):
                    if _drop_ifd_entry(buf, tiff_start, exif_ptr, entry):
                        removed.append(f"Interop 指针(0xA005)→{target}")
                        dropped = True
                    break
        if dropped:
            continue
        for tag, _typ, _cnt, value, entry in ifd0:
            if tag != ImageIFD.GPSTag or len(value) < 4:
                continue
            target = int.from_bytes(value[:4], byteorder)
            if target and not _ifd_target_valid(tiff, target):
                # IFD0 表尾紧跟 next IFD 指针（IFD1），移位时必须一起搬走
                if _drop_ifd_entry(buf, tiff_start, first, entry, next_field=True):
                    removed.append(f"GPS 指针(0x8825)→{target}")
                    dropped = True
                break
        if not dropped:
            break
    return removed


def _repair_exif_pointers(image_bytes):
    """剔除 EXIF 里指向被删数据的死指针，返回 (修好的字节, 剔除项说明)；无需修复时 (None, [])。

    自愈后必须通过「无损校验」（_repair_is_lossless）才采纳：宁可退回 Pillow 兜底，
    也不能为了读出标签而把某个值写坏。
    """
    tiff, tiff_start = _first_exif_tiff(image_bytes)
    if tiff is None:
        return None, []
    endian, first = _tiff_layout(tiff)
    if endian is None:
        return None, []
    before = _exif_tag_map(tiff, first)
    buf = bytearray(image_bytes)
    removed = _drop_bad_pointer_entries(buf, tiff_start)
    if not removed:
        return None, []
    repaired = bytes(buf)
    tiff2, _s2 = _first_exif_tiff(repaired)
    after = _exif_tag_map(tiff2, _tiff_layout(tiff2)[1])
    if not _repair_is_lossless(before, after):
        return None, []
    return repaired, removed


def _recover_core_identity(image_bytes, exif_dict=None):
    """主 IFD 缺相机身份标签时，从缩略图 IFD(IFD1) 找回来（同一文件内的搬移，不猜测不编造）。

    上游做「去拍摄信息」通常只删 IFD0 的表项，IFD1 里同一批标签往往原封不动
    （实测 Sony ILCE-6700：IFD0 只剩 7 项，IFD1 仍带 Make / Model / Software / DateTime）。
    返回 {"values": {tag_id: bytes}, "display": {"make": "SONY", ...}}；无可恢复项时两者皆空。
    """
    present = set((exif_dict or {}).get("0th", {}) or {})
    source = dict((exif_dict or {}).get("1st", {}) or {})
    if not source:
        # piexif 读不出 IFD1（Pillow 兜底分支）时用手写解析直接取原表项
        tiff, _start = _first_exif_tiff(image_bytes)
        if tiff is not None:
            _endian, first = _tiff_layout(tiff)
            if first is not None:
                next_off = _read_ifd(tiff, first)[1]
                if next_off:
                    source = {t: v for t, _ty, _c, v, _e in _read_ifd(tiff, next_off)[0]}

    values = {}
    for tag_id, _key, _label in _CORE_IDENTITY_TAGS:
        if tag_id in present:
            continue
        value = source.get(tag_id)
        if isinstance(value, (bytes, bytearray)):
            if not bytes(value).strip(b"\x00"):
                continue
            # 结尾 NUL 交给 piexif.dump 统一补，原样保留会叠成两个 NUL
            values[tag_id] = bytes(value).rstrip(b"\x00")
        elif isinstance(value, (int, tuple)) and value:
            values[tag_id] = value
    display = {key: _decode_text(values[tag_id], tag_id)
               for tag_id, key, _label in _CORE_IDENTITY_TAGS if tag_id in values}
    return {"values": values, "display": display}


def _promote_core_identity(image_bytes, exif_dict):
    """把主 IFD 缺失的相机身份标签从 IFD1 补进主 IFD（就地修改 exif_dict）。

    补进主 IFD 后，original.jpg、信息栏文本、导出的成片都会带上真实相机信息；
    缩略图本身仍不写盘（_dumpable_exif_bytes 里 1st 恒为空，避免崩溃与体积膨胀）。
    """
    recovered = _recover_core_identity(image_bytes, exif_dict)
    if recovered["values"]:
        exif_dict.setdefault("0th", {}).update(recovered["values"])
    return recovered


def _load_exif_dict(image_bytes):
    """容错读取 EXIF，返回 (exif_dict, from_piexif, warnings)。

    from_piexif=True 表示值可直接 piexif.dump；False 表示来自 Pillow 解码，落盘前需做类型转换。
    warnings 是结构问题的说明（非致命，但会丢信息，必须回显给用户，不能再静默降级）。
    IFD1(缩略图) 保留在返回值里供 _recover_core_identity 读取，写盘时仍不写出。
    """
    try:
        return piexif.load(image_bytes), True, []
    except Exception as exc:
        warnings = []
        try:
            repaired, removed = _repair_exif_pointers(image_bytes)
        except Exception:
            repaired, removed = None, []
        if repaired is not None:
            try:
                exif_dict = piexif.load(repaired)
                warnings.append("EXIF 结构损坏（" + "、".join(removed)
                                + " 指向被删除的数据），已剔除非法指针后完整解析")
                return exif_dict, True, warnings
            except Exception:
                pass
        detail = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
        warnings.append(f"EXIF 结构无法解析（{detail}），已按容错模式读取，部分标签可能缺失")
        return _pil_exif_to_piexif_dict(image_bytes), False, warnings


def _coerce_exif_value(value, tag_type=None):
    """把 Pillow 解码值转成 piexif.dump 要求的编码类型。

    Pillow 的 RATIONAL 标签返回 IFDRational（numbers.Rational 子类），
    需转成精确的 (分子, 分母) 元组后才能交给 piexif 序列化。
    个别相机以 RATIONAL 存整型标签（如 ISO 100/1），对整型标签归约为单个整数，
    避免按 spec 类型编码成多值数组导致显示异常。
    """
    integral = tag_type in (1, 3, 4, 6, 8, 9)  # BYTE/SHORT/LONG/SBYTE/SSHORT/SLONG
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, numbers.Rational):
        num, den = int(value.numerator), int(value.denominator)
        if integral:
            return num // den if den and num % den == 0 else round(num / den) if den else num
        return (num, den)
    if isinstance(value, str):
        return value.encode("utf-8")
    if isinstance(value, float):
        frac = Fraction(value).limit_denominator(1_000_000)
        num, den = frac.numerator, frac.denominator
        if integral:
            return num // den if num % den == 0 else round(num / den)
        return (num, den)
    if isinstance(value, tuple):
        return tuple(_coerce_exif_value(v, tag_type) for v in value)
    return value


def _dumpable_exif_bytes(exif_dict, from_piexif):
    """规范化 EXIF 字典并序列化：丢弃缩略图 IFD(1st) 与 piexif 不认识的标签。

    产出字节写盘后，后续任何 piexif.load 都能稳定重读，规避带缩略图的 EXIF 结构
    在 piexif 1.1.3 中触发 "incorrect value type to decode" 崩溃的问题。
    """
    tags = piexif._exif.TAGS
    out = {}
    for ifd in ("0th", "Exif", "GPS"):
        known = tags.get(ifd, {})
        source = exif_dict.get(ifd, {})
        if from_piexif:
            out[ifd] = {k: v for k, v in source.items() if k in known}
        else:
            out[ifd] = {k: _coerce_exif_value(v, known[k].get("type")) for k, v in source.items() if k in known}
    out["1st"] = {}
    return piexif.dump(out)


def _encode_ascii_text(value):
    """type=2(ASCII) 文本标签的写回编码：能用 ASCII 就用 ASCII，否则退回 UTF-8。

    EXIF 规范里 type=2 是 ASCII，但面板允许用户填中文（例如型号「索尼 ILCE-6700」）。
    一律 encode("utf-8") 会让严格按规范解析的软件显示乱码；一律 ASCII 又直接丢内容，
    所以优先 ASCII（合规），失败再 UTF-8（业界通行做法）。结尾 NUL 由 piexif.dump 统一补。
    """
    data = str(value).replace("\x00", "")
    try:
        return data.encode("ascii")
    except UnicodeEncodeError:
        return data.encode("utf-8")


def _canonical_exif_for_save(image_bytes, orientation=None, recovery=None):
    """生成可被 piexif 稳定重读的规范化 EXIF 字节；图片无 EXIF 时返回 None。

    orientation：非 None 时强制写入/覆盖 Orientation(0x0112)。
    上传时像素已经过 ImageOps.exif_transpose 摆正，必须把 Orientation 归一化成 1，
    否则浏览器/看图软件会「再转一次」，竖拍照片的水印方向就全错了。

    recovery：传入 dict 时写入本次的恢复信息
    （{"display": {"make": "SONY", ...}, "warnings": [...]}），供上传接口回显给用户：
    哪些身份标签是从缩略图 IFD 补回来的、EXIF 结构有什么问题。
    """
    exif_dict, from_piexif, warnings = _load_exif_dict(image_bytes)
    recovered = _promote_core_identity(image_bytes, exif_dict)
    if orientation is not None:
        exif_dict.setdefault("0th", {})[ImageIFD.Orientation] = int(orientation)
    if recovery is not None:
        recovery["display"] = recovered["display"]
        recovery["warnings"] = list(warnings)
    if not any(exif_dict.get(ifd) for ifd in ("0th", "Exif", "GPS")):
        return None
    try:
        return _dumpable_exif_bytes(exif_dict, from_piexif)
    except Exception:
        return None


def read_exif(image_bytes):
    """读取 EXIF，返回可直接下发给前端的字段表。

    文本字段按 ASCII/UTF-8/Shift-JIS 等多编码还原（日系相机常写 Shift-JIS，原先按
    UTF-8 errors="replace" 解码会整片变成 �）；MakerNote / PrintIM 这类二进制块不再
    当文本解码，而是标记 binary=True 交给前端只读展示。

    主 IFD 缺相机身份标签（Make/Model/Software/DateTime/Artist/Copyright）时，用缩略图
    IFD(IFD1) 里的同名标签补上展示，并标记 source="ifd1"（前端打「缩略图」徽章）——
    这是同一文件里已有的值，不是猜测；缺失时前端也会留空输入框让用户手填。
    """
    exif_dict, _from_piexif, _warnings = _load_exif_dict(image_bytes)

    result = {}
    for ifd_name in ("0th", "Exif"):
        ifd_data = exif_dict.get(ifd_name, {}) or {}
        for tag_id, value in ifd_data.items():
            if tag_id in _EXIF_POINTER_TAGS:  # 结构性指针标签，没有展示意义
                continue
            name = EXIF_TAG_NAMES.get(tag_id, f"Tag_{tag_id}")
            if name in result:
                # 极少数相机同时写入显示名相同的两个标签（历史上 ISO 34855/34867 同名），
                # 追加 tag_id 区分，避免后读到的值把前一个覆盖掉
                name = f"{name} ({tag_id})"
            if (isinstance(value, (tuple, list)) and value
                    and all(isinstance(v, int) for v in value)
                    and _spec_type(ifd_name, tag_id) in (1, 7)):
                # BYTE(1)/UNDEFINED(7) 类型的标签（XP 系列、ComponentsConfiguration 等）
                # piexif 会读成整数元组，先还原成字节才能按 UTF-16LE/文本处理
                value = bytes(v & 0xFF for v in value)
            if _is_binary_value(tag_id, value, _spec_type(ifd_name, tag_id)):
                result[name] = {
                    "tag_id": tag_id,
                    "ifd": ifd_name,
                    "raw": "",
                    "formatted": f"二进制数据（{len(value)} 字节）",
                    "binary": True,
                    "editable": False,
                }
                continue
            if isinstance(value, (bytes, bytearray)) and tag_id not in _BYTE_CODE_TAGS:
                code = _as_enum_code(value) if tag_id in _ENUM_TAG_LABELS else None
                # 枚举标签（FileSource/SceneType 等）先取出数值，其余按多编码文本还原；
                # 编码位标签保留原始字节，交给 format_exif_value 映射成 "Y Cb Cr -"
                value = code if code is not None else _decode_text(value, tag_id)
            formatted = format_exif_value(tag_id, value)
            result[name] = {
                "tag_id": tag_id,
                "ifd": ifd_name,
                # 编码位标签保留字节时 str() 是 b'..'，直接用展示文本更清爽
                "raw": formatted if tag_id in _BYTE_CODE_TAGS else str(value),
                "formatted": formatted,
                "binary": False,
                "editable": True,
            }

    # 主 IFD 缺身份标签时用缩略图 IFD(IFD1) 的值补上展示：同文件内的搬移，标记来源
    # source="ifd1" 让前端打徽章；用户可直接编辑保存，保存时会写进主 IFD0。
    recovered = _recover_core_identity(image_bytes, exif_dict)
    for tag_id, _key, label in _CORE_IDENTITY_TAGS:
        if tag_id not in recovered["values"]:
            continue
        name = EXIF_TAG_NAMES.get(tag_id, label)
        if name in result:
            name = f"{name} ({tag_id})"
        text = _decode_text(recovered["values"][tag_id], tag_id)
        result[name] = {
            "tag_id": tag_id,
            "ifd": "1st",
            "source": "ifd1",
            "raw": text,
            "formatted": text,
            "binary": False,
            "editable": True,
        }
    return result


def build_exif_bar_text(exif_data):
    """从 EXIF 提取相机信息栏四段内容：左(品牌+型号)、中(品牌)、右参数行、右日期行。"""
    make = str(exif_data.get("相机制造商", {}).get("raw", "")).strip()
    model = str(exif_data.get("相机型号", {}).get("raw", "")).strip()
    focal = str(exif_data.get("焦距", {}).get("formatted", "")).strip()
    fnum = str(exif_data.get("光圈值", {}).get("formatted", "")).strip()
    shutter = str(exif_data.get("曝光时间", {}).get("formatted", "")).strip().rstrip("sS")
    iso = str(exif_data.get("ISO感光度", {}).get("formatted", "")).replace(" ", "")
    # 日期兜底链：拍摄时间(36867) → 日期时间(306) → 数字化时间(36868)。
    # 306 常由缩略图 IFD 恢复而来，是「上游删了 36867」时唯一能用的日期来源。
    dt = ""
    for _date_key in ("拍摄时间", "日期时间", "数字化时间"):
        dt = str(exif_data.get(_date_key, {}).get("formatted", "") or "").strip()
        if dt:
            break

    left = " ".join(p for p in [make, model] if p)
    params = " ".join(p for p in [focal, fnum, shutter, iso] if p)

    date_str = dt
    m = re.match(r"(\d{4}):(\d{2}):(\d{2})\s+(.*)", dt)
    if m:
        date_str = f"{m.group(1)}.{m.group(2)}.{m.group(3)} {m.group(4)}"

    return left, make, params, date_str


# 信息栏文本缓存：按 original.jpg 的 mtime/size 失效，避免每帧都重新解析整份 EXIF
_exif_bar_cache = {"key": None, "text": ("", "", "", "")}


def _exif_bar_text():
    original_path = os.path.join(UPLOAD_DIR, _ORIGINAL_NAME)
    key = None
    if os.path.exists(original_path):
        st = os.stat(original_path)
        key = f"{st.st_mtime_ns}:{st.st_size}"
    if key != _exif_bar_cache["key"]:
        text = ("", "", "", "")
        if key:
            try:
                with open(original_path, "rb") as f:
                    text = build_exif_bar_text(read_exif(f.read()))
            except Exception:
                text = ("", "", "", "")
        _exif_bar_cache.update({"key": key, "text": text})
    return _exif_bar_cache["text"]


def _clear_exif_bar_cache():
    _exif_bar_cache.update({"key": None, "text": ("", "", "", "")})


def _wm_exif_text(wm_data):
    """信息栏 / 画框四行的文字来源（4 元组）。

    交互路径（浏览器）从 uploads/original.jpg 读并缓存；**批量生成**要给每张图喂它
    自己的 EXIF，走 payload 里的内部键 `_exif_text`：它不是模板参数（不进白名单、
    不进配置、不参与回显），所以老路径逐像素不变。批量内核见 scripts/batch_core.py。
    """
    cached = (wm_data or {}).get("_exif_text")
    if isinstance(cached, (list, tuple)) and len(cached) == 4:
        return tuple(str(x or "") for x in cached)
    return _exif_bar_text()


def get_image_info(image_bytes):
    img = Image.open(io.BytesIO(image_bytes))
    return {
        "width": img.width,
        "height": img.height,
        "format": img.format or "Unknown",
        "mode": img.mode,
    }


def apply_opacity(rgba_img, opacity):
    r, g, b, a = rgba_img.split()
    a = a.point(lambda x: int(x * opacity / 255))
    return Image.merge("RGBA", (r, g, b, a))


def _paste_layer(overlay, piece, x, y):
    """把 piece 以正确的 alpha 合成（over）方式贴进 overlay 的 (x, y) 处。

    注意：**不能**用 overlay.paste(piece, (x, y), piece)——Pillow 的 paste 会把 RGB
    预乘上 alpha，半透明水印会明显变暗：alpha=128 的纯红贴在蓝底上得到 (55,45,67)，
    而正确结果是 (143,30,45)。crop → alpha_composite → paste 才是真正的 over 合成，
    并且支持超出画布的坐标（crop 会自动补透明边，paste 会自动裁剪）。
    """
    x, y = int(x), int(y)
    region = overlay.crop((x, y, x + piece.width, y + piece.height))
    region.alpha_composite(piece)
    overlay.paste(region, (x, y))


def parse_color(hex_str, default=(255, 255, 255)):
    """把 #RRGGBB / #RGB 安全解析为 RGB；非法值回退 default（避免 500 而前端只看到“无反应”）。"""
    try:
        s = str(hex_str or "").strip().lstrip("#")
        if len(s) == 3:
            s = "".join(ch * 2 for ch in s)
        if len(s) < 6:
            return default
        return int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)
    except (ValueError, TypeError):
        return default


def _safe_float(value, default):
    """容错转 float：前端可能传来空串/字符串/NaN，一律回退默认值。"""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    if v != v or v in (float("inf"), float("-inf")):
        return default
    return v


def _safe_int(value, default):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _clamp(value, low, high):
    return max(low, min(high, value))


# 字体候选路径：优先中文字体（信息栏与默认水印文字都含中文），再退到西文
_CJK_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",      # 文泉驿微米黑（部署环境）
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "C:/Windows/Fonts/msyh.ttc",                           # 微软雅黑
    "C:/Windows/Fonts/simhei.ttf",                         # 黑体
    "/System/Library/Fonts/PingFang.ttc",
)
_CJK_BOLD_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",        # 文泉驿正黑（部署环境）
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
    "C:/Windows/Fonts/msyhbd.ttc",                         # 微软雅黑 Bold
    "C:/Windows/Fonts/simhei.ttf",                         # 黑体（本身即粗）
    "/System/Library/Fonts/PingFang.ttc",
)
_LATIN_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "C:/Windows/Fonts/arial.ttf",
)
_LATIN_BOLD_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
)


# 用一个私有区码位探测字体的 .notdef（缺字）字形：缺字时 getmask 结果与它完全一致
_NOTDEF_PROBE = "\ue123"
_glyph_probe_cache = {}


def _default_font(size):
    try:
        return ImageFont.load_default(size)  # Pillow ≥10.1 支持可缩放默认字体
    except TypeError:
        return ImageFont.load_default()


def _mask_signature(mask):
    """字形掩码指纹：尺寸 + 墨迹 bbox + 直方图。

    ImagingCore 没有 tobytes()/tostring()（Pillow 11 实测），所以用这三者组合当指纹。
    """
    bbox = mask.getbbox()
    if bbox is None:
        return (mask.size, None, None)
    return (mask.size, bbox, tuple(mask.histogram()))


def _font_has_glyph(font, ch):
    """判断字体是否真的含有 ch 的字形。

    arial/DejaVuSans 这类西文字体没有中文字形，Pillow 会静默画 .notdef 方框（不报错），
    所以不能只看“字体能否加载”——必须比对字形掩码与 .notdef 是否相同。
    """
    key = (getattr(font, "path", ""), getattr(font, "size", 0), ch)
    cached = _glyph_probe_cache.get(key)
    if cached is not None:
        return cached
    try:
        notdef = _mask_signature(font.getmask(_NOTDEF_PROBE))
        sig = _mask_signature(font.getmask(ch))
    except Exception:
        result = False
    else:
        # bbox 为 None ⇒ 该字符没有任何像素；与 .notdef 指纹相同 ⇒ 画的是缺字方框
        result = sig[1] is not None and sig != notdef
    _glyph_probe_cache[key] = result
    return result


def _font_covers_text(font, text):
    """文本里所有非空白字符都有字形才返回 True。"""
    for ch in set(text):
        if ch.isspace():
            continue
        if not _font_has_glyph(font, ch):
            return False
    return True


def load_cjk_font(size):
    """中文字体（相机信息栏、含中文的水印文字）。"""
    for path in _CJK_FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    return _default_font(size)


def load_watermark_font(size, bold=False, text=""):
    """水印文字字体：按文本实际字符挑字体，缺字自动换字体，避免中文渲染成空心方框。

    候选顺序 = 粗体（如需）→ 西文 → 中文；逐个用 _font_covers_text 验证字形覆盖，
    第一个全覆盖的即返回；都不覆盖时返回第一个能加载的字体。
    """
    text = str(text or "")
    if bold:
        candidates = list(_CJK_BOLD_FONT_CANDIDATES) + list(_LATIN_BOLD_FONT_CANDIDATES)
    else:
        candidates = list(_LATIN_FONT_CANDIDATES)
    candidates += list(_CJK_FONT_CANDIDATES)

    # 纯 ASCII 文本无需逐个探测字形（西文字体都覆盖），直接走快路径
    fast = text.isascii()
    fallback = None
    for path in candidates:
        try:
            font = ImageFont.truetype(path, size)
        except Exception:
            continue
        if fallback is None:
            fallback = font
        if fast or _font_covers_text(font, text):
            return font
    return fallback or _default_font(size)



# ---------------------------------------------------------------------------
# 图片水印存储：支持多次导入，每个文件为 watermark_<id>.png
# 旧接口的 watermark.png 继续保留（老页面缓存只发单张、不带 items）
# ---------------------------------------------------------------------------
_LEGACY_WM_NAME = "watermark.png"
_WATERMARK_PREFIX = "watermark_"
_WATERMARK_FILE_RE = re.compile(r"^watermark_([0-9a-zA-Z]{1,32})\.png$")
_WATERMARK_ID_RE = re.compile(r"^[0-9a-zA-Z]{1,32}$")
_WATERMARK_LAYER_MAX_DIM = 1000   # 前端 DOM 代理层用的预览尺寸
_WATERMARK_CACHE_LIMIT = 8

# 水印图内存缓存（只在 _io_lock 内读写）：拖动/预览每帧都要用，避免反复解码大 PNG
_watermark_cache = {"order": [], "items": {}}


def _watermark_path(wm_id):
    """水印 id → 文件路径；严格过滤 id，避免路径穿越。"""
    safe = str(wm_id or "").strip()
    if not _WATERMARK_ID_RE.match(safe):
        return None
    return os.path.join(UPLOAD_DIR, f"{_WATERMARK_PREFIX}{safe}.png")


def _list_watermark_ids():
    try:
        names = os.listdir(UPLOAD_DIR)
    except OSError:
        return []
    found = (_WATERMARK_FILE_RE.match(n) for n in names)
    return sorted(m.group(1) for m in found if m)


# ---------------------------------------------------------------------------
# 品牌贴图库（brands/）：信息栏里的「厂商文字」可以换成自己上传的品牌贴图
#
# 与图片水印的区别是**生命周期**：水印随 /api/reset 清掉，贴图是长期资产，
# 所以放 uploads/ 之外的目录 + 一份清单（清单只存名字等元信息，丢了也能照常出图）。
# 文件名 brand_<id>.png，id 与图片水印同款（^[0-9a-zA-Z]{1,32}$）过滤路径穿越。
# ---------------------------------------------------------------------------
_BRAND_PREFIX = "brand_"
_BRAND_FILE_RE = re.compile(r"^brand_([0-9a-zA-Z]{1,32})\.png$")
_BRAND_INDEX_NAME = "index.json"


def _brand_path(brand_id):
    """贴图 id → 文件路径；严格过滤 id，避免路径穿越。"""
    safe = str(brand_id or "").strip()
    if not _WATERMARK_ID_RE.match(safe):
        return None
    return os.path.join(BRAND_DIR, f"{_BRAND_PREFIX}{safe}.png")


def _list_brand_ids():
    try:
        names = os.listdir(BRAND_DIR)
    except OSError:
        return []
    found = (_BRAND_FILE_RE.match(n) for n in names)
    return sorted(m.group(1) for m in found if m)


def _brand_index_path():
    return os.path.join(BRAND_DIR, _BRAND_INDEX_NAME)


def _load_brand_index():
    """清单 {id: {name,width,height,uploaded_at}}；缺失/损坏一律降级为空表。"""
    try:
        with open(_brand_index_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): v for k, v in data.items() if isinstance(v, dict)}


def _save_brand_index(index):
    """原子写清单：先写临时文件再替换，避免半个 JSON 让下次启动读崩。"""
    path = _brand_index_path()
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(index, f, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def _load_brand_image(brand_id):
    return _load_cached_rgba(_brand_path(brand_id))


def _brand_items():
    """已上传贴图列表（含清单里的名字与原始尺寸，缺清单的用 id 兜底），新的排前面。"""
    index = _load_brand_index()
    items = []
    for brand_id in _list_brand_ids():
        path = _brand_path(brand_id)
        img = _load_cached_rgba(path)
        if img is None:
            continue
        meta = index.get(brand_id) or {}
        try:
            uploaded_at = float(meta.get("uploaded_at") or os.stat(path).st_mtime)
        except OSError:
            uploaded_at = 0.0
        items.append({
            "id": brand_id,
            "name": str(meta.get("name") or f"{_BRAND_PREFIX}{brand_id}.png")[:120],
            "width": int(meta.get("width") or img.width),
            "height": int(meta.get("height") or img.height),
            "uploaded_at": uploaded_at,
            "source": str(meta.get("source") or ""),   # 非空 = 从 brands/ 目录收编来的
            "preview": _data_url(_watermark_layer_preview(img), "image/png"),
        })
    items.sort(key=lambda it: (-it["uploaded_at"], it["id"]))
    return items


_LOOSE_BRAND_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff")


def _adopt_loose_brands():
    """把直接丢进 brands/ 的散图收编进贴图库。

    场景：用户手动把 logo 拷进 brands/ 目录，期望它出现在「品牌贴图」里 ——
    但库只认 brand_<id>.png（id 走 ^[0-9a-zA-Z]{1,32}$ 过滤路径穿越），
    散图会一直隐形，看着就像「功能没做」。
    收编方式是**改名 + 重新编码成 PNG**（save 到 brand_<id>.png 后删源文件）而不是复制：
    否则删掉贴图后源文件还在，下次列表又把它收编回来，像删不掉。
    清单里记 source（原文件名）⇒ 界面显示的仍是用户认得的那张图。
    读不动/非图片的文件原样留着、跳过；任何失败都不抛异常。
    返回本次新收编的 id 列表。
    """
    try:
        names = os.listdir(BRAND_DIR)
    except OSError:
        return []
    index = _load_brand_index()
    added = []
    for name in sorted(names):
        if name == _BRAND_INDEX_NAME or name.startswith(_BRAND_INDEX_NAME + "."):
            continue
        if _BRAND_FILE_RE.match(name) or not name.lower().endswith(_LOOSE_BRAND_EXTS):
            continue
        src = os.path.join(BRAND_DIR, name)
        if not os.path.isfile(src):
            continue
        try:
            stamp = os.stat(src).st_mtime
            img = Image.open(src)
            img.load()
            img = img.convert("RGBA")
        except Exception:
            continue          # 不是图片 / 读不动 → 原样留着，只是不当贴图
        brand_id = uuid.uuid4().hex[:8]
        dst = _brand_path(brand_id)
        if dst is None:
            continue
        try:
            img.save(dst, "PNG")
            os.remove(src)
        except OSError:
            continue
        index[brand_id] = {"name": name[:120], "width": img.width, "height": img.height,
                           "uploaded_at": stamp, "source": name[:120], "adopted": True}
        added.append(brand_id)
    if added:
        _save_brand_index(index)
        _clear_watermark_cache()
    return added


# ---------------------------------------------------------------------------
# 字体库（fonts/）：信息栏四段文字可以指定字体（系统字体 / 自己上传的字体文件）
#
# 与品牌贴图同一套生命周期约定：长期资产（跨会话保留、/api/reset 不清），
# 只由字体旁的「删除」按钮清掉；上传的字体存 fonts/font_<id>.ttf + 一份清单。
# 系统字体按 id 引用（_FONT_FAMILIES）：只列常用字体，且请求时按文件是否存在过滤
# ⇒ 下拉里出现的字体一定是本机真有的，不会「选了个不存在的字体」还看不出问题。
# ---------------------------------------------------------------------------
_FONT_PREFIX = "font_"
_FONT_FILE_RE = re.compile(r"^font_([0-9a-zA-Z]{1,32})\.ttf$")
_FONT_INDEX_NAME = "index.json"
_FONT_MAX_BYTES = 20 * 1024 * 1024
# 上传的字体统一存成 .ttf：FreeType 认的是文件头而不是扩展名，OTF/TTC 的内容
# 存成 .ttf 照样能加载，于是不必维护「扩展名白名单」这一层。
_FONT_PROBE_TEXT = "Ag8"
_FONT_AUTO_LABEL = "自动（按文字语言挑选）"
_FONT_UPLOAD_GROUP = "我上传的"

# (id, 中文名, regular, bold|None)：只列信息栏排版用得上的常用字体
_FONT_FAMILIES = (
    ("msyh", "微软雅黑", "C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/msyhbd.ttc"),
    ("simhei", "黑体", "C:/Windows/Fonts/simhei.ttf", "C:/Windows/Fonts/simhei.ttf"),
    ("simsun", "宋体", "C:/Windows/Fonts/simsun.ttc", "C:/Windows/Fonts/simsunb.ttf"),
    ("deng", "等线", "C:/Windows/Fonts/Deng.ttf", "C:/Windows/Fonts/Dengb.ttf"),
    ("kaiti", "楷体", "C:/Windows/Fonts/simkai.ttf", None),
    ("stkaiti", "华文楷体", "C:/Windows/Fonts/STKAITI.TTF", None),
    ("stsong", "华文宋体", "C:/Windows/Fonts/STSONG.TTF", None),
    ("arial", "Arial", "C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/arialbd.ttf"),
    ("georgia", "Georgia", "C:/Windows/Fonts/georgia.ttf", "C:/Windows/Fonts/georgiab.ttf"),
    ("times", "Times New Roman", "C:/Windows/Fonts/times.ttf", "C:/Windows/Fonts/timesbd.ttf"),
    ("consolas", "Consolas", "C:/Windows/Fonts/consola.ttf", "C:/Windows/Fonts/consolab.ttf"),
    ("segoe", "Segoe UI", "C:/Windows/Fonts/segoeui.ttf", "C:/Windows/Fonts/segoeuib.ttf"),
    ("wqy", "文泉驿微米黑", "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
     "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"),
    ("noto", "Noto Sans CJK", "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
     "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"),
    ("pingfang", "苹方", "/System/Library/Fonts/PingFang.ttc", None),
)
_FONT_FAMILY_LABELS = {fid: label for fid, label, _reg, _bold in _FONT_FAMILIES}


def _font_path(font_id):
    """上传字体 id → 文件路径；严格过滤 id，避免路径穿越。"""
    safe = str(font_id or "").strip()
    if not _WATERMARK_ID_RE.match(safe):
        return None
    return os.path.join(FONT_DIR, f"{_FONT_PREFIX}{safe}.ttf")


def _list_font_ids():
    try:
        names = os.listdir(FONT_DIR)
    except OSError:
        return []
    found = (_FONT_FILE_RE.match(n) for n in names)
    return sorted(m.group(1) for m in found if m)


def _font_index_path():
    return os.path.join(FONT_DIR, _FONT_INDEX_NAME)


def _load_font_index():
    """清单 {id: {name, uploaded_at}}；缺失/损坏一律降级为空表。"""
    try:
        with open(_font_index_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): v for k, v in data.items() if isinstance(v, dict)}


def _save_font_index(index):
    """原子写清单（临时文件 + os.replace），避免半个 JSON 让下次启动读崩。"""
    path = _font_index_path()
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(index, f, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def _font_upload_name(font_id):
    """上传字体在界面上的名字（清单优先，其次文件名）。"""
    meta = _load_font_index().get(str(font_id)) or {}
    return str(meta.get("name") or f"{_FONT_PREFIX}{font_id}.ttf")[:120]


def _font_items():
    """已上传的字体列表（新的排前面）；系统字体不算「上传的」，只在下拉里出现。"""
    items = []
    for font_id in _list_font_ids():
        path = _font_path(font_id)
        if not path:
            continue
        try:
            stat = os.stat(path)
        except OSError:
            continue
        meta = _load_font_index().get(font_id) or {}
        try:
            stamp = float(meta.get("uploaded_at") or stat.st_mtime)
        except (TypeError, ValueError):
            stamp = stat.st_mtime
        items.append({"id": font_id, "name": _font_upload_name(font_id),
                      "uploaded_at": stamp, "size": stat.st_size})
    items.sort(key=lambda it: (-it["uploaded_at"], it["id"]))
    return items


def _font_choice_options():
    """字体下拉的选项（value/label/group）；系统字体按文件存在过滤，上传的排后面。"""
    options = [{"value": "", "label": _FONT_AUTO_LABEL, "group": ""}]
    for fid, label, regular, _bold in _FONT_FAMILIES:
        if os.path.exists(regular):
            options.append({"value": fid, "label": label, "group": "系统字体"})
    for item in _font_items():
        options.append({"value": item["id"], "label": item["name"],
                        "group": _FONT_UPLOAD_GROUP})
    return options


def _resolve_font_choice(font_id):
    """字体 id → {id,label,path,bold_path,source}；空串/未收录/文件丢失返回 None。"""
    safe = str(font_id or "").strip()
    if not safe:
        return None
    for fid, label, regular, bold in _FONT_FAMILIES:
        if fid == safe and os.path.exists(regular):
            return {"id": fid, "label": label, "path": regular,
                    "bold_path": bold if (bold and os.path.exists(bold)) else None,
                    "source": "system"}
    path = _font_path(safe)
    if path and os.path.exists(path):
        return {"id": safe, "label": _font_upload_name(safe), "path": path,
                "bold_path": None, "source": "upload"}
    return None


# ---------------------------------------------------------------------------
# 水印模板（Preset）框架
#
# 目标：把「参照图那种相机信息栏」变成**数据**，而不是又写一套硬编码排版。
#
#   presets.json ──合并──▶ _DEFAULT_PRESETS（内置兜底，文件丢了也能用）
#        │
#        ├─ _PRESET_PARAM_SPECS：参数白名单（类型/范围/选项/默认值/UI 描述）
#        │   模板参数与前端 payload 都必须过这张表 ⇒ 手改 JSON 或改请求体
#        │   都无法把版面参数放大到离谱（越界夹紧、非法丢弃）
#        ├─ _resolve_watermark_payload：显式传入 > 模板 > 默认值
#        └─ _PRESET_RENDERERS：kind → 渲染器（目前 exif_bar → _draw_exif_bar）
#
# 前端控件也是后端下发的（GET /api/watermark/presets 返回 controls 描述），
# 所以「加模板 / 调数值」= 改 presets.json，前后端都不用动、服务也不用重启
# （模板按 mtime+size 缓存，改文件即失效）。
#
# 所有几何量都是「占画布宽度的比例」，因此 1600px 预览底图与全尺寸导出天然等比一致。
# 数值来自参照图 assets/9ef13dfc4aebca317e33d1b10c9e055.jpg 的像素实测
# （1440×1080 照片 + 197px 白条；用 scripts/check_bar_geometry.py 可复现）。
# ---------------------------------------------------------------------------
_PRESET_FILE_NAME = "presets.json"

# 信息栏四段：既是白条里的四个槽位，也是渲染顺序与回显顺序
_BAR_SEGMENTS = ("left", "center", "params", "date")

# 画框水印（Polaroid 风）四行，自上而下：主标题（厂商+型号）/ 品牌（厂商）/ 参数行 / 日期行
_FRAME_SEGMENTS = ("title", "brand", "params", "date")
_FRAME_ALIGN_CHOICES = ("left", "center", "right")
_FRAME_V_ALIGN_CHOICES = ("top", "middle", "bottom")
# 每行：中文名 / 字号默认（墨迹高 ÷ 照片宽）/ 加粗 / 颜色 / 默认是否显示
_FRAME_LINE_META = {
    "title": ("主标题", 0.030, True, "#000000", True),
    "brand": ("品牌行", 0.026, True, "#000000", False),
    "params": ("参数行", 0.018, False, "#000000", True),
    "date": ("日期行", 0.014, False, "#8d8d8d", False),
}


def _frame_line_param_specs():
    """画框水印四行的参数：与信息栏四段同一条口径（墨迹高度 / 宽度约束 / 字体 / 贴图），
    只是换成「一行」；对齐没有 auto（默认就是居中），因为画框只有一栏。
    """
    specs = {}
    for name, (label, cap, bold, color, show) in _FRAME_LINE_META.items():
        specs[f"{name}_show"] = {"type": "bool", "default": show, "label": f"显示{label}"}
        specs[f"{name}_text"] = {"type": "text", "default": "", "label": f"{label}文字",
                                 "hint": "留空则用 EXIF 里的对应内容"}
        specs[f"{name}_cap_ratio"] = {"type": "float", "min": 0.004, "max": 0.12, "default": cap,
                                      "label": f"{label}字号", "scale": 1000, "unit": "‰"}
        specs[f"{name}_bold"] = {"type": "bool", "default": bold, "label": f"{label}加粗"}
        specs[f"{name}_color"] = {"type": "color", "default": color, "label": f"{label}颜色"}
        specs[f"{name}_tracking"] = {"type": "float", "min": 0.0, "max": 1.0, "default": 0.0,
                                     "label": f"{label}字距"}
        specs[f"{name}_width_ratio"] = {"type": "float", "min": 0.0, "max": 0.8, "default": 0.0,
                                        "label": f"{label}文字宽度", "scale": 1000, "unit": "‰",
                                        "hint": "0=不约束；只在 ±25% 内横向量身"}
        specs[f"{name}_align"] = {"type": "choice", "choices": _FRAME_ALIGN_CHOICES,
                                  "default": "center", "label": f"{label}对齐"}
        specs[f"{name}_font"] = {"type": "font_ref", "default": "", "label": f"{label}字体",
                                 "hint": "留空=跟随「文字字体」"}
        specs[f"{name}_image"] = {"type": "image_ref", "default": "", "label": f"{label}贴图",
                                  "hint": "在下方贴图条里点选；不选则这一行用文字"}
        specs[f"{name}_image_mono"] = {"type": "bool", "default": False,
                                       "label": f"{label}贴图单色化",
                                       "hint": "用下面的颜色给贴图上色（需透明底 PNG）"}
        # 单色化的颜色：**留空 = 跟随「{行}颜色」**（默认值 ⇒ 老观感一字不变），
        # 也可固定成某个色（照片 logo 的 photo_logo_color 就是「固定」那一档）
        specs[f"{name}_image_color"] = {"type": "color", "default": "", "optional": True,
                                        "label": f"{label}贴图单色化颜色",
                                        "empty_label": f"{label}颜色",
                                        "hint": f"留空 = 跟随「{label}颜色」；点「跟随文字颜色」可切回"}
        specs[f"{name}_image_opacity"] = {"type": "int", "min": 0, "max": 255, "default": 255,
                                          "label": f"{label}贴图不透明度"}
    return specs


# ---------------------------------------------------------------------------
# 画框类共用的参数骨架（photo_frame / blur_backdrop）
#
# 两个 kind 用**同一批键名**：留白与文字区、四行文字、照片 logo。这样两者能共用一套
# 渲染骨架（_draw_framed_photo）、一套前端控件、一套配置口径；各自的 spec 里只覆盖需要
# 换说法的标签与默认值（「背景模糊」把留白说成背景边、底色默认压暗）。
# 注意：键名重合 ⇒ 配置里必须靠 type / preset 定位参数表（_config_params_kind 就是
# 这么做的），别用「有哪些键」去猜 kind。
# ---------------------------------------------------------------------------
_FRAME_BOX_SPECS = {
    "frame_show": {"type": "bool", "default": True, "label": "加留白画框",
                   "hint": "关掉 = 照片四周不留白，只保留底边文字区（像信息栏那样）"},
    "frame_gap_ratio": {"type": "float", "min": 0.0, "max": 0.2, "default": 0.035,
                        "label": "留白宽度", "scale": 100, "unit": "%"},
    "frame_color": {"type": "color", "default": "#ffffff", "label": "留白颜色"},
    "frame_opacity": {"type": "int", "min": 0, "max": 255, "default": 255,
                      "label": "留白不透明度"},
    "frame_top": {"type": "bool", "default": True, "label": "上边留白"},
    "frame_bottom": {"type": "bool", "default": True, "label": "下边留白",
                     "hint": "不含底边文字区（那一块高度由下一个参数决定）"},
    "frame_left": {"type": "bool", "default": True, "label": "左边留白"},
    "frame_right": {"type": "bool", "default": True, "label": "右边留白"},
    "frame_bottom_extra_ratio": {"type": "float", "min": 0.0, "max": 0.6, "default": 0.18,
                                 "label": "底边文字区高度", "scale": 100, "unit": "%",
                                 "hint": "在「下边留白」之外再往下加的空白；文字写在这里，"
                                         "0 = 不写文字"},
    "frame_text_margin_ratio": {"type": "float", "min": 0.0, "max": 0.2, "default": 0.04,
                                "label": "文字区左右边距", "scale": 100, "unit": "%"},
    "frame_line_gap_ratio": {"type": "float", "min": 0.0, "max": 0.2, "default": 0.02,
                             "label": "行距", "scale": 100, "unit": "%"},
    "frame_text_valign": {"type": "choice", "choices": _FRAME_V_ALIGN_CHOICES,
                          "default": "middle", "label": "文字区垂直位置"},
    "frame_border_ratio": {"type": "float", "min": 0.0, "max": 0.02, "default": 0.0,
                           "label": "描边粗细", "scale": 1000, "unit": "‰",
                           "hint": "0 = 只留白不描边"},
    "frame_border_color": {"type": "color", "default": "#000000", "label": "描边颜色"},
    "font_family": {"type": "font_ref", "default": "", "label": "文字字体",
                    "hint": "四行文字的默认字体；留空=自动（按文字语言挑选）；"
                            "也可上传自己的字体文件"},
}

# 照片上的 logo：两个画框类 kind 共用同一套（brands/ 贴图库 + 墨迹口径）
# 位置是「照片盒」内的归一化中心点（0,0 = 照片左上），宽度占**照片宽**的比例
# ⇒ 留白宽度怎么改，logo 的尺寸与落点都不会跑偏（预览与导出仍等比）
_FRAME_LOGO_SPECS = {
    "photo_logo": {"type": "image_ref", "default": "", "label": "照片 logo",
                   "hint": "在下方贴图条里点选；选好后点「在图上拖动」自由摆放"},
    "photo_logo_scale": {"type": "float", "min": 0.02, "max": 1.0, "default": 0.25,
                         "label": "logo 宽度（占照片宽）", "scale": 100, "unit": "%"},
    "photo_logo_x": {"type": "float", "min": 0.0, "max": 1.0, "default": 0.5,
                     "label": "logo 水平位置", "scale": 100, "unit": "%"},
    "photo_logo_y": {"type": "float", "min": 0.0, "max": 1.0, "default": 0.9,
                     "label": "logo 垂直位置", "scale": 100, "unit": "%"},
    "photo_logo_rotation": {"type": "float", "min": -180.0, "max": 180.0, "default": 0.0,
                            "step": 1, "label": "logo 旋转", "unit": "°"},
    "photo_logo_opacity": {"type": "int", "min": 0, "max": 255, "default": 255,
                           "label": "logo 不透明度"},
    "photo_logo_color": {"type": "color", "default": "#000000", "label": "logo 单色化颜色"},
    "photo_logo_mono": {"type": "bool", "default": False, "label": "照片 logo 单色化",
                        "hint": "用上面的颜色给贴图上色（需透明底 PNG）"},
}

# 四行文字（主标题 / 品牌 / 参数 / 日期）：两个画框类 kind 共用同一套键名与口径
_FRAME_LINE_SPECS = _frame_line_param_specs()


# ---------------------------------------------------------------------------
# 背景模糊（blur_backdrop）专有参数：模糊底图 / 暗角 / 中心照片投影
#
# 底图 = 照片自己 cover 铺满画布后高斯模糊（半径 = bg_blur_ratio × 照片宽）；暗角 = 径向
# 渐变蒙版；投影 = 照片盒外扩 + 偏移 + 模糊。所有几何量都是「照片宽」的比值 ⇒ 1600px
# 预览与全尺寸导出天然等比。
# ---------------------------------------------------------------------------
_BACKDROP_SPECS = {
    "bg_blur_ratio": {"type": "float", "min": 0.0, "max": 0.08, "default": 0.025,
                      "label": "模糊强度", "scale": 100, "unit": "%",
                      "hint": "模糊半径占照片宽的比例；0 = 底图不模糊"},
    "bg_zoom_ratio": {"type": "float", "min": 1.0, "max": 1.5, "default": 1.06,
                      "label": "底图放大",
                      "hint": "先放大再模糊：模糊会把画面外的空白吃进来，放大一点就不会在边上发灰"},
    "bg_brightness": {"type": "float", "min": -100.0, "max": 100.0, "default": -18.0,
                      "label": "底图亮度", "hint": "负值压暗（文字更容易看清）"},
    "bg_saturation": {"type": "float", "min": 0.0, "max": 200.0, "default": 100.0,
                      "label": "底图饱和度", "scale": 100, "unit": "%"},
    "bg_vignette": {"type": "float", "min": 0.0, "max": 1.0, "default": 0.45,
                    "label": "暗角强度", "scale": 100, "unit": "%"},
    "bg_vignette_start": {"type": "float", "min": 0.0, "max": 0.98, "default": 0.30,
                          "label": "暗角起始", "scale": 100, "unit": "%",
                          "hint": "画面中心到这里为止不变暗，越靠边越暗"},
    "bg_vignette_color": {"type": "color", "default": "#000000", "label": "暗角颜色"},
    "photo_shadow_show": {"type": "bool", "default": True, "label": "照片投影"},
    "photo_shadow_blur_ratio": {"type": "float", "min": 0.0, "max": 0.06, "default": 0.018,
                                "label": "投影模糊", "scale": 100, "unit": "%"},
    "photo_shadow_x_ratio": {"type": "float", "min": -0.06, "max": 0.06, "default": 0.0,
                             "label": "投影水平偏移", "scale": 100, "unit": "%"},
    "photo_shadow_y_ratio": {"type": "float", "min": -0.06, "max": 0.06, "default": 0.006,
                             "label": "投影垂直偏移", "scale": 100, "unit": "%"},
    "photo_shadow_spread_ratio": {"type": "float", "min": 0.0, "max": 0.05, "default": 0.004,
                                  "label": "投影外扩", "scale": 100, "unit": "%"},
    "photo_shadow_opacity": {"type": "int", "min": 0, "max": 255, "default": 110,
                             "label": "投影不透明度"},
    "photo_shadow_color": {"type": "color", "default": "#000000", "label": "投影颜色"},
}


_PRESET_PARAM_SPECS = {
    "exif_bar": {
        # --- 版式 ---
        "bar_mode": {"type": "choice", "choices": ("append", "overlay"), "default": "overlay",
                     "label": "版式", "hint": "append=照片下方追加白条；overlay=叠加在照片上"},
        "bar_height_ratio": {"type": "float", "min": 0.02, "max": 0.5, "default": 0.13681,
                             "label": "信息栏高度", "scale": 100, "unit": "%"},
        "bar_color": {"type": "color", "default": "#ffffff", "label": "背景色"},
        "bar_opacity": {"type": "int", "min": 0, "max": 255, "default": 255, "label": "背景不透明度"},
        "margin_ratio": {"type": "float", "min": 0.0, "max": 0.2, "default": 0.04236,
                         "label": "左右边距", "scale": 100, "unit": "%"},
        # --- 分隔线 ---
        "divider_center_ratio": {"type": "float", "min": 0.1, "max": 0.95, "default": 0.62326,
                                 "label": "分隔线位置", "scale": 100, "unit": "%"},
        "divider_gap_ratio": {"type": "float", "min": 0.0, "max": 0.15, "default": 0.0229,
                              "label": "分隔线留白", "scale": 100, "unit": "%"},
        "divider_width_ratio": {"type": "float", "min": 0.0005, "max": 0.02, "default": 0.00278,
                                "label": "分隔线粗细", "scale": 1000, "unit": "‰"},
        "divider_height_ratio": {"type": "float", "min": 0.05, "max": 1.0, "default": 0.46193,
                                 "label": "分隔线长度", "scale": 100, "unit": "%"},
        "divider_color": {"type": "color", "default": "#c9c9c9", "label": "分隔线色"},
        # --- 右侧两行 ---
        "line_gap_ratio": {"type": "float", "min": 0.0, "max": 0.2, "default": 0.02083,
                           "label": "参数/日期行距", "scale": 100, "unit": "%"},
        "font_scale": {"type": "float", "min": 0.3, "max": 3.0, "default": 1.0,
                       "label": "字号缩放", "scale": 100, "unit": "%"},
        # --- 文字：左侧（品牌 + 型号） ---
        "left_cap_ratio": {"type": "float", "min": 0.004, "max": 0.12, "default": 0.025,
                           "label": "型号字号", "scale": 1000, "unit": "‰"},
        "left_bold": {"type": "bool", "default": True, "label": "型号加粗"},
        "left_color": {"type": "color", "default": "#000000", "label": "型号颜色"},
        "left_tracking": {"type": "float", "min": 0.0, "max": 1.0, "default": 0.0, "label": "型号字距"},
        "left_width_ratio": {"type": "float", "min": 0.0, "max": 0.6, "default": 0.0,
                             "label": "型号文字宽度", "scale": 1000, "unit": "‰",
                             "hint": "0=不约束；内容与参照图一致时才设（±25% 内横向量身）"},
        "left_text": {"type": "text", "default": "", "label": "型号文字", "hint": "留空则用 EXIF"},
        # --- 文字：中间（品牌） ---
        "center_cap_ratio": {"type": "float", "min": 0.004, "max": 0.12, "default": 0.01736,
                             "label": "品牌字号", "scale": 1000, "unit": "‰"},
        "center_bold": {"type": "bool", "default": True, "label": "品牌加粗"},
        "center_color": {"type": "color", "default": "#000000", "label": "品牌颜色"},
        "center_tracking": {"type": "float", "min": 0.0, "max": 1.0, "default": 0.22, "label": "品牌字距"},
        "center_width_ratio": {"type": "float", "min": 0.0, "max": 0.6, "default": 0.0,
                               "label": "品牌文字宽度", "scale": 1000, "unit": "‰",
                               "hint": "0=不约束；内容与参照图一致时才设（±25% 内横向量身）"},
        "center_text": {"type": "text", "default": "", "label": "品牌文字", "hint": "留空则用 EXIF"},
        # --- 文字：右侧参数行 ---
        "params_cap_ratio": {"type": "float", "min": 0.004, "max": 0.12, "default": 0.02153,
                             "label": "参数字号", "scale": 1000, "unit": "‰"},
        "params_bold": {"type": "bool", "default": False, "label": "参数加粗"},
        "params_color": {"type": "color", "default": "#000000", "label": "参数颜色"},
        "params_tracking": {"type": "float", "min": 0.0, "max": 1.0, "default": 0.0, "label": "参数字距"},
        "params_width_ratio": {"type": "float", "min": 0.0, "max": 0.6, "default": 0.0,
                               "label": "参数文字宽度", "scale": 1000, "unit": "‰",
                               "hint": "0=不约束；内容与参照图一致时才设（±25% 内横向量身）"},
        "params_text": {"type": "text", "default": "", "label": "参数文字", "hint": "留空则用 EXIF"},
        # --- 文字：右侧日期行 ---
        "date_cap_ratio": {"type": "float", "min": 0.004, "max": 0.12, "default": 0.01667,
                           "label": "日期字号", "scale": 1000, "unit": "‰"},
        "date_bold": {"type": "bool", "default": False, "label": "日期加粗"},
        "date_color": {"type": "color", "default": "#8d8d8d", "label": "日期颜色"},
        "date_tracking": {"type": "float", "min": 0.0, "max": 1.0, "default": 0.0, "label": "日期字距"},
        "date_width_ratio": {"type": "float", "min": 0.0, "max": 0.6, "default": 0.0,
                             "label": "日期文字宽度", "scale": 1000, "unit": "‰",
                             "hint": "0=不约束；内容与参照图一致时才设（±25% 内横向量身）"},
        "date_text": {"type": "text", "default": "", "label": "日期文字", "hint": "留空则用 EXIF"},
        # --- 品牌贴图：该段改用「自己上传的贴图」而不是厂商给的字 ---
        # id 为空 = 该段照旧用文字；几何量沿用上面的 *_cap_ratio / *_width_ratio
        # （贴图也裁到实测墨迹再缩放，所以「换字号」与「换贴图」落在同一视觉大小上）
        "left_image": {"type": "image_ref", "default": "", "label": "左段贴图",
                       "hint": "在下方贴图条里点选；不选则左段照旧用「厂商 + 型号」文字"},
        "center_image": {"type": "image_ref", "default": "", "label": "中间贴图",
                         "hint": "在下方贴图条里点选；不选则中间段照旧用厂商文字"},
        "params_image": {"type": "image_ref", "default": "", "label": "参数行贴图",
                         "hint": "在下方贴图条里点选；不选则参数行照旧用文字"},
        "date_image": {"type": "image_ref", "default": "", "label": "日期行贴图",
                       "hint": "在下方贴图条里点选；不选则日期行照旧用文字"},
        "left_hide_make": {"type": "bool", "default": False, "label": "左段隐藏厂商名",
                           "hint": "贴图已经代表品牌时，把左段的厂商名去掉，只留型号"},
        "left_image_mono": {"type": "bool", "default": False, "label": "左段贴图单色化",
                            "hint": "用下面的颜色给贴图上色（需透明底 PNG）"},
        "center_image_mono": {"type": "bool", "default": False, "label": "中间贴图单色化",
                              "hint": "用下面的颜色给贴图上色（需透明底 PNG）"},
        "params_image_mono": {"type": "bool", "default": False, "label": "参数行贴图单色化",
                              "hint": "用下面的颜色给贴图上色（需透明底 PNG）"},
        "date_image_mono": {"type": "bool", "default": False, "label": "日期行贴图单色化",
                            "hint": "用下面的颜色给贴图上色（需透明底 PNG）"},
        # 单色化的颜色：**留空 = 跟随该段文字颜色**（默认值 ⇒ 老观感一字不变）；
        # 想固定成某个色就点色盘（等于照片 logo 的 photo_logo_color 那一档）
        "left_image_color": {"type": "color", "default": "", "optional": True,
                             "label": "左段贴图单色化颜色", "empty_label": "型号颜色",
                             "hint": "留空 = 跟随「型号颜色」；点「跟随文字颜色」可切回"},
        "center_image_color": {"type": "color", "default": "", "optional": True,
                               "label": "中间贴图单色化颜色", "empty_label": "品牌颜色",
                               "hint": "留空 = 跟随「品牌颜色」；点「跟随文字颜色」可切回"},
        "params_image_color": {"type": "color", "default": "", "optional": True,
                               "label": "参数行贴图单色化颜色", "empty_label": "参数行颜色",
                               "hint": "留空 = 跟随「参数行颜色」；点「跟随文字颜色」可切回"},
        "date_image_color": {"type": "color", "default": "", "optional": True,
                             "label": "日期行贴图单色化颜色", "empty_label": "日期颜色",
                             "hint": "留空 = 跟随「日期颜色」；点「跟随文字颜色」可切回"},
        "left_image_opacity": {"type": "int", "min": 0, "max": 255, "default": 255,
                               "label": "左段贴图不透明度"},
        "center_image_opacity": {"type": "int", "min": 0, "max": 255, "default": 255,
                                 "label": "中间贴图不透明度"},
        "params_image_opacity": {"type": "int", "min": 0, "max": 255, "default": 255,
                                 "label": "参数行贴图不透明度"},
        "date_image_opacity": {"type": "int", "min": 0, "max": 255, "default": 255,
                               "label": "日期行贴图不透明度"},
        # --- 字体：四段文字各选一个字体；留空=跟随「信息栏字体」，再留空=自动挑体 ---
        # 值 = 系统字体 id（见 _FONT_FAMILIES）或自己上传的字体 id。
        # 换字体只换字形：几何量仍是 *_cap_ratio 的**墨迹口径**，所以版式不会跑偏
        # （选中的字体缺字时自动回落，并把原因写进 warnings / applied.bar 的 font 里）。
        "font_family": {"type": "font_ref", "default": "", "label": "信息栏字体",
                        "hint": "四段文字的默认字体；留空=自动（按文字语言挑选）；也可上传自己的字体文件"},
        "left_font": {"type": "font_ref", "default": "", "label": "型号字体",
                      "hint": "留空=跟随「信息栏字体」"},
        "center_font": {"type": "font_ref", "default": "", "label": "品牌字体",
                        "hint": "留空=跟随「信息栏字体」"},
        "params_font": {"type": "font_ref", "default": "", "label": "参数行字体",
                        "hint": "留空=跟随「信息栏字体」"},
        "date_font": {"type": "font_ref", "default": "", "label": "日期行字体",
                      "hint": "留空=跟随「信息栏字体」"},
    },
    # ---------------------------------------------------------------------
    # 画框水印（Polaroid 风）：照片四周留白 + 底边文字区（1~4 行）
    #
    # 与信息栏同一条口径：几何量一律「占照片宽度的比例」、文字按墨迹 bbox 对齐 ⇒
    # 1600px 预览与全尺寸导出必然等比；字体（fonts/）与贴图（brands/）共用同一批库。
    # 与信息栏的分工：信息栏是「照片下方追加一条参数带」，画框是「照片被留白包住，
    # 文字写在底边的空白里」—— 所以这里没有 bar/mode/divider，只有 frame + 四行文字。
    # ---------------------------------------------------------------------
    "photo_frame": {
        # 骨架（留白与文字区 / 照片 logo / 四行文字）与「背景模糊」共用，见 _FRAME_BOX_SPECS
        **_FRAME_BOX_SPECS,
        **_FRAME_LOGO_SPECS,
        **_FRAME_LINE_SPECS,
    },
    # ---------------------------------------------------------------------
    # 背景模糊（blur_backdrop）：外围底 = 照片自己的模糊画像（+暗角 +照片投影）
    #
    # 骨架与画框**完全一致**：键名不动（留白/文字区/四行文字/照片 logo）⇒ 共用同一套
    # 渲染骨架、控件与配置口径；只把「外面那圈怎么画」换成模糊底图，并多出 bg_* /
    # photo_shadow_* 两组旋钮。下面几条是**同键名换说法**（只改标签与默认值）：
    # 「留白」→「背景边」、「留白颜色」→「背景底色」（默认压暗成 #101418）。
    # ---------------------------------------------------------------------
    "blur_backdrop": {
        **_FRAME_BOX_SPECS,
        **_FRAME_LOGO_SPECS,
        **_FRAME_LINE_SPECS,
        **_BACKDROP_SPECS,
        "frame_show": {"type": "bool", "default": True, "label": "加背景边框",
                       "hint": "关掉 = 照片四周不留背景，只保留底边文字区（像信息栏那样）"},
        "frame_gap_ratio": {"type": "float", "min": 0.0, "max": 0.2, "default": 0.06,
                            "label": "背景边宽", "scale": 100, "unit": "%"},
        "frame_color": {"type": "color", "default": "#101418", "label": "背景底色",
                        "hint": "底图不透明度 < 255 时露出的颜色（想更暗就调它）"},
        "frame_opacity": {"type": "int", "min": 0, "max": 255, "default": 255,
                          "label": "底图不透明度",
                          "hint": "小于 255 时模糊底图朝「背景底色」淡出"},
        "frame_top": {"type": "bool", "default": True, "label": "上边背景"},
        "frame_bottom": {"type": "bool", "default": True, "label": "下边背景",
                         "hint": "不含底边文字区（那一块高度由下一个参数决定）"},
        "frame_left": {"type": "bool", "default": True, "label": "左边背景"},
        "frame_right": {"type": "bool", "default": True, "label": "右边背景"},
        "frame_bottom_extra_ratio": {"type": "float", "min": 0.0, "max": 0.6, "default": 0.20,
                                     "label": "底边文字区高度", "scale": 100, "unit": "%",
                                     "hint": "在「下边背景」之外再往下加的空白；文字写在这里，"
                                             "0 = 不写文字"},
    },
}

# 画框水印四行的中文名（回显/告警/控件分组都用它，避免三处各写一份）
_FRAME_LINE_LABELS = {"title": "主标题行", "brand": "品牌行", "params": "参数行", "date": "日期行"}
# 模板控件里每行旋钮的出场顺序（真实键 = {行}_{后缀}）
_FRAME_LINE_CONTROL_SUFFIXES = ("show", "text", "cap_ratio", "bold", "color", "align",
                                "width_ratio", "tracking", "font", "image", "image_mono",
                                "image_color", "image_opacity")
# 留白/文字区/描边的旋钮顺序
_FRAME_BOX_CONTROL_KEYS = ("frame_show", "frame_gap_ratio", "frame_color", "frame_opacity",
                           "frame_top", "frame_bottom", "frame_left", "frame_right",
                           "frame_bottom_extra_ratio", "frame_text_margin_ratio",
                           "frame_line_gap_ratio", "frame_text_valign",
                           "frame_border_ratio", "frame_border_color")
# 照片 logo 的旋钮顺序
_FRAME_LOGO_CONTROL_KEYS = ("photo_logo", "photo_logo_scale", "photo_logo_x", "photo_logo_y",
                            "photo_logo_rotation", "photo_logo_opacity", "photo_logo_color",
                            "photo_logo_mono")

# 「背景模糊」的旋钮顺序：背景边与文字区 → 模糊底图 → 暗角 → 照片投影 → 描边（骨架里的
# frame_border_* 在这套版式里是「整片描边」）→ 文字 → 四行 → 照片 logo
_BACKDROP_BOX_CONTROL_KEYS = ("frame_show", "frame_gap_ratio", "frame_color", "frame_opacity",
                              "frame_top", "frame_bottom", "frame_left", "frame_right",
                              "frame_bottom_extra_ratio", "frame_text_margin_ratio",
                              "frame_line_gap_ratio", "frame_text_valign")
_BACKDROP_IMAGE_CONTROL_KEYS = ("bg_blur_ratio", "bg_zoom_ratio", "bg_brightness", "bg_saturation")
_BACKDROP_VIGNETTE_CONTROL_KEYS = ("bg_vignette", "bg_vignette_start", "bg_vignette_color")
_BACKDROP_SHADOW_CONTROL_KEYS = ("photo_shadow_show", "photo_shadow_blur_ratio",
                                 "photo_shadow_x_ratio", "photo_shadow_y_ratio",
                                 "photo_shadow_spread_ratio", "photo_shadow_opacity",
                                 "photo_shadow_color")
_BACKDROP_BORDER_CONTROL_KEYS = ("frame_border_ratio", "frame_border_color")

# 每个画框类 kind 的模板偏好（只写与白名单默认值不同的那几处）
_FRAME_PRESET_TWEAKS = {
    "photo_frame": {
        # 白边比默认略宽一点更像拍立得；底边文字区默认放两行（主标题 + 参数行）
        "frame_gap_ratio": 0.05,
        "frame_bottom_extra_ratio": 0.22,
        "photo_logo_y": 0.9,      # 照片上的 logo 默认贴照片下缘（拍立得常见位置）
    },
    "blur_backdrop": {
        # 背景边要比画框留得更宽一点（否则模糊底图看不出名堂）；底边文字区两行高
        "frame_gap_ratio": 0.06,
        "frame_bottom_extra_ratio": 0.20,
        "photo_logo_y": 0.88,
    },
}


def _frame_preset_params(kind="photo_frame"):
    """画框类水印模板的参数 = 白名单默认值 + 该 kind 的偏好。

    直接从白名单生成 ⇒ 模板 params 与 spec 天然一一对应（前端每个旋钮都拿得到初值，
    不会踩「模板缺键的 float 控件显示量程下限」那个老坑）。两个 kind 键名一致，
    所以同一份生成器服务 photo_frame（画框）与 blur_backdrop（背景模糊）。
    """
    params = {key: spec["default"] for key, spec in _PRESET_PARAM_SPECS[kind].items()}
    params.update(_FRAME_PRESET_TWEAKS.get(kind, {}))
    return params


def _frame_preset_controls(kind="photo_frame"):
    """画框类模板的控件表（顺序/分组名按 kind 走，骨架部分完全共用）。

    顺序：文字区（+ 该 kind 的专有块）→ 文字（全局字体）→ 四行（各一组）→ 照片 logo。
    """
    if kind == "blur_backdrop":
        controls = [{"key": key, "group": "背景与文字区"} for key in _BACKDROP_BOX_CONTROL_KEYS]
        controls += [{"key": key, "group": "模糊底图"} for key in _BACKDROP_IMAGE_CONTROL_KEYS]
        controls += [{"key": key, "group": "暗角"} for key in _BACKDROP_VIGNETTE_CONTROL_KEYS]
        controls += [{"key": key, "group": "照片投影"} for key in _BACKDROP_SHADOW_CONTROL_KEYS]
        controls += [{"key": key, "group": "描边"} for key in _BACKDROP_BORDER_CONTROL_KEYS]
    else:
        controls = [{"key": key, "group": "留白与文字区"} for key in _FRAME_BOX_CONTROL_KEYS]
    controls += [{"key": "font_family", "group": "文字"}]     # 四行的默认字体（留空=自动挑体）
    for name in _FRAME_SEGMENTS:
        controls += [{"key": f"{name}_{suffix}", "group": _FRAME_LINE_LABELS[name]}
                     for suffix in _FRAME_LINE_CONTROL_SUFFIXES]
    controls += [{"key": key, "group": "照片 logo"} for key in _FRAME_LOGO_CONTROL_KEYS]
    return controls


# 内置兜底模板：presets.json 缺失/损坏时仍然可用；文件里的同名模板会整条覆盖它
_DEFAULT_PRESETS = {
    "camera_bar_brand_logo": {
        "name": "相机信息栏 · 我的水印",
        "kind": "exif_bar",
        "description": "参照图版式：照片下方追加白条；左=型号（可只留型号）、中=品牌贴图或厂商名、右=参数/日期；字体、颜色、贴图都能自己调",
        "params": {
            "bar_color": "#ffffff",
            "bar_height_ratio": 0.13681,
            "bar_mode": "append",
            "bar_opacity": 255,
            "center_bold": True,
            "center_cap_ratio": 0.01736,
            "center_color": "#000000",
            "center_image": "",
            "center_image_mono": False,
            "center_image_color": "",
            "center_image_opacity": 255,
            "center_tracking": 0.22,
            "center_width_ratio": 0.0993,
            "date_cap_ratio": 0.01667,
            "date_color": "#8d8d8d",
            "date_image": "",
            "date_image_mono": False,
            "date_image_color": "",
            "date_image_opacity": 255,
            "date_width_ratio": 0.1882,
            "divider_center_ratio": 0.62326,
            "divider_color": "#c9c9c9",
            "divider_gap_ratio": 0.0229,
            "divider_height_ratio": 0.46193,
            "divider_width_ratio": 0.00278,
            "left_bold": True,
            "left_cap_ratio": 0.025,
            "left_color": "#000000",
            "left_hide_make": True,
            "left_image": "",
            "left_image_mono": False,
            "left_image_color": "",
            "left_image_opacity": 255,
            "left_width_ratio": 0.2556,
            "line_gap_ratio": 0.02083,
            "margin_ratio": 0.04236,
            "params_cap_ratio": 0.02153,
            "params_color": "#000000",
            "params_image": "",
            "params_image_mono": False,
            "params_image_color": "",
            "params_image_opacity": 255,
            "params_width_ratio": 0.3111,
        },
        "controls": [
            { "key": "bar_mode", "group": "版式" },
            { "key": "bar_height_ratio", "group": "版式" },
            { "key": "margin_ratio", "group": "版式" },
            { "key": "divider_center_ratio", "group": "版式" },
            { "key": "font_scale", "group": "文字" },
            { "key": "left_text", "group": "文字" },
            { "key": "left_cap_ratio", "group": "文字" },
            { "key": "center_text", "group": "文字" },
            { "key": "center_cap_ratio", "group": "文字" },
            { "key": "params_text", "group": "文字" },
            { "key": "params_cap_ratio", "group": "文字" },
            { "key": "date_text", "group": "文字" },
            { "key": "date_cap_ratio", "group": "文字" },
            { "key": "center_tracking", "group": "文字" },
            { "key": "line_gap_ratio", "group": "文字" },
            { "key": "font_family", "group": "字体" },
            { "key": "left_font", "group": "字体" },
            { "key": "center_font", "group": "字体" },
            { "key": "params_font", "group": "字体" },
            { "key": "date_font", "group": "字体" },
            { "key": "divider_height_ratio", "group": "分隔线" },
            { "key": "divider_gap_ratio", "group": "分隔线" },
            { "key": "date_color", "group": "颜色" },
            { "key": "divider_color", "group": "颜色" },
            { "key": "center_image", "group": "品牌贴图" },
            { "key": "center_image_mono", "group": "品牌贴图" },
            { "key": "center_image_color", "group": "品牌贴图" },
            { "key": "center_image_opacity", "group": "品牌贴图" },
            { "key": "left_image", "group": "品牌贴图" },
            { "key": "left_hide_make", "group": "品牌贴图" },
            { "key": "left_image_mono", "group": "品牌贴图" },
            { "key": "left_image_color", "group": "品牌贴图" },
            { "key": "left_image_opacity", "group": "品牌贴图" },
            { "key": "params_image", "group": "品牌贴图" },
            { "key": "params_image_mono", "group": "品牌贴图" },
            { "key": "params_image_color", "group": "品牌贴图" },
            { "key": "params_image_opacity", "group": "品牌贴图" },
            { "key": "date_image", "group": "品牌贴图" },
            { "key": "date_image_mono", "group": "品牌贴图" },
            { "key": "date_image_color", "group": "品牌贴图" },
            { "key": "date_image_opacity", "group": "品牌贴图" },
        ],
    },
    "photo_frame_brand": {
        "name": "画框水印 · 拍立得",
        "kind": "photo_frame",
        "description": "拍立得风：照片四周留白、底边更高，文字写在底边空白里（主标题=厂商+型号、参数行，还可加品牌行/日期行）；四行的字体/贴图/对齐都能单独调，也能在照片上摆 logo",
        "params": _frame_preset_params("photo_frame"),
        "controls": _frame_preset_controls("photo_frame"),
    },
    "blur_backdrop_brand": {
        "name": "背景模糊 · 品牌信息",
        "kind": "blur_backdrop",
        "description": "背景模糊：用照片自己的模糊画像铺满外围（可压暗、降饱和、加暗角），中心照片带投影；底边留一块写四行文字（主标题=厂商+型号、品牌/参数/日期行），四行的字体/贴图/对齐都能单独调，也能在照片上摆 logo",
        "params": _frame_preset_params("blur_backdrop"),
        "controls": _frame_preset_controls("blur_backdrop"),
    },
}

# 模板缓存：按 presets.json 的 mtime+size 失效 ⇒ 改完 JSON 不必重启服务
_presets_cache = {"key": None, "presets": {}}

# 老前端只发这几个键（信息栏）：命中这套键就继续走历史排版，旧页面观感零变化
_LEGACY_EXIF_KEYS = ("font_size", "bar_color", "text_color", "bar_opacity",
                     "opacity", "position_x", "position_y", "rotation")

# 模板 kind（决定参数表与渲染器）→ 水印 type（决定 apply_watermark 走哪个分支）
_PRESET_KIND_TO_TYPE = {"exif_bar": "exif", "photo_frame": "frame",
                        "blur_backdrop": "blur",
                        "text": "text", "image": "image"}


def _preset_defaults(kind):
    """某类模板的默认参数（= 白名单里的 default）。"""
    return {key: spec["default"] for key, spec in _PRESET_PARAM_SPECS.get(str(kind) or "", {}).items()}


def _exif_bar_uses_engine(wm_data):
    """是否走模板排版引擎：带模板，或带了任何新参数（老前端只有 _LEGACY_EXIF_KEYS）。"""
    if wm_data.get("_preset") or str(wm_data.get("preset") or "").strip():
        return True
    legacy = set(_LEGACY_EXIF_KEYS)
    return any(key in wm_data for key in _PRESET_PARAM_SPECS["exif_bar"] if key not in legacy)


def _normalize_color(value):
    """把 #RGB/#RRGGBB 规范成小写 #rrggbb；非法返回 None（模板写错颜色要能被发现）。"""
    text = str(value or "").strip().lstrip("#")
    if len(text) == 3:
        text = "".join(ch * 2 for ch in text)
    if not re.fullmatch(r"[0-9a-fA-F]{6}", text):
        return None
    return "#" + text.lower()


def _sanitize_image_ref(value):
    """贴图 id 白名单：空串=不用贴图（合法），形状不对=丢弃（None）。

    与文件名里的 id 用同一套正则，所以模板/请求体里塞 "../x"、绝对路径之类的
    值根本走不到文件系统 —— 越界丢弃、非法键回显在 invalid_params 里。
    """
    safe = str(value or "").strip()
    if not safe:
        return ""
    return safe if _WATERMARK_ID_RE.match(safe) else None


def _coerce_param_value(spec, value):
    """按 spec 校验并转换单个参数；非法返回 None（调用方负责丢弃）。"""
    kind = spec.get("type")
    if kind == "float":
        parsed = _safe_float(value, None)
        return None if parsed is None else round(_clamp(parsed, spec["min"], spec["max"]), 6)
    if kind == "int":
        parsed = _safe_int(value, None)
        return None if parsed is None else int(_clamp(parsed, spec["min"], spec["max"]))
    if kind == "bool":
        return bool(value)
    if kind == "color":
        # optional 的颜色控件允许**空串**（= 跟随该段文字颜色，见 *_image_color）：
        # 空串是合法值，不能扔进 invalid_params —— 否则每次自动保存都报一遍「非法」，
        # 而且导出→导入会不幂等（空值被当成坏值丢掉，读回就变成 spec 的默认值）
        if spec.get("optional") and not str(value or "").strip():
            return ""
        return _normalize_color(value)
    if kind == "choice":
        return value if value in spec.get("choices", ()) else None
    if kind == "text":
        return str(value)[:200]
    if kind == "image_ref":
        # 空串（不用贴图）是合法值；形状不对的 id 返回 None 被丢弃并回显
        return _sanitize_image_ref(value)
    if kind == "font_ref":
        # 字体 id 与贴图 id 同形状（系统字体 id / 上传字体 id），校验规则共用；
        # 空串 = 自动挑体（合法）
        return _sanitize_image_ref(value)
    return None


def _preset_control_step(spec):
    """控件步长（显示单位）：用「人话十进制」而不是 (max-min)/N。

    原因：`<input type=range>` 的 value 会被浏览器**吸附到 step 网格**上，只要网格
    不是十进制整数格（例如 (50-2)/200 = 0.24），手输 13.68 就会被静默改成 13.76 ——
    数字框显示 2 位小数、网格却只有 0.24，用户看到的就是「输什么都跳到特定数」。
    所以网格粒度必须 = 显示小数位：float 0.01（超大区间降 0.1），int 1。
    """
    if spec.get("type") == "int":
        return 1
    explicit = spec.get("step")
    if isinstance(explicit, (int, float)) and not isinstance(explicit, bool):
        return explicit
    span = abs(float(spec.get("max", 1)) - float(spec.get("min", 0)))
    scale = abs(float(spec.get("scale", 1) or 1))
    # scale 是「显示单位 vs 参数单位」的倍数：跨度过大时 0.01 格会拖出上万格，
    # 这时按 0.1 显示（人眼已经分不出来，也不影响 2 位小数的手输精度）
    return 0.1 if span * max(1.0, scale) > 5000 else 0.01


def _sanitize_preset_params(kind, params):
    """按白名单过滤参数，返回 (干净副本, 被丢弃的键)。"""
    specs = _PRESET_PARAM_SPECS.get(str(kind) or "", {})
    clean, dropped = {}, []
    for key, value in dict(params or {}).items():
        spec = specs.get(key)
        if spec is None:
            dropped.append(str(key))
            continue
        parsed = _coerce_param_value(spec, value)
        if parsed is None:
            dropped.append(str(key))
        else:
            clean[key] = parsed
    return clean, dropped


def _preset_params(kind, wm_data):
    """某一类模板的最终参数 = 默认值 ← payload（payload 里已含模板合并结果）。

    单一事实来源：所有键都先过白名单与类型转换，渲染器里不再散落 _safe_float/_clamp，
    也不会出现「模板写了个陌生键就静默变默认值」这种查不出来的问题。
    """
    specs = _PRESET_PARAM_SPECS.get(str(kind) or "", {})
    params = _preset_defaults(kind)
    for key, spec in specs.items():
        if key not in wm_data:
            continue
        parsed = _coerce_param_value(spec, wm_data[key])
        if parsed is not None:
            params[key] = parsed
    return params


def _exif_bar_params(wm_data):
    """信息栏最终参数（保留旧名：调用处与测试都按这个键名找）。"""
    return _preset_params("exif_bar", wm_data)


def _presets_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), _PRESET_FILE_NAME)


def _resolve_preset(preset_id, raw, table, depth=0):
    """解析单条模板（含 extends 继承：子模板只写差异，params 深合并、controls 缺省继承）。"""
    if not isinstance(raw, dict) or depth > 4:
        return None
    base = {}
    parent_id = raw.get("extends")
    if parent_id:
        base = _resolve_preset(parent_id, table.get(parent_id), table, depth + 1) or {}
    kind = str(raw.get("kind") or base.get("kind") or "exif_bar")
    params = dict(base.get("params") or {})
    if isinstance(raw.get("params"), dict):
        params.update(raw["params"])
    clean, dropped = _sanitize_preset_params(kind, params)
    return {
        "id": str(preset_id),
        "name": str(raw.get("name") or base.get("name") or preset_id),
        "kind": kind,
        "description": str(raw.get("description") or base.get("description") or ""),
        "params": clean,
        "invalid_params": dropped,
        "controls": raw.get("controls", base.get("controls", [])) or [],
    }


def _load_presets():
    """内置模板 + presets.json 合并结果 {id: preset}；JSON 坏了只用内置（前端不该整页崩）。"""
    path = _presets_path()
    key = None
    if os.path.exists(path):
        st = os.stat(path)
        key = f"{st.st_mtime_ns}:{st.st_size}"
    if key != _presets_cache["key"]:
        table = {pid: dict(raw) for pid, raw in _DEFAULT_PRESETS.items()}
        if key:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                data = None
            if isinstance(data, dict):
                for pid, raw in data.items():
                    if str(pid).startswith("_") or not isinstance(raw, dict):
                        continue  # 以 _ 开头的是注释/元信息
                    table[str(pid)] = raw
        resolved = {}
        for pid, raw in table.items():
            preset = _resolve_preset(pid, raw, table)
            if preset:
                resolved[pid] = preset
        _presets_cache.update({"key": key, "presets": resolved})
    return _presets_cache["presets"]


def _clear_presets_cache():
    _presets_cache.update({"key": None, "presets": {}})


def _resolve_watermark_payload(wm_data):
    """把前端 payload 解析成最终参数：显式传入 > 模板 > 默认值。

    - 不带 preset ⇒ 只做白名单校验，老前端行为完全不变
    - 模板未知 ⇒ 回显 _preset_error，而不是静默当成默认样式
    """
    payload = dict(wm_data or {})
    kind = str(payload.get("type") or "text")
    preset_id = str(payload.get("preset") or "").strip()
    if preset_id:
        preset = _load_presets().get(preset_id)
        if preset is None:
            payload["_preset_error"] = f"未知模板：{preset_id}"
        else:
            # 注意两套词汇不能混：模板 kind（exif_bar，决定参数表与渲染器）
            # 与前端 payload 的 type（exif，决定 apply_watermark 走哪个分支）要分开
            kind = preset["kind"] or kind
            payload["type"] = _PRESET_KIND_TO_TYPE.get(kind, kind)
            for key, value in preset["params"].items():
                payload.setdefault(key, value)   # 显式传入优先于模板
            payload["_preset"] = {"id": preset_id, "name": preset["name"], "kind": kind}
    for key, spec in _PRESET_PARAM_SPECS.get(kind, {}).items():
        if key not in payload:
            continue
        parsed = _coerce_param_value(spec, payload[key])
        if parsed is None:
            payload.pop(key, None)
        else:
            payload[key] = parsed
    return payload


# ---------------------------------------------------------------------------
# 配置（cfg）持久化：把「调好的那一套参数」存下来，下次打开直接用
#
# 语义只有一条：**当前生效的配置永远只有 configs/current.json 一份真源**
#   configs/current.json  —— 自动保存槽：前端改完参数防抖落盘，页面一打开就还原
#   configs/<id>.json     —— 命名快照（另存为）；「切换」= 把快照拷回 current
#   configs/index.json    —— 快照清单（id → 名字/时间）；丢了就降级用 id 当名字
#
# 导出的文件与 current.json 是**同一种结构**，因为「合法化」只有 _normalize_config
# 一个入口 ⇒ 导出→导入 幂等；越界值被夹紧、未知键被丢弃回显（后端
# _resolve_watermark_payload 仍是最后一道闸，配置再野也进不了渲染器）。
# 与 brands/、fonts/ 同一套生命周期：长期资产，/api/reset 不清，
# 只有「恢复默认」（POST /api/config/clear）会清掉 current（命名快照仍保留）。
# ---------------------------------------------------------------------------
_CONFIG_CURRENT_NAME = "current.json"
_CONFIG_INDEX_NAME = "index.json"
_CONFIG_FILE_RE = re.compile(r"^([0-9a-zA-Z]{1,32})\.json$")
_CONFIG_APP = "camera-watermark"
_CONFIG_VERSION = 1
_CONFIG_MAX_BYTES = 1024 * 1024     # 配置文件上限（正常只有几 KB）
_CONFIG_MAX_SLOTS = 50              # 命名快照上限
_CONFIG_MAX_ITEMS = 20              # 图片水印最多恢复这么多张
_CONFIG_MAX_NAME = 40
# current.json / index.json 是固定文件名，不算「命名快照」；也用同一张表挡住
# 「把 slot 传成 current/index」这种请求（否则一个 index.json 能被当成配置套用）
_CONFIG_RESERVED_IDS = ("current", "index")
_CONFIG_TYPES = ("text", "image", "exif", "frame", "blur")
# 导出格式白名单直接复用导出模块的 _EXPORT_FORMATS（见「导出」一节，避免两处各写一份）
# 参数键白名单；app/version/exported_at/name 之类只是元信息，导入导出都要认（不算未知键）
_CONFIG_KNOWN_KEYS = ("version", "app", "type", "preset", "params", "text", "exif",
                      "legacy", "export", "image", "migrated_from_v11")
_CONFIG_META_KEYS = ("app", "version", "exported_at", "saved_at", "name", "description")

# ---------------------------------------------------------------------------
# v11 → v12 配置迁移：v11 的信息栏有一批参数在 v12 被删掉（组件开关 / 逐段对齐 /
# bar_layout / photo_logo* / border_*），而「画框水印」把这些能力换了个口径实现。
# 老的 current.json 与旧导出文件直接按 v12 白名单过一遍会把它们**静默丢掉**
# （用户看到的就是「我的参数没了」），所以这里显式搬家：能搬的搬、搬不动的报出来。
# ---------------------------------------------------------------------------
_V11_EXIF_KEYS = (
    "bar_show", "bar_layout", "divider_show",
    "left_align", "center_align", "params_align", "date_align",
    "left_show", "center_show", "params_show", "date_show",
    "photo_logo", "photo_logo_scale", "photo_logo_x", "photo_logo_y",
    "photo_logo_rotation", "photo_logo_color", "photo_logo_mono", "photo_logo_opacity",
    "border_show", "border_gap_ratio", "border_gap_color", "border_width_ratio",
    "border_color", "border_top", "border_bottom", "border_left", "border_right",
)

# v11 → 画框水印 的搬家表 {v11 键: 画框键}。同名键（params_* / date_* / photo_logo* /
# font_family）不写在这里：它们本来就在 photo_frame 白名单里，会原样留下。
# **两套东西各归各位，别混**：v11 的「白条」（bar_*）= 照片下方那条带子 ⇒ v12 的
# 「底边文字区」；v11 的「画框」（border_*）= 照片四周的留白 ⇒ v12 的「四周留白」。
# 把 bar_show 当成 frame_show（四周留白开关）会凭空给照片加上左右与**上方**的白边
# —— 实测就是这样「白条跑到照片上面」的，所以 frame_show 只认 border_show。
# 同一个目标键**先到先得**（见顺序）：白条那套（bar_*）优先于画框那套（border_*）。
_V11_EXIF_TO_FRAME = {
    # 白条 → 底边文字区（高度/颜色/不透明度）
    "bar_height_ratio": "frame_bottom_extra_ratio",
    "bar_color": "frame_color",
    "bar_opacity": "frame_opacity",
    "margin_ratio": "frame_text_margin_ratio",
    "line_gap_ratio": "frame_line_gap_ratio",
    # 画框 → 四周留白（border_show 缺席时由 _migrate_legacy_config 显式写 False）
    "border_show": "frame_show",
    "border_gap_ratio": "frame_gap_ratio",
    "border_gap_color": "frame_color",
    "border_width_ratio": "frame_border_ratio",
    "border_color": "frame_border_color",
    "border_top": "frame_top",
    "border_bottom": "frame_bottom",
    "border_left": "frame_left",
    "border_right": "frame_right",
    # 段 → 行：左段 → 主标题、中间段 → 品牌行、参数行/日期行同名
    "left_show": "title_show",
    "center_show": "brand_show",
    "left_text": "title_text",
    "center_text": "brand_text",
    "left_cap_ratio": "title_cap_ratio",
    "center_cap_ratio": "brand_cap_ratio",
    "left_bold": "title_bold",
    "center_bold": "brand_bold",
    "left_color": "title_color",
    "center_color": "brand_color",
    "left_tracking": "title_tracking",
    "center_tracking": "brand_tracking",
    "left_width_ratio": "title_width_ratio",
    "center_width_ratio": "brand_width_ratio",
    "left_font": "title_font",
    "center_font": "brand_font",
    "left_image": "title_image",
    "center_image": "brand_image",
    "left_image_mono": "title_image_mono",
    "center_image_mono": "brand_image_mono",
    "left_image_opacity": "title_image_opacity",
    "center_image_opacity": "brand_image_opacity",
    # 逐段对齐也要搬（值要过一遍 _V11_ALIGN_VALUES）
    "left_align": "title_align",
    "center_align": "brand_align",
    "params_align": "params_align",
    "date_align": "date_align",
}

# v11 的对齐多了个 auto（= 那个版式的历史锚点），画框只有一栏 ⇒ auto 就是居中
_V11_ALIGN_VALUES = {"auto": "center"}

# 判断「这份配置是不是 v11 时代存下的」只看这些键：它们在 v12 的画框白名单里都不存在，
# 见到任意一个就一定是 v11 信息栏参数。params_show / date_show / photo_logo* 是两代同名的
# **共享键**（本就进画框白名单），不能当痕迹，否则迁移后的配置一过 _normalize_config 又被
# 当成 v11 再迁一次（导出→导入就不幂等了）。
_V11_EXIF_MARKERS = tuple(k for k in _V11_EXIF_KEYS
                          if k not in _PRESET_PARAM_SPECS["photo_frame"])

# v11 的相机信息栏模板在 v12 的对应模板（迁移后 type=frame + 这个 preset）
_V11_EXIF_TARGET_PRESET = "photo_frame_brand"


def _config_defaults():
    """默认配置：结构恒定（键全在），数值与前端页面初始状态一致。"""
    return {
        "version": _CONFIG_VERSION,
        "app": _CONFIG_APP,
        "type": "text",
        "preset": "",
        "params": {},
        "text": {"text": "", "font_size": 36, "color": "#ffffff", "bold": False},
        "exif": {"font_size": 0, "bar_color": "#ffffff",
                 "text_color": "#000000", "bar_opacity": 240},
        "legacy": {"position_x": 0.5, "position_y": 0.5, "rotation": 0, "opacity": 128},
        "export": {"format": "jpeg", "quality": 95},
        "image": {"items": []},
    }


def _config_params_kind(cfg_type, preset):
    """params 用哪张白名单表：模板自带 kind 优先；模板被删就按水印类型找一张。

    一张表都找不到 ⇒ 返回空串，_sanitize_preset_params 会把 params 全部丢弃 ——
    宁可不留参数，也不能把没校验过的值当参数用。
    """
    kind = str((preset or {}).get("kind") or "")
    if kind in _PRESET_PARAM_SPECS:
        return kind
    for kind, wm_type in _PRESET_KIND_TO_TYPE.items():
        if wm_type == cfg_type and kind in _PRESET_PARAM_SPECS:
            return kind
    return ""


def _migrate_legacy_config(src):
    """v11 → v12：把「v11 信息栏参数」显式搬成画框水印参数。

    返回 (新的 src, note)：note 为 None = 这份配置跟 v11 无关（原样返回，一个键都不动）。

    搬法（目标白名单始终是 _PRESET_PARAM_SPECS["photo_frame"]）：
      · 同名键（params_* / date_* / photo_logo* / font_family）原样留下
      · 改名键走 _V11_EXIF_TO_FRAME：**白条（bar_*）→ 底边文字区**、
        **画框（border_*）→ 四周留白**，左段 → 主标题、中间段 → 品牌行；
        逐段对齐的 auto → center
      · v11 的「白条开关」被消费（它决定底边文字区高度与四行开关）：关掉 ⇒ 画框不留白、
        文字区 0、四行都不显示；开着 ⇒ 高度照搬。而四周留白只认 v11 的 border_show ——
        模板的 frame_show 默认是 true，所以 v11 没开画框时必须**显式写 False**，
        否则照片四周（含上方）会凭空长出一圈白边
      · 搬不动的（分隔线 / bar_layout / bar_mode / font_scale…）进 note["dropped"]，
        由调用方回显成 warnings —— **不许静默丢参数**
    纯函数：不碰文件系统、不改入参。
    """
    if not isinstance(src, dict) or not isinstance(src.get("params"), dict):
        return src, None
    params = src["params"]
    if not any(key in params for key in _V11_EXIF_MARKERS):
        return src, None                       # 没 v11 痕迹：不迁移

    frame_specs = _PRESET_PARAM_SPECS["photo_frame"]
    migrated, donors = {}, {}
    for v11_key, frame_key in _V11_EXIF_TO_FRAME.items():
        if v11_key not in params or frame_key in donors:
            continue                           # 缺键 / 目标键已被更靠前的键占用（先到先得）
        value = params[v11_key]
        if v11_key.endswith("_align"):
            value = _V11_ALIGN_VALUES.get(str(value).strip().lower(), value)
        migrated[frame_key] = value
        donors[frame_key] = v11_key
    for key in list(params):
        if key in frame_specs and key not in donors:
            migrated[key] = params[key]        # 同名键直接留下（含 params_*/date_*/photo_logo*）
            donors[key] = key
    if "frame_show" not in migrated:
        # v11 没开画框（border_show 缺席 / false）⇒ **显式**关掉四周留白：模板
        # photo_frame_brand 的 frame_show 默认 true，不写这个 False 就会冒出四周白边
        # （照片上方多一条 = 用户报的「白条跑到图片上部」）
        migrated["frame_show"] = False
    if params.get("bar_show") is False:
        # v11 那条白条关掉 ⇒ 它连同里面的文字都没画。忠实还原成「不留白框、底边文字区 0、
        # 四行都不显示」，而不是硬凑一个空白框出来；白条高度因此也不再搬（会出现在 dropped 里）
        migrated["frame_show"] = False
        migrated["frame_bottom_extra_ratio"] = 0.0
        for name in _FRAME_SEGMENTS:
            migrated[f"{name}_show"] = False
        donors.pop("frame_bottom_extra_ratio", None)

    handled = set(donors.values())
    if "bar_show" in params:
        handled.add("bar_show")                # 白条开关本身被消费，不算「搬不动」
    notes = []
    if "bar_mode" in params:
        # v11 的叠加/追加在画框水印里没有对应物：画框永远把文字区追加在照片下方，说清楚
        notes.append("v11 的版式（叠加在照片上 / 照片下方追加）在画框水印里统一成"
                     "「照片下方追加一块文字区」，原来的叠加位置不再沿用")
    note = {
        "from": "v11-exif",
        "mapped": len(handled),
        "dropped": sorted(str(k) for k in params if k not in handled),
        "preset_from": str(src.get("preset") or ""),
    }
    if notes:
        note["notes"] = notes
    out = dict(src)
    out["type"] = "frame"                      # v11 的信息栏参数在 v12 的家 = 画框水印
    out["preset"] = _V11_EXIF_TARGET_PRESET    # 对应模板：画框水印 · 拍立得
    out["params"] = migrated
    return out, note


def _normalize_config(raw):
    """任意输入 → 规范化配置，返回 (config, invalid_params, warnings)。

    **只有这一个合法化入口**（自动保存 / 另存为 / 切换 / 导入 / 导出都走它），
    所以「导出的文件」必然能被导入回来、结果还一样（幂等）。
    纯函数、不碰文件系统：同一个输入永远得到同一个输出，便于反复套用与测试。
    """
    invalid, warnings = [], []
    src = raw if isinstance(raw, dict) else {}
    if raw is not None and not isinstance(raw, dict):
        invalid.append("config")

    # v11 → v12 迁移要排在「未知键收集」之前：v11 的 28 个键里有一批在 v12 已删掉，
    # 不先搬家就会被当成非法键静默丢掉（详见 _migrate_legacy_config）
    src, migration = _migrate_legacy_config(src)

    cfg = _config_defaults()
    cfg.update({"version": _CONFIG_VERSION, "app": _CONFIG_APP})
    if migration:
        cfg["migrated_from_v11"] = migration
        note = f"已从 v11 信息栏配置迁移到画框水印：搬过来 {migration['mapped']} 项"
        if migration["dropped"]:
            note += f"，丢弃 {len(migration['dropped'])} 项（{'、'.join(migration['dropped'])}）"
        for extra in (migration.get("notes") or []):
            note += f"；{extra}"
        warnings.append("migrated_v11: " + note)
    elif isinstance(src.get("migrated_from_v11"), dict):
        # 已经迁过的配置（导出文件再次导入）原样留住这条说明，导出→导入才是幂等的
        prev = src["migrated_from_v11"]
        cfg["migrated_from_v11"] = {
            "from": str(prev.get("from") or "v11-exif")[:40],
            "mapped": int(_clamp(_safe_int(prev.get("mapped"), 0), 0, 9999)),
            "dropped": [str(x)[:60] for x in (prev.get("dropped") or []) if str(x)][:60],
            "preset_from": str(prev.get("preset_from") or "")[:60],
        }
        kept_notes = [str(x)[:200] for x in (prev.get("notes") or []) if str(x)][:8]
        if kept_notes:
            cfg["migrated_from_v11"]["notes"] = kept_notes

    # 认不出来的键（app/exported_at 之类的元信息不算）原样报出来，省得「导入没生效」靠猜
    unknown = sorted(str(k) for k in src
                     if str(k) not in _CONFIG_KNOWN_KEYS and str(k) not in _CONFIG_META_KEYS
                     and not str(k).startswith("_"))
    invalid.extend(unknown)

    wm_type = str(src.get("type") or "").strip().lower()
    if wm_type in _CONFIG_TYPES:
        cfg["type"] = wm_type
    elif wm_type:
        invalid.append("type")

    # 模板：只认当前库里真有的 id。模板被删/改名 ⇒ 清空 preset 但保留 params，
    # 并写一条警告 —— 绝不静默套成另一个模板（那会让版式莫名其妙变样）
    preset_id = str(src.get("preset") or "").strip()
    preset = _load_presets().get(preset_id) if preset_id else None
    if preset_id and preset is None:
        warnings.append(f"preset_missing: {preset_id}")
    elif preset is not None:
        cfg["preset"] = preset_id

    raw_params = src.get("params")
    if isinstance(raw_params, dict):
        kind = _config_params_kind(cfg["type"], preset)
        clean, dropped = _sanitize_preset_params(kind, raw_params)
        cfg["params"] = clean
        invalid.extend(f"params.{key}" for key in dropped)
    elif raw_params is not None:
        invalid.append("params")

    # 文字水印（老前端那套字段）
    text = src.get("text") if isinstance(src.get("text"), dict) else {}
    cfg["text"] = {
        "text": str(text.get("text") if text.get("text") is not None else "")[:200],
        "font_size": int(_clamp(_safe_int(text.get("font_size"), 36), 1, 1000)),
        "color": _normalize_color(text.get("color")) or "#ffffff",
        "bold": bool(text.get("bold")),
    }

    # 信息栏老字段（不带模板时那套：字号 0=自动 / 条色 / 字色 / 条透明度）
    exif = src.get("exif") if isinstance(src.get("exif"), dict) else {}
    cfg["exif"] = {
        "font_size": int(_clamp(_safe_int(exif.get("font_size"), 0), 0, 1000)),
        "bar_color": _normalize_color(exif.get("bar_color")) or "#ffffff",
        "text_color": _normalize_color(exif.get("text_color")) or "#000000",
        "bar_opacity": int(_clamp(_safe_int(exif.get("bar_opacity"), 240), 0, 255)),
    }

    # 文字水印的通用几何量（位置/旋转/透明度）
    legacy = src.get("legacy") if isinstance(src.get("legacy"), dict) else {}
    cfg["legacy"] = {
        "position_x": round(_clamp(_safe_float(legacy.get("position_x"), 0.5), 0.0, 1.0), 5),
        "position_y": round(_clamp(_safe_float(legacy.get("position_y"), 0.5), 0.0, 1.0), 5),
        "rotation": round(_clamp(_safe_float(legacy.get("rotation"), 0.0), -180.0, 180.0), 2),
        "opacity": int(_clamp(_safe_int(legacy.get("opacity"), 128), 0, 255)),
    }

    # 导出选项（只影响下载，不影响预览）
    export = src.get("export") if isinstance(src.get("export"), dict) else {}
    fmt = str(export.get("format") or "").strip().lower()
    if fmt and fmt not in _EXPORT_FORMATS:
        invalid.append("export.format")
    cfg["export"] = {
        "format": fmt if fmt in _EXPORT_FORMATS else "jpeg",
        "quality": int(_clamp(_safe_int(export.get("quality"), _EXPORT_DEFAULT_QUALITY), 1, 100)),
    }

    # 图片水印：只存「引用 id + 几何量」（图片文件在 uploads/watermark_<id>.png）。
    # 这里**故意不查文件是否存在**（保持纯函数、图片也可能后补），前端套用时按
    # 现有的水印列表过滤 —— 配置里保留完整摆放信息，重新导入同一份 cfg 不会丢位置。
    items = []
    image = src.get("image") if isinstance(src.get("image"), dict) else {}
    raw_items = image.get("items")
    if isinstance(raw_items, list):
        for idx, item in enumerate(raw_items[:_CONFIG_MAX_ITEMS]):
            if not isinstance(item, dict):
                invalid.append(f"image.items[{idx}]")
                continue
            wm_id = _sanitize_image_ref(item.get("id"))
            if not wm_id:
                invalid.append(f"image.items[{idx}].id")
                continue
            items.append({
                "id": wm_id,
                "x": round(_clamp(_safe_float(item.get("x"), 0.5), 0.0, 1.0), 5),
                "y": round(_clamp(_safe_float(item.get("y"), 0.5), 0.0, 1.0), 5),
                "scale": round(_clamp(_safe_float(item.get("scale"), 0.3), 0.01, 1.0), 5),
                "rotation": round(_clamp(_safe_float(item.get("rotation"), 0.0), -180.0, 180.0), 2),
                "opacity": int(_clamp(_safe_int(item.get("opacity"), 255), 0, 255)),
            })
        if len(raw_items) > _CONFIG_MAX_ITEMS:
            warnings.append(f"image_items_truncated: {len(raw_items)}")
    elif raw_items is not None:
        invalid.append("image.items")
    cfg["image"] = {"items": items}

    return cfg, invalid, warnings


def _config_to_wm_payload(cfg):
    """配置 → 水印 payload；**必须与前端 `getWmParams()` 逐字段一致**。

    批量生成（scripts/batch_core.py）没有浏览器，得在 Python 侧把配置翻成同一份请求体，
    否则「单张下载」与「批量下载」会跑出两种结果（验证脚本 [16] 拿逐字节一致当锚点）。
    对照 index.html 的 getWmParams()：
      - type/opacity/position_x/position_y/rotation 恒定发
      - 有模板 → 发 preset + 模板全部参数（工作副本 = 配置里的 params）
      - 文字水印 → 空文字兜底成「水印」
      - 信息栏 → **只有没模板时**才发历史字段（font_size/bar_color/text_color/bar_opacity），
        有模板时发这些会盖掉同名模板数值
      - 图片水印 → scale 兜底 + items 列表（x/y 是水印中心点归一化坐标）
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    legacy = cfg.get("legacy") if isinstance(cfg.get("legacy"), dict) else {}
    wm_type = str(cfg.get("type") or "").strip().lower()
    if wm_type not in _CONFIG_TYPES:
        wm_type = "text"
    preset_id = str(cfg.get("preset") or "").strip()

    payload = {
        "type": wm_type,
        "opacity": int(_clamp(_safe_int(legacy.get("opacity"), 128), 0, 255)),
        "position_x": round(_clamp(_safe_float(legacy.get("position_x"), 0.5), 0.0, 1.0), 5),
        "position_y": round(_clamp(_safe_float(legacy.get("position_y"), 0.5), 0.0, 1.0), 5),
        "rotation": round(_clamp(_safe_float(legacy.get("rotation"), 0.0), -180.0, 180.0), 2),
    }
    if preset_id:
        payload["preset"] = preset_id
        params = cfg.get("params")
        if isinstance(params, dict):
            payload.update(params)              # 模板参数（已过白名单）

    if wm_type == "text":
        text = cfg.get("text") if isinstance(cfg.get("text"), dict) else {}
        payload["text"] = str(text.get("text") or "").strip() or "水印"
        payload["font_size"] = int(_clamp(_safe_int(text.get("font_size"), 36), 1, 1000))
        payload["color"] = _normalize_color(text.get("color")) or "#ffffff"
        payload["bold"] = bool(text.get("bold"))
    elif wm_type == "exif":
        if not preset_id:                       # 与前端一致：有模板就不发这些历史字段
            exif = cfg.get("exif") if isinstance(cfg.get("exif"), dict) else {}
            payload["font_size"] = int(_clamp(_safe_int(exif.get("font_size"), 0), 0, 1000))
            payload["bar_color"] = _normalize_color(exif.get("bar_color")) or "#ffffff"
            payload["text_color"] = _normalize_color(exif.get("text_color")) or "#000000"
            payload["bar_opacity"] = int(_clamp(_safe_int(exif.get("bar_opacity"), 240), 0, 255))
    elif wm_type == "image":
        payload["scale"] = 0.3
        image = cfg.get("image") if isinstance(cfg.get("image"), dict) else {}
        payload["items"] = [dict(item) for item in (image.get("items") or [])
                            if isinstance(item, dict)]
    return payload


def _config_path(slot_id=None):
    """配置路径：不给 id = current.json；给了 id 严格过滤（防路径穿越）。"""
    if slot_id is None:
        return os.path.join(CONFIG_DIR, _CONFIG_CURRENT_NAME)
    safe = str(slot_id or "").strip()
    if not _WATERMARK_ID_RE.match(safe):   # id 形状与贴图/字体同款
        return None
    if safe in _CONFIG_RESERVED_IDS:       # current/index 是固定文件，不能当快照用
        return None
    return os.path.join(CONFIG_DIR, f"{safe}.json")


def _list_config_slot_ids():
    try:
        names = os.listdir(CONFIG_DIR)
    except OSError:
        return []
    found = (_CONFIG_FILE_RE.match(n) for n in names)
    return sorted(m.group(1) for m in found if m and m.group(1) not in _CONFIG_RESERVED_IDS)


def _config_index_path():
    return os.path.join(CONFIG_DIR, _CONFIG_INDEX_NAME)


def _load_config_index():
    """快照清单 {id: {name, saved_at}}；缺失/损坏一律降级为空表（再用 id 当名字）。"""
    try:
        with open(_config_index_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): v for k, v in data.items() if isinstance(v, dict)}


def _save_config_index(index):
    """原子写清单（临时文件 + os.replace），避免半个 JSON 让下次读崩。"""
    path = _config_index_path()
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(index, f, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(tmp, path)
        return True
    except Exception:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        return False


def _stamp_text(stamp):
    """时间戳（epoch 秒）→ 人话时间；空/非法值返回空串。"""
    value = _safe_float(stamp, None)
    if not value:
        return ""
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(value))
    except (OSError, ValueError, OverflowError):
        return ""


def _read_config_file(path):
    """读配置文件 → (原始 dict 或 None, 错误信息)。坏 JSON 只报错、绝不抛异常。

    用 utf-8-sig 读：Windows 记事本另存过的 JSON 带 BOM，json.load 会直接报错。
    """
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            raw = json.load(f)
    except Exception:
        return None, "config_unreadable"
    if not isinstance(raw, dict):
        return None, "config_unreadable"
    return raw, ""


def _write_config_file(path, config):
    """原子写配置（临时文件 + os.replace）；失败返回 False。"""
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(tmp, path)
        return True
    except Exception:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        return False


def _config_mtime(path):
    try:
        return float(os.stat(path).st_mtime)
    except OSError:
        return 0.0


def _config_slot_items():
    """命名快照列表（新的排前面）：下拉里显示「名字 + 类型/模板 + 时间」。"""
    index = _load_config_index()
    presets = _load_presets()
    items = []
    for slot_id in _list_config_slot_ids():
        path = _config_path(slot_id)
        if not path:
            continue
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        meta = index.get(slot_id) or {}
        raw, err = _read_config_file(path)
        wm_type, preset_id = "", ""
        if isinstance(raw, dict):
            # 迁移后的类型/模板才是这份配置在 v12 里的真身（列表别显示成 exif）
            raw, _ = _migrate_legacy_config(raw)
            wm_type = str(raw.get("type") or "").strip().lower()
            preset_id = str(raw.get("preset") or "").strip()
        stamp = _safe_float(meta.get("saved_at"), None) or _config_mtime(path)
        items.append({
            "id": slot_id,
            "name": str(meta.get("name") or slot_id)[:_CONFIG_MAX_NAME],
            "saved_at": stamp,
            "saved_at_text": _stamp_text(stamp),
            "size": size,
            "type": wm_type if wm_type in _CONFIG_TYPES else "",
            "preset": preset_id,
            "preset_name": str((presets.get(preset_id) or {}).get("name") or "")[:60],
            "broken": bool(err),
        })
    items.sort(key=lambda it: (-(it["saved_at"] or 0.0), it["id"]))
    return items


def _clear_watermark_cache():
    _watermark_cache["items"].clear()
    _watermark_cache["order"].clear()


def _load_cached_rgba(path):
    """按 mtime+size 缓存解码结果的水印图读取（同一张图只解码一次）。"""
    if not path or not os.path.exists(path):
        return None
    st = os.stat(path)
    key = f"{path}:{st.st_mtime_ns}:{st.st_size}"
    cached = _watermark_cache["items"].get(key)
    if cached is not None:
        return cached
    try:
        with Image.open(path) as raw:
            raw.load()
            img = raw.convert("RGBA")
    except Exception:
        return None
    cache = _watermark_cache
    cache["items"][key] = img
    cache["order"].append(key)
    while len(cache["order"]) > _WATERMARK_CACHE_LIMIT:
        cache["items"].pop(cache["order"].pop(0), None)
    return img


def _load_watermark_image(wm_id):
    return _load_cached_rgba(_watermark_path(wm_id))


def _load_legacy_watermark_image():
    return _load_cached_rgba(os.path.join(UPLOAD_DIR, _LEGACY_WM_NAME))


def _watermark_layer_preview(img):
    """给前端 DOM 代理层用的水印图（≤1000px，保留透明通道，减小响应体）。"""
    longest = max(img.width, img.height)
    layer = img
    if longest > _WATERMARK_LAYER_MAX_DIM:
        ratio = _WATERMARK_LAYER_MAX_DIM / longest
        layer = img.resize(
            (max(1, int(img.width * ratio)), max(1, int(img.height * ratio))),
            Image.LANCZOS,
            reducing_gap=2.0,
        )
    buf = io.BytesIO()
    layer.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def _load_bar_font(size, bold, text, family="", report=None):
    """信息栏字体：指定了字体就用它，否则纯 ASCII 用西文字体（版式才与参照图一致），含中文才退到中文字体。

    与 load_watermark_font 的区别：后者把中文字体排在前面（宁可字形宽一点也不要有字画不出来），
    粗体 ASCII 会拿到 msyhbd；信息栏是排版敏感的场景，先按语言选族、再按字重选体，最后逐字探测兜底。

    family 是字体 id（系统字体或自己上传的）；report 是可选 dict，回填「实际用到的字体 / 为什么
    回落到自动挑体」，供 applied.bar 回显 —— 排查「选了字体但看着没变」时先看它。
    """
    text = str(text or "")
    chosen = _resolve_font_choice(family) if family else None
    if chosen is not None:
        # 有粗体面就优先取粗体面；没有就用常规面 + 描边模拟（见 _fit_text_layer）
        path = chosen["bold_path"] if (bold and chosen["bold_path"]) else chosen["path"]
        try:
            font = ImageFont.truetype(path, size)
        except Exception:
            font = None
        if font is not None and _font_covers_text(font, text):
            if report is not None:
                report.update({"id": chosen["id"], "label": chosen["label"],
                               "face": os.path.basename(path), "source": chosen["source"],
                               "emulated_bold": bool(bold and not chosen["bold_path"])})
            return font
        # 读不动 / 缺字（例如拿纯西文字体排中文）→ 回落自动挑体，并把原因带回前端
        if report is not None:
            report.update({"requested": str(family), "label": chosen["label"],
                           "fallback": "font_missing_glyph"})
    elif family and report is not None:
        report.update({"requested": str(family), "fallback": "font_unknown"})

    latin = list(_LATIN_BOLD_FONT_CANDIDATES if bold else _LATIN_FONT_CANDIDATES)
    cjk = list(_CJK_BOLD_FONT_CANDIDATES if bold else _CJK_FONT_CANDIDATES)
    fallback, fallback_path = None, ""
    for path in (latin + cjk if text.isascii() else cjk + latin):
        try:
            font = ImageFont.truetype(path, size)
        except Exception:
            continue
        if fallback is None:
            fallback, fallback_path = font, path
        if _font_covers_text(font, text):
            fallback, fallback_path = font, path
            break
    if report is not None:
        report.setdefault("id", "")
        report.setdefault("label", _FONT_AUTO_LABEL)
        report["face"] = os.path.basename(fallback_path) if fallback_path else ""
    return fallback or load_watermark_font(size, bold=bold, text=text)


def _render_text_layer(text, font, color, tracking_px=0, stroke=0):
    """把一段文本渲染成「紧贴墨迹」的 RGBA 图层（图层尺寸 = 墨迹尺寸）。

    - Pillow 没有字距参数，tracking_px > 0 时逐字符绘制并累加字距（相机信息栏的品牌常带宽字距）
    - stroke > 0 时用描边模拟加粗（选中的字体没有粗体面时用，保证「加粗」按得动）
    - 按 alpha bbox 裁切 ⇒ 之后能用墨迹边缘精确对齐，不受字体 ascent/leading 影响
    """
    text = str(text or "")
    if not text.strip():
        return None
    stroke = max(0, int(stroke))
    ascent, descent = font.getmetrics()
    width = int(sum(font.getlength(ch) for ch in text)
                + max(0, int(tracking_px)) * (len(text) - 1)) + 4 + stroke * 2
    layer = Image.new("RGBA", (max(1, width), max(1, ascent + descent + 4 + stroke * 2)),
                      (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    x = 2.0 + stroke
    for ch in text:
        draw.text((x, 2 + stroke), ch, font=font, fill=color,
                  stroke_width=stroke, stroke_fill=color if stroke else None)
        x += font.getlength(ch) + tracking_px
    box = layer.split()[3].getbbox()
    return layer.crop(box) if box else None


def _fit_text_layer(text, bold, color, target_ink, tracking_ratio=0.0, target_w=0,
                    family="", report=None):
    """解出「墨迹高度 = target_ink」的字号，返回 (图层, 字号)。

    先按比例解一次、再按实测复算：字体升部/斜杠/降部的差异都吃进这一步，
    所以模板里写的是「实测墨迹比例」，换字体也落在同一视觉大小上（参照图就是这么量出来的）。

    target_w > 0 时再把图层横向缩放到该墨迹宽度：系统西文字体（Arial）比参照图字体略宽，
    只对字号会让右侧文字行右边缘溢出十几个像素；宽度匹配后「左边缘 + 右边缘」都能对上。

    family 指定字体（空=自动挑体）；该字体没有粗体面时用描边模拟加粗 ——
    墨迹高度仍由实测复算，所以「加粗 / 不加粗」都落在同一墨迹尺寸上，版式不跑偏。
    """
    probe = 100   # 先用 100px 试排，测出墨迹高度再按比例解字号

    def build(size):
        font = _load_bar_font(size, bold, text, family, report)
        emulated = bool(bold and report is not None and report.get("emulated_bold"))
        return _render_text_layer(text, font, color, size * tracking_ratio,
                                  max(1, int(round(size * 0.03))) if emulated else 0)

    layer = build(probe)
    if layer is None:
        return None, 0
    size = max(1, int(round(probe * target_ink / max(1, layer.height))))
    for _ in range(2):
        layer = build(size)
        if layer is None:
            return None, size
        if abs(layer.height - target_ink) <= 1:
            break
        size = max(1, int(round(size * target_ink / max(1, layer.height))))
    target_w = int(round(target_w or 0))
    # 只在 ±25% 内做横向微调：差得太多说明「内容与模板不匹配」（比如型号名很长），
    # 硬压会把字压瘪，此时保持自然宽度更合理
    if target_w > 0 and layer.width and 0 < abs(layer.width - target_w) <= target_w * 0.25:
        layer = layer.resize((max(1, target_w), layer.height), Image.LANCZOS)
    return layer, size


def _draw_exif_bar_legacy(img, overlay, wm_data, info, scaled_font_size):
    """历史信息栏排版（老前端不带任何模板参数时走这里），逻辑与抽出前完全一致：

    叠加在照片底部、品牌居中、参数/日期右对齐，字号 = 画布宽 × 0.024。
    """
    left, center, params, date_str = _wm_exif_text(wm_data)

    bar_color = wm_data.get("bar_color", "#ffffff")
    text_color = wm_data.get("text_color", "#000000")
    bar_opacity = _clamp(_safe_int(wm_data.get("bar_opacity", 220), 220), 0, 255)
    explicit_size = _safe_int(wm_data.get("font_size", 0), 0)
    if explicit_size > 0:
        font_size = scaled_font_size(explicit_size)
    else:
        font_size = max(14, int(img.width * 0.024))

    br, bg, bb = parse_color(bar_color)
    tr, tg, tb = parse_color(text_color, (0, 0, 0))

    bar_height = max(40, int(img.height * 0.09))
    margin_x = max(8, int(img.width * 0.03))
    bar_y = img.height - bar_height

    bar = Image.new("RGBA", (img.width, bar_height), (br, bg, bb, bar_opacity))
    _paste_layer(overlay, bar, 0, bar_y)

    draw = ImageDraw.Draw(overlay)
    font = load_cjk_font(font_size)
    small = load_cjk_font(max(11, int(font_size * 0.78)))
    gap = max(2, int(font_size * 0.16))

    def text_metrics(s, f):
        b = draw.textbbox((0, 0), s, font=f)
        return b[2] - b[0], b[3] - b[1]

    def draw_text(s, f, align):
        w, h = text_metrics(s, f)
        y = bar_y + (bar_height - h) // 2
        if align == "left":
            x = margin_x
        elif align == "center":
            x = img.width // 2 - w // 2
        else:
            x = img.width - margin_x - w
        draw.text((x, y), s, font=f, fill=(tr, tg, tb, 255))

    if left:
        draw_text(left, font, "left")
    if center:
        draw_text(center, font, "center")

    if params and date_str:
        pw, ph = text_metrics(params, font)
        dw, dh = text_metrics(date_str, small)
        total_h = ph + dh + gap
        y0 = bar_y + (bar_height - total_h) // 2
        draw.text((img.width - margin_x - pw, y0), params, font=font, fill=(tr, tg, tb, 255))
        draw.text((img.width - margin_x - dw, y0 + ph + gap), date_str, font=small, fill=(tr, tg, tb, 255))
    elif params:
        draw_text(params, font, "right")
    elif date_str:
        draw_text(date_str, small, "right")

    if info is not None:
        info["bar"] = {
            "left": bool(left),
            "center": bool(center),
            "params": bool(params),
            "date": bool(date_str),
            "font_size": font_size,
        }


# ---------------------------------------------------------------------------
# 品牌贴图：用户上传的图当成「墨迹图层」，与文字图层共用同一套版式口径
# （先裁到 alpha bbox 再按 *_cap_ratio 缩放 ⇒ 换贴图与换字号落在同一视觉尺寸上）
# ---------------------------------------------------------------------------
def _trim_alpha_layer(img):
    """裁到有墨迹的区域（alpha bbox）——与文字图层的墨迹口径一致。"""
    if img is None:
        return None
    if img.mode != "RGBA":
        img = img.convert("RGBA")
    box = img.split()[3].getbbox()
    if not box:
        return None      # 全透明贴图：等于没画
    return img.crop(box)


def _has_alpha_edge(img):
    """贴图是否真的有透明区域：全不透明 ⇒ 单色化会变成一整块实心色块，需要先警告。"""
    if img is None or img.mode != "RGBA":
        return False
    lo, _hi = img.split()[3].getextrema()
    return lo < 255


def _fit_image_layer(layer, target_ink, target_w=0):
    """把（已裁过透明边的）贴图缩放到「墨迹高度 = target_ink」：等比，不做非等比拉伸。

    target_ink 来自模板的 *_cap_ratio（实测墨迹 ÷ 画布宽），所以贴图与文字落在同一
    视觉尺寸上；target_w > 0 时在 ±25% 内横向微调（与文字同规则：差太多说明内容与
    模板不匹配，此时保持自然比例，不把图压瘪）。
    """
    if layer is None or layer.width <= 0 or layer.height <= 0:
        return None
    target_ink = max(1, int(round(target_ink)))
    width = max(1, int(round(layer.width * target_ink / layer.height)))
    out = layer.resize((width, target_ink), Image.LANCZOS)
    target_w = int(round(target_w or 0))
    if target_w > 0 and 0 < abs(out.width - target_w) <= target_w * 0.25:
        out = out.resize((max(1, target_w), out.height), Image.LANCZOS)
    return out


def _brand_layer(name, params, scale, W):
    """该段的品牌贴图图层；返回 (图层, 回显信息, 告警列表)；没选贴图时返回 (None, None, [])。

    贴图是用户自己的图，所以这里要把「读不到 / 全透明 / 没有透明通道 / 分辨率偏低」
    讲清楚：能画就画并回显尺寸，画不了就返回 None 让调用方回落文字。
    """
    brand_id = str(params.get(f"{name}_image") or "").strip()
    if not brand_id:
        return None, None, []
    img = _load_brand_image(brand_id)
    if img is None:
        return None, None, [f"贴图「{brand_id}」不存在或无法读取，该段已回落为文字"]

    warnings = []
    mono = bool(params.get(f"{name}_image_mono"))
    has_alpha = _has_alpha_edge(img)
    src = _trim_alpha_layer(img)
    if src is None:
        return None, None, [f"贴图「{brand_id}」内容全透明，该段已回落为文字"]
    # 单色化的颜色：优先本槽位的专属色（{槽}_image_color），**留空 = 跟随该段文字颜色**
    # —— 与照片 logo 的 photo_logo_color 同一条口径，只是默认「跟随」而不是固定黑
    mono_color = str(params.get(f"{name}_image_color") or "").strip() or params[f"{name}_color"]
    if mono and has_alpha:
        # 用贴图自己的 alpha 当蒙版刷成单色：彩色/黑白 logo 都能统一成栏里的文字色
        solid = Image.new("RGBA", img.size, parse_color(mono_color, (0, 0, 0)) + (255,))
        solid.putalpha(img.split()[3])
        src = _trim_alpha_layer(solid) or src
    elif mono:
        warnings.append("贴图没有透明通道，单色化已跳过（上传透明底 PNG 才能单色化）")

    layer = _fit_image_layer(src, max(1, params[f"{name}_cap_ratio"] * scale * W),
                             max(0.0, params.get(f"{name}_width_ratio", 0.0)) * W)
    if layer is None:
        return None, None, warnings + [f"贴图「{brand_id}」为空图层，该段已回落为文字"]
    upscaled = layer.width > src.width or layer.height > src.height
    if upscaled:
        warnings.append("贴图分辨率偏低已被放大，建议换更大的原图")

    opacity = int(params.get(f"{name}_image_opacity", 255) or 0)
    if opacity < 255:
        layer = apply_opacity(layer, opacity)

    meta = {
        "id": brand_id,
        "src": [src.width, src.height],
        "out": [layer.width, layer.height],
        "mono": bool(mono and has_alpha),
        "mono_color": _normalize_color(mono_color) or "#000000",
        "mono_follow": not str(params.get(f"{name}_image_color") or "").strip(),
        "opacity": opacity,
        "upscaled": upscaled,
    }
    return layer, meta, warnings


def _photo_logo_layer(params, W, warnings):
    """照片上的 logo 图层：宽度 = photo_logo_scale × 照片宽（等比，不压瘪）。

    返回 (图层, 回显信息)；没选贴图返回 (None, None)；读不到 / 全透明 / 分辨率偏低
    都写进 warnings —— 与白条贴图同一条规矩：绝不静默。
    """
    brand_id = str(params.get("photo_logo") or "").strip()
    if not brand_id:
        return None, None
    src_img = _load_brand_image(brand_id)
    if src_img is None:
        warnings.append(f"照片 logo「{brand_id}」不存在或无法读取，已跳过")
        return None, None
    src = _trim_alpha_layer(src_img)
    if src is None:
        warnings.append(f"照片 logo「{brand_id}」内容全透明，已跳过")
        return None, None

    has_alpha = _has_alpha_edge(src_img)
    mono = bool(params.get("photo_logo_mono"))
    if mono and has_alpha:
        solid = Image.new("RGBA", src_img.size, parse_color(params["photo_logo_color"], (0, 0, 0)) + (255,))
        solid.putalpha(src_img.split()[3])
        src = _trim_alpha_layer(solid) or src
    elif mono:
        warnings.append("照片 logo 没有透明通道，单色化已跳过（上传透明底 PNG 才能单色化）")

    target_w = max(1, int(round(W * params["photo_logo_scale"])))
    target_h = max(1, int(round(src.height * target_w / src.width)))
    layer = src.resize((target_w, target_h), Image.LANCZOS)
    upscaled = target_w > src.width or target_h > src.height
    if upscaled:
        warnings.append("照片 logo 分辨率偏低已被放大，建议换更大的原图")

    opacity = int(params.get("photo_logo_opacity", 255) or 0)
    if opacity < 255:
        layer = apply_opacity(layer, opacity)
    rotation = _safe_float(params.get("photo_logo_rotation", 0), 0.0)
    if rotation:
        # 与图片水印同一口径：Pillow 是逆时针，前端代理层用 -rotation 对齐
        layer = layer.rotate(rotation, expand=True, resample=Image.BICUBIC)

    meta = {
        "id": brand_id,
        "src": [src.width, src.height],
        "out": [layer.width, layer.height],
        "mono": bool(mono and has_alpha),
        "opacity": opacity,
        "rotation": rotation,
        "upscaled": upscaled,
    }
    return layer, meta


def _paste_clipped(dst, layer, x, y, box):
    """把图层贴到 dst，但**裁剪到 box 内**（照片盒）然后 alpha_composite。

    照片 logo 的位置是「照片盒」坐标：裁一刀就不会糊到白条/画框上（预览所见=下载所得）；
    走 _paste_layer 而不是 paste，避免半透明边缘发黑。
    """
    bx, by, bw, bh = box
    x0, y0 = max(int(x), bx), max(int(y), by)
    x1, y1 = min(int(x) + layer.width, bx + bw), min(int(y) + layer.height, by + bh)
    if x1 <= x0 or y1 <= y0:
        return False
    piece = layer.crop((x0 - int(x), y0 - int(y), x1 - int(x), y1 - int(y)))
    _paste_layer(dst, piece, x0, y0)
    return True


# 回显/告警里用的段名（信息栏四段）
_BAR_SEGMENT_LABELS = {"left": "左段", "center": "中间段", "params": "参数行", "date": "日期行"}


def _draw_exif_bar_engine(img, wm_data, info=None):
    """数据驱动的信息栏排版（参数见 _PRESET_PARAM_SPECS["exif_bar"]）。

    版式固定为左右分栏：左=型号、中=品牌、右侧上=参数下=日期，中缝一条分隔线（参照图版式）；
    append 模式在照片下方追加白条（照片像素一个不动），overlay 模式叠在照片上。
    所有长度都是「照片宽度的比例」，文字按墨迹 bbox 对齐，因此预览与全尺寸导出必然等比一致。
    """
    W, H = img.size
    params = _exif_bar_params(wm_data)
    scale = params["font_scale"]

    left_exif, center_exif, params_exif, date_exif = _wm_exif_text(wm_data)
    # left_hide_make：贴图已经代表品牌时，左段「厂商 + 型号」里的厂商名要去掉
    # （只动 EXIF 兜底出来的文字；用户手填的 left_text 不碰）
    if params["left_hide_make"] and not params["left_text"] and center_exif \
            and left_exif.startswith(center_exif):
        left_exif = left_exif[len(center_exif):].strip() or left_exif
    texts = {
        "left": str(params["left_text"] or left_exif).strip(),
        "center": str(params["center_text"] or center_exif).strip(),
        "params": str(params["params_text"] or params_exif).strip(),
        "date": str(params["date_text"] or date_exif).strip(),
    }
    bar_mode = params["bar_mode"]
    bar_h = max(1, int(round(W * params["bar_height_ratio"])))
    margin = max(0, int(round(W * params["margin_ratio"])))
    divider_x = int(round(W * params["divider_center_ratio"]))
    gap = max(0, int(round(W * params["divider_gap_ratio"])))
    line_gap = max(0, int(round(W * params["line_gap_ratio"])))

    # 各段图层：选了品牌贴图就用贴图（模板说了用图就不再画文字），否则走文字排版。
    # 先算图层还有个好处：下面的守卫判的是「到底有没有东西可画」，
    # 于是「没有 EXIF、只贴了一个 logo」的图照样能出栏，不会凭空少一条白带。
    segments, warnings, image_fallback = {}, [], []
    for name in _BAR_SEGMENTS:
        logo, meta, warn = _brand_layer(name, params, scale, W)
        warnings.extend(warn)
        if logo is not None:
            segments[name] = {"kind": "image", "layer": logo, "font_size": 0, "meta": meta}
            continue
        if params.get(f"{name}_image"):
            image_fallback.append(name)     # 选了贴图但没画成 ⇒ 已回落文字，回显出来
        if not texts[name]:
            continue
        color = parse_color(params[f"{name}_color"], (0, 0, 0))
        # 字体：该段自己的设置优先，其次「信息栏字体」，都空 = 自动挑体
        family = params.get(f"{name}_font") or params.get("font_family") or ""
        report = {}
        layer, size = _fit_text_layer(
            texts[name], params[f"{name}_bold"], color + (255,),
            max(1, params[f"{name}_cap_ratio"] * scale * W), params[f"{name}_tracking"],
            max(0.0, params.get(f"{name}_width_ratio", 0.0)) * W,
            family=family, report=report)
        if layer is not None:
            if report.get("fallback"):
                # 选了字体但用不上（id 不在库里 / 缺字）：已回落自动挑体，回显原因而不是静默
                if report["fallback"] == "font_unknown":
                    note = f"字体「{report.get('requested') or family}」读不到（不在字体库里），已回落自动字体"
                else:
                    note = f"字体「{report.get('label') or family}」缺少这段文字需要的字形，已回落自动字体"
                warnings.append(f"{_BAR_SEGMENT_LABELS.get(name, name)}：{note}")
            segments[name] = {"kind": "text", "layer": layer, "font_size": size,
                              "meta": None, "font": report}

    if bar_mode == "append" and not segments:
        # 没有任何内容可画：append 不凭空多一条空带（回显原因而不是静默）
        if info is not None:
            info["bar"] = {"mode": bar_mode, "skipped": "EXIF 中没有可用的相机信息",
                           "preset": _preset_id(wm_data)}
        return img

    inner_h = H + bar_h if bar_mode == "append" else H
    inner = Image.new("RGBA", (W, inner_h), (0, 0, 0, 0))
    inner.paste(img, (0, 0))
    overlay = Image.new("RGBA", inner.size, (0, 0, 0, 0))
    # append = 照片下方追加白条（画布加高）；overlay = 白条叠在照片**下沿**、画布尺寸不变
    # （与老前端那条历史排版 _draw_exif_bar_legacy 的 bar_y = H - bar_height 一致）。
    # 别把 overlay 写成 0：那会变成「白条盖在照片顶部」，观感与两条历史通道都不符。
    bar_y = (inner_h - bar_h) if bar_mode == "append" else (H - bar_h)

    br, bg, bb = parse_color(params["bar_color"])
    _paste_layer(overlay, Image.new("RGBA", (W, bar_h), (br, bg, bb, params["bar_opacity"])), 0, bar_y)

    divider_w = max(1, int(round(W * params["divider_width_ratio"])))
    divider_h = max(1, int(round(bar_h * params["divider_height_ratio"])))
    divider_y = int(round(bar_y + (bar_h - divider_h) / 2))
    divider_x0 = int(round(divider_x - divider_w / 2))
    dr, dg, db = parse_color(params["divider_color"])
    _paste_layer(overlay, Image.new("RGBA", (divider_w, divider_h), (dr, dg, db, 255)), divider_x0, divider_y)

    center_y = bar_y + bar_h / 2

    def place(name, x, y):
        _paste_layer(overlay, segments[name]["layer"], int(round(x)), int(round(y)))

    # ---- 排版：左 = 型号 / 中 = 品牌 / 右侧上下两行 ----
    if "left" in segments:
        layer = segments["left"]["layer"]
        place("left", margin, center_y - layer.height / 2)
    if "center" in segments:
        layer = segments["center"]["layer"]
        place("center", divider_x - gap - layer.width, center_y - layer.height / 2)
    right = [name for name in ("params", "date") if name in segments]
    if right:
        group_h = sum(segments[name]["layer"].height for name in right) + line_gap * (len(right) - 1)
        y = center_y - group_h / 2
        for name in right:
            layer = segments[name]["layer"]
            place(name, divider_x + gap, y)
            y += layer.height + line_gap

    # ---- 合成：照片 + 白条 + 分隔线 + 各段 ----
    def segment_payload(name):
        """回显该段真正生效的形态：贴图（缩放前后尺寸）还是文字（实测墨迹尺寸）。"""
        entry = segments[name]
        if entry["kind"] == "image":
            return {"kind": "image", "image": entry["meta"]}
        out = {"kind": "text", "text": texts[name], "size": entry["font_size"],
               "w": entry["layer"].width, "h": entry["layer"].height}
        if entry.get("font"):
            # 回显这一段真正用的字体（含「为什么回落到自动」），排查「选了字体没变」看这里
            out["font"] = dict(entry["font"])
        if name in image_fallback:
            out["fallback"] = "image_missing"   # 选了贴图但没有可用文件
        return out

    if info is not None:
        info["bar"] = {
            "mode": bar_mode,
            "preset": _preset_id(wm_data),
            "font_scale": scale,
            "bar_height": bar_h,
            "margin": margin,
            "divider_x": divider_x,
            "divider": [divider_x0, divider_y, divider_w, divider_h],
            "segments": {name: segment_payload(name) for name in segments},
        }
        if image_fallback:
            info["bar"]["image_fallback"] = sorted(image_fallback)
        if warnings:
            info["bar"]["warnings"] = warnings
        if wm_data.get("_preset_error"):
            info["bar"]["preset_error"] = wm_data["_preset_error"]
    return Image.alpha_composite(inner, overlay)


def _preset_id(wm_data):
    """当前 payload 生效的模板 id（无模板返回 None），只用于 info 回显。"""
    preset = wm_data.get("_preset")
    return preset.get("id") if isinstance(preset, dict) else None


def _frame_pads(params, W):
    """画框四边留白（像素）：frame_show 关掉 = 四周不留白；单边开关各自生效。

    gap 是「占照片宽度的比例」⇒ 与其它几何量同口径，预览与导出天然等比。
    底边文字区（frame_bottom_extra_ratio）不算在留白里，由调用方另加。
    """
    if not params.get("frame_show"):
        return {"left": 0, "right": 0, "top": 0, "bottom": 0}
    gap = max(0, int(round(W * params["frame_gap_ratio"])))
    return {side: (gap if params.get(f"frame_{side}") else 0)
            for side in ("left", "right", "top", "bottom")}


# ---------------------------------------------------------------------------
# 背景模糊（blur_backdrop）：外围底 = 照片自己的模糊画像 + 暗角 + 中心照片投影
#
# 骨架与画框水印完全共用（_draw_framed_photo：画布 = 照片 + 四边留白 + 底边文字区），
# 区别只在「外面那圈怎么画」：这里贴一张模糊底图、给照片垫一层投影。
#
# 性能：模糊/暗角/投影都是低频运算，一律在 1/_BACKDROP_WORK_SCALE 分辨率上算完再拉回
# （半径同比例缩小）⇒ 视觉几乎无差、耗时低一个数量级；底图再按「源图指纹 + 画布 + 底图
# 参数」做 LRU 缓存 ⇒ 调文字类参数（字号/颜色/行距/贴图）时完全不重算模糊。
# ---------------------------------------------------------------------------
_BACKDROP_WORK_SCALE = 4                     # 模糊/投影的运算降采样倍数
_BACKDROP_CACHE_MAX = 4                      # 底图缓存上限（张）
_BACKDROP_CACHE_PIXELS = 8 * 1000 * 1000     # 超过这个像素的底图不进缓存（防内存爆）
_BACKDROP_FINGERPRINT_GRID = 16              # 源图指纹的采样格（16×16 个点）
_backdrop_cache = {"order": [], "items": {}}


def _backdrop_fingerprint(img):
    """源图内容指纹（缓存键的一部分）：尺寸 + 稀疏采样的像素。

    刻意不用 img.tobytes()：Pillow 11.2.1 上同尺寸不同内容可能拿到相同字节（判断蒙版
    一致性的 _mask_signature 就是为此改口径的），拿它当缓存键会「换了图却命中旧底图」。
    getpixel 是逐点访问，行为确定；256 次调用对任何尺寸都很便宜。
    """
    W, H = img.size
    if W <= 0 or H <= 0:
        return (W, H, ())
    step = _BACKDROP_FINGERPRINT_GRID
    samples = []
    for gy in range(step):
        y = min(H - 1, (gy * H + H // 2) // step)
        for gx in range(step):
            x = min(W - 1, (gx * W + W // 2) // step)
            samples.append(img.getpixel((x, y)))
    return (W, H, tuple(samples))


def _backdrop_cache_get(key):
    item = _backdrop_cache["items"].get(key)
    return item.copy() if item is not None else None      # 缓存里那份不许被就地改


def _backdrop_cache_put(key, image):
    items, order = _backdrop_cache["items"], _backdrop_cache["order"]
    items[key] = image
    order.append(key)
    while len(order) > _BACKDROP_CACHE_MAX:
        items.pop(order.pop(0), None)


def _cover_to(img, size, zoom=1.0, resample=Image.BILINEAR):
    """把图像按 cover 方式铺满 size（中心裁切）；zoom > 1 再整体放大一点。

    放大一点是为了让模糊后的边缘不发灰：模糊会把画面外的空白吃进来，底图先放大就能把
    「边」推到画面之外。默认 BILINEAR —— 这条路径都是「马上要模糊」的降采样。
    """
    W, H = max(1, int(size[0])), max(1, int(size[1]))
    if img.width <= 0 or img.height <= 0:
        return img
    factor = max(W / img.width, H / img.height) * max(1.0, _safe_float(zoom, 1.0))
    nw, nh = max(W, int(round(img.width * factor))), max(H, int(round(img.height * factor)))
    resized = img.resize((nw, nh), resample)
    left, top = (nw - W) // 2, (nh - H) // 2
    return resized.crop((left, top, left + W, top + H))


def _adjust_image(img, brightness, saturation):
    """亮度/饱和度（Pillow 口径：亮度 0 不变、-100 全黑；饱和度 100 不变、0 灰度）。"""
    if abs(float(brightness)) > 0.01:
        img = ImageEnhance.Brightness(img).enhance(max(0.0, 1.0 + float(brightness) / 100.0))
    if abs(float(saturation) - 100.0) > 0.01:
        img = ImageEnhance.Color(img).enhance(max(0.0, float(saturation) / 100.0))
    return img


def _apply_vignette(backdrop, params):
    """暗角：径向渐变蒙版（中心 0 → 四角 255）× 强度；起始半径之内不变暗。

    渐变是 256×256 拉伸到画布 ⇒ 天然成椭圆（四角最暗），符合常规暗角观感；过渡用
    smoothstep 平滑，避免出现一圈硬边。
    """
    strength = _clamp(_safe_float(params.get("bg_vignette"), 0.0), 0.0, 1.0)
    if strength <= 0.002:
        return backdrop
    start = _clamp(_safe_float(params.get("bg_vignette_start"), 0.0), 0.0, 0.98)
    mask = Image.radial_gradient("L").resize(backdrop.size, Image.BICUBIC)
    lo, span = int(round(start * 255)), max(1, 255 - int(round(start * 255)))
    lut = []
    for value in range(256):
        if value <= lo:
            lut.append(0)
            continue
        t = (value - lo) / span
        t = t * t * (3.0 - 2.0 * t)          # smoothstep：越靠边越暗，但不突兀
        lut.append(int(round(min(1.0, t) * strength * 255)))
    mask = mask.point(lut)
    shade = Image.new("RGBA", backdrop.size,
                      parse_color(params.get("bg_vignette_color"), (0, 0, 0)) + (255,))
    return Image.composite(shade, backdrop, mask)


def _blurred_backdrop(img, size, params):
    """外围模糊底图（画布尺寸的 RGBA）：照片 cover 铺满 → 模糊 → 亮度/饱和度 → 暗角 → 底色混合。

    返回 (底图, 回显)。模糊走「降采样算 + LANCZOS 拉回」，半径同步除 _BACKDROP_WORK_SCALE
    ⇒ 画质够用、耗时低一个数量级；底图按（源图指纹 + 画布 + 底图参数）缓存，所以调文字类
    参数时这一步根本不重算。
    """
    W, H = max(1, int(size[0])), max(1, int(size[1]))
    blur_ratio = _clamp(_safe_float(params.get("bg_blur_ratio"), 0.0), 0.0, 0.08)
    zoom = _clamp(_safe_float(params.get("bg_zoom_ratio"), 1.0), 1.0, 1.5)
    brightness = _clamp(_safe_float(params.get("bg_brightness"), 0.0), -100.0, 100.0)
    saturation = _clamp(_safe_float(params.get("bg_saturation"), 100.0), 0.0, 200.0)
    opacity = int(_clamp(_safe_int(params.get("frame_opacity"), 255), 0, 255))
    v_strength = _clamp(_safe_float(params.get("bg_vignette"), 0.0), 0.0, 1.0)
    v_start = _clamp(_safe_float(params.get("bg_vignette_start"), 0.0), 0.0, 0.98)
    meta = {
        "blur_ratio": blur_ratio, "blur_px": round(blur_ratio * W, 2), "zoom": zoom,
        "brightness": brightness, "saturation": saturation, "opacity": opacity,
        "vignette": {"strength": v_strength, "start": v_start,
                     "color": params.get("bg_vignette_color")},
        "work_scale": _BACKDROP_WORK_SCALE, "cached": False,
    }
    key = (_backdrop_fingerprint(img), (W, H), round(blur_ratio, 6), round(zoom, 4),
           round(brightness, 3), round(saturation, 3), opacity, params.get("frame_color"),
           round(v_strength, 4), round(v_start, 4), params.get("bg_vignette_color"))
    cached = _backdrop_cache_get(key)
    if cached is not None:
        meta["cached"] = True
        return cached, meta

    source = img.convert("RGBA")
    radius_small = blur_ratio * W / _BACKDROP_WORK_SCALE
    if radius_small >= 0.4:
        small = _cover_to(source, (max(1, W // _BACKDROP_WORK_SCALE),
                                   max(1, H // _BACKDROP_WORK_SCALE)), zoom)
        small = _adjust_image(small.filter(ImageFilter.GaussianBlur(radius_small)),
                              brightness, saturation)
        backdrop = small.resize((W, H), Image.LANCZOS)
    else:
        # 不模糊（或半径极小）：老实按原分辨率铺，别白白被降采样糊一次
        backdrop = _adjust_image(_cover_to(source, (W, H), zoom, Image.LANCZOS),
                                 brightness, saturation)
    backdrop = _apply_vignette(backdrop, params)
    if opacity < 255:
        base = Image.new("RGBA", (W, H),
                         parse_color(params.get("frame_color"), (16, 20, 24)) + (255,))
        backdrop = Image.blend(backdrop, base, 1.0 - opacity / 255.0)
    if W * H <= _BACKDROP_CACHE_PIXELS:
        _backdrop_cache_put(key, backdrop.copy())
    return backdrop, meta


def _photo_shadow_layer(photo_size, params):
    """中心照片的投影图层：照片盒外扩 spread、偏移 (dx, dy)、模糊 blur（都是照片宽的比值）。

    返回 (图层, 图层左上角相对照片左上角的位移, 回显)；透明度为 0 时返回 None（不画）。
    图层 = 照片 + 2×pad，pad 容下 3×blur + spread + 最大偏移 ⇒ 模糊的尾巴不会被裁掉。
    """
    W, H = int(photo_size[0]), int(photo_size[1])
    blur = _clamp(_safe_float(params.get("photo_shadow_blur_ratio"), 0.0), 0.0, 0.06) * W
    spread = _clamp(_safe_float(params.get("photo_shadow_spread_ratio"), 0.0), 0.0, 0.05) * W
    dx = _clamp(_safe_float(params.get("photo_shadow_x_ratio"), 0.0), -0.06, 0.06) * W
    dy = _clamp(_safe_float(params.get("photo_shadow_y_ratio"), 0.0), -0.06, 0.06) * W
    opacity = int(_clamp(_safe_int(params.get("photo_shadow_opacity"), 0), 0, 255))
    if opacity <= 0:
        return None
    pad = int(round(blur * 3 + spread + max(abs(dx), abs(dy)))) + 2
    size = (W + 2 * pad, H + 2 * pad)
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rectangle(
        [int(round(pad - spread + dx)), int(round(pad - spread + dy)),
         int(round(pad + W + spread + dx)), int(round(pad + H + spread + dy))], fill=opacity)
    if blur >= 0.6:
        # 模糊半径通常是几十像素，同样降采样算再拉回（投影是纯低频，看不出差别）
        small_w, small_h = max(1, size[0] // _BACKDROP_WORK_SCALE), max(1, size[1] // _BACKDROP_WORK_SCALE)
        mask = mask.resize((small_w, small_h), Image.BILINEAR).filter(
            ImageFilter.GaussianBlur(max(0.4, blur / _BACKDROP_WORK_SCALE))).resize(
            size, Image.LANCZOS)
    layer = Image.new("RGBA", size, parse_color(params.get("photo_shadow_color"), (0, 0, 0)) + (0,))
    layer.putalpha(mask)          # 颜色在 RGB 通道、形状在 alpha ⇒ alpha_composite 时自然叠加
    meta = {"blur_ratio": _safe_float(params.get("photo_shadow_blur_ratio"), 0.0),
            "blur_px": round(blur, 2),
            "x_ratio": _safe_float(params.get("photo_shadow_x_ratio"), 0.0),
            "y_ratio": _safe_float(params.get("photo_shadow_y_ratio"), 0.0),
            "spread_ratio": _safe_float(params.get("photo_shadow_spread_ratio"), 0.0),
            "opacity": opacity, "color": params.get("photo_shadow_color"),
            "offset_px": [round(dx, 2), round(dy, 2)], "pad": pad}
    return layer, pad, meta


def _draw_framed_photo(img, wm_data, info=None, kind="photo_frame"):
    """画框类水印骨架：照片外面加一圈「框」+ 底边一块更高的空白写文字。

    两个 kind 共用这套骨架（画布尺寸、四边留白、底边文字区、四行文字、照片 logo、描边
    全都一样），只有「外面那圈怎么画」不同：
      · photo_frame   = 填纯色（frame_color / frame_opacity），拍立得白边；
      · blur_backdrop = 贴照片自身的模糊底图（bg_*）+ 中心照片投影（photo_shadow_*）+ 暗角。

    - 画布 = 照片 + 四边留白（frame_gap_ratio × 照片宽，四边可分别关掉）
      + 底边文字区（frame_bottom_extra_ratio × 照片宽）。
    - 文字区在照片下方，四行（主标题 / 品牌行 / 参数行 / 日期行）自上而下排在里面，
      行距 = frame_line_gap_ratio × 照片宽，整组按 frame_text_valign 在文字区里对齐；
      每行都能单独开关 / 改字号（墨迹高 ÷ 照片宽）/ 颜色 / 加粗 / 字距 / 宽度 / 对齐 / 字体 / 贴图。
    - 文字留空则用 EXIF 兜底：主标题=厂商+型号、品牌行=厂商、参数行=焦距/光圈/快门/ISO、日期行=拍摄时间。
    - 照片上的 logo（photo_logo*）按「照片盒」归一化坐标摆放，宽度按照片宽的比例，
      可旋转 / 单色化 / 调透明度，且裁在照片内（不会糊到留白上）。
    所有几何量都是「照片宽度的比例」⇒ 1600px 预览与全尺寸导出必然等比一致。
    """
    W, H = img.size
    params = _preset_params(kind, wm_data)

    left_exif, center_exif, params_exif, date_exif = _wm_exif_text(wm_data)
    texts = {
        "title": str(params["title_text"] or left_exif).strip(),
        "brand": str(params["brand_text"] or center_exif).strip(),
        "params": str(params["params_text"] or params_exif).strip(),
        "date": str(params["date_text"] or date_exif).strip(),
    }

    pads = _frame_pads(params, W)
    pad_l, pad_r, pad_t = pads["left"], pads["right"], pads["top"]
    text_margin = max(0, int(round(W * params["frame_text_margin_ratio"])))
    line_gap = max(0, int(round(W * params["frame_line_gap_ratio"])))
    extra = max(0, int(round(W * params["frame_bottom_extra_ratio"])))
    pad_b = pads["bottom"] + extra
    canvas_w, canvas_h = W + pad_l + pad_r, H + pad_t + pad_b
    photo_box = [pad_l, pad_t, W, H]
    warnings, image_fallback = [], []

    # 各行的图层：选了贴图就用贴图（与信息栏同一套口径：墨迹高 = cap_ratio × 照片宽），
    # 否则走文字排版；两样都没有就这一行不画。
    lines = {}
    for name in _FRAME_SEGMENTS:
        if not params.get(f"{name}_show"):
            continue
        logo, meta, warn = _brand_layer(name, params, 1.0, W)
        warnings.extend(warn)
        if logo is not None:
            lines[name] = {"kind": "image", "layer": logo, "font_size": 0, "meta": meta}
            continue
        if params.get(f"{name}_image"):
            image_fallback.append(name)     # 选了贴图但没画成 ⇒ 已回落文字，回显出来
        if not texts[name]:
            continue
        color = parse_color(params[f"{name}_color"], (0, 0, 0))
        family = params.get(f"{name}_font") or params.get("font_family") or ""
        report = {}
        layer, size = _fit_text_layer(
            texts[name], params[f"{name}_bold"], color + (255,),
            max(1, params[f"{name}_cap_ratio"] * W), params[f"{name}_tracking"],
            max(0.0, params.get(f"{name}_width_ratio", 0.0)) * W,
            family=family, report=report)
        if layer is not None:
            if report.get("fallback"):
                if report["fallback"] == "font_unknown":
                    note = f"字体「{report.get('requested') or family}」读不到（不在字体库里），已回落自动字体"
                else:
                    note = f"字体「{report.get('label') or family}」缺少这段文字需要的字形，已回落自动字体"
                warnings.append(f"{_FRAME_LINE_LABELS.get(name, name)}：{note}")
            lines[name] = {"kind": "text", "layer": layer, "font_size": size,
                           "meta": None, "font": report}
    frame_visible = any(pads.values()) or extra > 0
    if not lines and not frame_visible:
        # 留白关掉、底边文字区 0、四行也都没有内容 ⇒ 结果与原图完全一样（回显原因而不是静默）
        if info is not None:
            info["frame"] = {"preset": _preset_id(wm_data), "canvas": [W, H],
                             "skipped": "画框已关闭，四行文字也都没有内容（EXIF 里也没有可用信息）"}
        return img

    # ---- 外框：画框填纯色；背景模糊贴「照片自身的模糊底图」+ 给照片垫一层投影 ----
    backdrop_meta, shadow_meta, shadow_pad = None, None, 0
    if kind == "blur_backdrop":
        canvas, backdrop_meta = _blurred_backdrop(img, (canvas_w, canvas_h), params)
        if params.get("photo_shadow_show"):
            shadow = _photo_shadow_layer((W, H), params)
            if shadow is not None:
                shadow_layer, shadow_pad, shadow_meta = shadow
                # 先垫投影、再贴照片：投影压在照片底下，只露出偏移/外扩的那一圈
                _paste_layer(canvas, shadow_layer, pad_l - shadow_pad, pad_t - shadow_pad)
    else:
        canvas = Image.new("RGBA", (canvas_w, canvas_h),
                           parse_color(params["frame_color"]) + (params["frame_opacity"],))
    canvas.paste(img, (pad_l, pad_t))

    # ---- 文字区排版：整组按 frame_text_valign 在文字区里对齐；居中 = 成片中轴 ----
    order = [name for name in _FRAME_SEGMENTS if name in lines]
    if extra <= 0 and order:
        warnings.append("底边文字区高度为 0，四行文字没有地方可写（把「底边文字区高度」调大即可）")
        order = []
    group_h = sum(lines[name]["layer"].height for name in order) + line_gap * max(0, len(order) - 1)
    area_top, area_h = pad_t + H, pad_b
    valign = str(params["frame_text_valign"])
    if valign == "top":
        y = area_top
    elif valign == "bottom":
        y = area_top + area_h - group_h
    else:
        y = area_top + (area_h - group_h) / 2
    y = max(y, area_top)
    band_l, band_r = text_margin, max(text_margin, canvas_w - text_margin)

    overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    for name in order:
        layer = lines[name]["layer"]
        align = str(params.get(f"{name}_align") or "center")
        if align == "left":
            x = band_l
        elif align == "right":
            x = band_r - layer.width
        else:
            x = (canvas_w - layer.width) / 2
        _paste_layer(overlay, layer, int(round(x)), int(round(y)))
        y += layer.height + line_gap
    canvas = Image.alpha_composite(canvas, overlay)

    # ---- 照片上的 logo：中心点按「照片盒」归一化坐标，裁在照片内（不糊到留白上）----
    logo_layer, logo_meta = _photo_logo_layer(params, W, warnings)
    logo_drawn = False
    if logo_layer is not None:
        cx = pad_l + params["photo_logo_x"] * W
        cy = pad_t + params["photo_logo_y"] * H
        logo_drawn = _paste_clipped(canvas, logo_layer, round(cx - logo_layer.width / 2),
                                    round(cy - logo_layer.height / 2), photo_box)
        if not logo_drawn:
            warnings.append("照片 logo 整个落在照片之外，已跳过")

    # ---- 描边：贴最外圈，粗细 = frame_border_ratio × 照片宽（0 = 只留白不描边）----
    ring = max(0, int(round(W * params["frame_border_ratio"])))
    if ring > 0:
        r, g, b = parse_color(params["frame_border_color"], (0, 0, 0))
        ImageDraw.Draw(canvas).rectangle([0, 0, canvas_w - 1, canvas_h - 1],
                                         outline=(r, g, b, 255), width=ring)

    def line_payload(name):
        """回显该行真正生效的形态：贴图（缩放前后尺寸）还是文字（实测墨迹尺寸 + 字体）。"""
        entry = lines[name]
        if entry["kind"] == "image":
            return {"kind": "image", "image": entry["meta"]}
        out = {"kind": "text", "text": texts[name], "size": entry["font_size"],
               "w": entry["layer"].width, "h": entry["layer"].height}
        if entry.get("font"):
            out["font"] = dict(entry["font"])
        if name in image_fallback:
            out["fallback"] = "image_missing"
        return out

    if info is not None:
        info["frame"] = {
            "preset": _preset_id(wm_data),
            "canvas": [canvas_w, canvas_h],
            "photo_box": list(photo_box),
            "gap": [pad_l, pad_t, pad_r, pads["bottom"]],
            "text_area": [0, pad_t + H, canvas_w, pad_b],
            "text_margin": text_margin,
            "valign": valign,
            "line_gap": line_gap,
            "lines": {name: line_payload(name) for name in lines},
        }
        if kind == "blur_backdrop":
            # 回显底图/投影真正生效的参数（含缓存命中与否）——「背景没变化」先看这里
            info["frame"]["backdrop"] = dict(backdrop_meta or {})
            if shadow_meta is not None:
                info["frame"]["backdrop"]["shadow"] = dict(shadow_meta)
        if ring > 0:
            info["frame"]["border"] = {"ring": ring, "color": params["frame_border_color"]}
        if logo_meta is not None:
            info["frame"]["photo_logo"] = dict(logo_meta, drawn=bool(logo_drawn),
                                               x=round(float(params["photo_logo_x"]), 5),
                                               y=round(float(params["photo_logo_y"]), 5),
                                               scale=round(float(params["photo_logo_scale"]), 5))
        if image_fallback:
            info["frame"]["image_fallback"] = sorted(image_fallback)
        if warnings:
            info["frame"]["warnings"] = warnings
        if wm_data.get("_preset_error"):
            info["frame"]["preset_error"] = wm_data["_preset_error"]
    return canvas


# 两个 kind 的渲染入口（骨架都在 _draw_framed_photo；留成两个实名函数，排查与回显好认）
def _draw_photo_frame(img, wm_data, info=None):
    """画框水印（拍立得风）：四周留白填色 + 底边空白写四行文字。"""
    return _draw_framed_photo(img, wm_data, info=info, kind="photo_frame")


def _draw_blur_backdrop(img, wm_data, info=None):
    """背景模糊：外围铺照片自身的模糊底图（+暗角）+ 中心照片投影，底边照旧写四行文字。"""
    return _draw_framed_photo(img, wm_data, info=info, kind="blur_backdrop")


def _draw_exif_bar(img, wm_data, info=None, scaled_font_size=None):
    """信息栏渲染入口：老前端（无模板参数）→ 历史排版；带模板/新参数 → 模板引擎。"""
    if not _exif_bar_uses_engine(wm_data):
        overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        _draw_exif_bar_legacy(img, overlay, wm_data, info, scaled_font_size)
        return Image.alpha_composite(img, overlay)
    return _draw_exif_bar_engine(img, wm_data, info)


# kind → 渲染器：新增一种模板类型只要在这里登记一个函数（_draw_exif_bar 自带新老分流）
_PRESET_RENDERERS = {
    "exif_bar": _draw_exif_bar,              # 信息栏：照片下方追加参数带 / 叠在照片上
    "photo_frame": _draw_photo_frame,        # 画框水印：四周留白 + 底边写文字（拍立得）
    "blur_backdrop": _draw_blur_backdrop,    # 背景模糊：模糊底图 + 照片投影 + 底边写文字
}


def apply_watermark(img, wm_data, info=None):
    """Apply text or image watermark onto an RGBA image. Returns RGBA image or None if image watermark is missing.

    图片水印支持多张：items=[{id, x, y, scale, rotation, opacity}]，x/y 是水印中心点的
    归一化坐标(0~1)、scale 是占画布宽度的比例，因此 1600px 预览底图与全尺寸导出天然等比一致。

    字号换算：前端字号以「最长边 1600px」为基准（预览底图正好是这个尺寸），
    画布越大字号按比例放大，因此「预览所见 = 下载所得」；≤1600px 的图预览与下载
    同一张画布，系数恒为 1.0，字号含义与原来完全一致。

    info（可选）：传入 dict 时回填本次实际使用的参数，便于定位「预览没变化」类问题。
    """
    # 模板框架：显式传入 > 模板 presets.json > 白名单默认值（不带 preset 时与历史行为一致）
    wm_data = _resolve_watermark_payload(wm_data)
    wm_type = str(wm_data.get("type", "text") or "text")
    opacity = _clamp(_safe_int(wm_data.get("opacity", 128), 128), 0, 255)
    position_x = _safe_float(wm_data.get("position_x", 0.5), 0.5)
    position_y = _safe_float(wm_data.get("position_y", 0.5), 0.5)
    rotation = _safe_float(wm_data.get("rotation", 0), 0.0)
    size_factor = max(img.width, img.height) / PREVIEW_MAX_DIM

    def scaled_font_size(raw):
        return max(1, int(round(_safe_float(raw, 36) * size_factor)))

    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    if info is not None:
        info["type"] = wm_type

    if wm_type == "image":
        # 支持多张图片水印：items=[{id,x,y,scale,rotation,opacity}, ...]
        # x,y 为水印中心点的归一化坐标(0~1)，scale 为「占画布宽度的比例」
        # ⇒ 1600px 预览底图与全尺寸导出天然等比一致，无需换算
        raw_items = wm_data.get("items")
        if isinstance(raw_items, list) and raw_items:
            items = [it for it in raw_items if isinstance(it, dict)]
        else:
            # 兼容旧接口：单张 watermark.png + 顶层参数
            items = [{
                "id": None,
                "x": position_x,
                "y": position_y,
                "scale": wm_data.get("scale", 0.3),
                "rotation": rotation,
                "opacity": opacity,
            }]

        placed = []
        for item in items:
            wm_id = item.get("id")
            wm_img = _load_watermark_image(wm_id) if wm_id else _load_legacy_watermark_image()
            if wm_img is None:
                continue

            item_scale = _clamp(_safe_float(item.get("scale", wm_data.get("scale", 0.3)), 0.3), 0.01, 1.0)
            item_rotation = _safe_float(item.get("rotation", rotation), 0.0)
            item_opacity = _clamp(_safe_int(item.get("opacity", opacity), opacity), 0, 255)
            item_x = _safe_float(item.get("x", item.get("position_x", position_x)), position_x)
            item_y = _safe_float(item.get("y", item.get("position_y", position_y)), position_y)

            target_w = max(1, int(img.width * item_scale))
            target_h = max(1, int(wm_img.height * (target_w / wm_img.width)))
            piece = wm_img.resize((target_w, target_h), Image.LANCZOS, reducing_gap=3.0)
            if item_opacity < 255:
                piece = apply_opacity(piece, item_opacity)
            if item_rotation:
                piece = piece.rotate(item_rotation, expand=True, resample=Image.BICUBIC)
            paste_x = int(item_x * img.width - piece.width / 2)
            paste_y = int(item_y * img.height - piece.height / 2)
            _paste_layer(overlay, piece, paste_x, paste_y)
            placed.append({
                "id": wm_id,
                "x": round(item_x, 5),
                "y": round(item_y, 5),
                "scale": round(item_scale, 5),
                "w": piece.width,
                "h": piece.height,
            })

        if not placed:
            return None
        if info is not None:
            info["items"] = placed
    elif wm_type == "exif":
        # 信息栏自成一套画布逻辑（append 模式会加高画布），整体交给渲染器：
        # 模板合并与白名单校验已在 _resolve_watermark_payload 里完成
        return _PRESET_RENDERERS["exif_bar"](img, wm_data, info=info, scaled_font_size=scaled_font_size)
    elif wm_type == "frame":
        # 画框水印同样自成一套画布逻辑（四边留白 + 底边文字区会放大画布）
        return _PRESET_RENDERERS["photo_frame"](img, wm_data, info=info)
    elif wm_type == "blur":
        # 背景模糊：画布 = 照片 + 背景边 + 底边文字区（照片像素一个不动）
        return _PRESET_RENDERERS["blur_backdrop"](img, wm_data, info=info)
    else:
        # 空文本/纯空白绝不能「静默不画」：那会让用户看到“点了预览毫无变化也没有提示”
        text = str(wm_data.get("text") or "").strip() or "水印"
        font_size = scaled_font_size(wm_data.get("font_size", 36) or 36)
        color = wm_data.get("color", "#ffffff")
        bold = bool(wm_data.get("bold", False))
        r, g, b = parse_color(color)
        draw = ImageDraw.Draw(overlay)
        font = load_watermark_font(font_size, bold=bold, text=text)
        if info is not None:
            info["text"] = text
            info["font"] = os.path.basename(getattr(font, "path", "") or "default")
            info["font_size"] = font_size

        bbox = draw.textbbox((0, 0), text, font=font)
        text_w = bbox[2] - bbox[0]
        text_h = bbox[3] - bbox[1]

        if rotation != 0:
            tmp = Image.new("RGBA", (text_w + 20, text_h + 20), (0, 0, 0, 0))
            tmp_draw = ImageDraw.Draw(tmp)
            tmp_draw.text((10, 10), text, font=font, fill=(r, g, b, opacity))
            tmp = tmp.rotate(rotation, expand=True, resample=Image.BICUBIC)
            paste_x = int(position_x * img.width - tmp.width / 2)
            paste_y = int(position_y * img.height - tmp.height / 2)
            _paste_layer(overlay, tmp, paste_x, paste_y)
        else:
            x = int(position_x * img.width - text_w / 2)
            y = int(position_y * img.height - text_h / 2)
            draw.text((x, y), text, font=font, fill=(r, g, b, opacity))

    return Image.alpha_composite(img, overlay)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/upload", methods=["POST"])
@_locked
def upload_image():
    if "file" not in request.files:
        return jsonify({"error": "未选择文件"}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "文件名为空"}), 400

    image_bytes = f.read()
    try:
        raw_img = Image.open(io.BytesIO(image_bytes))
        raw_img.load()
    except Image.DecompressionBombError:
        return jsonify({"error": "图片像素过高，无法处理，请缩小尺寸后再试"}), 400
    except Exception:
        return jsonify({"error": "无法解析图片，请确认是有效的 JPG/PNG 等图片格式"}), 400

    # 方向归一化：竖拍手机照片用 Orientation(6/8) 记录朝向，Pillow 不会自动摆正。
    # 不摆正就会出现「预览是竖的、水印却按横的排」——成片里水印整整歪 90°。
    # 摆正后必须把 Orientation 写成 1，否则看图软件/浏览器会再转一次（双重旋转）。
    try:
        raw_orientation = int(raw_img.getexif().get(ImageIFD.Orientation, 1) or 1)
    except Exception:
        raw_orientation = 1
    img = ImageOps.exif_transpose(raw_img) or raw_img

    # Save original as JPEG for EXIF operations（保留原始 EXIF，并规范化，避免 piexif 后续解析崩溃）
    original_path = os.path.join(UPLOAD_DIR, "original.jpg")
    if img.mode != "RGB":
        rgb_img = img.convert("RGB")
    else:
        rgb_img = img
    # 生成规范化 EXIF 写进 original.jpg。recovery 回传「从缩略图 IFD 补回了哪些身份标签」
    # 与「EXIF 结构有什么问题」，由上传接口回显给用户（修复前这些信息是静默丢弃的）。
    exif_recovery = {}
    exif_for_save = _canonical_exif_for_save(image_bytes, orientation=1, recovery=exif_recovery)
    if exif_for_save:
        rgb_img.save(original_path, "JPEG", quality=95, exif=exif_for_save)
    else:
        rgb_img.save(original_path, "JPEG", quality=95)

    # Save working copy as PNG for watermark operations
    working_path = os.path.join(UPLOAD_DIR, "working.png")
    if img.mode != "RGBA":
        img = img.convert("RGBA")
    img.save(working_path, "PNG")

    info = get_image_info(image_bytes)
    # 像素已摆正 ⇒ 尺寸/格式按摆正后的图回显，前端信息栏与实际看到的画布一致
    info.update({
        "width": img.width,
        "height": img.height,
        "orientation": raw_orientation,
        "orientation_normalized": raw_orientation != 1,
    })
    exif_data = read_exif(image_bytes)

    # 记下原始格式/文件名：下载「保持原格式」要用（source.json 随 /api/reset 一起清）
    try:
        with open(os.path.join(UPLOAD_DIR, _SOURCE_NAME), "w", encoding="utf-8") as sf:
            json.dump({
                "format": (raw_img.format or "").upper(),
                "filename": f.filename,
                "has_alpha": raw_img.mode in ("RGBA", "LA", "P"),
                "size": [img.width, img.height],
                "orientation": raw_orientation,
            }, sf, ensure_ascii=False)
    except Exception:
        pass

    # 预览底图：最长边 ≤1600px 的 JPEG。后续所有实时预览都在它上面合成，
    # 避免每帧都解码并重编码全尺寸 PNG（原先单帧 ~3.9s、响应体 ~65MB，拖滑块必卡）。
    base = _cache_preview_base(img)
    if base is None:
        return jsonify({"error": "无法生成预览图"}), 500
    base_bytes, base_size, _ = base
    _clear_exif_bar_cache()

    return jsonify({
        "image_info": info,
        "exif_data": exif_data,
        "preview": _data_url(base_bytes, "image/jpeg"),
        "preview_size": {"width": base_size[0], "height": base_size[1]},
        "preview_max_dim": PREVIEW_MAX_DIM,
        # 从缩略图 IFD 补齐的相机身份标签（make/model/software/datetime…）与结构告警
        "exif_recovered": exif_recovery.get("display", {}),
        "exif_warnings": exif_recovery.get("warnings", []),
    })


@app.route("/api/exif/update", methods=["POST"])
@_locked
def update_exif():
    data = request.json
    edits = data.get("edits", {})

    original_path = os.path.join(UPLOAD_DIR, "original.jpg")
    if not os.path.exists(original_path):
        return jsonify({"error": "请先上传图片"}), 400

    with open(original_path, "rb") as f:
        original_bytes = f.read()

    exif_dict, exif_from_piexif, _exif_warnings = _load_exif_dict(original_bytes)

    # 显示名 → tag_id 的逆向映射：标签表内显示名唯一（见 _EXIF_NAME_TO_TAG）
    TAG_MAP = _EXIF_NAME_TO_TAG

    EXIF_IFD_TAGS = set(getattr(ExifIFD, attr) for attr in dir(ExifIFD) if not attr.startswith("_") and isinstance(getattr(ExifIFD, attr), int))

    for name, value in edits.items():
        tag_id = TAG_MAP.get(name)
        if tag_id is None:
            continue
        ifd_name = "Exif" if tag_id in EXIF_IFD_TAGS else "0th"

        if isinstance(value, str):
            if tag_id in (ExifIFD.FNumber,):
                try:
                    v = float(value.replace("f/", ""))
                    exif_dict[ifd_name][tag_id] = (int(v * 10), 10)
                except ValueError:
                    pass
            elif tag_id in (ExifIFD.FocalLength, ExifIFD.FocalLengthIn35mmFilm):
                try:
                    v = float(value.replace("mm", ""))
                    exif_dict[ifd_name][tag_id] = (int(v * 10), 10)
                except ValueError:
                    pass
            elif tag_id in (ExifIFD.ISOSpeedRatings, ExifIFD.ISOSpeed,
                            ExifIFD.StandardOutputSensitivity, ExifIFD.RecommendedExposureIndex):
                # 前端回传的是格式化文本（"ISO 400"），取其中的整数
                v = _parse_int_text(value)
                if v is not None:
                    exif_dict[ifd_name][tag_id] = v
            elif tag_id == ExifIFD.ExposureTime:
                try:
                    v_str = value.replace("s", "")
                    if "/" in v_str:
                        num, den = v_str.split("/")
                        exif_dict[ifd_name][tag_id] = (int(num), int(den))
                    else:
                        v = float(v_str)
                        if v < 1:
                            exif_dict[ifd_name][tag_id] = (1, int(round(1 / v)))
                        else:
                            exif_dict[ifd_name][tag_id] = (int(v * 10), 10)
                except (ValueError, ZeroDivisionError):
                    pass
            elif tag_id == ExifIFD.UserComment:
                exif_dict[ifd_name][tag_id] = UserComment.dump(value, encoding="unicode")
            else:
                tag_type = _spec_type(ifd_name, tag_id)
                if tag_type in (1, 3, 4, 6, 8, 9):
                    # 整型/枚举型标签：前端显示的是格式化文本（"正常"、"ISO 400"），
                    # 必须还原成整数，否则会把 UTF-8 文本写进 SHORT/LONG 标签导致写盘失败
                    code = _enum_code_from_text(tag_id, value)
                    if code is not None:
                        exif_dict[ifd_name][tag_id] = code
                else:
                    if tag_type == 2:
                        # ASCII 文本标签按规范写 ASCII（中文才退回 UTF-8），
                        # 否则「相机型号=索尼 ILCE-6700」这类中文会让严格解析的软件显示乱码
                        exif_dict[ifd_name][tag_id] = _encode_ascii_text(value)
                    else:
                        exif_dict[ifd_name][tag_id] = value.encode("utf-8")
        elif isinstance(value, (int, float)):
            exif_dict[ifd_name][tag_id] = value

    try:
        exif_bytes = _dumpable_exif_bytes(exif_dict, exif_from_piexif)
        # 只替换 APP1(EXIF) 段：像素数据原样保留，无画质损失，也不必重写 working.png
        try:
            updated_bytes = piexif.insert(exif_bytes, original_bytes)
        except Exception:
            img = Image.open(io.BytesIO(original_bytes))
            if img.mode == "RGBA":
                img = img.convert("RGB")
            buf = io.BytesIO()
            img.save(buf, "JPEG", exif=exif_bytes, quality=95)
            updated_bytes = buf.getvalue()

        with open(original_path, "wb") as f:
            f.write(updated_bytes)

        # working.png 像素未变，无需重写：预览底图缓存依旧有效，
        # 改完参数后预览仍然即时（信息栏文本按 original.jpg 的 mtime 自动失效）。
        _clear_exif_bar_cache()
        return jsonify({"success": True, "exif_data": read_exif(updated_bytes)})
    except Exception as e:
        return jsonify({"error": f"写入EXIF失败: {str(e)}"}), 500


@app.route("/api/watermark/upload", methods=["POST"])
@_locked
def upload_watermark():
    if "file" not in request.files:
        return jsonify({"error": "未选择水印文件"}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "文件名为空"}), 400

    try:
        img = Image.open(io.BytesIO(f.read()))
        img.load()
    except Exception:
        return jsonify({"error": "无法识别的图片文件"}), 400

    if img.mode != "RGBA":
        img = img.convert("RGBA")

    # 支持多次导入：每次生成一个新的 watermark_<id>.png，互不覆盖
    wm_id = uuid.uuid4().hex[:8]
    wm_path = _watermark_path(wm_id)
    if wm_path is None:
        return jsonify({"error": "水印 id 非法"}), 400
    img.save(wm_path, "PNG")
    # 兼容旧前端（缓存里只会发单张 type=image、不带 items）
    img.save(os.path.join(UPLOAD_DIR, _LEGACY_WM_NAME), "PNG")
    _clear_watermark_cache()

    return jsonify({
        "success": True,
        "id": wm_id,
        "width": img.width,
        "height": img.height,
        "preview": _data_url(_watermark_layer_preview(img), "image/png"),
    })


@app.route("/api/watermark/remove", methods=["POST"])
@_locked
def remove_watermark():
    data = request.json or {}
    wm_id = str(data.get("id") or "").strip()
    path = _watermark_path(wm_id)
    if path is None:
        return jsonify({"error": "缺少或非法的水印 id"}), 400

    if os.path.exists(path):
        os.remove(path)
    _clear_watermark_cache()

    # 没有多张水印了，同步清掉旧接口的单张文件，避免旧页面还能画出已删除的水印
    ids = _list_watermark_ids()
    if not ids:
        legacy = os.path.join(UPLOAD_DIR, _LEGACY_WM_NAME)
        if os.path.exists(legacy):
            os.remove(legacy)

    return jsonify({"success": True, "remaining": ids})


@app.route("/api/watermark/presets", methods=["GET"])
@_locked
def list_presets():
    """模板列表 + 控件描述：前端据此动态生成 UI，不硬编码任何参数名。

    每条模板返回 params（模板自带数值）与 controls（含类型/范围/选项/单位/分组），
    因此「加模板 / 改数值 / 改控件」都只是改 presets.json，前端零改动。
    """
    items = []
    for preset_id, preset in _load_presets().items():
        specs = _PRESET_PARAM_SPECS.get(preset["kind"], {})
        controls = []
        for raw in preset.get("controls") or []:
            entry = {"key": raw} if isinstance(raw, str) else dict(raw)
            spec = specs.get(entry.get("key"))
            if spec is None:
                continue
            entry.setdefault("label", spec.get("label") or entry["key"])
            entry["type"] = spec["type"]
            entry["value"] = preset["params"].get(entry["key"], spec.get("default"))
            for field in ("min", "max", "scale", "unit", "hint", "optional", "empty_label"):
                if field in spec:
                    entry[field] = spec[field]
            if spec["type"] == "choice":
                entry["choices"] = list(spec["choices"])
            if spec["type"] in ("float", "int"):
                # 步长 = 显示小数位（见 _preset_control_step）：网格必须与数字框的
                # 小数位一致，否则 range 会把用户手输的精确值吸附到粗网格上
                entry.setdefault("step", _preset_control_step(spec))
            if spec["type"] == "font_ref":
                # 字体下拉的选项由后端下发（系统字体按本机文件存在过滤 + 我上传的）
                entry["options"] = _font_choice_options()
            controls.append(entry)
        items.append({
            "id": preset_id,
            "name": preset["name"],
            "kind": preset["kind"],
            "description": preset["description"],
            "params": preset["params"],
            "controls": controls,
            # 模板里写错/写漏的键回显出来，省得「改了没生效」只能靠猜
            "invalid_params": preset.get("invalid_params", []),
        })
    items.sort(key=lambda p: (p["kind"], p["id"]))
    return jsonify({"presets": items, "kinds": sorted(_PRESET_PARAM_SPECS)})


@app.route("/api/watermark/list", methods=["GET"])
@_locked
def list_watermarks():
    """已导入的水印列表（刷新页面后恢复用），preview 为 ≤1000px 的 PNG data URL。"""
    items = []
    for wm_id in _list_watermark_ids():
        img = _load_watermark_image(wm_id)
        if img is None:
            continue
        items.append({
            "id": wm_id,
            "width": img.width,
            "height": img.height,
            "preview": _data_url(_watermark_layer_preview(img), "image/png"),
        })
    return jsonify({"items": items})


# ---------------------------------------------------------------------------
# 品牌贴图 API：跨会话保留的长期资源（/api/reset 不清），供信息栏模板引用
# ---------------------------------------------------------------------------
@app.route("/api/brand/upload", methods=["POST"])
@_locked
def upload_brand_image():
    """上传一张品牌贴图；返回 id，模板参数里用 {left,center,params,date}_image 引用它。"""
    if "file" not in request.files:
        return jsonify({"error": "未选择贴图文件"}), 400
    upload = request.files["file"]
    if not upload.filename:
        return jsonify({"error": "文件名为空"}), 400
    try:
        img = Image.open(io.BytesIO(upload.read()))
        img.load()
        img = img.convert("RGBA")
    except Exception:
        # 矢量图/损坏文件在这里就被挡住，不会变成 500 HTML
        return jsonify({"error": "无法解析图片，请换一张（推荐透明底 PNG）"}), 400

    brand_id = uuid.uuid4().hex[:8]
    path = _brand_path(brand_id)
    if path is None:      # 理论不可达，守住「id 不过正则就不落盘」的约定
        return jsonify({"error": "贴图 id 非法"}), 400
    img.save(path, "PNG")

    name = os.path.basename(str(upload.filename)).strip()[:120] or f"{_BRAND_PREFIX}{brand_id}.png"
    index = _load_brand_index()
    index[brand_id] = {"name": name, "width": img.width, "height": img.height,
                       "uploaded_at": os.stat(path).st_mtime}
    _save_brand_index(index)
    _clear_watermark_cache()
    return jsonify({"success": True, "id": brand_id, "name": name,
                    "width": img.width, "height": img.height,
                    "preview": _data_url(_watermark_layer_preview(img), "image/png")})


@app.route("/api/brand/list", methods=["GET"])
@_locked
def list_brand_images():
    """已上传的品牌贴图列表（刷新页面后前端据此渲染缩略图条）。

    顺带把直接丢进 brands/ 目录的散图收编进库：用户手动放 logo 是常见做法，
    不收编的话库里看着是空的 —— 历史上就因此误判成「前端根本没做上传入口」。
    """
    adopted = _adopt_loose_brands()
    return jsonify({"items": _brand_items(), "adopted": adopted})


@app.route("/api/brand/remove", methods=["POST"])
@_locked
def remove_brand_image():
    """删除一张品牌贴图（清单与缓存一起清，引用它的模板会回落到文字）。"""
    data = request.json or {}
    brand_id = str(data.get("id") or "").strip()
    path = _brand_path(brand_id)
    if path is None:
        return jsonify({"error": "缺少或非法的贴图 id"}), 400
    if os.path.exists(path):
        os.remove(path)
    index = _load_brand_index()
    index.pop(brand_id, None)
    _save_brand_index(index)
    _clear_watermark_cache()
    return jsonify({"success": True, "remaining": _list_brand_ids()})


# ---------------------------------------------------------------------------
# 字体 API：信息栏文字可选「系统字体 / 我上传的字体」；上传的字体与品牌贴图一样
# 是长期资产（跨会话保留、/api/reset 不清），只由字体旁的「删除」按钮清掉。
# ---------------------------------------------------------------------------
@app.route("/api/font/upload", methods=["POST"])
@_locked
def upload_font():
    """上传一个字体文件；返回 id，模板参数里用 font_family / {段}_font 引用它。"""
    if "file" not in request.files:
        return jsonify({"error": "未选择字体文件"}), 400
    upload = request.files["file"]
    if not upload.filename:
        return jsonify({"error": "文件名为空"}), 400
    raw = upload.read()
    if not raw:
        return jsonify({"error": "字体文件是空的"}), 400
    if len(raw) > _FONT_MAX_BYTES:
        return jsonify({"error": f"字体文件超过 {_FONT_MAX_BYTES // (1024 * 1024)}MB 上限"}), 400

    font_id = uuid.uuid4().hex[:8]
    path = _font_path(font_id)
    if path is None:      # 理论不可达，守住「id 不过正则就不落盘」的约定
        return jsonify({"error": "字体 id 非法"}), 400
    # 先写文件再用 Pillow 试加载：不是字体（或字体损坏）立刻删掉并报 400，
    # 绝不把「加载不了的字体」留在库里让用户对着一堆不生效的选项猜
    try:
        with open(path, "wb") as f:
            f.write(raw)
        probe = ImageFont.truetype(path, 24)
        mask = probe.getmask(_FONT_PROBE_TEXT)
    except Exception:
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass
        return jsonify({"error": "无法解析字体文件，请上传 .ttf / .otf / .ttc（未压缩或已解压）"}), 400
    if mask is None or mask.getbbox() is None:
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass
        return jsonify({"error": "这个字体画不出西文字形，换一个试试"}), 400

    name = os.path.basename(str(upload.filename)).strip()[:120] or f"{_FONT_PREFIX}{font_id}.ttf"
    index = _load_font_index()
    index[font_id] = {"name": name, "uploaded_at": os.stat(path).st_mtime}
    _save_font_index(index)
    return jsonify({"success": True, "id": font_id, "name": name,
                    "items": _font_items(), "fonts": _font_choice_options()})


@app.route("/api/font/list", methods=["GET"])
@_locked
def list_fonts():
    """已上传的字体 + 下拉选项（前端也可直接用 /api/watermark/presets 里的 options）。"""
    return jsonify({"items": _font_items(), "fonts": _font_choice_options()})


@app.route("/api/font/remove", methods=["POST"])
@_locked
def remove_font():
    """删除一个上传的字体（引用它的模板会自动回落到自动挑体，系统字体删不掉）。"""
    data = request.json or {}
    font_id = str(data.get("id") or "").strip()
    path = _font_path(font_id)
    if path is None:
        return jsonify({"error": "缺少或非法的字体 id"}), 400
    if font_id in _FONT_FAMILY_LABELS:
        return jsonify({"error": "系统字体不能删除（下拉里空一格 = 自动）"}), 400
    if os.path.exists(path):
        os.remove(path)
    index = _load_font_index()
    index.pop(font_id, None)
    _save_font_index(index)
    _clear_watermark_cache()
    return jsonify({"success": True, "remaining": _list_font_ids(),
                    "items": _font_items(), "fonts": _font_choice_options()})


# ---------------------------------------------------------------------------
# 配置 API：自动保存槽（current）+ 命名快照 + 导出/导入
#
# 前端把「当前这套参数」防抖 POST 到 /api/config/save（打开页面 GET 回来还原），
# 另存为/切换/改名/删除走 /api/config/slots/*，导出/导入就是同一份 JSON 进出一个文件。
# ---------------------------------------------------------------------------
@app.route("/api/config", methods=["GET"])
@_locked
def get_config():
    """当前生效的配置（configs/current.json）+ 命名快照清单（下拉切换用）。"""
    path = _config_path()
    cfg, invalid, warnings = None, [], []
    if os.path.exists(path):
        raw, err = _read_config_file(path)
        if err:
            warnings.append(f"{err}: {_CONFIG_CURRENT_NAME}")
        else:
            cfg, invalid, warnings = _normalize_config(raw)
    stamp = _config_mtime(path)
    return jsonify({
        "exists": cfg is not None,
        "config": cfg,
        "saved_at": stamp,
        "saved_at_text": _stamp_text(stamp),
        "invalid_params": invalid,
        "warnings": warnings,
        "slots": _config_slot_items(),
        "max_slots": _CONFIG_MAX_SLOTS,
        "version": _CONFIG_VERSION,
    })


@app.route("/api/config/save", methods=["POST"])
@_locked
def save_config():
    """自动保存：规范化后写 current.json（前端防抖调用，文件只有几 KB）。"""
    data = request.json if isinstance(request.json, dict) else {}
    raw = data.get("config") if isinstance(data.get("config"), dict) else data
    cfg, invalid, warnings = _normalize_config(raw)
    path = _config_path()
    if not _write_config_file(path, cfg):
        return jsonify(f"配置写入失败：{CONFIG_DIR} 可能不可写（部署目录权限 / 磁盘）"), 500
    stamp = _config_mtime(path)
    return jsonify({"success": True, "config": cfg, "saved_at": stamp,
                    "saved_at_text": _stamp_text(stamp),
                    "invalid_params": invalid, "warnings": warnings})


@app.route("/api/config/export", methods=["GET"])
@_locked
def export_config():
    """导出配置成 .json 文件（默认当前生效的 current；?slot=<id> 可导出某个快照）。

    ?inline=1 = 同一份 JSON 但**不带 attachment**：浏览器标签页直接显示正文。
    部署环境 / 预览 iframe 禁掉下载时，前端「查看 JSON」面板靠它取正文给用户复制；
    导入接口认的就是这段文本，所以复制粘贴与下载文件完全等价。
    """
    slot_id = str(request.args.get("slot") or "").strip()
    path = _config_path(slot_id) if slot_id else _config_path()
    if path is None:
        return jsonify({"error": "配置 id 非法"}), 400
    if not os.path.exists(path):
        return jsonify({"error": "还没有可导出的配置：改一下任意参数会自动保存，或点「另存为」存一套"}), 400
    raw, err = _read_config_file(path)
    if err:
        return jsonify({"error": "配置文件读不动，无法导出"}), 400
    cfg, _invalid, _warnings = _normalize_config(raw)
    payload = dict(cfg)
    payload["app"] = _CONFIG_APP
    payload["exported_at"] = _stamp_text(time.time())
    if slot_id:
        payload["name"] = str((_load_config_index().get(slot_id) or {}).get("name") or slot_id)
    blob = io.BytesIO(json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"))
    filename = f"watermark-cfg-{time.strftime('%Y%m%d-%H%M%S')}.json"
    # ?inline=1：不带 attachment 的同一份 JSON —— 宿主环境禁止下载（预览 iframe / 代理拦截）时，
    # 前端「查看 JSON」面板照样能拿到正文；导入接口本来就认这段文本。
    if str(request.args.get("inline") or "").strip().lower() in ("1", "true", "yes", "on"):
        resp = send_file(blob, mimetype="application/json")
        resp.headers["Cache-Control"] = "no-store"
        return resp
    return send_file(blob, mimetype="application/json", as_attachment=True,
                     download_name=filename)


@app.route("/api/config/import", methods=["POST"])
@_locked
def import_config():
    """导入配置文件 → 覆盖「当前生效配置」（命名快照不受影响）。

    兼容两种传法：multipart 上传 file（前端用这种，能带文件名）或直接 POST JSON。
    BOM 也认（记事本另存过的 JSON 带 BOM，写着玩就会打不开）。
    """
    upload = request.files.get("file")
    raw, filename = None, ""
    if upload is not None:
        filename = os.path.basename(str(upload.filename or ""))[:120]
        blob = upload.read(_CONFIG_MAX_BYTES + 1)
        if len(blob) > _CONFIG_MAX_BYTES:
            return jsonify({"error": f"配置文件超过 {_CONFIG_MAX_BYTES // 1024}KB 上限"}), 400
        try:
            raw = json.loads(blob.decode("utf-8-sig"))
        except Exception:
            return jsonify({"error": "配置文件不是合法的 JSON"}), 400
    else:
        data = request.json if isinstance(request.json, dict) else {}
        raw = data.get("config") if isinstance(data.get("config"), dict) else data
    if not isinstance(raw, dict) or not raw:
        return jsonify({"error": "配置内容为空"}), 400
    version = _safe_int(raw.get("version"), _CONFIG_VERSION)
    if version != _CONFIG_VERSION:
        return jsonify({"error": f"配置版本不兼容（v{version}）；本工具只认 v{_CONFIG_VERSION}"}), 400
    cfg, invalid, warnings = _normalize_config(raw)
    path = _config_path()
    if not _write_config_file(path, cfg):
        return jsonify(f"配置写入失败：{CONFIG_DIR} 可能不可写（部署目录权限 / 磁盘）"), 500
    stamp = _config_mtime(path)
    return jsonify({"success": True, "config": cfg, "filename": filename,
                    "saved_at": stamp, "saved_at_text": _stamp_text(stamp),
                    "invalid_params": invalid, "warnings": warnings,
                    "slots": _config_slot_items()})


@app.route("/api/config/slots/save", methods=["POST"])
@_locked
def save_config_slot():
    """另存为命名快照：{name, config?, id?}（不给 config 就把 current 存过去）。

    带 id = 覆盖已有快照；不带 = 新建。
    """
    data = request.json if isinstance(request.json, dict) else {}
    name = str(data.get("name") or "").strip()[:_CONFIG_MAX_NAME]
    if not name:
        return jsonify({"error": "请先给配置起个名字"}), 400
    raw = data.get("config")
    if not isinstance(raw, dict):
        raw, err = _read_config_file(_config_path())
        if err:
            return jsonify({"error": "当前还没有可保存的配置"}), 400
    slot_id = str(data.get("id") or "").strip()
    if slot_id:
        path = _config_path(slot_id)
        if path is None or not os.path.exists(path):
            return jsonify({"error": "找不到该配置"}), 400
    else:
        if len(_list_config_slot_ids()) >= _CONFIG_MAX_SLOTS:
            return jsonify({"error": f"命名配置最多 {_CONFIG_MAX_SLOTS} 个，请先删掉几个"}), 400
        slot_id = uuid.uuid4().hex[:8]
        path = _config_path(slot_id)
        if path is None:      # 理论不可达，守住「id 不过正则就不落盘」的约定
            return jsonify({"error": "配置 id 非法"}), 400
    cfg, invalid, warnings = _normalize_config(raw)
    if not _write_config_file(path, cfg):
        return jsonify(f"配置写入失败：{CONFIG_DIR} 可能不可写（部署目录权限 / 磁盘）"), 500
    index = _load_config_index()
    index[slot_id] = {"name": name, "saved_at": time.time()}
    if not _save_config_index(index):
        warnings.append("config_index_unwritable")
    return jsonify({"success": True, "id": slot_id, "name": name, "config": cfg,
                    "invalid_params": invalid, "warnings": warnings,
                    "slots": _config_slot_items(), "max_slots": _CONFIG_MAX_SLOTS})


@app.route("/api/config/slots/rename", methods=["POST"])
@_locked
def rename_config_slot():
    """给命名快照改名（只动清单里的名字，配置文件本身不重写）。"""
    data = request.json if isinstance(request.json, dict) else {}
    slot_id = str(data.get("id") or "").strip()
    name = str(data.get("name") or "").strip()[:_CONFIG_MAX_NAME]
    path = _config_path(slot_id) if slot_id else None
    if path is None or not os.path.exists(path):
        return jsonify({"error": "找不到该配置"}), 400
    if not name:
        return jsonify({"error": "名字不能为空"}), 400
    index = _load_config_index()
    entry = dict(index.get(slot_id) or {})
    entry.update({"name": name, "saved_at": time.time()})
    index[slot_id] = entry
    if not _save_config_index(index):
        return jsonify({"error": "配置清单写入失败，请重试"}), 500
    return jsonify({"success": True, "id": slot_id, "name": name,
                    "slots": _config_slot_items()})


@app.route("/api/config/slots/delete", methods=["POST"])
@_locked
def delete_config_slot():
    """删除命名快照（当前生效的 current 不受影响）。"""
    data = request.json if isinstance(request.json, dict) else {}
    slot_id = str(data.get("id") or "").strip()
    path = _config_path(slot_id) if slot_id else None
    if path is None:
        return jsonify({"error": "配置 id 非法"}), 400
    if os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            return jsonify({"error": "删除失败，请重试"}), 500
    index = _load_config_index()
    index.pop(slot_id, None)
    _save_config_index(index)
    return jsonify({"success": True, "id": slot_id, "slots": _config_slot_items()})


@app.route("/api/config/apply", methods=["POST"])
@_locked
def apply_config_slot():
    """切换到某个命名快照：把它**拷回 current**（current 永远是唯一真源）。"""
    data = request.json if isinstance(request.json, dict) else {}
    slot_id = str(data.get("slot") or data.get("id") or "").strip()
    path = _config_path(slot_id) if slot_id else None
    if path is None:
        return jsonify({"error": "配置 id 非法"}), 400
    raw, err = _read_config_file(path)
    if err:
        return jsonify({"error": "该配置读不动（文件可能损坏）"}), 400
    cfg, invalid, warnings = _normalize_config(raw)
    if not _write_config_file(_config_path(), cfg):
        return jsonify(f"配置写入失败：{CONFIG_DIR} 可能不可写（部署目录权限 / 磁盘）"), 500
    stamp = _config_mtime(_config_path())
    return jsonify({"success": True, "id": slot_id, "config": cfg,
                    "saved_at": stamp, "saved_at_text": _stamp_text(stamp),
                    "invalid_params": invalid, "warnings": warnings})


@app.route("/api/config/clear", methods=["POST"])
@_locked
def clear_config():
    """恢复默认：只删 current.json（命名快照保留），前端回到初始状态。"""
    path = _config_path()
    removed = os.path.exists(path)
    if removed:
        try:
            os.remove(path)
        except OSError:
            return jsonify({"error": "清除失败，请重试"}), 500
    return jsonify({"success": True, "removed": removed,
                    "slots_kept": len(_list_config_slot_ids())})


@app.route("/api/reset", methods=["POST"])
@_locked
def reset_project():
    """Clear current work image and watermark, reset frontend state.

    注意：**不动 brands/、fonts/ 与 configs/** —— 品牌贴图、字体、调好的配置都是长期资产，
    只由它们自己的「删除/恢复默认」清掉（配置那边的按钮在「配置」卡片里）。
    """
    for name in ("original.jpg", "working.png", _LEGACY_WM_NAME, _SOURCE_NAME):
        path = os.path.join(UPLOAD_DIR, name)
        if os.path.exists(path):
            os.remove(path)
    # 多张图片水印（watermark_<id>.png）同样清空
    for wm_id in _list_watermark_ids():
        path = _watermark_path(wm_id)
        if path and os.path.exists(path):
            os.remove(path)
    _clear_watermark_cache()
    _clear_preview_base()
    _clear_exif_bar_cache()
    return jsonify({"success": True, "brands_kept": len(_list_brand_ids()),
                    "fonts_kept": len(_list_font_ids()),
                    "config_kept": os.path.exists(_config_path()),
                    "config_slots_kept": len(_list_config_slot_ids())})


@app.route("/api/watermark/preview", methods=["POST"])
@_locked
def watermark_preview():
    data = request.json or {}

    base = _preview_base()
    if base is None:
        return jsonify({"error": "请先上传图片"}), 400

    base_bytes, _, _ = base
    img = Image.open(io.BytesIO(base_bytes)).convert("RGBA")
    applied = {}
    result = apply_watermark(img, data, info=applied)
    if result is None:
        return jsonify({"error": "请先导入水印图片"}), 400

    buf = io.BytesIO()
    _flatten_rgb(result).save(buf, "JPEG", quality=PREVIEW_JPEG_QUALITY)
    # applied 回显本次真正生效的参数：出现「预览没变化」时可一眼分辨是前端没发对还是后端没画
    return jsonify({"preview": _data_url(buf.getvalue(), "image/jpeg"), "applied": applied})


_EXPORT_FORMATS = ("jpeg", "png", "original")
_EXPORT_DEFAULT_QUALITY = 95


def _read_source_info():
    """读取上传时记录的原始格式信息（source.json）；缺失返回 {}。"""
    path = os.path.join(UPLOAD_DIR, _SOURCE_NAME)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as sf:
            data = json.load(sf)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _export_plan_for(fmt, source_format=None):
    """导出格式规划：返回 (Pillow 格式名, 扩展名, 是否保留 alpha)。

    - jpeg（默认）：兼容老前端，与历史行为一致
    - png：无损、保留透明通道
    - original：跟随上传时的格式；HEIC/RAW 这类 Pillow 存不了的自动回退 JPEG

    source_format 只有 original 用到：交互路径传 source.json 记的上传格式，
    批量生成传**该输入文件自己的**格式（一个文件夹里 jpg/png 混着也不会串味）。
    """
    fmt = str(fmt or "jpeg").strip().lower()
    if fmt not in _EXPORT_FORMATS:
        fmt = "jpeg"
    if fmt == "original":
        fmt = str(source_format or "jpeg").strip().lower()
    if fmt in ("jpg", "jpe"):
        fmt = "jpeg"
    if fmt == "png":
        return "png", ".png", True
    if fmt == "jpeg":
        return "jpeg", ".jpg", False
    try:  # 其它格式：先试能不能编码，不能再退 JPEG（不能让用户拿到空文件）
        Image.new("RGB", (1, 1)).save(io.BytesIO(), fmt.upper())
    except Exception:
        return "jpeg", ".jpg", False
    return fmt, "." + fmt, True


def _export_plan(fmt):
    """单张导出（交互路径）：original 跟随本次上传的格式（uploads/source.json）。"""
    return _export_plan_for(fmt, _read_source_info().get("format"))


def _save_output(buf, image, fmt, quality, exif_bytes):
    """编码导出：JPEG 应用 quality，PNG 无损但同样写入 EXIF。"""
    kwargs = {"exif": exif_bytes} if exif_bytes else {}
    if fmt == "jpeg":
        kwargs["quality"] = quality
    image.save(buf, fmt.upper(), **kwargs)


@app.route("/api/watermark/download", methods=["POST"])
@_locked
def watermark_download():
    data = request.json or {}

    working_path = os.path.join(UPLOAD_DIR, "working.png")
    if not os.path.exists(working_path):
        return jsonify({"error": "请先上传图片"}), 400

    img = Image.open(working_path).convert("RGBA")
    result = apply_watermark(img, data)
    if result is None:
        return jsonify({"error": "请先导入水印图片"}), 400

    # 导出选项：format=jpeg|png|original（默认 jpeg 与原文件名，老前端完全不感知）
    fmt, ext, keep_alpha = _export_plan(data.get("format"))
    quality = int(_clamp(_safe_int(data.get("quality", _EXPORT_DEFAULT_QUALITY), _EXPORT_DEFAULT_QUALITY), 1, 100))

    # 导出时保留（可能已被编辑的）EXIF：相机参数是水印成片的一部分，不能丢
    original_path = os.path.join(UPLOAD_DIR, _ORIGINAL_NAME)
    exif_bytes = None
    if os.path.exists(original_path):
        try:
            with open(original_path, "rb") as f:
                exif_bytes = _canonical_exif_for_save(f.read())
        except Exception:
            exif_bytes = None

    output = result if keep_alpha else _flatten_rgb(result)
    buf = io.BytesIO()
    try:
        _save_output(buf, output, fmt, quality, exif_bytes)
    except Exception:
        fmt, ext = "jpeg", ".jpg"
        buf = io.BytesIO()
        _save_output(buf, _flatten_rgb(result), fmt, quality, exif_bytes)
    buf.seek(0)

    mime = {"jpeg": "image/jpeg", "png": "image/png"}.get(fmt, f"image/{fmt}")
    return send_file(buf, mimetype=mime, as_attachment=True,
                     download_name=f"watermarked_image{ext}")


if __name__ == "__main__":
    port = int(os.environ.get("FLASK_PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
