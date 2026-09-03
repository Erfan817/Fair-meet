# FairMeet 开发日志

## 2026-09-03 Day 1：调通地理编码

- **做了什么**：环境搭建（Python 3.14.5 用 `py` 命令启动、venv、httpx/python-dotenv）+ 第一个高德地理编码 API 调用成功 + 首次 git 提交（792033c）
- **踩的坑**：
  1. 脚本建错目录（建到了 `C:\Users\aire`），`load_dotenv` 找不到 `.env` → `KeyError: 'AMAP_WEB_KEY'`
  2. `.env` 根本没创建（只有模板 `.env.example`）→ 同样的报错。教训：模板是给人看的，程序读的是 `.env`
  3. git 命令"运行好久"：其实是中文输入法把英文引号变成了弯引号，PowerShell 在 `>>` 提示符下一直等输入，命令根本没执行。解法：`Ctrl+C` 取消，引号手动敲
- **学到的**：
  - Traceback 从下往上读，最后一行才是真正的错误类型
  - git commit 是纯本地操作，不上传任何地方；`.git` 文件夹是存档室，只看不碰
  - `.env` 被 `.gitignore` 挡住，首次提交确认未泄露 key ✅
- **发现的问题**：一个地名可能返回多个结果（"北京大学"有 4 个校区，`count: 4`），暂定取 `geocodes[0]`。风险：用户想去的可能是别的校区 → 以后可做"地点确认"交互

## 2026-09-03 Day 2：补 Python 基础 + 公平中心纯函数

- **做了什么**：
  - 廖雪峰 Python 教程学到"面向对象入门"（变量/字典/列表/函数/模块/import）
  - 实现 `fairmeet/center.py`：球面质心算法（经纬度 → 三维向量求平均 → 转回经纬度）
  - 写了第一批单元测试，4 个用例全绿：单点返回自身、顺序无关、赤道上对称两点、空列表抛 ValueError
- **学到的**：
  - pytest 规则：`test_` 开头的函数自动被发现执行，不需要 main
  - 浮点数不能直接用 `==` 比（`0.1+0.2 != 0.3`），要用 `pytest.approx`
  - `pytest.raises` 做"反向测试"：断言代码**应该**报错
  - "先红后绿"工作法：先写测试看它失败，再写实现到全绿，证明测试不是摆设
- **Code review 待改**：
  - [ ] `import math` 写在了函数里 → 惯例：import 一律放文件顶部
  - [ ] 清理模板残留的提示注释
  - 改完重跑测试确认全绿 → commit：`feat: geographic center calculation with tests`
- **下一步**：Day 5 把 `geocode_test.py` 重构成 `fairmeet/geocode.py` 正式模块
  - 设计决策待做：key 作为参数传入（依赖注入雏形）；地址识别失败时返回 None 还是抛异常（写进日志说理由）
  - 新技能：respx mock 测试，目标断网也能跑测试
