from epicvibe.config import Settings
from epicvibe.inference.anthropic_provider import AnthropicProvider
from epicvibe.inference.base import FakeProvider, InferenceProvider


def make_provider(settings: Settings) -> InferenceProvider:
    if settings.inference_provider == "anthropic":
        return AnthropicProvider(api_key=settings.anthropic_api_key,
                                 model=settings.anthropic_model,
                                 base_url=settings.anthropic_base_url)
    return FakeProvider({"order_sets": [], "confidence": "low"})
