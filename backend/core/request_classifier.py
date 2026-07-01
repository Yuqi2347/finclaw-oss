from __future__ import annotations

from typing import Literal


RequestType = Literal["execution", "artifact_access", "question", "mixed"]


class RequestClassifier:
    """分类用户请求，决定 tool_choice 策略

    根据用户消息内容判断请求类型：
    - execution: 明确执行类请求（删除、启动、添加等），强制 tool_choice="required"
    - artifact_access: 报告打开/下载/查看类请求，强制 tool_choice="required"
    - question: 纯问答类请求，使用 tool_choice="auto"
    - mixed: 混合类请求，使用 tool_choice="auto" 但加强 prompt
    """

    EXECUTION_KEYWORDS = [
        "删除", "启动", "运行", "更新", "添加", "移除",
        "加入", "生成", "执行", "确认", "取消", "修改", "创建",
        "新增", "导入", "导出", "保存", "提交", "发布", "部署"
    ]

    DATA_REFRESH_KEYWORDS = [
        "刷新数据", "刷新行情", "刷新快照", "刷新新闻", "刷新基本面",
        "刷新公司资料", "更新数据", "更新行情", "更新快照",
    ]

    ARTIFACT_ACCESS_KEYWORDS = [
        "打开报告", "下载报告", "查看报告", "读取报告", "阅读报告",
        "看报告", "查报告",
        "报告链接", "下载链接", "打开链接", "查看链接",
    ]

    ARTIFACT_ACCESS_TRIGGERS = [
        "打开",
        "下载",
        "链接",
        "报告库",
        "报告详情",
        "html",
        "md",
        "json",
    ]

    QUESTION_KEYWORDS = [
        "是什么", "为什么", "怎么样", "如何", "什么时候",
        "有哪些", "能否", "可以吗", "是否", "多少", "哪个",
        "哪些", "什么", "谁", "在哪", "几个", "几次"
    ]

    def classify(self, message: str, has_pending_action: bool) -> RequestType:
        """分类请求类型

        Args:
            message: 用户消息
            has_pending_action: 当前是否有待确认操作

        Returns:
            "execution": 明确执行类请求，强制 tool_choice="required"
            "question": 纯问答类请求，使用 tool_choice="auto"
            "mixed": 混合类请求，使用 tool_choice="auto" 但加强 prompt
        """
        message_lower = message.lower().strip()

        # 用户说"确认"/"取消"时，如果有 pending action 则是执行类
        confirmation_keywords = ["确认", "取消", "同意", "拒绝", "批准", "否决"]
        if has_pending_action and any(kw in message_lower for kw in confirmation_keywords):
            return "execution"

        if any(keyword in message_lower for keyword in self.ARTIFACT_ACCESS_KEYWORDS):
            return "artifact_access"

        if "报告" in message or "report" in message_lower:
            if any(trigger in message_lower for trigger in self.ARTIFACT_ACCESS_TRIGGERS):
                return "artifact_access"

        if any(keyword in message for keyword in self.DATA_REFRESH_KEYWORDS):
            return "question"

        # 统计执行和问答关键词
        exec_count = sum(1 for kw in self.EXECUTION_KEYWORDS if kw in message)
        question_count = sum(1 for kw in self.QUESTION_KEYWORDS if kw in message)

        # 短消息（<30字）且包含执行关键词 -> 执行类
        if len(message) < 30 and exec_count > 0:
            return "execution"

        # 执行关键词明显多于问答关键词 -> 执行类
        if exec_count > question_count and exec_count >= 1:
            return "execution"

        # 纯问答
        if question_count > 0 and exec_count == 0:
            return "question"

        # 默认混合类（保守策略）
        return "mixed"

    def get_tool_choice(self, request_type: RequestType) -> str | dict:
        """根据请求类型返回 tool_choice 参数

        Args:
            request_type: 请求类型

        Returns:
            "required": 强制调用工具（执行类请求、报告链接访问类请求）
            "auto": 允许 LLM 选择（问答类和混合类请求）
        """
        if request_type in {"execution", "artifact_access"}:
            return "required"  # 强制调用工具
        return "auto"  # 允许 LLM 选择


request_classifier = RequestClassifier()
