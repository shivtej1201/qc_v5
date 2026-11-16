import asyncio
from db import Database

async def test_db():
    db = Database()
    await db.init()
    
    # 1. Simulate CLASSIFYING stage
    url = "https://www.bajajfinserv.in/test-upsert"
    await db.update_classification(url, "PDP", "Business Loan", "HTML_DATA_PAGETYPE")
    
    # Verify
    pt, biz, method = await db.get_classification(url)
    print("After classification:")
    print(f"page_type={pt}, business={biz}, method={method}")
    assert pt == "PDP" and biz == "Business Loan"
    
    # 2. Simulate VALIDATING stage (where http worker returns a status dict without classification)
    result_dict = {
        "normalized_url": url,
        "original_url": url,
        "initial_status_code": 200,
        "final_status_code": 200,
        "status": "WORKING",
        "final_url": url,
        "redirect_count": 0,
        "redirect_chain": "[]",
        "response_time_ms": 100,
        "content_type": "text/html",
        "content_length": 1234,
        "error_type": None,
        "error_message": None,
        "check_method": "GET",
        "browser_verified": False,
        "checked_at": "2026-10-10T00:00:00+00:00"
    }
    await db.batch_insert_results([result_dict])
    
    # Verify again
    pt, biz, method = await db.get_classification(url)
    print("After validation:")
    print(f"page_type={pt}, business={biz}, method={method}")
    assert pt == "PDP" and biz == "Business Loan"
    
    print("DB upsert bug fixed!")

if __name__ == "__main__":
    asyncio.run(test_db())
