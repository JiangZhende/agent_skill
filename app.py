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
import shutil
import sys
import argparse
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.resolve()
sys.path.insert(0, str(PROJECT_ROOT))


from core import auth, config, db, errors
from core.sessions import SessionError, SessionService
from core import skill_installer
from core.skill_installer import install_from_zip, install_from_skill_md, uninstall_skill
from core.skill_loader import SkillRegistry

SKILLS_DIR = PROJECT_ROOT / "skills"

# 会话、对话记忆和文件都在数据库和文件存储里；在 main() 里按 --skills-dir 创建
service: SessionService = None


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
        version = skill_installer.active_version(s.name, SKILLS_DIR)
        tag = f"（v{version}）" if version else ""
        net = " 🌐" if s.meta.network else ""
        lines.append(f"- {icon} **{s.name}**{tag}{net}：{s.meta.description}  \n  触发词: `{triggers}`")
    return "\n".join(lines)


def _version_choices() -> list[tuple[str, str]]:
    """未生效的版本（待审核的在前），供"发布 / 删除版本"下拉框使用。值是 name@version。"""
    return [
        (f"{r['name']} v{r['version']}（{skill_installer.STATUS_LABELS.get(r['status'], r['status'])}）",
         f"{r['name']}@{r['version']}")
        for r in skill_installer.list_versions(SKILLS_DIR) if not r["active"]
    ]


def _split_version(value: str) -> tuple[str, int]:
    name, _, version = (value or "").rpartition("@")
    if not name or not version.isdigit():
        raise skill_installer.SkillInstallError("请先选择一个版本")
    return name, int(version)


def _build_skill_image(skill_dir, name):
    """发布带 requirements.txt 的 skill 时构建它的沙箱镜像；本机执行模式下没有镜像可建。"""
    if config.USE_DOCKER:
        from core.executor import build_skill_image
        build_skill_image(skill_dir, config.DOCKER_IMAGE, name=name)


