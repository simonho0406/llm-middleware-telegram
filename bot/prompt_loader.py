import os
import logging

logger = logging.getLogger(__name__)

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

    def get_prompt(self, name: str) -> str | None:
        """Gets a raw prompt template by name."""
        prompt_template = self.prompts.get(name.upper())
        if prompt_template:
            return prompt_template
        raise FileNotFoundError(f"Prompt '{name}' not found.")

# Initialize the prompt manager
prompt_manager = PromptManager()