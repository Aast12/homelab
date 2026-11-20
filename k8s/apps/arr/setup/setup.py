#!/usr/bin/env python3
"""Sets every media app's login and wires the apps together.

Run by the media-setup Job after `lab.sh apply arr`. Every step reads the
current state first and only changes what differs, so it is safe to re-run;
re-run it after changing secrets.env. Values come from the `arr` Secret
(secrets.env); addresses default to the cluster's Service names.

Python standard library only, so it runs on a plain python image.
"""
import base64
import http.cookiejar
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

E = os.environ
USER = E["ADMIN_USER"]
PASSWORD = E["ADMIN_PASSWORD"]
WAIT = int(E.get("WAIT_SECONDS", "900"))

# Addresses as the apps reach each other (and as this job reaches them).
URL = {
    "sonarr": E.get("SONARR_URL", "http://sonarr"),
    "radarr": E.get("RADARR_URL", "http://radarr"),
    "lidarr": E.get("LIDARR_URL", "http://lidarr"),
    "prowlarr": E.get("PROWLARR_URL", "http://prowlarr"),
    "bazarr": E.get("BAZARR_URL", "http://bazarr"),
    "jellyfin": E.get("JELLYFIN_URL", "http://jellyfin"),
    "jellyseerr": E.get("JELLYSEERR_URL", "http://jellyseerr"),
    "flaresolverr": E.get("FLARESOLVERR_URL", "http://flaresolverr:8191"),
    "qbittorrent": E.get("QBITTORRENT_URL", "http://downloads:8080"),
    "nzbget": E.get("NZBGET_URL", "http://downloads:6789"),
}
KEY = {app: E[f"{app.upper()}_API_KEY"] for app in ("sonarr", "radarr", "lidarr", "prowlarr")}

# One library volume, mounted at /data in every app, so downloads move into
# the library as hardlinks instead of copies.
MEDIA = {"sonarr": "/data/media/tv", "radarr": "/data/media/movies", "lidarr": "/data/media/music"}
CATEGORY = {"sonarr": "tv", "radarr": "movies", "lidarr": "music"}
SERVARR_API = {"sonarr": "v3", "radarr": "v3", "lidarr": "v1", "prowlarr": "v1"}

changed = 0


def log(app, msg):
    print(f"[{app}] {msg}", flush=True)


def note(app, what, did):
    """Log a step; `did` is True when it changed something."""
    global changed
    if did:
        changed += 1
    log(app, f"{what}: {'set' if did else 'already set'}")


def hostport(url):
    u = urllib.parse.urlsplit(url)
    return u.hostname, u.port or (443 if u.scheme == "https" else 80)


class Client:
    """A tiny HTTP client with cookies, JSON and form bodies."""

    def __init__(self, base, headers=None):
        self.base = base.rstrip("/")
        self.headers = headers or {}
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def call(self, method, path, body=None, form=None, query=None, headers=None, raw=False):
        url = self.base + path
        if query:
            url += "?" + urllib.parse.urlencode(query, doseq=True)
        h = dict(self.headers, **(headers or {}))
        data = None
        if form is not None:
            data = urllib.parse.urlencode(form, doseq=True).encode()
            h["Content-Type"] = "application/x-www-form-urlencoded"
        elif body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method, headers=h)
        try:
            with self.opener.open(req, timeout=60) as resp:
                text = resp.read().decode()
        except urllib.error.HTTPError as err:
            err.msg = f"{method} {path}: {err.read().decode(errors='replace')[:600]}"
            raise
        if raw:
            return text
        return json.loads(text) if text.strip() else None

    def get(self, path, **kw):
        return self.call("GET", path, **kw)

    def post(self, path, body=None, **kw):
        return self.call("POST", path, body=body, **kw)

    def put(self, path, body=None, **kw):
        return self.call("PUT", path, body=body, **kw)