def _list_skill_names() -> list[str]:
    """返回已安装的 skill 名称列表，供下拉框使用。"""
    return sorted(SkillRegistry(SKILLS_DIR).skills.keys())


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

    def _session_choices(user) -> list[tuple[str, str]]:
        """历史会话下拉框的选项：标题 + 最后使用时间。"""
        return [
            (f"{time.strftime('%m-%d %H:%M', time.localtime(r['last_used_at']))}  {r['title'] or '（无标题）'}",
             r["session_id"])
            for r in service.list_sessions(user)
        ]

    def _output_files(session, user) -> list[str]:
        """把会话的全部产物取到本页面专用的临时目录，返回路径列表（供下载）。"""
        sid = session.get("id")
        if not sid:
            return []
        out_dir = Path(session.get("out_dir") or tempfile.mkdtemp(prefix="agent_out_"))
        session["out_dir"] = str(out_dir)
        shutil.rmtree(out_dir, ignore_errors=True)
        out_dir.mkdir(parents=True, exist_ok=True)
        paths = []
        for f in service.list_files(user, sid):
            if f["area"] == "outputs":
                paths.append(str(service.fetch_file(user, sid, "outputs", f["name"], dest_dir=out_dir)))
        return paths

    def _files_update(paths):
        return gr.update(value=paths, visible=True) if paths else gr.update(value=None, visible=False)

    def on_submit(task, uploaded_files, history, session, model_id, request: gr.Request):
        """提交运行，然后从数据库读取运行事件并显示。

        运行由 worker 执行，不在这个页面的请求里：页面关掉或刷新后运行照常继续，
        重新打开该会话就能看到结果。
        """
        if not task.strip():
            yield history, session, gr.update(), _session_label(session)
            return
        user = _current_user(request)
        if len(task) > config.MAX_TASK_CHARS:
            raise gr.Error(f"任务描述过长（上限 {config.MAX_TASK_CHARS} 字符）")
        try:
            session_id, run_id = service.submit(
                user, session.get("id"),
                None if session.get("id") else model_id,   # 模型只在新建会话时选
                task, source="web",
                upload_files=[f.name for f in (uploaded_files or [])],
            )
        except SessionError as e:
            if e.status == 404:  # 会话已被删除或过期：让用户从新对话开始，而不是卡在一个用不了的会话上
                session.pop("id", None)
            raise gr.Error(e.message)

        session["id"], session["run_id"] = session_id, run_id
        history = list(history or [])
        history.append({"role": "user", "content": task})
        yield history, session, gr.update(visible=False), _session_label(session)

        after, token_idx = 0, None
        while True:
            batch = service.read_events(user, run_id, after)
            for ev in batch:
                after = ev["seq"]
                if ev["type"] == "token":
                    item = {"role": "assistant", "content": ev["content"]}
                    if token_idx is None:
                        history.append(item)
                        token_idx = len(history) - 1
                    else:
                        history[token_idx] = item
                    continue
                if token_idx is not None:   # 步骤完成后，流式的临时回答被正式的步骤替换
                    history.pop(token_idx)
                    token_idx = None
                if ev["type"] == "step":
                    history.append({k: ev[k] for k in ("role", "content", "metadata") if k in ev})
                elif ev["type"] == "error":
                    history.append({"role": "assistant", "content": f"⚠️ {ev['content']}"})
                elif ev["type"] == "done":
                    if ev.get("status") == "cancelled":
                        history.append({"role": "assistant", "content": "⏹️ 已停止"})
                    session.pop("run_id", None)
                    yield history, session, _files_update(_output_files(session, user)), _session_label(session)
                    return
            yield history, session, gr.update(visible=False), _session_label(session)
            if not batch:
                time.sleep(0.2)

    def on_stop(session, request: gr.Request):
        """停止当前会话正在进行的运行（在下一个检查点停下，正在执行的脚本会先跑完）。"""
        run_id = session.get("run_id")
        if run_id:
            try:
                service.cancel(_current_user(request), run_id)
            except SessionError as e:
                raise gr.Error(e.message)

    def on_session_select(session_id, session, request: gr.Request):
        """打开一个历史会话：恢复聊天记录和产物，之后发的消息接着它继续。"""
        if not session_id:
            return gr.update(), session, gr.update(), _session_label(session)
        user = _current_user(request)
        try:
            detail = service.get_session(user, session_id, with_events=True)
        except SessionError as e:
            raise gr.Error(e.message)
        history = []
        for run in detail["runs"]:
            history.append({"role": "user", "content": run["task"]})
            history.extend(run.get("events") or [])
            if run["error"]:
                history.append({"role": "assistant", "content": f"⚠️ {run['error']}"})
        session = {"id": session_id, "out_dir": session.get("out_dir")}
        return history, session, _files_update(_output_files(session, user)), _session_label(session)

    def on_sessions_refresh(session, request: gr.Request):
        return gr.update(choices=_session_choices(_current_user(request)), value=session.get("id"))

    def on_session_delete(session, request: gr.Request):
        """删除当前会话，连同它的文件和记录。"""
        user = _current_user(request)
        sid = session.get("id")
        if not sid:
            raise gr.Error("当前没有打开的会话")
        try:
            service.delete_session(user, sid)
        except SessionError as e:
            raise gr.Error(e.message)
        shutil.rmtree(session.get("out_dir") or "", ignore_errors=True)
        return [], {}, gr.update(value=None, visible=False), _session_label({}), \
            gr.update(choices=_session_choices(user), value=None)

    def on_clear(session):
        # 只是让页面回到空白：原来的会话还在"历史会话"里，可以随时再打开
        shutil.rmtree(session.get("out_dir") or "", ignore_errors=True)
        return [], {}, gr.update(value=None, visible=False), _session_label({}), gr.update(value=None)

    def on_upload_hint(uploaded_files):
        if not uploaded_files:
            return "未上传文件"
        names = [Path(f.name).name for f in uploaded_files]
        return "已上传: " + ", ".join(names)

    # ── Skills 事件 ────────────────────────────────────────────────────────────

    def _skills_view(status: str, selected: str = None, review: str = ""):
        """Skills 页所有操作共用的返回值：(状态消息, skill 列表, 卸载下拉框, 版本下拉框, 版本概要)。"""
        return (
            status,
            _list_skills_md(),
            gr.update(choices=_list_skill_names(), value=None),
            gr.update(choices=_version_choices(), value=selected),
            review,
        )

    def _admin_only(request) -> str | None:
        return None if _current_user(request).is_admin else "⛔ 只有管理员可以管理 skill"

    def on_skill_upload(skill_file, request: gr.Request):
        """上传 ZIP 或 SKILL.md，生成一个待审核版本（不影响正在使用的 skill）。"""
        if denied := _admin_only(request):
            return _skills_view(denied)
        if skill_file is None:
            return _skills_view("请先选择文件")

        user = _current_user(request).name
        path = Path(skill_file.name)
        try:
            if path.suffix.lower() == ".zip":
                name, version, msg = install_from_zip(path, SKILLS_DIR, user=user)
            elif path.suffix.lower() == ".md":
                name, version, msg = install_from_skill_md(path, SKILLS_DIR, user=user)
            else:
                return _skills_view(f"不支持的文件类型 '{path.suffix}'，请上传 .zip 或 SKILL.md")
            review = skill_installer.review_summary(name, version, SKILLS_DIR)
        except skill_installer.SkillInstallError as e:
            return _skills_view(f"上传失败：{e}")
        return _skills_view(f"✅ {msg}。检查右侧的概要和脚本后，点\"发布\"才会生效。", f"{name}@{version}", review)

    def on_version_select(value, request: gr.Request):
        if _admin_only(request) or not value:
            return ""
        try:
            return skill_installer.review_summary(*_split_version(value), SKILLS_DIR)
        except skill_installer.SkillInstallError as e:
            return f"{e}"

    def on_version_publish(value, request: gr.Request):
        """发布选中的版本（发布旧版本即回滚）。带依赖的 skill 会先构建镜像，可能要等一两分钟。"""
        if denied := _admin_only(request):
            return _skills_view(denied)
        try:
            name, version = _split_version(value)
            msg = skill_installer.publish(name, version, SKILLS_DIR, user=_current_user(request).name,
                                          build_image=_build_skill_image)
        except skill_installer.SkillInstallError as e:
            return _skills_view(f"发布失败：{e}")
        except RuntimeError as e:  # 镜像构建失败：版本保持原状态
            return _skills_view(f"发布失败：{e}", value)
        return _skills_view(f"✅ {msg}")

    def on_version_discard(value, request: gr.Request):
        if denied := _admin_only(request):
            return _skills_view(denied)
        try:
            msg = skill_installer.discard_version(*_split_version(value), SKILLS_DIR)
        except skill_installer.SkillInstallError as e:
            return _skills_view(f"删除失败：{e}")
        return _skills_view(f"🗑️ {msg}")

    def on_skill_uninstall(skill_name, request: gr.Request):
        """卸载选中的 skill（连同它的所有版本）。"""
        if denied := _admin_only(request):
            return _skills_view(denied)
        if not skill_name:
            return _skills_view("请先选择要卸载的 skill")
        try:
            _, msg = uninstall_skill(skill_name, SKILLS_DIR)
        except skill_installer.SkillInstallError as e:
            return _skills_view(f"卸载失败：{e}")
        return _skills_view(f"🗑️ {msg}")

    def on_skill_refresh(request: gr.Request):
        view = _skills_view("")
        # 版本列表只给管理员
        return view if not _admin_only(request) else (view[0], view[1], view[2], gr.update(choices=[]), "")

    def on_page_load(request: gr.Request):
        """按登录用户的角色决定是否显示 skill 的安装 / 卸载区（真正的权限校验在上面的处理函数里）。"""
        user = _current_user(request)
        is_admin = user.is_admin
        # 版本列表和历史会话在这里按用户填充，不放进所有人都能拿到的页面初始配置里
        versions = gr.update(choices=_version_choices() if is_admin else [])
        sessions = gr.update(choices=_session_choices(user), value=None)
        return gr.update(visible=is_admin), gr.update(visible=not is_admin), versions, sessions

    # ── 界面布局 ───────────────────────────────────────────────────────────────

    with gr.Blocks(title="Agent Skills", fill_height=True) as demo:
        # 页面只记住当前打开的是哪个会话；会话内容都在服务端
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
                            stop_btn = gr.Button("停止", scale=1)
                            clear_btn = gr.Button("新对话", scale=1)

                        gr.Markdown("---")
                        file_upload = gr.File(
                            label="上传文件（可多个）",
                            file_count="multiple",
                        )
                        upload_hint = gr.Markdown("未上传文件")
                        session_info = gr.Markdown(_session_label({}))

                        gr.Markdown("---")
                        session_dropdown = gr.Dropdown(
                            label="历史会话（选中即可继续）",
                            choices=[],
                            value=None,
                            allow_custom_value=True,
                        )
                        delete_btn = gr.Button("删除当前会话", variant="stop", size="sm")

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
                refresh_kwargs = dict(fn=on_sessions_refresh, inputs=[session_state], outputs=[session_dropdown])
                send_btn.click(**submit_kwargs).then(**refresh_kwargs)
                task_input.submit(**submit_kwargs).then(**refresh_kwargs)
                stop_btn.click(on_stop, inputs=[session_state])
                clear_btn.click(on_clear, inputs=[session_state],
                                outputs=[chatbot, session_state, output_files, session_info, session_dropdown])
                session_dropdown.input(on_session_select, inputs=[session_dropdown, session_state],
                                       outputs=[chatbot, session_state, output_files, session_info])
                delete_btn.click(on_session_delete, inputs=[session_state],
                                 outputs=[chatbot, session_state, output_files, session_info, session_dropdown])

            # ── Tab 2: Skills 管理 ─────────────────────────────────────────────
            with gr.Tab("🧩 Skills 管理"):
                with gr.Row():

                    with gr.Column(scale=1, min_width=320, visible=False) as admin_col:
                        gr.Markdown("### 上传 Skill")
                        gr.Markdown(
                            "上传后是**待审核**状态，不影响正在使用的 skill；发布后才生效。"
                            "再次上传同名 skill 会新增一个版本，不会覆盖。\n\n"
                            "- **ZIP**：`SKILL.md` + `scripts/` 等，可带一个顶层目录；"
                            "上限 10MB、200 个文件\n"
                            "- **SKILL.md**：单文件，适合无脚本的 skill\n\n"
                            "skill 名以 SKILL.md 里的 `name` 为准。需要额外 Python 包时放一个 "
                            "`requirements.txt`，发布时会为它构建镜像。"
                        )
                        skill_file_input = gr.File(
                            label="上传 ZIP 或 SKILL.md",
                            file_types=[".zip", ".md"],
                        )
                        install_btn = gr.Button("上传", variant="primary")

                        gr.Markdown("---")
                        gr.Markdown("### 审核与版本")
                        version_dropdown = gr.Dropdown(
                            label="未生效的版本（待审核的在前）",
                            choices=[],
                            value=None,
                            # 选项随上传和发布变化；合法性由处理函数自己校验，不依赖框架记住的选项列表
                            allow_custom_value=True,
                        )
                        with gr.Row():
                            publish_btn = gr.Button("发布 / 回滚到此版本", variant="primary")
                            discard_btn = gr.Button("删除此版本", variant="stop")

                        gr.Markdown("---")
                        gr.Markdown("### 卸载 Skill")
                        uninstall_dropdown = gr.Dropdown(
                            label="选择要卸载的 Skill（连同所有版本）",
                            choices=_list_skill_names(),
                            value=None,
                            allow_custom_value=True,
                        )
                        uninstall_btn = gr.Button("卸载 Skill", variant="stop")

                    with gr.Column(scale=2):
                        admin_notice = gr.Markdown("_上传、发布和卸载 skill 需要管理员账号。_", visible=False)
                        skill_status = gr.Markdown("")
                        version_review = gr.Markdown("")
                        gr.Markdown("### 已生效的 Skills")
                        refresh_btn = gr.Button("🔄 刷新列表", size="sm")
                        skills_list = gr.Markdown(_list_skills_md())

                view_outputs = [skill_status, skills_list, uninstall_dropdown, version_dropdown, version_review]
                install_btn.click(on_skill_upload, inputs=[skill_file_input], outputs=view_outputs)
                version_dropdown.change(on_version_select, inputs=[version_dropdown], outputs=[version_review])
                publish_btn.click(on_version_publish, inputs=[version_dropdown], outputs=view_outputs)
                discard_btn.click(on_version_discard, inputs=[version_dropdown], outputs=view_outputs)
                uninstall_btn.click(on_skill_uninstall, inputs=[uninstall_dropdown], outputs=view_outputs)
                refresh_btn.click(on_skill_refresh, outputs=view_outputs)

        demo.load(on_page_load, outputs=[admin_col, admin_notice, version_dropdown, session_dropdown])

    return demo


