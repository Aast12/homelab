#!/usr/bin/env python3
"""Writes ADMIN_USER/ADMIN_PASSWORD into qBittorrent's and nzbget's config
files before they start. Run by the downloads Pod's init container.

qBittorrent has no way to set a first password except the random one it
prints in its log, and nzbget's image breaks on passwords with spaces when
given as environment variables. qBittorrent also gets LocalHostAuth=false,
so gluetun's port-forwarding command inside the Pod needs no login.
A file that doesn't exist yet (first start) is left for the app to create;
the setup Job sets the login through the app's API then. Leaves files alone
when they already match."""
import base64
import hashlib
import hmac
import os
import re

USER, PASSWORD = os.environ["ADMIN_USER"], os.environ["ADMIN_PASSWORD"]
QBT = os.environ.get("QBT_CONF", "/qbittorrent/qBittorrent/qBittorrent.conf")
NZBGET = os.environ.get("NZBGET_CONF", "/nzbget/nzbget.conf")


def ini_set(path, section, want, label, default=None):
    """Set keys in one [section] of an INI-style file (section=None: no sections).
    A missing file is created from `default` if given, else left for the app."""
    if not os.path.exists(path):
        if default is None:
            print(f"{label}: no config yet; the setup Job sets the login after first start")
            return
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "w").write(default)
        for p in (os.path.dirname(path), path):
            try:
                os.chown(p, 1000, 1000)
            except PermissionError:
                pass
    lines = open(path).read().splitlines()
    cur, values = None, {}
    for line in lines:
        if line.startswith("[") and line.endswith("]"):
            cur = line[1:-1]
        elif "=" in line and cur == section and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            values[k] = v
    todo = {k: v(values.get(k)) if callable(v) else v for k, v in want.items()}
    todo = {k: v for k, v in todo.items() if v is not None and values.get(k) != v}
    if not todo:
        print(f"{label}: login already set")
        return
    out, cur, done = [], None, set()
    for line in lines:
        if line.startswith("[") and line.endswith("]"):
            if cur == section:
                out += [f"{k}={v}" for k, v in todo.items() if k not in done]
                done |= set(todo)
            cur = line[1:-1]
        elif cur == section and "=" in line and line.split("=", 1)[0] in todo:
            k = line.split("=", 1)[0]
            out.append(f"{k}={todo[k]}")
            done.add(k)
            continue
        out.append(line)
    if cur == section:
        out += [f"{k}={v}" for k, v in todo.items() if k not in done]
    elif section not in [l[1:-1] for l in lines if l.startswith("[")]:
        out += ["", f"[{section}]"] + [f"{k}={v}" for k, v in todo.items()]
    open(path, "w").write("\n".join(out) + "\n")
    try:
        os.chown(path, 1000, 1000)  # the apps run as 1000:1000
    except PermissionError:
        pass
    print(f"{label}: login set ({', '.join(sorted(todo))})")


def pbkdf2(password, salt):
    return hashlib.pbkdf2_hmac("sha512", password.encode(), salt, 100_000, 64)


def qbt_hash(stored):
    """Keep the stored hash if it matches PASSWORD, else make a new one."""
    m = re.fullmatch(r'"?@ByteArray\(([^:]+):([^)]+)\)"?', stored or "")
    if m and hmac.compare_digest(pbkdf2(PASSWORD, base64.b64decode(m.group(1))), base64.b64decode(m.group(2))):
        return stored
    salt = os.urandom(16)
    return '"@ByteArray({}:{})"'.format(base64.b64encode(salt).decode(),
                                        base64.b64encode(pbkdf2(PASSWORD, salt)).decode())


# On a first start there is no qBittorrent.conf yet, and without one qBittorrent
# starts with a random password the setup Job can't know. Write the image's
# defaults (accepted legal notice, no UPnP) so the login can go in now; the
# image only copies its own defaults when the file is missing.
QBT_DEFAULT = """[LegalNotice]
Accepted=true

[Preferences]
Connection\\UPnP=false

[WebUI]
Address=*
ServerDomains=*
"""
# qBittorrent 5 keeps Web UI settings in [WebUI] (4.x used [Preferences] WebUI\...).
ini_set(QBT, "WebUI", {"Username": USER, "Password_PBKDF2": qbt_hash, "LocalHostAuth": "false"}, "qBittorrent",
        default=QBT_DEFAULT)
# nzbget.conf has no sections; ini_set's section=None matches lines before any [header].
ini_set(NZBGET, None, {"ControlUsername": USER, "ControlPassword": PASSWORD}, "nzbget")
