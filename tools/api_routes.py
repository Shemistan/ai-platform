#!/usr/bin/env python3
"""Справочник HTTP API платформы и моста narrator из кода: docs/api-routes.md.

Маршруты — ветки `case ("GET"|"POST", [...])` в app/platform/aiplatform/server.py
и aiplatform/*/service.py (путь модуля — `/api/<PREFIX>/…`). Разбор через `ast`,
код не импортируется. Описание ветки — комментарий прямо над `case`, иначе первая
строка docstring метода `self.<метод>(…)`, который ветка зовёт. Тело и ответ —
по вызовам `req.json` / `req.body` (`body_to_file`) / `req.arg` / `req.send_json` / `send_file`.
Команды моста — `add_parser` в app/narrator/src/narrator/bridge.py.

Генерируется блок `<!-- gate:begin api-routes -->…<!-- gate:end api-routes -->`;
текст вне блока пишется руками.

    python3 tools/api_routes.py          # пересобрать блок
    python3 tools/api_routes.py --check  # ./check: блок устарел → код 1 и подсказка
"""
import ast
import http
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLATFORM = os.path.join("app", "platform", "aiplatform")
BRIDGE = os.path.join("app", "narrator", "src", "narrator", "bridge.py")
DOC = os.path.join("docs", "api-routes.md")
BLOCK = "api-routes"
METHODS = {"GET", "POST"}


class Route:
    def __init__(self, module, method, path, body, response, about, line):
        self.module, self.method, self.path = module, method, path
        self.body, self.response, self.about, self.line = body, response, about, line


def _str(node):
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _segments(pattern):
    """Шаблон пути из MatchSequence: "x" → x, имя → {имя}, *rest → {rest…}."""
    out = []
    for p in pattern.patterns:
        if isinstance(p, ast.MatchValue) and _str(p.value) is not None:
            out.append(p.value.value)
        elif isinstance(p, ast.MatchAs) and p.name:
            out.append("{%s}" % p.name)
        elif isinstance(p, ast.MatchStar):
            out.append("{%s…}" % (p.name or "path"))
        else:
            out.append("{?}")
    return out


def _route_case(case):
    """(метод, [сегменты, …]) или None, если ветка — не маршрут с явным методом.

    Путь-альтернатива (`["state"] | ["config"]`) даёт несколько путей.
    """
    pat = case.pattern
    if not (isinstance(pat, ast.MatchSequence) and len(pat.patterns) == 2):
        return None
    method, path = pat.patterns
    if not (isinstance(method, ast.MatchValue) and _str(method.value) in METHODS):
        return None
    options = path.patterns if isinstance(path, ast.MatchOr) else [path]
    if not all(isinstance(p, ast.MatchSequence) for p in options):
        return None
    return method.value.value, [_segments(p) for p in options]


def _guard(case):
    """`if name in ("a", "b")` → "name: a | b" — допустимые значения сегмента."""
    g = case.guard
    if (isinstance(g, ast.Compare) and isinstance(g.ops[0], ast.In) and isinstance(g.left, ast.Name)
            and isinstance(g.comparators[0], (ast.Tuple, ast.List, ast.Set))):
        values = [_str(e) for e in g.comparators[0].elts]
        if all(values):
            return "`{%s}`: %s" % (g.left.id, ", ".join(values))
    return ""


def _attr_call(node, owner, names):
    """Вызов owner.<name>(…) с name из names."""
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name) and node.func.value.id == owner
            and node.func.attr in names)


def _status(call):
    for arg in call.args[1:2]:
        if isinstance(arg, ast.Attribute) and arg.attr in http.HTTPStatus.__members__:
            return int(http.HTTPStatus[arg.attr])
    return None


