import os
import yaml
import logging
from typing import Dict, List, Any

logger = logging.getLogger(__name__)

class SkillRegistryService:
    def __init__(self, skills_dir: str = "skills"):
        self.skills_dir = skills_dir
        self.skills: Dict[str, Dict[str, Any]] = {}  # skill_name -> parsed dictionary

    def load_skills(self):
        """
        Scans self.skills_dir for subdirectories containing SKILL.md.
        Parses YAML frontmatter, extracts parameters and descriptions,
        and stores metadata and natural-language playbook separately.
        """
        if not os.path.exists(self.skills_dir):
            logger.warning(f"Skills directory '{self.skills_dir}' does not exist. Creating it.")
            os.makedirs(self.skills_dir, exist_ok=True)
            return

        self.skills.clear()
        
        for item in os.listdir(self.skills_dir):
            item_path = os.path.join(self.skills_dir, item)
            if os.path.isdir(item_path):
                skill_md_path = os.path.join(item_path, "SKILL.md")
                if os.path.exists(skill_md_path):
                    try:
                        with open(skill_md_path, 'r', encoding='utf-8') as f:
                            content = f.read()
                        
                        # Markdown files with frontmatter start/delimit with '---'
                        # We split by '---' to isolate YAML frontmatter and playbook body
                        parts = content.split("---")
                        if len(parts) >= 3:
                            # Split might have an empty first string if file starts with '---'
                            # parts[0] is typically '', parts[1] is frontmatter, parts[2] is body
                            frontmatter_str = parts[1].strip()
                            playbook_body = "---".join(parts[2:]).strip()
                            
                            metadata = yaml.safe_load(frontmatter_str)
                            if not isinstance(metadata, dict):
                                logger.error(f"Invalid YAML frontmatter in '{skill_md_path}': parsed as {type(metadata)}")
                                continue
                                
                            name = metadata.get("name")
                            description = metadata.get("description")
                            
                            if not name or not description:
                                logger.error(f"Malformed skill metadata in '{skill_md_path}': 'name' and 'description' are required.")
                                continue
                                
                            self.skills[name] = {
                                "metadata": metadata,
                                "playbook": playbook_body,
                                "file_path": skill_md_path
                            }
                            logger.info(f"Successfully loaded skill '{name}' from '{skill_md_path}'")
                        else:
                            logger.error(f"Skill file '{skill_md_path}' is missing YAML frontmatter delimiters '---'")
                    except Exception as e:
                        logger.error(f"Failed to load skill from '{skill_md_path}': {e}", exc_info=True)

    def get_skills_as_tools(self) -> List[Dict[str, Any]]:
        """
        Formats each skill's metadata into standard OpenAI Tool Calling schema.
        The description should clearly invite the LLM to call the skill.
        """
        tools = []
        for name, skill_data in self.skills.items():
            meta = skill_data["metadata"]
            
            # Use default empty parameters schema if none provided
            parameters = meta.get("parameters", {
                "type": "object",
                "properties": {}
            })
            
            tool = {
                "type": "function",
                "function": {
                    "name": f"skill_{name}",
                    "description": meta["description"],
                    "parameters": parameters
                }
            }
            tools.append(tool)
        return tools

    def get_skill_playbook(self, skill_name: str) -> str:
        """Retrieves the full Markdown body of the skill (deferred execution context)."""
        skill = self.skills.get(skill_name)
        if not skill:
            return f"[Error: Skill '{skill_name}' not found.]"
        return skill["playbook"]
