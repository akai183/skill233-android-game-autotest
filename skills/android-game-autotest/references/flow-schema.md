# 流程 JSON 格式

一份流程就是一次跑测的完整描述：设备怎么准备、按顺序做哪些事、怎么判定、留什么证据。
引擎是 `scripts/runner.py`，它不认识业务，只认识 `action`。

## 顶层结构

```json
{
  "name": "登录冒烟",
  "description": "拉起游戏，走到登录页，确认没有崩溃",
  "vars": {"PKG": "com.example.game"},
  "device": {"stay_awake": true, "unlock": true},
  "steps": []
}
```

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `name` | 是 | 报告标题 |
| `description` | 否 | 报告副标题，写清这次要验证什么 |
| `vars` | 否 | 初始变量。变量可被 `set` / `ocr` 覆盖，供 `assert` 的表达式使用 |
| `device` | 否 | `stay_awake`（默认 true，跑测期间保持亮屏）、`unlock`（默认 true，开跑前自动解锁） |
| `steps` | 是 | 步骤数组，按顺序执行 |

引擎在 `setup()` 阶段自动注入 `DEVICE_SERIAL`、`SCREEN_W`、`SCREEN_H` 三个变量。

## 步骤通用字段

```json
{"id": "launch", "action": "launch", "package": "com.example.game",
 "screenshot": true, "message": "已拉起", "fatal": false, "when": "SCREEN_W > 1000"}
```

| 字段 | 说明 |
| --- | --- |
| `id` | 步骤标识，报告和截图文件名用它。不写则用 `step<序号>` |
| `action` | 动作名，见下表。写错会在 dry-run 阶段报「未知 action」 |
| `screenshot` | 是否自动截图，默认 `true`。高频动作（`sleep`/`key`/`logcat_mark`）建议设 false |
| `before_shot` | 默认 false；设 true 会在动作**执行前**也截一张 |
| `message` | 成功时在报告里显示的话 |
| `fatal` | 默认 false。设 true 时本步失败会中断后续步骤 |
| `when` | 表达式，为假则跳过本步 |

**失败语义**：默认所有步骤都会执行完，失败被收集起来一次性出报告 —— 一次跑测发现多个问题，
而不是第一个问题就中断。只有 `fatal: true` 才中断。

## action 全表

参数名为实测值。**写错参数名不会报错，只是不生效**，所以新增流程后务必先 `--dry-run` 再真跑。

### 设备与环境

| action | 参数 | 用途 |
| --- | --- | --- |
| `wake` | – | 唤醒屏幕（已亮则不发命令） |
| `unlock` | `pin`、`must_unlock`（默认 true） | 解锁；`must_unlock` 为 true 时解锁失败即判失败 |
| `sleep` | `seconds`（默认 1.0） | 等待 |
| `launch` | `package`、`activity`、`expect_activity`、`timeout`（默认 30）、`wait` | 启动应用。不传 `activity` 时走 `monkey` 拉 LAUNCHER，最稳；`expect_activity` 是「启动后应出现的 Activity 名（子串）」 |
| `force_stop` | `package` | 强停应用（保留数据） |
| `clear_data` | `package` | 清应用数据（**不可逆**，会掉登录态） |

### 输入

