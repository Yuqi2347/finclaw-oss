"""
记忆系统分析工具
提供记忆质量监控和统计分析功能
"""

import re
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Any


MEMORY_DIR = Path(__file__).parent.parent / "data" / "memory"


def analyze_memory_system() -> Dict[str, Any]:
    """
    分析记忆系统健康度

    Returns:
        {
            "profile": {...},
            "playbook": {...},
            "convictions": {...},
            "overall_health": "good" | "warning" | "critical"
        }
    """
    profile_stats = analyze_profile()
    playbook_stats = analyze_playbook()
    convictions_stats = analyze_convictions()

    # 评估整体健康度
    overall_health = "good"
    if playbook_stats["dimension_count"] == 0:
        overall_health = "warning"
    if convictions_stats["missing_failure_conditions"] > 0:
        overall_health = "critical"

    return {
        "profile": profile_stats,
        "playbook": playbook_stats,
        "convictions": convictions_stats,
        "overall_health": overall_health,
        "generated_at": datetime.now().isoformat()
    }


def analyze_profile() -> Dict[str, Any]:
    """分析 Profile 统计数据"""
    try:
        file_path = MEMORY_DIR / "profile.md"
        if not file_path.exists():
            return {"exists": False, "entry_count": 0}

        content = file_path.read_text(encoding="utf-8")
        lines = content.split("\n")

        # 统计条目数
        entries = [line for line in lines if line.strip().startswith("- [")]
        entry_count = len(entries)

        # 统计置信度分布
        confidence_dist = {"high": 0, "medium": 0, "low": 0, "disputed": 0}
        for entry in entries:
            if "[high]" in entry:
                confidence_dist["high"] += 1
            elif "[medium]" in entry:
                confidence_dist["medium"] += 1
            elif "[low]" in entry:
                confidence_dist["low"] += 1
            if "disputed" in entry:
                confidence_dist["disputed"] += 1

        # 提取最后更新时间
        match = re.search(r"最后更新：(.+)", content)
        last_updated = match.group(1).strip() if match else "未知"

        return {
            "exists": True,
            "entry_count": entry_count,
            "confidence_distribution": confidence_dist,
            "last_updated": last_updated,
            "file_size": len(content)
        }
    except Exception as e:
        return {"exists": False, "error": str(e)}


def analyze_playbook() -> Dict[str, Any]:
    """分析 Playbook 统计数据"""
    try:
        file_path = MEMORY_DIR / "playbook.md"
        if not file_path.exists():
            return {"exists": False, "dimension_count": 0, "chapter_count": 0}

        content = file_path.read_text(encoding="utf-8")
        lines = content.split("\n")

        architecture = _extract_current_research_architecture(content)
        dimension_count = len([line for line in architecture.splitlines() if re.match(r"^\s*维度\S*[：:]", line)])

        # 提取最后更新时间
        match = re.search(r"(\d{4}-\d{2}-\d{2})：", content)
        last_updated = match.group(1) if match else "未知"

        return {
            "exists": True,
            "chapter_count": dimension_count,
            "dimension_count": dimension_count,
            "last_updated": last_updated,
            "file_size": len(content)
        }
    except Exception as e:
        return {"exists": False, "error": str(e)}


def analyze_convictions() -> Dict[str, Any]:
    """分析 Convictions 统计数据"""
    try:
        file_path = MEMORY_DIR / "convictions.md"
        if not file_path.exists():
            return {"exists": False, "active_count": 0}

        content = file_path.read_text(encoding="utf-8")
        lines = content.split("\n")

        # 统计 active / watching 投资判断数量
        active_count = len([line for line in lines if line.startswith("### [active]")])
        watching_count = len([line for line in lines if line.startswith("### [watching]")])

        # 检查失效条件缺失
        missing_failure_conditions = 0
        current_conviction = None
        has_failure_condition = False

        for line in lines:
            if line.startswith("### [active]"):
                if current_conviction and not has_failure_condition:
                    missing_failure_conditions += 1
                current_conviction = line
                has_failure_condition = False
            if "失效条件" in line:
                has_failure_condition = True

        # 最后一个共识检查
        if current_conviction and not has_failure_condition:
            missing_failure_conditions += 1

        # 统计层级分布
        layer_dist = {
            "market": len([line for line in lines if line.startswith("## 市场层")]),
            "industry_mainline": len([line for line in lines if line.startswith("## 行业/主线层")]),
            "stock": len([line for line in lines if line.startswith("## 标的层")]),
            "watching": len([line for line in lines if line.startswith("## 观察中")])
        }

        # 提取最后更新时间
        match = re.search(r"创建.*?(\d{4}-\d{2}-\d{2})", content)
        last_updated = match.group(1) if match else "未知"

        return {
            "exists": True,
            "active_count": active_count,
            "watching_count": watching_count,
            "missing_failure_conditions": missing_failure_conditions,
            "layer_distribution": layer_dist,
            "last_updated": last_updated,
            "file_size": len(content)
        }
    except Exception as e:
        return {"exists": False, "error": str(e)}