def _describe_body(stmts):
    """(тело, ответ, вызванные self-методы) по операторам ветки."""
    body, query, response, called = [], [], [], []
    for stmt in stmts:
        for node in ast.walk(stmt):
            if _attr_call(node, "req", {"json"}) and "JSON" not in body:
                body.append("JSON")
            elif _attr_call(node, "req", {"body", "body_to_file"}) and "файл" not in body:
                body.append("файл")
            elif _attr_call(node, "req", {"arg"}) and node.args and _str(node.args[0]):
                query.append("?" + node.args[0].value)
            elif (isinstance(node, ast.Compare) and isinstance(node.ops[0], ast.In)
                  and isinstance(node.comparators[0], ast.Attribute)
                  and node.comparators[0].attr == "query" and _str(node.left)):
                query.append("?" + node.left.value)
            elif _attr_call(node, "req", {"send_json"}):
                code = _status(node)
                response.append("JSON" + (" %d" % code if code else ""))
            elif _attr_call(node, "req", {"send_file", "send_bytes", "send_text"}):
                response.append("файл")
            elif _attr_call(node, "self", {"_send_file", "_static"}) or (
                    isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr.startswith("_send")):
                response.append("файл")
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and isinstance(node.func.value, ast.Name) and node.func.value.id == "self":
                called.append(node.func.attr)
    return body + _uniq(query), _uniq(response), called


def _uniq(items):
    return sorted(set(items), key=items.index)


def _comment_above(lines, lineno):
    """Комментарии прямо над строкой lineno (1-based), без «# fmt:»."""
    out = []
    i = lineno - 2
    while i >= 0 and lines[i].strip().startswith("#"):
        text = lines[i].strip().lstrip("#").strip()
        if not text.startswith("fmt:"):
            out.insert(0, text)
        i -= 1
    return " ".join(out)


def _functions(tree):
    out = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name not in out:
            out[node.name] = node
    return out


def _doc(fn):
    doc = ast.get_docstring(fn) if fn is not None else None
    return doc.strip().splitlines()[0].strip() if doc else ""


def _tails(tree):
    """Match → операторы после него в том же блоке: общий ответ веток без return."""
    out = {}
    for node in ast.walk(tree):
        for field in ("body", "orelse", "finalbody"):
            block = getattr(node, field, None)
            if isinstance(block, list):
                for i, stmt in enumerate(block):
                    if isinstance(stmt, ast.Match):
                        out[id(stmt)] = block[i + 1:]
    return out


def _prefix(tree):
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "PREFIX" for t in node.targets):
            return _str(node.value)
    return None


def routes_in(path, module, base):
    with open(path, encoding="utf-8") as f:
        source = f.read()
    tree = ast.parse(source)
    lines = source.splitlines()
    funcs = _functions(tree)
    tails = _tails(tree)
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Match):
            continue
        for case in node.cases:
            parsed = _route_case(case)
            if parsed is None:
                continue
            method, options = parsed
            body, response, called = _describe_body(case.body)
            # Ответ шлёт помощник (`self._create(req, …)`) или общий хвост после match.
            for name in called:
                if not response and name in funcs:
                    more_body, response, _ = _describe_body(funcs[name].body)
                    body = body or more_body
            ends = case.body and isinstance(case.body[-1], (ast.Return, ast.Raise))
            if not response and not ends:
                response = _describe_body(tails.get(id(node), []))[1]
            about = _comment_above(lines, case.pattern.lineno)
            if not about:
                about = next((_doc(funcs.get(n)) for n in called if _doc(funcs.get(n))), "")
            about = "; ".join(x for x in (about.rstrip("."), _guard(case)) if x)
            for segments in options:
                url = "/" + "/".join(base + segments)
                found.append(Route(module, method, url, ", ".join(body) or "—", ", ".join(response) or "—",
                                   about, case.pattern.lineno))
    return found


def collect(root=ROOT):
    """Все маршруты: сначала общие из server.py, затем модули по алфавиту."""
    pkg = os.path.join(root, PLATFORM)
    result = routes_in(os.path.join(pkg, "server.py"), "server", ["api"])
    for name in sorted(os.listdir(pkg)):
        path = os.path.join(pkg, name, "service.py")
        if not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8") as f:
            prefix = _prefix(ast.parse(f.read())) or name
        result += routes_in(path, name, ["api", prefix])
    return result