def wait_for(app, url):
    """Wait until the app answers HTTP at all (any status)."""
    deadline = time.time() + WAIT
    while True:
        try:
            urllib.request.urlopen(url, timeout=10)
            return
        except urllib.error.HTTPError:
            return
        except Exception as err:  # not up yet
            if time.time() > deadline:
                sys.exit(f"[{app}] not reachable at {url} after {WAIT}s: {err}")
            log(app, "waiting to come up...")
            time.sleep(10)


# ----------------------------------------------------------------- Servarr
def servarr(app):
    return Client(f"{URL[app]}/api/{SERVARR_API[app]}", {"X-Api-Key": KEY[app]})


def fields_of(item):
    return {f["name"]: f.get("value") for f in item.get("fields", [])}


def provider(api, kind, implementation, name, fields, extra=None, secret_fields=()):
    """Create or update a Servarr provider (download client, application,
    notification, indexer proxy) named `name`. Returns True if it changed.

    Servarr masks secret fields in what it returns, so when everything else
    matches, the saved provider is tested as it is: a pass means the stored
    secret still works and nothing is written."""
    existing = next((x for x in api.get(f"/{kind}") if x["name"] == name), None)
    if existing is None:
        item = next(s for s in api.get(f"/{kind}/schema") if s["implementation"] == implementation)
    else:
        item = existing
        current = fields_of(existing)
        same = all(current.get(k) == v for k, v in fields.items() if k not in secret_fields)
        same = same and all(existing.get(k) == v for k, v in (extra or {}).items())
        if same and not secret_fields:
            return False
        if same:
            try:
                api.post(f"/{kind}/test", existing)
                return False
            except urllib.error.HTTPError:
                pass  # the stored secret no longer works: write the current one
    item = json.loads(json.dumps(item))
    item["name"] = name
    item.update(extra or {})
    for f in item["fields"]:
        if f["name"] in fields:
            f["value"] = fields[f["name"]]
    if existing is None:
        api.post(f"/{kind}", item, query={"forceSave": "true"})
    else:
        api.put(f"/{kind}/{existing['id']}", item, query={"forceSave": "true"})
    return True


def form_login_ok(base, path, form):
    """POST a login form without following the redirect; Servarr apps send
    a failed login back to /login?...loginFailed=true."""
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **kw):
            return None
    opener = urllib.request.build_opener(NoRedirect)
    req = urllib.request.Request(base + path, data=urllib.parse.urlencode(form).encode(), method="POST")
    try:
        opener.open(req, timeout=30)
        return True
    except urllib.error.HTTPError as err:
        return err.code in (301, 302, 303) and "loginFailed" not in err.headers.get("Location", "")


def servarr_login(app):
    """Forms login with ADMIN_USER/ADMIN_PASSWORD."""
    api = servarr(app)
    host = api.get("/config/host")
    ok = (host.get("authenticationMethod") == "forms" and host.get("username") == USER
          and form_login_ok(URL[app], "/login", {"username": USER, "password": PASSWORD}))
    if not ok:
        host.update(authenticationMethod="forms", authenticationRequired="enabled",
                    username=USER, password=PASSWORD, passwordConfirmation=PASSWORD)
        api.put(f"/config/host/{host['id']}", host)
    note(app, "login", not ok)


def root_folder(app):
    api = servarr(app)
    path = MEDIA[app]
    if any(r["path"].rstrip("/") == path for r in api.get("/rootfolder")):
        return note(app, f"root folder {path}", False)
    body = {"path": path}
    if app == "lidarr":  # Lidarr's root folders carry the defaults for new artists
        body.update(name="Music",
                    defaultQualityProfileId=api.get("/qualityprofile")[0]["id"],
                    defaultMetadataProfileId=api.get("/metadataprofile")[0]["id"],
                    defaultMonitorOption="all", defaultNewItemMonitorOption="all", defaultTags=[])
    api.post("/rootfolder", body)
    note(app, f"root folder {path}", True)


