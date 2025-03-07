from collections import namedtuple
import mimetypes
import os
from pathlib import Path
import signal
import sys
import time
import tempfile

from PIL import PngImagePlugin, Image

import gradio as gr
import gradio.routes
import gradio.utils


import tab_main, tab_settings, cmd_args, settings, utilities, logger, launch, paths
from shared_state import state

import gr_hijack

# ================================================================
# brought from AUTOMATIC1111/stable-diffusion-webui and modified


mimetypes.init()
mimetypes.add_type("application/javascript", ".js")

Savedfile = namedtuple("Savedfile", ["name"])
GradioTemplateResponseOriginal = gradio.routes.templates.TemplateResponse
git = "git"
stored_commit_hash = None
interface = None


def cleanup_tmpdr():
    if not state.temp_dir or not state.temp_dir.is_dir():
        return

    for p in state.temp_dir.glob("**/*.png"):
        if p.is_file():
            os.remove(p)


def register_tmp_file(gradio: gr.Blocks, filename):
    if hasattr(gradio, "temp_file_sets"):  # gradio >=3.15
        gradio.temp_file_sets[0] = gradio.temp_file_sets[0] | {
            os.path.abspath(filename)
        }


def save_pil_to_file(pil_image: Image.Image):
    already_saved_as = getattr(pil_image, "already_saved_as", None)
    if already_saved_as and os.path.isfile(already_saved_as):
        register_tmp_file(interface, already_saved_as)

        file_obj = Savedfile(already_saved_as)
        return file_obj

    tmpdir = state.temp_dir
    use_metadata = False
    metadata = PngImagePlugin.PngInfo()
    for key, value in pil_image.info.items():
        if isinstance(key, str) and isinstance(value, str):
            metadata.add_text(key, value)
            use_metadata = True

    if tmpdir:
        if not tmpdir.is_dir():
            tmpdir.mkdir(parents=True)
        file_obj = tempfile.NamedTemporaryFile(delete=False, suffix=".png", dir=tmpdir)
    else:
        file_obj = tempfile.NamedTemporaryFile(delete=False, suffix=".png")
    pil_image.save(file_obj, pnginfo=(metadata if use_metadata else None))
    return file_obj


# override save to file function so that it also writes PNG info
gr.processing_utils.save_pil_to_file = save_pil_to_file


def webpath(fn: Path):
    path = str(fn.absolute()).replace("\\", "/")
    return f"file={path}?{os.path.getmtime(fn)}"

def read_localization():
    with open(utilities.base_dir_path() / "javascript" / "zh-Hans.json", "r", encoding="utf-8") as f:
        return f.read()

def javascript_html():
    head = ""
    if cmd_args.opts.localization == "zh-Hans":
        head += f'<script type="text/javascript">var localization = {read_localization()}</script>\n'

    js_path = utilities.base_dir_path() / "javascript"

    for p in sorted(js_path.glob("*.js")):
        if not p.is_file():
            continue
        head += f'<script type="text/javascript" src="{webpath(p)}"></script>\n'

    return head


def css_html():
    css_path = utilities.base_dir_path() / "css"
    head = ""

    for p in sorted(css_path.glob("*.css")):
        if not p.is_file():
            continue
        head += f'<link rel="stylesheet" property="stylesheet" href="{webpath(p)}">'

    return head


def reload_javascript():
    js = javascript_html()
    css = css_html()

    def template_response(*args, **kwargs):
        res = GradioTemplateResponseOriginal(*args, **kwargs)
        res.body = res.body.replace(b"</head>", f"{js}</head>".encode("utf8"))
        res.body = res.body.replace(b"</body>", f"{css}</body>".encode("utf8"))
        res.init_headers()
        return res

    gradio.routes.templates.TemplateResponse = template_response


def commit_hash():
    global stored_commit_hash

    if stored_commit_hash is not None:
        return stored_commit_hash

    try:
        command = f'cd "{utilities.base_dir()}" & {git} rev-parse HEAD'
        result = launch.run(command)
        stored_commit_hash = result.stdout.decode("utf-8").strip()
    except Exception:
        stored_commit_hash = "<none>"

    return stored_commit_hash


def versions_html():
    # import torch

    python_version = ".".join([str(x) for x in sys.version_info[0:3]])
    commit = commit_hash()
    short_commit = commit[0:8]

    return f"""
python: <span title="{sys.version}">{python_version}</span>
 • 
gradio: {gr.__version__}
 • 
commit: <a href="https://github.com/toshiaki1729/dataset-tag-editor-standalone/commit/{commit}">{short_commit}</a>
"""


def create_ui():
    reload_javascript()

    with gr.Blocks(analytics_enabled=False, title="Dataset Tag Editor") as gui:
        with gr.Tab("Main"):
            tab_main.on_ui_tabs()
        with gr.Tab("Settings"):
            tab_settings.on_ui_tabs()

        gr.Textbox(elem_id="ui_created", value="", visible=False)

        footer = f'<div class="versions">{versions_html()}</div>'
        gr.HTML(footer, elem_id="footer")
    return gui


def wait_on_server():
    global interface
    while True:
        time.sleep(0.5)
        if state.need_restart:
            state.need_restart = False
            if interface:
                time.sleep(0.25)
                interface.close()
                time.sleep(0.25)
            break


# ================================================================


def main():
    global interface

    def sigint_handler(sig, frame):
        os._exit(0)

    signal.signal(signal.SIGINT, sigint_handler)

    while True:
        state.begin()

        settings.load()
        paths.initialize()

        state.temp_dir = (utilities.base_dir_path() / "temp").absolute()
        if settings.current.use_temp_files and settings.current.temp_directory != "":
            state.temp_dir = Path(settings.current.temp_directory)

        if settings.current.cleanup_tmpdir:
            cleanup_tmpdr()

        interface = create_ui().queue(64)

        app, _, _ = interface.launch(
            server_port=cmd_args.opts.port,
            server_name=cmd_args.opts.server_name,
            share=cmd_args.opts.share,
            auth=[tuple(cred.split(":")) for cred in cmd_args.opts.auth]
            if cmd_args.opts.auth
            else None,
            ssl_keyfile=cmd_args.opts.tls_key,
            ssl_certfile=cmd_args.opts.tls_cert,
            debug=cmd_args.opts.gradio_debug,
            prevent_thread_lock=True,
            quiet=True,
            root_path=cmd_args.opts.root_path
        )

        # Disable a very open middleware as Stable Diffusion web UI does
        app.user_middleware = [
            x for x in app.user_middleware if x.cls.__name__ != "CORSMiddleware"
        ]

        wait_on_server()
        logger.write("Restarting UI...")
