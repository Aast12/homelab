#!/usr/bin/env python3
"""Points the public DNS records for DOMAIN at the Pi's NetBird address.

Away from home, devices resolve DOMAIN and *.DOMAIN through public DNS, get the
Pi's NetBird address, and reach the Pi through the tunnel; NetBird addresses
never clash with the network the device is on. At home, Blocky answers with the
Pi's home address instead. Anyone outside your NetBird network gets an address
they cannot reach.

Run by setup.sh with DOMAIN, CF_DNS_API_TOKEN and NETBIRD_IP set. Creates or
updates two records, both DNS only (not proxied by Cloudflare):
  DOMAIN    A      NETBIRD_IP
  *.DOMAIN  CNAME  DOMAIN
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

API = os.environ.get("CF_API", "https://api.cloudflare.com/client/v4")
DOMAIN = os.environ["DOMAIN"]
TOKEN = os.environ["CF_DNS_API_TOKEN"]
NETBIRD_IP = os.environ["NETBIRD_IP"]


def call(method, path, body=None):
    req = urllib.request.Request(
        API + path,
        method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            out = json.load(resp)
    except urllib.error.HTTPError as err:
        try:
            out = json.load(err)
        except ValueError:
            out = {"success": False, "errors": [f"HTTP {err.code}"]}
    if not out.get("success"):
        sys.exit(f"public-dns: Cloudflare {method} {path} failed: {out.get('errors')}")
    return out["result"]


def zone_id():
    # the zone is the longest parent of DOMAIN that the token can see
    labels = DOMAIN.split(".")
    for i in range(len(labels) - 1):
        zones = call("GET", "/zones?name=" + ".".join(labels[i:]))
        if zones:
            return zones[0]["id"]
    sys.exit(f"public-dns: the Cloudflare token sees no zone for {DOMAIN}")


def upsert(zone, name, rtype, content):
    want = {"type": rtype, "name": name, "content": content, "proxied": False,
            "ttl": 300, "comment": "managed by homelab core/setup.sh"}
    records = call("GET", f"/zones/{zone}/dns_records?name=" + urllib.parse.quote(name, safe=""))
    keep = next((r for r in records if r["type"] == rtype), None)
    # anything else answering this name would send devices somewhere else
    for r in records:
        if r is not keep and r["type"] in ("A", "AAAA", "CNAME"):
            call("DELETE", f"/zones/{zone}/dns_records/{r['id']}")
            print(f"  removed {name} {r['type']} {r['content']}")
    if keep is None:
        call("POST", f"/zones/{zone}/dns_records", want)
        print(f"  created {name} {rtype} {content}")
    elif keep["content"] != content or keep["proxied"]:
        call("PUT", f"/zones/{zone}/dns_records/{keep['id']}", want)
        print(f"  updated {name} {rtype} {content} (was {keep['content']})")
    else:
        print(f"  {name} {rtype} {content} already set")


zone = zone_id()
print(f"public DNS for {DOMAIN} -> {NETBIRD_IP} (the Pi's NetBird address)")
upsert(zone, DOMAIN, "A", NETBIRD_IP)
upsert(zone, "*." + DOMAIN, "CNAME", DOMAIN)
