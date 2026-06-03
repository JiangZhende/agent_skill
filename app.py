#!/usr/bin/env python
"""
Gradio 前端：skills-based agent web UI

用法:
    python app.py
    python app.py --share
    python app.py --port 7861
"""
import os
import sys
import argparse
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.resolve()
sys.path.insert(0, str(PROJECT_ROOT))

_SMOLAGENTS_DEV = Path.home() / "code" / "deepresearch"
if _SMOLAGENTS_DEV.exists() and str(_SMOLAGENTS_DEV) not in sys.path:
    sys.path.insert(0, str(_SMOLAGENTS_DEV))

from smolagents.gradio_ui import stream_to_gradio
from core.agent import build_agent
from core import trajectory as traj_recorder
from core.skill_installer import install_from_zip, install_from_skill_md
from core.skill_loader import SkillRegistry
from tools import ALL_TOOLS

MODEL_ID = os.getenv("MODEL_ID", "Pro/zai-org/GLM-5.1")
API_BASE  = os.getenv("API_BASE", "https://api.siliconflow.cn/v1/")
API_KEY   = os.getenv("API_KEY",  "sk-sptwlpbrnycnelnftjzvfwgdhlznnbgvrimiuzqairsstjou")
SKILLS_DIR = PROJECT_ROOT / "skills"


def _build(upload_files, outputs_dir):
    return build_agent(
        model_id=MODEL_ID,
        api_base=API_BASE,
        api_key=API_KEY,
        skills_dir=SKILLS_DIR,
        extra_tools=ALL_TOOLS,
        upload_files=upload_files or None,
        outputs_persist_to=outputs_dir,
    )


def _list_skills_md() -> str:
    """返回当前 skills 目录的 Markdown 列表，用于展示。"""
    reg = SkillRegistry(SKILLS_DIR)
    if not reg.skills:
        return "_（暂无已安装的 skill）_"
    lines = []
    for s in reg.skills.values():
        triggers = "、".join(s.meta.triggers) if s.meta.triggers else "—"
        has_scripts = (s.dir / "scripts").exists()
        icon = "📦" if has_scripts else "📄"
        lines.append(f"- {icon} **{s.name}**：{s.meta.description}  \n  触发词: `{triggers}`")
    return "\n".join(lines)


