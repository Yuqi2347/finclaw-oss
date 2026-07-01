"""
记忆系统 API 端点（FastAPI 版本）
提供记忆查看、编辑、统计等功能
"""

from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.services.long_term_memory import long_term_memory_service

router = APIRouter(prefix="/api/memory", tags=["memory"])

MEMORY_DIR = Path(__file__).parent.parent / "data" / "memory"
ARCHIVE_DIR = MEMORY_DIR / "archive"


class MemoryUpdateRequest(BaseModel):
    content: str
    reason: str | None = None


class CandidateUpdateRequest(BaseModel):
    content: str | None = None
    evidence: str | None = None
    reason: str | None = None
    confidence: float | None = None
    operation: str | None = None


class CandidateRejectRequest(BaseModel):
    reason: str | None = None


class ConflictResolveRequest(BaseModel):
    resolution: str
    note: str | None = None


@router.get("/profile")
async def get_profile():
    """获取用户画像"""
    try:
        return long_term_memory_service.get_core("profile")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/playbook")
async def get_playbook():
    """获取研究框架"""
    try:
        return long_term_memory_service.get_core("playbook")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/convictions")
async def get_convictions():
    """获取当前有效投资判断"""
    try:
        return long_term_memory_service.get_core("convictions")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/archive/{file_type}")
async def get_archive(file_type: str):
    """获取归档文件"""
    try:
        if file_type not in ["profile", "playbook", "convictions"]:
            raise HTTPException(status_code=400, detail="无效的文件类型")

        file_path = ARCHIVE_DIR / f"{file_type}_archive.md"
        if not file_path.exists():
            raise HTTPException(status_code=404, detail="归档文件不存在")

        content = file_path.read_text(encoding="utf-8")

        return {
            "success": True,
            "content": content,
            "metadata": {
                "file_size": len(content)
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/{file_type}")
async def update_memory(file_type: str, request: MemoryUpdateRequest):
    """更新记忆文件（带自动备份）"""
    try:
        if file_type == "profile":
            raise HTTPException(status_code=403, detail="用户画像由 Agent 自动维护，不支持手动编辑")
        return long_term_memory_service.update_core(
            file_type,
            request.content,
            reason=request.reason or "用户手动编辑",
            source="manual_edit",
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/stats")
async def get_stats():
    """获取记忆系统统计数据"""
    try:
        stats = {}
        for file_type in ("profile", "playbook", "convictions"):
            payload = long_term_memory_service.get_core(file_type)
            stats[file_type] = {"exists": bool(payload.get("success")), **payload.get("metadata", {})}
        stats["candidates"] = {
            "pending": len(long_term_memory_service.list_candidates(status="pending").get("candidates", [])),
        }
        stats["conflicts"] = {
            "pending": len(long_term_memory_service.list_conflicts(status="pending").get("conflicts", [])),
        }
        return {
            "success": True,
            "stats": stats
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/index")
async def get_index():
    try:
        return long_term_memory_service.get_index()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/candidates")
async def list_candidates(status: str | None = "pending", target: str | None = None):
    try:
        return long_term_memory_service.list_candidates(status=status, target=target)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/candidates/{candidate_id}")
async def update_candidate(candidate_id: str, request: CandidateUpdateRequest):
    try:
        return long_term_memory_service.update_candidate(candidate_id, request.dict(exclude_none=True))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/candidates/{candidate_id}/approve")
async def approve_candidate(candidate_id: str):
    try:
        return long_term_memory_service.approve_candidate(candidate_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/candidates/{candidate_id}/reject")
async def reject_candidate(candidate_id: str, request: CandidateRejectRequest | None = None):
    try:
        return long_term_memory_service.reject_candidate(candidate_id, reason=(request.reason if request else "") or "")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/conflicts")
async def list_conflicts(status: str | None = "pending"):
    try:
        return long_term_memory_service.list_conflicts(status=status)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/conflicts/{conflict_id}/resolve")
async def resolve_conflict(conflict_id: str, request: ConflictResolveRequest):
    try:
        return long_term_memory_service.resolve_conflict(conflict_id, request.resolution, request.note or "")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/events")
async def list_events(limit: int = 50):
    try:
        return long_term_memory_service.list_events(limit=limit)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
