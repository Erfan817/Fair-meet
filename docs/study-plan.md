# FairMeet 学习计划与进度

> 边做边学的完整路线。每完成一个任务就更新此文件（这也是 git 历史的一部分）。

## 总体路线

```
Phase 0 最小内核(2周) → Phase 1 等时圈公平算法(3周) → Phase 2 对话Agent(3周) → Phase 3 工程质量(2周) → Phase 4 加分项
```

| 阶段 | 内容 | 核心技能收获 | 状态 |
|------|------|-------------|------|
| Phase 0 | 完整推荐管线：地理编码→球面质心→POI→评分→地图 | Python 工程化、pytest、异步基础、FastAPI | 🚧 进行中 |
| Phase 1 | 通勤时间矩阵 + 方差最小化替代地理中点；策略模式；Redis 缓存；三级降级链 | 算法建模、缓存设计、并发限流 | ⬜ |
| Phase 2 | 会话状态机 + 增量重排 + function calling + 成本护栏 | LLM 应用、Agent 设计、成本控制 | ⬜ |
| Phase 3 | Docker compose、GitHub Actions CI、压测与 P95 优化 | DevOps、性能工程 | ⬜ |
| Phase 4 | 抽算法为 PyPI 包发布 / 写算法对比技术博客（二选一） | 开源发布、技术写作 | ⬜ |

## Phase 0 细化日程

| 天 | 任务 | 状态 | 提交 |
|----|------|------|------|
| Day 1~2 | 环境搭建 + 调通高德地理编码 | ✅ | `792033c` |
| Day 3~4 | 球面质心纯函数 + 4 个单元测试 | ✅ | `2577d68` |
| Day 5 | `config.py` + `geocode.py` 重构 + respx mock 测试（断网可跑） | 👈 进行中（空文件已建） | |
| Day 6 | async/await：多地址并发查询 + Semaphore 限流 | ⬜ | |
| Day 7 | POI 周边搜索 + 多因子评分排序（权重可配置） | ⬜ | |
| Day 8 | FastAPI 骨架：`POST /api/v1/recommend` | ⬜ | |
| Day 9 | 地图结果页（高德 JS API）+ 极简输入页 | ⬜ | |
| Day 10 | 错误处理收尾 + README 快速开始 + 验收自查 | ⬜ | |

## Phase 0 验收标准（来自规格第 6 节）

- [ ] 3 个真实地点请求返回 Top 5 场所 + 可打开的地图页
- [ ] 输入含无效地名时正常返回并列出被丢弃位置
- [ ] pytest 全绿且断网可跑
- [ ] 新环境按 README 5 分钟跑起来
- [ ] 中心计算独立模块，可整体替换
- [ ] devlog 记录完整
- [ ] 仓库无 API key 泄露

## 提交信息规范（Conventional Commits 简化版）

| 前缀 | 用途 | 示例 |
|------|------|------|
| `feat:` | 新功能 | `feat: geocode module with mock tests` |
| `fix:` | 修 bug | `fix: handle empty geocodes list` |
| `refactor:` | 不改功能只改结构 | `refactor: move import to top` |
| `docs:` | 文档 | `docs: update devlog` |
| `test:` | 只加测试 | `test: boundary cases for center` |
| `chore:` | 杂务（配置、gitignore） | `chore: add .vscode to gitignore` |

## 进度更新规则

1. 每完成一天任务：更新本文件的日程表（状态 + 提交号）
2. 每次踩坑/学到东西：devlog 记一条
3. 每次提交前：`py -m pytest` 必须全绿
