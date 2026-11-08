# dsh-camera-watermark

> 仓库只包含代码、模板和脚本。品牌贴图、参考照片、用户上传图片、本地配置和水印示例均未包含；`engine/uploads/`、`brands/`、`fonts/`、`configs/` 只保留 `.gitkeep` 占位。

把工作区里的「相机水印 / 图像 EXIF 相机参数编辑器」封装成 DeepSeek Harness 插件。

- **Host 插件**：注册 6 个模型可调用工具（模板、单张、批量、EXIF 读/写、工作台）。
- **引擎原样保留**：`engine/` 是原 Flask 应用（`app.py`、`scripts/batch_core.py`、`presets.json`、模板与资源）。插件不重写渲染逻辑。
- **所见即所得**：单张/批量都走 `batch_core.render_one()`，与工作台「下载图片」是同一条权威合成路径。
- **数据独立**：插件有自己的 `engine/uploads/`、`engine/brands/`、`engine/fonts/`、`engine/configs/`；仓库里这些目录只有 `.gitkeep`，首次使用/上传时由应用自动创建内容。
- **零 npm 依赖**：Host 代码只用 Node 内置模块，避免「从工作区目录安装的插件解析不到 DSH 内部包」的问题。

## 工具

| 工具 | 用途 |
| --- | --- |
| `camera_watermark_presets` | 列出模板；传 `id` 查看完整参数表与当前配置 |
| `camera_watermark_apply` | 单张出片：`input` + 可选 `preset`/`params`；默认跟随当前配置 |
| `camera_watermark_batch` | 文件夹批量：`inputs`、`out`、`suffix`、`dry_run`、`force` |
| `camera_watermark_exif_read` | 读 EXIF，返回可编辑字段名与值 |
| `camera_watermark_exif_write` | 改 EXIF（优先只替换 APP1；容器不允许时回退 JPEG 并告警） |
| `camera_watermark_studio` | 启动/停止/查询原交互式工作台（Flask） |

单张/批量都不需要启动 Flask。需要拖拽调参、实时预览时再调用 `camera_watermark_studio`，把返回的 URL 给用户。

## 侧栏入口

插件在 Web 侧栏底部的操作区注册了一个 **相机水印** 按钮（`sidebar.footer.action`）：

- 侧栏展开时显示图标 + 名称，收起时只显示相机图标，悬停有提示。
- 点击后调用 Host 的 `POST /camera-watermark/studio`，由插件启动（或复用）Flask 工作台，再在新标签页打开返回的 `http://127.0.0.1:<studioPort>`。
- 不需要先手动启动工作台；首次点击会等 Flask 就绪（最多 20 秒）。
- 浏览器若拦截新标签页，会把地址复制到剪贴板并弹窗提示。

按钮只负责打开交互式工作台；单张/批量/EXIF 仍然通过上面的工具完成，不经过浏览器。

### 调用示例

```text
camera_watermark_apply
  input:  C:\photos\DSC01234.jpg
  preset: camera_bar_brand_logo
  quality: 92
```

```text
camera_watermark_apply
  input:  C:\photos\DSC01234.jpg
  preset: photo_frame_brand
  params: { "frame_gap_ratio": 0.05, "title_text": "DSH STUDIO" }
```

```text
camera_watermark_batch
  inputs: ["C:\photos\2026-08-08"]
  out:    "C:\photos\out"
  preset: blur_backdrop_brand
  dry_run: true
```

`params` 的键就是 `camera_watermark_presets(id=...)` 返回的 `params` 键。`config` 支持 `current`、命名快照 id，或导出的 `.json` 绝对路径。

## 安装

在 DSH 里用 `plugin_manager`：

```text
action: install_bundle
target: <此目录的绝对路径>
```

安装后 bundle 会写进当前 profile 的 bundle 列表并插入 `camera-watermark` 插件行；新的工具在下一次模型步骤可见。升级本目录里的 JS/Python 后，`remove_bundle` 再 `install_bundle` 可加载新代码（替换已安装包需要重新加载模块）。

## 配置

`cordis.patch.yml` 的默认值：