def download_clients(app):
    api = servarr(app)
    cat_field = {"sonarr": "tvCategory", "radarr": "movieCategory", "lidarr": "musicCategory"}[app]
    qhost, qport = hostport(URL["qbittorrent"])
    nhost, nport = hostport(URL["nzbget"])
    did = provider(api, "downloadclient", "QBittorrent", "qBittorrent",
                   {"host": qhost, "port": qport, "useSsl": False, "username": USER,
                    "password": PASSWORD, cat_field: CATEGORY[app]},
                   {"enable": True}, secret_fields=("password",))
    note(app, "download client qBittorrent", did)
    did = provider(api, "downloadclient", "Nzbget", "NZBGet",
                   {"host": nhost, "port": nport, "useSsl": False, "username": USER,
                    "password": PASSWORD, cat_field: CATEGORY[app]},
                   {"enable": True}, secret_fields=("password",))
    note(app, "download client NZBGet", did)


def jellyfin_connection(app, jellyfin_key):
    """Tell Jellyfin to rescan when an app imports something."""
    api = servarr(app)
    host, port = hostport(URL["jellyfin"])
    events = {k: True for k in ("onDownload", "onUpgrade", "onRename", "onReleaseImport")}
    schema = next(s for s in api.get("/notification/schema") if s["implementation"] == "MediaBrowser")
    events = {k: v for k, v in events.items() if k in schema}
    did = provider(api, "notification", "MediaBrowser", "Jellyfin",
                   {"host": host, "port": port, "useSsl": False, "apiKey": jellyfin_key,
                    "updateLibrary": True, "notify": False},
                   events, secret_fields=("apiKey",))
    note(app, "Jellyfin library updates", did)


def prowlarr_wiring():
    api = servarr("prowlarr")
    tags = api.get("/tag")
    tag = next((t for t in tags if t["label"] == "flaresolverr"), None) or api.post("/tag", {"label": "flaresolverr"})
    did = provider(api, "indexerProxy", "FlareSolverr", "FlareSolverr",
                   {"host": URL["flaresolverr"].rstrip("/") + "/", "requestTimeout": 60},
                   {"tags": [tag["id"]]})
    note("prowlarr", "FlareSolverr proxy (tag indexers 'flaresolverr' to use it)", did)
    for app, impl in (("sonarr", "Sonarr"), ("radarr", "Radarr"), ("lidarr", "Lidarr")):
        did = provider(api, "applications", impl, impl,
                       {"prowlarrUrl": URL["prowlarr"], "baseUrl": URL[app], "apiKey": KEY[app]},
                       {"syncLevel": "fullSync"}, secret_fields=("apiKey",))
        note("prowlarr", f"pushes indexers to {impl}", did)


# ----------------------------------------------------------------- qBittorrent
def qbittorrent():
    """The login itself is written into qBittorrent.conf by the downloads
    Pod's init container; this sets where downloads go."""
    q = Client(URL["qbittorrent"])
    try:  # 5.x answers 204 with a session cookie; a wrong login is a 401
        q.call("POST", "/api/v2/auth/login", form={"username": USER, "password": PASSWORD}, raw=True)
    except urllib.error.HTTPError:
        sys.exit("[qbittorrent] login failed: is the downloads Pod running with the current secrets.env?")
    note("qbittorrent", "login", False)
    prefs = q.get("/api/v2/app/preferences")
    want = {"save_path": "/data/torrents", "temp_path_enabled": False, "auto_tmm_enabled": True,
            "torrent_changed_tmm_enabled": True, "save_path_changed_tmm_enabled": True,
            "category_changed_tmm_enabled": True}
    norm = lambda v: v.rstrip("/") if isinstance(v, str) else v  # noqa: E731
    diff = {k: v for k, v in want.items() if norm(prefs.get(k)) != norm(v)}
    if diff:
        q.call("POST", "/api/v2/app/setPreferences", form={"json": json.dumps(diff)}, raw=True)
    note("qbittorrent", "downloads to /data/torrents", bool(diff))
    cats = q.get("/api/v2/torrents/categories") or {}
    for name in CATEGORY.values():
        path = f"/data/torrents/{name}"
        cur = cats.get(name)
        if cur is None:
            q.call("POST", "/api/v2/torrents/createCategory", form={"category": name, "savePath": path}, raw=True)
        elif cur.get("savePath", "").rstrip("/") != path:
            q.call("POST", "/api/v2/torrents/editCategory", form={"category": name, "savePath": path}, raw=True)
        note("qbittorrent", f"category {name} -> {path}", cur is None or cur.get("savePath", "").rstrip("/") != path)