| action | 参数 | 用途 |
| --- | --- | --- |
| `tap` | `ratio: [rx, ry]` 或 `x`、`y` | 点击。`ratio` 是 0–1 的屏幕比例，换分辨率不用改流程 |
| `swipe` | `from_ratio`/`to_ratio`（或 `x1 y1 x2 y2`）、`ms`（默认 300） | 滑动。注意时长参数叫 `ms`，不是 `duration` |
| `key` | `codes: ["KEYCODE_BACK"]` | 按键，可传多个，按顺序发 |
| `text` | `text`、`clear`、`clear_times`（默认 40） | 输入 **ASCII**；中文不可用（`input text` 限制）。`clear: true` 会先移到末尾再连按 DEL 清空 |
| `tap_color` | `color`（必填）、`box`、`tolerance`、`min_pixels`（60）、`tap`（true）、`must_find`（true） | **按颜色找控件再点**。`color` 可用预设名 `red/orange/white/gold/green/blue` 或 `[R,G,B]`。`box` 必须给，否则大片同色背景会被误聚成一簇 |
| `probe_entry` | `ratio` 或 `x`/`y`、`wait`（1.8）、`expect`、`package`、`threshold`（0.85）、`must_open`（true）、`samples`（2）、`scale`（2） | 点入口并**区分三种结果**：面板打开 / 没反应 / 进程崩溃。先查 `pidof` 再判画面——崩溃退回桌面时主城得分同样掉到 0.29，普通 `tap` 会把闪退误报成「面板打开了」。`must_open` 为 true 时「没反应」也判失败 |
| `ime` | `mode`（`off`/`on`/`list`/`reset`，默认 `off`）、`ime_id` | 输入法控制。中文输入法会把 `input text` 的 ASCII 变成拼音组词（实测 `autotest233` → 「啊去to'te'st」），必须 `mode: off` 切掉 |

### 等待与判定

| action | 参数 | 用途 |
| --- | --- | --- |
| `wait_activity` | `contains`、`timeout`（默认 30） | 等前台 Activity 名包含指定子串 |
| `wait_stable` | `timeout`（60）、`settle`（2.0）、`interval`（0.8）、`threshold`（0.01）、`must_stable` | 等画面静止，用于「等加载/等动画结束」 |
| `wait_change` | `timeout`（30）、`interval`（0.6）、`threshold`（0.01）、`box`、`must_change`（默认 true） | 等画面变化，用于「点完确认有反应」 |
| `wait_stuck` | `samples`（6）、`interval`（1.0）、`box`、`must_stuck`、`must_move` | 判定某区域卡死不动 / 或必须还在动 |
| `wait_template` | `template`、`threshold`（0.85）、`timeout`（30）、`interval`（0.8）、`scale`（2）、`box`、`tap`、`must_find`（默认 true） | 等模板图出现；`tap: true` 命中后顺手点它的中心 |
| `wait_text` | `text`、`exact`、`timeout`（15）、`tap`、`must_find` | 等**原生**控件文字出现（Unity 画面无效） |
| `screenshot` | `name` | 留证截图，存 `runs/<时间戳>/<name>.png` |
| `assert_gone` | `template`（必填）、`threshold`（0.90）、`box`、`scale`（2）、`timeout`（8）、`interval`（0.6）、`samples`（3） | **负向断言**：断言模板图**不在**画面上。用于「点入口后面板确实打开了」。不加这个，点偏时 `close_panel` 会因「一开始就匹配主城」假通过 |
| `probe_template` | `template`、`threshold`（0.85）、`box`、`scale`（2）、`save`（默认 `PRESENT`） | 只看一帧判断模板在不在（不等），结果写 `vars[save]` 与 `vars[save+"_score"]`。适合画面已稳定时做分支 |
| `dismiss_tutorial` | `color`（red）、`box`、`min_pixels`（300）、`timeout`（12）、`interval`（0.8）、`max_taps`（3） | **轮询**等待引导浮层出现并点掉，再确认没重弹。浮层是延迟 1~2 秒才弹的，用一次性的 `tap_color` 会漏 |
| `close_panel` | `expect`（必填，目标画面模板）、`threshold`（0.90）、`scale`（2）、`timeout`（20）、`interval`（0.8）、`samples`（3）、`x_box`、`color`、`x_ratio`、`arrow_ratio`、`back_text_ratio`、`outside_ratio` | 关掉当前面板直到回到 `expect` 画面。按「红X色块 → 右上X坐标 → 左上箭头 → 左上返回文字 → 浮层外空白」**轮换尝试，每轮都重新截图验证**。Unity 面板的关闭控件各不相同，`KEYCODE_BACK` 实测一个都关不掉 |

`box` 一律是 `[左, 上, 宽, 高]` 的**屏幕比例**（0–1），不是像素。

