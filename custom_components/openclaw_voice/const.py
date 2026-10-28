"""Configuration ownership and integration constants."""
from homeassistant.const import Platform

DOMAIN = "openclaw_voice"
PLATFORMS = [Platform.STT, Platform.CONVERSATION, Platform.TTS]
API_PATH = "/plugins/openclaw-home-assistant/v1"
CONF_URL = "url"
CONF_CLIENT_TOKEN = "client_token"
CONF_CLIENT_ID = "client_id"
CONF_SESSION_MODE = "session_mode"
DEFAULT_SESSION_MODE = "persistent"
SESSION_MODES = ("persistent", "per_conversation")
