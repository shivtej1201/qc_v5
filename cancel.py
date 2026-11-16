import httpx
jobs = httpx.get('http://127.0.0.1:8002/api/jobs').json()['jobs']
for job in jobs:
    if job['status'] in ['RUNNING', 'QUEUED']:
        print('Cancelling', job['id'])
        httpx.post(f"http://127.0.0.1:8002/api/jobs/{job['id']}/cancel")