# ----------------------------------------------------------------- NZBGet
def nzbget():
    """Sets nzbget's own login (its image's NZBGET_PASS breaks on spaces),
    its folders and categories, and the Usenet server if one is given."""
    def rpc(method, *params, login=(USER, PASSWORD)):
        cred = base64.b64encode(f"{login[0]}:{login[1]}".encode()).decode()
        req = urllib.request.Request(f"{URL['nzbget']}/jsonrpc", method="POST",
                                     data=json.dumps({"method": method, "params": list(params)}).encode(),
                                     headers={"Authorization": f"Basic {cred}", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)["result"]

    login = (USER, PASSWORD)
    try:
        rpc("version", login=login)
    except urllib.error.HTTPError:
        login = ("nzbget", "tegbzn6789")  # the image's default, until this job first runs
        try:
            rpc("version", login=login)
        except urllib.error.HTTPError:
            sys.exit("[nzbget] login failed with secrets.env and with the default: changed by hand?")
    _rpc = rpc
    rpc = lambda method, *p: _rpc(method, *p, login=login)  # noqa: E731

    conf = {o["Name"]: o["Value"] for o in rpc("config")}
    want = {"ControlUsername": USER, "ControlPassword": PASSWORD,
            "DestDir": "/data/usenet/completed", "InterDir": "/data/usenet/intermediate"}
    for i, name in enumerate(CATEGORY.values(), start=1):
        want[f"Category{i}.Name"] = name
        want[f"Category{i}.DestDir"] = ""
    for i in range(len(CATEGORY) + 1, 10):  # drop the image's default categories
        if f"Category{i}.Name" in conf:
            want[f"Category{i}.Name"] = ""
    if E.get("USENET_HOST"):
        want.update({
            "Server1.Active": "yes", "Server1.Name": E.get("USENET_NAME", E["USENET_HOST"]),
            "Server1.Host": E["USENET_HOST"], "Server1.Port": E.get("USENET_PORT", "563"),
            "Server1.Encryption": "yes" if E.get("USENET_SSL", "yes") == "yes" else "no",
            "Server1.Username": E.get("USENET_USER", ""), "Server1.Password": E.get("USENET_PASSWORD", ""),
            "Server1.Connections": E.get("USENET_CONNECTIONS", "8"),
        })
    diff = {k: v for k, v in want.items() if conf.get(k, "") != v}
    if diff:
        conf.update(diff)
        if not rpc("saveconfig", [{"Name": k, "Value": v} for k, v in conf.items()]):
            sys.exit("[nzbget] saveconfig failed")
        rpc("reload")
        deadline = time.time() + 120  # reload restarts nzbget; wait for the new login
        while True:
            try:
                _rpc("version", login=(USER, PASSWORD))
                break
            except Exception:
                if time.time() > deadline:
                    sys.exit("[nzbget] did not come back after applying its settings")
                time.sleep(3)
    note("nzbget", "login, folders, categories" + (" and Usenet server" if E.get("USENET_HOST") else ""), bool(diff))
    if not E.get("USENET_HOST"):
        log("nzbget", "no USENET_HOST in secrets.env: no news server set")


# ----------------------------------------------------------------- Bazarr
def bazarr():
    """Bazarr gets BAZARR_API_KEY as its API key, so this job can always
    reach it, even after the login changes."""
    b = Client(URL["bazarr"], {"X-API-KEY": E["BAZARR_API_KEY"]})
    try:
        b.get("/api/system/status")
    except urllib.error.HTTPError:
        # Not set yet: the page embeds the key Bazarr generated, for its own
        # frontend, either straight away (no login set yet) or once signed
        # in. Use it once to set ours.
        web = Client(URL["bazarr"])
        try:
            web.post("/api/system/account", form={"action": "login", "username": USER,
                                                   "password": PASSWORD}, raw=True)
        except urllib.error.HTTPError:
            pass  # no login set yet
        page = web.get("/", raw=True)
        m = re.search(r'apiKey"?\s*:\s*"([0-9a-f]{32})"', page)
        if not m:
            sys.exit("[bazarr] BAZARR_API_KEY is not its API key and no generated key is visible:"
                     " was the key changed by hand?")
        b.headers["X-API-KEY"] = m.group(1)
    s = b.get("/api/system/settings")
    langs = [x.strip() for x in E.get("SUBTITLE_LANGUAGES", "en,es").split(",") if x.strip()]
    providers = ["podnapisi"]
    form = {
        "settings-auth-type": "form", "settings-auth-username": USER, "settings-auth-password": PASSWORD,
        "settings-auth-apikey": E["BAZARR_API_KEY"],
        "settings-general-use_sonarr": "true", "settings-general-use_radarr": "true",
        "settings-general-serie_default_enabled": "true", "settings-general-serie_default_profile": "1",
        "settings-general-movie_default_enabled": "true", "settings-general-movie_default_profile": "1",
    }
    for app in ("sonarr", "radarr"):
        host, port = hostport(URL[app])
        form.update({f"settings-{app}-ip": host, f"settings-{app}-port": str(port),
                     f"settings-{app}-apikey": KEY[app], f"settings-{app}-ssl": "false",
                     f"settings-{app}-base_url": "/"})
    if E.get("OPENSUBTITLES_USER"):
        providers.append("opensubtitlescom")
        form["settings-opensubtitlescom-username"] = E["OPENSUBTITLES_USER"]
        form["settings-opensubtitlescom-password"] = E.get("OPENSUBTITLES_PASSWORD", "")
    if E.get("SUBDL_API_KEY"):
        providers.append("subdl")
        form["settings-subdl-api_key"] = E["SUBDL_API_KEY"]
    if E.get("ADDIC7ED_USER"):
        providers.append("addic7ed")
        form["settings-addic7ed-username"] = E["ADDIC7ED_USER"]
        form["settings-addic7ed-password"] = E.get("ADDIC7ED_PASSWORD", "")
    form["settings-general-enabled_providers"] = providers

    g = s["general"]
    login_ok = form_login_ok(URL["bazarr"], "/api/system/account",
                             {"action": "login", "username": USER, "password": PASSWORD})
    same = (s["auth"]["type"] == "form" and s["auth"]["username"] == USER and login_ok
            and b.headers["X-API-KEY"] == E["BAZARR_API_KEY"]
            and g["use_sonarr"] and g["use_radarr"] and sorted(g["enabled_providers"]) == sorted(providers)
            and all(str(s[app]["apikey"]) == KEY[app] and str(s[app]["ip"]) == hostport(URL[app])[0]
                    for app in ("sonarr", "radarr"))
            and str(g["serie_default_profile"]) == "1" and str(g["movie_default_profile"]) == "1")
    profiles = b.get("/api/system/languages/profiles")
    want_items = [{"id": i, "language": code, "audio_exclude": "False", "audio_only_include": "False",
                   "hi": "False", "forced": "False"} for i, code in enumerate(langs, start=1)]
    profile = next((p for p in profiles if p["profileId"] == 1), None)
    same = same and profile is not None and [i["language"] for i in profile["items"]] == langs
    if not same:
        form["languages-enabled"] = langs
        form["languages-profiles"] = json.dumps([{
            "profileId": 1, "name": "Default", "cutoff": None, "items": want_items,
            "mustContain": [], "mustNotContain": [], "originalFormat": False, "tag": None}])
        b.post("/api/system/settings", form=form, raw=True)
    note("bazarr", f"login, Sonarr, Radarr, subtitles {','.join(langs)} from {', '.join(providers)}", not same)


# ----------------------------------------------------------------- key store
SA = "/var/run/secrets/kubernetes.io/serviceaccount"
STORE = "jellyfin-api-key"


def kube(method, path, body=None):
    import ssl
    ctx = ssl.create_default_context(cafile=f"{SA}/ca.crt")
    token = open(f"{SA}/token").read()
    req = urllib.request.Request(
        f"https://kubernetes.default.svc{path}", method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, context=ctx, timeout=30) as r:
        return json.load(r)


def stored_jellyfin_key():
    """The Jellyfin API key this job created, kept so it can reset the
    admin password when ADMIN_PASSWORD changes."""
    if E.get("KEY_STORE_FILE"):  # testing outside the cluster
        f = E["KEY_STORE_FILE"]
        return open(f).read().strip() if os.path.exists(f) else None
    ns = open(f"{SA}/namespace").read()
    try:
        data = kube("GET", f"/api/v1/namespaces/{ns}/secrets/{STORE}")["data"]
        return base64.b64decode(data["key"]).decode()
    except urllib.error.HTTPError as err:
        if err.code == 404:
            return None
        raise


def store_jellyfin_key(key):
    if E.get("KEY_STORE_FILE"):
        open(E["KEY_STORE_FILE"], "w").write(key)
        return
    ns = open(f"{SA}/namespace").read()
    body = {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": STORE},
            "data": {"key": base64.b64encode(key.encode()).decode()}}
    try:
        kube("PUT", f"/api/v1/namespaces/{ns}/secrets/{STORE}", body)
    except urllib.error.HTTPError as err:
        if err.code != 404:
            raise
        kube("POST", f"/api/v1/namespaces/{ns}/secrets", body)


# ----------------------------------------------------------------- Jellyfin
JF_AUTH = 'MediaBrowser Client="homelab-setup", Device="media-setup", DeviceId="homelab-media-setup", Version="1"'


def jellyfin():
    """First-run admin, the three libraries, and an API key for the others."""
    j = Client(URL["jellyfin"], {"Authorization": JF_AUTH})
    info = j.get("/System/Info/Public")
    first = not info.get("StartupWizardCompleted")
    if first:
        j.post("/Startup/Configuration", {"UICulture": "en-US", "MetadataCountryCode": "US",
                                          "PreferredMetadataLanguage": "en"})
        j.get("/Startup/User")
        j.post("/Startup/User", {"Name": USER, "Password": PASSWORD})
        j.post("/Startup/RemoteAccess", {"EnableRemoteAccess": True, "EnableAutomaticPortMapping": False})
        j.post("/Startup/Complete")
    note("jellyfin", "first-run admin", first)
    try:
        auth = j.post("/Users/AuthenticateByName", {"Username": USER, "Pw": PASSWORD})
        reset = False
    except urllib.error.HTTPError:
        # ADMIN_PASSWORD changed: reset it with the API key kept from before
        key = stored_jellyfin_key()
        if not key:
            sys.exit("[jellyfin] admin login failed and no stored API key to reset it with")
        admin = Client(URL["jellyfin"], {"Authorization": JF_AUTH + f', Token="{key}"'})
        user = next((u for u in admin.get("/Users") if u["Name"] == USER), None)
        if user is None:
            sys.exit(f"[jellyfin] no user named {USER}: ADMIN_USER changed after the first run?")
        admin.post(f"/Users/{user['Id']}/Password", {"NewPw": PASSWORD})
        auth = j.post("/Users/AuthenticateByName", {"Username": USER, "Pw": PASSWORD})
        reset = True
    note("jellyfin", "admin password", reset)
    j.headers["Authorization"] = JF_AUTH + f', Token="{auth["AccessToken"]}"'

    have = {f["Name"] for f in j.get("/Library/VirtualFolders")}
    for name, kind, path in (("Movies", "movies", "/data/media/movies"),
                             ("Shows", "tvshows", "/data/media/tv"),
                             ("Music", "music", "/data/media/music")):
        if name not in have:
            j.post("/Library/VirtualFolders", {"LibraryOptions": {}},
                   query={"name": name, "collectionType": kind, "paths": path, "refreshLibrary": "true"})
        note("jellyfin", f"library {name} -> {path}", name not in have)

    keys = j.get("/Auth/Keys")["Items"]
    key = next((k["AccessToken"] for k in keys if k["AppName"] == "homelab"), None)
    if key is None:
        j.post("/Auth/Keys", query={"app": "homelab"})
        key = next(k["AccessToken"] for k in j.get("/Auth/Keys")["Items"] if k["AppName"] == "homelab")
    if stored_jellyfin_key() != key:
        store_jellyfin_key(key)
    note("jellyfin", "API key for the other apps", False)
    return key


# ----------------------------------------------------------------- Jellyseerr
def jellyseerr():
    s = Client(f"{URL['jellyseerr']}/api/v1")
    public = s.get("/settings/public")
    first = not public["initialized"]
    host, port = hostport(URL["jellyfin"])
    login = {"username": USER, "password": PASSWORD}
    new_server = public.get("mediaServerType") != 2  # 2 = Jellyfin, 4 = none yet
    if new_server:  # the first sign-in also tells Seerr where Jellyfin is
        login.update(hostname=host, port=port, useSsl=False, urlBase="",
                     email=E.get("ADMIN_EMAIL", f"{USER}@lab.local"), serverType=2)
    try:
        s.post("/auth/jellyfin", login)
    except urllib.error.HTTPError as err:
        sys.exit(f"[jellyseerr] Jellyfin sign-in failed ({err.code}): {err.msg}")
    note("jellyseerr", "signed in with the Jellyfin admin", new_server)

    libs = s.post("/settings/jellyfin/library/sync")
    off = [lib for lib in libs if not lib.get("enabled")]
    for lib in off:
        s.put(f"/settings/jellyfin/library/{lib['id']}", {"enabled": True})
    note("jellyseerr", "Jellyfin libraries enabled", bool(off))

    for app, extra in (("radarr", {"minimumAvailability": "released"}),
                       ("sonarr", {"enableSeasonFolders": True})):
        host, port = hostport(URL[app])
        conn = {"hostname": host, "port": port, "apiKey": KEY[app], "useSsl": False, "baseUrl": ""}
        test = s.post(f"/settings/{app}/test", conn)
        profile = test["profiles"][0]
        want = dict(conn, name=app.capitalize(), activeProfileId=profile["id"],
                    activeProfileName=profile["name"], activeDirectory=MEDIA[app], is4k=False,
                    isDefault=True, externalUrl="", syncEnabled=False, preventSearch=False, **extra)
        if app == "sonarr":
            want.update(activeAnimeProfileId=profile["id"], activeAnimeProfileName=profile["name"],
                        activeAnimeDirectory=MEDIA[app])
        cur = next((x for x in s.get(f"/settings/{app}") if x["name"] == want["name"]), None)
        if cur is None:
            s.post(f"/settings/{app}", want)
        elif any(cur.get(k) != v for k, v in want.items() if k not in ("activeProfileId", "activeProfileName",
                                                                        "activeAnimeProfileId",
                                                                        "activeAnimeProfileName")):
            s.put(f"/settings/{app}/{cur['id']}", dict(cur, **want))
        else:
            note("jellyseerr", f"{app.capitalize()} for requests", False)
            continue
        note("jellyseerr", f"{app.capitalize()} for requests", True)

    if first:
        s.post("/settings/initialize")


# ----------------------------------------------------------------- main
def main():
    for app, url in URL.items():
        wait_for(app, url)
    jf_key = jellyfin()
    qbittorrent()  # the download clients first: the apps test them when adding
    nzbget()
    for app in ("sonarr", "radarr", "lidarr", "prowlarr"):
        servarr_login(app)
    for app in ("sonarr", "radarr", "lidarr"):
        root_folder(app)
        download_clients(app)
        jellyfin_connection(app, jf_key)
    prowlarr_wiring()
    bazarr()
    jellyseerr()
    print(f"done: {changed} change(s)", flush=True)


if __name__ == "__main__":
    main()
