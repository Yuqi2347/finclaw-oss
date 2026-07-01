from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.default_config import DEFAULT_CONFIG

from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Create a custom config
config = DEFAULT_CONFIG.copy()
config["deep_think_llm"] = "gpt-5.4-mini"  # Use a different model
config["quick_think_llm"] = "gpt-5.4-mini"  # Use a different model
config["max_debate_rounds"] = 1  # Increase debate rounds

# Configure data vendors. This A-share fork reads market data from FinDataHub.
config["data_vendors"] = {
    "core_stock_apis": "a_stock",
    "technical_indicators": "a_stock",
    "fundamental_data": "a_stock",
    "news_data": "a_stock",
    "signal_data": "a_stock",
}
config["strict_data_vendor"] = True

# Initialize with custom config
ta = TradingAgentsGraph(debug=True, config=config)

# forward propagate
_, decision = ta.propagate("000001.SZ", "2026-07-01")
print(decision)

# Memorize mistakes and reflect
# ta.reflect_and_remember(1000) # parameter is the position returns
