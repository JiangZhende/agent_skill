"""
主入口示例。

实际使用时：
1. 修改 model_id / api_base 指向你内网的推理服务
2. 在 tools/ 下添加你的真实业务工具
3. 在 skills/ 下添加你的业务 skills
"""
import os
from pathlib import Path

from core.agent import build_agent
from tools import ALL_TOOLS


def main():
    agent = build_agent(
        # 用 OpenAI 兼容协议接你的内网推理服务
        # 比如 vLLM: api_base="http://your-vllm:8000/v1"
        model_id=os.getenv("MODEL_ID", "gpt-4o-mini"),
        api_base=os.getenv("API_BASE"),
        api_key=os.getenv("API_KEY") or os.getenv("OPENAI_API_KEY"),
        skills_dir=Path(__file__).parent / "skills",
        extra_tools=ALL_TOOLS,
        use_docker=False,  # POC 阶段先关，上量后开
    )

    # 示例任务：测试 skill 选择和组合
    task = "帮我生成华南区 2025Q3 的销售报告，看看 Top 产品有哪些"

    print(f"任务: {task}\n")
    print("=" * 60)
    result = agent.run(task)
    print("=" * 60)
    print(f"\n最终结果:\n{result}")


if __name__ == "__main__":
    main()
