"""Sidebar: stock input, config display, and history list."""

from __future__ import annotations

from datetime import date

import streamlit as st

from web.history import get_history


def render_sidebar() -> None:
    """Render the sidebar with input controls and history."""

    st.markdown(
        """
        <div style="text-align:center; margin-bottom:1.5rem;">
            <span style="font-size:2rem; font-weight:800; color:#ff5a1f;">Trading</span><span style="font-size:2rem; font-weight:800; color:#f5f1eb;">Agents</span><span style="font-size:2rem; font-weight:800; color:#f5f1eb;">-</span><span style="font-size:2rem; font-weight:800; color:#ff5a1f;">Astock</span>
            <div style="font-size:0.85rem; color:#888; margin-top:0.2rem;">
                A股多Agent投研系统
            </div>
            <div style="font-size:0.7rem; color:#555; margin-top:0.3rem;">
                by <a href="https://github.com/simonlin1212" style="color:#ff5a1f; text-decoration:none;">simonlin1212</a>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.markdown("---")
    st.markdown("#### 新建分析")

    ticker = st.text_input(
        "股票代码",
        placeholder="例: 300750",
        key="input_ticker",
        help="输入6位A股代码",
    )

    trade_date = st.date_input(
        "分析日期",
        value=date.today(),
        key="input_date",
    )

    tracker = st.session_state.get("tracker")
    is_busy = tracker is not None and tracker.is_running

    if st.button(
        "开始分析" if not is_busy else "分析进行中...",
        use_container_width=True,
        disabled=is_busy or not ticker,
        type="primary",
    ):
        st.session_state["start_analysis"] = {
            "ticker": ticker.strip(),
            "trade_date": trade_date.strftime("%Y-%m-%d"),
        }
        st.session_state["viewing_history"] = None

    st.markdown("---")
    st.markdown("#### 历史记录")

    history = get_history()
    if not history:
        st.caption("暂无历史记录")
        return

    for entry in history[:20]:
        t, d = entry["ticker"], entry["date"]
        label = f"{t}  ·  {d}"
        if st.button(label, key=f"hist_{t}_{d}", use_container_width=True):
            st.session_state["viewing_history"] = entry["path"]
            st.session_state["start_analysis"] = None

    st.markdown("---")
    st.caption("⚠️ 仅供学习研究，不构成投资建议")