**判定类动作的 `samples`**：带背景动画的画面，同一模板单帧得分会在 0.86–0.96 之间抖
（实测某主城 12 帧里 3 帧低于 0.90）。`assert_gone` / `close_panel` 默认连拍 3 帧取**最大值**再比阈值，
避免把动画低谷当成「画面变了」。

### 断言与变量

| action | 参数 | 用途 |
| --- | --- | --- |
| `assert` | `expr`、`message` | 断言表达式为真，假则本步 fail |
| `set` | `name`、`expr` 或 `value` | 设置变量 |

`expr` 支持：变量名、数字、字符串、比较（`== != > >= < <=`）、布尔（`and or not`）、
算术（`+ - * /`）、`in`、`len/str/int/float/abs/round/min/max/all/any/bool`、
以及 `.lower()/.upper()/.startswith()/.endswith()`。**不能调任意函数**。

### 日志与证据

| action | 参数 | 用途 |
| --- | --- | --- |
| `logcat_mark` | – | 标记日志时间点。后续 `check_crash` / `dump_logcat` 只看这个点之后的日志 |
| `check_crash` | `must_clean`（默认 true） | 扫标记以来的日志并按类聚合。`must_clean: true` 时发现任何异常即 fail；想「记录但不判失败」就设 false |
| `dump_ui` | `name` | 存一份 uiautomator XML |
| `dump_logcat` | `name` | 存日志原文到 `runs/<时间戳>/<name>.log` |

### OCR

| action | 参数 | 用途 |
| --- | --- | --- |
| `ocr_harvest` | `text`、`box`、`save` | 从当前画面把已知文本切成字形，存成 npz 模板（**自举**） |
| `ocr` | `template`、`box`、`threshold`（0.72）、`save`、`must_match` | 用模板读数字。结果写入 `vars[save]`（字符串）与 `vars[save + "_num"]`（第一个数字） |

数字 OCR 的正确用法是先 harvest 一次、之后反复读：

```json
{"id": "harvest", "action": "ocr_harvest", "text": "95%", "save": "ocr/pct.npz",
 "box": [0.43, 0.842, 0.16, 0.02]},
{"id": "read", "action": "ocr", "template": "ocr/pct.npz",
 "box": [0.43, 0.842, 0.16, 0.02], "save": "PROGRESS"},
{"id": "check", "action": "assert", "expr": "PROGRESS_num == 95"}
```

**区域框要精确**：太松会把相邻元素切进来，太紧会切掉字符边缘，两种都会让 `harvested=0`。

## 资源路径

`template` / `save` 这类资源参数按以下顺序解析：

1. 绝对路径 → 直接用
2. 相对当前工作目录存在 → 用绝对化的它
3. 否则拼到 `assets/` 下（`assets/templates/xxx.png`、`assets/ocr/xxx.npz`）

## 最小可跑例子

`flows/smoke.json` 是模板（含 `__PKG__` / `__ACTIVITY__` 占位符），用生成器填：

```bash
python scripts/new_flow.py --pkg com.example.game
python scripts/runner.py local/flows/com.example.game.smoke.json --dry-run
python scripts/runner.py local/flows/com.example.game.smoke.json
```

## 产物

```
runs/<YYYYmmdd-HHMMSS>/
├── report.json          机器可读汇总
├── index.html           人看的报告
├── junit.xml            接 CI
├── events.jsonl         每步一行事件流
├── flow.json            本次流程快照（报告自解释）
├── run.log              控制台日志
├── NN-<id>-after.png    每步截图
├── logcat-full.log      日志原文（dump_logcat 产出）
└── crash-logcat.log     崩溃窗口日志（check_crash 产出）
```

`report.json` 关键字段：`total / pass / fail / error / skip / crashed / crash_events / fatal / passed`。

**`crashed` 与 `passed` 是两件事**：步骤可以全 pass 但 `crashed: true`（流程走通了，期间有崩溃）。
两个都要看。
