import os
import yaml
from dotenv import load_dotenv
import logging
from zoneinfo import ZoneInfo

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

# Configure logging with Asia/Taipei timezone
handler = logging.StreamHandler()
formatter = TaipeiTZFormatter(
    fmt='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
handler.setFormatter(formatter)
root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)
root_logger.handlers = [handler]  # Ensure only our handler is used

logging.getLogger("httpx").setLevel(logging.WARNING) # Reduce httpx verbosity
logger = logging.getLogger(__name__)

# Load environment variables from .env file
load_dotenv()

# --- Load settings from environment variables ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434") # Default if not set

# Load multiple Gemini API keys (comma-separated or numbered)
# Example .env: GEMINI_API_KEYS="key1,key2,key3" OR
# GEMINI_API_KEY_1="key1"
# GEMINI_API_KEY_2="key2"
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
# Fallback to single key if multiple not found
if not GEMINI_API_KEYS:
     single_key = os.getenv("GOOGLE_API_KEY") # Keep compatibility with old name
     if single_key:
         GEMINI_API_KEYS.append(single_key)

# --- Load settings from config.yaml ---
CONFIG_YAML_PATH = 'config.yaml'
try:
    with open(CONFIG_YAML_PATH, 'r') as f:
        yaml_config = yaml.safe_load(f)
except FileNotFoundError:
    logger.error(f"Configuration file '{CONFIG_YAML_PATH}' not found.")
    yaml_config = {}
except yaml.YAMLError as e:
    logger.error(f"Error parsing YAML configuration file: {e}")
    yaml_config = {}

# --- Load API Keys from Environment ---
# Note: TELEGRAM_BOT_TOKEN and GEMINI_API_KEYS are already loaded from .env above
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY") # Load OpenRouter Key from .env
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")

# --- Load Provider Defaults ---
DEFAULT_PROVIDER = yaml_config.get("default_provider", "ollama")
DEFAULT_OLLAMA_MODEL = yaml_config.get("default_ollama_model", "llama3") # Fallback default
DEFAULT_GEMINI_MODEL = yaml_config.get("default_gemini_model", "gemini-1.5-pro-latest") # Fallback default
DEFAULT_OPENROUTER_MODEL = yaml_config.get("default_openrouter_model", "mistralai/mistral-7b-instruct") # Load OpenRouter Default

# --- Load Model Lists ---
GEMINI_ASK_ALL_MODELS = yaml_config.get("gemini_ask_all_models", []) # Load the list for ask_all
OPENROUTER_ALLOWED_MODELS = yaml_config.get("openrouter_allowed_models", []) # Load allowed OpenRouter models
OPENROUTER_HTTP_REFERER = yaml_config.get("openrouter_http_referer", "YOUR_APP_URL_OR_NAME")

# --- Load Other Settings ---
SESSION_FILE_PATH = yaml_config.get("session_file_path", "data/sessions.json")
ALLOWED_CHAT_IDS = yaml_config.get("allowed_chat_ids", None) # None means allow all
REQUEST_TIMEOUT_SECONDS = yaml_config.get("REQUEST_TIMEOUT_SECONDS", 180) # Default to 180 seconds if not set
DEFAULT_MAX_CONTEXT_TOKENS = yaml_config.get("default_max_context_tokens", 3800) # Default token limit
CONTEXT_TOKEN_OUTPUT_BUFFER = yaml_config.get("context_token_output_buffer", 1000) # Default to 1000 if not in yaml

