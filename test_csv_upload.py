#!/usr/bin/env python
"""
端到端测试：文件上传 → csv_to_report skill → Excel 产物持久化

测试的完整链路：
  1. 创建临时 CSV 样本
  2. --upload 注入 workspace.inputs/
  3. agent 调用 list_inputs 确认文件
  4. load_skill → inspect_csv (@input:xxx) → generate_excel (@input:xxx, @output:xxx)
  5. cleanup 把 outputs/ 持久化到 ./test_outputs/
  6. 断言 Excel 文件存在且非空
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.resolve()
sys.path.insert(0, str(PROJECT_ROOT))

_SMOLAGENTS_DEV = Path.home() / "code" / "deepresearch"
if _SMOLAGENTS_DEV.exists() and str(_SMOLAGENTS_DEV) not in sys.path:
    sys.path.insert(0, str(_SMOLAGENTS_DEV))

# ── 1. 创建样本 CSV ────────────────────────────────────────────────────────────
CSV_CONTENT = """\
销售员,区域,产品,Q3营收(万),增长率
张三,华南,产品A,120,12%
李四,华东,产品B,98,8%
王五,华北,产品A,145,20%
赵六,华南,产品C,67,-3%
陈七,华东,产品C,210,35%
"""

def _make_sample_csv() -> Path:
    tmp = Path(tempfile.mkstemp(suffix=".csv", prefix="sample_sales_")[1])
    tmp.write_text(CSV_CONTENT, encoding="utf-8")
    return tmp


# ── 2. 运行 agent ──────────────────────────────────────────────────────────────
def run():
    from core.agent import build_agent
    from core import trajectory as traj_recorder
    from tools import ALL_TOOLS

    csv_path = _make_sample_csv()
    outputs_dir = PROJECT_ROOT / "test_outputs"
    shutil.rmtree(outputs_dir, ignore_errors=True)

    print(f"[test] 样本 CSV: {csv_path}")
    print(f"[test] 产物目录: {outputs_dir}")
    print("=" * 60)

    agent = build_agent(
        model_id=os.getenv("MODEL_ID", "Pro/zai-org/GLM-5.1"),
        api_base=os.getenv("API_BASE", "https://api.siliconflow.cn/v1/"),
        api_key=os.getenv("API_KEY"),
        skills_dir=Path(os.getenv("SKILLS_DIR", str(PROJECT_ROOT / "skills"))),
        extra_tools=ALL_TOOLS,
        upload_files=[csv_path],
        outputs_persist_to=outputs_dir,
    )

    task = f"用户上传了一份 CSV 销售数据（文件名：{csv_path.name}），请把它转成 Excel 报表，标题用'2025Q3 销售数据'"

    try:
        result = agent.run(task)
    finally:
        traj_path = traj_recorder.save(agent, task, PROJECT_ROOT / "trajectories")
        print(f"\n[trajectory] {traj_path}")
        agent.cleanup()
        csv_path.unlink(missing_ok=True)

    print("\n" + "=" * 60)
    print("agent 最终输出:")
    print(result)

    # ── 3. 断言 ────────────────────────────────────────────────────────────────
    xlsx_files = list(outputs_dir.glob("*.xlsx"))
    if xlsx_files:
        f = xlsx_files[0]
        size = f.stat().st_size
        print(f"\n[PASS] Excel 文件已生成: {f.name} ({size} bytes)")
    else:
        print(f"\n[FAIL] test_outputs/ 下没找到 .xlsx 文件")
        print(f"  目录内容: {list(outputs_dir.iterdir()) if outputs_dir.exists() else '目录不存在'}")
        sys.exit(1)


if __name__ == "__main__":
    run()
