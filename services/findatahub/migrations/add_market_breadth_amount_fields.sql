-- 添加两市成交额字段到 market_breadth_snapshots 表
-- 迁移日期: 2026-05-21

ALTER TABLE market_breadth_snapshots ADD COLUMN total_amount REAL;
ALTER TABLE market_breadth_snapshots ADD COLUMN total_amount_billion REAL;
ALTER TABLE market_breadth_snapshots ADD COLUMN total_volume REAL;
