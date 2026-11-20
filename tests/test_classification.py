import asyncio
from qcv5.crawler.classifier import Classifier

async def test_classifier():
    c = Classifier()
    
    print("Testing Regex Layer (PDP Loan)")
    p, b, m = await c.classify_url("https://www.bajajfinserv.in/business-loan")
    print(f"Result: PageType={p}, Business={b}, Method={m}")
    assert b == "Business Loan"
    
    print("Testing Regex Layer (PDP Insurance)")
    p, b, m = await c.classify_url("https://www.bajajfinserv.in/health-insurance")
    print(f"Result: PageType={p}, Business={b}, Method={m}")
    assert b == "Insurance"
    
    print("Testing Regex Layer (Home)")
    p, b, m = await c.classify_url("https://www.bajajfinserv.in/")
    print(f"Result: PageType={p}, Business={b}, Method={m}")
    assert p == "Home"
    
    print("Testing Fallback Layer")
    p, b, m = await c.classify_url("https://www.bajajfinserv.in/some-unknown-path")
    print(f"Result: PageType={p}, Business={b}, Method={m}")
    
    print("All tests passed.")

if __name__ == "__main__":
    asyncio.run(test_classifier())