# --- Load Custom OpenAI-Compatible Providers ---
CUSTOM_PROVIDERS_CONFIG = []
custom_providers_list = yaml_config.get("custom_openai_providers", [])
if isinstance(custom_providers_list, list):
    for provider_config in custom_providers_list:
        if isinstance(provider_config, dict) and 'name' in provider_config and 'base_url' in provider_config and 'default_model' in provider_config:
            provider_name = provider_config['name']
            api_key_env_var = f"{provider_name.upper()}_API_KEY"
            api_key = os.getenv(api_key_env_var)
            if api_key:
                CUSTOM_PROVIDERS_CONFIG.append({
                    "name": provider_name,
                    "base_url": provider_config['base_url'],
                    "api_key": api_key,
                    "default_model": provider_config['default_model'],
                    "allowed_models": provider_config.get('allowed_models', []) # Optional
                })
            else:
                logger.warning(f"API key environment variable '{api_key_env_var}' not found for custom provider '{provider_name}'. This provider will be disabled.")
        else:
            logger.warning(f"Invalid configuration format for an item in 'custom_openai_providers': {provider_config}. Skipping.")
else:
     logger.warning("'custom_openai_providers' in config.yaml is not a list. No custom providers loaded.")


# --- Validate essential configurations ---
if not TELEGRAM_BOT_TOKEN or TELEGRAM_BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN":
    logger.warning("TELEGRAM_BOT_TOKEN is not set or is the default placeholder in .env. The bot will not work.")
    # In a real scenario, you might want to raise an error or exit
    # raise ValueError("TELEGRAM_BOT_TOKEN is not configured.")

if not GEMINI_API_KEYS or any(key == "YOUR_GEMINI_KEY_1" for key in GEMINI_API_KEYS): # Check against the example placeholder
    logger.warning("No valid Gemini API keys found or default placeholder used in .env. Gemini features may be disabled or limited.")

if not OPENROUTER_API_KEY or OPENROUTER_API_KEY == "YOUR_OPENROUTER_API_KEY":
    logger.warning("OPENROUTER_API_KEY not set or is the default placeholder in .env. OpenRouter features will be disabled.")

# Log loaded config for verification (optional, remove sensitive keys in production)
logger.info("Configuration loaded:")
logger.info(f"  DEFAULT_PROVIDER: {DEFAULT_PROVIDER}")
logger.info(f"  DEFAULT_OLLAMA_MODEL: {DEFAULT_OLLAMA_MODEL}")
logger.info(f"  DEFAULT_GEMINI_MODEL: {DEFAULT_GEMINI_MODEL}")
logger.info(f"  DEFAULT_OPENROUTER_MODEL: {DEFAULT_OPENROUTER_MODEL}") # Log OpenRouter default
logger.info(f"  Tavily API Key Loaded: {'Yes' if TAVILY_API_KEY else 'No'}")
logger.info(f"  GEMINI_ASK_ALL_MODELS: {GEMINI_ASK_ALL_MODELS}")
logger.info(f"  OPENROUTER_ALLOWED_MODELS: {OPENROUTER_ALLOWED_MODELS}") # Log allowed OpenRouter models
logger.info(f"  OPENROUTER_HTTP_REFERER: {OPENROUTER_HTTP_REFERER}")
logger.info(f"  OLLAMA_HOST: {OLLAMA_HOST}")
logger.info(f"  SESSION_FILE_PATH: {SESSION_FILE_PATH}")
logger.info(f"  ALLOWED_CHAT_IDS: {ALLOWED_CHAT_IDS}")
logger.info(f"  REQUEST_TIMEOUT_SECONDS: {REQUEST_TIMEOUT_SECONDS}")
logger.info(f"  Number of Gemini Keys loaded: {len(GEMINI_API_KEYS)}")
logger.info(f"  Number of Custom Providers loaded: {len(CUSTOM_PROVIDERS_CONFIG)}")
logger.info(f"  CONTEXT_TOKEN_OUTPUT_BUFFER: {CONTEXT_TOKEN_OUTPUT_BUFFER}")
# Avoid logging tokens/keys directly in production logs
# logger.info(f"  TELEGRAM_BOT_TOKEN: {'Set' if TELEGRAM_BOT_TOKEN else 'Not Set'}")
# for provider in CUSTOM_PROVIDERS_CONFIG:
#     logger.info(f"  Custom Provider '{provider['name']}': Base URL='{provider['base_url']}', Default Model='{provider['default_model']}', Key Set={'Yes' if provider['api_key'] else 'No'}")
