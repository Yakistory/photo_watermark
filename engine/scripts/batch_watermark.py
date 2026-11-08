"""批量水印命令行：给一批图片（或文件夹）+ 一份配置，逐张出片。

用法::

    python scripts/batch_watermark.py "相册" --current --out "相册/_带水印"
    python scripts/batch_watermark.py "相册" --config configs/current.json --format png
    python scripts/batch_watermark.py a.jpg b.jpg --slot 我的风格 --suffix _logo -r --force
    python scripts/batch_watermark.py "相册" --dry-run            # 只渲染不写盘

参数从哪来（默认 --current）：
    --current          页面上自动保存的那份（configs/current.json）
    --config PATH      任意 .json（含界面「导出配置」下来的文件）
    --slot ID          命名快照（configs/<id>.json）

输出：
    --out DIR           输出文件夹（默认：写回每张图旁边）
    --suffix _wm        输出文件名后缀（默认 _wm）
    --format/--quality  默认取配置里存的那份；original = 跟随每张图自己的格式
    -r/--recursive      递归子目录（有 --out 时保留相对子目录结构）
    --force             已存在就覆盖（默认跳过 ⇒ 重跑安全）
    --dry-run           只渲染不写盘（提前暴露坏图 / 缺图片水印）
    --report PATH       写报告 JSON（逐张 src/dest/失败原因/回显）
    --quiet             只打汇总与失败项

退出码：0 全部成功 · 1 部分失败 · 2 参数/配置错（没开跑）· 3 一张都没成。

**不会动 uploads/**（图片水印、品牌贴图、字体都是只读引用），也不会覆盖原图
（后缀为空且指回输入目录会直接拒绝）。
"""
import argparse
import os
import sys

# Windows 控制台默认 GBK：路径里有生僻字/符号时别在打印时炸掉
try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import batch_core as BC  # noqa: E402


def build_parser():
    parser = argparse.ArgumentParser(
        prog="batch_watermark.py",
        description="批量水印：用界面里调好的配置给一批图片逐张出片",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("inputs", nargs="+", metavar="图片或文件夹",
                        help="要处理的图片文件或文件夹（可给多个）")
    source = parser.add_argument_group("配置来源（默认 --current）")
    source.add_argument("--current", action="store_true",
                        help="用页面上自动保存的那份（configs/current.json）")
    source.add_argument("--config", metavar="PATH", help="用任意 .json 配置（含导出文件）")
    source.add_argument("--slot", metavar="ID", help="用命名快照 configs/<id>.json")
    out = parser.add_argument_group("输出")
    out.add_argument("--out", metavar="DIR", help="输出文件夹（默认写回输入图旁边）")
    out.add_argument("--suffix", default=BC.DEFAULT_SUFFIX, metavar="TXT",
                     help=f"输出文件名后缀（默认 {BC.DEFAULT_SUFFIX}）")
    out.add_argument("--format", default="", choices=["", "jpeg", "png", "original"],
                     help="导出格式（默认用配置里那份）")
    out.add_argument("--quality", type=int, default=None, metavar="1-100",
                     help="JPEG 质量（默认用配置里那份）")
    out.add_argument("-r", "--recursive", action="store_true",
                     help="递归子目录（给 --out 时保留相对子目录结构）")
    out.add_argument("--force", action="store_true",
                     help="已存在就覆盖（默认跳过已存在的结果）")
    out.add_argument("--dry-run", action="store_true",
                     help="只试运行：照常渲染但不写盘（提前暴露坏图 / 缺图片水印）")
    out.add_argument("--report", metavar="PATH", help="写报告 JSON（逐张结果）")
    out.add_argument("--quiet", action="store_true", help="只打汇总与失败项")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.config and args.slot:
        print("错误：--config 与 --slot 只能给一个", file=sys.stderr)
        return 2

    if args.config or args.slot:
        loaded = BC.load_config(args.config, args.slot)
    else:
        loaded = BC.load_config()
    print(f"配置：{BC.describe_config(loaded)}")
    if loaded.get("ok"):
        print(f"       {loaded['path']}")
    else:
        print(f"错误：{loaded.get('error')}", file=sys.stderr)
        return 2

    notes = BC.config_reference_notes(loaded)
    for level, text in notes:
        if level != "info":
            print(f"  [{'错误' if level == 'error' else '提醒'}] {text}")

    options = BC.build_options(args.inputs, args.out or "", args.suffix, args.force,
                               args.format, args.quality, args.recursive, args.dry_run)
    scan = BC.scan_for(options)
    plan_notes = BC.validate_plan(scan, loaded, options)
    for level, text in plan_notes:
        mark = {"error": "错误", "warning": "提醒"}.get(level)
        print(f"  [{mark}] {text}" if mark else f"  {text}")
    if any(level == "error" for level, _text in plan_notes):
        print("未开始：请先解决上面的错误", file=sys.stderr)
        return 2

    def on_event(kind, payload):
        if kind == "item" and not args.quiet:
            print("  " + BC.format_item_line(payload, scan["stats"]["images"]))
        elif kind == "item" and payload.get("error"):
            print("  " + BC.format_item_line(payload, scan["stats"]["images"]))

    print(f"\n开始生成（{len(scan['files'])} 张）…")
    summary = BC.run_batch(scan, loaded, options, on_event=on_event)

    print("\n===== 汇总 =====")
    for line in BC.format_summary_lines(summary):
        print("  " + line)
    if args.report:
        ok, error = BC.write_report(summary, args.report)
        print(f"  报告：{args.report}" if ok else f"  报告写入失败：{error}")
    elif not summary.get("failed"):
        print(f"  提示：加 --report 报告.json 可以留下逐张记录"
              f"（默认报告名 {BC.REPORT_NAME}）")
    code = BC.exit_code(summary, loaded)
    if code:
        print(f"  退出码 {code}" + {1: "（部分图片失败）", 3: "（一张都没成）"}.get(code, ""))
    return code


if __name__ == "__main__":
    sys.exit(main())