def generate_health_report() -> str:
    """生成记忆系统健康报告（markdown 格式）"""
    stats = analyze_memory_system()

    report = "# 记忆系统健康报告\n\n"
    report += f"生成时间：{stats['generated_at']}\n\n"
    report += f"整体健康度：**{stats['overall_health'].upper()}**\n\n"
    report += "---\n\n"

    # Profile 报告
    profile = stats["profile"]
    report += "## Profile（用户画像）\n\n"
    if profile["exists"]:
        report += f"- 条目数：{profile['entry_count']}\n"
        report += f"- 置信度分布：\n"
        for level, count in profile["confidence_distribution"].items():
            report += f"  - {level}: {count}\n"
        report += f"- 最后更新：{profile['last_updated']}\n"
    else:
        report += "- ⚠️ Profile 文件不存在\n"
    report += "\n"

    # Playbook 报告
    playbook = stats["playbook"]
    report += "## Playbook（研究框架）\n\n"
    if playbook["exists"]:
        report += f"- 当前研究维度数：{playbook['dimension_count']}\n"
        report += f"- 最后更新：{playbook['last_updated']}\n"
        if playbook["dimension_count"] == 0:
            report += "- ⚠️ 警告：Playbook 尚未定义当前研究架构\n"
    else:
        report += "- ⚠️ Playbook 文件不存在\n"
    report += "\n"

    # Convictions 报告
    convictions = stats["convictions"]
    report += "## Convictions（当前投资判断）\n\n"
    if convictions["exists"]:
        report += f"- Active 判断数：{convictions['active_count']}\n"
        report += f"- Watching 判断数：{convictions.get('watching_count', 0)}\n"
        report += f"- 层级分布：\n"
        for layer, count in convictions["layer_distribution"].items():
            report += f"  - {layer}: {count}\n"
        report += f"- 最后更新：{convictions['last_updated']}\n"
        if convictions["missing_failure_conditions"] > 0:
            report += f"- ❌ 严重：{convictions['missing_failure_conditions']} 条投资判断缺失失效条件\n"
    else:
        report += "- ⚠️ Convictions 文件不存在\n"
    report += "\n"

    report += "---\n\n"
    report += "## 建议\n\n"

    if stats["overall_health"] == "critical":
        report += "- ❌ 发现严重问题，请立即修复缺失失效条件的投资判断\n"
    elif stats["overall_health"] == "warning":
        report += "- ⚠️ Playbook 需要补充当前研究架构，建议在实际使用中逐步完善\n"
    else:
        report += "- ✅ 记忆系统运行正常\n"

    return report


def _extract_current_research_architecture(content: str) -> str:
    text = str(content or "")
    match = re.search(r"(?ms)^##\s+当前研究架构\s*$", text)
    if not match:
        return re.sub(r"(?ms)<!-- finclaw-memory:.*?-->\s*", "", text).strip()
    next_heading = re.search(r"(?m)^##\s+", text[match.end():])
    end = match.end() + next_heading.start() if next_heading else len(text)
    section = text[match.start():end]
    return re.sub(r"(?ms)<!-- finclaw-memory:.*?-->\s*", "", section).strip()


if __name__ == "__main__":
    # 命令行运行时生成报告
    report = generate_health_report()
    print(report)

    # 保存报告
    report_path = MEMORY_DIR / "health_report.md"
    report_path.write_text(report, encoding="utf-8")
    print(f"\n报告已保存到：{report_path}")
