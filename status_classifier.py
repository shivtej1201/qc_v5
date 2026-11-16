class StatusClassifier:
    @staticmethod
    def classify(status_code: int) -> str:
        if not status_code:
            return "Failed"
        if 200 <= status_code <= 299:
            return "Working"
        if 300 <= status_code <= 399:
            return "Redirect"
        if status_code == 400:
            return "Bad Request"
        if status_code == 401:
            return "Unauthorized"
        if status_code == 403:
            return "Forbidden"
        if status_code == 404:
            return "Broken URL"
        if status_code == 410:
            return "Gone"
        if status_code == 429:
            return "Rate Limited"
        if 500 <= status_code <= 599:
            return "Server Error"
        return "Unknown"