def bridge_commands(root=ROOT):
    """(команда, аргументы, описание) из add_parser моста narrator."""
    path = os.path.join(root, BRIDGE)
    if not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    fn = next((n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "add_parser"), None)
    if fn is None:
        return []
    out = []

    def args_of(call_chain):
        # b.add_parser(...).add_argument("id") → ["<id>"]; "--x" → ["[--x]"]
        res = []
        node = call_chain
        while isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "add_argument":
            name = _str(node.args[0]) if node.args else None
            if name:
                res.insert(0, "[%s]" % name if name.startswith("-") else "<%s>" % name)
            node = node.func.value
        return res

    def help_of(call):
        return next((_str(k.value) for k in call.keywords if k.arg == "help"), "") or ""

    for stmt in fn.body:
        if isinstance(stmt, ast.For) and isinstance(stmt.iter, ast.Tuple):
            extra = []
            for inner in ast.walk(stmt):
                if isinstance(inner, ast.Expr):
                    extra = args_of(inner.value)
            for item in stmt.iter.elts:
                if isinstance(item, ast.Tuple) and len(item.elts) == 2:
                    out.append((_str(item.elts[0]), extra, _str(item.elts[1]) or ""))
        elif isinstance(stmt, (ast.Expr, ast.Assign)):
            call = stmt.value
            node = call
            while isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and node.func.attr == "add_argument":
                node = node.func.value
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and node.func.attr == "add_parser" and node.args and _str(node.args[0]) != "bridge":
                extra = args_of(call)
                if isinstance(stmt, ast.Assign):  # n = b.add_parser(...); n.add_argument(...)
                    var = stmt.targets[0].id if isinstance(stmt.targets[0], ast.Name) else None
                    for later in fn.body:
                        if isinstance(later, ast.Expr) and _attr_call(later.value, var or "", {"add_argument"}):
                            extra += args_of(later.value)
                out.append((node.args[0].value, extra, help_of(node)))
    return out


def _cell(text):
    return text.replace("|", "\\|")


def render(root=ROOT):
    routes = collect(root)
    out = ["Маршрутов: %d. Собрано `python3 tools/api_routes.py` — руками не править." % len(routes), ""]
    module = None
    for r in routes:
        if r.module != module:
            module = r.module
            src = "server.py" if module == "server" else "%s/service.py" % module
            out += ["", "### %s" % ("Общие" if module == "server" else "`%s`" % module), "",
                    "Код: `app/platform/aiplatform/%s`" % src, "",
                    "| Метод | Путь | Тело / query | Ответ | Что делает |",
                    "|---|---|---|---|---|"]
        out.append("| %s | `%s` | %s | %s | %s |" % (r.method, r.path, _cell(r.body), _cell(r.response),
                                                     _cell(r.about) or "—"))
    commands = bridge_commands(root)
    if commands:
        out += ["", "### Мост `narrate bridge`", "", "Код: `app/narrator/src/narrator/bridge.py`", "",
                "| Команда | Аргументы | Что отдаёт |", "|---|---|---|"]
        for name, extra, about in commands:
            out.append("| `%s` | %s | %s |" % (name, " ".join("`%s`" % a for a in extra) or "—", _cell(about)))
    return "\n".join(out).replace("\n\n\n", "\n\n").strip("\n")


def replace_block(text, content):
    begin, end = "<!-- gate:begin %s -->" % BLOCK, "<!-- gate:end %s -->" % BLOCK
    pattern = re.compile(re.escape(begin) + r".*?" + re.escape(end), re.DOTALL)
    if not pattern.search(text):
        return None
    return pattern.sub(lambda _m: "%s\n%s\n%s" % (begin, content, end), text)


def sync(root=ROOT, check=False):
    """Пересобрать блок (при check — только сверить); код выхода для ./check."""
    path = os.path.join(root, DOC)
    if not os.path.exists(path):
        print("✗ api-routes: нет %s" % DOC, file=sys.stderr)
        return 1
    with open(path, encoding="utf-8") as f:
        old = f.read()
    new = replace_block(old, render(root))
    if new is None:
        print("✗ api-routes: в %s нет маркеров <!-- gate:begin %s -->" % (DOC, BLOCK), file=sys.stderr)
        return 1
    if new == old:
        if not check:
            print("✓ api-routes: %s актуален" % DOC)
        return 0
    if check:
        print("✗ api-routes: %s устарел — маршруты в коде изменились.\n"
              "  Пересобери: python3 tools/api_routes.py && git add %s" % (DOC, DOC), file=sys.stderr)
        return 1
    with open(path, "w", encoding="utf-8") as f:
        f.write(new)
    print("✓ api-routes: %s пересобран" % DOC)
    return 0


def main(argv):
    return sync(ROOT, check="--check" in argv[1:])


if __name__ == "__main__":
    sys.exit(main(sys.argv))
