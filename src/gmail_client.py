"""
Wraps the Gmail API for searching messages and fetching full message content.
"""

from googleapiclient.discovery import build
from tenacity import retry, wait_exponential, stop_after_attempt

SUBSCRIPTION_QUERIES = [
    'subject:(receipt OR invoice OR "payment confirmation" OR "payment receipt") -category:social -category:promotions -category:updates',
    'subject:("your subscription" OR "auto-renew" OR "auto-renewal" OR "trial ending" OR "trial ends") -category:social -category:updates',
    'subject:("renewal confirmation" OR "subscription renewed" OR "your receipt from" OR "payment successful") -category:social -category:updates',
    'from:(billing@ OR receipts@ OR invoices@) -category:social -category:promotions -category:updates',
]


class GmailClient:
    def __init__(self, creds):
        self.service = build("gmail", "v1", credentials=creds)

    @retry(wait=wait_exponential(multiplier=1, min=2, max=30), stop=stop_after_attempt(5))
    def search(self, query: str, max_results: int = 500):
        results, page_token = [], None
        while True:
            resp = self.service.users().messages().list(
                userId="me", q=query, pageToken=page_token, maxResults=100
            ).execute()
            results.extend(resp.get("messages", []))
            page_token = resp.get("nextPageToken")
            if not page_token or len(results) >= max_results:
                break
        return results[:max_results]

    @retry(wait=wait_exponential(multiplier=1, min=2, max=30), stop=stop_after_attempt(5))
    def get_message(self, msg_id: str):
        return self.service.users().messages().get(
            userId="me", id=msg_id, format="full"
        ).execute()

    def get_messages_batch(self, msg_ids: list, batch_size: int = 100):
        """
        Fetches many messages using Gmail's batch HTTP endpoint instead of
        one request per message. Yields (msg_id, message_dict) pairs as
        they complete. Failed individual requests are logged and skipped
        rather than aborting the whole batch.
        """
        results = {}
        errors = {}

        def _callback(request_id, response, exception):
            if exception is not None:
                errors[request_id] = exception
            else:
                results[request_id] = response

        for i in range(0, len(msg_ids), batch_size):
            chunk = msg_ids[i:i + batch_size]
            batch = self.service.new_batch_http_request(callback=_callback)

            for msg_id in chunk:
                batch.add(
                    self.service.users().messages().get(userId="me", id=msg_id, format="full"),
                    request_id=msg_id,
                )

            batch.execute()

            for msg_id in chunk:
                if msg_id in results:
                    yield msg_id, results[msg_id]
                elif msg_id in errors:
                    print(f"[batch error] {msg_id}: {errors[msg_id]}")

            results.clear()
            errors.clear()