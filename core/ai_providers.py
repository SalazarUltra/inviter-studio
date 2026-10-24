"""Curated text models, checked against official provider docs on 2026-10-15.

This is a starter list, not an account-specific availability guarantee.
Existing connections and manually entered model IDs remain valid inputs.
"""

CATALOG_CHECKED = "15.10.2026"
PROVIDERS = {
    "openai": {
        "name": "OpenAI",
        "models": ["gpt-6-luna", "gpt-6.1-sol", "gpt-6-astra"],
        "placeholder": "sk-…",
        "keys_url": "https://platform.openai.com/api-keys",
        "docs_url": "https://developers.openai.com/api/docs/models",
        "advice": "Для коротких сообщений начните с gpt-6-luna. Sol и Astra — для более сложных задач.",
        "steps": "Откройте OpenAI Platform → API keys → Create new secret key. Скопируйте ключ сразу. Проверьте API-баланс и лимит расходов: подписка ChatGPT не оплачивает API.",
    },
    "anthropic": {
        "name": "Anthropic (Claude)",
        "models": ["claude-haiku-5-5", "claude-sonnet-5-5"],
        "placeholder": "sk-ant-…",
        "keys_url": "https://platform.claude.com/settings/keys",
        "docs_url": "https://platform.claude.com/docs/en/models/overview",
        "advice": "Haiku — для быстрых коротких сообщений; Sonnet — для более сложного текста.",
        "steps": "Откройте Claude Console → API keys → Create key. Скопируйте ключ и проверьте API-кредиты. Подписка на чат Claude и оплата API оформляются отдельно.",
    },
    "gemini": {
        "name": "Google Gemini",
        "models": ["gemini-3.5-flash-lite", "gemini-3.8-flash"],
        "placeholder": "AIza…",
        "keys_url": "https://aistudio.google.com/apikey",
        "docs_url": "https://ai.google.dev/gemini-api/docs/models",
        "advice": "Flash-Lite — для коротких сообщений; Flash — для более сложных задач.",
        "steps": "Откройте Google AI Studio → Get API key → Create API key. Выберите проект и скопируйте ключ. Доступная квота, оплата и ограничения зависят от проекта и региона.",
    },
    "xai": {
        "name": "xAI (Grok)",
        "models": ["grok-4.7"],
        "placeholder": "xai-…",
        "keys_url": "https://console.x.ai/",
        "docs_url": "https://docs.x.ai/developers/models",
        "advice": "Grok 4.7 — текущая основная текстовая модель. Для коротких сообщений используется низкий уровень рассуждений.",
        "steps": "Откройте консоль провайдера → API keys, создайте и скопируйте ключ. Проверьте API-кредиты и лимиты команды. Подписка на чат Grok не заменяет оплату API.",
    },
}


def normalize_model(provider: str, model: str) -> str:
    value = model.strip()
    if provider == "gemini":
        value = value.removeprefix("models/")
    return value


def validate_connection(name: str, provider: str, api_key: str, model: str) -> tuple[str, str, str]:
    if provider not in PROVIDERS:
        raise ValueError("Выберите AI-провайдера из списка.")
    model = normalize_model(provider, model)
    api_key = api_key.strip()
    if not name.strip():
        raise ValueError("Введите название подключения, например «Основной AI».")
    if not model or any(char.isspace() for char in model) or "://" in model:
        raise ValueError("Укажите ID модели из списка или документации, без пробелов и адреса сайта.")
    if not api_key or any(char.isspace() for char in api_key):
        raise ValueError("Вставьте API-ключ целиком, без пробелов и переносов строк.")
    known_provider = next(
        (
            key
            for prefix, key in (("sk-ant-", "anthropic"), ("AIza", "gemini"), ("xai-", "xai"))
            if api_key.startswith(prefix)
        ),
        None,
    )
    if known_provider and known_provider != provider:
        raise ValueError("Этот ключ похож на ключ другого провайдера. Проверьте выбранного провайдера.")
    if api_key.isdecimal() or (len(api_key) == 32 and all(char in "0123456789abcdefABCDEF" for char in api_key)):
        raise ValueError("Это похоже на Telegram API ID или API Hash. Здесь нужен API-ключ AI-провайдера.")
    return name.strip(), api_key, model
