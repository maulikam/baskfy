"""SW10: Track C §4 and §6 as source-level theorems over the web hub, the API and the worker.

`docs/swing/02` Track C:

* §4 **No web-app orders.** "`apps/web` gets no route under `/swing` that can reach the
  gateway." `lib/swing/__tests__/read-only.test.ts` asserts that from inside the web app; this
  file asserts it from the Python side — over *every* `.ts`/`.tsx` under both directories,
  counted, with comments stripped so an explanation of why the page cannot trade is not what
  fails the build — because a vitest suite that is skipped, deleted or scoped down leaves the
  API's tree with no witness. Two witnesses, one per side of the wire, the SW4 pattern.
* §6 **No multi-tenant.** "Every `sw_` row carries `user_id`." Three angles: every `sw_` table
  in the ORM has a NOT NULL `user_id`; every place the API or the worker *constructs* an `sw_`
  row — `SwX(...)`, `insert(SwX).values(...)`, raw SQL — names `user_id`; and the only id the
  jobs write for is `BASKFY_SOLE_USER_ID`, never a literal, never a form field.

Plus the structural half of §3/§4 on this side: nothing in `baskfy_api` imports the execution
package, so no future route can reach `OrderGateway` by accident.

THE SCANS ARE OVER CODE, NOT PROSE
----------------------------------
Python is read through `ast` and TypeScript through a small comment stripper that respects
string literals. A docstring saying "nothing here calls place_order" is exactly the kind of
sentence these modules should carry, and a scan that failed on it would train people to delete
the explanation rather than keep the rule. The test files under `__tests__` name the banned
words on purpose — they are the vitest's own vocabulary — so they are counted and held to the
narrower rule that a *test* must satisfy: it imports nothing that executes and posts nothing.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import pytest
from sqlalchemy import Table

from baskfy_core.models import Base

REPO: Final = Path(__file__).resolve().parents[3]
WEB_SRC: Final = REPO / "apps" / "web" / "src"
SWING_PAGES: Final = WEB_SRC / "app" / "(app)" / "swing"
SWING_LIB: Final = WEB_SRC / "lib" / "swing"
PYTHON_ROOTS: Final = (
    REPO / "services" / "api" / "src",
    REPO / "services" / "worker" / "src",
)

#: Words that, in a route's code, mean an order path is one edit away. Lower-cased.
BANNED_IN_ROUTES: Final[tuple[str, ...]] = (
    "/swing/execute",
    "/swing/rearm",
    "/execute",
    "place_order",
    "placeorder",
    "place_gtt",
    "delete_gtt",
    "kiteconnect",
    "ordergateway",
    "baskfy_execution",
    "confirm=true",
    'confirm: "true"',
    'method: "put"',
)

#: `05` §2 (as amended by SW14 and STANDING-ANSWERS A14): the hub's ONLY server actions. A file
#: marked "use server" may export exactly these names and nothing else; every other file may not
#: be a server action at all. Each is a money-free write `02` Track A permits.
ALLOWED_ACTIONS: Final[frozenset[str]] = frozenset(
    {"watchAdd", "watchDismiss", "watchAnnotate", "watchReconfirm", "settingsSave"}
)

#: The one file that may send a non-GET, and the only paths it may send one to — the routes
#: `test_swing_readonly.py` whitelists as writes that move no money.
WRITE_HELPER: Final = "write.ts"
WRITE_WHITELIST: Final[frozenset[str]] = frozenset(
    {"/swing/watch", "/swing/watch/{id}", "/swing/config"}
)

#: An import specifier containing any of these reaches, or could reach, an order path.
BANNED_IMPORT_FRAGMENTS: Final[tuple[str, ...]] = (
    "execution",
    "gateway",
    "kite",
    "broker",
    "order",
    "lib/desk",
)

#: The read endpoints the hub may name — the same whitelist as the vitest, by design.
READ_WHITELIST: Final[frozenset[str]] = frozenset(
    {
        "/swing/setups",
        "/swing/sectors",
        "/swing/market",
        "/swing/config",
        "/swing/watch",
        "/swing/positions",
        "/swing/journal",
        # SW14: yesterday's verdicts under the watchlist rows. A read of what the monitor
        # raised, never a way to raise one.
        "/swing/signals",
    }
)

#: The environment variable that names the one tenant (`02` Track C §6).
SOLE_USER_ENV: Final = "BASKFY_SOLE_USER_ID"


# --- the web tree ----------------------------------------------------------------------


def web_files() -> list[Path]:
    """Every TypeScript file under the two swing directories, tests included."""
    found: list[Path] = []
    for root in (SWING_PAGES, SWING_LIB):
        found.extend(path for path in root.rglob("*") if path.suffix in {".ts", ".tsx"})
    return sorted(found)


def is_test_file(path: Path) -> bool:
    return "__tests__" in path.parts or path.name.endswith((".test.ts", ".test.tsx"))


@dataclass(frozen=True, slots=True)
class Token:
    """One lexical piece of a TypeScript file: code, a string, a regex literal or a comment."""

    kind: str
    text: str


#: What may precede a `/` that opens a regex literal rather than a division. The heuristic
#: every parser-less JavaScript lexer uses; the swing tree has no division after a `)`.
_REGEX_MAY_FOLLOW: Final = frozenset("(,=:[!&|?{};+-*%<>~^\n")


def _string_end(source: str, start: int) -> int:
    """Index just past the closing quote of the literal opening at ``start``."""
    quote = source[start]
    end = start + 1
    while end < len(source) and source[end] != quote:
        end += 2 if source[end] == "\\" else 1
    return min(end + 1, len(source))


def _regex_end(source: str, start: int) -> int:
    """Index just past the closing `/` of the regex literal opening at ``start``."""
    end = start + 1
    in_class = False
    while end < len(source) and (source[end] != "/" or in_class):
        if source[end] == "\\":
            end += 2
            continue
        if source[end] == "[":
            in_class = True
        elif source[end] == "]":
            in_class = False
        elif source[end] == "\n":
            break
        end += 1
    return min(end + 1, len(source))


def _comment_end(source: str, start: int) -> int:
    """Index just past the comment opening at ``start`` (`//` to end of line, `/*` to `*/`)."""
    if source.startswith("//", start):
        end = source.find("\n", start)
        return len(source) if end == -1 else end
    end = source.find("*/", start + 2)
    return len(source) if end == -1 else end + 2


def tokenize_ts(source: str) -> list[Token]:
    """A small lexer: enough of TypeScript to tell code from strings, regexes and comments.

    A state machine rather than a regex, for the same reason the desk's SQL translator is one:
    `"https://"` inside a string is not a comment, `/"[^"]+"/` inside a regex is not a string,
    and a regex that knows both is a state machine spelled worse.
    """
    tokens: list[Token] = []
    code: list[str] = []
    index = 0

    def flush() -> None:
        if code:
            tokens.append(Token("code", "".join(code)))
            code.clear()

    def last_code_char() -> str:
        for char in reversed(code):
            if not char.isspace():
                return char
        for token in reversed(tokens):
            if token.kind == "code" and token.text.strip():
                return token.text.rstrip()[-1]
        return "\n"

    while index < len(source):
        char = source[index]
        pair = source[index : index + 2]
        if char in {'"', "'", "`"}:
            kind, end = "string", _string_end(source, index)
        elif pair in {"//", "/*"}:
            kind, end = "comment", _comment_end(source, index)
        elif char == "/" and last_code_char() in _REGEX_MAY_FOLLOW:
            kind, end = "regex", _regex_end(source, index)
        else:
            code.append(char)
            index += 1
            continue
        flush()
        tokens.append(Token(kind, source[index:end]))
        index = end
    flush()
    return tokens


def strip_ts_comments(source: str) -> str:
    """The code without its `//` and `/* */` comments; strings and regexes left intact."""
    return "".join(token.text for token in tokenize_ts(source) if token.kind != "comment")


def blank_ts_strings(code: str) -> str:
    """The code with the *contents* of every string and regex literal removed — the quotes
    stay, so what is left is what the file *does* rather than what it says."""
    out: list[str] = []
    for token in tokenize_ts(code):
        if token.kind == "string":
            out.append(token.text[0] * 2)
        elif token.kind == "regex":
            out.append("//")
        elif token.kind == "code":
            out.append(token.text)
    return "".join(out)


_IMPORT_SPECIFIER: Final = re.compile(r"""(?:from|import)\s*\(?\s*["']([^"']+)["']""")


def import_specifiers(code: str) -> list[str]:
    return _IMPORT_SPECIFIER.findall(code)


class TestTheWebHubCannotReachAnOrder:
    def test_every_file_under_both_swing_directories_is_scanned_and_counted(self) -> None:
        """A scan over nothing passes silently; this one says what it read."""
        files = web_files()
        pages = [path for path in files if SWING_PAGES in path.parents]
        library = [path for path in files if SWING_LIB in path.parents]
        assert len(pages) >= 5, f"the hub has fewer pages than SW4-SW8 built: {pages}"
        assert len(library) >= 2, f"lib/swing is thinner than expected: {library}"
        routes = [path for path in files if not is_test_file(path)]
        tests = [path for path in files if is_test_file(path)]
        assert len(routes) >= 7, f"expected the seven route/lib files, found {routes}"
        assert tests, "the vitest witness is gone; the API side is now the only one"
        assert len(routes) + len(tests) == len(files)

    @pytest.mark.parametrize(
        "path", [p for p in web_files() if not is_test_file(p)], ids=lambda p: p.name
    )
    def test_no_route_under_swing_imports_execution_or_calls_execute(self, path: Path) -> None:
        code = strip_ts_comments(path.read_text(encoding="utf-8"))
        lowered = code.lower()
        for word in BANNED_IN_ROUTES:
            assert word not in lowered, f"{path.relative_to(REPO)} names {word!r} in its code"
        for specifier in import_specifiers(code):
            for fragment in BANNED_IMPORT_FRAGMENTS:
                assert fragment not in specifier.lower(), (
                    f"{path.relative_to(REPO)} imports {specifier!r}, which reaches {fragment}"
                )
        # Writes: only the named server actions, only through the one write helper, only to
        # the money-free routes. A form is fine — a form that reaches an order is not, and the
        # bans above plus these two allow-lists are what make that structural.
        if path.name == WRITE_HELPER:
            for target in re.findall(r"\"(/swing/[^\"]*)\"", code):
                assert target in WRITE_WHITELIST, f"{WRITE_HELPER} writes to {target}"
        else:
            for verb in ('method: "post"', 'method: "patch"', 'method: "delete"'):
                assert verb not in lowered, f"{path.relative_to(REPO)} sends {verb} itself"
        if '"use server"' in code:
            exported = set(re.findall(r"export\s+async\s+function\s+(\w+)", code))
            assert exported and exported <= ALLOWED_ACTIONS, (
                f"{path.relative_to(REPO)} declares server actions {sorted(exported)}; "
                f"05 §2 allows only {sorted(ALLOWED_ACTIONS)}"
            )

    @pytest.mark.parametrize(
        "path", [p for p in web_files() if is_test_file(p)], ids=lambda p: p.name
    )
    def test_the_test_files_under_swing_reach_no_order_path_either(self, path: Path) -> None:
        """A test may *name* the banned words — that is its job — but it may not import an
        execution module or post anything, because a test file is still code that runs."""
        code = strip_ts_comments(path.read_text(encoding="utf-8"))
        for specifier in import_specifiers(code):
            for fragment in BANNED_IMPORT_FRAGMENTS:
                assert fragment not in specifier.lower(), (
                    f"{path.relative_to(REPO)} imports {specifier!r}, which reaches {fragment}"
                )
        # With every string literal blanked, what is left is what the test *does*: a global
        # `fetch(` it wires up, a server action it declares. Neither may appear.
        doing = blank_ts_strings(code)
        assert not re.search(r"\bfetch\s*\(", doing), f"{path.relative_to(REPO)} calls fetch"
        assert "use server" not in doing

    def test_every_fetch_path_in_the_hub_is_on_the_read_whitelist(self) -> None:
        """The same whitelist the vitest keeps, checked from this side: a path added to the
        fetch helper has to be added here too, by hand, with a diff on it."""
        fetcher = strip_ts_comments((SWING_LIB / "fetch.ts").read_text(encoding="utf-8"))
        paths = re.findall(r"readOrNull<[^>]*>\(\s*\"([^\"]+)\"", fetcher)
        assert paths, "fetch.ts reads nothing; the regex or the helper changed shape"
        for path in paths:
            assert path in READ_WHITELIST, f"{path} is not a read the hub may make"
        assert not re.search(r"fetch\([^)]*method", fetcher, re.I)

    def test_the_write_helper_reaches_only_the_money_free_routes(self) -> None:
        writer = strip_ts_comments((SWING_LIB / WRITE_HELPER).read_text(encoding="utf-8"))
        targets = set(re.findall(r"\"(/swing/[^\"]*)\"", writer))
        assert targets, f"{WRITE_HELPER} names no route; the regex or the helper changed shape"
        stray = sorted(targets - WRITE_WHITELIST)
        assert not stray, f"{WRITE_HELPER} reaches {stray}"
        assert "/swing/execute" not in writer and "/desk/" not in writer

    def test_the_comment_stripper_keeps_strings_and_drops_comments(self) -> None:
        """The scan is only as honest as its stripper, so the stripper has a test."""
        source = (
            '// place_order in a line comment\nconst a = "https://x/y"; /* /execute */ '
            "const b = 'it\\'s'; const c = `tpl // not a comment`;"
        )
        stripped = strip_ts_comments(source)
        assert "place_order" not in stripped
        assert "/execute" not in stripped
        assert '"https://x/y"' in stripped
        assert "'it\\'s'" in stripped
        assert "`tpl // not a comment`" in stripped
        assert blank_ts_strings('fetch("x"); const y = \'method: "POST"\';') == (
            "fetch(\"\"); const y = '';"
        )
        regex = 'const m = s.match(/readOrNull<[^>]*>\\(\\s*"([^"]+)"/g); const z = "after";'
        assert blank_ts_strings(regex) == 'const m = s.match(//g); const z = "";'
        assert strip_ts_comments(regex) == regex
        assert strip_ts_comments("const half = a / b; // note") == "const half = a / b; "


# --- the API cannot import the order path --------------------------------------------------


def python_files(root: Path) -> Iterator[Path]:
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" not in path.parts:
            yield path


def python_code_only(source: str) -> str:
    """The module with every docstring removed, so prose about the rule cannot fail the rule."""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree)


#: The execution package's *shapes and ids* — pure modules the API may read (a holding row's
#: normalisation, the tenant pair, the client-id mint). Everything else in `baskfy_execution` —
#: the gateway, the GTT path, the broker adapters, the guards — is the order path, and the API
#: service may not import it.
EXECUTION_SHAPES_THE_API_MAY_IMPORT: Final[frozenset[str]] = frozenset(
    {
        "baskfy_execution",
        "baskfy_execution.broker_ports",
        "baskfy_execution.tenancy",
        "baskfy_execution.client_ids",
    }
)
#: What the package root may be asked for by name; anything else from it is the order path.
EXECUTION_ROOT_NAMES_THE_API_MAY_IMPORT: Final[frozenset[str]] = frozenset(
    {"TenantIds", "mint_client_id", "refuse_cross_tenant"}
)


class TestTheApiServiceHasNoPathToTheGateway:
    def test_nothing_in_the_api_service_imports_the_order_path(self) -> None:
        """Track C §4, structurally: a router cannot reach what its package never imports.

        `baskfy_api` does import three pure pieces of `baskfy_execution` (the holding-row shape
        for the broker sync, the tenant pair and the client-id mint for the plan store); those
        are listed, and the gateway, the GTT path, the adapters and the guards are not."""
        offenders: list[str] = []
        for path in python_files(REPO / "services" / "api" / "src"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name.startswith(("baskfy_execution", "kiteconnect", "app.")):
                            offenders.append(f"{path.relative_to(REPO)}: import {alias.name}")
                elif isinstance(node, ast.ImportFrom) and node.module:
                    module = node.module
                    if module.startswith(("kiteconnect", "app.")):
                        offenders.append(f"{path.relative_to(REPO)}: from {module}")
                    elif module.startswith("baskfy_execution"):
                        if module not in EXECUTION_SHAPES_THE_API_MAY_IMPORT:
                            offenders.append(f"{path.relative_to(REPO)}: from {module}")
                        elif module == "baskfy_execution":
                            offenders.extend(
                                f"{path.relative_to(REPO)}: from baskfy_execution import {a.name}"
                                for a in node.names
                                if a.name not in EXECUTION_ROOT_NAMES_THE_API_MAY_IMPORT
                            )
        assert offenders == []

    def test_the_api_source_never_names_a_placing_verb(self) -> None:
        """Over the code (docstrings stripped), so the explanations may stay."""
        offenders: list[str] = []
        for path in python_files(REPO / "services" / "api" / "src"):
            code = python_code_only(path.read_text(encoding="utf-8"))
            for word in ("OrderGateway", "place_order", "place_gtt", ".place(", "kiteconnect"):
                if word in code:
                    offenders.append(f"{path.relative_to(REPO)} names {word}")
        assert offenders == []


# --- every sw_ write carries the sole user --------------------------------------------------


def sw_tables() -> list[Table]:
    return sorted(
        (table for table in Base.metadata.tables.values() if table.name.startswith("sw_")),
        key=lambda table: table.name,
    )


def sw_class_names() -> frozenset[str]:
    """The ORM class behind every `sw_` table, by name."""
    tables = set(sw_tables())
    return frozenset(
        mapper.class_.__name__
        for mapper in Base.registry.mappers
        if isinstance(mapper.local_table, Table) and mapper.local_table in tables
    )


@dataclass(frozen=True, slots=True)
class WriteSite:
    """One place the code constructs or inserts an `sw_` row."""

    where: str
    kind: str
    carries_user_id: bool


def _name_of(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _joined_text(node: ast.expr) -> str:
    """The literal text of a string constant or an f-string, placeholders elided."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            part.value
            for part in node.values
            if isinstance(part, ast.Constant) and isinstance(part.value, str)
        )
    return ""