def _start_sweeper():
    """后台线程：定期回收空闲会话的沙箱和工作副本，删除过期会话。"""
    import threading

    def loop():
        while True:
            time.sleep(15)
            try:
                service.sweep()
            except Exception:
                import logging
                logging.getLogger("agent.web").exception("session sweep failed")

    threading.Thread(target=loop, daemon=True, name="session-sweeper").start()


def main():
    global SKILLS_DIR, service
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=7860)
    parser.add_argument("--share", action="store_true")
    parser.add_argument("--skills-dir", type=Path, default=None, help="覆盖默认 skills 目录")
    args = parser.parse_args()

    if args.skills_dir:
        SKILLS_DIR = args.skills_dir.resolve()

    db.get_engine()           # 连接数据库并升级表结构
    auth.check_startup()
    service = SessionService(skills_dir=SKILLS_DIR)
    if config.WORKER_MODE == "embedded":
        # 进程内的 worker：开发和单机部署时不用另起进程。
        # WORKER_MODE=external 时由 `python -m core.worker` 执行，本进程不碰 Docker
        from core.worker import Worker
        if config.USE_DOCKER:
            from core.executor import cleanup_orphans
            cleanup_orphans()  # 上次进程崩溃后遗留的沙箱容器
        service.startup()
        Worker(service).start()
    _start_sweeper()

    launch_kwargs = {}
    if not auth.auth_disabled():
        launch_kwargs.update(auth=auth.authenticate_login, auth_message="用户名填账号名，密码填 token")
    create_app().launch(server_port=args.port, share=args.share, **launch_kwargs)


if __name__ == "__main__":
    main()
