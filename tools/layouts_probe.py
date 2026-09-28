#!/usr/bin/env python3
"""What the Steam Deck keeps about controller layouts: a one-off probe for the layouts feature.

Run it on the Deck (desktop or over SSH) and send back the report it writes:

    python3 layouts_probe.py                    look around, change nothing
    python3 layouts_probe.py --template GAME    also copy GAME's layout into Steam's templates
    python3 layouts_probe.py --cleanup          remove that copy again

GAME is a folder name from the "Steam Controller Configs" list of the report (an AppID or the
name of a non-Steam game). The report goes to ~/deckdrop-layouts-probe.txt and to the screen,
and then it is shared on the home network until Ctrl+C: the script prints a link to open on a
phone or PC (only the report is served, nothing else). --no-serve skips that.
Only --template and --cleanup write anything, and only the one probe file in the templates folder.
"""
import argparse
import datetime
import http.server
import importlib.util
import json
import os
import re
import socket
import sys
import urllib.request
from pathlib import Path

LIMIT = 60                 # entries per listing
TEXT_LIMIT = 20000         # characters of a file printed as is
PROBE_TEMPLATE = "controller_neptune_deckdrop_probe.vdf"
PROBE_TITLE = "DeckDrop probe"
TOKEN = re.compile(r'\s*(?://[^\n]*\n\s*)*("(?:\\.|[^"\\])*"|\{|\}|[^\s{}"]+)', re.S)

out = []


def say(line=""):
    out.append(line)
    print(line)


def head(title):
    say()
    say("== " + title)


def steam_root(arg):
    if arg:
        return Path(arg).expanduser()
    for cand in (Path.home() / ".local/share/Steam", Path.home() / ".steam/steam", Path.home() / ".steam/root"):
        if (cand / "userdata").is_dir():
            return cand.resolve()
    return Path.home() / ".local/share/Steam"


def parse_vdf(text):
    """Text KeyValues into nested dicts; a block is a list, as layouts repeat "group" and "preset"."""
    pos, stack, root, key = 0, [], {}, None
    cur = root
    while True:
        m = TOKEN.match(text, pos)
        if not m or not m.group(1):
            break
        tok, pos = m.group(1), m.end()
        if tok == "{":
            child = {}
            if isinstance(cur.get(key), list):
                cur[key].append(child)
            else:
                cur[key] = [child]
            stack.append(cur)
            cur, key = child, None
        elif tok == "}":
            if not stack:
                break
            cur, key = stack.pop(), None
        else:
            tok = tok[1:-1].replace('\\"', '"').replace("\\\\", "\\") if tok.startswith('"') else tok
            if key is None:
                key = tok
            else:
                cur[key] = tok
                key = None
    return root


def first(node, *path):
    """node[path...], taking the first block of repeated ones."""
    for k in path:
        if not isinstance(node, dict) or k not in node:
            return None
        node = node[k]
        if isinstance(node, list):
            node = node[0] if node else None
    return node


def stamp(p):
    st = p.stat()
    when = datetime.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")
    return f"{st.st_size:>8} B  {when}"


def listing(d, depth=1, indent="  "):
    try:
        items = sorted(d.iterdir(), key=lambda p: p.name.lower())
    except OSError as e:
        say(f"{indent}(cannot read: {e})")
        return
    for i, p in enumerate(items):
        if i == LIMIT:
            say(f"{indent}... {len(items) - LIMIT} more")
            break
        if p.is_dir():
            say(f"{indent}{p.name}/")
            if depth > 1:
                listing(p, depth - 1, indent + "  ")
        else:
            say(f"{indent}{p.name}  {stamp(p)}")


def dump(p):
    say(f"--- {p}")
    try:
        text = p.read_text("utf-8", "replace")
    except OSError as e:
        say(f"(cannot read: {e})")
        return
    say(text[:TEXT_LIMIT].rstrip())
    if len(text) > TEXT_LIMIT:
        say(f"... {len(text) - TEXT_LIMIT} more characters")
    say("---")


def summary(p):
    """What a layout file says about itself, without printing all of it."""
    try:
        text = p.read_text("utf-8", "replace")
    except OSError as e:
        return f"(cannot read: {e})"
    m = parse_vdf(text).get("controller_mappings")
    m = m[0] if isinstance(m, list) and m else {}
    if not isinstance(m, dict):
        return "no controller_mappings block"
    keys = [k for k, v in m.items() if not isinstance(v, list)]
    blocks = sorted({k for k, v in m.items() if isinstance(v, list)})
    counts = {k: len(m[k]) for k in blocks}
    actions = first(m, "actions")
    action_sets = sorted(actions) if isinstance(actions, dict) else []
    info = {k: m[k] for k in keys}
    return (f"values={json.dumps(info, ensure_ascii=False)} blocks={counts}"
            + (f" action_sets={action_sets}" if action_sets else ""))


