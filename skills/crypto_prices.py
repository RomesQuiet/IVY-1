# skills/crypto_prices.py — example skill (Ivy can write files like this herself).
import requests

from ivy.toolkit import tool


@tool(coins="Comma-separated coin names or symbols, e.g. 'bitcoin, eth, solana'")
def crypto_prices(coins: str = "bitcoin") -> str:
    """Live cryptocurrency prices in USD and EUR with the 24-hour change."""
    ids = []
    for name in (c.strip() for c in coins.split(",") if c.strip()):
        hits = requests.get("https://api.coingecko.com/api/v3/search",
                            params={"query": name}, timeout=8).json().get("coins", [])
        if hits:
            ids.append(hits[0]["id"])
    if not ids:
        return f"No coins match '{coins}'."
    data = requests.get("https://api.coingecko.com/api/v3/simple/price", timeout=8,
                        params={"ids": ",".join(ids), "vs_currencies": "usd,eur",
                                "include_24hr_change": "true"}).json()
    return "\n".join(
        f"{coin}: ${p['usd']:,} / €{p['eur']:,} ({p.get('usd_24h_change', 0):+.1f}% 24h)"
        for coin, p in data.items()
    )