def _enclosing_functions(tree: ast.Module) -> dict[int, ast.FunctionDef | ast.AsyncFunctionDef]:
    """Node id → the innermost function it sits in."""
    owner: dict[int, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    for function in ast.walk(tree):
        if isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef):
            for inner in ast.walk(function):
                owner[id(inner)] = function
    return owner


def _function_names_user_id(function: ast.FunctionDef | ast.AsyncFunctionDef | None) -> bool:
    if function is None:
        return False
    return any(
        isinstance(node, ast.Constant) and node.value == "user_id" for node in ast.walk(function)
    )


def write_sites(path: Path, sw_names: frozenset[str]) -> list[WriteSite]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    owner = _enclosing_functions(tree)
    where = str(path.relative_to(REPO))
    sites: list[WriteSite] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            target = _name_of(node.func)
            if target in sw_names:
                sites.append(
                    WriteSite(
                        where=f"{where}:{node.lineno}",
                        kind=f"{target}(...)",
                        carries_user_id=any(k.arg == "user_id" for k in node.keywords),
                    )
                )
            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "values"
                and isinstance(node.func.value, ast.Call)
                and _name_of(node.func.value.func) == "insert"
                and node.func.value.args
                and _name_of(node.func.value.args[0]) in sw_names
            ):
                table = _name_of(node.func.value.args[0])
                by_keyword = any(k.arg == "user_id" for k in node.keywords)
                by_payload = bool(node.args) and _function_names_user_id(owner.get(id(node)))
                sites.append(
                    WriteSite(
                        where=f"{where}:{node.lineno}",
                        kind=f"insert({table}).values(...)",
                        carries_user_id=by_keyword or by_payload,
                    )
                )
        if isinstance(node, ast.Constant | ast.JoinedStr):
            text = _joined_text(node)
            if re.search(r"\b(INSERT\s+INTO|UPDATE)\s+sw_", text, re.I):
                sites.append(
                    WriteSite(
                        where=f"{where}:{node.lineno}",
                        kind="raw SQL",
                        carries_user_id="user_id" in text,
                    )
                )
    return sites


