import importlib.util, json, os, re, sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))
os.chdir(ROOT)


def load_env():
    for name in (".env", ".env.local"):
        p = os.path.join(ROOT, name)
        if os.path.exists(p):
            for line in open(p, encoding="utf-8"):
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip("\"'"))


load_env()
if not os.environ.get("GEMINI_API_KEY"):
    sys.exit("GEMINI_API_KEY is missing. Create a file named .env containing: GEMINI_API_KEY=your_key")

modules = {}


def load(name):
    if name not in modules:
        spec = importlib.util.spec_from_file_location("api_" + name, os.path.join(ROOT, "api", name + ".py"))
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        modules[name] = m
    return modules[name]


class App(SimpleHTTPRequestHandler):
    def send(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        p = self.path.split("?")[0]
        # never serve the API code, .env or other hidden files
        if p.startswith("/api") or p.endswith(".py") or any(s.startswith(".") for s in p.split("/") if s):
            return self.send(404, {"error": "Not found"})
        return super().do_GET()

    def do_POST(self):
        p = self.path.split("?")[0]
        name = p[len("/api/"):].strip("/") if p.startswith("/api/") else ""
        if not re.fullmatch(r"\w+", name) or not os.path.exists(os.path.join(ROOT, "api", name + ".py")):
            return self.send(404, {"error": "Unknown API"})
        try:
            load(name).handler.do_POST(self)
        except Exception as e:
            self.send(500, {"error": str(e)})


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    print(f"Running at http://localhost:{port}  (Ctrl+C to stop)")
    ThreadingHTTPServer(("127.0.0.1", port), App).serve_forever()