---
name: android-game-autotest
description: 用 adb 驱动安卓真机，在**不改动游戏客户端**的前提下跑 Unity/团结引擎游戏的实机流程回归：前置体检、截图与模板/OCR 判定、JSON 步骤流编排、崩溃日志按类聚合、出 report.json / index.html / junit.xml。适用于商店包、审核包、热更包这类不能改包的验证，以及启动冒烟、登录流程、关键界面回归、卡死与崩溃取证。触发词：实机跑测、adb 跑测、真机自动化、跑测流程、doctor 体检、selftest 自检、游戏卡死检测、UI 自动化回归、安卓冒烟测试。
---

# 安卓游戏实机跑测（adb 方案）

进程外跑测：只依赖 `adb` 二进制，对**任何已安装的 APK** 都能跑，不需要改客户端、不需要 Development 构建、不需要注入 SDK。

前置认知（数据线、Hub、授权这些坑）：**USB 集线器是掉线第一嫌疑人，尽量直连主机**；设备从 `adb devices` 消失但 `ioreg` 里还在，通常是链路抖了，等 60 秒会自行重新枚举，别急着 kill-server。性能采样是另一件事，本 skill 只做流程回归。

## 何时用 / 不用

**用**：

- 商店包、审核包、热更包这类**不能改包**的验证 —— 项目内的 `GameAutoTest233` 这类进程内框架要求 Development 构建 + 专用宏，这类包用不了
- 启动冒烟：能不能启动、能不能过登录、有没有崩、卡在哪个界面
- 上线前回归：关键界面截图对比、主流程是否走得通
- 崩溃 / ANR 现场取证：日志按类聚合 + 逐步截图

**不用**：

- 要断言游戏内部状态（血量、背包、任务进度）—— 这需要进程内框架或协议层
- 要读 Unity 画面里的 UI 控件树 —— 见下方能力边界，做不到
- 要识别任意中文文字 —— OCR 只认你自己 harvest 过的字形

## 能力边界（实测，先读完再决定方案）

| 能做 | 不能做 |
| --- | --- |
| 坐标点击/滑动/按键；原生弹窗按文字点 | 读 Unity 画面里的 UI 控件树（SurfaceView 在 uiautomator 里 **0 节点**） |
| 截图 + 帧差 + 模板匹配 + 数字 OCR 判定画面 | 识别任意中文文字（OCR 需自建字形模板） |
| 抓 logcat 并按类聚合崩溃 | 断言游戏内部状态（血量/背包/任务进度） |
| 全流程留证（每步截图 + 事件流 + JUnit） | 中文输入（`input text` 仅 ASCII） |

**最重要的一条**：Unity 画面在 `uiautomator dump` 里是 0 节点，所以**游戏层只能靠「坐标点击 + 截图判定」**；
但**原生层（协议弹窗、权限弹窗、登录页）对象树完全可见**，能按文字精确点。
混合策略：原生层用文字定位，游戏层用坐标 + 图像。

## 工具包

```
安卓实机跑测/
├── SKILL.md
├── requirements.txt   Python 依赖（numpy / Pillow 浮动下限）
├── scripts/
│   ├── adbkit.py     设备驱动层：连接、截图、点击、uiautomator、logcat、崩溃识别、自愈
│   ├── vision.py     图像层：Frame、灰度、帧差、dhash、模板匹配、DigitOCR
│   ├── runner.py     JSON 步骤流引擎 + 报告产出
│   ├── doctor.py     前置体检：一条命令判断「这台机器 + 这台手机现在能不能开跑」
│   ├── selftest.py   驱动层自检：22 个用例，改完代码跑它验回归
│   └── new_flow.py   从模板生成流程 JSON（填包名即可）
├── flows/
│   ├── smoke.json              启动冒烟模板（含 __PKG__ / __ACTIVITY__ 占位符）
│   └── examples/               实战流程样例（某 Unity 手游的登录/新号引导/主城巡检/广度巡检）
├── assets/           放 templates/ 参考截图与 ocr/ 字形模板（首次使用是空的）
├── references/
│   ├── flow-schema.md   流程 JSON 完整格式与 32 个 action 参数表
│   └── troubleshooting.md 已踩过的坑与判读口径
└── runs/             跑测产物（已 gitignore）
```

