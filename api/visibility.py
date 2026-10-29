import datetime, ipaddress, json, re, socket
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler
from urllib.parse import urljoin, urlparse
import requests
from google import genai
from google.genai import types

_client = genai.Client()  # reads GEMINI_API_KEY automatically


def ask(system, user, tokens=2000):
    r = _client.models.generate_content(
        model="gemini-2.5-flash", contents=user,
        config=types.GenerateContentConfig(
            system_instruction=system, response_mime_type="application/json",
            max_output_tokens=tokens, thinking_config=types.ThinkingConfig(thinking_budget=0)))
    return json.loads(r.text)


UA = {"user-agent": "AnswerAuditBot/1.0 (student project)"}
STOP = set("a an the of to in on for and or is are do does how what why when which who can i my your with by at from it this that be as".split())
SYSTEM = """You assess whether website content clearly answers questions that AI answer engines might be asked.
You receive page excerpts and target queries, plus measured facts computed by code.
Rules:
- Judge ONLY from the supplied excerpts. Never assume content that is not shown.
- answered must be "Yes", "Partly" or "No".
- evidence must be an exact quote of at most 20 words copied from the excerpts, or "" if there is none.
- Everything you write is an ESTIMATE. Do not state or imply real AI-search rankings or traffic.
- missing_answer: the specific answer the site should add, described without inventing facts about the business.
- schema: suggest only types that fit the content (FAQPage, HowTo, Product, Organization, LocalBusiness, Article, Service). Do not suggest types already detected.
Return ONLY JSON: {"summary":"","queries":[{"query":"","answered":"","evidence":"","missing_answer":"","schema":"","content_gap":""}],"site_gaps":[""]}"""


def is_public(url):
    try:
        u = urlparse(url)
        if u.scheme not in ("http", "https") or not u.hostname:
            return False
        return all(ipaddress.ip_address(i[4][0]).is_global for i in socket.getaddrinfo(u.hostname, None))
    except Exception:
        return False


def get(url):
    r = requests.get(url, headers=UA, timeout=6)
    if not is_public(r.url):
        raise ValueError("Redirected to a non-public address")
    return {"url": url, "final": r.url, "status": r.status_code, "html": r.text if "text/html" in r.headers.get("content-type", "") else ""}


def safe_get(u):
    try:
        return get(u)
    except Exception:
        return None


def parse(p):
    h = p["html"]
    clean = re.sub(r"<script[\s\S]*?</script>|<style[\s\S]*?</style>|<nav[\s\S]*?</nav>|<footer[\s\S]*?</footer>", " ", h, flags=re.I)
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", clean)).strip()
    heads = [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", m.group(2))).strip() for m in re.finditer(r"<h([1-3])[^>]*>([\s\S]*?)</h\1>", clean, re.I)]
    heads = [x for x in heads if x]
    schema = set()

    def walk(o):
        if isinstance(o, dict):
            t = o.get("@type")
            for x in ([t] if isinstance(t, str) else t or []):
                schema.add(x)
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    for m in re.finditer(r'<script[^>]+application/ld\+json[^>]*>([\s\S]*?)</script>', h, re.I):
        try:
            walk(json.loads(m.group(1)))
        except ValueError:
            pass
    t = re.search(r"<title[^>]*>([\s\S]*?)</title>", h, re.I)
    return {"url": p["final"], "title": t.group(1).strip() if t else "", "headings": heads, "schema": sorted(schema), "text": text,
            "words": len(text.split()), "questionHeadings": sum(1 for x in heads if "?" in x),
            "hasList": bool(re.search(r"<(ul|ol)\b", clean, re.I)), "hasTable": bool(re.search(r"<table\b", clean, re.I))}


def internal(p, host):
    out = []
    for m in re.finditer(r'<a\s[^>]*href=["\']([^"\'#]+)["\']', p["html"], re.I):
        u = urlparse(urljoin(p["final"], m.group(1)))
        if u.hostname == host and u.scheme in ("http", "https") and not re.search(r"\.(pdf|jpg|png|zip|svg|webp)$", u.path, re.I):
            href = u._replace(fragment="").geturl()
            if href not in out:
                out.append(href)
    return out


def terms(q):
    return [w for w in re.sub(r"[^a-z0-9\s]", " ", q.lower()).split() if w not in STOP]


def measure(q, pages):
    t = terms(q)
    per = []
    for p in pages:
        low, hs = p["text"].lower(), " ".join(p["headings"]).lower()
        per.append({"url": p["url"], "textCoverage": round(sum(w in low for w in t) / (len(t) or 1), 2),
                    "headingCoverage": round(sum(w in hs for w in t) / (len(t) or 1), 2)})
    per.sort(key=lambda x: -(x["textCoverage"] + x["headingCoverage"]))
    return {"terms": t, "bestPage": per[0], "pages": per}


class handler(BaseHTTPRequestHandler):
    def send(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_POST(self):
        try:
            d = json.loads(self.rfile.read(int(self.headers.get("content-length", 0) or 0)) or b"{}")
        except ValueError:
            return self.send(400, {"error": "Invalid JSON"})
        url = d.get("url", "")
        qs = [str(q).strip() for q in d.get("queries", []) if str(q).strip()][:8]
        if not d.get("consent"):
            return self.send(400, {"error": "Confirm you have permission to analyse this site."})
        if not qs:
            return self.send(400, {"error": "Add at least one target query."})
        if not is_public(url):
            return self.send(400, {"error": "Enter a valid public http(s) URL."})
        try:
            start = urlparse(url)
            first = get(url)
            with ThreadPoolExecutor(4) as ex:
                more = list(ex.map(safe_get, internal(first, start.hostname)[:4]))
            pages = [parse(p) for p in [first] + more if p and p["status"] < 400 and p["html"]]
            if not pages:
                raise ValueError("Could not read any pages from this site.")
            measured = {"pages": [{"url": p["url"], "title": p["title"], "words": p["words"], "schemaTypes": p["schema"], "questionHeadings": p["questionHeadings"], "hasList": p["hasList"], "hasTable": p["hasTable"]} for p in pages],
                        "queries": [{"query": q, **measure(q, pages)} for q in qs]}
            excerpts = [{"url": p["url"], "title": p["title"], "headings": p["headings"][:25], "text": p["text"][:2500]} for p in pages]
            ai = None
            try:
                payload = {"queries": qs, "detectedSchema": sorted({s for p in pages for s in p["schema"]}), "excerpts": excerpts}
                ai = ask(SYSTEM, json.dumps(payload), 3000)
                alltext = " ".join(p["text"].lower() for p in pages)
                for q in ai.get("queries", []):  # verify quotes against the real page text
                    ev = " ".join((q.get("evidence") or "").lower().split())
                    q["evidenceVerified"] = bool(ev) and ev in alltext
                    if not q["evidenceVerified"]:
                        q["evidence"] = ""
            except Exception:
                pass
            self.send(200, {"site": f"{start.scheme}://{start.netloc}", "date": datetime.date.today().isoformat(), "measured": measured, "estimated": ai})
        except Exception as e:
            self.send(500, {"error": str(e)})