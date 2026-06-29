import os
import logging
from jinja2 import Environment, FileSystemLoader, TemplateNotFound, select_autoescape

import config
from graph_client import UserData

log = logging.getLogger(__name__)

_env: Environment | None = None


def _get_env() -> Environment:
    global _env
    if _env is None:
        _env = Environment(
            loader=FileSystemLoader(config.TEMPLATE_DIR),
            autoescape=select_autoescape(["html"]),
        )
    return _env


def _reload_env() -> Environment:
    global _env
    _env = None
    return _get_env()


def _resolve_template_names(template_name: str | None) -> tuple[str, str]:
    """Return (html_filename, txt_filename) for the given template name."""
    if not template_name or template_name == "default":
        return "signature.html", "signature.txt"
    return f"{template_name}.html", f"{template_name}.txt"


def render(user: UserData, template_name: str | None = None) -> tuple[str, str]:
    env = _get_env()
    ctx = {
        "user": user,
        "custom": user.custom,
    }

    html_file, txt_file = _resolve_template_names(template_name)

    # HTML template — fall back to signature.html if named template not found
    try:
        html = env.get_template(html_file).render(**ctx)
    except TemplateNotFound:
        if html_file != "signature.html":
            log.warning("Template %s not found, falling back to signature.html", html_file)
            try:
                html = env.get_template("signature.html").render(**ctx)
            except TemplateNotFound:
                html = ""
            except Exception as exc:
                log.error("Error rendering HTML signature fallback: %s", exc)
                html = ""
        else:
            log.warning("signature.html not found, using empty HTML signature")
            html = ""
    except Exception as exc:
        log.error("Error rendering HTML signature: %s", exc)
        html = ""

    # Plaintext template — fall back to signature.txt if named template not found
    try:
        txt = env.get_template(txt_file).render(**ctx)
    except TemplateNotFound:
        if txt_file != "signature.txt":
            log.warning("Template %s not found, falling back to signature.txt", txt_file)
            try:
                txt = env.get_template("signature.txt").render(**ctx)
            except TemplateNotFound:
                txt = ""
            except Exception as exc:
                log.error("Error rendering plaintext signature fallback: %s", exc)
                txt = ""
        else:
            log.warning("signature.txt not found, using empty plaintext signature")
            txt = ""
    except Exception as exc:
        log.error("Error rendering plaintext signature: %s", exc)
        txt = ""

    return html, txt


def list_templates() -> list[str]:
    """Return sorted list of available template names (always includes 'default')."""
    import os
    names: set[str] = set()
    try:
        for fname in os.listdir(config.TEMPLATE_DIR):
            if fname.endswith(".html") and fname != "signature.html":
                names.add(fname[:-5])
    except OSError:
        pass
    names.discard("default")
    return ["default"] + sorted(names)
