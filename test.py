#!/usr/bin/env python
"""
CLI 入口。

基本用法:
    python test.py --task "帮我生成华南区 2025Q3 的销售报告"
    python test.py --task "分析 /var/log/app.log 中的错误"

高级用法:
    python test.py --task "..." --skill sales_report   # 强制指定 skill
    python test.py --task "..." --verbose              # 打印推理过程
    python test.py --task "..." --docker               # 启用 Docker 沙箱
    python test.py --list-skills                       # 查看所有可用 skill
"""
import argparse
import os
import sys
from pathlib import Path

# 让 main 入口能正确 import 项目内的模块
PROJECT_ROOT = Path(__file__).parent.resolve()
sys.path.insert(0, str(PROJECT_ROOT))

_SMOLAGENTS_DEV = Path.home() / "code" / "deepresearch"
if _SMOLAGENTS_DEV.exists() and str(_SMOLAGENTS_DEV) not in sys.path:
    sys.path.insert(0, str(_SMOLAGENTS_DEV))

from core.skill_loader import SkillRegistry
from core import trajectory as traj_recorder


def cmd_list_skills(skills_dir: Path):
    """列出所有可用 skill"""
    reg = SkillRegistry(skills_dir)
    print(f"发现 {len(reg.skills)} 个 skills：\n")
    for name, skill in reg.skills.items():
        print(f"  {name}")
        print(f"    描述: {skill.meta.description}")
        print(f"    触发词: {', '.join(skill.meta.triggers) or '（无）'}")
        print(f"    路径: {skill.dir}")
        print()


def _wrap_model_verbose(model):
    """包装 model.__call__，把每次模型返回的原始文本和 tool_calls 打印出来。"""
    import json
    original_call = model.__call__

    def verbose_call(*a, **kw):
        response = original_call(*a, **kw)
        print("\n\033[36m─── 模型原始输出 ───\033[0m")
        if response.content:
            print(f"\033[37m{response.content}\033[0m")
        if response.tool_calls:
            for tc in response.tool_calls:
                args_str = json.dumps(tc.function.arguments, ensure_ascii=False, indent=2) \
                    if isinstance(tc.function.arguments, dict) else tc.function.arguments
                print(f"\033[33m[tool_call] {tc.function.name}\033[0m")
                print(f"\033[33m{args_str}\033[0m")
        print("\033[36m──────────────────\033[0m\n")
        return response

    model.__call__ = verbose_call
    return model


def cmd_run_task(args, skills_dir: Path):
    """执行一个任务"""
    # 延迟 import：--list-skills 等不依赖 LLM 的命令可以在没装 smolagents 时跑
    from core.agent import build_agent
    from tools import ALL_TOOLS

    agent = build_agent(
        model_id="Pro/zai-org/GLM-5.1",
        api_base="https://api.siliconflow.cn/v1/",
        api_key="sk-sptwlpbrnycnelnftjzvfwgdhlznnbgvrimiuzqairsstjou",
        skills_dir=skills_dir,
        extra_tools=ALL_TOOLS,
        upload_files=args.upload or None,
        outputs_persist_to=args.outputs_dir,
    )

    _wrap_model_verbose(agent.model)

    # 如果指定了 --skill，强制把这个 skill 注入到任务里
    task = args.task
    if args.skill:
        reg = SkillRegistry(skills_dir)
        if args.skill not in reg.skills:
            print(f"错误: skill '{args.skill}' 不存在", file=sys.stderr)
            sys.exit(1)
        task = (
            f"使用 {args.skill} skill 处理以下任务（先 load_skill 加载）：\n"
            f"{args.task}"
        )

    if args.verbose:
        print(f"[任务] {task}")
        print(f"[模型] {os.getenv('MODEL_ID', 'gpt-4o-mini')}")
        print(f"[沙箱] {'Docker' if args.docker else 'Local'}")
        print("=" * 60)

    # smolagents 默认会打印推理过程，--verbose 不变更行为，可以再扩展
    try:
        result = agent.run(task)
    finally:
        traj_path = traj_recorder.save(agent, task, PROJECT_ROOT / "trajectories")
        print(f"[trajectory] {traj_path}")
        outputs = agent.workspace.list_outputs()
        agent.cleanup()

    print("\n" + "=" * 60)
    print("最终结果:")
    print(result)

    if outputs:
        print("\n=== 生成的产物文件 ===")
        for f in outputs:
            if args.outputs_dir:
                final_path = Path(args.outputs_dir) / f['name']
                print(f"  {f['name']} ({f['size_human']}) -> {final_path}")
            else:
                print(f"  {f['name']} ({f['size_human']})  [未指定 --outputs-dir，文件已清理]")


def main():
    parser = argparse.ArgumentParser(
        prog="test.py",
        description="Skills-based Agent CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python test.py --task "华南区 2025Q3 销售报告"
  python test.py --task "分析 /var/log/app.log" --skill log_analysis
  python test.py --list-skills
        """,
    )
    parser.add_argument("--task", "-t", type=str, help="要执行的任务描述")
    parser.add_argument("--skill", "-s", type=str, help="强制指定使用的 skill 名称")
    parser.add_argument("--upload", "-u", action="append", type=str,
                        help="上传文件到 agent（可多次，对应 @input:<filename>）")
    parser.add_argument("--outputs-dir", "-o", type=Path,
                        help="把 agent 生成的产物文件持久化到此目录")
    parser.add_argument("--verbose", "-v", action="store_true", help="打印更多调试信息")
    parser.add_argument("--docker", action="store_true", help="启用 Docker 沙箱（暂未实现）")
    parser.add_argument("--list-skills", action="store_true", help="列出所有可用 skill")
    parser.add_argument(
        "--skills-dir",
        type=Path,
        default=PROJECT_ROOT / "skills",
        help="skills 目录路径",
    )

    args = parser.parse_args()

    if args.list_skills:
        cmd_list_skills(args.skills_dir)
        return

    if not args.task:
        parser.error("必须提供 --task 或 --list-skills")

    cmd_run_task(args, args.skills_dir)


if __name__ == "__main__":
    main()
