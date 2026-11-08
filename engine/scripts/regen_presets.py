"""一次性脚本：把 presets.json 与 app.py 的内置模板对齐（v12）。

只做四件事：更新 _readme、把 camera_bar_brand_logo 回退成 v10 形态、
补上 photo_frame_brand、删掉除此之外的模板。跑完即可删除。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app  # noqa: E402

PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "presets.json")

README = [
    "水印模板 = 数据，不是代码。改完保存即生效（后端按 mtime 缓存，不必重启服务）。",
    "几何量统一用『占照片宽度的比例』，所以 1600px 预览与全尺寸导出必然等比一致。",
    "cap_ratio 是『文字墨迹高度 / 照片宽度』的实测值——参照图就是这么量出来的（scripts/check_bar_geometry.py）。",
    "数值控件的 step 由后端按显示小数位下发（float=0.01）；滑块网格必须与数字框小数位一致，否则手输会被浏览器吸附。",
    "字体参数（font_family / {行}_font）的值 = 系统字体 id 或 fonts/ 里自己上传的字体 id，留空=自动挑体。",
    "extends 继承父模板：子模板只写差异，params 会深合并、controls 缺省继承（父模板被删掉，子模板会掉回默认值）。",
    "controls 决定前端显示哪些旋钮（顺序=显示顺序，group=分组标题），留空则不带旋钮。",
    "params 里出现白名单之外的键会被丢弃，并在 /api/watermark/presets 的 invalid_params 里回显。",
    "新参数必须同时写进 params 与 app.py 的 _PRESET_PARAM_SPECS：控件初值读的是 params（缺键的 float 会显示量程下限）。",
    "两种模板：kind=exif_bar（信息栏：照片下方追加一条参数带，照片与白条一起出片）/ kind=photo_frame（画框水印：照片四周留白、底边空白里写文字，拍立得风）。",
    "画框水印的几何：frame_* 管留白与文字区（frame_gap_ratio=四边留白宽度、frame_bottom_extra_ratio=底边文字区高度），{行}_* 管主标题/品牌/参数/日期四行（对齐只有 left/center/right，默认居中）。",
]


def entry(preset_id):
    raw = app._DEFAULT_PRESETS[preset_id]
    return {
        "name": raw["name"],
        "kind": raw["kind"],
        "description": raw["description"],
        "params": dict(raw["params"]),
        "controls": [dict(c) for c in raw["controls"]],
    }


def main():
    with open(PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    data["_readme"] = README
    keep = {"_readme"} | set(app._DEFAULT_PRESETS)
    for key in list(data):
        if key not in keep:
            del data[key]
    for preset_id in app._DEFAULT_PRESETS:
        data[preset_id] = entry(preset_id)
    with open(PATH, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    print("written", PATH, "presets:", [k for k in sorted(data) if k != "_readme"])
    for preset_id in sorted(app._DEFAULT_PRESETS):
        clean, dropped = app._sanitize_preset_params(app._DEFAULT_PRESETS[preset_id]["kind"],
                                                     data[preset_id]["params"])
        print(preset_id, "params:", len(clean), "dropped:", dropped)


if __name__ == "__main__":
    main()
