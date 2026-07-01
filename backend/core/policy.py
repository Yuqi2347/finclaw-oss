from __future__ import annotations

from .models import ApprovalPolicy, Permission, RiskLevel


def default_risk_for_permission(permission: Permission) -> RiskLevel:
    mapping = {
        Permission.READ: RiskLevel.SAFE_READ,
        Permission.ANALYZE_CACHED: RiskLevel.SAFE_READ,
        Permission.LOW_RISK_REFRESH: RiskLevel.LOW_EXPENSIVE,
        Permission.EXPENSIVE_CONFIRM: RiskLevel.MEDIUM_EXPENSIVE,
        Permission.WRITE_CONFIRM: RiskLevel.WRITE,
        Permission.DANGEROUS_WRITE: RiskLevel.DANGEROUS,
    }
    return mapping[permission]


def requires_confirmation(
    permission: Permission,
    risk: RiskLevel | None = None,
    policy: ApprovalPolicy = ApprovalPolicy.BALANCED,
) -> bool:
    if permission == Permission.LOW_RISK_REFRESH:
        return False
    assessed_risk = risk or default_risk_for_permission(permission)
    if policy == ApprovalPolicy.NEVER:
        return False
    if policy == ApprovalPolicy.ALWAYS_ASK:
        return assessed_risk != RiskLevel.SAFE_READ
    if policy == ApprovalPolicy.AUTO_LOW_RISK:
        return assessed_risk in {RiskLevel.MEDIUM_EXPENSIVE, RiskLevel.HIGH_EXPENSIVE, RiskLevel.WRITE, RiskLevel.DANGEROUS}
    if policy == ApprovalPolicy.STRICT:
        return permission in {
            Permission.EXPENSIVE_CONFIRM,
            Permission.WRITE_CONFIRM,
            Permission.DANGEROUS_WRITE,
        }
    return assessed_risk in {
        RiskLevel.LOW_EXPENSIVE,
        RiskLevel.MEDIUM_EXPENSIVE,
        RiskLevel.HIGH_EXPENSIVE,
        RiskLevel.WRITE,
        RiskLevel.DANGEROUS,
    } or permission in {
        Permission.EXPENSIVE_CONFIRM,
        Permission.WRITE_CONFIRM,
        Permission.DANGEROUS_WRITE,
    }


def permission_label(permission: Permission) -> str:
    labels = {
        Permission.READ: "读取数据",
        Permission.ANALYZE_CACHED: "读取/总结已有分析",
        Permission.LOW_RISK_REFRESH: "低风险刷新",
        Permission.EXPENSIVE_CONFIRM: "运行高成本分析",
        Permission.WRITE_CONFIRM: "修改用户数据",
        Permission.DANGEROUS_WRITE: "高风险修改",
    }
    return labels[permission]


def risk_label(risk: RiskLevel) -> str:
    labels = {
        RiskLevel.SAFE_READ: "安全读取",
        RiskLevel.LOW_EXPENSIVE: "低成本外部操作",
        RiskLevel.MEDIUM_EXPENSIVE: "中等成本外部操作",
        RiskLevel.HIGH_EXPENSIVE: "高成本外部操作",
        RiskLevel.WRITE: "修改用户数据",
        RiskLevel.DANGEROUS: "高风险操作",
    }
    return labels[risk]
