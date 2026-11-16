import asyncio
import argparse
import sys
from config import settings
from engine import AuditEngine

def main():
    parser = argparse.ArgumentParser(description="High-Speed Link Auditor")
    parser.add_argument("--input", required=True, help="Input Excel file")
    parser.add_argument("--output", default="report.xlsx", help="Output Excel file")
    parser.add_argument("--concurrency", type=int, help="HTTP Concurrency (override)")
    parser.add_argument("--browser-fallback", type=bool, help="Enable Playwright fallback")
    parser.add_argument("--force-recheck", action="store_true", help="Ignore SQLite cache")
    
    args = parser.parse_args()
    
    if args.concurrency:
        settings.HTTP_CONCURRENCY = args.concurrency
    if args.browser_fallback is not None:
        settings.BROWSER_FALLBACK = args.browser_fallback
    if args.force_recheck:
        settings.CACHE_ENABLED = False
        
    engine = AuditEngine(args.input, args.output)
    
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        
    try:
        asyncio.run(engine.start())
    except KeyboardInterrupt:
        print("\nProcess interrupted. Results in DB are saved.")

if __name__ == "__main__":
    main()
