"""Repo analyzer rules (D-183) on small synthetic repositories. The baseline public apps are scored separately by
scripts/eval_analyzer.py (network); these tests pin each rule without the network."""

import json

from agent.repo_analyzer import DEVELOPER_ONLY, analyze
from platform_core.engine import RawInput, recommend
from platform_core.pricing import PriceTable

DEFAULTS = {"traffic_scale": "T1", "traffic_pattern": "IDLE_MOSTLY", "downtime_tolerance": "OK",
            "latency_sensitivity": "TOLERANT", "exposure": "PUBLIC", "tier": "DEV", "budget_usd_month": 30}


def repo(tmp_path, files: dict):
    for name, content in files.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content if isinstance(content, str) else json.dumps(content), encoding="utf-8")
    return tmp_path


def engine(analysis):
    raw = {**DEFAULTS, **{k: v for k, v in analysis.draft().items() if v is not None and k != "spa_fallback"}}
    rec = recommend(RawInput.from_dict(raw), PriceTable.load())
    return rec["status"], (rec.get("architecture") or {}).get("candidate_id")


def test_plain_html_is_a_static_site(tmp_path):
    a = analyze(repo(tmp_path, {"index.html": "<h1>hi</h1>", "style.css": ""}))
    assert a.value("app_form") == "STATIC_SITE" and a.value("spa_fallback") is False
    assert a.confirm == [] and a.unsupported == []
    assert engine(a) == ("RECOMMENDED", "NONE+NONE")


def test_react_router_build_needs_spa_fallback(tmp_path):
    a = analyze(repo(tmp_path, {"package.json": {"scripts": {"build": "vite build"},
                                                 "dependencies": {"react": "18", "react-router-dom": "6"}},
                                "public/index.html": ""}))
    assert a.value("app_form") == "STATIC_SITE" and a.value("spa_fallback") is True


def test_express_socket_io_is_realtime_and_goes_to_container(tmp_path):
    a = analyze(repo(tmp_path, {"package.json": {"dependencies": {"express": "4", "socket.io": "4"}},
                                "server.js": "app.listen(process.env.PORT || 3000)"}))
    assert a.value("app_form") == "SERVER_APP" and a.value("realtime_connection") is True
    assert a.value("port_env") == "PORT"
    assert "realtime_connection" in a.confirm, "an answer that moves the blueprint is confirmed by the developer"
    assert engine(a)[1] == "CTR+NONE"


def test_api_without_storage_evidence_is_asked_where_it_keeps_data(tmp_path):
    """v2: no DB and no state evidence is NOT proof of a stateless app (held-out thelounge); the developer is asked."""
    a = analyze(repo(tmp_path, {"package.json": {"dependencies": {"express": "4"}},
                                "index.js": "const port = process.env.PORT"}))
    assert a.confirm == ["in_memory_state"] and a.value("in_memory_state") is False
    assert set(DEVELOPER_ONLY) <= set(a.ask) and "request_duration" in a.ask
    assert engine(a)[1] == "SLS+NONE"


def test_parsing_multipart_alone_is_not_a_large_upload(tmp_path):
    """go-httpbin parses multipart forms only to echo them; that must not push it to Container."""
    echo = ('package main\nimport "net/http"\nfunc h(r *http.Request){ r.ParseMultipartForm(1<<20) }\n'
            'func main(){ http.ListenAndServe(":"+os.Getenv("PORT"), nil) }')
    a = analyze(repo(tmp_path, {"go.mod": "module x\n", "main.go": echo}))
    assert a.value("large_payload") is False


def test_upload_saved_to_disk_is_a_large_payload(tmp_path):
    save = ('package main\nimport "net/http"\n'
            'func h(r *http.Request){ f,_,_ := r.FormFile("file"); o,_ := os.CreateTemp("", "u"); io.Copy(o, f) }\n'
            'func main(){ http.ListenAndServe(":"+os.Getenv("PORT"), nil) }')
    a = analyze(repo(tmp_path, {"go.mod": "module x\n", "main.go": save}))
    assert a.value("large_payload") is True and "large_payload" in a.confirm