依赖：**Python 3.13+**、`numpy`、`Pillow`、`adb`。没有 cv2 / scipy 依赖。

版本下限钉在 3.13 的理由：3.9 已于 2025-10-31 EOL、3.10 已于 2026-10-01 EOL；且 3.9/3.10 上
pip 只能解析到 numpy 2.0.x / Pillow 11.3，拿不到新版。代码本身在 3.9–3.15 全都能跑（无 3.10+ 独占
语法、无 numpy 2.0 移除别名、无已删 stdlib 模块），所以这个下限是政策选择而非技术限制 —— 要放宽
随时可以，但别低于受支持版本。3.13 支持到 2029-10、3.14 到 2030-10、3.15 到 2031-10。

```powershell
python -m pip install -r requirements.txt
$env:ADB = "$env:LOCALAPPDATA\Android\Sdk\platform-tools\adb.exe"   # 不在 PATH 时才需要
```

POSIX 下同样：`python3 -m pip install -r requirements.txt`、`export ADB=~/Library/Android/sdk/platform-tools/adb`。

`requirements.txt` 钉的是浮动下限：`numpy>=2.3,<3`、`pillow>=12,<14`（不是死锁版本）。下限是
**实测过的最低可工作组合**（numpy 2.3.5 + Pillow 12.3.0 在 3.13 上跑通全部离线用例与 5 个流程校验）；
实际装出来通常是 numpy 2.5.5 + Pillow 13.0.0。上限用 `<3` / `<14` 是为了挡住未来的大版本破坏性变更。
`adbkit` 会按 `ADB` → `ANDROID_HOME`/`ANDROID_SDK_ROOT` → 各平台常见安装位置 → PATH 的顺序自己找，
找不到会明确报错，不会假装成功。

## 作业顺序

### 0. 确认设备在线

```powershell
adb devices -l
```

看到 `<序列号>  device  usb:...` 才算在线。`offline` / 空列表 → 拔插 USB、看手机上有没有授权弹窗、
必要时 `adb kill-server; adb start-server`。

### 1. 前置体检（每次开跑前必做）

```powershell
python scripts/doctor.py --pkg com.example.game     # 查包是否已装
python scripts/doctor.py                            # 不查包，只体检链路
python scripts/doctor.py --json                     # 给 agent 解析
```

每条检查给 `OK / WARN / FAIL` 加可执行 hint，**有 FAIL 就不要开跑**。典型 FAIL 与处置：

| 检查项 | FAIL 含义 | 处置 |
| --- | --- | --- |
| 设备 | 没连上 / offline | 拔插、授权、`adb reconnect` |
| 屏幕唤醒 | 息屏了 | 工具会自动唤醒；持续失败查 `stay_on_while_plugged_in` |
| 锁屏 | 有密码锁 | `--pin` 传 PIN，或在流程里用 `unlock` 步骤 |
| 截图链路 | 连续超时 | **USB 传输层卡死**是最常见故障，换线/换口/直连不用 Hub |
| 目标应用 | 没装 | `adb install -r -t <apk>`（45MB 包经 USB 2.0 Hub 约 1.5 分钟） |

### 2. 生成流程 JSON

先拿模板填包名：

```powershell
python scripts/new_flow.py --pkg com.example.game
python scripts/new_flow.py --pkg com.example.game --activity com.example.game.MainActivity --out local/flows/login.json
```

生成的 `local/flows/<包名>.smoke.json` 做的是：唤醒 → 标记日志 → 强停 → 拉起 → 等画面稳定 → 截图 →
扫崩溃 → 存日志 → 收尾截图。够做启动冒烟。

生成的流程落在 `local/flows/`（`flows/` 只放随技能分发的模板，本地产物不进版本库）。
要跑更长的流程就往 `steps` 里加动作。**完整 action 表与参数见 [references/flow-schema.md](references/flow-schema.md)**，
新增步骤后一定先 `--dry-run`（写错参数名不会报错，只是不生效）。

一个典型的多步流程长这样：

