"""
Wraps the Gmail API for searching messages and fetching full message content.
"""

from googleapiclient.discovery import build
from tenacity import retry, wait_exponential, stop_after_attempt

SUBSCRIPTION_QUERIES = [
    # Broad recurring-billing subject terms — single words, not rigid exact
    # phrases, so variations in real-world subject lines still match.
    'subject:(subscription OR renewal OR renew OR "auto-renew" OR "auto renew" '
    'OR membership OR "trial ending" OR "trial ends" OR "billing cycle") '
    '-category:social',

    # Generic payment/billing subject terms. Broader than before, but the
    # one-time-purchase false positives this can catch (e.g. a single
    # Amazon order receipt) are handled downstream by the classifier.
    'subject:(receipt OR invoice OR "payment confirmation" OR "payment successful" '
    'OR "payment received" OR "your bill" OR "billing statement") '
    '-category:social',

    # Known major subscription providers, searched by sender domain rather
    # than subject wording — catches real subscriptions regardless of how
    # that specific provider phrases their subject line.
    'from:(netflix.com OR spotify.com OR adobe.com OR amazon.com OR disneyplus.com '
    'OR youtube.com OR microsoft.com OR dropbox.com OR apple.com OR hulu.com '
    'OR hbomax.com OR playstation.com OR github.com OR google.com OR icloud.com '
    'OR canva.com OR notion.so OR zoom.us OR slack.com '

    # AI / Productivity
    'OR openai.com OR chatgpt.com OR anthropic.com OR claude.ai '
    'OR perplexity.ai OR cursor.com OR grammarly.com OR jasper.ai '
    'OR copy.ai OR midjourney.com OR runwayml.com OR elevenlabs.io '
    'OR character.ai OR poe.com OR lovable.dev OR replit.com '

    # Streaming / Entertainment
    'OR primevideo.com OR paramount.com OR paramountplus.com '
    'OR peacocktv.com OR crunchyroll.com OR discoveryplus.com '
    'OR appletv.com OR max.com OR spotify.com OR tidal.com '
    'OR audible.com OR kindle.com '

    # Gaming
    'OR xbox.com OR microsoft.com OR nintendo.com OR steampowered.com '
    'OR epicgames.com OR ea.com OR ubisoft.com OR riotgames.com '
    'OR battlenet.com OR blizzard.com OR twitch.tv '

    # Cloud Storage / Software
    'OR one.google.com OR drive.google.com OR box.com '
    'OR evernote.com OR todoist.com OR 1password.com '
    'OR lastpass.com OR dashlane.com OR nordvpn.com '
    'OR expressvpn.com OR surfshark.com'

     # Business / Developer / SaaS
    'OR atlassian.com OR jira.com OR confluence.com '
    'OR monday.com OR asana.com OR trello.com '
    'OR hubspot.com OR salesforce.com OR hubspot.net '
    'OR freshbooks.com OR quickbooks.intuit.com OR intuit.com '
    'OR grammarly.com OR loom.com OR calendly.com '
    'OR miro.com OR figma.com OR framer.com '

    # Education
    'OR coursera.org OR udemy.com OR skillshare.com '
    'OR masterclass.com OR duolingo.com OR linkedin.com '

    # Shopping / Memberships
    'OR walmart.com OR costco.com OR ebay.com '
    'OR chewy.com OR instacart.com '

    # News / Publications
    'OR nytimes.com OR washingtonpost.com OR wsj.com '
    'OR economist.com OR medium.com) -category:social',

    # Known billing-system sender address patterns.
    'from:(billing@ OR receipts@ OR invoices@ OR subscriptions@) '
    '-category:social',
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