```yaml
pythonPath: ''        # 空 = 依次探测 python、py -3
studioPort: 5000
timeoutSeconds: 300   # 单次工具调用的 Python 超时
```

机器上有多个 Python 时把 `pythonPath` 指到带 flask/Pillow/piexif 的那个解释器。插件通过「退出码」探测解释器，不依赖管道 stdio，因此在受限沙箱里也能工作。

## 依赖

Python 3 + `flask==3.1.1`、`Pillow==11.2.1`、`piexif==1.1.3`（见 `engine/requirements.txt`）。

```powershell
python -m pip install -r engine\requirements.txt
```

## 数据与升级

- `engine/` 内是代码与数据。升级原应用时，用新代码覆盖 `engine/` 中的同名文件，保留 `engine/configs/`、`engine/brands/`、`engine/fonts/`、`engine/uploads/`。
- 工作台与工具读写同一份 `engine/configs/current.json`：在工作台里调好的样式，单张/批量工具默认直接复用。
- 图片水印引用的 `uploads/watermark_<id>.png` 与贴图 `brands/brand_<id>.png` 不会自动从旧目录同步；换机时把旧 `uploads/`、`brands/`、`fonts/` 放进 `engine/` 对应目录即可。

## 已验证

以下为开发阶段用本地样张做的验证记录；仓库不包含样张、输出图或示例水印。

在 DSH 会话里直接调用工具（不是只跑测试脚本）：

- `camera_watermark_presets`：列出 3 条模板与当前配置。
- `camera_watermark_apply`：`camera_bar_brand_logo` 信息栏出片成功，四段文字回显正确；`photo_frame_brand` + `params`（`frame_gap_ratio`、`title_text`）出片为 6054×5889，画框与文字正确。
- `camera_watermark_batch`：两张 5504×4128 样张套 `photo_frame_brand`，2/2 成功；逐张保留 EXIF。
- `camera_watermark_exif_read`：读取 51 个可编辑字段与四段信息栏文本。
- `camera_watermark_exif_write`：写入「相机型号 / ISO感光度」后读回正确；样张 APP1 结构触发重编码回退并在结果中告警。
- `camera_watermark_studio`：启动/查询/停止 Flask 工作台，端口与进程由插件管理。
- Client 半区：`client.js` 注册 `sidebar.footer.action`，mock DOM 点击后成功打开工作台 URL；Host 侧 `POST /camera-watermark/studio` 返回 200 与 URL，`GET` 返回 405。
- 侧栏按钮的**最终视觉效果**需要在浏览器刷新后确认（Host 安装与 Client 模块注册已通过静态检查/合约测试；本环境没有浏览器控制）。

## 说明

- 插件由 Host 半区（工具 + Flask 进程 + `/camera-watermark/studio` 路由）和 Client 半区（侧栏按钮）组成；Client 代码只用浏览器模块表里的 React 与 `--dsw-alias-*` 主题变量，不导入 DSH Client 内部包。
- 工作台保持独立标签页打开，避免在插件页面里嵌 iframe（DSH 的页面规范不推荐 iframe，主题与语言无法继承）。
- 工具返回文件路径与摘要，不把整张图塞进会话；需要展示成品时用 `present` 工具传输出文件。
- 单张输出默认在原图旁生成 `<原名>_wm.<ext>`；已存在时自动追加 `_2`，不会覆盖原图。批量默认 `suffix=_wm`，已存在的成品会跳过（`force=true` 才覆盖）。

## 作者

Yakistory — https://github.com/Yakistory

## 来源

相机水印引擎（`engine/`）最初来自 Coze 项目 `image-exif-watermark-tool` v0.0.35（2026）。
DSH 适配层、Python 桥接、6 个模型工具与 Web 侧栏入口由 Yakistory 编写。

## 许可证

MIT，见 [LICENSE](LICENSE) 与 [NOTICE](NOTICE)。仓库不包含第三方图片、品牌贴图、用户上传图片或本地配置。

## 致谢

- DeepSeek Harness 插件 API（MIT）
- Flask（BSD-3-Clause）、Pillow（HPND）、piexif（MIT）
- 客户端入口参考 DSH 官方装饰插件模板的写法
