# skill233-android-phone-debug

> 让 AI Agent 用 `adb` 驱动 Android 真机跑流程回归——不改包、不注入 SDK、不依赖 Android Studio。

一套面向 **Unity / 团结引擎手游** 的实机跑测工具包：前置体检、截图与模板/颜色/OCR 判定、JSON 步骤流编排、崩溃日志按类聚合，产出 `report.json` / `index.html` / `junit.xml`。

适配对象是**商店包、审核包、热更包**这类「拿不到源码、也不能改包」的验证场景。

---

## 为什么是进程外方案

Android UI 自动化常见三条路，各有硬伤：

| 方案 | 问题 |
|---|---|
| Appium / uiautomator2 | 依赖原生控件树。Unity 整个游戏画面是**一块 SurfaceView**，控件树里只有一个节点，所有按钮都点不到 |
| 客户端注入（SDK 埋点） | 要改包重新构建。审核包、商店包根本不能改 |
| **本方案：进程外 `adb`** | 只依赖 `adb` 二进制。截图 → 图像识别定位 → `input tap` 下发。对任何已安装 APK 都能跑 |

代价是「点得准不准」全靠图像识别，所以本工具包把大量精力放在**判定可靠性**上（见下文「踩过的坑」）。

## 目录结构

```
skill233-android-phone-debug/
└── skills/
    └── android-game-autotest/
        ├── SKILL.md                    Agent 使用入口：何时用、作业顺序、判读口径
        ├── scripts/
        │   ├── adbkit.py               设备驱动层：连接、截图、点击、uiautomator、logcat、崩溃识别、自愈
        │   ├── vision.py               图像层：Frame、帧差、dhash、模板匹配、颜色定位、DigitOCR
        │   ├── runner.py               JSON 步骤流引擎 + 报告产出（32 个 action）
        │   ├── doctor.py               前置体检：一条命令判断「这台机器 + 这台手机现在能不能开跑」
        │   ├── selftest.py             驱动层自检：22 个用例，改完代码跑它验回归
        │   └── new_flow.py             从模板生成流程 JSON（填包名即可）
        ├── flows/
        │   ├── smoke.json              启动冒烟模板（含 __PKG__ / __ACTIVITY__ 占位符）
        │   └── examples/               实战流程样例：登录 / 新号引导 / 主城巡检 / 广度巡检
        ├── assets/
        │   └── templates/              参考截图（模板匹配用）
        └── references/
            ├── flow-schema.md          流程 JSON 完整格式与 32 个 action 参数表
            └── troubleshooting.md      已踩过的坑与判读口径（26 条）
```

`runs/`（跑测产物）与 `local/`（生成的流程）已 gitignore，不进版本库。

## 安装

```bash
git clone https://github.com/neko233-com/skill233-android-phone-debug.git
```

把 `skills/android-game-autotest/` 整个目录复制到 Agent 的技能目录（**整目录复制**，否则 `references/` 的相对链接会断）：

```bash
# DeepSeek Harness
cp -r skill233-android-phone-debug/skills/android-game-autotest ~/.dsh/skills/

# Claude Code
cp -r skill233-android-phone-debug/skills/android-game-autotest ~/.claude/skills/

# OpenCode / MiMoCode
cp -r skill233-android-phone-debug/skills/android-game-autotest ~/.config/opencode/skills/
```

依赖：Python 3.9+、`numpy`（`vision.py` 用到）、`adb`（Platform Tools）。不需要 Node，不需要 Appium，不需要 Android Studio。

## 快速开始

```bash
cd ~/.dsh/skills/android-game-autotest

# 1. 体检：这台机器 + 这台手机现在能不能开跑
python scripts/doctor.py --pkg com.example.game

# 2. 自检：确认工具包本身没坏（不依赖真机，约 10s）
python scripts/selftest.py --skip-slow

# 3. 生成一条冒烟流程
python scripts/new_flow.py --pkg com.example.game \
    --activity com.example.game.MainActivity \
    --out local/flows/smoke.json

# 4. 跑
python scripts/runner.py local/flows/smoke.json
```

跑完在 `runs/<时间戳>/` 拿到 `report.json`、`index.html`（浏览器看）、`junit.xml`（CI 用）和逐步截图。

## 流程 JSON 长什么样

一条流程就是一串 JSON 步骤，`ratio` 用 0–1 的屏幕比例，换分辨率不用改流程：

```json
{
  "name": "登录到主城",
  "vars": { "ACCOUNT": "test_account" },
  "steps": [
    { "id": "wake", "action": "wake" },
    { "id": "ime-off", "action": "ime", "mode": "off" },
    { "id": "stop", "action": "force_stop", "package": "com.example.game" },
    { "id": "launch", "action": "launch", "package": "com.example.game", "timeout": 60 },
    { "id": "wait-login", "action": "wait_template",
      "template": "templates/login_start_btn.png", "timeout": 90 },
    { "id": "tap-start", "action": "wait_template",
      "template": "templates/login_start_btn.png", "tap": true },
    { "id": "wait-maincity", "action": "wait_template",
      "template": "templates/maincity_action_btn.png", "timeout": 120 },
    { "id": "check", "action": "check_crash", "must_clean": true }
  ]
}
```

