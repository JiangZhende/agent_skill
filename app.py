#!/usr/bin/env python
"""
Gradio 前端：skills-based agent web UI

用法:
    python app.py
    python app.py --share
    python app.py --port 7861
"""
import json
import os
import re
import sys
import argparse
import threading
import uuid
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.resolve()
sys.path.insert(0, str(PROJECT_ROOT))


from core import auth, config, errors
from core.agent import build_agent
from core import trajectory as traj_recorder
from core.sessions import SessionError, UserLimiter
from core.skill_installer import install_from_zip, install_from_skill_md, uninstall_skill
from core.skill_loader import SkillRegistry
from tools import ALL_TOOLS
from smolagents.memory import ActionStep, PlanningStep, FinalAnswerStep
from smolagents.gradio_ui import pull_messages_from_step
from smolagents.models import ChatMessageStreamDelta, agglomerate_stream_deltas

SKILLS_DIR = PROJECT_ROOT / "skills"

# 每用户的频率、并发和会话数上限（与 api.py 用同一组配置，各自计数）
_limiter = UserLimiter(config.RATE_LIMIT_PER_MINUTE, config.MAX_CONCURRENT_RUNS_PER_USER)
_agent_counts: dict[str, int] = {}
_agent_counts_lock = threading.Lock()


def _release_state(state):
    """回收一个会话状态里的 agent（主动点"新对话"、空闲超时、关闭页面时都会走到这里）。"""
    agent = state.pop("agent", None) if isinstance(state, dict) else None
    if agent is None:
        return
    try:
        agent.cleanup()
    except Exception:
        pass
    owner = state.get("owner")
    with _agent_counts_lock:
        if owner in _agent_counts:
            _agent_counts[owner] = max(0, _agent_counts[owner] - 1)


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


def _list_skill_names() -> list[str]:
    """返回已安装的 skill 名称列表，供下拉框使用。"""
    return sorted(SkillRegistry(SKILLS_DIR).skills.keys())


def _extract_final_answer_stream(accumulated: list[ChatMessageStreamDelta]) -> str:
    """从累积的 ChatMessageStreamDelta 中提取 final_answer 的 answer 文本。
    支持 native tool call（delta.tool_calls）和 content JSON 两种模型输出路径。
    """
    msg = agglomerate_stream_deltas(accumulated)

    # native tool call 路径：arguments 在 tool_calls 里
    if msg.tool_calls:
        tc = msg.tool_calls[0]
        if tc.function.name == "final_answer":
            raw_args = tc.function.arguments or ""
            # 尝试解析完整 JSON
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                return args.get("answer", "") if isinstance(args, dict) else ""
            except json.JSONDecodeError:
                # 部分截断：从原始字符串提取 answer 值
                m = re.search(r'"answer"\s*:\s*"((?:[^"\\]|\\.)*)', raw_args)
                if m:
                    partial = m.group(1)
                    partial = re.sub(r'\\u([0-9a-fA-F]{4})', lambda x: chr(int(x.group(1), 16)), partial)
                    return partial.replace("\\n", "\n").replace('\\"', '"').replace("\\\\", "\\")
        return f"🔧 {tc.function.name}..."

    # content JSON 路径：模型把 JSON tool call 输出在 content 里
    if msg.content:
        content = msg.content.strip()
        if not content.startswith("{"):
            return content
        tool_m = re.search(r'"(?:name|tool)"\s*:\s*"([^"]+)"', content)
        if not tool_m or tool_m.group(1) != "final_answer":
            return f"🔧 {tool_m.group(1)}..." if tool_m else ""
        m = re.search(r'"answer"\s*:\s*"((?:[^"\\]|\\.)*)', content)
        if m:
            partial = m.group(1)
            try:
                return json.loads(f'"{partial}"')
            except json.JSONDecodeError:
                partial = re.sub(r'\\u([0-9a-fA-F]{4})', lambda x: chr(int(x.group(1), 16)), partial)
                return partial.replace("\\n", "\n").replace('\\"', '"').replace("\\\\", "\\")

    return ""


