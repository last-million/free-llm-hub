"""app.py's module logger is `_log`, never `log` -- a plain `log.info(...)`
compiles fine (it is only resolved at CALL time) and then crashes the very
first time that line actually runs.

Found the hard way: `log.info(...)` in the startup port-sweep code (added
earlier the same day) crashed EVERY clean `python app.py` with NameError,
before the socket was even bound. Every "successful restart" in between was
quietly talking to an OLDER process from before that line landed -- which is
also why seven hub processes turned up running at once. A second `log.warning`
sat inside a request handler, which Flask would have turned into a bare 500
the first time that except branch fired.

Caught by `python -c "import ast"` scanning for a bare `log.<call>` name, not
by `python -c "import app"` -- import succeeds either way, since the NameError
only fires when the line actually executes.
"""
import ast
import os


APP_PY = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.py")


def test_app_py_never_calls_the_undefined_name_log():
    tree = ast.parse(open(APP_PY, encoding="utf-8").read(), filename="app.py")
    offenders = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                and node.value.id == "log"):
            offenders.append(getattr(node, "lineno", "?"))
    assert not offenders, (
        "app.py's logger is `_log` -- these lines call the undefined name "
        "`log` instead, at line(s): %s" % offenders)