def create_app():
    import gradio as gr

    # ── Chat 事件 ──────────────────────────────────────────────────────────────

    def _get_or_create_agent(session, model_id, api_base, api_key, file_paths):
        """首次创建 agent；之后复用，多轮对话靠 agent.memory 保持上下文。"""
        if session.get("agent") is None:
            outputs_dir = Path(tempfile.mkdtemp(prefix="agent_out_"))
            agent = build_agent(
                model_id=model_id.strip() or MODEL_ID,
                api_base=api_base.strip() or API_BASE,
                api_key=api_key.strip() or API_KEY,
                skills_dir=SKILLS_DIR,
                extra_tools=ALL_TOOLS,
                upload_files=file_paths or None,
                outputs_persist_to=outputs_dir,
            )
            session["agent"] = agent
            session["outputs_dir"] = str(outputs_dir)
            session["is_new"] = True
        else:
            agent = session["agent"]
            for f in (file_paths or []):
                try:
                    agent.workspace.add_input(f)
                except Exception:
                    pass
            session["is_new"] = False
        return agent

    def on_submit(task, uploaded_files, history, session, model_id, api_base, api_key):
        if not task.strip():
            yield history, session, gr.update()
            return

        file_paths = [f.name for f in (uploaded_files or [])]
        agent = _get_or_create_agent(session, model_id, api_base, api_key, file_paths)
        reset_memory = session.pop("is_new", True)

        history = list(history or [])
        history.append({"role": "user", "content": task})
        yield history, session, gr.update(visible=False)

        try:
            for msg in stream_to_gradio(agent, task=task, reset_agent_memory=reset_memory):
                history.append(msg)
                yield history, session, gr.update(visible=False)
        finally:
            traj_recorder.save(agent, task, PROJECT_ROOT / "trajectories")
            # 不 cleanup，保留 agent 供下一轮使用

        outputs_dir = Path(session["outputs_dir"])
        out_files = [str(p) for p in outputs_dir.glob("*") if p.is_file()]
        if out_files:
            yield history, session, gr.update(value=out_files, visible=True)
        else:
            yield history, session, gr.update(visible=False)

    def on_clear(session):
        if session.get("agent"):
            session["agent"].cleanup()
        return [], {}, gr.update(visible=False)

    def on_upload_hint(uploaded_files):
        if not uploaded_files:
            return "未上传文件"
        names = [Path(f.name).name for f in uploaded_files]
        return "已上传: " + ", ".join(names)

    # ── Skills 事件 ────────────────────────────────────────────────────────────

    def on_skill_upload(skill_file):
        """安装上传的 ZIP 或 SKILL.md，返回 (状态消息, 更新后的 skill 列表)。"""
        if skill_file is None:
            return "请先选择文件", _list_skills_md()

        path = Path(skill_file.name)
        try:
            if path.suffix.lower() == ".zip":
                name, msg = install_from_zip(path, SKILLS_DIR)
            elif path.name == "SKILL.md" or path.suffix.lower() == ".md":
                name, msg = install_from_skill_md(path, SKILLS_DIR)
            else:
                return f"不支持的文件类型 '{path.suffix}'，请上传 .zip 或 SKILL.md", _list_skills_md()
        except Exception as e:
            return f"安装失败：{e}", _list_skills_md()

        return f"✅ {msg}", _list_skills_md()

    def on_skill_refresh():
        return _list_skills_md()

    # ── 界面布局 ───────────────────────────────────────────────────────────────

    with gr.Blocks(title="Agent Skills", fill_height=True) as demo:
        session_state = gr.State({})

        with gr.Tabs():

            # ── Tab 1: 对话 ────────────────────────────────────────────────────
            with gr.Tab("💬 对话"):
                with gr.Row(equal_height=True):

                    with gr.Column(scale=1, min_width=280):
                        gr.Markdown("## Agent Skills")

                        task_input = gr.Textbox(
                            label="任务描述",
                            placeholder="例：把上传的 CSV 转成 Excel 报表",
                            lines=4,
                        )
                        with gr.Row():
                            send_btn = gr.Button("发送", variant="primary", scale=3)
                            clear_btn = gr.Button("新对话", scale=1)

                        gr.Markdown("---")
                        file_upload = gr.File(
                            label="上传文件（可多个）",
                            file_count="multiple",
                        )
                        upload_hint = gr.Markdown("未上传文件")

                        gr.Markdown("---")
                        output_files = gr.File(
                            label="产物文件（点击下载）",
                            file_count="multiple",
                            interactive=False,
                            visible=False,
                        )

                        gr.Markdown("---")
                        with gr.Accordion("⚙️ 模型配置", open=False):
                            model_id_input = gr.Textbox(
                                label="Model ID",
                                value=MODEL_ID,
                                placeholder="例：gpt-4o-mini",
                            )
                            api_base_input = gr.Textbox(
                                label="API Base URL",
                                value=API_BASE,
                                placeholder="例：https://api.openai.com/v1/",
                            )
                            api_key_input = gr.Textbox(
                                label="API Key",
                                value=API_KEY,
                                type="password",
                            )

                    with gr.Column(scale=3):
                        chatbot = gr.Chatbot(
                            label="对话 & 推理过程",
                            type="messages",
                            height=680,
                            resizable=True,
                            show_copy_button=True,
                        )

                file_upload.change(on_upload_hint, [file_upload], [upload_hint])

                submit_kwargs = dict(
                    fn=on_submit,
                    inputs=[task_input, file_upload, chatbot, session_state,
                            model_id_input, api_base_input, api_key_input],
                    outputs=[chatbot, session_state, output_files],
                )
                send_btn.click(**submit_kwargs)
                task_input.submit(**submit_kwargs)
                clear_btn.click(on_clear, inputs=[session_state],
                                outputs=[chatbot, session_state, output_files])

            # ── Tab 2: Skills 管理 ─────────────────────────────────────────────
            with gr.Tab("🧩 Skills 管理"):
                with gr.Row():

                    with gr.Column(scale=1, min_width=320):
                        gr.Markdown("### 安装新 Skill")
                        gr.Markdown(
                            "支持两种格式：\n"
                            "- **ZIP**：包含 `SKILL.md` + `scripts/` 目录\n"
                            "- **SKILL.md**：单文件，适合无脚本的 skill\n\n"
                            "ZIP 结构示例：\n"
                            "```\n"
                            "my_skill.zip\n"
                            "├── my_skill/\n"
                            "│   ├── SKILL.md\n"
                            "│   └── scripts/\n"
                            "│       └── run.py\n"
                            "```"
                        )
                        skill_file_input = gr.File(
                            label="上传 ZIP 或 SKILL.md",
                            file_types=[".zip", ".md"],
                        )
                        install_btn = gr.Button("安装 Skill", variant="primary")
                        install_status = gr.Markdown("")

                    with gr.Column(scale=2):
                        gr.Markdown("### 已安装 Skills")
                        refresh_btn = gr.Button("🔄 刷新列表", size="sm")
                        skills_list = gr.Markdown(_list_skills_md())

                install_btn.click(
                    on_skill_upload,
                    inputs=[skill_file_input],
                    outputs=[install_status, skills_list],
                )
                refresh_btn.click(
                    on_skill_refresh,
                    outputs=[skills_list],
                )

    return demo


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=7860)
    parser.add_argument("--share", action="store_true")
    args = parser.parse_args()

    create_app().launch(server_port=args.port, share=args.share)


if __name__ == "__main__":
    main()
