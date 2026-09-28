#!/usr/bin/env python3
"""What the Steam Deck keeps about controller layouts: a one-off probe for the layouts feature.

Run it on the Deck (desktop or over SSH) and send back the report it writes:

    python3 layouts_probe.py                    look around, change nothing
    python3 layouts_probe.py --copy FROM TO     also copy a layout of game FROM to game TO
    python3 layouts_probe.py --cleanup          remove those copies again
    python3 layouts_probe.py --ask APPID        what the running Steam says about a game's layout
    python3 layouts_probe.py --select APPID URL ask Steam to switch the game to layout URL

FROM and TO are folder names from the "Steam Controller Configs" list of the report (an AppID
or the name of a non-Steam game). --copy puts FROM's layout, titled "DeckDrop probe", both
among Steam's templates and among TO's own saved layouts, and asks Steam what it sees for TO. The report goes to ~/deckdrop-layouts-probe.txt and to the screen,
and then it is shared on the home network until Ctrl+C: the script prints a link to open on a
phone or PC (only the report is served, nothing else). --no-serve skips that.
APPID is the game's Steam AppID (DeckDrop shows it on the game page). --select first copies
all of Steam Controller Configs to ~/deckdrop-probe-backup/<time>, then asks Steam to switch,
and reports what Steam answered and which layout files it changed.
Only --copy and --cleanup write anything themselves: the probe files, nothing else.
"""
import argparse
import datetime
import http.server
import importlib.util
import json
import os
import hashlib
import re
import shutil
import socket
import sys
import urllib.request
from pathlib import Path

LIMIT = 60                 # entries per listing
TEXT_LIMIT = 20000         # characters of a file printed as is
PROBE_TEMPLATE = "controller_neptune_deckdrop_probe.vdf"
PROBE_TITLE = "DeckDrop probe"
PROBE_PERSONAL = "deckdrop probe_0.vdf"
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
        text = p.read_text("utf-8-sig", "replace")
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
            title = first(parse_vdf(f.read_text("utf-8-sig", "replace")), "controller_mappings", "title")
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


def load_deckdrop():
    """Make `import deckdrop` work: the installed single file (~/deckdrop/deckdrop.py), else this checkout."""
    if "deckdrop" in sys.modules:
        return
    installed = Path.home() / "deckdrop/deckdrop.py"
    if installed.is_file():
        spec = importlib.util.spec_from_file_location("deckdrop_installed", installed)
        spec.loader.exec_module(importlib.util.module_from_spec(spec))
    else:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


def load_ws():
    load_deckdrop()
    from deckdrop.steam.cdp import WS
    return WS


def shortcuts():
    """[(name, appid)] of the non-Steam games, read by DeckDrop's own shortcuts.vdf parser."""
    load_deckdrop()
    from deckdrop.steam.library import shortcuts_index
    return sorted({(r["name"], r["appid"]) for r in shortcuts_index().values()}, key=lambda x: x[0].lower())


def list_shortcuts(steam):
    head("non-Steam games: name, appid and the layout folder named after them")
    try:
        items = shortcuts()
    except Exception as e:  # noqa: BLE001
        say(f"  cannot read the shortcuts: {e}")
        return
    folders = {p.name for p in (steam / "steamapps/common/Steam Controller Configs").glob("*/config/*") if p.is_dir()}
    for name, appid in items:
        guess = folder_for(name)
        say(f"  {name!r}  appid={appid}  folder={guess!r} {'(exists)' if guess in folders else '(none)'}")


def folder_for(name):
    """Our guess at Steam's folder name for a shortcut: lower case, keeping letters, digits, _, spaces and !."""
    return re.sub(r"[^\w !]", "", name.lower())


def appid_of(steam, folder):
    if folder.isdigit():
        return int(folder)
    try:
        return next((appid for name, appid in shortcuts() if folder_for(name) == folder), None)
    except Exception:  # noqa: BLE001
        return None


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


CONFIG = r"""(async () => {
  const I = SteamClient.Input, out = {}, msgs = [];
  const wait = (p, ms) => Promise.race([Promise.resolve(p), new Promise(r => setTimeout(() => r('(no answer)'), ms))]);
  let reg = null;
  try { reg = I.RegisterForControllerConfigInfoMessages(m => msgs.push(m)); } catch (e) { out.register = String(e); }
  for (const [name, args] of [['GetConfigForAppAndController', [APPID, 0]],
                              ['QueryControllerConfigsForApp', [APPID, 0, false]]]) {
    try { const v = await wait(I[name](...args), 4000); out[name] = v === undefined ? '(undefined)' : v; }
    catch (e) { out[name] = 'error: ' + e; }
  }
  await new Promise(r => setTimeout(r, 3000));
  out.ConfigInfoMessages = msgs;
  try { reg && reg.unregister && reg.unregister(); } catch (e) {}
  return JSON.parse(JSON.stringify(out, (k, v) => typeof v === 'bigint' ? String(v) : v));
})()"""


