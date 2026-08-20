"""
Standardizes provider names and date formats so the same subscription
doesn't show up under multiple spellings/domains.
"""

PROVIDER_ALIASES = {
    "netflix": "Netflix",
    "spotify": "Spotify",
    "adobe": "Adobe Creative Cloud",
    "aws": "Amazon Web Services",
    "amazon": "Amazon Prime",
    "disneyplus": "Disney+",
    "youtube": "YouTube Premium",
    "microsoft": "Microsoft 365",
    "dropbox": "Dropbox",
    "apple": "Apple Subscriptions",
    "hulu": "Hulu",
    "hbomax": "HBO Max",
    "max.com": "Max",
    "playstation": "PlayStation Network",
    "github": "GitHub",
    "atlassian": "Atlassian",
    "confluence": "Atlassian Confluence",
    "jira": "Atlassian Jira",
    "grammarly": "Grammarly",
    "inflectra": "Inflectra",
    "openai": "OpenAI",
    "chatgpt": "OpenAI / ChatGPT",
    "anthropic": "Anthropic / Claude",
    "claude": "Anthropic / Claude",
    "midjourney": "Midjourney",
    "notion": "Notion",
    "slack": "Slack",
    "zoom": "Zoom",
    "figma": "Figma",
    "canva": "Canva",
    "cursor": "Cursor",
    "perplexity": "Perplexity AI",
    "1password": "1Password",
    "nordvpn": "NordVPN",
}


def normalize_provider(display_name: str, sender_email: str = "") -> str:
    """
    Matches known providers using both the display name and sender email,
    stripping raw email addresses and subdomains.
    """
    display = (display_name or "").strip()
    sender = (sender_email or "").strip()
    match_key = f"{display} {sender}".lower()

    for alias_key, canonical in PROVIDER_ALIASES.items():
        if alias_key in match_key:
            return canonical

    # If display_name is an email address, clean it up
    if "@" in display:
        domain = display.split("@")[-1]
        parts = domain.split(".")
        if len(parts) >= 2:
            return parts[-2].capitalize()

    return display.title() if display else "Unknown Provider"


def normalize_date(d):
    return d.isoformat() if d else None