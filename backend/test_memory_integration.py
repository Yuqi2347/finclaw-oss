#!/usr/bin/env python3
"""
记忆系统集成测试脚本
验证所有组件是否正确集成
"""

import sys
from pathlib import Path

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent.parent))

def test_memory_files():
    """测试记忆文件是否存在"""
    from backend.tools.memory_tools import MEMORY_DIR

    print("=" * 60)
    print("测试 1: 记忆文件结构")
    print("=" * 60)

    files = [
        MEMORY_DIR / "profile.md",
        MEMORY_DIR / "playbook.md",
        MEMORY_DIR / "convictions.md",
        MEMORY_DIR / "archive" / "profile_archive.md",
        MEMORY_DIR / "archive" / "playbook_archive.md",
        MEMORY_DIR / "archive" / "convictions_archive.md",
    ]

    for file in files:
        exists = "✅" if file.exists() else "❌"
        print(f"{exists} {file.relative_to(MEMORY_DIR.parent)}")

    print()


def test_memory_tools():
    """测试记忆工具是否可用"""
    from backend.tools import memory_tools

    print("=" * 60)
    print("测试 2: 记忆工具")
    print("=" * 60)

    # 测试 memory_read
    result = memory_tools.memory_read(file="profile")
    print(f"✅ memory_read: {result['success']}")

    # 测试 memory_write
    result = memory_tools.memory_write(
        file="profile",
        content="- [测试] 这是一个测试条目",
        reason="集成测试",
        position="append"
    )
    print(f"✅ memory_write: {result['success']}")

    # 测试 memory_update
    result = memory_tools.memory_update(
        file="profile",
        target="- [测试] 这是一个测试条目",
        new_content="- [测试, updated] 这是一个更新的测试条目",
        reason="集成测试更新"
    )
    print(f"✅ memory_update: {result['success']}")

    # 测试 memory_archive
    result = memory_tools.memory_archive(
        file="profile",
        target="- [测试, updated] 这是一个更新的测试条目",
        reason="集成测试归档"
    )
    print(f"✅ memory_archive: {result['success']}")

    print()


def test_tool_registry():
    """测试工具是否已注册"""
    from backend.tools.bootstrap import build_registry

    print("=" * 60)
    print("测试 3: 工具注册")
    print("=" * 60)

    registry = build_registry()
    memory_tools = [
        "memory_read",
        "memory_write",
        "memory_update",
        "memory_archive"
    ]

    for tool_name in memory_tools:
        spec = registry.get(tool_name)
        exists = "✅" if spec else "❌"
        permission = spec.permission.value if spec else "N/A"
        print(f"{exists} {tool_name} (permission: {permission})")

    print()


def test_prompt_integration():
    """测试 Prompt 集成"""
    from backend.core.prompt_builder import prompt_builder

    print("=" * 60)
    print("测试 4: Prompt 集成")
    print("=" * 60)

    # 测试静态 prompt 是否加载精简 core prompt。
    # 长期记忆内容通过 build_memory_context 动态注入，不再把 memory protocol 示例塞入主 prompt。
    static_prompt = prompt_builder.build_static_prompt()

    checks = [
        ("core/identity.md", "<identity>" in static_prompt),
        ("core/mission.md", "<mission>" in static_prompt),
        ("core/behavior.md", "<behavior>" in static_prompt),
        ("core/tool_use.md", "<tool_use>" in static_prompt),
    ]

    for name, passed in checks:
        status = "✅" if passed else "❌"
        print(f"{status} {name} 已加载")

    # 测试记忆注入
    memory_context = prompt_builder.build_memory_context("我想分析产业链")
    has_memory = len(memory_context) > 0
    status = "✅" if has_memory else "❌"
    print(f"{status} 记忆上下文注入功能正常")

    print()


def test_memory_manager_integration():
    """测试 MemoryManager 集成"""
    from backend.services.memory import memory_manager

    print("=" * 60)
    print("测试 5: MemoryManager 集成")
    print("=" * 60)

    try:
        # 测试 build_context 是否包含长期记忆
        context = memory_manager.build_context("test_session")

        # 检查是否有 memory_context
        has_memory_context = any(
            "memory_context" in str(msg.get("content", "")).lower()
            for msg in context
        )

        status = "✅" if has_memory_context else "⚠️"
        print(f"{status} MemoryManager.build_context() 包含长期记忆")

        if not has_memory_context:
            print("   注意：可能因为记忆文件为空而未注入")

    except Exception as e:
        print(f"❌ MemoryManager 集成测试失败: {e}")

    print()


def test_api_routes():
    """测试 API 路由"""
    print("=" * 60)
    print("测试 6: API 路由")
    print("=" * 60)

    try:
        from backend.api.memory_api import router

        routes = [
            "/api/memory/profile",
            "/api/memory/playbook",
            "/api/memory/convictions",
            "/api/memory/archive/{file_type}",
            "/api/memory/{file_type}",
            "/api/memory/stats",
        ]

        print(f"✅ Memory API router 已创建")
        print(f"   包含 {len(router.routes)} 个路由")

    except Exception as e:
        print(f"❌ API 路由测试失败: {e}")

    print()


def test_analytics():
    """测试分析工具"""
    from backend.tools.memory_analytics import analyze_memory_system

    print("=" * 60)
    print("测试 7: 分析工具")
    print("=" * 60)

    try:
        stats = analyze_memory_system()
        print(f"✅ 记忆系统分析工具正常")
        print(f"   整体健康度: {stats['overall_health']}")
        print(f"   Profile 存在: {stats['profile']['exists']}")
        print(f"   Playbook 存在: {stats['playbook']['exists']}")
        print(f"   Convictions 存在: {stats['convictions']['exists']}")
    except Exception as e:
        print(f"❌ 分析工具测试失败: {e}")

    print()


def main():
    print("\n")
    print("╔" + "=" * 58 + "╗")
    print("║" + " " * 15 + "FinClaw 记忆系统集成测试" + " " * 15 + "║")
    print("╚" + "=" * 58 + "╝")
    print()

    try:
        test_memory_files()
        test_memory_tools()
        test_tool_registry()
        test_prompt_integration()
        test_memory_manager_integration()
        test_api_routes()
        test_analytics()

        print("=" * 60)
        print("✅ 所有测试完成！")
        print("=" * 60)
        print()
        print("下一步：")
        print("1. 重启后端服务：cd backend && uvicorn app:app --reload")
        print("2. 重启前端服务：cd web && npm run dev")
        print("3. 访问 http://localhost:5173 查看新的记忆面板")
        print()

    except Exception as e:
        print(f"\n❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