def controller_configs(steam):
    head("Steam Controller Configs")
    base = steam / "steamapps/common/Steam Controller Configs"
    if not base.is_dir():
        say(f"  no {base}")
        return
    for account in sorted(p for p in base.iterdir() if p.is_dir()):
        cfg = account / "config"
        say(f"account {account.name}: {cfg}")
        if not cfg.is_dir():
            listing(account, 2)
            continue
        games = sorted((p for p in cfg.iterdir() if p.is_dir()), key=lambda p: p.name.lower())
        say(f"  {len(games)} game folders")
        for g in games[:LIMIT]:
            say(f"  {g.name}/")
            for f in sorted(g.iterdir()):
                if f.is_file():
                    say(f"    {f.name}  {stamp(f)}")
                    if f.suffix == ".vdf":
                        say(f"      {summary(f)}")
        for f in sorted(p for p in cfg.iterdir() if p.is_file()):
            say(f"  {f.name}  {stamp(f)}")
        for f in sorted(cfg.glob("configset*.vdf")):
            dump(f)
        sample = sorted(cfg.glob("*/controller_neptune.vdf"), key=lambda p: p.stat().st_size)
        if sample:
            say("smallest Deck layout, in full:")
            dump(sample[0])


def userdata(steam):
    head("userdata: controller settings kept per account")
    for acc in sorted((steam / "userdata").glob("*")):
        if not acc.is_dir() or not acc.name.isdigit():
            continue
        say(f"account {acc.name}")
        remote = acc / "241100"
        if remote.is_dir():
            say(f"  {remote}:")
            listing(remote, 4, "    ")
        else:
            say(f"  no {remote}")
        for f in sorted((acc / "config").glob("*controller*")) + sorted((acc / "config").glob("*input*")):
            say(f"  config/{f.name}  {stamp(f)}" if f.is_file() else f"  config/{f.name}/")


def templates(steam):
    head("controller_base: Valve's templates (shown for every game)")
    base = steam / "controller_base"
    if not base.is_dir():
        say(f"  no {base}")
        return
    listing(base, 1)
    for name in ("templates", "template"):
        d = base / name
        if not d.is_dir():
            continue
        say(f"{d}:")
        for f in sorted(d.glob("*.vdf"))[:LIMIT * 2]:
            title = first(parse_vdf(f.read_text("utf-8", "replace")), "controller_mappings", "title")
            say(f"  {f.name}  {stamp(f)}  title={title!r}")
        probe = d / PROBE_TEMPLATE
        if probe.exists():
            say(f"probe template is in place: {probe}")


def other_files(steam):
    head("other controller files under Steam (games and caches skipped)")
    skip = {"common", "compatdata", "shadercache", "workshop", "downloading", "temp", "logs", "appcache",
            "depotcache", "config/htmlcache", "ubuntu12_32", "ubuntu12_64", "steamrt64"}
    found = 0
    for root, dirs, files in os.walk(steam):
        rel = os.path.relpath(root, steam)
        dirs[:] = [d for d in dirs if d not in skip and os.path.join(rel, d) not in skip
                   and not os.path.islink(os.path.join(root, d))]
        for f in files:
            if re.search(r"controller|configset", f, re.I):
                say(f"  {os.path.join(rel, f)}")
                found += 1
                if found >= LIMIT * 3:
                    say("  ... stopped")
                    return


def cdp_eval(port, expr):
    """Evaluate expr in Steam's SharedJSContext with DeckDrop's own websocket client."""
    ws_class = load_ws()
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=2) as r:
        targets = json.load(r)
    url = next((t["webSocketDebuggerUrl"] for t in targets
                if t.get("title") == "SharedJSContext" and t.get("webSocketDebuggerUrl")), None)
    if not url:
        raise RuntimeError("no SharedJSContext among " + ", ".join(repr(t.get("title")) for t in targets))
    ws = ws_class(url, 15)
    try:
        ws.send_text(json.dumps({"id": 1, "method": "Runtime.evaluate",
                                 "params": {"expression": expr, "awaitPromise": True, "returnByValue": True}}))
        while True:
            msg = json.loads(ws.recv_text())
            if msg.get("id") == 1:
                res = msg.get("result", {})
                if res.get("exceptionDetails"):
                    raise RuntimeError(json.dumps(res["exceptionDetails"])[:500])
                return (res.get("result") or {}).get("value")
    finally:
        ws.close()


def load_ws():
    """The WS class of the installed DeckDrop (~/deckdrop/deckdrop.py), else of this checkout."""
    installed = Path.home() / "deckdrop/deckdrop.py"
    if installed.is_file():
        spec = importlib.util.spec_from_file_location("deckdrop_installed", installed)
        spec.loader.exec_module(importlib.util.module_from_spec(spec))
    else:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
    from deckdrop.steam.cdp import WS
    return WS