完整 32 个 action 与参数见 [`references/flow-schema.md`](skills/android-game-autotest/references/flow-schema.md)。

## 踩过的坑（这部分才是价值所在）

在一个真实手游项目的主城巡检上修掉的问题——**都是会让人得出错误结论的坑**：

**1. 别用视觉估算坐标，用颜色/模板算真实中心。**
实测角色选择页的「确定」按钮，肉眼看在 `y≈0.725`，按那里点三次毫无反应；用颜色定位量出真实范围 `y 1793–1905`，中心 `(0.4991, 0.7677)`，一按就进。**估算偏 100+ px 是常态**，因为面板实际内容区常比截图上看起来偏下。

**2. 面板的关闭控件各不相同，没有通用返回键。**
实测某游戏 7 个主城入口：`KEYCODE_BACK` **一个都关不掉**；右上红 X 只对 4 个有效；左上箭头只对 2 个有效；还有 1 个要用左上白色返回文字。所以工具里是 `close_panel`：按「红X色块 → 右上X坐标 → 左上箭头 → 左上返回文字 → 浮层外空白」轮换试，**每轮都重新截图验证是否真的回来了**。

**3. 判定「面板打开了」必须用负向断言。**
点入口没生效时（点偏、被浮层挡住），`close_panel` 会**一开始就匹配主城 → 直接判成功**，整条流程静默假绿。真实发生过：设置入口的截图明明还是主城，报告却显示通过。所以每个入口后面必须跟 `assert_gone`。

**4. 引导浮层是延迟弹出的，不能「查一次没有就当没有」。**
进主城后新手引导浮层要等 1~2 秒才弹，然后**拦截所有点击**。立刻截图查会查不到 → 跳过 → 后面 8 个入口全部点空。

**5. 带背景动画的画面，模板得分天然抖动，单帧判定不可靠。**
同一主城画面连拍 12 帧，同一模板得分 **min 0.858 / max 0.963**，3/12 帧低于 0.90 阈值。只看一帧会把动画低谷误判成「画面变了」。判定类动作默认连拍 N 帧取**最大值**。

**6. 崩溃退回桌面 ≠ 面板打开。**
进程闪退后主城模板得分同样掉到 0.29，只看分数会把闪退误报成「面板打开了」，然后继续往系统桌面上点。所以 `probe_entry` 会**先查 `pidof` 进程存活再判画面**。

**7. USB 链路抖动会返回 0 字节或截断的截图。**
内核日志刷 `babble error` 时，`screencap` 可能返回 0 字节，或返回一个长度不是 0 但不够 `16 + w*h*4` 的**截断帧**——只校验 `len > 16` 会漏过去，解码出一张撕裂图，后续模板匹配全乱。所以要按 `w/h` 算出完整帧应有的字节数再校验。

**8. 中文输入法会污染 `input text`。**
实测 `autotest233` 被敲成「啊去to'te'st」。跑测前必须 `ime disable` 切到 ASCII。

**9. 日志里「错误」有三种，不能混一个桶。**
真崩溃（`Fatal signal` / `stack corruption`）、待修缺陷（客户端该改但不崩）、良性告警（引擎序列化布局之类）。判成崩溃 → 正常跑测假红且打断后续步骤；判成良性 → 漏报。所以工具分三个桶，跑测照常判绿，缺陷单独列出交客户端。

**10. 一条真实错误能刷出 24 条假崩溃。**
`GameLogger` 打一条 Error 后把调用栈逐行跟出，每行都带 `E Tuanjie :` 前缀，会被通用兜底规则当成新的独立崩溃。必须识别栈续行（`#00 pc 0000000000051698`、`at Foo.Bar()`、`--- End of stack trace`）并跳过。

## 已验证范围

在一个真实 Unity/团结引擎手游（Android 12，il2cpp，arm64-v8a，1080×2408）上实测：

- 登录到主城流程 15/15 通过
- 新号引导流程（含分支自适应）15/15 通过
- 主城 8 入口巡检 44/44 通过
- 主城广度巡检（8 入口 + 面板内部 + 任务系统双页签）50 步 49 pass / 1 skip
- 驱动层自检 22 个用例全绿

## 能力边界

**能做**：进程外黑盒跑测、图像定位、流程编排、崩溃取证、报告产出。

**不能做**：
- 读不到游戏内逻辑状态（血量、金币数值只能靠 OCR，且只在画面稳定时可靠）
- 点不中纯动态生成且无固定特征的控件（没有稳定颜色/形状可匹配）
- 绕过锁屏、支付验证、验证码或任何访问控制

## 安全边界

- 仅操作你拥有或已获授权的设备
- `pm clear`（清数据）、`uninstall`、`reboot` 属破坏性操作，流程里需显式声明
- 日志与截图可能含隐私信息，导出前先脱敏
- 不尝试绕过锁屏、支付验证、验证码或任何访问控制

## 许可证

MIT
