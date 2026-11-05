import ast
from pathlib import Path


def test_referral_handler_declares_settings_dependency():
    source = Path("app/handlers/user.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    handler = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "referral_handler"
    )
    arguments = {arg.arg for arg in handler.args.args + handler.args.kwonlyargs}
    assert "settings" in arguments
