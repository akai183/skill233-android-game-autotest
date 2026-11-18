# assets

放两类东西，**都是你自己跑测时生成的**，仓库里不带任何项目的专用素材：

| 目录 | 内容 | 怎么来 |
| --- | --- | --- |
| `templates/` | 参考截图（PNG） | 手动截图后裁到目标元素，或直接 `adb exec-out screencap -p > x.png` |
| `ocr/` | 数字字形模板（`.npz`） | `ocr_harvest` 动作从实机画面切出来，`DigitOCR.save()` 存 |

流程里的 `template` / `save` 参数写相对名（如 `templates/login_page.png`、`ocr/pct.npz`），
引擎会自动拼到本目录。

**不要提交大图**：参考图只截目标元素那一小块，整屏 PNG 动辄 3MB。
只保留跑测确实要用的几张，其余靠 `runs/` 里的截图留档（`runs/` 已 gitignore）。