def steam_config(port, appid):
    head(f"what the running Steam reports for appid {appid} (read only)")
    try:
        say(json.dumps(cdp_eval(port, CONFIG.replace("APPID", str(int(appid)))), indent=1, ensure_ascii=False))
    except Exception as e:  # noqa: BLE001
        say(f"  not available: {e}")


PLAIN = r"""const wait = (p, ms) => Promise.race([Promise.resolve(p), new Promise(r => setTimeout(() => r('(no answer)'), ms))]);
  const plain = v => { try { return JSON.parse(JSON.stringify(v, (k, x) => typeof x === 'bigint' ? String(x)
    : typeof x === 'function' ? undefined : x instanceof Map ? Array.from(x.entries()) : x)); } catch (e) { return String(v); } };"""

ASK = r"""(async () => {
  const I = SteamClient.Input, out = {};
  PLAIN
  out.globals = Object.keys(window).filter(k => /controller|input/i.test(k));
  for (const k of out.globals) {
    const o = window[k];
    if (!o || typeof o !== 'object') continue;
    out['keys of ' + k] = Object.keys(o).slice(0, 80);
    for (const f of Object.keys(o))
      if (/controller|active|index|slot/i.test(f) && typeof o[f] !== 'function') out[k + '.' + f] = plain(o[f]);
  }
  try { out.GetControllerPreviouslySeen = plain(await wait(I.GetControllerPreviouslySeen(), 3000)); }
  catch (e) { out.GetControllerPreviouslySeen = 'error: ' + e; }
  for (let i = 0; i < 4; i++) {
    try { out['GetConfigForAppAndController(appid, ' + i + ')'] = plain(await wait(I.GetConfigForAppAndController(APPID, i), 3000)); }
    catch (e) { out['GetConfigForAppAndController(appid, ' + i + ')'] = 'error: ' + e; }
  }
  return out;
})()""".replace("PLAIN", PLAIN)

SELECT = r"""(async () => {
  const I = SteamClient.Input;
  PLAIN
  const get = async () => { try { return plain(await wait(I.GetConfigForAppAndController(APPID, INDEX), 3000)); } catch (e) { return 'error: ' + e; } };
  const before = await get();
  let result;
  try { result = plain(await wait(I.SetSelectedConfigForApp(APPID, INDEX, URL, false, 1), 5000)); } catch (e) { result = 'error: ' + e; }
  await new Promise(r => setTimeout(r, 2500));
  return {before, SetSelectedConfigForApp: result === undefined ? '(undefined)' : result, after: await get()};
})()""".replace("PLAIN", PLAIN)


def run_js(port, title, js):
    head(title)
    try:
        say(json.dumps(cdp_eval(port, js), indent=1, ensure_ascii=False))
    except Exception as e:  # noqa: BLE001
        say(f"  not available: {e}")