def test_port_only_from_a_cli_flag_breaks_the_app_contract(tmp_path):
    """hashicorp/http-echo: -listen flag only, the platform passes no args (D-34)."""
    src = ('package main\nimport ("flag";"net/http")\nvar l = flag.String("listen", ":5678", "addr")\n'
           'func main(){ http.ListenAndServe(*l, nil) }')
    a = analyze(repo(tmp_path, {"go.mod": "module x\n", "main.go": src, "Dockerfile": "FROM scratch\nEXPOSE 5678\n"}))
    assert "PORT_FROM_FLAG_ONLY" in a.unsupported and a.value("port_env") is None


def test_flags_mapped_to_env_vars_give_the_port_variable(tmp_path):
    """gotenberg: every flag can be set as an env var (api-port -> API_PORT)."""
    flags = 'package api\nfunc d(fs *F){ fs.Int("api-port", 3000, "port") }'
    main = ('package main\nimport ("net/http";"strings")\n'
            'func env(n string) string { return strings.ToUpper(strings.ReplaceAll(n, "-", "_")) }\n'
            'func main(){ http.ListenAndServe(":3000", nil) }')
    a = analyze(repo(tmp_path, {"go.mod": "module x\n", "main.go": main, "pkg/api/api.go": flags}))
    assert a.value("port_env") == "API_PORT" and a.unsupported == []


def test_go_request_timeout_flag_sets_request_duration(tmp_path):
    src = ('package main\nimport ("net/http";"time")\nfunc d(fs *F){ fs.Duration("api-timeout", time.Duration(30)*time.Second, "t") }\n'
           'func main(){ http.ListenAndServe(":"+os.Getenv("PORT"), nil) }')
    a = analyze(repo(tmp_path, {"go.mod": "module x\n", "main.go": src}))
    assert a.value("request_duration") == "ABOUT_30S" and "request_duration" not in a.ask
    assert engine(a)[1] == "CTR+NONE", "30 s is too close to the API Gateway limit (EXC_SLS_TIMEOUT)"


def test_prisma_postgres_is_detected_and_confirmed(tmp_path):
    a = analyze(repo(tmp_path, {"package.json": {"dependencies": {"express": "4", "@prisma/client": "5"}},
                                "src/prisma/schema.prisma": 'datasource db {\n  provider = "postgresql"\n}'}))
    assert a.value("data") == "POSTGRES" and a.findings["data"].confidence == "high" and "data" in a.confirm


def test_several_db_drivers_are_low_confidence(tmp_path):
    a = analyze(repo(tmp_path, {"package.json": {"dependencies": {"express": "4", "pg": "8", "mysql2": "3", "better-sqlite3": "9"}}}))
    assert a.findings["data"].confidence == "low" and "data" in a.confirm
    assert a.value("in_memory_state") is True, "an embedded SQLite file is state on local disk"


def test_express_session_without_a_store_is_in_memory_state(tmp_path):
    a = analyze(repo(tmp_path, {"package.json": {"dependencies": {"express": "4", "express-session": "1"}}}))
    assert a.value("in_memory_state") is True
    b = analyze(repo(tmp_path / "b", {"package.json": {"dependencies": {"express": "4", "express-session": "1", "connect-redis": "7"}}}))
    assert b.value("in_memory_state") is False


def test_game_server_port_is_non_http(tmp_path):
    a = analyze(repo(tmp_path, {"Dockerfile": "FROM java\nEXPOSE 25565\nVOLUME [\"/data\"]\n"}))
    assert a.value("non_http") is True and "NON_HTTP" in a.unsupported


def test_one_dockerfile_per_service_is_multi_service(tmp_path):
    files = {f"src/{s}/Dockerfile": "FROM scratch\nEXPOSE 8080\n" for s in ("cart", "checkout", "frontend")}
    a = analyze(repo(tmp_path, files))
    assert a.value("multi_service") is True and "MULTI_SERVICE" in a.unsupported


