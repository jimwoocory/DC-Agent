# AI → CDR 自动转换器

这是一个 Python 总控工具。Illustrator 负责正确读取 AI 私有数据并生成无颜色转换、无降采样的 PDF；CorelDRAW 通过内部 JavaScript API 的 `OpenDocument` 和 `SaveAs(..., cdrCDR)` 写出真正的 CDR。Python 不伪造 CDR，只负责 NAS 队列、文件校验和调度。工具不读取或写入系统剪贴板，也不依赖在线转换网站。

## 公司使用流程

mini4 常驻运行转换服务，员工从飞书小助手进入 H5 工具：

1. 在飞书小助手的“办公”菜单打开“AI转CDR”。
2. 一次选择一份或多份 `.ai`；如需指定印刷数值，逐份填写“源 CMYK → 目标 CMYK”。
3. H5 把整批文件和颜色配置原子写入 NAS，mini4 按队列顺序调用 Illustrator 和 CorelDRAW。
4. H5 显示每份文件和整批的百分比、阶段、已用时间和预计剩余时间。
5. 完成后可在 H5 下载 CDR；原始 AI、颜色校准、桥接 PDF、预览图和 JSON 报告自动留档。

员工不需要挂载 NAS、远程控制 mini4或复制粘贴文件。同一台 mini4 不并行驱动多个
Illustrator/CorelDRAW 窗口；批量任务采用单 Worker 顺序处理，避免文档和脚本互相串线。

正式后台链路是 `AI → PDF 矢量桥接 → CDR → 重新打开校验`。PDF 是后台中间结果，
设计员不用手工操作，但可在 `outbox/bridge_pdf/` 和任务报告中查看。当前流程不经过
CVG 或 SVG。

## 命令行用法

在安装了 Adobe Illustrator 2026 和 CorelDRAW 2026 的 Mac 上运行：

```bash
python3 ai_cdr_converter.py init --root "$HOME/AI-CDR"
python3 ai_cdr_converter.py once --root "$HOME/AI-CDR" --dry-run
python3 ai_cdr_converter.py once --root "$HOME/AI-CDR"
```

把 `.ai` 文件放入 `~/AI-CDR/inbox/`。成功后会得到：

- `outbox/同名.cdr`：转换结果
- `outbox/originals/`：原始 AI 文件
- `outbox/bridge_pdf/`：Illustrator 生成的中转 PDF
- `outbox/previews/`：CorelDRAW 重新打开 CDR 后生成的非空预览
- `reports/`：每个任务的 JSON 记录
- `failed/`：失败任务、原稿和诊断文件

队列根目录可以直接写成已经挂载的 NAS 路径，例如：

```bash
python3 ai_cdr_converter.py init --root "/Volumes/设计文件/AI-CDR"
```

NAS只保存队列和最终文件。程序认领任务后，会先校验并复制到 mini4 的
`~/Library/Caches/com.dianchi.ai-cdr-converter/work/`，Illustrator 和
CorelDRAW只操作这个本地副本。CDR和中转PDF完整生成后，程序先以隐藏的
`.uploading` 文件传回 NAS，确认大小后再一次性改成正式文件名。因此网络中断时，
同事不会把半个 CDR 当成成功结果。

共享目录中的状态如下：

- `inbox/`：同事上传 AI 的入口
- `processing/`：mini4 已认领、正在转换的任务
- `outbox/`：已经完整上传的 CDR
- `failed/`：失败原稿、本地工作文件和错误信息
- `reports/`：每个任务的 JSON 结果
- `color_profiles/`：设计员载入并通过格式校验的 ICC/ICM 文件
- `progress/`：H5 读取的每份任务阶段、百分比、耗时和错误快照

## 安装与常驻后台

安装器把固定签名的 `AI-CDR-Converter.app` 安装到 `/Applications`，并建立登录后自动运行的服务。第一次安装需要在 macOS“隐私与安全性 → 辅助功能”中允许 `/Applications/AI-CDR-Converter.app`；Illustrator 首次被控制时还需允许一次“自动化”权限。后续使用同一签名更新时无需重复授权。

手动安装服务的命令：

```bash
python3 ai_cdr_converter.py install-service --root "$HOME/AI-CDR"
```

服务会持续监控 `inbox/`。运行日志保存在 mini4 本机的 `~/Library/Logs/AI-CDR-Converter/service.stdout.log` 和 `service.stderr.log`，不会把频繁写入的日志放到 NAS。

macOS 版 CorelDRAW 没有用于外部直接执行 JavaScript 文件的命令行入口，因此固定签名的转换程序只使用原生辅助功能 API 打开“脚本”面板并双击固定 Worker。真正的 PDF 打开、ICC 嵌入和 CDR 写入全部在 CorelDRAW 内部 API 中完成，不使用“另存为”键盘输入。

## 颜色处理

默认配置执行以下处理：

- Illustrator 打开原始 AI，而不是直接读取可能为空白的 PDF 兼容页。
- PDF 导出使用 `ColorConversion.None`，不转换 RGB/CMYK 数值。
- 保留文档现有 ICC 配置，不重新指定目标色彩空间。
- 彩色、灰度和单色图像全部禁止降采样。
- 嵌入外链图像，避免 mini4 找不到原电脑上的链接文件。
- 文字默认转曲，避免 CorelDRAW 缺字体后替换字体。

### 设计员手动 CMYK 校准

飞书 H5 可以为批量中的每份 AI 建立同名 `.color.json` 配置，也可以把当前规则复制到
整批文件。每条规则包含：

- 原稿中要查找的 CMYK 数值；
- 希望写入 PDF 和 CDR 的目标 CMYK 数值；
- 只处理填充、只处理描边，或两者都处理。

校准在 Illustrator 导出 PDF 前执行，不会保存或覆盖原 AI。后台会记录每条规则实际
命中的对象数量；任何规则命中为 0 时任务会进入 `failed/`，避免错误地显示“校准成功”。
颜色配置随原稿一起归档，最终报告记录规则数量和命中明细。

界面同时预留“载入 ICC 颜色文件”区域。当前版本会检查文件是否含 ICC 标准签名，
复制到 `color_profiles/` 并记录 SHA-256，供印刷设备、纸张和颜色策略核对。ICC 状态为
`registered_only`：尚未确认具体输出设备策略前，不自动改变文档颜色空间，避免载入一个
不合适的 ICC 后让全稿发生不可控偏色。手动 CMYK 数值映射不受此限制，会按规则生效。

这些参数在队列根目录的 `config.json` 中可调整。该流程避免主动转换颜色数值，但显示器、CorelDRAW 颜色管理设置、输出设备和纸张仍会影响视觉结果；正式印刷前应统一 ICC 配置，并以中转 PDF、CDR 和实物打样做最终核对。

## 状态和排错

```bash
python3 ai_cdr_converter.py status --root "$HOME/AI-CDR"
```

如果任务进入 `failed/`，先查看对应 `reports/*.json` 的 `error` 字段。一次性权限问题通常是尚未允许 Illustrator 自动化，或未把固定路径 `/Applications/AI-CDR-Converter.app` 加入“辅助功能”。
