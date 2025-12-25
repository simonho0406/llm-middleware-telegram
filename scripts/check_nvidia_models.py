import asyncio
import os
from services.openai_compatible_service import OpenAICompatibleService

# Mock config - assumes env var is set or user has it
# We need to read the API key from the environment var which might not be set in this context?
# Actually, the app runs fine, so the env var is likely in .env or environment.
# But I cannot access .env directly easily if it's not loaded.
# I'll try to load it or just assume I can't run this easily without the key.
# Wait, I can import `config` module which loads .env!

from dotenv import load_dotenv
load_dotenv()

async def list_nvidia_models():
    nvidia_config = {
        "name": "nvidia",
        "base_url": "https://integrate.api.nvidia.com/v1",
        "api_key": os.getenv("CUSTOM_PROVIDER_API_KEY_NVIDIA"),
        "default_model": "test"
    }
    
    if not nvidia_config["api_key"]:
        print("Error: CUSTOM_PROVIDER_API_KEY_NVIDIA not found in environment.")
        return

    service = OpenAICompatibleService(nvidia_config)
    models = await service.list_models()
    
    print(f"Found {len(models)} models.")
    deepseek_models = [m for m in models if "deepseek" in m.lower()]
    print("DeepSeek Models on Nvidia:")
    for m in deepseek_models:
        print(f" - {m}")

if __name__ == "__main__":
    asyncio.run(list_nvidia_models())