def test_skipped_directories_do_not_count(tmp_path):
    """Dependencies under node_modules or test fixtures must not change the answer."""
    a = analyze(repo(tmp_path, {"index.html": "", "node_modules/socket.io/package.json": {"dependencies": {"ws": "8"}},
                                "test/server/package.json": {"dependencies": {"express": "4"}}}))
    assert a.value("app_form") == "STATIC_SITE"


def test_every_finding_has_evidence(tmp_path):
    a = analyze(repo(tmp_path, {"package.json": {"dependencies": {"express": "4", "socket.io": "4", "pg": "8"}}}))
    assert all(f.evidence for f in a.findings.values()), {k: f for k, f in a.findings.items() if not f.evidence}


# ------------------------------------------------------------------ v2 rules (first held-out set, D-184)

GO_MAIN = 'package main\nfunc main(){ http.ListenAndServe(":"+os.Getenv("PORT"), nil) }'


def test_indirect_go_modules_are_not_features(tmp_path):
    """memos: gorilla/websocket only as `// indirect` -> no WebSocket endpoint."""
    gomod = "module x\nrequire (\n\tgithub.com/gorilla/websocket v1.5.3 // indirect\n)\n"
    a = analyze(repo(tmp_path, {"go.mod": gomod, "main.go": GO_MAIN}))
    assert a.value("realtime_connection") is False


def test_go_sqlite_driver_is_local_state(tmp_path):
    a = analyze(repo(tmp_path, {"go.mod": "module x\nrequire modernc.org/sqlite v1.56.0\n", "main.go": GO_MAIN}))
    assert a.value("in_memory_state") is True and a.value("data") == "OTHER"


def test_node_builtin_sqlite_is_local_state(tmp_path):
    """thelounge: node:sqlite is built into Node, so it never shows up in package.json."""
    a = analyze(repo(tmp_path, {"package.json": {"dependencies": {"express": "4"}},
                                "server/store.ts": 'import {DatabaseSync} from "node:sqlite";'}))
    assert a.value("in_memory_state") is True


def test_port_pinned_in_dockerfile_cmd_breaks_the_contract(tmp_path):
    """postmanlabs/httpbin: CMD gunicorn -b 0.0.0.0:80 -- an env var cannot move it to 8080."""
    a = analyze(repo(tmp_path, {"requirements.txt": "flask\ngunicorn\n",
                                "Dockerfile": 'FROM python\nEXPOSE 80\nCMD ["gunicorn", "-b", "0.0.0.0:80", "app:app"]\n'}))
    assert "PORT_FROM_FLAG_ONLY" in a.unsupported and "port 80" in a.findings["port_env"].evidence[0]


def test_python_reads_port_from_env(tmp_path):
    a = analyze(repo(tmp_path, {"requirements.txt": "flask\n", "app.py": 'port = int(os.environ.get("PORT", 8080))',
                                "Dockerfile": 'FROM python\nCMD ["python", "app.py"]\n'}))
    assert a.value("port_env") == "PORT" and a.unsupported == []


def test_test_tooling_env_vars_are_not_app_settings(tmp_path):
    """vaultwarden: playwright/ reads MARIADB_PORT for its test database."""
    a = analyze(repo(tmp_path, {"package.json": {"dependencies": {"express": "4"}},
                                "playwright/global-utils.ts": "const p = process.env.MARIADB_PORT",
                                "server.js": "app.listen(process.env.PORT)"}))
    assert a.value("port_env") == "PORT"


def test_viper_env_prefix_gives_the_port_variable(tmp_path):
    """memos: viper SetEnvPrefix("memos") + flag "port" -> MEMOS_PORT."""
    src = ('package main\nfunc init(){ rootCmd.Flags().Int("port", 8081, "p"); viper.SetEnvPrefix("memos"); viper.AutomaticEnv() }\n'
           'func main(){ http.ListenAndServe(":8081", nil) }')
    a = analyze(repo(tmp_path, {"go.mod": "module x\n", "cmd/main.go": src}))
    assert a.value("port_env") == "MEMOS_PORT"