```json
{
  "name": "登录流程冒烟",
  "vars": {"PKG": "com.example.game"},
  "device": {"stay_awake": true, "unlock": true},
  "steps": [
    {"id": "wake",   "action": "wake", "screenshot": false},
    {"id": "mark",   "action": "logcat_mark", "screenshot": false},
    {"id": "stop",   "action": "force_stop", "package": "com.example.game", "screenshot": false},
    {"id": "launch", "action": "launch", "package": "com.example.game",
                     "expect_activity": "com.example.game", "timeout": 30},
    {"id": "stable", "action": "wait_stable", "timeout": 90, "settle": 3.0},
    {"id": "agree",  "action": "wait_text", "text": "同意", "tap": true, "timeout": 10,
                     "must_find": false, "message": "有协议弹窗就点掉"},
    {"id": "login",  "action": "wait_template", "template": "templates/login_page.png",
                     "threshold": 0.85, "timeout": 30},
    {"id": "crash",  "action": "check_crash", "must_clean": true},
    {"id": "log",    "action": "dump_logcat", "name": "full", "screenshot": false}
  ]
}
```

### 3. 先 dry-run，再真跑

```powershell
python scripts/runner.py flows/xxx.json --dry-run    # 只校验结构，不连设备
python scripts/runner.py flows/xxx.json              # 真跑
python scripts/runner.py flows/xxx.json --json       # 末尾输出一行 JSON 摘要，给 agent 解析
```

`--serial` 指定设备（多设备必填），`--out` 改产物目录。

**默认不中断**：某步失败会继续跑完剩余步骤，最后一次性出报告 —— 一次跑测发现多个问题。
只有标了 `"fatal": true` 的步骤失败才中断。

### 4. 读报告

产物在 `runs/<时间戳>/`：

| 文件 | 内容 |
| --- | --- |
| `report.json` | 机器可读汇总：`total/pass/fail/error/skip/crashed/crash_events/artifacts` |
| `index.html` | 人看的报告（步骤表 + 截图 + 崩溃归类） |
| `junit.xml` | 接 CI |
| `events.jsonl` | 每步一行事件流 |
| `flow.json` | 本次流程快照，报告自解释 |
| `NN-<step-id>-after.png` | 每步截图 |
| `logcat-full.log` / `crash-logcat.log` | 日志原文 / 崩溃窗口日志 |

**`crashed` 与 `passed` 是两件事**：步骤可以全 pass 但 `crashed: true`（流程走通了，但期间有崩溃）。
两个都要看。

## 判定画面的正确姿势

Unity 画面只能靠图像判定。**关键教训：永远按区域判定，不要看全屏。**

真实案例：某游戏 Loading 页背景云层一直在飘，全屏帧差恒为 `0.4281`（看起来"一直在动"），
但进度条区域帧差恒为 `0.0000`（实际卡死）。看全屏会得出完全相反的结论。

```json
{"action": "wait_stuck", "samples": 6, "interval": 1.0,
 "box": [0.03, 0.825, 0.94, 0.05], "must_stuck": true}
```

`box` 是 `[左, 上, 宽, 高]` 的**屏幕比例**（0–1）。

**帧差阈值参考**（1080x2408 实测）：静止 `0.000`，动画中块均值 `0.022–0.029`，全屏均值 `0.05`。
阈值 `0.005–0.01` 能区分"变了/没变"。

**dhash** 适合整屏签名（对背景云层飘动不敏感），判断"是不是同一个界面"。
**模板匹配**：参考图放 `assets/templates/`，`match_template` 返回 score + 坐标，自匹配 score≈1.0；跨分辨率要先调 `scale`。
**数字 OCR**：先对准数字区域 `ocr_harvest` 一次，之后就能反复读（见 flow-schema）。

## 点界面：五条必须记住的实测结论

Unity 画面只能靠坐标点，所以「点得准不准」直接决定流程真假。以下都是踩出来的：

**1. 别用视觉估算坐标，用颜色/模板算真实中心。**
实测角色选择页的「确定」按钮，肉眼看在 `y≈0.725`，按那里点三次毫无反应；
用颜色定位（橙红底 `R>150, 60<G<140, B<90`）量出真实范围 `y 1793–1905`，
中心 `(0.4991, 0.7677)`，一按就进。**估算偏 100+ px 是常态。**
工具里用 `tap_color`（预设 `red/orange/white/gold/green/blue`）或 `find_color_blob`。
`box` 必须给——全图找同色元素会把大片背景误聚成一簇。

