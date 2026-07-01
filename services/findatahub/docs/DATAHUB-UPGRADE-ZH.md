# FinDataHub 升级说明

## 已完成

- Phase 1: Stock layer 升级
  - `price_daily` / `technical_indicators` 支持 `raw` 和 `qfq`
  - SQLite 启动时自动迁移旧表
  - `data-package` 默认返回兼容结构，同时补充 `daily_qfq` / `daily_raw`
  - `data-quality` 支持更细的股票数据质量判断

- Phase 2: Market layer 实时事实层
  - 指数快照
  - 板块快照
  - 市场广度快照
  - 资金流快照
  - 主题热度快照
  - 衍生市场事件

- Phase 4: 基础工程化
  - 启动自动 schema upgrade
  - 新增升级文档
  - 保持旧 API 向后兼容

## 待继续完善

- 更完整的资金流字段映射与 provider 适配
- 更细的市场事件规则与历史回放
- DataHub 前端质量视图
- TradingAgents / FinClaw 的更深层消费升级
