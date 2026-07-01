from __future__ import annotations

from collections.abc import Iterable
from sqlalchemy import text
from sqlalchemy.engine import Engine


def run_schema_upgrades(engine: Engine) -> None:
    if not engine.url.drivername.startswith("sqlite"):
        return
    with engine.begin() as conn:
        _upgrade_sqlite_price_tables(conn)
        _upgrade_sqlite_market_context_tables(conn)
        _upgrade_sqlite_refresh_logs(conn)
        _upgrade_sqlite_market_news_tables(conn)


def _upgrade_sqlite_price_tables(conn) -> None:
    if "adjustment" not in _table_columns(conn, "price_daily"):
        _rebuild_price_daily(conn)
    if "adjustment" not in _table_columns(conn, "technical_indicators"):
        _rebuild_technical_indicators(conn)
    _upgrade_sqlite_technical_indicator_columns(conn)


def _upgrade_sqlite_technical_indicator_columns(conn) -> None:
    if not _table_exists(conn, "technical_indicators"):
        return
    columns = _table_columns(conn, "technical_indicators")
    extras = {
        "boll_mid": "FLOAT",
        "boll_ub": "FLOAT",
        "boll_lb": "FLOAT",
        "vwma20": "FLOAT",
    }
    for column, ddl in extras.items():
        if column not in columns:
            conn.execute(text(f"ALTER TABLE technical_indicators ADD COLUMN {column} {ddl}"))


def _upgrade_sqlite_refresh_logs(conn) -> None:
    if not _table_exists(conn, "refresh_logs"):
        return
    columns = _table_columns(conn, "refresh_logs")
    extras = {
        "target_scope": "VARCHAR(64)",
        "source": "VARCHAR(32)",
        "error_code": "VARCHAR(64)",
        "started_at": "DATETIME",
        "finished_at": "DATETIME",
    }
    for column, ddl in extras.items():
        if column not in columns:
            conn.execute(text(f"ALTER TABLE refresh_logs ADD COLUMN {column} {ddl}"))


def _upgrade_sqlite_market_context_tables(conn) -> None:
    if not _table_exists(conn, "market_breadth_snapshots"):
        return
    breadth_columns = _table_columns(conn, "market_breadth_snapshots")
    extras = {
        "total_amount": "FLOAT",
        "total_amount_billion": "FLOAT",
        "total_volume": "FLOAT",
    }
    for column, ddl in extras.items():
        if column not in breadth_columns:
            conn.execute(text(f"ALTER TABLE market_breadth_snapshots ADD COLUMN {column} {ddl}"))


def _upgrade_sqlite_market_news_tables(conn) -> None:
    if not _table_exists(conn, "market_news_articles"):
        conn.execute(
            text(
                """
                CREATE TABLE market_news_articles (
                    id INTEGER NOT NULL PRIMARY KEY,
                    news_id VARCHAR(160) NOT NULL,
                    provider VARCHAR(64) NOT NULL,
                    source_platform VARCHAR(64) NOT NULL,
                    source_label VARCHAR(128),
                    title VARCHAR(512) NOT NULL,
                    summary TEXT,
                    url TEXT,
                    crawl_date DATE NOT NULL,
                    rank_position INTEGER,
                    published_at_text VARCHAR(128),
                    category VARCHAR(64),
                    event_type VARCHAR(64),
                    confidence FLOAT,
                    final_score FLOAT,
                    raw_payload JSON,
                    fetched_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL,
                    CONSTRAINT uq_market_news_id_date UNIQUE (news_id, crawl_date)
                )
                """
            )
        )
    indexes = [
        "CREATE INDEX IF NOT EXISTS ix_market_news_articles_news_id ON market_news_articles (news_id)",
        "CREATE INDEX IF NOT EXISTS ix_market_news_articles_source_platform ON market_news_articles (source_platform)",
        "CREATE INDEX IF NOT EXISTS ix_market_news_articles_crawl_date ON market_news_articles (crawl_date)",
    ]
    for ddl in indexes:
        conn.execute(text(ddl))


