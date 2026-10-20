"""The AI layer: provider-agnostic, so a provider is one class.

- base.py: the interface (AIProvider, AIRequest, AIResult) and the kinds of
  failure every provider reports in (ProviderError).
- gemini.py, claude.py: Google's and Anthropic's official SDKs behind it.
- providers.py: which provider and key, through which models, in order.
- runner.py: asking model after model until an answer parses.
- prompting.py: data blocks and scrubbing contacts out of what is sent.
- crypto.py: clubs' own keys, encrypted at rest.

What the features ask, and who may, is in app/api/ai.
"""
