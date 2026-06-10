import logging
import asyncio
import base64
import config
import json
import random
from typing import List, Dict, Optional, AsyncGenerator, Any
from google import genai
from google.genai import types
from google.genai import errors as google_exceptions

logger = logging.getLogger(__name__)

def map_json_schema_to_gemini(schema_dict: dict) -> types.Schema:
    if not schema_dict:
        return None
        
    schema_type = schema_dict.get("type", "object").upper()
    properties = {}
    for k, v in schema_dict.get("properties", {}).items():
        properties[k] = map_json_schema_to_gemini(v)
        
    items = None
    if "items" in schema_dict:
        items = map_json_schema_to_gemini(schema_dict["items"])
        
    return types.Schema(
        type=schema_type,
        description=schema_dict.get("description"),
        properties=properties or None,
        required=schema_dict.get("required"),
        items=items
    )

def translate_openai_tools_to_gemini(openai_tools: list) -> list:
    if not openai_tools:
        return None
    function_declarations = []
    for tool in openai_tools:
        if tool.get("type") == "function":
            func = tool.get("function", {})
            name = func.get("name")
            description = func.get("description")
            parameters = func.get("parameters")
            
            schema_obj = None
            if parameters:
                try:
                    schema_obj = map_json_schema_to_gemini(parameters)
                except Exception as e:
                    logger.warning(f"Failed to parse parameters to types.Schema: {e}.")
                    schema_obj = parameters

            fd = types.FunctionDeclaration(
                name=name,
                description=description,
                parameters=schema_obj
            )
            function_declarations.append(fd)
            
    if function_declarations:
        return [types.Tool(function_declarations=function_declarations)]
    return None

def _strip_unsigned_tool_call_turns(full_prompt: list) -> list:
    """
    Remove legacy tool-call turns that lack a thought_signature so Gemini
    doesn't reject the request with 400 INVALID_ARGUMENT. Also removes the
    immediately-following function_response turn to keep the history consistent.
    """
    unsigned_indices = set()
    for idx, c in enumerate(full_prompt):
        parts = getattr(c, 'parts', None) or []
        has_fc = any(getattr(p, 'function_call', None) for p in parts)
        has_sig = any(
            getattr(p, 'thought_signature', None)
            for p in parts if getattr(p, 'function_call', None)
        )
        if getattr(c, 'role', None) == 'model' and has_fc and not has_sig:
            unsigned_indices.add(idx)
            # Also strip the immediately-following user function_response turn
            if idx + 1 < len(full_prompt):
                next_parts = getattr(full_prompt[idx + 1], 'parts', None) or []
                if (getattr(full_prompt[idx + 1], 'role', None) == 'user'
                        and any(getattr(p, 'function_response', None) for p in next_parts)):
                    unsigned_indices.add(idx + 1)
    return [c for idx, c in enumerate(full_prompt) if idx not in unsigned_indices]


