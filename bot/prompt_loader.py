import os
import logging
import datetime

logger = logging.getLogger(__name__)

def get_environment_context() -> str:
    try:
        import config as _cfg
        tz_conf = _cfg._yaml_config.get('timezone', {})
        offset_hours = tz_conf.get('offset_hours', 0)
        tz_label = tz_conf.get('label', 'UTC')
    except Exception:
        offset_hours = 0
        tz_label = 'UTC'
    tz = datetime.timezone(datetime.timedelta(hours=offset_hours))
    now = datetime.datetime.now(tz)
    date_str = now.strftime('%Y-%m-%d')
    time_str = now.strftime(f'%H:%M:%S {tz_label}')
    return f"\n\n---\n\n# Current Environment\nDate: {date_str}\nTime: {time_str}\n"

class PromptManager:
    def __init__(self, prompt_dir='prompts'):
        self.prompts = {}
        self.prompt_dir = prompt_dir
        self._load_all_prompts()

    def _load_prompt(self, filename: str) -> str:
        """Loads a single prompt file from the prompts directory."""
        try:
            prompt_path = os.path.join(self.prompt_dir, filename)
            with open(prompt_path, 'r', encoding='utf-8') as f:
                return f.read()
        except FileNotFoundError:
            logger.error(f"Prompt file not found: {prompt_path}")
            return ""
        except Exception as e:
            logger.exception(f"Error loading prompt file {prompt_path}: {e}")
            return ""

    def _load_all_prompts(self):
        """Loads all .md files from the prompt directory into the prompts dictionary."""
        if not os.path.isdir(self.prompt_dir):
            logger.error(f"Prompt directory not found: {self.prompt_dir}")
            return

        for filename in os.listdir(self.prompt_dir):
            if filename.endswith('.md'):
                key = filename.upper().replace('.MD', '')
                self.prompts[key] = self._load_prompt(filename)
                if self.prompts[key]:
                    logger.info(f"Successfully loaded prompt: {key}")
                else:
                    logger.warning(f"Failed to load prompt: {key}")

    def get_prompt(self, name: str, inject_environment: bool = True) -> str:
        """Gets a raw prompt template by name, optionally injecting temporal context."""
        prompt_template = self.prompts.get(name.upper())
        if prompt_template:
            if inject_environment:
                return prompt_template + get_environment_context()
            return prompt_template
        raise FileNotFoundError(f"Prompt '{name}' not found.")

# Initialize the prompt manager
prompt_manager = PromptManager()