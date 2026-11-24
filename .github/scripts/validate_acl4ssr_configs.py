"""Validate the fork's four Clash configs before publishing an upstream merge."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
import re
import subprocess
import sys
import time


RAW_ROOT = "https://raw.githubusercontent.com/ACL4SSR/ACL4SSR/master/"
CDN_ROOTS = {
    "jsDelivr": "https://cdn.jsdelivr.net/gh/ACL4SSR/ACL4SSR@master/",
    "GHProxy": "https://gh-proxy.com/" + RAW_ROOT,
    "GHFast": "https://ghfast.top/" + RAW_ROOT,
}
ACTIVE_RAW_RULE = re.compile(
    rb"(?m)^(ruleset=[^,\r\n]+,)"
    + re.escape(RAW_ROOT.encode("ascii"))
    + rb"([^,\s]+)"
)
SAMPLE_ROUTES = {
    "www.adobe.com": "🎨 Adobe",
    "www.google.com": "🌐 Google",
    "mail.google.com": "🌐 Google",
    "flow.google.com": "💬 Ai平台",
    "aisandbox-pa.googleapis.com": "💬 Ai平台",
    "gemini.google.com": "💬 Ai平台",
    "notebook.google.com": "💬 Ai平台",
    "www.youtube.com": "📹 油管视频",
    "mtalk.google.com": "📢 谷歌FCM",
    "dl.google.com": "🎯 全球直连",
}


def git(repo, *args, data=None):
    return subprocess.check_output(["git", "-C", str(repo), *args], input=data)


def validate_groups_and_routes(config, rule_files):
    groups = {}
    rules = []
    for line in config.splitlines():
        key, _, value = line.partition("=")
        if key == "custom_proxy_group":
            group, _, *options = value.split("`")
            if group in groups or len(options) != len(set(options)):
                raise ValueError(f"Duplicate group or option: {group}")
            groups[group] = {option[2:] for option in options if option.startswith("[]")}
        if key == "ruleset":
            group, source = value.split(",", 1)
            if source.startswith("[]"):
                rules.append((group, [source[2:]]))
            elif source.startswith(RAW_ROOT):
                rules.append((group, rule_files[source[len(RAW_ROOT):]]))
            else:
                raise ValueError(f"Unsupported ruleset source: {source}")

    checked = set()

    def check_group(group, active):
        if group in active:
            raise ValueError(f"Circular group reference: {group}")
        if group in checked:
            return
        missing = groups[group] - groups.keys() - {"DIRECT", "REJECT"}
        if missing:
            raise ValueError(f"Unknown options in {group}: {sorted(missing)}")
        for child in groups[group] & groups.keys():
            check_group(child, active | {group})
        checked.add(group)

    for group in groups:
        check_group(group, set())

    domain_rules = []
    for group, entries in rules:
        if group not in groups:
            raise ValueError(f"Ruleset references unknown group: {group}")
        for entry in entries:
            kind, _, value = entry.strip().lower().partition(",")
            if kind in {"domain", "domain-suffix", "domain-keyword"} and value:
                domain_rules.append((group, kind, value))

    for host, expected in SAMPLE_ROUTES.items():
        actual = next(
            (
                group for group, kind, value in domain_rules
                if (kind == "domain" and host == value)
                or (kind == "domain-suffix" and (host == value or host.endswith("." + value)))
                or (kind == "domain-keyword" and value in host)
            ),
            None,
        )
        if actual != expected:
            raise ValueError(f"{host} matched {actual}, expected {expected}")
    print(f"Verified {len(groups)} groups and {len(SAMPLE_ROUTES)} routing samples.")


def known_rule_blobs(repo, path):
    tip = git(repo, "rev-parse", f"upstream/master:{path}").decode().strip()
    history = git(repo, "log", "-8", "--format=%H", "upstream/master", "--", path)
    cached = {
        git(repo, "rev-parse", f"{commit}:{path}").decode().strip()
        for commit in history.decode().splitlines()
    }
    return tip, cached


def verify_download(repo, label, url, tip, cached):
    request = Request(url, headers={"Accept-Encoding": "identity", "User-Agent": "curl/8.7.1"})
    for attempt in range(3):
        try:
            with urlopen(request, timeout=15) as response:
                if response.status != 200 or urlsplit(response.url).hostname != urlsplit(url).hostname:
                    raise ValueError(f"HTTP {response.status}, final URL {response.url}")
                body = response.read(8 * 1024 * 1024 + 1)
                if len(body) > 8 * 1024 * 1024:
                    raise ValueError("Ruleset exceeds the 8 MiB download limit")
            blob = git(repo, "hash-object", "--stdin", data=body).decode().strip()
            if blob == tip:
                return label, False
            if blob in cached:
                print(f"Verified cached upstream content: {url}")
                return label, True
            raise ValueError("Downloaded content does not match a recent upstream version")
        except (OSError, ValueError) as error:
            if attempt == 2:
                raise RuntimeError(f"{url}: {error}") from error
            time.sleep(attempt + 1)


def main(repo):
    source = repo / "Clash/config/ACL4SSR_Online_Full.ini"
    original = source.read_bytes()
    paths = sorted({match[2].decode("ascii") for match in ACTIVE_RAW_RULE.finditer(original)})
    if not paths:
        raise ValueError("No upstream rule links found")
    for name, root in CDN_ROOTS.items():
        file = source.with_name(f"{source.stem}_{name}.ini")
        expected = ACTIVE_RAW_RULE.sub(
            lambda match: match[1] + root.encode("ascii") + match[2], original
        )
        if file.read_bytes() != expected:
            raise ValueError(f"{file.name} differs from the source beyond the CDN URLs")

    rule_files = {
        path: git(repo, "show", f"upstream/master:{path}").decode().splitlines()
        for path in paths
    }
    validate_groups_and_routes(original.decode(), rule_files)

    roots = {"Raw": RAW_ROOT, **CDN_ROOTS}
    known = {path: known_rule_blobs(repo, path) for path in paths}
    with ThreadPoolExecutor(max_workers=12) as workers:
        downloads = [
            workers.submit(verify_download, repo, label, root + path, *known[path])
            for path in paths
            for label, root in roots.items()
        ]
        results = [download.result() for download in downloads]
    for label in roots:
        cached = sum(is_cached for provider, is_cached in results if provider == label)
        print(f"{label}: {len(paths)}/{len(paths)} verified ({cached} cached).")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