def test_rust_web_app_with_websocket_and_sqlite(tmp_path):
    cargo = ('[package]\nname = "x"\n[features]\nsqlite = ["diesel/sqlite"]\n[dependencies]\n'
             'rocket = "0.5"\nrocket_ws = "0.1"\ndiesel = { version = "2" }\n')
    a = analyze(repo(tmp_path, {"Cargo.toml": cargo}))
    assert a.value("app_form") == "SERVER_APP" and a.value("realtime_connection") is True
    assert a.value("in_memory_state") is True and a.value("port_env") == "ROCKET_PORT"


# ------------------------------------------------------------------ v3 rules (docs/baseline/README.md section 4)

def test_v3_frontend_and_backend_folders_are_a_screen_plus_api(tmp_path):
    """held-out 2 fastapi-template: React in frontend/, FastAPI + Postgres in backend/ was drafted as a static site."""
    a = analyze(repo(tmp_path, {"frontend/package.json": {"dependencies": {"react": "18"}},
                                "backend/pyproject.toml": 'dependencies = [\n  "fastapi>=0.1",\n  "psycopg>=3",\n]\n'}))
    assert a.value("app_form") == "STATIC_PLUS_API"
    assert a.value("data") == "POSTGRES"
    assert "app_form" in a.confirm, "a new app-form rule is not sure enough to skip the developer"


def test_v3_app_in_complete_folder_gives_spring_port(tmp_path):
    """held-out 2 spring-rest: the guide's app lives in complete/, so the port variable was missed."""
    a = analyze(repo(tmp_path, {"complete/pom.xml": "<artifactId>spring-boot-starter-web</artifactId>",
                                "initial/pom.xml": "<artifactId>spring-boot-starter-web</artifactId>"}))
    assert a.value("app_form") == "SERVER_APP" and a.value("port_env") == "SERVER_PORT"


def test_v3_requirements_folder_is_read(tmp_path):
    """held-out 2 wagtail-bakery: Django and psycopg were only in requirements/base.txt and production.txt."""
    a = analyze(repo(tmp_path, {"requirements/base.txt": "Django>=4\nwagtail\n",
                                "requirements/production.txt": "-r base.txt\npsycopg[binary]\n",
                                "requirements-dev.txt": "pytest\n"}))
    assert a.value("app_form") == "SERVER_APP" and a.value("data") == "POSTGRES"
    assert any("requirements/production.txt" in e for e in a.findings["data"].evidence)


def test_v3_report_is_not_a_port_variable(tmp_path):
    """held-out 2 wagtail-bakery: CSP_REPORT_URI matched *PORT*; the real variable is PORT."""
    a = analyze(repo(tmp_path, {"requirements.txt": "django\n",
                                "settings.py": 'CSP = os.environ.get("CSP_REPORT_URI")\nPORT = os.environ.get("PORT")\n'}))
    assert a.value("port_env") == "PORT"
    b = analyze(repo(tmp_path / "only-report", {"requirements.txt": "django\n",
                                               "settings.py": 'CSP = os.environ.get("CSP_REPORT_URI")\n'}))
    assert b.value("port_env") is None
    c = analyze(repo(tmp_path / "prefixed", {"requirements.txt": "django\n",
                                            "app.py": 'os.environ.get("MEMOS_PORT")\n'}))
    assert c.value("port_env") == "MEMOS_PORT"


def test_v3_frontend_folder_alone_stays_a_static_site(tmp_path):
    a = analyze(repo(tmp_path, {"frontend/package.json": {"scripts": {"build": "vite build"}, "dependencies": {"vue": "3"}},
                                "frontend/index.html": ""}))
    assert a.value("app_form") == "STATIC_SITE"
