"""Run test_ssrf_adversarial.py in a sandbox with no public DNS: resolve ONLY its public test
hostnames to the addresses real DNS returns (rebinding names -> 127.0.0.1, google -> public IP),
so the SSRF decisions are exercised for the right reason instead of failing on NXDOMAIN."""
import os, runpy, socket, sys
STUB = {"www.google.com": "142.250.183.36", "127.0.0.1.nip.io": "127.0.0.1", "localtest.me": "127.0.0.1"}
_orig = socket.getaddrinfo
def _gai(host, *a, **k):
    if isinstance(host, str) and host.lower() in STUB:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (STUB[host.lower()], 0))]
    return _orig(host, *a, **k)
socket.getaddrinfo = _gai
code_dir = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else ".")
os.chdir(code_dir); sys.path.insert(0, code_dir)
runpy.run_path(os.path.join(code_dir, "tests", "test_ssrf_adversarial.py"), run_name="__main__")