def all_write_sites() -> list[WriteSite]:
    names = sw_class_names()
    found: list[WriteSite] = []
    for root in PYTHON_ROOTS:
        for path in python_files(root):
            found.extend(write_sites(path, names))
    return found


class TestEverySwWriteCarriesTheSoleUserId:
    def test_every_sw_table_has_a_not_null_user_id_column(self) -> None:
        """The schema's half of Track C §6: a row without a tenant cannot exist."""
        tables = sw_tables()
        assert len(tables) >= 12, f"expected the twelve SW2 tables at least, found {len(tables)}"
        assert len(sw_class_names()) == len(tables), "an sw_ table without an ORM class"
        for table in tables:
            columns = {column.name: column for column in table.columns}
            assert "user_id" in columns, f"{table.name} has no user_id column"
            assert columns["user_id"].nullable is False, f"{table.name}.user_id is nullable"

    def test_every_sw_row_constructed_in_api_and_worker_carries_a_user_id(self) -> None:
        """The code's half: every `SwX(...)`, every `insert(SwX).values(...)` and every raw
        `INSERT INTO sw_` / `UPDATE sw_` names `user_id`. Counted, so a refactor that moved the
        writes somewhere this scan does not look would show as a falling number."""
        sites = all_write_sites()
        assert len(sites) >= 12, f"only {len(sites)} write sites found; the scan lost its subject"
        offenders = [f"{site.where} {site.kind}" for site in sites if not site.carries_user_id]
        assert offenders == [], f"sw_ writes without a user_id: {offenders}"

    def test_the_scan_sees_the_writes_it_is_supposed_to_see(self) -> None:
        """The two shapes that are easy to miss — a bulk `values(batch)` and a plain
        constructor — are both in the census, by file."""
        kinds = {(site.where.split(":")[0], site.kind) for site in all_write_sites()}
        assert any(
            path.endswith("tasks/swing.py") and kind.startswith("insert(SwSetupDaily)")
            for path, kind in kinds
        ), "the detectors' bulk upsert was not seen"
        assert any(
            path.endswith("tasks/swing_eod.py") and kind == "SwPlan(...)" for path, kind in kinds
        ), "the evening's plan writer was not seen"
        assert any(
            path.endswith("swing_watch.py") and kind == "SwWatch(...)" for path, kind in kinds
        ), "the watchlist's writer was not seen"

    def test_the_sole_user_id_comes_from_the_environment_and_never_from_a_literal(self) -> None:
        """`02` Track B/C: the tenant is read once per process from `BASKFY_SOLE_USER_ID`. The
        swing jobs take `user_id` as a parameter and never invent one; the one place that
        resolves it names the variable, and refuses to default it."""
        providers = (REPO / "services/worker/src/baskfy_worker/providers.py").read_text(
            encoding="utf-8"
        )
        assert SOLE_USER_ENV in providers
        assert "swing_user_id=_sole_user_id()" in providers
        for path in python_files(REPO / "services/worker/src/baskfy_worker/tasks"):
            if not path.name.startswith("swing"):
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.keyword) and node.arg == "user_id":
                    assert not isinstance(node.value, ast.Constant), (
                        f"{path.relative_to(REPO)}:{node.value.lineno} passes a literal user_id"
                    )
                if isinstance(node, ast.Attribute) and node.attr == "environ":
                    pytest.fail(
                        f"{path.relative_to(REPO)}:{node.lineno} reads the environment "
                        "itself; the tenant is resolved once, in providers.py"
                    )

    def test_every_api_swing_route_scopes_to_the_sole_user(self) -> None:
        """The API side of §6: every handler in `routers/swing.py` resolves its tenant through
        `scoped_sole_user_id`, which refuses any other principal (SW4/SW5)."""
        source = (REPO / "services/api/src/baskfy_api/routers/swing.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        handlers = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef)
            and node.name.startswith(("get_", "post_", "patch_", "delete_"))
        ]
        assert len(handlers) >= 9
        for handler in handlers:
            calls = {
                _name_of(node.func) for node in ast.walk(handler) if isinstance(node, ast.Call)
            }
            assert "scoped_sole_user_id" in calls, f"{handler.name} does not scope its tenant"
