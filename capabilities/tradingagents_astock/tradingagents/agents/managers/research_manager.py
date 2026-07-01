"""Research Manager: turns the bull/bear debate into a structured investment plan for the trader."""

from __future__ import annotations

from tradingagents.agents.schemas import ResearchPlan, render_research_plan
from tradingagents.agents.utils.agent_utils import build_instrument_context, get_language_instruction
from tradingagents.agents.utils.structured import (
    bind_structured,
    invoke_structured_or_freetext,
)


def create_research_manager(llm):
    structured_llm = bind_structured(llm, ResearchPlan, "Research Manager")

    def research_manager_node(state) -> dict:
        instrument_context = build_instrument_context(state["company_of_interest"])
        history = state["investment_debate_state"].get("history", "")

        investment_debate_state = state["investment_debate_state"]

        prompt = f"""As the Research Manager and debate facilitator, your role is to critically evaluate this round of debate and deliver a clear, actionable investment plan for the trader.

{instrument_context}

Note: This is an A-share (China mainland) stock. Always factor in regulatory policy impact. If the research bundle also includes capital-flow or lockup/insider-reduction context, incorporate it as supplemental evidence rather than assuming it is always available.

---

**评级刻度**（必须且只能选择一个）：
- **买入**：多方逻辑强，建议新建或显著增加仓位
- **增配**：观点偏积极，建议逐步增加敞口
- **持有**：多空证据相对均衡，建议维持现有仓位
- **减配**：观点偏谨慎，建议降低敞口或部分止盈
- **卖出**：空方逻辑强，建议退出或避免介入

只在多空证据真正均衡时选择“持有”；否则应根据更强的一方明确表态。

---

**Debate History:**
{history}

Be decisive and ground every conclusion in specific evidence from the debate.{get_language_instruction()}"""

        investment_plan = invoke_structured_or_freetext(
            structured_llm,
            llm,
            prompt,
            render_research_plan,
            "Research Manager",
        )

        new_investment_debate_state = {
            "judge_decision": investment_plan,
            "history": investment_debate_state.get("history", ""),
            "bear_history": investment_debate_state.get("bear_history", ""),
            "bull_history": investment_debate_state.get("bull_history", ""),
            "current_response": investment_plan,
            "count": investment_debate_state["count"],
        }

        return {
            "investment_debate_state": new_investment_debate_state,
            "investment_plan": investment_plan,
        }

    return research_manager_node
