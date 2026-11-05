"""Repository analyzer (D-183): read a repo and draft the platform's requirement answers.

Deterministic rules over files (manifests, Dockerfiles, source), no LLM and no network. Every value carries the file and
reason that produced it, and every answer that would change the blueprint is put on `confirm` for the developer.
What the code cannot tell (traffic, downtime tolerance, audience, budget) is left for the developer on `ask`.

Usage: python -m agent.repo_analyzer <repo-dir>
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

SKIP_DIRS = {".git", "node_modules", "vendor", ".venv", "venv", "__pycache__", "target", "dist", "test", "tests",
             "testing", "testdata", "e2e", "docs", "doc", "examples", "example", ".devcontainer", "extra", ".github"}
VERSION = "v3"  # v3 (2026-11-05): the rule list fixed in docs/baseline/README.md section 4 (from held-out 2's misses)
# v3: an app often lives in one of these sub-folders (README section 4, the list as written there). Their manifests are
# read after the root's. frontend/ + backend/ together is a screen plus an API.
APP_SUBDIRS = ("backend", "frontend", "complete", "app")
# v3: an env var name that is the port: PORT itself or ending in _PORT... (not REPORT, as in CSP_REPORT_URI).
PORT_VAR = r"(?:[A-Z0-9_]*_)?PORT[A-Z0-9_]*"
MAX_DEPTH = 4
MAX_SOURCE_FILES = 400
# Test and tooling code is not the app: its env vars and ports are not the app's settings (vaultwarden's
# playwright/ MARIADB_PORT). Dockerfiles are still read from these folders (memos ships scripts/Dockerfile).
SOURCE_SKIP = {"playwright", "cypress", "scripts", "tools", "hack", "bench", "benchmark", "benchmarks", "fixtures", "mocks"}

# Ports that are not HTTP (game servers, databases, mail, MQTT, ...): an app exposing only these is out of scope.
NON_HTTP_PORTS = {22, 25, 53, 1883, 3306, 5432, 6379, 9092, 25565, 25575, 27015, 27017, 19132}

NODE_SERVER = {"express", "fastify", "koa", "@hapi/hapi", "hapi", "@nestjs/core", "next", "nuxt", "restify", "polka"}
NODE_FRONTEND = {"react", "vue", "svelte", "@angular/core", "preact", "solid-js"}
NODE_ROUTER = {"react-router", "react-router-dom", "vue-router", "@angular/router", "svelte-routing", "@solidjs/router"}
NODE_REALTIME = {"socket.io", "ws", "sockjs", "graphql-ws", "@socket.io/redis-adapter", "uws", "faye-websocket"}
NODE_UPLOAD = {"multer", "busboy", "formidable", "express-fileupload", "@fastify/multipart"}
NODE_LOCAL_STATE = {"better-sqlite3", "sqlite3", "ueberdb2", "lowdb", "nedb", "@seald-io/nedb", "node-persist"}
NODE_SESSION_STORES = {"connect-redis", "connect-pg-simple", "connect-mongo", "express-mysql-session", "connect-dynamodb"}
NODE_DB = {"pg": "POSTGRES", "postgres": "POSTGRES", "pg-promise": "POSTGRES", "mysql": "OTHER", "mysql2": "OTHER",
           "mongodb": "OTHER", "mongoose": "OTHER", "@aws-sdk/client-dynamodb": "DYNAMODB", "@aws-sdk/lib-dynamodb": "DYNAMODB",
           "better-sqlite3": "OTHER", "sqlite3": "OTHER"}
GO_REALTIME = ("github.com/gorilla/websocket", "nhooyr.io/websocket", "github.com/coder/websocket", "golang.org/x/net/websocket")
GO_DB = {"github.com/lib/pq": "POSTGRES", "github.com/jackc/pgx": "POSTGRES", "github.com/go-sql-driver/mysql": "OTHER",
         "go.mongodb.org/mongo-driver": "OTHER", "github.com/aws/aws-sdk-go-v2/service/dynamodb": "DYNAMODB",
         "modernc.org/sqlite": "OTHER", "github.com/mattn/go-sqlite3": "OTHER"}
GO_LOCAL_STATE = ("modernc.org/sqlite", "github.com/mattn/go-sqlite3", "go.etcd.io/bbolt", "github.com/dgraph-io/badger")
RUST_SERVER = ("actix-web", "axum", "rocket", "warp", "poem", "salvo", "tide")
RUST_REALTIME = ("tokio-tungstenite", "tungstenite", "actix-ws", "actix-web-actors", "rocket_ws", "axum-tungstenite")
RUST_DB = {"postgres": "POSTGRES", "tokio-postgres": "POSTGRES", "rusqlite": "OTHER", "mysql": "OTHER", "mongodb": "OTHER"}
JAVA_DB = {"postgresql": "POSTGRES", "mysql-connector": "OTHER", "mariadb": "OTHER", "h2": "OTHER"}
PY_SERVER = {"flask", "fastapi", "django", "uvicorn", "gunicorn", "starlette", "aiohttp", "tornado", "sanic"}
PY_REALTIME = {"channels", "websockets", "python-socketio", "flask-socketio"}
PY_DB = {"psycopg2": "POSTGRES", "psycopg2-binary": "POSTGRES", "psycopg": "POSTGRES", "asyncpg": "POSTGRES",
         "pymysql": "OTHER", "mysqlclient": "OTHER", "pymongo": "OTHER"}

# What the code cannot tell: the developer always answers these (the engine's non-code inputs).
DEVELOPER_ONLY = ["traffic_scale", "traffic_pattern", "downtime_tolerance", "latency_sensitivity", "exposure", "tier",
                  "budget_usd_month"]


@dataclass
class Finding:
    value: object
    confidence: str  # high | medium | low
    evidence: list[str] = field(default_factory=list)


@dataclass
class Analysis:
    findings: dict[str, Finding]
    unsupported: list[str]   # MULTI_SERVICE | NON_HTTP | PORT_FROM_FLAG_ONLY
    confirm: list[str]       # inferred answers that change the blueprint: the developer must confirm them
    ask: list[str]           # answers the code cannot give

    def value(self, key: str):
        f = self.findings.get(key)
        return f.value if f else None

    def draft(self) -> dict:
        """Requirement draft for the engine (RawInput shape). Developer-only fields stay None until answered."""
        d = {k: None for k in DEVELOPER_ONLY}
        data = self.value("data")
        d.update({
            "app_form": self.value("app_form"),
            "capabilities": {} if self.value("app_form") == "STATIC_SITE" else {
                "realtime_connection": bool(self.value("realtime_connection")),
                "large_payload": bool(self.value("large_payload")),
                "in_memory_state": bool(self.value("in_memory_state")),
                "non_http": bool(self.value("non_http")),
                **({"request_duration": self.value("request_duration")} if self.value("request_duration") else {}),
            },
            "data": {"needed": data not in (None, "NONE"), "detected": data or "NONE"},
            "spa_fallback": bool(self.value("spa_fallback")),
        })
        return d

    def to_dict(self) -> dict:
        return {"findings": {k: asdict(v) for k, v in self.findings.items()}, "unsupported": self.unsupported,
                "confirm": self.confirm, "ask": self.ask, "draft": self.draft()}


# ------------------------------------------------------------------ file collection

def _walk(root: Path):
    stack = [(root, 0)]
    while stack:
        d, depth = stack.pop()
        try:
            entries = sorted(d.iterdir())
        except OSError:
            continue
        for p in entries:
            if p.is_dir():
                if p.name not in SKIP_DIRS and not p.name.startswith(".") and depth < MAX_DEPTH:
                    stack.append((p, depth + 1))
            else:
                yield p, depth


def _read(p: Path, limit: int = 400_000) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")[:limit]
    except OSError:
        return ""


class Repo:
    def __init__(self, root: Path):
        self.root = root
        self.files = [(p, d) for p, d in _walk(root)]

    def rel(self, p: Path) -> str:
        return p.relative_to(self.root).as_posix()

    def named(self, *names: str, max_depth: int = MAX_DEPTH) -> list[Path]:
        return [p for p, d in self.files if p.name in names and d <= max_depth]

    def source(self, *suffixes: str, max_depth: int = 3) -> list[Path]:
        out = [p for p, d in self.files if p.suffix in suffixes and d <= max_depth
               and not p.name.endswith(("_test.go", ".test.js", ".spec.js", ".test.ts", ".spec.ts", ".min.js"))
               and not SOURCE_SKIP & set(p.relative_to(self.root).parts[:-1])]
        return out[:MAX_SOURCE_FILES]


# ------------------------------------------------------------------ rules

def _node_deps(repo: Repo) -> dict[str, str]:
    """name -> package.json path, runtime dependencies of the root and server-side package.json files."""
    deps: dict[str, str] = {}
    for p in repo.named("package.json", max_depth=1):
        try:
            j = json.loads(_read(p))
        except ValueError:
            continue
        for name in (j.get("dependencies") or {}):
            deps.setdefault(name, repo.rel(p))
    return deps


def _node_scripts(repo: Repo) -> dict[str, str]:
    p = repo.root / "package.json"
    try:
        return (json.loads(_read(p)).get("scripts") or {}) if p.exists() else {}
    except ValueError:
        return {}


def _go_modules(repo: Repo) -> tuple[str, str]:
    """Direct requirements only: `// indirect` modules are pulled in by a dependency, not used by the app
    (memos lists gorilla/websocket as indirect and has no WebSocket endpoint)."""
    mods = repo.named("go.mod", max_depth=1)
    if not mods:
        return "", ""
    direct = "\n".join(line for line in _read(mods[0]).splitlines() if "// indirect" not in line)
    return direct, repo.rel(mods[0])


def _cargo(repo: Repo) -> tuple[set[str], str, str]:
    """Rust: dependency names and the raw Cargo.toml (for features such as diesel's "sqlite"/"postgres")."""
    p = repo.root / "Cargo.toml"
    if not p.exists():
        return set(), "", ""
    txt = _read(p)
    deps: set[str] = set()
    section = ""
    for line in txt.splitlines():
        s = line.strip()
        if s.startswith("["):
            section = s
            continue
        m = re.match(r"([A-Za-z0-9_-]+)\s*=", s)
        if m and "dependencies" in section:
            deps.add(m.group(1))
    return deps, txt, "Cargo.toml"


def _fixed_cmd_port(repo: Repo, files: list[Path]) -> str | None:
    """A port pinned in the image's start command (`gunicorn -b 0.0.0.0:80`, `--port 3000`): an env var cannot change it."""
    for f in files:
        for m in re.finditer(r"^\s*(?:CMD|ENTRYPOINT)\s+(.+)$", _read(f), re.M):
            cmd = m.group(1)
            hit = re.search(r'(?:-b|--bind|--listen|--port|-p)["\',\s=]+["\']?(?:[\d.]+|0\.0\.0\.0|\[::\])?:?(\d{2,5})\b', cmd)
            if hit:
                return f"{repo.rel(f)}: start command fixes port {hit.group(1)}"
    return None


def _app_dirs(repo: Repo) -> list[Path]:
    """The root, then the v3 app sub-folders that exist."""
    return [repo.root] + [repo.root / d for d in APP_SUBDIRS if (repo.root / d).is_dir()]


def _java_build(repo: Repo) -> tuple[str, str]:
    for base in _app_dirs(repo):  # v3: complete/pom.xml (spring guides keep the app there)
        for name in ("pom.xml", "build.gradle", "build.gradle.kts"):
            p = base / name
            if p.exists():
                return _read(p), repo.rel(p)
    return "", ""


def _py_deps(repo: Repo) -> tuple[dict[str, str], str]:
    """name -> file. v3: also requirements/*.txt and requirements-*.txt, at the root and in the app sub-folders."""
    deps: dict[str, str] = {}
    for base in _app_dirs(repo):
        files = [base / "requirements.txt", base / "pyproject.toml"]
        files += sorted(base.glob("requirements-*.txt")) + sorted((base / "requirements").glob("*.txt"))
        for p in files:
            if p.is_file():
                for name in re.findall(r"^[\s\"']*([a-z0-9_.-]+)", _read(p).lower(), re.M):
                    deps.setdefault(name, repo.rel(p))
    return deps, next(iter(deps.values()), "")


def _dockerfiles(repo: Repo) -> list[Path]:
    return [p for p, d in repo.files if d <= 2 and (p.name == "Dockerfile" or p.name.lower() == "dockerfile"
                                                     or p.name.startswith("Dockerfile.") or p.suffix == ".dockerfile")]


def _exposed(repo: Repo, files: list[Path]) -> dict[int, str]:
    ports: dict[int, str] = {}
    for f in files:
        for m in re.finditer(r"^\s*EXPOSE\s+([^\n]+)", _read(f), re.M | re.I):
            for tok in m.group(1).split():
                num = re.match(r"(\d+)", tok)
                if num:
                    ports.setdefault(int(num.group(1)), repo.rel(f))
    return ports


def analyze(root: str | Path) -> Analysis:
    repo = Repo(Path(root))
    f: dict[str, Finding] = {}
    unsupported: list[str] = []
    node = _node_deps(repo)
    go_mod, go_mod_file = _go_modules(repo)
    java, java_file = _java_build(repo)
    py, _ = _py_deps(repo)  # name -> file it came from
    dockerfiles = _dockerfiles(repo)
    ports = _exposed(repo, dockerfiles)
    rust, cargo, cargo_file = _cargo(repo)
    go_src = {repo.rel(p): _read(p) for p in repo.source(".go")}
    js_src = {repo.rel(p): _read(p, 200_000) for p in repo.source(".js", ".ts", ".mjs", ".cjs", max_depth=2)}
    py_src = {repo.rel(p): _read(p, 200_000) for p in repo.source(".py", max_depth=2)}
    rs_src = {repo.rel(p): _read(p, 200_000) for p in repo.source(".rs", max_depth=3)}

    # Several deployable services: one Dockerfile per sibling directory (src/<svc>/Dockerfile).
    parents: dict[str, int] = {}
    for d in dockerfiles:
        if len(d.relative_to(repo.root).parts) == 3:
            parents[d.parent.parent.name] = parents.get(d.parent.parent.name, 0) + 1
    multi = max(parents.values(), default=0) >= 3
    f["multi_service"] = Finding(multi, "high" if multi else "medium",
                                 [f"{k}/*/Dockerfile x{n}" for k, n in parents.items() if n >= 3] or ["one deployable unit"])
    if multi:
        unsupported.append("MULTI_SERVICE")

    # HTTP server evidence
    http_ev = [f"{node[n]}: {n}" for n in sorted(NODE_SERVER & node.keys())]
    go_server = r"ListenAndServe|http\.Server\{|echo\.New\(|gin\.(New|Default)\("
    http_ev += [f"{p}: net/http server" for p, s in go_src.items() if re.search(go_server, s)][:2]
    if go_mod and re.search(r"labstack/echo|gin-gonic/gin|go-chi/chi|gofiber/fiber", go_mod):
        http_ev.append(f"{go_mod_file}: Go web framework")
    if java and re.search(r"spring-boot-starter-web|quarkus-resteasy|micronaut-http-server", java):
        http_ev.append(f"{java_file}: Java web framework")
    http_ev += [f"{py[n]}: {n}" for n in sorted(PY_SERVER & py.keys())]
    http_ev += [f"{cargo_file}: {n}" for n in RUST_SERVER if n in rust]
    frontend = sorted(NODE_FRONTEND & node.keys())
    has_index = any(p.name == "index.html" and d <= 1 for p, d in repo.files)

    # Non-HTTP: the only exposed ports are known non-HTTP ports and nothing serves HTTP.
    non_http = bool(ports) and all(p in NON_HTTP_PORTS for p in ports) and not http_ev
    f["non_http"] = Finding(non_http, "high" if non_http else "medium",
                            [f"{src}: EXPOSE {p}" for p, src in ports.items()] if non_http else ["HTTP server or no non-HTTP port"])
    if non_http:
        unsupported.append("NON_HTTP")

    split = (repo.root / "frontend").is_dir() and (repo.root / "backend").is_dir()
    if split and frontend and http_ev:  # v3: a screen in frontend/ and an API in backend/
        form = Finding("STATIC_PLUS_API", "medium", ["frontend/ and backend/ in one repo"] + http_ev[:2]
                       + [f"package.json: {', '.join(frontend)}"])
    elif http_ev or non_http or (dockerfiles and not frontend and not has_index):
        form = Finding("SERVER_APP", "high" if http_ev else "medium",
                       http_ev or [f"{repo.rel(d)}: container image" for d in dockerfiles[:1]])
    elif frontend or has_index:
        ev = [f"package.json: {', '.join(frontend)}"] if frontend else []
        ev += ["index.html at the root"] if has_index else []
        if "build" in _node_scripts(repo):
            ev.append("package.json: build script (deploy the build output)")
        form = Finding("STATIC_SITE", "high", ev)
    else:
        form = Finding("SERVER_APP", "low", ["no web entry point found"])
    f["app_form"] = form
    static = form.value == "STATIC_SITE"

    routers = sorted(NODE_ROUTER & node.keys())
    f["spa_fallback"] = Finding(static and bool(routers), "high" if routers else "medium",
                                [f"package.json: {', '.join(routers)}"] if routers else ["no client-side router"])

    if not static:
        rt = [f"{node[n]}: {n}" for n in sorted(NODE_REALTIME & node.keys())]
        rt += [f"{go_mod_file}: {m}" for m in GO_REALTIME if m in go_mod]
        rt += [f"{java_file}: spring-boot-starter-websocket"] if "spring-boot-starter-websocket" in java else []
        rt += [f"{py[n]}: {n}" for n in sorted(PY_REALTIME & py.keys())]
        rt += [f"{cargo_file}: {n}" for n in RUST_REALTIME if n in rust]
        if re.search(r'^\s*axum\s*=.*features\s*=\s*\[[^\]]*"ws"', cargo, re.M):
            rt.append(f"{cargo_file}: axum feature ws")
        f["realtime_connection"] = Finding(bool(rt), "high" if rt else "medium", rt or ["no WebSocket/SSE library"])

        up = [f"{node[n]}: {n}" for n in sorted(NODE_UPLOAD & node.keys())]
        # Parsing a multipart form is not enough (go-httpbin parses one only to echo it back); keeping the uploaded
        # file on disk in the same handler file is what marks file-upload processing.
        up += [f"{p}: multipart upload saved to disk" for p, s in go_src.items()
               if re.search(r"MultipartForm|FormFile\(", s) and re.search(r"CreateTemp|os\.Create\(|os\.WriteFile|os\.OpenFile", s)][:1]
        up += [f"{p}: request body limit flag" for p, s in go_src.items() if re.search(r'"[\w-]*body-limit"', s)][:1]
        up += [f"{py['python-multipart']}: python-multipart"] if "python-multipart" in py else []
        f["large_payload"] = Finding(bool(up), "medium" if up else "low", up or ["no upload handling found"])

        st = [f"{node[n]}: {n} (data on local disk)" for n in sorted(NODE_LOCAL_STATE & node.keys())]
        if "express-session" in node and not (NODE_SESSION_STORES & node.keys()):
            st.append(f"{node['express-session']}: express-session with the default in-memory store")
        st += [f"{repo.rel(d)}: VOLUME" for d in dockerfiles if re.search(r"^\s*VOLUME\s", _read(d), re.M)]
        st += [f"{go_mod_file}: {m} (embedded database file)" for m in GO_LOCAL_STATE if m in go_mod]
        st += [f"{p}: node:sqlite (built-in, not in package.json)" for p, s in js_src.items()
               if re.search(r"""from\s+["']node:sqlite["']|require\(["']node:sqlite["']\)""", s)][:1]
        st += [f"{cargo_file}: {n}" for n in ("rusqlite", "sled") if n in rust]
        if re.search(r'^\s*(diesel|sqlx)\s*=.*"sqlite', cargo, re.M) or re.search(r'^sqlite\w*\s*=\s*\[', cargo, re.M):
            st.append(f"{cargo_file}: SQLite feature")
        f["in_memory_state"] = Finding(bool(st), "medium" if st else "low", st or ["no local state found"])

        # Request timeouts the app itself declares (e.g. a 30 s API timeout flag)
        dur = None
        for p, s in go_src.items():
            m = re.search(r'"[\w-]*timeout[\w-]*"\s*,\s*time\.Duration\((\d+)\)\s*\*\s*time\.Second', s)
            if m and int(m.group(1)) >= 30:
                secs = int(m.group(1))
                dur = Finding("ABOUT_30S" if secs <= 30 else "S30_TO_60S" if secs <= 60 else "S60_TO_120S" if secs <= 120
                              else "OVER_120S", "medium", [f"{p}: request timeout default {secs}s"])
                break
        if dur:
            f["request_duration"] = dur

    # Data: drivers the app depends on. Several drivers = the app supports several engines; the developer picks.
    engines: dict[str, list[str]] = {}
    for n, kind in NODE_DB.items():
        if n in node:
            engines.setdefault(kind, []).append(f"{node[n]}: {n}")
    if "@prisma/client" in node:
        for s in repo.named("schema.prisma"):
            m = re.search(r'provider\s*=\s*"(postgresql|mysql|sqlite|mongodb)"', _read(s))
            if m:
                kind = "POSTGRES" if m.group(1) == "postgresql" else "OTHER"
                engines.setdefault(kind, []).append(f"{repo.rel(s)}: provider {m.group(1)}")
    for mod, kind in GO_DB.items():
        if mod in go_mod:
            engines.setdefault(kind, []).append(f"{go_mod_file}: {mod}")
    for key, kind in JAVA_DB.items():
        if re.search(rf"<artifactId>[\w-]*{key}[\w-]*</artifactId>|['\"][\w.]*:{key}", java):
            engines.setdefault(kind, []).append(f"{java_file}: {key}")
    for n, kind in PY_DB.items():
        if n in py:
            engines.setdefault(kind, []).append(f"{py[n]}: {n}")
    for n, kind in RUST_DB.items():
        if n in rust:
            engines.setdefault(kind, []).append(f"{cargo_file}: {n}")
    for feat, kind in (("postgres", "POSTGRES"), ("mysql", "OTHER"), ("sqlite", "OTHER")):
        if re.search(rf'^\s*(diesel|sqlx)\s*=.*"{feat}|^{feat}\w*\s*=\s*\[', cargo, re.M):
            engines.setdefault(kind, []).append(f"{cargo_file}: {feat} feature")
    drivers = sum(len(v) for v in engines.values())
    if not engines:
        f["data"] = Finding("NONE", "medium", ["no database driver"])
    else:
        pick = "POSTGRES" if "POSTGRES" in engines else "DYNAMODB" if "DYNAMODB" in engines else "OTHER"
        f["data"] = Finding(pick, "high" if drivers == 1 else "low",
                            [e for v in engines.values() for e in v]
                            + (["several drivers: the developer picks the engine"] if drivers > 1 else []))

    # Port: platform contract is "listen on $PORT (8080)" (D-34). Find how this app is told its port.
    if not static and not non_http:
        env_ev: list[tuple[str, str]] = []
        for p, s in {**go_src, **js_src, **py_src, **rs_src}.items():
            port_env = (r'(?:Getenv|getEnv\w*|LookupEnv|environ\.get|getenv|env::var)\(\s*["\'](' + PORT_VAR + r')["\']'
                        r'|process\.env\.(' + PORT_VAR + r')\b|environ\[["\'](' + PORT_VAR + r')["\']\]')
            for m in re.finditer(port_env, s):
                env_ev.append((m.group(1) or m.group(2) or m.group(3), p))
            prefix = re.search(r'SetEnvPrefix\(\s*"(\w+)"\s*\)', s)  # viper: flag "port" -> <PREFIX>_PORT
            if prefix:
                for q, t in go_src.items():
                    for m in re.finditer(r'\.Flags\(\)\.\w+\(\s*"([\w-]*port)"', t):
                        env_ev.append((f"{prefix.group(1)}_{m.group(1)}".upper().replace("-", "_"), f"{q} + {p}: viper env prefix"))
            if re.search(r"ToUpper\(strings\.ReplaceAll\(", s):  # flags overridable as env vars (FLAG-NAME -> FLAG_NAME)
                for q, t in go_src.items():
                    for m in re.finditer(r'\.Int\("([\w-]*port)"', t):
                        env_ev.append((m.group(1).upper().replace("-", "_"), f"{q} + {p}"))
        for p, d in repo.files:
            if d <= 1 and p.name.startswith("settings.json"):
                for m in re.finditer(r"\$\{(" + PORT_VAR + r"):", _read(p)):
                    env_ev.append((m.group(1), repo.rel(p)))
        if java and "spring-boot" in java:
            env_ev.append(("SERVER_PORT", f"{java_file}: Spring Boot (server.port, default 8080)"))
        if "rocket" in rust:
            env_ev.append(("ROCKET_PORT", f"{cargo_file}: Rocket reads ROCKET_PORT"))
        names = sorted({n for n, _ in env_ev}, key=lambda n: (n != "PORT", n))
        pinned = _fixed_cmd_port(repo, dockerfiles)
        if pinned and not names:
            f["port_env"] = Finding(None, "medium", [pinned])
            unsupported.append("PORT_FROM_FLAG_ONLY")
        elif names:
            f["port_env"] = Finding(names[0], "high" if names[0] == "PORT" else "medium",
                                    [f"{src}: {n}" for n, src in env_ev if n == names[0]][:2])
        else:
            flag = next((f"{p}: port only via a command-line flag" for p, s in go_src.items()
                         if re.search(r'flag\.\w+\("(listen|port|addr)"', s)), None)
            f["port_env"] = Finding(None, "medium" if flag else "low", [flag or "no port setting found"])
            if flag:
                unsupported.append("PORT_FROM_FLAG_ONLY")

    # Confirm only what moves the blueprint: a capability the analyzer found (true), a database it picked, a request
    # duration it read from config, or an app form it is not sure of. "No evidence found" is not asked again.
    confirm = [k for k in ("realtime_connection", "large_payload", "in_memory_state", "request_duration")
               if k in f and f[k].value not in (False, None)]
    if f["app_form"].confidence != "high":
        confirm.insert(0, "app_form")
    if f["data"].value != "NONE" or f["data"].confidence == "low":
        confirm.append("data")
    # v2: "no storage found" is not "stores nothing" (thelounge keeps users and logs on disk via node:sqlite and a
    # config home; deployed as stateless it would lose them silently, and validation cannot see that). A server app
    # with no database and no state evidence is always asked where it keeps its data.
    if not static and not non_http and f["data"].value == "NONE" and f.get("in_memory_state") \
            and not f["in_memory_state"].value and "in_memory_state" not in confirm:
        confirm.append("in_memory_state")
    ask = list(DEVELOPER_ONLY) + ([] if static or "request_duration" in f else ["request_duration"])
    return Analysis(f, unsupported, confirm, ask)


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__)
        return 2
    print(json.dumps(analyze(argv[0]).to_dict(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
