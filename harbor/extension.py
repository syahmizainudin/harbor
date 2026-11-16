"""Harbor's Chrome extension (in ../extension).

Its manifest carries a fixed public key, so Chrome gives it the same ID wherever it's loaded.
Harbor accepts requests from exactly that origin by default (``HARBOR_EXTENSION_ORIGINS``
changes this). Browsers set ``Origin`` themselves, so web pages and other extensions can't
claim it. ``tests/test_extension.py`` checks that this ID matches the manifest's key.
"""

EXTENSION_ID = "pioghlmpnbcbgiemaphgpnendofahggb"
EXTENSION_ORIGIN = f"chrome-extension://{EXTENSION_ID}"
