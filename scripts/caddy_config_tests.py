"""Startup regression suite for the Caddy front-door config.

Guards the fix for the "blank page under every name but mcapp.local" bug.

Up to Caddyfile.mcapp v2 the site addresses were the hard-coded literal
``mcapp.local`` on both :80 and :443. Caddy matches sites by Host header, so a
request arriving under any *other* name the box answers to — ``<host>.fritz.box``
from a Fritz!Box's DHCP-DNS, ``.home.arpa``/``.lan`` from other routers, a
resolv.conf search domain, the reverse-DNS name, or the bare LAN IP — matched no
site and got Caddy's no-site-matched reply: an **empty 200**. Not a 404, not an
error page; a white page. The installer's own success summary printed the IP URL
unconditionally, so it advertised exactly such a blank page as an access point.

Three invariants are pinned here, one per moving part of the fix:

1. **Template** — :80 is a catch-all (no host matcher) so every name works, and
   :443 carries an explicit multi-name SAN list (a bare :443 has no name to
   issue an internal-CA certificate for, so it cannot be a catch-all). The
   version marker embeds the hostname, so renaming the box counts as stale.
2. **caddy_site()** — must still find the probe target now that the :443 line is
   a comma-separated list; the pre-v3 ``-F:`` parse matched none of it and
   silently fell back to ``$(hostname).local``.
3. **probe_access_point()** — must reject an empty 200. This is the invariant
   most likely to be "simplified" away, because ``curl -f`` looks sufficient
   and is not: it reports success on the exact response that renders blank.

Like scripts/update_runner_tests.py this exercises bootstrap/ shell code rather
than an importable module, so it drives the real functions via subprocess
instead of copying their logic — a copy would drift from what ships.
"""

from __future__ import annotations

import http.server
import re
import shutil
import socket
import subprocess
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

# Resolved to absolute paths up front, matching config_migration_tests.py: the
# suite must not depend on whatever PATH the caller happens to export, and an
# absolute argv[0] is what keeps these subprocess calls honest.
_AWK = shutil.which("awk")
_BASH = shutil.which("bash")

_REPO = Path(__file__).resolve().parent.parent
_TEMPLATE = _REPO / "bootstrap" / "templates" / "caddy" / "Caddyfile.mcapp"
_PACKAGES_SH = _REPO / "bootstrap" / "lib" / "packages.sh"
_HEALTH_SH = _REPO / "bootstrap" / "lib" / "health.sh"

# Placeholder configure_caddy() substitutes with `hostname -s` at install time.
_PLACEHOLDER = "@@HOST@@"

# Suffixes the :443 SAN list must cover. .local is mDNS/avahi (the Apple path),
# .fritz.box is Fritz!Box DHCP-DNS (the common German ham setup), .home.arpa is
# RFC 8375, and .lan / .home are what OpenWrt and friends hand out.
_REQUIRED_TLS_SUFFIXES = (".local", ".fritz.box", ".home.arpa", ".lan", ".home")

_failures: list[str] = []


def _check(ok: bool, label: str) -> bool:
    print(f"{'✅ PASS' if ok else '❌ FAIL'} | {label}")
    if not ok:
        _failures.append(label)
    return ok


def _render(host: str = "mcapp") -> str:
    """Render the template the way configure_caddy() does."""
    return _TEMPLATE.read_text(encoding="utf-8").replace(_PLACEHOLDER, host)


def _site_address_lines(rendered: str) -> list[str]:
    """Lines that open a site block, i.e. non-comment lines ending in '{'.

    Excludes the global options block ('{' alone) and snippet definitions
    ('(name) {'), neither of which is a site address.
    """
    lines = []
    for raw in rendered.splitlines():
        line = raw.strip()
        if not line.endswith("{") or line.startswith(("#", "(")):
            continue
        if line == "{":
            continue
        lines.append(line)
    return lines


# ── 1. template invariants ────────────────────────────────────────────────