def _rebuild_price_daily(conn) -> None:
    if not _table_exists(conn, "price_daily"):
        return
    conn.execute(text("ALTER TABLE price_daily RENAME TO price_daily_legacy"))
    conn.execute(
        text(
            """
            CREATE TABLE price_daily (
                id INTEGER NOT NULL PRIMARY KEY,
                ticker VARCHAR(32) NOT NULL,
                trade_date DATE NOT NULL,
                adjustment VARCHAR(16) NOT NULL DEFAULT 'qfq',
                open FLOAT,
                high FLOAT,
                low FLOAT,
                close FLOAT,
                volume FLOAT,
                amount FLOAT,
                source VARCHAR(32) NOT NULL,
                created_at DATETIME NOT NULL,
                CONSTRAINT uq_daily_ticker_date_adj UNIQUE (ticker, trade_date, adjustment)
            )
            """
        )
    )
    conn.execute(
        text(
            """
            INSERT INTO price_daily (
                id, ticker, trade_date, adjustment, open, high, low, close,
                volume, amount, source, created_at
            )
            SELECT
                id, ticker, trade_date, 'qfq', open, high, low, close,
                volume, amount, source, created_at
            FROM price_daily_legacy
            """
        )
    )
    conn.execute(text("DROP TABLE price_daily_legacy"))
    _create_price_daily_indexes(conn)


def _rebuild_technical_indicators(conn) -> None:
    if not _table_exists(conn, "technical_indicators"):
        return
    conn.execute(text("ALTER TABLE technical_indicators RENAME TO technical_indicators_legacy"))
    conn.execute(
        text(
            """
            CREATE TABLE technical_indicators (
                id INTEGER NOT NULL PRIMARY KEY,
                ticker VARCHAR(32) NOT NULL,
                trade_date DATE NOT NULL,
                adjustment VARCHAR(16) NOT NULL DEFAULT 'qfq',
                ma5 FLOAT,
                ma10 FLOAT,
                ma20 FLOAT,
                ma60 FLOAT,
                rsi14 FLOAT,
                macd FLOAT,
                macd_signal FLOAT,
                macd_hist FLOAT,
                atr14 FLOAT,
                created_at DATETIME NOT NULL,
                CONSTRAINT uq_indicator_ticker_date_adj UNIQUE (ticker, trade_date, adjustment)
            )
            """
        )
    )
    conn.execute(
        text(
            """
            INSERT INTO technical_indicators (
                id, ticker, trade_date, adjustment, ma5, ma10, ma20, ma60,
                rsi14, macd, macd_signal, macd_hist, atr14, created_at
            )
            SELECT
                id, ticker, trade_date, 'qfq', ma5, ma10, ma20, ma60,
                rsi14, macd, macd_signal, macd_hist, atr14, created_at
            FROM technical_indicators_legacy
            """
        )
    )
    conn.execute(text("DROP TABLE technical_indicators_legacy"))
    _create_technical_index_indexes(conn)


def _create_price_daily_indexes(conn) -> None:
    indexes = [
        "CREATE INDEX IF NOT EXISTS ix_price_daily_ticker ON price_daily (ticker)",
        "CREATE INDEX IF NOT EXISTS ix_price_daily_trade_date ON price_daily (trade_date)",
        "CREATE INDEX IF NOT EXISTS ix_price_daily_adjustment ON price_daily (adjustment)",
    ]
    for ddl in indexes:
        conn.execute(text(ddl))


def _create_technical_index_indexes(conn) -> None:
    indexes = [
        "CREATE INDEX IF NOT EXISTS ix_technical_indicators_ticker ON technical_indicators (ticker)",
        "CREATE INDEX IF NOT EXISTS ix_technical_indicators_trade_date ON technical_indicators (trade_date)",
        "CREATE INDEX IF NOT EXISTS ix_technical_indicators_adjustment ON technical_indicators (adjustment)",
    ]
    for ddl in indexes:
        conn.execute(text(ddl))


def _table_exists(conn, name: str) -> bool:
    row = conn.execute(
        text("SELECT name FROM sqlite_master WHERE type='table' AND name=:name"),
        {"name": name},
    ).fetchone()
    return row is not None


def _table_columns(conn, name: str) -> set[str]:
    if not _table_exists(conn, name):
        return set()
    rows = conn.execute(text(f"PRAGMA table_info({name})")).fetchall()
    return {row[1] for row in rows}
