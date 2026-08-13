"""
Standardizes provider names and date formats so the same subscription
doesn't show up under multiple spellings/domains.
"""

PROVIDER_ALIASES = {
    "netflix.com": "Netflix",
    "spotify.com": "Spotify",
    "adobe": "Adobe Creative Cloud",
    "aws": "Amazon Web Services",
    "amazon.com": "Amazon Prime",
    "disneyplus.com": "Disney+",
    "youtube.com": "YouTube Premium",
    "microsoft.com": "Microsoft 365",
    "dropbox.com": "Dropbox",
    "apple.com": "Apple Subscriptions",
    "hulu.com": "Hulu",
    "hbomax.com": "HBO Max",
    "playstation.com": "PlayStation Network",
    "github.com": "GitHub",
}


def normalize_provider(display_name: str, sender_email: str = "") -> str:
    """
    Matches known providers using both the display name and sender email
    (domains catch cases the display name alone would miss), but always
    falls back to a clean title-cased version of just the display name.
    """
    match_key = f"{display_name} {sender_email}".strip().lower()
    for alias_key, canonical in PROVIDER_ALIASES.items():
        if alias_key in match_key:
            return canonical
    return display_name.strip().title()


def normalize_date(d):
    return d.isoformat() if d else None