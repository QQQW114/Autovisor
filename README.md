##  Autovisor

**Github项目主页：**[CXRunfree/Autovisor](https://github.com/CXRunfree/Autovisor)

---

# ⚠️ 本仓库是一个 Fork —— 请先读这一节

| | |
|---|---|
| **Fork 自** | [CXRunfree/Autovisor](https://github.com/CXRunfree/Autovisor) |
| **上游许可** | MIT License |
| **基线版本** | v3.18.4（2026-09-24） |
| **上游主页** | https://github.com/CXRunfree/Autovisor |

上游是个成熟项目（持续维护 3 年多，900+ stars），**本分支只做加法，不删改上游行为**。
所有改动都用 `[本地改动]` 注释标出，便于和上游对比。

## 本分支做了什么

上游遇到课中的人机验证时，只能**停下等你手动完成**。
本分支补上了**自动作答**：先自己解（最多 3 次），解不出来才转人工。

因为用的是**纯本地图像处理**，所以：

- 不需要任何 API key
- 不会把验证码图片传到第三方打码平台

## 快速开始（从零部署到能跑）

### 1. 准备环境

需要 **Python 3.13**（推荐用 [uv](https://github.com/astral-sh/uv) 管理环境）。

```bash
git clone https://github.com/QQQW114/Autovisor.git
cd Autovisor

uv venv .venv --python 3.13
uv pip install --python .venv/Scripts/python.exe httpx pillow playwright pygetwindow requests numpy opencv-python ddddocr
.venv/Scripts/python.exe -m playwright install msedge
```

> 不用 uv 也行：把 `uv venv .venv --python 3.13` 换成 `python -m venv .venv`，
> 把 `uv pip install --python <路径>` 换成 `.venv/Scripts/pip install` 即可。
>
> `ddddocr` 是本分支新增的依赖（验证码识别用），上游不需要。

### 2. 首次运行

**Windows**：双击 `启动.bat`
**macOS**：`./run_macos.sh`

程序在进入主流程前会先跑一次**配置向导**，问你两件事：

```
填写账号信息
  学校全称（例：XX职业技术学院）:          ← 学号登录才问
  智慧树账号（手机号 / 邮箱 / 纯学号）:
  密码:                                    ← 输入时不回显

填写课程链接
  课程播放页地址（浏览器打开课程后复制地址栏整条链接）:
```

**课程链接怎么拿**：
浏览器登录智慧树 → 「我的学堂」→ 点开课程 → 点进任意一集视频的**播放页** →
复制地址栏**整条**链接。

必须是能直接看到**视频播放器**的页面。不要填课程介绍页 / 学堂首页，
否则程序找不到课程目录。

填完写进 `config.ini`，以后不再问。已有的值不会被覆盖，只补空缺项。

### 3. 确认跑起来了

日志在 `logs/` 最新那个 `.txt`。正常会滚动出现：

```
正在学习:1.1.1、xxx
课时结果 | 序号=1/52 完成=True 平台进度=100%
正在学习:1.1.2、xxx
```

看到 `异常脚本` / `关闭插件` / `课程锁定` 立即停程序。

> ⚠️ **不要关闭、也不要最小化**浏览器窗口（最小化会暂停渲染，连播放都会停）。
> 想后台挂机就把 `config.ini` 里 `enableHideWindow` 改成 `True`（移出屏幕，不是最小化）。

### 4. 验证码与弹题怎么处理

不用管，程序会自己处理：

| 情况 | 程序行为 | 什么时候需要你 |
|---|---|---|
| 登录滑块 | 自动过（`enableAutoCaptcha = True`） | 过不去时改成 `manualLogin = True` 手动拖一次 |
| 课中弹题 | 自动答题 + 自动关闭 | 不需要 |
| 课中人机验证 | 自动作答，**最多 3 次** | 3 次没过会停下等你，完成后自动恢复 |

需要你动手的唯一场景：日志出现 `自动作答未成功，请手动完成验证...`。

## 换课程 / 换账号

**方式一：双击 `换课程.bat`**（最省事）

```
[1] Change course URL
[2] Change account
[3] Change both
[0] Exit
```

**方式二：命令行**

```bash
python setup_wizard.py --course     # 换课程
python setup_wizard.py --account    # 换账号
python setup_wizard.py --all        # 两个都重填
python setup_wizard.py --show       # 只看当前配置（账号打码显示）
```

**方式三：直接改 `config.ini`**

```ini
[course-url]
URL1 = https://studyvideoh5.zhihuishu.com/stuStudy?recruitAndCourseId=xxxxxxxx
URL2 =
URL3 =
URLn =
```

**要跑多门课**：依次填 `URL1`、`URL2`、`URL3`…
程序会**一门一门串行**跑完（不是同时开）。跑完一门自动切下一门。

未用完的行留空即可（启动时每个空行会打一条"不是一个有效网址"，忽略它）。

> **改完必须重启程序才生效。** 另外注意 `config.ini` 已被 `.gitignore` 排除，
> 不会被提交到仓库。

## 支持哪些题面

平台的课中验证是腾讯 tcaptcha 点选类，实测有 6 种题面：

| 题面示例 | 类型 |
|---|---|
| `请点击灰色小写n` | 直接识别 |
| `请点击黄色大写X` | 直接识别（颜色限定目标） |
| `请点击数字5朝向一样的大写K` | 朝向比对（参照 vs 候选） |
| `请点击小写b颜色一样的大写R` | 颜色比对 |
| `请点击正向的小写u` | 姿态筛选（选最正立的） |
| `请点击侧向的数字3` | 姿态筛选（选最歪的） |

## 怎么解的

```
1. 按颜色分离字符   题里每个字符颜色不同，比形态学分割可靠得多
2. ddddocr 逐字识别
   ⚠️ 绝对不能调 set_ranges()，实测一调识别就全空
   ⚠️ 3D 立体字符用「原彩色图放大 4 倍」送 OCR 最稳
3. 姿态判定用模板旋转匹配   把字符转一圈，找与正立模板最吻合的角度
   （光看"倾斜量"不够：实测侧向字符能转 ~90°，而倾斜量仍只有几度）
4. 按题面判据挑候选（朝向差 / 色相差 / 姿态极值）
```

## 安全护栏

```
最多 3 次，超过就停下等人工
没把握就放弃，绝不猜 —— 连续答错会触发平台风控
置信度不足（形状分<0.45 / 角度差>22° / 区分度<6°）一律拒答
点击用拟人轨迹（多步移动 + 随机抖动 + 随机间隔）
```

设计原则是**宁可不做也不做错**：拒答只是转人工，猜错会消耗验证机会。

## 相对上游的改动清单

| # | 文件 | 改动 |
|---|---|---|
| 1 | `modules/captcha_solver.py` | **新增** —— 验证码求解器（本地 CV） |
| 2 | `modules/tasks.py` | 验证码流程重写：自动作答 + 严格可见性判定 + 拟人化点击 |
| 3 | `modules/login.py` | **新增学号登录通道**（上游只支持手机号/邮箱） |
| 4 | `Autovisor.py` | 学号登录分支、固定 profile、可配代理、开放调试端口 |
| 5 | `setup_account.py` | **新增** —— 首次运行引导，账号为空时交互询问 |
| 6 | `modules/configs.py` | 新增配置项 `login_mode` / `school` / `manualLogin` / `proxyServer` |
| 7 | `modules/installer.py` | 放宽依赖版本校验（镜像源版本不符会反复重装并死锁） |
| 8 | `sweep_captcha.py`、`build_dataset.py`、`regress.py` | **新增** —— 验证码样本采集与离线回归工具 |

### 上游原有流程里的三个坑（本分支已修）

1. **课中弹题永远关不掉，程序卡死**（最严重）
   上游用 `page.wait_for_selector(".el-scrollbar__view")` 定位题目，但页面上有 **3 个**同名元素
   （课程目录侧栏也是 `el-scrollbar`），取到的是侧栏那个 → 在它里面找 `.number` 永远是 0
   → **从不答题**。而平台要求「未完成的弹题不能关闭」：未答题时点关闭会弹提示框，
   并把它的 `.v-modal` 遮罩压在弹题之上（弹题 z=2001，遮罩 z=2002）——
   此后**弹题里任何元素都点不动**，连它自己的关闭按钮也不行。另外
   `page.press(".el-dialog", "Escape")` 关弹窗实测也已失效。
   本分支改为：**清除遮挡 → 在 `.dialog-test` 作用域内答题 → 点「关闭」按钮**。

2. **`[id^=tcaptcha_transform]` 是常驻隐藏容器**（`opacity=0`、位置 `y=-1000000`）。
   Playwright 的 `is_visible()` 会把它判为"可见" → 误认为验证码一直在 → 程序卡死。
   必须用带 `opacity` + 视口位置检查的判定。

3. **验证控件消失 ≠ 页面已恢复**。过早放行会让主流程找不到 `video` 元素而崩溃。

## 已知能力边界

以下情况会**主动放弃、转人工**（是设计，不是故障）：

- 旋转后的字符被 OCR 误读（例：侧向的 `n` 被读成 `i`）
- `l` / `I` / `1` 这类本身难分的字符
- 参照与候选的姿态差异过小（区分度不足）
- 图中有多个同名候选、且题面没有颜色限定

离线回归现状：**12 个样本 → 9 个给出结论 / 0 异常 / 3 个拒答**。

## 验证码求解器的开发工具（可选）

在 `tools/captcha/` 下。这些不是运行必需，是给"想继续改进识别率"的人用的。

有验证码挂着的页面时，可以批量采集样本：

```bash
cd tools/captcha
python sweep_captcha.py 30     # 点验证码右上角"刷新"按钮换题，采 30 个
                               # 刷新只换题、不计失败，所以可以放心点
python build_dataset.py        # 从日志+截图重建「题目+图片」配对
python regress.py              # 跑离线回归，输出报告 + 每题标注图
```

产物落在 `verify_shots/`（已加入 `.gitignore`，不会误提交）。

离线回归现状：**12 个样本 → 9 个给出结论 / 0 异常 / 3 个拒答**。

> 注意：这几个脚本的路径是按项目根目录写的，
> 若移动位置需要同步改脚本里的 `RUN` 常量。

---

------
#### 2026/9/15 公告

感谢大家三年多以来的喜爱与支持~

准备读研, 本项目佛系更新

#### 2026/9/24 Autovisor-3.18.4 更新

**本次更新:**

- 新增融合共享课支持: 自动跳过 AI 随堂练习弹窗、展开折叠目录、关闭弹窗后恢复播放, 并读取课时真实学习进度 ([#158](https://github.com/CXRunfree/Autovisor/pull/158)).

**近期更新:**

- 修复课程页卡死: 已读的"学前必读"弹窗是隐藏节点, 等它可见会一直等下去, 现在超时即跳过 ([#155](https://github.com/CXRunfree/Autovisor/pull/155)).
- 课时切换、目录识别的等待也加上超时, 异常页面不再长时间卡住.
- 支持 Python 3.13.

------
#### 一、程序介绍

**项目简介:**

这是一个可无人监督的自动化程序, 基于微软的 Playwright 框架, 由 Python 和 JavaScript 编写而成. 核心原理是使用浏览器模拟用户操作.

**程序功能:**

- **支持自动登录**
- **自动通过登录滑块验证(可选)**
- **自动播放和切换下一集**
- **自动跳过弹窗和弹题**
- **自动静音、设置指定倍速**
- **自动检测暂停并自动续播**
- **支持智慧共享课、翻转课、融合共享课**
- **支持刷习惯分**
- **支持隐藏窗口后台刷课**
- 启动时自动检查更新
- 后台实时更新学习进度
- macOS 支持源码运行
- 根据当前时间自动设置背景色(白天/夜晚)
- 完成章节时提示已刷课时长
- 各种自定义配置

#### 二、发行版运行

1. 请确保系统为 Windows 10 及以上.

2. 发行版文件夹内自带 **config.ini** (可能没显示 `.ini` 后缀名), 使用 **文本编辑器** 打开.

3. 根据文件内的注释, 填写配置文件:

   - 默认启动系统自带的 Edge 浏览器;
   - 文件里的 **EXE_PATH 项** 用于自定义浏览器路径, 需要精确到**浏览器可执行文件的位置**, 可以保持默认不填;
   - 不知道浏览器的安装路径? 见下方 **五、常见问题**.
   - 确认 **保存修改** 后再退出.

   <p align="left"><img src="resources/markdown/config_detail.png" width="600" alt="config.ini 配置说明"></p>

4. 注意事项

   - **`config.ini` 配置项不需要加引号.**

   - **如果需要修改下载镜像源, 请编辑 `data/mirrors.json`**

5. 运行 **Autovisor.exe**, 会自动打开浏览器, 进入网课界面后就能自动刷课了!

   (如果未设置 **enableAutoCaptcha=True**, 则需要**手动完成**登录时的滑块验证)

**发行版目录说明:**

- `resources/`: 图片、脚本等程序资源.
- `packages/`: 自动下载的 NumPy、OpenCV 等运行时依赖.
- `data/`: 登录 Cookies 和镜像源配置.

#### 三、macOS 源码运行

需要 macOS 11 (Big Sur) 及以上 (Apple Silicon / Intel 均可) 和系统 Chrome浏览器. 先复制模板并填写配置:

```bash
cp config.macos.ini.example config.macos.ini
```

打开 `config.macos.ini`, 填入课程链接 (账号密码可留空, 用浏览器手动登录), 然后运行:

```bash
./run_macos.sh
```

首次运行会自动部署环境 (安装 `uv`、依赖和必要的浏览器), 之后直接启动. 环境或配置变化时会自动重新部署, 也可用 `./run_macos.sh --setup` 强制重建.

#### 四、发行版下载

- Github: [Releases · CXRunfree/Autovisor](https://github.com/CXRunfree/Autovisor/releases)
- 网盘备用: [蓝奏云 · Autovisor-for-windows](https://wwk.lanzouj.com/b05evsxif) 密码: 492l

这是已经打包好的程序, 若需要**源代码**请于 Github 项目主页下载.

#### 五、常见问题

1. 解压后没有 `Autovisor.exe` / 提示缺少依赖文件?

   - 多为杀毒软件误杀: 加入信任区后重新解压; 也请确认下载的是 Releases 里的发行包, 而不是源码.

2. 第一次启动很久没反应 / 依赖下载失败?

   - 首次会检查并下载运行时依赖 (numpy、opencv-python), 等后台日志跑完即可; 下载失败会自动换用 `data/mirrors.json` 里的下一个镜像.
   - 报 `ModuleNotFoundError` / `ImportError: numpy...` 时, 删掉 `packages/` 目录重新运行.

3. 提示 **未检测到有效网址** / 一直**等待登录完成** ?

   - `[course-url]` 要填**课程播放页**的地址 (能直接看到课程视频的页面), 不要填课程首页.
   - 登录页改版导致的等待已在最新版修复, 请先升级到最新发行版.

4. 登录时还是要手动过滑块?

   - 只有 `enableAutoCaptcha = True` 才会自动过滑块, 否则需要手动完成.

5. 只出现命令行窗口, 没有浏览器界面?

   - 命令行是程序后台, 可以查看运行状态; 浏览器加载需要时间, 后台没异常退出就不必担心. 报错通常是浏览器路径配置有误.
   - 找浏览器路径: 打开浏览器, 地址栏输入 `chrome://version`, 其中的"可执行文件目录"就是.

   <p align="left"><img src="resources/markdown/broswer_version.png" width="720" alt="Chrome 可执行文件目录"></p>

6. 看不到浏览器窗口 / 想后台刷课 / 报 `MoveWindow 无效的窗口句柄`?

   - 最小化会让浏览器暂停渲染, 进度不增加, 也会影响弹题检测. 想后台刷课请用 `enableHideWindow = True`, 程序会把窗口移出屏幕.
   - 若报窗口句柄错误, 改回 `enableHideWindow = False`.

7. 卡在加载播放页 / 不会自动跳下一课 / 新版页面刷不了课?

   - 这类基本都是**页面结构还没适配**, 不是配置问题. 目前支持: 智慧共享课、翻转课、融合共享课.

   - 智慧树新版 **AI 课程** (带 AI 角标的课程) 尚未适配, 表现就是识别不到课时、停在加载中或单个视频无限循环; 需要等待适配

     欢迎附上课程链接和日志提 issue.

8. 弹题关不掉 / 程序卡住?

   - 弹题随时可能出现, 而检测不是实时的, 所以无法完全消除; 页面长时间无响应时, 先确认浏览器没有被最小化.

9. 遇到 PPT、PDF 等没有视频的章节会卡住?

   - 目前只处理视频课时, 非视频章节需要手动切到有视频的课时.

10. 不想再弹赞赏码?

    - 感谢您对本项目的支持~ 只需要设置 `showDonateCode = False` 就好了!

------
#### 已知问题

- **长时间挂机**有概率弹出人机验证, 程序检测到后会暂停操作, 直到手动验证完成;
- 浏览器窗口若**最小化**可能导致视频播放进度不增加;
- 若出现其他异常崩溃, 请提交 issue 并附上报错信息.

#### 写在最后

觉得体验还不错? 请留下你宝贵的 Star ⭐, 并分享给更多有需要的人!

或者为项目发电支持一下~

<p align="left"><img src="resources/markdown/donate.png" width="200" alt="赞赏码"></p>

**作者的 CSDN:** [欢迎关注~](https://blog.csdn.net/Runfreeone)

**声明：本程序只可用于学习和研究计算机原理, 请于 24h 内删除所有存档！**