def configs_state(steam):
    base = steam / "steamapps/common/Steam Controller Configs"
    return {str(p.relative_to(base)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in base.rglob("*") if p.is_file()} if base.is_dir() else {}


def select_layout(steam, port, appid, url, index):
    base = steam / "steamapps/common/Steam Controller Configs"
    backup = Path.home() / "deckdrop-probe-backup" / datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    if base.is_dir():
        shutil.copytree(base, backup / base.name)
    head("backup before switching")
    say(f"  {base} -> {backup / base.name}")
    before = configs_state(steam)
    js = SELECT.replace("APPID", str(int(appid))).replace("INDEX", str(int(index))).replace("URL", json.dumps(url))
    run_js(port, f"switch appid {appid}, controller {index}, to {url}", js)
    after = configs_state(steam)
    head("layout files Steam changed")
    for k in sorted(set(before) | set(after)):
        if before.get(k) != after.get(k):
            say(f"  {'added' if k not in before else 'removed' if k not in after else 'changed'}: {k}")
            if k in after and k.endswith(".vdf") and k.split("/")[-1].startswith("configset"):
                dump(base / k)


def template_dir(steam):
    base = steam / "controller_base"
    return next((base / n for n in ("templates", "template") if (base / n).is_dir()), base / "templates")


def game_dir(steam, game):
    configs = steam / "steamapps/common/Steam Controller Configs"
    return next(iter(sorted(configs.glob(f"*/config/{glob_escape(game)}"))), None)


def retitle(text):
    return re.subn(r'("title"\s+)"(?:\\.|[^"\\])*"', lambda m: m.group(1) + f'"{PROBE_TITLE}"', text, count=1)[0]


def copy_layout(steam, src_game, dst_game):
    head(f"experiment: {src_game}'s layout copied to {dst_game}")
    src_dir, dst_dir = game_dir(steam, src_game), game_dir(steam, dst_game)
    if src_dir is None or dst_dir is None:
        say(f"  no folder {src_game if src_dir is None else dst_game!r}: pick folder names from the list above")
        return
    saved = sorted(p for p in src_dir.glob("*_[0-9]*.vdf"))
    src = saved[0] if saved else src_dir / "controller_neptune.vdf"
    if not src.is_file():
        say(f"  {src_dir} has no layout file")
        return
    text = retitle(src.read_text("utf-8-sig", "replace"))
    template = template_dir(steam) / PROBE_TEMPLATE
    personal = dst_dir / PROBE_PERSONAL
    template.parent.mkdir(parents=True, exist_ok=True)
    template.write_text(text, "utf-8")
    personal.write_text(text, "utf-8")
    say(f"  source   {src}")
    say(f"  template {template}")
    say(f"  personal {personal}")
    say(f"  On the Deck open {dst_game}'s controller settings -> Browse layouts. Is '{PROBE_TITLE}' under")
    say("  Templates? Under Your layouts? Then open another game: is it in its Templates?")
    say("  If you see nothing, restart Steam and look again. Remove the copies with --cleanup.")
    return dst_dir.name


def glob_escape(name):
    return re.sub(r"([*?\[])", r"[\1]", name)


def cleanup(steam):
    head("cleanup")
    found = [template_dir(steam) / PROBE_TEMPLATE]
    found += (steam / "steamapps/common/Steam Controller Configs").glob(f"*/config/*/{PROBE_PERSONAL}")
    removed = 0
    for p in found:
        if p.exists():
            p.unlink()
            say(f"  removed {p}")
            removed += 1
    if not removed:
        say("  no probe files left")


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
    ap.add_argument("--copy", nargs=2, metavar=("FROM", "TO"), help="copy FROM's layout to TO and to the templates")
    ap.add_argument("--cleanup", action="store_true", help="remove the probe files")
    ap.add_argument("--ask", metavar="APPID", type=int, help="what Steam says about the game's layout")
    ap.add_argument("--select", nargs=2, metavar=("APPID", "URL"), help="switch the game to layout URL")
    ap.add_argument("--index", type=int, default=0, help="controller index for --select (default 0)")
    ap.add_argument("--report", default=str(Path.home() / "deckdrop-layouts-probe.txt"))
    ap.add_argument("--no-serve", action="store_true", help="do not share the report on the network")
    ap.add_argument("--serve-port", type=int, default=8089)
    args = ap.parse_args()

    steam = steam_root(args.steam)
    say(f"DeckDrop layouts probe, {datetime.datetime.now():%Y-%m-%d %H:%M}, Python {sys.version.split()[0]}")
    say(f"Steam: {steam}")
    if args.cleanup:
        cleanup(steam)
    elif args.ask or args.select:
        list_shortcuts(steam)
        appid = args.ask or int(args.select[0])
        run_js(args.port, f"what the running Steam says about appid {appid} (read only)", ASK.replace("APPID", str(appid)))
        if args.select:
            select_layout(steam, args.port, appid, args.select[1], args.index)
    else:
        controller_configs(steam)
        userdata(steam)
        templates(steam)
        other_files(steam)
        list_shortcuts(steam)
        if not args.no_cdp:
            steam_api(args.port)
        if args.copy:
            target = copy_layout(steam, *args.copy)
            appid = target and appid_of(steam, target)
            if appid and not args.no_cdp:
                steam_config(args.port, appid)
            elif target:
                say(f"  (no appid found for {target!r}, so Steam was not asked about it)")
    Path(args.report).write_text("\n".join(out) + "\n", "utf-8")
    print(f"\nreport saved to {args.report}")
    if not args.no_serve:
        serve(args.report, args.serve_port)


if __name__ == "__main__":
    main()
