import os
import yaml
from dotenv import load_dotenv
import logging
from zoneinfo import ZoneInfo

# --- Basic Setup ---

class TaipeiTZFormatter(logging.Formatter):
    def __init__(self, fmt=None, datefmt=None):
        super().__init__(fmt=fmt, datefmt=datefmt)
        self.tz = ZoneInfo("Asia/Taipei")

    def formatTime(self, record, datefmt=None):
        from datetime import datetime
        ct = datetime.fromtimestamp(record.created, tz=self.tz)
        if datefmt:
            s = ct.strftime(datefmt)
        else:
            s = ct.isoformat()
        return s

if not logging.getLogger().handlers:
    handler = logging.StreamHandler()
    formatter = TaipeiTZFormatter(
        fmt='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    handler.setFormatter(formatter)
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.addHandler(handler)

logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# --- Environment & YAML Loading (Immediate) ---

load_dotenv()

# These can be global as they are read directly from the environment at import time
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")
DB_PATH = "data/bot_sessions.db"
gemini_keys_str = os.getenv("GEMINI_API_KEYS")
if gemini_keys_str:
    GEMINI_API_KEYS = [key.strip() for key in gemini_keys_str.split(',') if key.strip()]
else:
    GEMINI_API_KEYS = []
    i = 1
    while True:
        key = os.getenv(f"GEMINI_API_KEY_{i}")
        if key:
            GEMINI_API_KEYS.append(key)
            i += 1
        else:
            break
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
GOOGLE_CSE_ID = os.getenv("GOOGLE_CSE_ID")

# Load YAML config into a private variable
_yaml_config = {}
CONFIG_YAML_PATH = 'config.yaml'
try:
    with open(CONFIG_YAML_PATH, 'r') as f:
        _yaml_config = yaml.safe_load(f)
except FileNotFoundError:
    logger.error(f"Configuration file '{CONFIG_YAML_PATH}' not found.")
except yaml.YAMLError as e:
    logger.error(f"Error parsing YAML configuration file: {e}")

from bot.prompt_loader import prompt_manager
PROMPTS = prompt_manager
logger.info("Prompt manager initialized and attached to config at import time.")

# --- Accessor Functions for All YAML-derived Config Values ---

def get_storage_backend():
    return _yaml_config.get("storage_backend", os.getenv("STORAGE_BACKEND", "database"))

def get_default_provider():
    return _yaml_config.get("default_provider", "ollama")

def get_default_ollama_model():
    return _yaml_config.get("default_ollama_model", "llama3")

def get_default_gemini_model():
    return _yaml_config.get("default_gemini_model", "gemini-1.5-pro-latest")

def get_gemini_max_output_tokens():
    return _yaml_config.get("gemini", {}).get("max_output_tokens", 8192)

def get_default_openrouter_model():
    return _yaml_config.get("default_openrouter_model", "mistralai/mistral-7b-instruct")

def get_gemini_ask_all_models():
    return _yaml_config.get("gemini_ask_all_models", [])

def get_openrouter_allowed_models():
    return _yaml_config.get("openrouter_allowed_models", [])

def get_openrouter_http_referer():
    return _yaml_config.get("openrouter_http_referer", "YOUR_APP_URL_OR_NAME")

def get_web_search_provider():
    return _yaml_config.get("web_search", {}).get("provider", "tavily")

def get_session_file_path():
    return _yaml_config.get("session_file_path", "data/sessions.json")

def get_allowed_chat_ids():
    return _yaml_config.get("allowed_chat_ids", None)

def get_request_timeout_seconds():
    return _yaml_config.get("REQUEST_TIMEOUT_SECONDS", 180)

def get_ollama_request_timeout_seconds():
    # Default to 20m (1200s) for local models if not specified
    return _yaml_config.get("OLLAMA_REQUEST_TIMEOUT_SECONDS", 1200)

def get_default_max_context_tokens():
    return _yaml_config.get("default_max_context_tokens", 3800)

def get_context_token_output_buffer():
    return _yaml_config.get("context_token_output_buffer", 1000)

def get_custom_providers_config():
    return _yaml_config.get("custom_openai_providers", [])

def get_expert_panel_config():
    return _yaml_config.get("expert_panel", {})