#!/usr/bin/env python3
"""数据库迁移脚本：添加两市成交额字段"""

import sqlite3
import sys
from pathlib import Path

from services.findatahub.backend.config import settings

# 数据库路径
DB_PATH = Path(settings.db_url.replace("sqlite:///", "", 1)) if settings.db_url.startswith("sqlite:///") else Path("findatahub.sqlite")

# 迁移 SQL
MIGRATION_SQL = """
ALTER TABLE market_breadth_snapshots ADD COLUMN total_amount REAL;
ALTER TABLE market_breadth_snapshots ADD COLUMN total_amount_billion REAL;
ALTER TABLE market_breadth_snapshots ADD COLUMN total_volume REAL;
"""

def main():
    if not DB_PATH.exists():
        print(f"❌ 数据库文件不存在: {DB_PATH}")
        sys.exit(1)

    print(f"📁 数据库路径: {DB_PATH}")

    try:
        conn = sqlite3.connect(str(DB_PATH))
        cursor = conn.cursor()

        # 检查字段是否已存在
        cursor.execute("PRAGMA table_info(market_breadth_snapshots)")
        columns = [row[1] for row in cursor.fetchall()]

        if "total_amount" in columns:
            print("✅ 字段已存在，无需迁移")
            conn.close()
            return

        print("🔄 开始迁移...")

        # 执行迁移
        for sql in MIGRATION_SQL.strip().split(";"):
            sql = sql.strip()
            if sql:
                print(f"   执行: {sql}")
                cursor.execute(sql)

        conn.commit()
        print("✅ 迁移成功！")

        # 验证
        cursor.execute("PRAGMA table_info(market_breadth_snapshots)")
        columns = [row[1] for row in cursor.fetchall()]
        print(f"📊 当前字段: {', '.join(columns)}")

        conn.close()

    except Exception as e:
        print(f"❌ 迁移失败: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
