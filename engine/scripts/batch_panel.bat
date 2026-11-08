@echo off
rem 批量水印小窗：双击即可（在窗口里选输入文件夹 / 配置 / 输出文件夹）
rem 命令行批量请用 batch_watermark.py；本文件只负责开窗
chcp 65001 >nul
cd /d "%~dp0.."
python "%~dp0batch_panel.py" %*
if errorlevel 1 pause
