#!/usr/bin/env python3
"""
简化的记忆系统集成验证脚本
"""

import sys
from pathlib import Path

print("=" * 60)
print("FinClaw 记忆系统集成验证")
print("=" * 60)
print()

# 1. 检查文件结构
print("✓ 检查文件结构...")
memory_dir = Path(__file__).parent / "data" / "memory"
files_to_check = [
    memory_dir / "profile.md",
    memory_dir / "playbook.md",
    memory_dir / "convictions.md",
    memory_dir / "archive" / "profile_archive.md",
    memory_dir / "archive" / "playbook_archive.md",
    memory_dir / "archive" / "convictions_archive.md",
]

all_exist = True
for file in files_to_check:
    if file.exists():
        print(f"  ✅ {file.name}")
    else:
        print(f"  ❌ {file.name} 不存在")
        all_exist = False

if not all_exist:
    print("\n❌ 文件结构不完整")
    sys.exit(1)

print()

# 2. 检查工具文件
print("✓ 检查工具文件...")
tool_files = [
    Path(__file__).parent / "tools" / "memory_tools.py",
    Path(__file__).parent / "api" / "memory_api.py",
    Path(__file__).parent / "services" / "memory_hook.py",
    Path(__file__).parent / "tools" / "memory_analytics.py",
]

for file in tool_files:
    if file.exists():
        print(f"  ✅ {file.name}")
    else:
        print(f"  ❌ {file.name} 不存在")
        sys.exit(1)

print()

# 3. 检查 prompt 文件
print("✓ 检查 prompt 文件...")
prompt_files = [
    Path(__file__).parent / "prompts" / "core" / "identity.md",
    Path(__file__).parent / "prompts" / "core" / "mission.md",
    Path(__file__).parent / "prompts" / "core" / "behavior.md",
    Path(__file__).parent / "prompts" / "core" / "tool_use.md",
    Path(__file__).parent / "prompts" / "memory" / "session_hook_prompt.md",
]

for file in prompt_files:
    if file.exists():
        print(f"  ✅ {file.name}")
    else:
        print(f"  ❌ {file.name} 不存在")
        sys.exit(1)

print()

# 4. 检查前端文件
print("✓ 检查前端文件...")
frontend_files = [
    Path(__file__).parent.parent / "web" / "src" / "components" / "MemoryPanel" / "index.tsx",
    Path(__file__).parent.parent / "web" / "src" / "components" / "MemoryPanel" / "MemoryPanel.css",
]

for file in frontend_files:
    if file.exists():
        print(f"  ✅ {file.name}")
    else:
        print(f"  ❌ {file.name} 不存在")
        sys.exit(1)

print()

# 5. 检查关键集成点
print("✓ 检查关键集成点...")

# 检查 bootstrap.py 是否注册了工具
bootstrap_file = Path(__file__).parent / "tools" / "bootstrap.py"
if bootstrap_file.exists():
    content = bootstrap_file.read_text(encoding="utf-8")
    if "memory_tools" in content and "memory_read" in content:
        print("  ✅ bootstrap.py 已注册记忆工具")
    else:
        print("  ❌ bootstrap.py 未注册记忆工具")
        sys.exit(1)
else:
    print("  ❌ bootstrap.py 不存在")
    sys.exit(1)

# 检查 prompt_builder.py 是否保留动态长期记忆注入
prompt_builder_file = Path(__file__).parent / "core" / "prompt_builder.py"
if prompt_builder_file.exists():
    content = prompt_builder_file.read_text(encoding="utf-8")
    if "core/identity.md" in content and "build_memory_context" in content:
        print("  ✅ prompt_builder.py 已集成 core prompt 与动态记忆系统")
    else:
        print("  ❌ prompt_builder.py 未集成 core prompt 或动态记忆系统")
        sys.exit(1)
else:
    print("  ❌ prompt_builder.py 不存在")
    sys.exit(1)

# 检查 memory.py 是否调用了 build_memory_context
memory_service_file = Path(__file__).parent / "services" / "memory.py"
if memory_service_file.exists():
    content = memory_service_file.read_text(encoding="utf-8")
    if "user_message" in content and "build_system_messages" in content:
        print("  ✅ services/memory.py 已集成长期记忆")
    else:
        print("  ❌ services/memory.py 未集成长期记忆")
        sys.exit(1)
else:
    print("  ❌ services/memory.py 不存在")
    sys.exit(1)

# 检查 app.py 是否注册了 memory API
app_file = Path(__file__).parent / "app.py"
if app_file.exists():
    content = app_file.read_text(encoding="utf-8")
    if "memory_api" in content or "memory_router" in content:
        print("  ✅ app.py 已注册记忆 API")
    else:
        print("  ❌ app.py 未注册记忆 API")
        sys.exit(1)
else:
    print("  ❌ app.py 不存在")
    sys.exit(1)

# 检查 Chat.tsx 是否使用了 MemoryPanel
chat_file = Path(__file__).parent.parent / "web" / "src" / "components" / "Chat.tsx"
if chat_file.exists():
    content = chat_file.read_text(encoding="utf-8")
    if "MemoryPanel" in content and "from \"./MemoryPanel\"" in content:
        print("  ✅ Chat.tsx 已集成 MemoryPanel")
    else:
        print("  ❌ Chat.tsx 未集成 MemoryPanel")
        sys.exit(1)
else:
    print("  ❌ Chat.tsx 不存在")
    sys.exit(1)

print()
print("=" * 60)
print("✅ 所有集成检查通过！")
print("=" * 60)
print()
print("下一步：")
print("1. 启动后端服务：cd backend && uvicorn app:app --reload")
print("2. 启动前端服务：cd web && npm run dev")
print("3. 访问 http://localhost:5173 查看记忆面板")
print()