def _test_template() -> None:
    template_text = _TEMPLATE.read_text(encoding="utf-8")

    _check(
        _PLACEHOLDER in template_text,
        f"template is parameterised: {_PLACEHOLDER} placeholder present "
        "(hard-coding the hostname is what broke every renamed box)",
    )

    rendered = _render("mcapp")
    _check(
        _PLACEHOLDER not in rendered,
        "rendering substitutes every placeholder occurrence (no @@ left behind)",
    )

    sites = _site_address_lines(rendered)

    http_sites = [s for s in sites if ":80" in s]
    _check(
        http_sites == [":80 {"],
        f"port 80 is a bare catch-all site — every Host header is served, got {http_sites!r}",
    )

    tls_sites = [s for s in sites if ":443" in s]
    _check(
        len(tls_sites) == 1,
        f"exactly one :443 site block, got {len(tls_sites)}",
    )

    tls_line = tls_sites[0] if tls_sites else ""
    for suffix in _REQUIRED_TLS_SUFFIXES:
        _check(
            f"mcapp{suffix}:443" in tls_line,
            f":443 SAN list covers mcapp{suffix} (internal CA issues one leaf per name)",
        )

    _check(
        not re.search(r"^\s*:443\s*\{", rendered, re.MULTILINE),
        ":443 is NOT a catch-all — a nameless TLS site has no certificate to present",
    )

    # The marker is how configure_caddy() decides whether the installed file is
    # stale. Carrying the hostname is the part that makes a rename re-render.
    marker = re.search(r"mcapp-caddy-config-version:\s*(\d+)\s+host=(\S+)", template_text)
    _check(
        marker is not None,
        "version marker carries both version and host= (a rename must count as stale)",
    )

    declared = re.search(
        r"^readonly CADDY_CONFIG_VERSION=(\d+)",
        _PACKAGES_SH.read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    _check(
        marker is not None and declared is not None and marker.group(1) == declared.group(1),
        "template marker version == CADDY_CONFIG_VERSION in packages.sh "
        f"(template={marker.group(1) if marker else '?'}, "
        f"packages.sh={declared.group(1) if declared else '?'}) — a mismatch means "
        "upgrades either never re-render or re-render on every run",
    )


# ── 2. caddy_site() must parse the comma-separated :443 list ──────────────


def _extract_caddy_site_awk() -> str | None:
    """Pull the awk program out of packages.sh so the test can't drift from it."""
    text = _PACKAGES_SH.read_text(encoding="utf-8")
    match = re.search(r"site=\$\(awk '(.*?)' /etc/caddy/Caddyfile", text, re.DOTALL)
    return match.group(1) if match else None


def _test_caddy_site() -> None:
    if not _check(_AWK is not None, "awk is available to run caddy_site()'s parser"):
        return
    program = _extract_caddy_site_awk()
    if not _check(
        program is not None, "caddy_site()'s awk program is extractable from packages.sh"
    ):
        return
    assert program is not None
    assert _AWK is not None

    cases = [
        (
            "mcapp.local:443, mcapp.fritz.box:443, mcapp.home.arpa:443 {",
            "mcapp.local",
            "v3 comma-separated list -> first (the .local name avahi can resolve)",
        ),
        (
            "mcapp.local:443 {",
            "mcapp.local",
            "v2 single-address form still parses (an old file may be on disk)",
        ),
        (
            "{$TLS_HOSTNAME} {",
            "",
            "public-TLS template yields nothing, so caddy_site falls back to config.json",
        ),
        (
            "\t# comment mentioning :443 {\nmcapp.local:443 {",
            "mcapp.local",
            "a comment is not mistaken for a site address",
        ),
    ]

    for source, expected, label in cases:
        result = subprocess.run(  # noqa: S603 - fixed argv, _AWK is a resolved absolute path
            [_AWK, program],
            input=source + "\n",
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        got = result.stdout.strip()
        _check(got == expected, f"caddy_site: {label} (got {got!r}, want {expected!r})")


# ── 3. probe_access_point() must reject the empty 200 ─────────────────────


class _Handler(http.server.BaseHTTPRequestHandler):
    """Serves the two responses that matter, keyed by path."""

    def do_GET(self) -> None:  # BaseHTTPRequestHandler's required name
        if self.path == "/health":
            body = self.server.health_body  # type: ignore[attr-defined]  # set in _serving()
            self.send_response(200)
            # Mirror Caddy's no-site-matched reply: 200, and for the empty case
            # no Content-Type at all — the shape that fools `curl -f`.
            if body:
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args: object) -> None:
        """Silence the default stderr access log."""


@contextmanager
def _serving(body: bytes) -> Iterator[str]:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    server = http.server.HTTPServer(("127.0.0.1", port), _Handler)
    server.health_body = body  # type: ignore[attr-defined]  # read back in do_GET
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _bash(script: str) -> int:
    """Run a bash snippet against health.sh and return its exit status."""
    assert _BASH is not None
    return subprocess.run(  # noqa: S603 - fixed argv, _BASH is a resolved absolute path
        [_BASH, "-c", script],
        capture_output=True,
        timeout=30,
        check=False,
    ).returncode


def _probe(host: str) -> bool:
    """Run the real probe_access_point() out of health.sh."""
    return _bash(f'source "{_HEALTH_SH}"; probe_access_point "{host}" http') == 0


def _test_probe() -> None:
    if not _check(_BASH is not None, "bash is available to source health.sh"):
        return

    # Guard next: a MISSING function also makes bash exit non-zero, which would
    # let "rejects an empty 200" pass for entirely the wrong reason.
    defined = _bash(f'source "{_HEALTH_SH}"; declare -F probe_access_point')
    if not _check(defined == 0, "health.sh defines probe_access_point as a top-level function"):
        return

    with _serving(b'{"status":"healthy"}') as host:
        _check(_probe(host), "probe accepts a real /health JSON response")

    # The regression itself: Caddy's no-site-matched reply.
    with _serving(b"") as host:
        _check(
            not _probe(host),
            "probe REJECTS an empty 200 — the blank page `curl -f` alone calls success",
        )

    # Nothing listening at all.
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        dead = f"127.0.0.1:{probe.getsockname()[1]}"
    _check(not _probe(dead), "probe rejects a closed port")


def run_caddy_config_tests() -> bool:
    """Run the suite. Returns True when every check passed."""
    print("\n🧪 Testing Caddy front-door config (template, caddy_site, probe):")
    _failures.clear()
    _test_template()
    _test_caddy_site()
    _test_probe()
    if _failures:
        print(f"\n  {len(_failures)} check(s) failed:")
        for failure in _failures:
            print(f"    - {failure}")
    return not _failures


if __name__ == "__main__":
    raise SystemExit(0 if run_caddy_config_tests() else 1)