API = r"""(() => {
  const out = {};
  const sc = window.SteamClient || {};
  out.namespaces = Object.keys(sc).sort();
  for (const ns of Object.keys(sc)) {
    const obj = sc[ns];
    if (!obj || typeof obj !== 'object') continue;
    const seen = new Set();
    for (let o = obj; o && o !== Object.prototype; o = Object.getPrototypeOf(o))
      for (const k of Object.getOwnPropertyNames(o)) seen.add(k);
    const names = [];
    for (const k of seen) {
      let f; try { f = obj[k]; } catch (e) { continue; }
      if (typeof f === 'function' && k !== 'constructor') names.push(k + '/' + f.length);
    }
    const hit = /input|controller|config|layout|template/i;
    if (ns === 'Input' || hit.test(ns)) out[ns] = names.sort();
    else {
      const some = names.filter(n => hit.test(n));
      if (some.length) out[ns + ' (matching)'] = some.sort();
    }
  }
  return out;
})()"""


def steam_api(port):
    head("SteamClient API (live Steam, read only: names/arity)")
    try:
        say(json.dumps(cdp_eval(port, API), indent=1, ensure_ascii=False))
    except Exception as e:  # noqa: BLE001
        say(f"  not available: {e}")
        say("  (DeckDrop's Steam control must be on: Settings in DeckDrop, then restart Steam)")


def template_dir(steam):
    base = steam / "controller_base"
    return next((base / n for n in ("templates", "template") if (base / n).is_dir()), base / "templates")


def put_template(steam, game):
    head(f"experiment: {game}'s layout as a template")
    configs = steam / "steamapps/common/Steam Controller Configs"
    src = next(iter(sorted(configs.glob(f"*/config/{glob_escape(game)}/controller_neptune.vdf"))), None)
    if src is None:
        say(f"  no controller_neptune.vdf for {game!r}: pick a folder name from the list above")
        return
    text = src.read_text("utf-8", "replace")
    text, n = re.subn(r'("title"\s+)"(?:\\.|[^"\\])*"', lambda m: m.group(1) + f'"{PROBE_TITLE}"', text, count=1)
    if not n:
        say("  the layout has no title line; copied as is")
    dest = template_dir(steam) / PROBE_TEMPLATE
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text, "utf-8")
    say(f"  copied {src}")
    say(f"      -> {dest}")
    say(f"  Now open any OTHER game's controller settings on the Deck: is '{PROBE_TITLE}' among the templates?")
    say("  If not, restart Steam and look again. Remove it with --cleanup.")


def glob_escape(name):
    return re.sub(r"([*?\[])", r"[\1]", name)


def cleanup(steam):
    head("cleanup")
    dest = template_dir(steam) / PROBE_TEMPLATE
    if dest.exists():
        dest.unlink()
        say(f"  removed {dest}")
    else:
        say(f"  nothing to remove at {dest}")


def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return socket.gethostname() + ".local"


def serve(report, port):
    """Share the report, and only it, until Ctrl+C: / shows it, /download saves it."""
    body = Path(report).read_bytes()
    name = Path(report).name

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            if self.path.startswith("/download"):
                self.send_header("Content-Disposition", f'attachment; filename="{name}"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            print("sent the report to", self.client_address[0])

    srv = http.server.HTTPServer(("0.0.0.0", port), Handler)
    base = f"http://{lan_ip()}:{srv.server_address[1]}"
    print(f"\nOpen on your phone or PC (same Wi-Fi):\n  {base}/          view\n  {base}/download  save the file")
    print("Ctrl+C to stop", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--steam", help="Steam folder (found automatically)")
    ap.add_argument("--port", type=int, default=int(os.environ.get("DECKDROP_CEF_PORT", "8080")))
    ap.add_argument("--no-cdp", action="store_true", help="do not ask the running Steam")
    ap.add_argument("--template", metavar="GAME", help="copy GAME's layout into Steam's templates")
    ap.add_argument("--cleanup", action="store_true", help="remove the probe template")
    ap.add_argument("--report", default=str(Path.home() / "deckdrop-layouts-probe.txt"))
    ap.add_argument("--no-serve", action="store_true", help="do not share the report on the network")
    ap.add_argument("--serve-port", type=int, default=8089)
    args = ap.parse_args()

    steam = steam_root(args.steam)
    say(f"DeckDrop layouts probe, {datetime.datetime.now():%Y-%m-%d %H:%M}, Python {sys.version.split()[0]}")
    say(f"Steam: {steam}")
    if args.cleanup:
        cleanup(steam)
    else:
        controller_configs(steam)
        userdata(steam)
        templates(steam)
        other_files(steam)
        if not args.no_cdp:
            steam_api(args.port)
        if args.template:
            put_template(steam, args.template)
    Path(args.report).write_text("\n".join(out) + "\n", "utf-8")
    print(f"\nreport saved to {args.report}")
    if not args.no_serve:
        serve(args.report, args.serve_port)


if __name__ == "__main__":
    main()
