import urllib.request
import urllib.error

BASE_URL = "http://127.0.0.1:8002"

def check_endpoint(path: str) -> int:
    url = f"{BASE_URL}{path}"
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception as e:
        print(f"Error checking {url}: {e}")
        return 500

def main():
    print("=" * 60)
    print("TESTING ENDPOINT SECURITY: ACCESS CONTROL & PATH TRAVERSAL")
    print("=" * 60)

    # 1. Path traversal in screenshot filename
    s1 = check_endpoint("/api/artifacts/api_response_audit/12345/screenshots/..%2f..%2fsecret.png")
    assert s1 in [400, 404], f"Expected 400 or 404, got {s1}"
    print(f"  [OK] Path traversal in screenshot filename blocked (HTTP {s1})")

    # 2. Path traversal in screenshot job_id
    s2 = check_endpoint("/api/artifacts/api_response_audit/..%2f..%2fetc/screenshots/test.png")
    assert s2 in [400, 404], f"Expected 400 or 404, got {s2}"
    print(f"  [OK] Path traversal in screenshot job_id blocked (HTTP {s2})")

    # 3. Nonexistent job_id for screenshot
    s3 = check_endpoint("/api/artifacts/api_response_audit/nonexistent-job-uuid-12345/screenshots/test.png")
    assert s3 == 404, f"Expected 404, got {s3}"
    print(f"  [OK] Nonexistent job_id screenshot access blocked (HTTP {s3})")

    # 4. Path traversal in report download job_id
    s4 = check_endpoint("/api/jobs/..%2f..%2fetc/download")
    assert s4 in [400, 404], f"Expected 400 or 404, got {s4}"
    print(f"  [OK] Path traversal in report download blocked (HTTP {s4})")

    # 5. Nonexistent job_id for report download
    s5 = check_endpoint("/api/jobs/nonexistent-job-uuid-12345/download")
    assert s5 == 404, f"Expected 404, got {s5}"
    print(f"  [OK] Nonexistent job_id report download blocked (HTTP {s5})")

    print("\n" + "=" * 60)
    print("ALL ACCESS CONTROL & PATH TRAVERSAL TESTS PASSED!")
    print("=" * 60)

if __name__ == "__main__":
    main()