class GeminiService:
    def __init__(self, api_keys: Optional[List[str]] = None):
        """Initializes the Gemini service with a list of API keys for rate-limit rotation."""
        self.api_keys = api_keys if api_keys is not None else config.GEMINI_API_KEYS
        # Cache one genai.Client per API key. Each client wraps an httpx/grpc
        # connection pool — constructing a new one per request leaks sockets
        # (the SDK has no synchronous finalizer). Persistent clients also keep
        # the pool warm, removing connect-startup latency on each call.
        self._clients: Dict[str, "genai.Client"] = {}
        if not self.api_keys:
            logger.warning("GeminiService initialized with no API keys.")

    def _get_client(self, api_key: str) -> "genai.Client":
        """Lazily create-and-cache a genai.Client for the given API key."""
        client = self._clients.get(api_key)
        if client is None:
            client = genai.Client(api_key=api_key)
            self._clients[api_key] = client
        return client

    async def close(self) -> None:
        """Drop cached genai.Client instances on bot shutdown / polling restart.

        google-genai's Client does not expose an async close; the best we can
        do is drop our refs so GC can reclaim them. CRITICAL: must clear the
        dict so the next polling-loop iteration creates fresh clients bound
        to the new event loop (mirrors providers.py provider-cache reset).
        """
        self._clients.clear()
        logger.info("GeminiService client cache cleared.")

    async def generate_response(self, model: str, prompt: str, context_history: Optional[List[Dict]] = None, request_timeout: int = None, tools: list = None) -> AsyncGenerator[str, None]:
        """Generates a streaming response using instance-scoped clients."""
        if not self.api_keys:
            yield "[Error: Gemini API keys not configured]"
            return

        # Format history for v2 SDK
        gemini_history = []
        if context_history:
            for msg in context_history:
                role = msg.get("role")
                content = msg.get("content")
                
                if role == "user":
                    gemini_history.append(types.Content(
                        role="user",
                        parts=[types.Part(text=content or "")]
                    ))
                elif role == "assistant":
                    parts = []
                    if content:
                        parts.append(types.Part(text=content))
                    
                    if "tool_calls" in msg:
                        for tc in msg["tool_calls"]:
                            args = tc.get("function", {}).get("arguments", {})
                            if isinstance(args, str):
                                try:
                                    args = json.loads(args)
                                except Exception:
                                    args = {"arguments": args}
                            # Restore thought_signature (stored as base64) so Gemini's
                            # thinking models can validate multi-turn continuity.
                            sig_b64 = tc.get("gemini_thought_signature")
                            thought_sig_bytes = base64.b64decode(sig_b64) if sig_b64 else None
                            parts.append(types.Part(
                                function_call=types.FunctionCall(
                                    name=tc.get("function", {}).get("name"),
                                    args=args
                                ),
                                thought_signature=thought_sig_bytes
                            ))
                    gemini_history.append(types.Content(
                        role="model",
                        parts=parts
                    ))
                elif role == "tool":
                    name = msg.get("name")
                    if not name and msg.get("tool_call_id"):
                        for h_msg in context_history:
                            if h_msg.get("role") == "assistant" and "tool_calls" in h_msg:
                                for tc in h_msg["tool_calls"]:
                                    if tc.get("id") == msg.get("tool_call_id"):
                                        name = tc.get("function", {}).get("name")
                                        break
                    if not name:
                        name = "tool"
                    try:
                        resp_dict = json.loads(content)
                        if not isinstance(resp_dict, dict):
                            resp_dict = {"result": content}
                    except Exception:
                        resp_dict = {"result": content}
                        
                    part = types.Part(
                        function_response=types.FunctionResponse(
                            name=name,
                            response=resp_dict
                        )
                    )
                    gemini_history.append(types.Content(
                        role="user",
                        parts=[part]
                    ))
                else:
                    gemini_role = 'user' if role == 'user' else 'model'
                    gemini_history.append(types.Content(
                        role=gemini_role,
                        parts=[types.Part(text=content or "")]
                    ))
        
        full_prompt = list(gemini_history)
        if prompt:
            full_prompt.append(types.Content(
                role="user",
                parts=[types.Part(text=prompt)]
            ))
            
        translated_tools = translate_openai_tools_to_gemini(tools)
        generation_config = types.GenerateContentConfig(
            max_output_tokens=config.get_gemini_max_output_tokens(),
            tools=translated_tools
        )

        thought_sig_stripped = False

        # Allow one retry pass: normal first, then with legacy unsigned tool-call turns removed.
        for _pass in range(2):
            for i, key in enumerate(self.api_keys):
                try:
                    logger.info(f"Attempting Gemini request with Key Index: {i}")
                    client = self._get_client(key)

                    response_stream = await client.aio.models.generate_content_stream(
                        model=model,
                        contents=full_prompt,
                        config=generation_config
                    )

                    tool_calls = []
                    async for chunk in response_stream:
                        if chunk.candidates and chunk.candidates[0].content and chunk.candidates[0].content.parts:
                            for part in chunk.candidates[0].content.parts:
                                if part.function_call:
                                    name = part.function_call.name
                                    args = part.function_call.args
                                    args_str = json.dumps(args) if isinstance(args, (dict, list)) else str(args or "{}")
                                    tc_id = f"call_{random.randint(100000, 999999)}"
                                    tc_entry = {
                                        "id": tc_id,
                                        "type": "function",
                                        "function": {
                                            "name": name,
                                            "arguments": args_str
                                        }
                                    }
                                    # Preserve thought_signature (lives on Part, not FunctionCall)
                                    # so it can be restored when replaying this turn in future requests.
                                    if part.thought_signature:
                                        tc_entry["gemini_thought_signature"] = base64.b64encode(
                                            part.thought_signature
                                        ).decode("ascii")
                                    tool_calls.append(tc_entry)

                        if hasattr(chunk, 'text') and chunk.text:
                            yield chunk.text
                        elif chunk.candidates and chunk.candidates[0].finish_reason:
                            reason = chunk.candidates[0].finish_reason
                            reason_str = str(reason)
                            if reason_str in ('STOP', 'FINISH_REASON_UNSPECIFIED', '1'):
                                pass  # Normal completion
                            elif 'MAX_TOKENS' in reason_str or reason_str in ('2',):
                                # Soft truncation: partial content was already streamed; just warn.
                                logger.warning(f"Gemini response truncated at token limit (Key Index: {i}). Partial content returned.")
                            else:
                                # Safety block, recitation, or other hard stop — flag as error.
                                logger.warning(f"Gemini content blocked (Key Index: {i}, Reason: {reason_str})")
                                yield f"[Error: Content blocked by Gemini - {reason_str}]"
                                return

                    if tool_calls:
                        yield json.dumps({"tool_calls": tool_calls})

                    logger.info(f"Gemini stream finished successfully with Key Index: {i}")
                    return  # success

                except google_exceptions.APIError as e:
                    if "429" in str(e) or "quota" in str(e).lower() or "exhausted" in str(e).lower():
                        logger.warning(f"Gemini key at index {i} is rate-limited, trying next key. Reason: {e}")
                        continue
                    elif "thought_signature" in str(e) and not thought_sig_stripped:
                        # Legacy tool-call turns in history lack thought_signature.
                        # Strip them (and their paired function_response turns) and retry once.
                        thought_sig_stripped = True
                        full_prompt = _strip_unsigned_tool_call_turns(full_prompt)
                        logger.warning(
                            f"Gemini rejected request due to missing thought_signature on legacy "
                            f"tool-call turns (Key Index: {i}). Stripped unsigned turns, retrying..."
                        )
                        break  # break key loop → go to next _pass
                    else:
                        logger.exception(f"A non-recoverable Gemini error occurred with Key Index {i} (Model: {model}): {e}")
                        yield f"[Error: A critical error occurred with the Gemini API: {e}]"
                        return
                except Exception as e:
                    if "429" in str(e) or "exhausted" in str(e).lower():
                        logger.warning(f"Gemini key at index {i} is rate-limited, trying next key. Reason: {e}")
                        continue
                    logger.exception(f"An unexpected Gemini error occurred with Key Index {i} (Model: {model}): {e}")
                    yield f"[Error: A critical error occurred with the Gemini API: {e}]"
                    return
            else:
                # for-loop completed normally (all keys exhausted, no break)
                logger.error("All Gemini API keys are rate-limited or failing.")
                yield "[Error: All Gemini API keys are currently rate-limited or failing.]"
                return

            # Reached here only when key loop broke due to thought_sig strip → retry next pass
            if not thought_sig_stripped:
                break

        if thought_sig_stripped:
            logger.error("Gemini failed even after stripping legacy tool-call turns.")
            yield "[Error: Gemini rejected the conversation history. Start a fresh conversation with /new.]"

    async def list_models(self) -> List[Dict[str, Any]]:
        """Lists available Gemini models using the first working key."""
        if not self.api_keys:
            logger.warning("Cannot list Gemini models: No API keys configured.")
            return []

        for i, key in enumerate(self.api_keys):
            try:
                client = self._get_client(key)
                # v2 SDK: client.models.list()
                models_iter = await asyncio.to_thread(client.models.list)
                
                generative_models = [
                    {"id": m.name.split('/')[-1] if '/' in m.name else m.name, "name": m.display_name}
                    for m in models_iter
                    if 'generateContent' in m.supported_actions
                ]
                logger.info(f"Successfully listed {len(generative_models)} models with Key Index: {i}.")
                return generative_models
            except Exception as e:
                logger.exception(f"Failed to list models with Key Index {i}: {e}")
                continue
        
        logger.error("Failed to list models with any of the provided Gemini keys.")
        return []

    async def check_status(self) -> tuple[bool, str]:
        """Checks the status of the Gemini API by verifying key configuration and attempting to list models."""
        if not self.api_keys:
            return False, "Not configured (missing API keys)"
            
        try:
            models = await self.list_models()
            if models:
                return True, f"Online ({len(models)} models available, {len(self.api_keys)} keys active)"
            else:
                return False, "Offline (Failed to connect or list models)"
        except Exception as e:
            return False, f"Error connecting: {e}"

    async def generate_concurrent_responses(self, prompt: str, context_history: Optional[List[Dict]] = None) -> Dict[str, str]:
        """Generates responses from multiple configured Gemini models concurrently."""
        if not self.api_keys:
            return {"error": "[Error: Gemini API keys not configured]"}
        
        ask_models = config.get_gemini_ask_all_models()
        if not ask_models:
            return {"error": "[Error: No models configured for concurrent generation]"}

        working_key = None
        for i, key in enumerate(self.api_keys):
            try:
                client = self._get_client(key)
                await asyncio.to_thread(client.models.list)
                logger.info(f"Found working Gemini key at index {i} for concurrent requests.")
                working_key = key
                break
            except Exception:
                continue
        
        if not working_key:
            logger.exception("No working Gemini key found for concurrent requests.")
            return {model: "[Error: No available API keys]" for model in ask_models}

        # Reuse the pooled client for this batch (cached per key, not per call)
        batch_client = self._get_client(working_key)
        
        full_prompt = []
        if context_history:
            for msg in context_history:
                role = 'user' if msg.get('role') == 'user' else 'model'
                content = msg.get('content', '')
                full_prompt.append({'role': role, 'parts': [{'text': content}]})
        full_prompt.append({'role': 'user', 'parts': [{'text': prompt}]})
        
        generation_config = types.GenerateContentConfig(
             max_output_tokens=config.get_gemini_max_output_tokens()
        )

        async def _concurrent_task(model_name: str) -> str:
            try:
                response = await asyncio.wait_for(
                    batch_client.aio.models.generate_content(
                        model=model_name,
                        contents=full_prompt,
                        config=generation_config
                    ),
                    timeout=config.get_request_timeout_seconds()
                )
                return response.text.strip() if hasattr(response, 'text') and response.text else "[Empty Response]"
            except Exception as e:
                logger.exception(f"Error during concurrent Gemini generation for model {model_name}: {e}")
                return f"[Error: {e}]"

        tasks = [_concurrent_task(model) for model in ask_models]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        return {model: (res if not isinstance(res, Exception) else f"[Exception: {res}]") 
                for model, res in zip(ask_models, results)}

    async def _generate_single_model_non_streaming(self, model_id: str, prompt: str, context_history: Optional[List[Dict]] = None) -> str:
        """Internal helper to generate a response from a single model non-streamingly."""
        full_response = ""
        try:
            async for chunk in self.generate_response(model=model_id, prompt=prompt, context_history=context_history):
                full_response += chunk
            return full_response.strip()
        except Exception as e:
            logger.exception(f"Error in _generate_single_model_non_streaming for {model_id}: {e}")
            return f"[Error: {e}]"