def create_app():
    import gradio as gr

    # ── Chat 事件 ──────────────────────────────────────────────────────────────

    def _current_user(request) -> auth.User:
        user = auth.user_by_name(getattr(request, "username", None))
        if user is None:
            raise gr.Error("登录已失效，请刷新页面重新登录")
        return user

    def _session_label(session) -> str:
        sid = session.get("id") if isinstance(session, dict) else None
        return f"会话 ID：`{sid}`" if sid else "会话 ID：发送第一条消息后生成"

    def _get_or_create_agent(session, user, model_id, file_paths):
        """首次创建 agent；之后复用，多轮对话靠 agent.memory 保持上下文。"""
        if session.get("agent") is None:
            try:
                model_id = config.resolve_model(model_id)
            except ValueError as e:
                raise gr.Error(str(e))
            with _agent_counts_lock:
                if _agent_counts.get(user.name, 0) >= config.MAX_SESSIONS_PER_USER:
                    raise gr.Error(f"会话数已达上限（{config.MAX_SESSIONS_PER_USER}），请先在其他页面点\"新对话\"或关闭它们")
                _agent_counts[user.name] = _agent_counts.get(user.name, 0) + 1
            outputs_dir = Path(tempfile.mkdtemp(prefix="agent_out_"))
            try:
                agent = build_agent(
                    model_id=model_id,
                    api_base=config.API_BASE,
                    api_key=config.API_KEY,
                    skills_dir=SKILLS_DIR,
                    extra_tools=ALL_TOOLS,
                    upload_files=file_paths or None,
                    outputs_persist_to=outputs_dir,
                    use_docker=config.USE_DOCKER,
                    docker_image=config.DOCKER_IMAGE,
                )
            except BaseException:
                with _agent_counts_lock:
                    _agent_counts[user.name] -= 1
                raise
            session["owner"] = user.name
            session["id"] = uuid.uuid4().hex[:16]
            session["agent"] = agent
            session["outputs_dir"] = str(outputs_dir)
            session["is_new"] = True
        else:
            agent = session["agent"]
            failed = []
            newly_added = []
            for f in (file_paths or []):
                try:
                    name = agent.workspace.add_input(f)
                    newly_added.append(name)
                except Exception as e:
                    failed.append(f"{Path(f).name}: {e}")
            if failed:
                raise RuntimeError("部分文件上传失败：\n" + "\n".join(failed))
            session["is_new"] = False
            session["newly_uploaded"] = newly_added
        return agent

    def on_submit(task, uploaded_files, history, session, model_id, request: gr.Request):
        if not task.strip():
            yield history, session, gr.update(), _session_label(session)
            return
        user = _current_user(request)
        if len(task) > config.MAX_TASK_CHARS:
            raise gr.Error(f"任务描述过长（上限 {config.MAX_TASK_CHARS} 字符）")
        try:
            _limiter.start_run(user)
        except SessionError as e:
            raise gr.Error(e.message)
        try:
            for hist, sess, files in _run_turn(task, uploaded_files, history, session, user, model_id):
                yield hist, sess, files, _session_label(sess)
        finally:
            _limiter.end_run(user)

    def _run_turn(task, uploaded_files, history, session, user, model_id):
        file_paths = [f.name for f in (uploaded_files or [])]
        agent = _get_or_create_agent(session, user, model_id, file_paths)
        reset_memory = session.pop("is_new", True)
        newly_uploaded = session.pop("newly_uploaded", [])
        if newly_uploaded:
            names = "、".join(f"`@input:{n}`" for n in newly_uploaded)
            task = f"[本轮新上传的文件：{names}]\n{task}"

        history = list(history or [])
        history.append({"role": "user", "content": task})
        yield history, session, gr.update(visible=False)

        try:
            accumulated_deltas: list[ChatMessageStreamDelta] = []
            streaming_idx = None  # index of the current streaming placeholder in history
            for event in agent.run(task, stream=True, reset=reset_memory):
                if isinstance(event, ChatMessageStreamDelta):
                    accumulated_deltas.append(event)
                    content = _extract_final_answer_stream(accumulated_deltas)
                    if not content:
                        yield history, session, gr.update(visible=False)
                        continue
                    placeholder = {"role": "assistant", "content": content}
                    if streaming_idx is None:
                        history.append(placeholder)
                        streaming_idx = len(history) - 1
                    else:
                        history[streaming_idx] = placeholder
                elif isinstance(event, (ActionStep, PlanningStep, FinalAnswerStep)):
                    accumulated_deltas = []
                    if streaming_idx is not None:
                        history.pop(streaming_idx)
                        streaming_idx = None
                    for msg in pull_messages_from_step(
                        event, skip_model_outputs=getattr(agent, "stream_outputs", False)
                    ):
                        history.append({"role": msg.role, "content": msg.content, **({"metadata": msg.metadata} if msg.metadata else {})})
                yield history, session, gr.update(visible=False)
        except Exception as e:
            # 把原因显示在对话里，而不是只在服务端终端打印一段堆栈
            if streaming_idx is not None:
                history.pop(streaming_idx)
            history.append({"role": "assistant", "content": f"⚠️ {errors.describe(e, session.get('id'))}"})
            yield history, session, gr.update(visible=False)
            return
        finally:
            traj_recorder.save(agent, task, PROJECT_ROOT / "trajectories",
                               user=user.name, session_id=session.get("id"), source="web")
            # 不 cleanup，保留 agent 供下一轮使用

        outputs_dir = Path(session["outputs_dir"])
        out_files = [str(p) for p in outputs_dir.glob("*") if p.is_file()]
        if out_files:
            yield history, session, gr.update(value=out_files, visible=True)
        else:
            yield history, session, gr.update(visible=False)

    def on_clear(session):
        _release_state(session)
        return [], {}, gr.update(visible=False), _session_label({})

    def on_upload_hint(uploaded_files):
        if not uploaded_files:
            return "未上传文件"
        names = [Path(f.name).name for f in uploaded_files]
        return "已上传: " + ", ".join(names)

    # ── Skills 事件 ────────────────────────────────────────────────────────────

    def on_skill_upload(skill_file, request: gr.Request):
        """安装上传的 ZIP 或 SKILL.md，返回 (状态消息, skill 列表, 卸载下拉框)。"""
        if not _current_user(request).is_admin:
            return "⛔ 只有管理员可以安装 skill", _list_skills_md(), gr.update()
        if skill_file is None:
            return "请先选择文件", _list_skills_md(), gr.update()

        path = Path(skill_file.name)
        try:
            if path.suffix.lower() == ".zip":
                name, msg = install_from_zip(path, SKILLS_DIR)
            elif path.name == "SKILL.md" or path.suffix.lower() == ".md":
                name, msg = install_from_skill_md(path, SKILLS_DIR)
            else:
                return f"不支持的文件类型 '{path.suffix}'，请上传 .zip 或 SKILL.md", _list_skills_md(), gr.update()
        except Exception as e:
            return f"安装失败：{e}", _list_skills_md(), gr.update()

        names = _list_skill_names()
        return f"✅ {msg}", _list_skills_md(), gr.update(choices=names)

    def on_skill_uninstall(skill_name, request: gr.Request):
        """卸载选中的 skill，返回 (状态消息, skill 列表, 卸载下拉框)。"""
        if not _current_user(request).is_admin:
            return "⛔ 只有管理员可以卸载 skill", _list_skills_md(), gr.update()
        if not skill_name:
            return "请先选择要卸载的 skill", _list_skills_md(), gr.update()
        try:
            _, msg = uninstall_skill(skill_name, SKILLS_DIR)
        except Exception as e:
            return f"卸载失败：{e}", _list_skills_md(), gr.update()
        names = _list_skill_names()
        return f"🗑️ {msg}", _list_skills_md(), gr.update(choices=names, value=None)

    def on_skill_refresh():
        names = _list_skill_names()
        return _list_skills_md(), gr.update(choices=names)

    def on_page_load(request: gr.Request):
        """按登录用户的角色决定是否显示 skill 的安装 / 卸载区（真正的权限校验在上面的处理函数里）。"""
        is_admin = _current_user(request).is_admin
        return gr.update(visible=is_admin), gr.update(visible=not is_admin)

    # ── 界面布局 ───────────────────────────────────────────────────────────────

    with gr.Blocks(title="Agent Skills", fill_height=True) as demo:
        # 空闲超时或页面关闭后，Gradio 删除状态时回收 agent 和沙箱
        session_state = gr.State({}, time_to_live=config.SESSION_IDLE_TTL, delete_callback=_release_state)

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
                        session_info = gr.Markdown(_session_label({}))

                        gr.Markdown("---")
                        output_files = gr.File(
                            label="产物文件（点击下载）",
                            file_count="multiple",
                            interactive=False,
                            visible=False,
                        )

                        gr.Markdown("---")
                        model_id_input = gr.Dropdown(
                            label="模型（新对话生效）",
                            choices=config.allowed_models(),
                            value=config.MODEL_ID,
                            visible=len(config.allowed_models()) > 1,
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
                    inputs=[task_input, file_upload, chatbot, session_state, model_id_input],
                    outputs=[chatbot, session_state, output_files, session_info],
                )
                send_btn.click(**submit_kwargs)
                task_input.submit(**submit_kwargs)
                clear_btn.click(on_clear, inputs=[session_state],
                                outputs=[chatbot, session_state, output_files, session_info])

            # ── Tab 2: Skills 管理 ─────────────────────────────────────────────
            with gr.Tab("🧩 Skills 管理"):
                with gr.Row():

                    with gr.Column(scale=1, min_width=320, visible=False) as admin_col:
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

                        gr.Markdown("---")
                        gr.Markdown("### 卸载 Skill")
                        uninstall_dropdown = gr.Dropdown(
                            label="选择要卸载的 Skill",
                            choices=_list_skill_names(),
                            value=None,
                        )
                        uninstall_btn = gr.Button("卸载 Skill", variant="stop")
                        uninstall_status = gr.Markdown("")

                    with gr.Column(scale=2):
                        admin_notice = gr.Markdown("_安装和卸载 skill 需要管理员账号。_", visible=False)
                        gr.Markdown("### 已安装 Skills")
                        refresh_btn = gr.Button("🔄 刷新列表", size="sm")
                        skills_list = gr.Markdown(_list_skills_md())

                install_btn.click(
                    on_skill_upload,
                    inputs=[skill_file_input],
                    outputs=[install_status, skills_list, uninstall_dropdown],
                )
                uninstall_btn.click(
                    on_skill_uninstall,
                    inputs=[uninstall_dropdown],
                    outputs=[uninstall_status, skills_list, uninstall_dropdown],
                )
                refresh_btn.click(
                    on_skill_refresh,
                    outputs=[skills_list, uninstall_dropdown],
                )

        demo.load(on_page_load, outputs=[admin_col, admin_notice])

    return demo


def main():
    global SKILLS_DIR
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=7860)
    parser.add_argument("--share", action="store_true")
    parser.add_argument("--skills-dir", type=Path, default=None, help="覆盖默认 skills 目录")
    args = parser.parse_args()

    if args.skills_dir:
        SKILLS_DIR = args.skills_dir.resolve()

    auth.check_startup()
    if config.USE_DOCKER:
        from core.executor import cleanup_orphans
        cleanup_orphans()  # 上次进程崩溃后遗留的沙箱容器

    launch_kwargs = {}
    if not auth.auth_disabled():
        launch_kwargs.update(auth=auth.authenticate_login, auth_message="用户名填账号名，密码填 token")
    create_app().launch(server_port=args.port, share=args.share, **launch_kwargs)


if __name__ == "__main__":
    main()