**2. 面板的关闭控件各不相同，没有通用返回键。**
实测某游戏 7 个主城入口：`KEYCODE_BACK` **一个都关不掉**；右上红 X 只对 4 个有效；
左上 `<` 箭头只对 2 个有效；还有 1 个要用左上白色返回文字。
所以用 `close_panel`：它按「红X色块 → 右上X坐标 → 左上箭头 → 左上返回文字 → 浮层外空白」
轮换试，**每轮都重新截图用目标模板验证是否真的回来了**，不靠"点了就当成功"。
模态浮层（点货币弹出的「获取途径」）连 BACK/ESC/点遮罩左右都无效，只有点浮层上方空白能关。

**3. 判定"面板打开了"要用负向断言。**
点入口没生效时（点偏了、被浮层挡住），`close_panel` 会**一开始就匹配主城 → 直接判成功**，
整条流程静默假绿。所以每个入口后面必须跟 `assert_gone`（断言主城模板消失）。
这个坑真实发生过：设置入口的截图明明还是主城，报告却显示通过。

**4. 引导浮层是延迟弹出的，不能"查一次没有就当没有"。**
进主城后新手引导浮层要等 1~2 秒才弹出来，然后**拦截所有点击**。
立刻截图查红 X 会查不到 → 跳过 → 后面 8 个入口全部点空。
用 `dismiss_tutorial`（轮询等待浮层出现、点掉、再确认没重弹），不要用一次性的 `tap_color`。

**5. 带背景动画的画面，模板得分会天然抖动，单帧判定不可靠。**
实测同一主城画面连拍 12 帧，同一模板得分 **min 0.858 / max 0.963**，3/12 帧低于 0.90。
只看一帧会把动画低谷误判成「画面变了」。`assert_gone` / `close_panel` 都做了
**连拍 N 帧取最大值**再比阈值（`samples` 参数，默认 3）。

## 崩溃识别

工具包已把日志解析成「按类聚合 + 次数」，报告里每类只留首条样本和 `count`。

- 具体错误模式排在通用兜底之前，否则全被归到「引擎错误日志」
- 团结引擎的 tag 是 **`Tuanjie`** 不是 `Unity`，格式是 `E Tuanjie :`（空格不是斜杠）
- `logcat -t N` 在部分设备上是「最近 N 毫秒」而不是「最近 N 行」，会让崩溃检测**静默漏报** ——
  工具包已改为整份 `logcat -d -v epoch` 后本地截断

细节与另外几个坑见 [references/troubleshooting.md](references/troubleshooting.md)。

## 改完工具包必须回归

```powershell
python scripts/selftest.py                 # 22 个用例，全绿才算没改坏
python scripts/selftest.py --skip-slow     # 跳过耗时用例，快速冒烟
python scripts/selftest.py --pkg com.example.game   # 顺带验「已安装检测」用例
```

自检覆盖：shell（含 `grep` 无匹配不抛异常的回归）、屏幕尺寸、raw/PNG 截图、帧解码、帧差、区域裁剪、
模板自匹配、**NCC 得分边界（纯色画面不得假匹配）**、**颜色定位色块中心**、OCR 模板、uiautomator 解析、
Activity 检测、应用安装检测、点击/按键/滑动、logcat 全量、logcat 时间窗口、崩溃聚合、自愈参数、流程 dry-run。

**vision.py 的 API 形态（易错）**：`Frame` 字段是 `width/height/pixels/meta`（不是 `.w/.h`）；
`dhash` / `mean_abs_diff` / `match_template` 是**模块级函数**，不是 Frame 的方法；
`Device.swipe` 的时长参数叫 `ms` 不是 `duration`。

## 判读与处置

- **不要自动改基准图**。视觉断言失败先人工确认是「功能坏了」还是「美术改版了」。
- `clear_data` 会清账号态，跑测前想清楚是否需要保留登录状态。
- 长跑测前确认电量（doctor 查，<20% WARN）与温度（>42°C WARN，降频会影响时序）。
- `runs/` 每次 1–2MB（截图 + 日志），定期清理旧目录。
- 崩溃归类只是**线索**，最终定性要人看日志上下文与截图。

## 交付

- 报告：`runs/<时间戳>/index.html`（人看）与 `report.json`（机器读），两者一起给。
- 结论要写清「哪一步、什么现象、证据文件是哪个」，不要只说「跑挂了」。
- 复现：把这次用的 `flow.json` 与包体版本（`versionName` / 构建时间）一起记下来，否则结论不可复现。
